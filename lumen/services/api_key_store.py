"""외부 API 키 저장소 — 발급/해시/검증/폐기 (MySQL, chat_api_keys).

보안:
- 키 평문은 발급 시 1회만 반환하고 저장하지 않는다. DB엔 SHA-256 해시만 저장한다.
- 검증은 요청 키를 sha256 해시해 조회 후 `hmac.compare_digest` 로 타이밍 안전 비교(CLAUDE.md §4).
- 키 CRUD는 owner_user_id/owner_project_id 소유권 검증(IDOR, CLAUDE.md §5).
"""

import hashlib
import hmac
import logging
import secrets
from collections.abc import Iterable
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from lumen.config import get_settings
from lumen.db import get_session_factory, is_db_available, mark_db_unhealthy
from lumen.models.chat_db import ChatApiKey, ChatUsageLog, UserWallet

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


class ApiKeyLimitConflict(ValueError):
    """한도 설정 충돌 — 409."""

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


def _validate_monthly_credit_limit(val: Decimal | str | float | int | None) -> Decimal | None:
    if val is None:
        return None
    if isinstance(val, str) and "e" in val.lower():
        raise ValueError("월 한도는 지수 표기법(scientific notation)을 사용할 수 없습니다")
    if isinstance(val, float) and "e" in str(val).lower():
        raise ValueError("월 한도는 지수 표기법(scientific notation)을 사용할 수 없습니다")
    try:
        dec = val if isinstance(val, Decimal) else Decimal(str(val))
    except Exception as exc:
        raise ValueError("월 한도는 올바른 숫자여야 합니다") from exc
    if not dec.is_finite() or dec <= 0:
        raise ValueError("월 한도는 0보다 큰 유한한 숫자여야 합니다")
    if dec >= Decimal("10000000000"):
        raise ValueError("월 한도는 10,000,000,000 미만이어야 합니다")
    normalized = dec.normalize()
    sign, digits, exp = normalized.as_tuple()
    if isinstance(exp, int) and exp < 0 and -exp > 8:
        raise ValueError("월 한도는 소수점 이하 유효 자릿수가 8자리 이하여야 합니다")
    return dec


def _dec_str(val: Decimal | float | str | None) -> str | None:
    if val is None:
        return None
    return format(Decimal(str(val)), "f")


def calculate_effective_limit(
    owner_limit: Decimal | None,
    admin_limit: Decimal | None,
    system_quota: Decimal | None,
) -> Decimal | None:
    candidates: list[Decimal] = []
    if owner_limit is not None:
        candidates.append(owner_limit)
    if admin_limit is not None:
        candidates.append(admin_limit)
    if system_quota is not None and system_quota > 0:
        candidates.append(system_quota)
    return min(candidates) if candidates else None


def _month_start(dt: datetime | None = None) -> datetime:
    if dt is None:
        dt = datetime.now(UTC)
    return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


async def _get_system_quotas_batch(session, user_ids: Iterable[str]) -> dict[str, Decimal]:
    u_set = set(user_ids)
    if not u_set:
        return {}
    default_quota = Decimal(str(get_settings().chat_default_monthly_quota))
    res = {uid: default_quota for uid in u_set}
    stmt = select(UserWallet.user_id, UserWallet.max_quota_monthly).where(UserWallet.user_id.in_(u_set))
    rows = (await session.execute(stmt)).all()
    for uid, max_q in rows:
        if max_q is not None:
            res[uid] = Decimal(str(max_q))
    return res


async def _get_system_quota(session, user_id: str) -> Decimal:
    res = await _get_system_quotas_batch(session, [user_id])
    return res.get(user_id, Decimal(str(get_settings().chat_default_monthly_quota)))


async def _get_month_credited_costs_batch(session, key_ids: Iterable[int], start_dt: datetime) -> dict[int, Decimal]:
    k_set = set(key_ids)
    if not k_set:
        return {}
    res = {kid: Decimal("0") for kid in k_set}
    stmt = (
        select(ChatUsageLog.api_key_id, func.coalesce(func.sum(ChatUsageLog.credited_cost), Decimal("0")))
        .where((ChatUsageLog.api_key_id.in_(k_set)) & (ChatUsageLog.created_at >= start_dt))
        .group_by(ChatUsageLog.api_key_id)
    )
    rows = (await session.execute(stmt)).all()
    for kid, total in rows:
        if kid is not None and total is not None:
            res[kid] = Decimal(str(total))
    return res


async def _get_month_credited_cost(session, key_id: int, start_dt: datetime) -> Decimal:
    res = await _get_month_credited_costs_batch(session, [key_id], start_dt)
    return res.get(key_id, Decimal("0"))


def _public(
    row: ChatApiKey,
    system_quota: Decimal | None = None,
    month_usage: Decimal | None = None,
) -> dict:
    """조회용 — 시크릿 없이 표시 정보만(key_prefix 로 어떤 키인지 식별)."""
    eff = calculate_effective_limit(
        row.owner_monthly_credit_limit,
        row.admin_monthly_credit_limit,
        system_quota,
    )
    return {
        "id": row.id,
        "name": row.name,
        "key_prefix": row.key_prefix,
        "scopes": list(row.scopes),
        "owner_monthly_credit_limit": _dec_str(row.owner_monthly_credit_limit),
        "admin_monthly_credit_limit": _dec_str(row.admin_monthly_credit_limit),
        "system_monthly_credit_limit": _dec_str(system_quota),
        "effective_monthly_credit_limit": _dec_str(eff),
        "month_credited_cost": _dec_str(month_usage if month_usage is not None else Decimal("0")),
        "is_active": row.is_active,
        "last_used_at": _iso(row.last_used_at),
        "created_at": _iso(row.created_at),
        "revoked_at": _iso(row.revoked_at),
    }


def _public_admin(
    row: ChatApiKey,
    system_quota: Decimal | None = None,
    month_usage: Decimal | None = None,
) -> dict:
    pub = _public(row, system_quota=system_quota, month_usage=month_usage)
    pub["owner_user_id"] = row.owner_user_id
    pub["owner_project_id"] = row.owner_project_id
    return pub


async def create_key(
    user_id: str,
    project_id: str,
    name: str,
    scopes: list[str],
    monthly_credit_limit: Decimal | None = None,
) -> dict:
    """새 API 키 발급. 반환 dict 의 `key` 는 평문(1회만 노출) — 이후 조회 불가."""
    validated_scopes = _valid_scopes(scopes)
    if validated_scopes is None:
        raise ValueError("API 키 scope가 올바르지 않습니다")
    limit = _validate_monthly_credit_limit(monthly_credit_limit)
    raw = _KEY_PREFIX + secrets.token_urlsafe(32)
    key_prefix = raw[: len(_KEY_PREFIX) + 4]  # 예: sk-afgl-AbCd
    row = ChatApiKey(
        owner_user_id=user_id,
        owner_project_id=project_id,
        name=(name or "").strip()[:100],
        key_prefix=key_prefix,
        key_hash=_hash_key(raw),
        scopes=list(validated_scopes),
        owner_monthly_credit_limit=limit,
        is_active=True,
    )
    factory = _require_db()
    try:
        async with factory() as session, session.begin():
            system_quota = await _get_system_quota(session, user_id)
            if limit is not None and system_quota > 0 and limit > system_quota:
                raise ApiKeyLimitConflict("API 키 월 한도는 시스템 월 쿼터를 초과할 수 없습니다")
            session.add(row)
            await session.flush()
            pub = _public(row, system_quota=system_quota, month_usage=Decimal("0"))
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
            system_quota = await _get_system_quota(session, user_id)
            usages = await _get_month_credited_costs_batch(session, [r.id for r in rows], _month_start())
            return [_public(r, system_quota=system_quota, month_usage=usages.get(r.id, Decimal("0"))) for r in rows]
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ApiKeyStorageUnavailable("chat DB 오류") from exc


async def update_owner_limits(
    key_id: int,
    user_id: str,
    project_id: str,
    monthly_credit_limit: Decimal | None,
) -> dict:
    """소유자의 키 월 한도 수정/해제."""
    limit = _validate_monthly_credit_limit(monthly_credit_limit)
    factory = _require_db()
    try:
        async with factory() as session, session.begin():
            row = await session.get(ChatApiKey, key_id)
            if row is None:
                raise ApiKeyNotFound(f"{key_id} 를 찾을 수 없습니다")
            if row.owner_user_id != user_id or row.owner_project_id != project_id:
                raise ApiKeyForbidden("소유자가 아닙니다")
            system_quota = await _get_system_quota(session, user_id)
            if limit is not None:
                if row.admin_monthly_credit_limit is not None and limit > row.admin_monthly_credit_limit:
                    raise ApiKeyLimitConflict("API 키 월 한도는 관리자 한도를 초과할 수 없습니다")
                if system_quota > 0 and limit > system_quota:
                    raise ApiKeyLimitConflict("API 키 월 한도는 시스템 월 쿼터를 초과할 수 없습니다")
            row.owner_monthly_credit_limit = limit
            await session.flush()
            month_usage = await _get_month_credited_cost(session, row.id, _month_start())
            return _public(row, system_quota=system_quota, month_usage=month_usage)
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ApiKeyStorageUnavailable("chat DB 오류") from exc


async def list_keys_admin(
    owner_user_id: str | None = None,
    owner_project_id: str | None = None,
    before_id: int | None = None,
    limit: int = 50,
) -> dict:
    """관리자용 전체 API 키 목록 커서 페이지."""
    limit = max(1, min(200, limit))
    factory = _require_db()
    try:
        async with factory() as session:
            stmt = select(ChatApiKey)
            if owner_user_id:
                stmt = stmt.where(ChatApiKey.owner_user_id == owner_user_id)
            if owner_project_id:
                stmt = stmt.where(ChatApiKey.owner_project_id == owner_project_id)
            if before_id is not None:
                stmt = stmt.where(ChatApiKey.id < before_id)
            stmt = stmt.order_by(ChatApiKey.id.desc()).limit(limit + 1)

            rows = (await session.execute(stmt)).scalars().all()
            has_more = len(rows) > limit
            page_rows = rows[:limit] if has_more else rows
            next_before_id = page_rows[-1].id if has_more and page_rows else None

            key_ids = [r.id for r in page_rows]
            user_ids = {r.owner_user_id for r in page_rows}

            quotas = await _get_system_quotas_batch(session, user_ids)
            usages = await _get_month_credited_costs_batch(session, key_ids, _month_start())

            items = [
                _public_admin(r, system_quota=quotas.get(r.owner_user_id), month_usage=usages.get(r.id, Decimal("0")))
                for r in page_rows
            ]
            return {"items": items, "next_before_id": next_before_id}
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ApiKeyStorageUnavailable("chat DB 오류") from exc


async def update_admin_limits(
    key_id: int,
    monthly_credit_limit: Decimal | None,
) -> dict:
    """관리자의 키 월 ceiling 수정/해제."""
    limit = _validate_monthly_credit_limit(monthly_credit_limit)
    factory = _require_db()
    try:
        async with factory() as session, session.begin():
            row = await session.get(ChatApiKey, key_id)
            if row is None:
                raise ApiKeyNotFound(f"{key_id} 를 찾을 수 없습니다")
            system_quota = await _get_system_quota(session, row.owner_user_id)
            if limit is not None:
                if system_quota > 0 and limit > system_quota:
                    raise ApiKeyLimitConflict("API 키 월 한도는 시스템 월 쿼터를 초과할 수 없습니다")
                row.admin_monthly_credit_limit = limit
                if row.owner_monthly_credit_limit is not None and row.owner_monthly_credit_limit > limit:
                    row.owner_monthly_credit_limit = limit
            else:
                row.admin_monthly_credit_limit = None
            await session.flush()
            month_usage = await _get_month_credited_cost(session, row.id, _month_start())
            return _public_admin(row, system_quota=system_quota, month_usage=month_usage)
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
