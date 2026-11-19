"""빌트인 AI 채팅 확장(MCP 서버 / 커스텀 HTTP 툴) 관리 저장소 (MySQL).

스코프 모델:
- scope='global' (owner_* NULL): 관리자만 생성/수정/삭제; MCP 실행은 소유자별 승인 필요.
- scope='user': owner and verified Keystone domain, or origin project for legacy rows.
- scope='domain': current members may use the row; only domain managers may manage it.

MCP execution requires the current configuration, approval and credential.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Collection, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, OperationalError

from lumen.crypto import (
    decrypt_chat_content,
    decrypt_llm_provider_key,
    encrypt_chat_content,
    encrypt_llm_provider_key,
)
from lumen.db import get_session_factory, is_db_available, mark_db_unhealthy
from lumen.models.chat_db import ChatCustomTool, ChatMcpActivation, ChatMcpOAuthConnection, ChatMcpServer, ChatSkill
from lumen.services import ssrf
from lumen.services.domain_scope import personal_mcp_scope_key, scope_clause, scope_matches

logger = logging.getLogger(__name__)

_MODELS = {"mcp": ChatMcpServer, "tool": ChatCustomTool, "skill": ChatSkill}

# MCP supports hosted streamable HTTP and SSE only. Local process (stdio) transports are rejected.
_MCP_TRANSPORTS = ("http", "sse")

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


def _stored_tools(raw) -> list[dict]:
    tools = raw.get("tools") if isinstance(raw, dict) else None
    return [tool for tool in tools if isinstance(tool, dict)] if isinstance(tools, list) else []


def _public_mcp(row: ChatMcpServer) -> dict:
    # Header values remain write-only and are exposed only as administrator configuration state.
    return {
        "id": row.id,
        "scope": row.scope,
        "owner_domain_id": getattr(row, "owner_domain_id", None),
        "owner_user_id": getattr(row, "owner_user_id", None),
        "owner_project_id": getattr(row, "owner_project_id", None),
        "implicit_selection": bool(getattr(row, "implicit_selection", True)),
        "name": row.name,
        "transport": row.transport,
        "url": row.url,
        "has_headers": bool(row.encrypted_headers) or bool(row.headers),
        "auth_mode": _mcp_auth_mode(row),
        "oauth_scopes": _clean_oauth_scopes(getattr(row, "oauth_scopes", None)),
        "has_oauth_client": bool(getattr(row, "oauth_client_id", None)),
        "has_oauth_client_secret": bool(getattr(row, "encrypted_oauth_client_secret", None)),
        "is_active": row.is_active,
        "load_policy": _load_policy(row),
        "effect_overrides": row.tool_effect_overrides,
        "config_version": row.config_version,
        "template_key": row.template_key,
        "connection_label": row.connection_label,
        "tools": [{"name": tool.get("name"), "description": tool.get("description")} for tool in _stored_tools(row.tool_catalog)],
        "tool_count": len(_stored_tools(row.tool_catalog)),
        "catalog_refreshed_at": _iso(row.catalog_refreshed_at),
        "catalog_error": row.catalog_error,
        "created_at": _iso(row.created_at),
    }


def _reveal_mcp(row: ChatMcpServer) -> dict:
    """Execution-only representation. Static secrets flow only in administrator mode."""
    auth_mode = _mcp_auth_mode(row)
    return {
        "id": row.id,
        "scope": row.scope,
        "owner_domain_id": getattr(row, "owner_domain_id", None),
        "implicit_selection": bool(getattr(row, "implicit_selection", True)),
        "name": row.name,
        "transport": row.transport,
        "url": row.url,
        "headers": _decrypt_headers(row, fail_closed=True)
        if auth_mode == "admin" and (
            getattr(row, "scope", None) in {"global", "domain"}
            or (getattr(row, "scope", None) == "user" and bool(getattr(row, "template_key", None)))
        ) and bool(row.encrypted_headers)
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
        "domain_id": row.domain_id,
        "owner_domain_id": row.owner_domain_id,
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


def _public_skill(row: ChatSkill, *, include_private: bool = True) -> dict:
    return {
        "id": row.id,
        "scope": row.scope,
        "name": row.name,
        "description": row.description,
        **({"instructions": decrypt_chat_content(row.instructions) if row.instructions else ""} if include_private else {}),
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
            "description",
            "url",
            "method",
            "timeout_seconds",
            "transport",
            "auth_mode",
            "params_schema",
            "config_version",
            "effect",
            "load_policy",
        )
    }
    for key in ("domain_id", "owner_domain_id", "activation_generation", "activation_owner_domain_id",
                "credential_id"):
        if item.get(key):
            material[key] = item[key]
    return hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


async def validated_frozen_selection(
    snapshot: object, *, user_id: str, project_id: str
) -> tuple[tuple[int, ...], tuple[int, ...], list[str]]:
    """Re-check a queued run's exact active configuration without widening selection."""
    if not isinstance(snapshot, dict):
        return (), (), ["run extension selection is unavailable"]
    selected_by_kind = {"tool": snapshot.get("tools"), "mcp": snapshot.get("mcp")}
    resolved: dict[str, tuple[int, ...]] = {"tool": (), "mcp": ()}
    warnings: list[str] = []
    for kind, selected in selected_by_kind.items():
        if not isinstance(selected, list):
            warnings.append(f"run {kind} selection is invalid")
            continue
        kind_ids = [item["id"] for item in selected if isinstance(item, dict) and type(item.get("id")) is int]
        if not selected:
            continue
        visible = await list_for_user(
            kind, user_id=user_id, project_id=project_id, active_only=True,
            include_domain=kind_ids, selected_ids=kind_ids, runtime=True,
        )
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
                current_version = now.get("credential_version", 0)
                if type(expected_version) is not int or expected_version != current_version:
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
                raise ExtensionValidationError("transport 는 http(streamable HTTP) 또는 sse 만 지원합니다")
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


async def _owner_rows(
    session, model, user_id: str, project_id: str, domain_id: str | None,
    *, server_ids: Collection[int] | None = None, lock: bool = False,
) -> dict[int, ChatMcpActivation | ChatMcpOAuthConnection]:
    """A domain row shadows even an active origin row when it is disabled or revoked."""
    query = select(model).where(
        model.owner_user_id == user_id,
        scope_clause(model.owner_domain_id, model.owner_project_id, domain_id=domain_id, project_id=project_id),
    )
    if server_ids is not None:
        query = query.where(model.mcp_server_id.in_(server_ids))
    if lock:
        query = query.with_for_update()
    rows = {}
    for row in (await session.execute(query)).scalars():
        if row.owner_domain_id is not None or row.mcp_server_id not in rows:
            rows[row.mcp_server_id] = row
    return rows


async def _owner_activations(
    session, user_id: str, project_id: str, domain_id: str | None = None,
    *, server_ids: Collection[int] | None = None, lock: bool = False,
) -> dict[int, ChatMcpActivation]:
    return await _owner_rows(
        session, ChatMcpActivation, user_id, project_id, domain_id, server_ids=server_ids, lock=lock,
    )


async def _mcp_connections(
    session, servers: Collection[ChatMcpServer], user_id: str, project_id: str, domain_id: str | None,
    organization_domain: str | None,
) -> dict[int, ChatMcpOAuthConnection]:
    personal = [row.id for row in servers if row.scope != "domain" and _mcp_auth_mode(row) == "oauth"]
    shared = [
        row.id for row in servers
        if row.scope == "domain" and organization_domain and row.owner_domain_id == organization_domain
        and _mcp_auth_mode(row) == "oauth"
    ]
    connections = {}
    if personal:
        connections.update(await _owner_rows(
            session, ChatMcpOAuthConnection, user_id, project_id, domain_id, server_ids=personal,
        ))
    if shared:
        connections.update(await _owner_rows(
            session, ChatMcpOAuthConnection, organization_domain, organization_domain, None, server_ids=shared,
        ))
    return connections


async def _manage_domain_tool(user_id: str, project_id: str, domain_id: str) -> None:
    if not domain_id or request_domain(user_id, project_id) != domain_id:
        raise ExtensionForbidden("Custom tools require the current Keystone domain")
    await _domain_access(user_id, project_id, domain_id, manage=True)


async def list_domain_tools(domain_id: str, *, user_id: str, project_id: str) -> list[dict]:
    await _manage_domain_tool(user_id, project_id, domain_id)
    try:
        async with _require_db()() as session:
            rows = (await session.execute(select(ChatCustomTool).where(
                ChatCustomTool.scope == "domain", ChatCustomTool.owner_domain_id == domain_id,
            ).order_by(ChatCustomTool.id))).scalars()
            return [_public_tool(row) for row in rows]
    except OperationalError as exc:
        raise ChatStorageUnavailable("chat DB 오류") from exc


def _mcp_effectively_active(row: ChatMcpServer, activation: ChatMcpActivation | None) -> bool:
    return bool(row.is_active and (
        row.scope == "user" or (
            activation and activation.is_active and activation.config_version == row.config_version
        )
    ))


def _mcp_connected(row: ChatMcpServer, connection: ChatMcpOAuthConnection | None) -> bool:
    if _mcp_auth_mode(row) == "admin":
        return bool(row.encrypted_headers)
    return _mcp_auth_mode(row) != "oauth" or bool(
        connection and connection.status == "active" and connection.encrypted_tokens
        and connection.server_config_version == row.config_version
    )


def _activation_metadata(row: ChatMcpServer, activation: ChatMcpActivation | None) -> dict:
    return {
        "is_active": _mcp_effectively_active(row, activation),
        "server_is_active": bool(row.is_active),
        "activation_is_active": bool(activation and activation.is_active) if row.scope in {"global", "domain"} else bool(row.is_active),
        "activation_config_version": activation.config_version if activation else None,
        "activation_generation": activation.generation if activation else 0,
        "activation_owner_domain_id": activation.owner_domain_id if activation else None,
    }


async def set_mcp_activation(
    item_id: int, *, user_id: str, project_id: str, is_active: bool, config_version: int
) -> dict:
    """Approve only this owner's exact global revision; never mutate publication."""
    if not user_id or not project_id:
        raise ExtensionForbidden("MCP activation requires an owner and project")
    if type(is_active) is not bool or type(config_version) is not int or config_version < 1:
        raise ExtensionValidationError("is_active and positive config_version are required")
    domain_id = request_domain(user_id, project_id)
    factory = _require_db()
    # Organization membership is read from Keystone BEFORE the row lock: token refresh takes the same lock, so a slow
    # directory must never stall every run that uses this server. The locked section re-verifies the row's domain.
    authorized_domain: str | None = None
    try:
        async with factory() as probe:
            peek = await probe.get(ChatMcpServer, item_id)
            peek_domain = peek.owner_domain_id if peek is not None and peek.scope == "domain" else None
    except OperationalError as exc:
        raise ChatStorageUnavailable("chat DB 오류") from exc
    if peek_domain is not None:
        await _domain_access(user_id, project_id, peek_domain)
        authorized_domain = peek_domain
    try:
        async with factory() as session, session.begin():
            # Serialize first activation and every epoch transition on the published row.
            server = await session.get(ChatMcpServer, item_id, with_for_update=True)
            if server is None:
                raise ExtensionNotFound(f"MCP 서버 {item_id} 를 찾을 수 없습니다")
            if server.scope not in {"global", "domain"}:
                raise ExtensionForbidden("Only published MCP servers support activation")
            if server.scope == "domain" and (authorized_domain is None or server.owner_domain_id != authorized_domain):
                raise ExtensionForbidden("Organization connection changed; retry the activation")
            if not server.is_active:
                raise ExtensionValidationError("MCP server is unavailable")
            if server.config_version != config_version:
                raise ExtensionValidationError("MCP configuration changed; review the current version")
            activation = (await _owner_rows(
                session, ChatMcpActivation, user_id, project_id, domain_id, server_ids=(item_id,), lock=True,
            )).get(item_id)
            if activation is None:
                activation = ChatMcpActivation(
                    mcp_server_id=item_id, owner_user_id=user_id, owner_project_id=project_id,
                    owner_domain_id=domain_id, is_active=is_active, config_version=config_version, generation=1,
                )
                session.add(activation)
            elif (
                activation.is_active != is_active or activation.config_version != config_version
                or activation.owner_domain_id != domain_id
            ):
                activation.is_active = is_active
                activation.config_version = config_version
                activation.owner_domain_id = domain_id
                activation.generation += 1
            await session.flush()
            connection = (await _mcp_connections(
                session, (server,), user_id, project_id, domain_id, authorized_domain,
            )).get(item_id)
            metadata = _activation_metadata(server, activation)
            metadata["is_active"] = metadata["is_active"] and _mcp_connected(server, connection)
            return {**_public_mcp(server), **metadata}
    except OperationalError as exc:
        raise ChatStorageUnavailable("chat DB 오류") from exc


async def list_for_user(
    kind: str,
    *,
    user_id: str,
    project_id: str,
    active_only: bool = False,
    reveal_secrets: bool = False,
    include_private: bool = True,
    include_domain: bool | Collection[int] = True,
    selected_ids: Collection[int] | None = None,
    runtime: bool = False,
) -> list[dict]:
    """사용자용 — MCP catalogue + 본인 user 스코프; 실행은 유효한 소유자 승인만.

    reveal_secrets=True 는 MCP 실행 경로(tool_runtime) 전용으로, 복호화된 실제
    인증 헤더를 포함한 dict 를 반환한다. API 응답으로는 절대 노출하지 말 것.
    """
    model = _model(kind)
    if selected_ids is not None and not selected_ids:
        return []
    factory = _require_db()
    serialize = _reveal_mcp if (reveal_secrets and kind == "mcp") else _PUBLIC[kind]
    domain_id = request_domain(user_id, project_id)
    organization_domain = None
    try:
        if kind == "mcp" or (kind == "tool" and domain_id):
            organization_domain = await _caller_domain(
                factory, model, user_id, project_id, include_domain,
                personal=runtime, selected_ids=selected_ids,
            )
            if runtime and domain_id and domain_id != organization_domain:
                return []
            if include_domain is False or (kind == "tool" and organization_domain != domain_id):
                organization_domain = None
        async with factory() as session:
            published = model.scope == "global"
            if kind != "mcp":
                published = published & model.is_active.is_(True)
            if organization_domain:
                domain_rows = (model.scope == "domain") & (model.owner_domain_id == organization_domain)
                if include_domain is not True:
                    domain_rows = domain_rows & model.id.in_(include_domain)
                if kind == "tool":
                    domain_rows = domain_rows & model.is_active.is_(True)
                published = published | domain_rows
            stmt = select(model).where(published | personal_clause(kind, user_id, project_id, domain_id))
            if selected_ids is not None:
                stmt = stmt.where(model.id.in_(selected_ids))
            if active_only:
                stmt = stmt.where(model.is_active.is_(True))
            rows = (await session.execute(stmt.order_by(model.id))).scalars().all()
            if kind != "mcp":
                return [
                    _public_skill(r, include_private=include_private) if kind == "skill" else serialize(r)
                    for r in rows
                ]
            ids = [row.id for row in rows]
            activations = await _owner_activations(session, user_id, project_id, domain_id, server_ids=ids)
            connections = await _mcp_connections(session, rows, user_id, project_id, domain_id, organization_domain)
            result = []
            for row in rows:
                activation = activations.get(row.id) if row.scope != "user" else None
                connection = connections.get(row.id)
                connected = _mcp_connected(row, connection)
                active = _mcp_effectively_active(row, activation) and connected
                if (active_only or reveal_secrets) and not active:
                    continue
                item = serialize(row)
                item.update(_activation_metadata(row, activation))
                item["is_active"] = active
                item["auth_connected"] = connected
                if _mcp_auth_mode(row) == "oauth":
                    item["credential_version"] = connection.credential_version if active else None
                    if connection is not None:
                        item["credential_id"] = connection.id
                result.append(item)
            return result
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
    owner_domain_id: str | None = None,
    catalog_authorized: bool = False,
) -> dict:
    model = _model(kind)
    if scope not in ("global", "user", "domain") or (scope == "domain" and kind == "skill"):
        raise ExtensionValidationError("scope 는 global|user|domain 여야 합니다")
    if scope == "domain":
        if not owner_domain_id or not owner_user_id or not owner_project_id:
            raise ExtensionValidationError("domain 스코프는 도메인과 관리 소유자 정보가 필요합니다")
        if kind == "tool":
            await _manage_domain_tool(owner_user_id, owner_project_id, owner_domain_id)
        else:
            await _domain_access(owner_user_id, owner_project_id, owner_domain_id, manage=True)
    if scope == "user" and (not owner_user_id or not owner_project_id):
        raise ExtensionValidationError("user 스코프는 소유자 정보가 필요합니다")
    if not fields.get("name"):
        raise ExtensionValidationError("name 은 필수입니다")
    if kind == "tool" and not fields.get("url"):
        raise ExtensionValidationError("커스텀 툴은 url 이 필수입니다")
    if kind == "mcp":
        if not (fields.get("url") or "").strip():
            raise ExtensionValidationError("원격 MCP 서버는 url 이 필수입니다")
        if scope == "user" and not catalog_authorized:
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
        owner_user_id=(owner_user_id if scope in {"user", "domain"} else None),
        owner_project_id=(owner_project_id if scope in {"user", "domain"} else None),
        **_required_defaults.get(kind, {}),
    )
    if kind == "tool":
        row.domain_id = request_domain(owner_user_id, owner_project_id) if scope == "user" else None
        row.owner_domain_id = owner_domain_id if scope == "domain" else None
    _apply_fields(kind, row, fields)
    if kind == "mcp":
        row.template_key = fields.get("template_key")
        row.connection_label = fields.get("connection_label")
        row.owner_domain_id = (
            owner_domain_id if scope == "domain"
            else request_domain(owner_user_id, owner_project_id) if scope == "user" else None
        )
        row.connection_scope_key = connection_scope_key(
            scope, owner_user_id, owner_project_id, row.owner_domain_id,
        )
        row.implicit_selection = False if scope == "domain" else bool(fields.get("implicit_selection", True))
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


async def get_global_mcp_revealed(item_id: int) -> dict:
    """Administrator execution view of one global MCP connection, with decrypted admin headers."""
    factory = _require_db()
    try:
        async with factory() as session:
            row = await session.get(ChatMcpServer, item_id)
            if row is None or row.scope != "global":
                raise ExtensionNotFound(f"{item_id} 를 찾을 수 없습니다")
            return _reveal_mcp(row)
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc


async def record_catalog(item_id: int, *, tools: list[dict] | None, error: str | None) -> list[dict]:
    """Store discovery metadata only. It never bumps config_version, so OAuth connections stay valid.

    Returns the previously stored tools so the caller can report what changed. A failure keeps the last good catalog.
    """
    factory = _require_db()
    try:
        async with factory() as session, session.begin():
            row = await session.get(ChatMcpServer, item_id)
            if row is None or row.scope != "global":
                raise ExtensionNotFound(f"{item_id} 를 찾을 수 없습니다")
            previous = _stored_tools(row.tool_catalog)
            if tools is not None:
                row.tool_catalog = {"tools": tools}
                row.catalog_refreshed_at = func.utc_timestamp()
                row.catalog_error = None
            else:
                row.catalog_error = (error or "")[:300] or None
            await session.flush()
            return previous
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc


async def _load_authorized(
    session, model, item_id: int, *, requester_user_id: str | None, requester_project_id: str | None, admin: bool,
    domain_id: str | None = None,
):
    if domain_id is not None and not admin:
        if model is ChatCustomTool:
            await _manage_domain_tool(requester_user_id, requester_project_id, domain_id)
        else:
            await _domain_access(requester_user_id, requester_project_id, domain_id, manage=True)
    row = await session.get(model, item_id, with_for_update=True)
    if row is None:
        raise ExtensionNotFound(f"{item_id} 를 찾을 수 없습니다")
    if model is ChatCustomTool and admin and row.scope != "global":
        raise ExtensionForbidden("The administrator tool endpoint manages only global tools")
    if domain_id is not None and (row.scope != "domain" or row.owner_domain_id != domain_id):
        raise ExtensionNotFound(f"{item_id} 를 찾을 수 없습니다")
    if row.scope == "global":
        if not admin:
            raise ExtensionForbidden("global 확장은 관리자만 변경할 수 있습니다")
    elif row.scope == "domain":
        if not admin and domain_id is None:
            raise ExtensionForbidden("Use the domain extension management endpoint")
    elif row.scope != "user" or (
        not admin and not personal_matches(
            row, requester_user_id, requester_project_id,
            domain_id=request_domain(requester_user_id, requester_project_id),
        )
    ):
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
    domain_id: str | None = None,
    catalog_authorized: bool = False,
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
                domain_id=domain_id,
            )
            if kind == "mcp" and row.scope == "user" and not catalog_authorized:
                _ensure_user_mcp_policy(patch)
                if row.template_key and ({"url", "transport"} & patch.keys()):
                    raise ExtensionValidationError("Catalog connections cannot be repointed; delete and recreate")
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
    domain_id: str | None = None,
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
                domain_id=domain_id,
            )
            await session.delete(row)
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc


# Whether the current request is an unrestricted human Keystone session. API keys, reduced credentials and background
# work are never human here, so they cannot use system-admin cross-domain reach or manage organization state.
_actor_human: ContextVar[bool] = ContextVar("mcp_actor_human", default=False)
_actor_owner: ContextVar[tuple[str, str, str] | None] = ContextVar("extension_actor_domain", default=None)
_actor_mode: ContextVar[str | None] = ContextVar("extension_actor_mode", default=None)


@contextmanager
def actor_scope(
    *, human: bool, owner: tuple[str, str, str] | None = None, mode: str = "request",
) -> Iterator[None]:
    token = _actor_human.set(bool(human))
    owner_token = _actor_owner.set(owner)
    mode_token = _actor_mode.set(mode)
    try:
        yield
    finally:
        _actor_mode.reset(mode_token)
        _actor_owner.reset(owner_token)
        _actor_human.reset(token)


def request_domain(user_id: str | None, project_id: str | None) -> str | None:
    owner = _actor_owner.get()
    return owner[2] if owner and owner[:2] == (user_id, project_id) else None



def connection_scope_key(scope: str, user_id: str | None, project_id: str | None, domain_id: str | None) -> str:
    if scope == "domain":
        return f"domain:{domain_id}"
    if scope == "user" and user_id and project_id:
        return personal_mcp_scope_key(user_id, project_id, domain_id)
    return "global"


def personal_clause(kind: str, user_id: str, project_id: str, domain_id: str | None):
    model = _MODELS[kind]
    owner = (model.scope == "user") & (model.owner_user_id == user_id)
    if kind == "skill":
        return owner & (model.owner_project_id == project_id)
    domain_column = model.owner_domain_id if kind == "mcp" else model.domain_id
    return owner & scope_clause(domain_column, model.owner_project_id, domain_id=domain_id, project_id=project_id)


def personal_matches(row, user_id: str | None, project_id: str | None, *, domain_id: str | None = None) -> bool:
    if not user_id or not project_id or row.owner_user_id != user_id:
        return False
    if isinstance(row, ChatSkill):
        return row.owner_project_id == project_id
    row_domain = row.domain_id if isinstance(row, ChatCustomTool) else row.owner_domain_id
    return scope_matches(
        row_domain, row.owner_project_id, domain_id=domain_id,
        project_id=project_id,
    )


async def _domain_access(
    user_id: str, project_id: str, domain_id: str | None = None, *, manage: bool = False, human: bool | None = None,
) -> dict:
    """Exact domain membership (or manager authority). ``human`` defaults to the request actor, i.e. not human."""
    from lumen.services.mcp_domain_authority import resolve_domain_authority

    is_human = _actor_human.get() if human is None else human
    if manage and not is_human:
        raise ExtensionForbidden("Exact domain manager authority required")
    authority = await resolve_domain_authority(
        user_id, project_id, domain_id, compute_manage=manage, unrestricted_human=is_human,
    )
    if not authority["is_member"] or (manage and not authority["can_manage"]):
        raise ExtensionForbidden("Exact domain manager authority required" if manage else "Domain membership required")
    return authority


async def _caller_domain(
    factory, model, user_id: str, project_id: str, include: bool | Collection[int],
    *, personal: bool = False, selected_ids: Collection[int] | None = None,
) -> str | None:
    """Resolve live membership only when a scoped row may be used."""
    organization = (model.scope == "domain") if include is True else None
    if include is not True and include is not False and include:
        organization = (model.scope == "domain") & model.id.in_(include)
    expected = request_domain(user_id, project_id)
    if personal and expected:
        # The pinned project must still be usable before any new I/O, including origin-only fallback.
        candidate = (model.scope == "global") | (model.owner_user_id == user_id) | (model.scope == "domain")
    else:
        candidate = organization
    if candidate is None:
        return None
    probe = select(model.id).where(candidate)
    if selected_ids is not None:
        probe = probe.where(model.id.in_(selected_ids))
    async with factory() as session:
        if (await session.execute(probe.limit(1))).first() is None:
            return None
    from fastapi import HTTPException

    try:
        domain = (await _domain_access(user_id, project_id))["domain_id"]
        # Never use a changed live domain to replace a request's verified authority.
        return domain if not expected or expected == domain else None
    except (HTTPException, ExtensionForbidden) as exc:
        logger.warning("Scoped extension rows hidden: domain authority unavailable (%s)", getattr(exc, "status_code", "denied"))
        return None


async def runtime_mcp_domains(
    user_id: str, project_id: str, server_ids: Collection[int] | None,
) -> tuple[str | None, str | None]:
    expected = request_domain(user_id, project_id)
    if _actor_mode.get() == "request":
        return expected, None
    live = await _caller_domain(
        _require_db(), ChatMcpServer, user_id, project_id, True if server_ids is None else server_ids,
        personal=True, selected_ids=server_ids,
    )
    if expected and live != expected:
        raise ExtensionForbidden("MCP execution project or domain is no longer available")
    return expected, live
