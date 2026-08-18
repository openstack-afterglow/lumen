"""외부 API 키 저장소 — 발급/해시/검증/폐기 (MySQL, chat_api_keys).

보안:
- 키 평문은 발급 시 1회만 반환하고 저장하지 않는다. DB엔 SHA-256 해시만 저장한다.
- 검증은 요청 키를 sha256 해시해 조회 후 `hmac.compare_digest` 로 타이밍 안전 비교(CLAUDE.md §4).
- 키 CRUD는 owner_user_id/owner_project_id 소유권 검증(IDOR, CLAUDE.md §5).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from lumen.db import get_session_factory, is_db_available, mark_db_unhealthy
from lumen.models.chat_db import ChatApiKey

logger = logging.getLogger(__name__)

_KEY_PREFIX = "sk-afgl-"  # 발급 키 접두(식별·표시용)

DEFAULT_API_KEY_SCOPES = ("models:read", "compat:completions:write")
API_KEY_SCOPES = frozenset(
    {
        "models:read",
        "compat:completions:write",
        "native:conversations:read",
        "native:conversations:write",
        "native:runs:read",
        "native:runs:write",
        "native:extensions:read",
        "native:extensions:write",
        "native:tools:execute",
        "native:memory:read",
        "native:memory:write",
        "native:agents:use",
        "usage:read",
    }
)


def _valid_scopes(scopes: object) -> tuple[str, ...] | None:
    if not isinstance(scopes, list) or not scopes:
        return None
    if any(not isinstance(scope, str) or scope not in API_KEY_SCOPES for scope in scopes):
        return None
    if len(set(scopes)) != len(scopes):
        return None
    return tuple(scopes)


class ApiKeyStorageUnavailable(RuntimeError):
    """chat DB 미구성/장애."""


class ApiKeyNotFound(LookupError):
    """대상 미존재 — 404."""


class ApiKeyForbidden(PermissionError):
    """소유자 불일치 — 403."""


def _require_db():
    if not is_db_available():
        raise ApiKeyStorageUnavailable("chat DB 를 사용할 수 없습니다")
    factory = get_session_factory()
    if factory is None:
        raise ApiKeyStorageUnavailable("chat DB 가 구성되지 않았습니다")
    return factory


def _hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _iso(dt) -> str | None:
    return dt.isoformat() if dt else None


def _public(row: ChatApiKey) -> dict:
    """조회용 — 시크릿 없이 표시 정보만(key_prefix 로 어떤 키인지 식별)."""
    return {
        "id": row.id,
        "name": row.name,
        "key_prefix": row.key_prefix,
        "scopes": list(row.scopes),
        "is_active": row.is_active,
        "last_used_at": _iso(row.last_used_at),
        "created_at": _iso(row.created_at),
        "revoked_at": _iso(row.revoked_at),
    }


async def create_key(user_id: str, project_id: str, name: str, scopes: list[str]) -> dict:
    """새 API 키 발급. 반환 dict 의 `key` 는 평문(1회만 노출) — 이후 조회 불가."""
    validated_scopes = _valid_scopes(scopes)
    if validated_scopes is None:
        raise ValueError("API 키 scope가 올바르지 않습니다")
    raw = _KEY_PREFIX + secrets.token_urlsafe(32)
    key_prefix = raw[: len(_KEY_PREFIX) + 4]  # 예: sk-afgl-AbCd
    row = ChatApiKey(
        owner_user_id=user_id,
        owner_project_id=project_id,
        name=(name or "").strip()[:100],
        key_prefix=key_prefix,
        key_hash=_hash_key(raw),
        scopes=list(validated_scopes),
        is_active=True,
    )
    factory = _require_db()
    try:
        async with factory() as session, session.begin():
            session.add(row)
            await session.flush()
            pub = _public(row)
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ApiKeyStorageUnavailable("chat DB 오류") from exc
    pub["key"] = raw  # 평문 — 1회만
    return pub


async def list_keys(user_id: str, project_id: str) -> list[dict]:
    """본인 API 키 목록(시크릿 없음). 저장소 장애 시 예외."""
    factory = _require_db()
    try:
        async with factory() as session:
            rows = (
                (
                    await session.execute(
                        select(ChatApiKey)
                        .where((ChatApiKey.owner_user_id == user_id) & (ChatApiKey.owner_project_id == project_id))
                        .order_by(ChatApiKey.id.desc())
                    )
                )
                .scalars()
                .all()
            )
            return [_public(r) for r in rows]
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ApiKeyStorageUnavailable("chat DB 오류") from exc


async def revoke_key(key_id: int, user_id: str, project_id: str) -> None:
    """키 폐기(비활성) — 소유자만. 원장 보존 위해 삭제 대신 is_active=False."""
    factory = _require_db()
    try:
        async with factory() as session, session.begin():
            row = await session.get(ChatApiKey, key_id)
            if row is None:
                raise ApiKeyNotFound(f"{key_id} 를 찾을 수 없습니다")
            if row.owner_user_id != user_id or row.owner_project_id != project_id:
                raise ApiKeyForbidden("소유자가 아닙니다")
            row.is_active = False
            row.revoked_at = datetime.now(UTC)
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ApiKeyStorageUnavailable("chat DB 오류") from exc


async def verify_key(raw: str) -> dict | None:
    """API 키 평문 → {user_id, project_id, api_key_id} | None.

    fail-closed: DB 미가용/오류/미존재/비활성/불일치는 모두 None(호출자가 401 처리).
    """
    if not raw or not raw.startswith(_KEY_PREFIX):
        return None
    if not is_db_available():
        return None
    factory = get_session_factory()
    if factory is None:
        return None
    computed = _hash_key(raw)
    try:
        async with factory() as session:
            row = (
                await session.execute(select(ChatApiKey).where(ChatApiKey.key_hash == computed))
            ).scalar_one_or_none()
            if row is None or not row.is_active:
                return None
            # 조회는 해시로 했지만 타이밍 안전 비교를 명시(CLAUDE.md §4).
            if not hmac.compare_digest(row.key_hash, computed):
                return None
            scopes = _valid_scopes(row.scopes)
            if scopes is None:
                return None
            info = {
                "user_id": row.owner_user_id,
                "project_id": row.owner_project_id,
                "api_key_id": row.id,
                "scopes": scopes,
            }
            # last_used_at 갱신(best-effort — 실패해도 인증은 성공).
            try:
                row.last_used_at = datetime.now(UTC)
                await session.commit()
            except Exception:
                await session.rollback()
            return info
    except OperationalError:
        mark_db_unhealthy()
        return None
    except Exception:
        logger.warning("API 키 검증 실패", exc_info=True)
        return None
