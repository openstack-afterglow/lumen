"""빌트인 AI 채팅 확장(MCP 서버 / 커스텀 HTTP 툴) 관리 저장소 (MySQL).

스코프 모델:
- scope='global' (owner_* NULL): 관리자만 생성/수정/삭제, 모든 사용자에게 적용.
- scope='user' (owner_user_id/owner_project_id): 소유자만 생성/수정/삭제, 본인에게만 적용.

⚠️ IDOR: user 스코프 수정/삭제는 (owner_user_id, owner_project_id)가 요청자와 일치할 때만 허용.
사용자는 목록에서 자신의 것 + 활성 global 만 본다.
"""

from __future__ import annotations

import hashlib
import json
import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError

from lumen.db import get_session_factory, is_db_available, mark_db_unhealthy
from lumen.models.chat_db import ChatCustomTool, ChatMcpOAuthConnection, ChatMcpServer, ChatSkill
from lumen.services import ssrf
from lumen.crypto import (
    decrypt_chat_content,
    decrypt_llm_provider_key,
    encrypt_chat_content,
    encrypt_llm_provider_key,
)

logger = logging.getLogger(__name__)

_MODELS = {"mcp": ChatMcpServer, "tool": ChatCustomTool, "skill": ChatSkill}

# MCP supports only HTTPS streamable HTTP. Local process and legacy SSE transports are rejected.
_MCP_TRANSPORTS = ("http",)

_AUTH_MODES = ("none", "oauth", "admin")
_LOAD_POLICIES = ("preloaded", "on_demand")
_USER_MCP_FORBIDDEN_FIELDS = frozenset(
    {"headers", "auth_mode", "oauth_scopes", "oauth_client_id", "oauth_client_secret"}
)


def _ensure_user_mcp_policy(fields: dict) -> None:
    if _USER_MCP_FORBIDDEN_FIELDS & fields.keys():
        raise ExtensionValidationError("MCP 인증 설정은 관리자 승인 후에만 사용할 수 있습니다")


def _mcp_auth_mode(row: ChatMcpServer) -> str:
    """Classify legacy rows as administrator-gated rather than restoring user secret injection."""
    mode = getattr(row, "auth_mode", None)
    if mode in _AUTH_MODES:
        return mode
    return "admin" if getattr(row, "encrypted_headers", None) or getattr(row, "headers", None) else "none"


class ChatStorageUnavailable(RuntimeError):
    """chat DB 미구성/장애 — 503."""


class ExtensionNotFound(LookupError):
    """대상 미존재 — 404."""


class ExtensionForbidden(PermissionError):
    """소유자 불일치/스코프 위반 — 403."""


class ExtensionValidationError(ValueError):
    """입력 검증 실패 — 400."""


class ExtensionSecretUnavailable(RuntimeError):
    """An encrypted extension secret cannot be safely used."""


def _require_db():
    if not is_db_available():
        raise ChatStorageUnavailable("chat DB 를 사용할 수 없습니다")
    factory = get_session_factory()
    if factory is None:
        raise ChatStorageUnavailable("chat DB 가 구성되지 않았습니다")
    return factory


def _iso(dt) -> str | None:
    return dt.isoformat() if dt else None


def _decrypt_headers(row: ChatMcpServer, *, fail_closed: bool = False) -> dict:
    """Restore MCP headers, refusing execution when an encrypted value is invalid."""
    if row.encrypted_headers:
        try:
            data = json.loads(decrypt_llm_provider_key(row.encrypted_headers))
            if not isinstance(data, dict):
                raise ValueError("decrypted MCP headers are not an object")
            return data
        except Exception as exc:
            logger.warning("MCP 헤더 복호 실패 id=%s", row.id, exc_info=True)
            if fail_closed:
                raise ExtensionSecretUnavailable("MCP 인증 헤더를 복호화할 수 없습니다") from exc
            return {}
    if isinstance(row.headers, dict):  # 레거시 plaintext
        return row.headers
    return {}


def _mask_headers(headers: dict) -> dict:
    """헤더 값은 시크릿(Authorization 토큰 등)일 수 있으므로 키만 노출하고 값은 가린다."""
    return {k: "••••••" for k in headers}


def _load_policy(row: ChatMcpServer | ChatCustomTool) -> str:
    policy = getattr(row, "load_policy", None)
    return policy if policy in _LOAD_POLICIES else "on_demand"


def _public_mcp(row: ChatMcpServer) -> dict:
    # Header values remain write-only and are exposed only as administrator configuration state.
    headers = _decrypt_headers(row)
    return {
        "id": row.id,
        "scope": row.scope,
        "name": row.name,
        "transport": row.transport,
        "url": row.url,
        "has_headers": bool(row.encrypted_headers) or bool(headers),
        "auth_mode": _mcp_auth_mode(row),
        "oauth_scopes": _clean_oauth_scopes(getattr(row, "oauth_scopes", None)),
        "has_oauth_client": bool(getattr(row, "oauth_client_id", None)),
        "has_oauth_client_secret": bool(getattr(row, "encrypted_oauth_client_secret", None)),
        "is_active": row.is_active,
        "load_policy": _load_policy(row),
        "effect_overrides": row.tool_effect_overrides,
        "config_version": row.config_version,
        "created_at": _iso(row.created_at),
    }


def _reveal_mcp(row: ChatMcpServer) -> dict:
    """Execution-only representation. Static secrets flow only in administrator mode."""
    auth_mode = _mcp_auth_mode(row)
    return {
        "id": row.id,
        "name": row.name,
        "transport": row.transport,
        "url": row.url,
        "headers": _decrypt_headers(row, fail_closed=True)
        if auth_mode == "admin" and getattr(row, "scope", None) == "global" and bool(row.encrypted_headers)
        else {},
        "auth_mode": auth_mode,
        "oauth_scopes": _clean_oauth_scopes(getattr(row, "oauth_scopes", None)),
        "is_active": row.is_active,
        "effect_overrides": row.tool_effect_overrides,
        "config_version": row.config_version,
        "load_policy": _load_policy(row),
    }


def _clean_oauth_scopes(raw) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [
        scope
        for item in raw[:32]
        if isinstance(item, str)
        and 1 <= len(scope := item.strip()) <= 128
        and not any(char.isspace() for char in scope)
    ]


def _public_tool(row: ChatCustomTool) -> dict:
    return {
        "id": row.id,
        "scope": row.scope,
        "name": row.name,
        "description": row.description,
        "method": row.method,
        "url": row.url,
        "params_schema": row.params_schema,
        "timeout_seconds": row.timeout_seconds,
        "is_active": row.is_active,
        "effect": row.effect,
        "config_version": row.config_version,
        "load_policy": _load_policy(row),
        "created_at": _iso(row.created_at),
    }


def _public_skill(row: ChatSkill) -> dict:
    return {
        "id": row.id,
        "scope": row.scope,
        "name": row.name,
        "description": row.description,
        "instructions": decrypt_chat_content(row.instructions) if row.instructions else "",
        "is_active": row.is_active,
        "created_at": _iso(row.created_at),
    }


def selection_fingerprint(item: dict) -> str:
    """Return a secret-free configuration identity suitable for encrypted run payloads."""
    material = {
        key: item.get(key)
        for key in (
            "id",
            "name",
            "url",
            "method",
            "transport",
            "auth_mode",
            "params_schema",
            "config_version",
            "effect",
            "load_policy",
        )
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


async def mcp_credential_versions(
    server_ids: list[int] | tuple[int, ...], *, user_id: str, project_id: str
) -> dict[int, int | None]:
    """Return the active OAuth credential epoch for the selected MCP sources."""
    ids = [server_id for server_id in server_ids if isinstance(server_id, int)]
    if not ids:
        return {}
    factory = _require_db()
    try:
        async with factory() as session:
            servers = {
                row.id: row
                for row in (await session.execute(select(ChatMcpServer).where(ChatMcpServer.id.in_(ids)))).scalars()
            }
            oauth = {
                row.mcp_server_id: row
                for row in (
                    await session.execute(
                        select(ChatMcpOAuthConnection).where(
                            (ChatMcpOAuthConnection.mcp_server_id.in_(ids))
                            & (ChatMcpOAuthConnection.owner_user_id == user_id)
                            & (ChatMcpOAuthConnection.owner_project_id == project_id)
                        )
                    )
                ).scalars()
            }
            return {
                server_id: (
                    connection.credential_version
                    if (
                        connection
                        and connection.status == "active"
                        and connection.server_config_version == server.config_version
                        and server.is_active
                    )
                    else None
                )
                for server_id, server in servers.items()
                if _mcp_auth_mode(server) == "oauth"
                for connection in [oauth.get(server_id)]
            }
    except OperationalError as exc:
        raise ChatStorageUnavailable("chat DB 오류") from exc


async def validated_frozen_selection(
    snapshot: object, *, user_id: str, project_id: str
) -> tuple[tuple[int, ...], tuple[int, ...], list[str]]:
    """Re-check a queued run's exact active configuration without widening selection."""
    if not isinstance(snapshot, dict):
        return (), (), ["run extension selection is unavailable"]
    selected_by_kind = {"tool": snapshot.get("tools"), "mcp": snapshot.get("mcp")}
    selected_mcp = selected_by_kind["mcp"]
    mcp_ids = (
        [item["id"] for item in selected_mcp if isinstance(item, dict) and isinstance(item.get("id"), int)]
        if isinstance(selected_mcp, list)
        else []
    )
    credential_versions = await mcp_credential_versions(mcp_ids, user_id=user_id, project_id=project_id)
    resolved: dict[str, tuple[int, ...]] = {"tool": (), "mcp": ()}
    warnings: list[str] = []
    for kind, selected in selected_by_kind.items():
        if not isinstance(selected, list):
            warnings.append(f"run {kind} selection is invalid")
            continue
        visible = await list_for_user(kind, user_id=user_id, project_id=project_id, active_only=True)
        current = {item["id"]: item for item in visible if isinstance(item.get("id"), int)}
        ids: list[int] = []
        for item in selected:
            if not isinstance(item, dict) or not isinstance(item.get("id"), int):
                warnings.append(f"run {kind} selection is invalid")
                continue
            now = current.get(item["id"])
            if now is None or item.get("config_fingerprint") != selection_fingerprint(now):
                warnings.append(f"selected {kind} changed or was revoked")
                continue
            if kind == "mcp":
                expected_version = item.get("credential_version")
                current_version = credential_versions.get(item["id"], 0)
                if not isinstance(expected_version, int) or expected_version != current_version:
                    warnings.append("selected MCP credential changed or was revoked")
                    continue
            ids.append(item["id"])
        resolved[kind] = tuple(ids)
    return resolved["tool"], resolved["mcp"], warnings


def frozen_selection_fingerprints(
    snapshot: object,
    selected_tool_ids: tuple[int, ...],
    selected_mcp_ids: tuple[int, ...],
) -> tuple[tuple[str, int, str], ...]:
    """Return the exact validated configuration identities for a durable run."""
    if not isinstance(snapshot, dict):
        return ()
    selected = {"tool": set(selected_tool_ids), "mcp": set(selected_mcp_ids)}
    fingerprints: list[tuple[str, int, str]] = []
    for kind, ids in selected.items():
        entries = snapshot.get(f"{kind}s" if kind == "tool" else kind)
        if not isinstance(entries, list):
            continue
        for item in entries:
            if not isinstance(item, dict):
                continue
            identifier = item.get("id")
            fingerprint = item.get("config_fingerprint")
            if identifier in ids and isinstance(identifier, int) and isinstance(fingerprint, str):
                fingerprints.append((kind, identifier, fingerprint))
    return tuple(fingerprints)


_PUBLIC = {"mcp": _public_mcp, "tool": _public_tool, "skill": _public_skill}


def _model(kind: str):
    m = _MODELS.get(kind)
    if m is None:
        raise ExtensionValidationError(f"알 수 없는 확장 종류: {kind}")
    return m


def _apply_fields(kind: str, row, fields: dict) -> None:
    """kind 별 허용 필드만 row 에 반영(화이트리스트). 소유권/스코프 필드는 여기서 다루지 않음."""
    if kind == "mcp":
        if "name" in fields and fields["name"]:
            row.name = str(fields["name"]).strip()
        if "transport" in fields and fields["transport"]:
            if fields["transport"] not in _MCP_TRANSPORTS:
                raise ExtensionValidationError("transport 는 http(streamable HTTP)만 지원합니다")
            row.transport = fields["transport"]
        if "url" in fields:
            url = (fields["url"] or "").strip()
            if url:
                if not url.lower().startswith("https://"):
                    raise ExtensionValidationError("url 은 https:// 로 시작하는 원격 주소여야 합니다")
                try:
                    ssrf.validate_url(url)
                except ssrf.SsrfBlocked as exc:
                    raise ExtensionValidationError("url 은 공개 HTTPS 주소여야 합니다") from exc
            row.url = url or None
        if "headers" in fields:
            # 헤더 값은 인증 시크릿(Bearer/API key)일 수 있으므로 AES-256-GCM 으로 암호화 저장.
            headers = fields["headers"]
            if headers is None:
                pass  # 미지정(field 없음과 동일 취급) = 기존 유지
            elif not isinstance(headers, dict):
                raise ExtensionValidationError("headers 는 문자열 값의 객체여야 합니다")
            elif not headers:
                # 명시적 빈 dict = 전체 삭제.
                row.encrypted_headers = None
                row.headers = None
            else:
                # 기존 헤더 위에 per-key overlay: 마스킹 sentinel(• 만) 값은 기존 유지, 실제 값은 교체/추가.
                # (마스킹 값 재전송으로 다른 헤더가 유실되지 않도록.)
                merged = dict(_decrypt_headers(row, fail_closed=True))
                for k, v in headers.items():
                    if not isinstance(k, str) or not isinstance(v, str):
                        raise ExtensionValidationError("headers 는 문자열 값의 객체여야 합니다")
                    key = k.strip()
                    if (
                        not key
                        or len(key) > 128
                        or len(v) > 4096
                        or any(char in key for char in "\r\n:")
                        or "\r" in v
                        or "\n" in v
                    ):
                        raise ExtensionValidationError("MCP header 형식이 올바르지 않습니다")
                    if set(v) == {"•"}:  # 마스킹 표시 = 변경 없음
                        continue
                    if v:  # 실제 값 = 교체/추가
                        merged[key] = v
                if merged:
                    row.encrypted_headers = encrypt_llm_provider_key(json.dumps(merged, ensure_ascii=False))
                    row.headers = None  # 레거시 plaintext 컬럼은 비운다
                else:
                    row.encrypted_headers = None
                    row.headers = None
        if "auth_mode" in fields and fields["auth_mode"] is not None:
            mode = str(fields["auth_mode"]).strip().lower()
            if mode not in _AUTH_MODES:
                raise ExtensionValidationError("auth_mode 는 none|oauth|admin 만 지원합니다")
            row.auth_mode = mode
        if "headers" in fields and (getattr(row, "auth_mode", "none") != "admin"):
            raise ExtensionValidationError("정적 MCP 인증 헤더는 관리자 승인(auth_mode=admin)이 필요합니다")
        if (
            fields.get("oauth_scopes") or fields.get("oauth_client_id") or fields.get("oauth_client_secret")
        ) and getattr(row, "auth_mode", "none") != "oauth":
            raise ExtensionValidationError("OAuth 설정에는 auth_mode=oauth 가 필요합니다")
        if "oauth_scopes" in fields:
            raw_scopes = fields["oauth_scopes"]
            if raw_scopes is None:
                row.oauth_scopes = None
            elif isinstance(raw_scopes, list):
                scopes = _clean_oauth_scopes(raw_scopes)
                if len(scopes) != len(raw_scopes) or len(set(scopes)) != len(scopes):
                    raise ExtensionValidationError("oauth_scopes 형식이 올바르지 않습니다")
                row.oauth_scopes = scopes or None
            else:
                raise ExtensionValidationError("oauth_scopes 는 scope 목록이어야 합니다")
        if "oauth_client_id" in fields:
            client_id = str(fields["oauth_client_id"] or "").strip()
            if len(client_id) > 512 or "\r" in client_id or "\n" in client_id:
                raise ExtensionValidationError("OAuth client ID 형식이 올바르지 않습니다")
            row.oauth_client_id = client_id or None
        if "oauth_client_secret" in fields:
            secret = fields["oauth_client_secret"]
            if secret is None:
                pass
            elif not isinstance(secret, str):
                raise ExtensionValidationError("OAuth client secret 형식이 올바르지 않습니다")
            elif secret:
                row.encrypted_oauth_client_secret = encrypt_llm_provider_key(secret)
            else:
                row.encrypted_oauth_client_secret = None
        if getattr(row, "encrypted_oauth_client_secret", None) and not getattr(row, "oauth_client_id", None):
            raise ExtensionValidationError("OAuth client secret에는 client ID가 필요합니다")
    elif kind == "skill":
        if "name" in fields and fields["name"]:
            row.name = str(fields["name"]).strip()
        if "description" in fields:
            row.description = (str(fields["description"]).strip() or None) if fields["description"] else None
        if "instructions" in fields and fields["instructions"] is not None:
            text = str(fields["instructions"]).strip()
            if not text:
                raise ExtensionValidationError("스킬 지침(instructions)은 비어 있을 수 없습니다")
            if len(text) > 20000:
                raise ExtensionValidationError("스킬 지침이 너무 깁니다(최대 20000자)")
            row.instructions = encrypt_chat_content(text)
    else:  # tool
        if "name" in fields and fields["name"]:
            name = str(fields["name"]).strip()
            if not name.replace("_", "").isalnum():
                raise ExtensionValidationError("툴 이름은 영숫자/언더스코어만 허용됩니다")
            row.name = name
        if "description" in fields and fields["description"]:
            row.description = str(fields["description"]).strip()
        if "method" in fields and fields["method"]:
            if fields["method"].upper() not in ("GET", "POST"):
                raise ExtensionValidationError("method 는 GET|POST 만 허용됩니다")
            row.method = fields["method"].upper()
        if "url" in fields and fields["url"]:
            row.url = str(fields["url"]).strip()
        if "params_schema" in fields:
            row.params_schema = fields["params_schema"] or None
        if "timeout_seconds" in fields and fields["timeout_seconds"] is not None:
            row.timeout_seconds = max(1, min(int(fields["timeout_seconds"]), 60))
    if "load_policy" in fields:
        if getattr(row, "scope", None) != "global":
            raise ExtensionValidationError("도구 로딩 정책은 관리자 전역 확장에만 설정할 수 있습니다")
        policy = str(fields["load_policy"] or "").strip().lower()
        if policy not in _LOAD_POLICIES:
            raise ExtensionValidationError("load_policy 는 preloaded|on_demand 만 지원합니다")
        row.load_policy = policy
    if "is_active" in fields and fields["is_active"] is not None:
        row.is_active = bool(fields["is_active"])


async def list_global(kind: str) -> list[dict]:
    """관리자용 — global 스코프 전체."""
    model = _model(kind)
    factory = _require_db()
    try:
        async with factory() as session:
            rows = (
                (await session.execute(select(model).where(model.scope == "global").order_by(model.id))).scalars().all()
            )
            return [_PUBLIC[kind](r) for r in rows]
    except OperationalError as exc:
        # 선택적 확장 목록 — 전역 circuit breaker 를 열지 않는다(스키마 미적용/일시 실패가
        # 핵심 채팅 엔드포인트까지 503 으로 블랙아웃시키는 연쇄 방지). 진짜 DB 다운은 핵심 쿼리가 감지.
        raise ChatStorageUnavailable("chat DB 오류") from exc


async def list_for_user(
    kind: str,
    *,
    user_id: str,
    project_id: str,
    active_only: bool = False,
    reveal_secrets: bool = False,
) -> list[dict]:
    """사용자용 — 활성 global + 본인 user 스코프.

    reveal_secrets=True 는 MCP 실행 경로(tool_runtime) 전용으로, 복호화된 실제
    인증 헤더를 포함한 dict 를 반환한다. API 응답으로는 절대 노출하지 말 것.
    """
    model = _model(kind)
    factory = _require_db()
    serialize = _reveal_mcp if (reveal_secrets and kind == "mcp") else _PUBLIC[kind]
    try:
        async with factory() as session:
            stmt = select(model).where(
                ((model.scope == "global") & (model.is_active.is_(True)))
                | ((model.scope == "user") & (model.owner_user_id == user_id) & (model.owner_project_id == project_id))
            )
            if active_only:
                stmt = stmt.where(model.is_active.is_(True))
            rows = (await session.execute(stmt.order_by(model.id))).scalars().all()
            return [serialize(r) for r in rows]
    except OperationalError as exc:
        # 선택적 확장 목록 — 전역 circuit breaker 를 열지 않는다(스키마 미적용/일시 실패가
        # 핵심 채팅(conversations 등)까지 503 으로 블랙아웃시키는 연쇄 방지). 진짜 DB 다운은 핵심 쿼리가 감지.
        raise ChatStorageUnavailable("chat DB 오류") from exc


async def create(
    kind: str,
    fields: dict,
    *,
    scope: str,
    owner_user_id: str | None = None,
    owner_project_id: str | None = None,
) -> dict:
    model = _model(kind)
    if scope not in ("global", "user"):
        raise ExtensionValidationError("scope 는 global|user 여야 합니다")
    if scope == "user" and (not owner_user_id or not owner_project_id):
        raise ExtensionValidationError("user 스코프는 소유자 정보가 필요합니다")
    if not fields.get("name"):
        raise ExtensionValidationError("name 은 필수입니다")
    if kind == "tool" and not fields.get("url"):
        raise ExtensionValidationError("커스텀 툴은 url 이 필수입니다")
    if kind == "mcp":
        if not (fields.get("url") or "").strip():
            raise ExtensionValidationError("원격 MCP 서버는 url 이 필수입니다")
        if scope == "user":
            _ensure_user_mcp_policy(fields)
    if kind == "skill" and not (fields.get("instructions") or "").strip():
        raise ExtensionValidationError("스킬은 지침(instructions)이 필수입니다")

    # 스키마 NOT NULL 컬럼 기본값 — _apply_fields 가 덮어쓴다.
    _required_defaults = {
        "tool": {"description": "", "url": ""},
        "skill": {"instructions": ""},
    }
    row = model(
        scope=scope,
        owner_user_id=(owner_user_id if scope == "user" else None),
        owner_project_id=(owner_project_id if scope == "user" else None),
        **_required_defaults.get(kind, {}),
    )
    _apply_fields(kind, row, fields)
    factory = _require_db()
    try:
        async with factory() as session, session.begin():
            session.add(row)
            await session.flush()
            return _PUBLIC[kind](row)
    except IntegrityError as exc:
        raise ExtensionValidationError("제약 위반(중복 등)") from exc
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc


async def _load_authorized(
    session, model, item_id: int, *, requester_user_id: str | None, requester_project_id: str | None, admin: bool
):
    row = await session.get(model, item_id)
    if row is None:
        raise ExtensionNotFound(f"{item_id} 를 찾을 수 없습니다")
    if row.scope == "global":
        if not admin:
            raise ExtensionForbidden("global 확장은 관리자만 변경할 수 있습니다")
    else:  # user
        if admin:
            pass  # 관리자는 열람/변경 허용
        elif row.owner_user_id != requester_user_id or row.owner_project_id != requester_project_id:
            raise ExtensionForbidden("소유자가 아닙니다")
    return row


async def _revoke_mcp_oauth_connections(session, server_id: int) -> None:
    """Invalidate every OAuth token bound to an MCP configuration revision."""
    connections = (
        await session.execute(
            select(ChatMcpOAuthConnection).where(ChatMcpOAuthConnection.mcp_server_id == server_id).with_for_update()
        )
    ).scalars()
    for connection in connections:
        if connection.status != "revoked":
            connection.status = "revoked"
            connection.credential_version += 1
            connection.expires_at = None


async def update(
    kind: str,
    item_id: int,
    patch: dict,
    *,
    requester_user_id: str | None = None,
    requester_project_id: str | None = None,
    admin: bool = False,
) -> dict:
    model = _model(kind)
    factory = _require_db()
    try:
        async with factory() as session, session.begin():
            row = await _load_authorized(
                session,
                model,
                item_id,
                requester_user_id=requester_user_id,
                requester_project_id=requester_project_id,
                admin=admin,
            )
            if kind == "mcp" and row.scope == "user":
                _ensure_user_mcp_policy(patch)
            _apply_fields(kind, row, patch)
            if kind in {"mcp", "tool"}:
                row.config_version += 1
            if kind == "mcp":
                await _revoke_mcp_oauth_connections(session, row.id)
            await session.flush()
            return _PUBLIC[kind](row)
    except IntegrityError as exc:
        raise ExtensionValidationError("제약 위반") from exc
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc


async def delete(
    kind: str,
    item_id: int,
    *,
    requester_user_id: str | None = None,
    requester_project_id: str | None = None,
    admin: bool = False,
) -> None:
    model = _model(kind)
    factory = _require_db()
    try:
        async with factory() as session, session.begin():
            row = await _load_authorized(
                session,
                model,
                item_id,
                requester_user_id=requester_user_id,
                requester_project_id=requester_project_id,
                admin=admin,
            )
            await session.delete(row)
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc
