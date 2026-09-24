"""Core host adapter for remote MCP OAuth.

SQL ownership, encrypted credentials, single-use state and row-lock epochs stay
here. Discovery, PKCE, browser callback and token refresh live in the packaged
``remote-mcp`` plugin, resolved through the registry; the functions at the foot
preserve caller signatures for routes and tool-runtime bindings.
"""
from __future__ import annotations

import json
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

from lumen_plugin_api.contracts import Namespace
from lumen_plugin_api.mcp import McpConnection, McpConnectionStore, McpOAuthError, McpOAuthProvider
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError

from lumen.config import get_settings, is_development_loopback_http_url
from lumen.crypto import decrypt_llm_provider_key, encrypt_llm_provider_key
from lumen.db import get_session_factory, mark_db_unhealthy
from lumen.models.chat_db import ChatMcpOAuthConnection, ChatMcpOAuthRequest, ChatMcpServer
from lumen.plugins.registry import get_plugin
from lumen.services import ssrf
from lumen.services.extensions_store import (
    ChatStorageUnavailable,
    ExtensionForbidden,
    ExtensionNotFound,
    ExtensionSecretUnavailable,
    _clean_oauth_scopes,
)

logger = logging.getLogger(__name__)
INITIATOR_COOKIE = "afterglow_mcp_oauth_initiator"


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(expires_at: datetime | None) -> datetime | None:
    # MariaDB returns UTC DATETIME values without tzinfo.
    return expires_at.replace(tzinfo=UTC) if expires_at is not None and expires_at.tzinfo is None else expires_at


def _decrypt(blob: str) -> dict[str, Any]:
    try:
        payload = json.loads(decrypt_llm_provider_key(blob))
        if not isinstance(payload, dict):
            raise ValueError("OAuth payload is not an object")
        return payload
    except Exception as exc:
        raise ExtensionSecretUnavailable("MCP OAuth secret cannot be decrypted") from exc


def _encrypt(payload: dict[str, Any]) -> str:
    return encrypt_llm_provider_key(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


@asynccontextmanager
async def _session():
    factory = get_session_factory()
    if factory is None:
        raise ChatStorageUnavailable("chat DB 오류")
    try:
        async with factory() as session, session.begin():
            yield session
    except (OperationalError, IntegrityError) as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc


async def _visible_server(session, server_id: int, namespace: Namespace) -> ChatMcpServer:
    if not namespace.project_id:
        raise ExtensionForbidden("MCP OAuth requires a project")
    server = await session.get(ChatMcpServer, server_id)
    if server is None:
        raise ExtensionNotFound(f"MCP 서버 {server_id} 를 찾을 수 없습니다")
    if server.scope == "global":
        if not server.is_active:
            raise ExtensionForbidden("비활성 MCP 서버입니다")
    elif server.owner_user_id != namespace.user_id or server.owner_project_id != namespace.project_id:
        raise ExtensionForbidden("소유자가 아닙니다")
    if not server.url:
        raise McpOAuthError("MCP server URL is unavailable")
    return server


def _oauth_configuration_matches(server: ChatMcpServer | None, config_version: int) -> bool:
    return bool(server and server.is_active and server.auth_mode == "oauth" and server.config_version == config_version)


def _static_client(server: ChatMcpServer) -> dict[str, str] | None:
    client_id = server.oauth_client_id
    encrypted_secret = server.encrypted_oauth_client_secret
    if not isinstance(client_id, str) or not client_id.strip():
        if encrypted_secret:
            raise McpOAuthError("OAuth client secret is configured without a client ID")
        return None
    client = {"client_id": client_id.strip(), "client_secret": ""}
    if encrypted_secret:
        try:
            secret = decrypt_llm_provider_key(encrypted_secret)
        except Exception as exc:
            raise ExtensionSecretUnavailable("MCP OAuth client secret cannot be decrypted") from exc
        if not isinstance(secret, str) or not secret:
            raise McpOAuthError("OAuth client secret is invalid")
        client["client_secret"] = secret
    return client


def _connection(row: ChatMcpOAuthConnection) -> McpConnection:
    return McpConnection(
        id=row.id,
        server_id=row.mcp_server_id,
        user_id=row.owner_user_id,
        project_id=row.owner_project_id,
        config_version=row.server_config_version,
        credential_epoch=row.credential_version,
        status=row.status,
        token=_decrypt(row.encrypted_tokens) if row.status == "active" else {},
        expires_at=_aware(row.expires_at),
    )


async def _connection_row(session, server_id: int, namespace: Namespace, *, lock: bool = False):
    query = select(ChatMcpOAuthConnection).where(
        ChatMcpOAuthConnection.mcp_server_id == server_id,
        ChatMcpOAuthConnection.owner_user_id == namespace.user_id,
        ChatMcpOAuthConnection.owner_project_id == namespace.project_id,
    )
    if lock:
        query = query.with_for_update()
    return (await session.execute(query)).scalars().first()


class _RefreshLease:
    def __init__(self, row: ChatMcpOAuthConnection) -> None:
        self._row = row
        self.connection = _connection(row)

    async def save(self, token: dict[str, Any], expires_at: datetime | None) -> None:
        if self._row.status != "active":
            raise McpOAuthError("OAuth connection has been revoked")
        self._row.encrypted_tokens = _encrypt(token)
        self._row.expires_at = expires_at
        self._row.credential_version += 1

    async def revoke(self) -> None:
        if self._row.status != "revoked":
            self._row.status = "revoked"
            self._row.credential_version += 1
            self._row.expires_at = None
            self._row.encrypted_tokens = _encrypt({})


class SqlMcpConnectionStore(McpConnectionStore):
    """Scope checks and encrypted SQL transactions for the packaged OAuth client."""

    def oauth_configuration(self) -> dict[str, Any]:
        return oauth_configuration()

    async def server(self, server_id: int, namespace: Namespace) -> dict[str, Any]:
        async with _session() as session:
            row = await _visible_server(session, server_id, namespace)
            return {
                "id": row.id,
                "url": row.url,
                "scope": row.scope,
                "auth_mode": row.auth_mode,
                "config_version": row.config_version,
                "static_client": _static_client(row),
                "oauth_scopes": _clean_oauth_scopes(row.oauth_scopes),
            }

    async def set_auth_mode(
        self, server_id: int, namespace: Namespace, auth_mode: Literal["none", "oauth"]
    ) -> None:
        async with _session() as session:
            row = await _visible_server(session, server_id, namespace)
            if row.scope == "user" and row.auth_mode != auth_mode:
                row.auth_mode = auth_mode
                row.config_version += 1

    async def create_state(
        self, *, state_hash: str, server_id: int, namespace: Namespace,
        payload: dict[str, Any], expires_at: datetime
    ) -> None:
        async with _session() as session:
            row = await _visible_server(session, server_id, namespace)
            if not row.is_active or row.auth_mode != "oauth":
                raise McpOAuthError("This MCP server is not configured for OAuth")
            session.add(ChatMcpOAuthRequest(
                id=str(uuid.uuid4()),
                state_hash=state_hash,
                mcp_server_id=row.id,
                owner_user_id=namespace.user_id,
                owner_project_id=namespace.project_id,
                server_config_version=row.config_version,
                encrypted_payload=_encrypt(payload),
                status="pending",
                expires_at=expires_at,
            ))

    async def consume_state(self, state_hash: str) -> dict[str, Any]:
        expired_or_used = False
        payload: dict[str, Any] | None = None
        async with _session() as session:
            request = (await session.execute(
                select(ChatMcpOAuthRequest)
                .where(ChatMcpOAuthRequest.state_hash == state_hash)
                .with_for_update()
            )).scalars().first()
            if request is None or request.status != "pending" or _aware(request.expires_at) <= _now():
                if request is not None and request.status == "pending":
                    request.status = "expired"
                    request.completed_at = _now()
                expired_or_used = True
            else:
                payload = _decrypt(request.encrypted_payload)
                request.status = "processing"
        if expired_or_used or payload is None:
            raise McpOAuthError("OAuth authorization request has expired or was already used")
        return payload

    async def set_state_status(self, state_hash: str, status: str) -> None:
        """Best-effort terminal status update. Never undo a completed or failed request."""
        try:
            async with _session() as session:
                row = (await session.execute(
                    select(ChatMcpOAuthRequest)
                    .where(ChatMcpOAuthRequest.state_hash == state_hash)
                    .with_for_update()
                )).scalars().first()
                if row is not None and row.status == "processing":
                    row.status = status
                    row.completed_at = _now()
        except Exception:
            logger.warning("MCP OAuth request status update failed", exc_info=True)

    async def complete_state(
        self, state_hash: str, *, token: dict[str, Any], expires_at: datetime | None
    ) -> int:
        failed_reason: str | None = None
        server_id: int | None = None
        async with _session() as session:
            request = (await session.execute(
                select(ChatMcpOAuthRequest)
                .where(ChatMcpOAuthRequest.state_hash == state_hash)
                .with_for_update()
            )).scalars().first()
            if request is None or request.status != "processing":
                raise McpOAuthError("OAuth authorization request could not be completed")
            server = await session.get(ChatMcpServer, request.mcp_server_id, with_for_update=True)
            if not _oauth_configuration_matches(server, request.server_config_version):
                request.status = "failed"
                request.completed_at = _now()
                failed_reason = "MCP server configuration changed; start OAuth again"
            else:
                namespace = Namespace(user_id=request.owner_user_id, project_id=request.owner_project_id)
                connection = await _connection_row(session, request.mcp_server_id, namespace, lock=True)
                if connection is None:
                    connection = ChatMcpOAuthConnection(
                        id=str(uuid.uuid4()),
                        mcp_server_id=request.mcp_server_id,
                        owner_user_id=request.owner_user_id,
                        owner_project_id=request.owner_project_id,
                        encrypted_tokens=_encrypt(token),
                        credential_version=1,
                        server_config_version=server.config_version,
                        status="active",
                        expires_at=expires_at,
                    )
                    session.add(connection)
                else:
                    connection.encrypted_tokens = _encrypt(token)
                    connection.credential_version += 1
                    connection.server_config_version = server.config_version
                    connection.status = "active"
                    connection.expires_at = expires_at
                request.status = "completed"
                request.completed_at = _now()
                server_id = request.mcp_server_id
        if failed_reason:
            raise McpOAuthError(failed_reason)
        assert server_id is not None
        return server_id

    async def connection(self, server_id: int, namespace: Namespace) -> McpConnection | None:
        async with _session() as session:
            await _visible_server(session, server_id, namespace)
            row = await _connection_row(session, server_id, namespace)
            return _connection(row) if row is not None else None

    async def connections(self, namespace: Namespace) -> tuple[McpConnection, ...]:
        if not namespace.project_id:
            raise ExtensionForbidden("MCP OAuth requires a project")
        async with _session() as session:
            rows = (await session.execute(
                select(ChatMcpOAuthConnection).where(
                    ChatMcpOAuthConnection.owner_user_id == namespace.user_id,
                    ChatMcpOAuthConnection.owner_project_id == namespace.project_id,
                    ChatMcpOAuthConnection.status == "active",
                )
            )).scalars().all()
            if not rows:
                return ()
            servers = {
                server.id: server for server in (
                    await session.execute(select(ChatMcpServer).where(
                        ChatMcpServer.id.in_([row.mcp_server_id for row in rows])
                    ))
                ).scalars()
            }
            return tuple(
                _connection(row)
                for row in rows
                if _oauth_configuration_matches(servers.get(row.mcp_server_id), row.server_config_version)
            )

    async def disconnect(self, server_id: int, namespace: Namespace) -> None:
        async with _session() as session:
            await _visible_server(session, server_id, namespace)
            row = await _connection_row(session, server_id, namespace, lock=True)
            if row is not None:
                await _RefreshLease(row).revoke()

    @asynccontextmanager
    async def locked_refresh(self, connection_id: str):
        """Hold the SQL row lock across the plugin's token exchange and credential epoch update."""
        async with _session() as session:
            observed = await session.get(ChatMcpOAuthConnection, connection_id)
            if observed is None or observed.status != "active":
                raise McpOAuthError("OAuth connection is no longer active")
            server = await session.get(ChatMcpServer, observed.mcp_server_id, with_for_update=True)
            row = await session.get(ChatMcpOAuthConnection, connection_id, with_for_update=True, populate_existing=True)
            if row is None or row.status != "active":
                raise McpOAuthError("OAuth connection is no longer active")
            if not _oauth_configuration_matches(server, row.server_config_version):
                raise McpOAuthError("OAuth connection configuration changed")
            yield _RefreshLease(row)


def connection_store() -> McpConnectionStore:
    return SqlMcpConnectionStore()


def _configured_callback_url(value: str) -> str:
    """Validate the configured redirect target without accepting request input."""
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError as exc:
        raise McpOAuthError("OAuth callback URL is invalid") from exc
    if parsed.scheme.lower() == "https":
        if not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise McpOAuthError("OAuth callback URL must use a public HTTPS URL")
        normalized = urlunsplit(("https", parsed.netloc, parsed.path or "/", "", ""))
        try:
            ssrf.validate_url(normalized)
        except ssrf.SsrfBlocked as exc:
            raise McpOAuthError("OAuth callback URL is not publicly reachable") from exc
        return normalized
    if is_development_loopback_http_url(value):
        return urlunsplit(("http", parsed.netloc, parsed.path or "/", "", ""))
    raise McpOAuthError("OAuth callback URL must use public HTTPS or development HTTP loopback")


def _callback_url() -> str:
    settings = get_settings()
    configured = str(getattr(settings, "chat_mcp_oauth_callback_url", "") or "").strip()
    if configured:
        return _configured_callback_url(configured)
    public_base = str(getattr(settings, "public_api_base", "") or "").strip().rstrip("/")
    frontend_base = str(getattr(settings, "frontend_base_url", "") or "").strip().rstrip("/")
    base = public_base or frontend_base
    if not base:
        raise McpOAuthError("OAuth callback URL is not configured")
    return _configured_callback_url(f"{base}/api/v1/chat/mcp-oauth/callback")


def callback_cookie_secure() -> bool:
    return urlsplit(_callback_url()).scheme.lower() == "https"


def _normalize_origin(value: object, *, require_bare_path: bool = False) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower()
    if (
        not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment
        or (require_bare_path and parsed.path not in {"", "/"})
        or (scheme != "https" and not is_development_loopback_http_url(value))
    ):
        return None
    host = f"[{hostname}]" if ":" in hostname and not hostname.startswith("[") else hostname
    if port is not None and not ((scheme == "https" and port == 443) or (scheme == "http" and port == 80)):
        host = f"{host}:{port}"
    return f"{scheme}://{host}"


def oauth_configuration() -> dict[str, Any]:
    """Host-supplied, allowlisted callback and browser-return configuration."""
    settings = get_settings()
    configured_origins = (*settings.cors_origin_list, settings.frontend_base_url, settings.public_api_base)
    allowed = tuple(sorted({
        normalized for configured in configured_origins
        if (normalized := _normalize_origin(configured)) is not None
    }))
    # OAuth detection/status work without a callback URL. Only begin() requires it.
    try:
        callback_url = _callback_url()
    except McpOAuthError as exc:
        if str(exc) != "OAuth callback URL is not configured":
            raise
        callback_url = ""
    return {
        "callback_url": callback_url,
        "allowed_return_origins": allowed,
    }


def approved_return_origin(value: object) -> str | None:
    origin = _normalize_origin(value, require_bare_path=True)
    return origin if origin is not None and origin in oauth_configuration()["allowed_return_origins"] else None


def _oauth_provider() -> McpOAuthProvider:
    """Resolve the started remote-mcp plugin; neither store nor settings leak into it."""
    return get_plugin("mcp", "remote-mcp")


def _namespace(user_id: str, project_id: str) -> Namespace:
    return Namespace(user_id=user_id, project_id=project_id)


async def detect(server_id: int, *, user_id: str, project_id: str) -> dict[str, Any]:
    return await _oauth_provider().detect(server_id, _namespace(user_id, project_id))


async def begin(
    server_id: int, *, user_id: str, project_id: str, initiator_nonce: str, return_origin: str | None = None
) -> dict[str, str]:
    return await _oauth_provider().begin_connect(
        server_id, _namespace(user_id, project_id), initiator_nonce=initiator_nonce, return_origin=return_origin
    )


async def complete(
    *, state: str, code: str | None, error: str | None, iss: str | None, initiator_nonce: str | None
) -> tuple[int, str | None]:
    return await _oauth_provider().complete_callback(
        state=state, code=code, error=error, iss=iss, initiator_nonce=initiator_nonce
    )


async def status(server_id: int, *, user_id: str, project_id: str) -> dict[str, Any]:
    return await _oauth_provider().connection_status(server_id, _namespace(user_id, project_id))


async def disconnect(server_id: int, *, user_id: str, project_id: str) -> None:
    await _oauth_provider().disconnect(server_id, _namespace(user_id, project_id))


async def headers_for_user(*, user_id: str, project_id: str) -> dict[int, dict[str, str]]:
    return await _oauth_provider().headers_for_user(_namespace(user_id, project_id))
