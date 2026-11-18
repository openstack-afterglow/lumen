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
from collections.abc import Mapping
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

from fastapi import HTTPException
from lumen_plugin_api.contracts import Namespace
from lumen_plugin_api.mcp import McpConnection, McpConnectionStore, McpOAuthError, McpOAuthProvider
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError

from lumen.auth import ensure_scopes, resolve_project_authority
from lumen.config import get_settings, is_development_loopback_http_url
from lumen.crypto import decrypt_llm_provider_key, encrypt_llm_provider_key
from lumen.db import get_session_factory, mark_db_unhealthy
from lumen.models.chat_db import ChatMcpActivation, ChatMcpOAuthConnection, ChatMcpOAuthRequest, ChatMcpServer
from lumen.plugins.registry import get_plugin
from lumen.services import extensions_store as extensions
from lumen.services import ssrf
from lumen.services.extensions_store import (
    ChatStorageUnavailable,
    ExtensionForbidden,
    ExtensionNotFound,
    ExtensionSecretUnavailable,
    _clean_oauth_scopes,
    _mcp_connections,
    _mcp_effectively_active,
    _owner_activations,
    _owner_rows,
    personal_clause,
    personal_matches,
    request_domain,
    runtime_mcp_domains,
)

logger = logging.getLogger(__name__)
INITIATOR_COOKIE = "afterglow_mcp_oauth_initiator"
_ORGANIZATION_DOMAIN = "_lumen_organization_domain"


@dataclass(frozen=True)
class OAuthFence:
    connection_id: str | None
    epoch: int
    config_version: int
    activation_generation: int
    activation_domain: str | None

    @classmethod
    def from_server(cls, server: dict, epoch: int) -> OAuthFence:
        return cls(
            server.get("credential_id"), epoch, server["config_version"],
            server.get("activation_generation", 0), server.get("activation_owner_domain_id"),
        )


_frozen_oauth: ContextVar[tuple[Namespace, Mapping[int, OAuthFence]] | None] = ContextVar(
    "lumen_frozen_mcp_oauth", default=None,
)
_discovered_revisions: ContextVar[dict[int, int] | None] = ContextVar("mcp_oauth_discovered_revisions", default=None)


@contextmanager
def _discovering_revision():
    token = _discovered_revisions.set({})
    try:
        yield
    finally:
        _discovered_revisions.reset(token)


def _require_discovered_revision(server: ChatMcpServer) -> None:
    revisions = _discovered_revisions.get()
    if revisions is not None and revisions.get(server.id) != server.config_version:
        raise McpOAuthError("MCP server configuration changed during discovery")


@contextmanager
def frozen_credentials(namespace: Namespace, expected: Mapping[int, OAuthFence]):
    """Allow refresh and token disclosure only for these exact selections."""
    bound = _frozen_oauth.set((namespace, expected))
    try:
        yield
    finally:
        _frozen_oauth.reset(bound)


def _matches_frozen(
    row: ChatMcpOAuthConnection, namespace: Namespace, activation: ChatMcpActivation | None,
) -> bool:
    frozen = _frozen_oauth.get()
    if frozen is None:
        return True
    owner, expected = frozen
    return owner == namespace and expected.get(row.mcp_server_id) == OAuthFence(
        row.id, row.credential_version, row.server_config_version,
        activation.generation if activation else 0, activation.owner_domain_id if activation else None,
    )


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


async def _visible_server(
    session, server_id: int, namespace: Namespace, *, domain_id: str | None = None, lock: bool = False,
    manage_organization: bool = False,
) -> ChatMcpServer:
    if not namespace.project_id:
        raise ExtensionForbidden("MCP OAuth requires a project")
    server = await session.get(ChatMcpServer, server_id)
    if server is None:
        raise ExtensionNotFound(f"MCP 서버 {server_id} 를 찾을 수 없습니다")
    if server.scope == "global":
        if not server.is_active:
            raise ExtensionForbidden("비활성 MCP 서버입니다")
    elif server.scope == "domain":
        if not server.owner_domain_id:
            raise ExtensionForbidden("Organization MCP has no domain owner")
        await extensions._domain_access(
            namespace.user_id, namespace.project_id, server.owner_domain_id, manage=manage_organization,
        )
        if not server.is_active:
            raise ExtensionForbidden("비활성 MCP 서버입니다")
    elif server.scope != "user" or not personal_matches(
        server, namespace.user_id, namespace.project_id, domain_id=domain_id,
    ):
        raise ExtensionForbidden("소유자가 아닙니다")
    if not server.url:
        raise McpOAuthError("MCP server URL is unavailable")
    if lock:
        expected = _server_scope_revision(server)
        server = await session.get(ChatMcpServer, server_id, with_for_update=True, populate_existing=True)
        if server is None or _server_scope_revision(server) != expected:
            raise McpOAuthError("MCP server configuration changed")
    return server


def _server_scope_revision(server: ChatMcpServer) -> tuple:
    return (
        server.scope, server.owner_user_id, server.owner_project_id, server.owner_domain_id, server.config_version,
    )


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


async def _connection_row(
    session, server: ChatMcpServer, namespace: Namespace, domain_id: str | None, *, lock: bool = False,
):
    user_id, project_id = namespace.user_id, namespace.project_id
    if server.scope == "domain":
        user_id = project_id = server.owner_domain_id
        domain_id = None
    return (await _owner_rows(
        session, ChatMcpOAuthConnection, user_id, project_id, domain_id,
        server_ids=(server.id,), lock=lock,
    )).get(server.id)


async def _recheck_state_owner(request: ChatMcpOAuthRequest) -> None:
    """Check project membership, action authority and any pinned domain before decryption."""
    authority = await resolve_project_authority(request.owner_user_id, request.owner_project_id)
    ensure_scopes({
        "auth_type": "keystone", "user_id": request.owner_user_id, "project_id": request.owner_project_id,
        **authority,
    }, "native:mcp:write")
    if request.owner_domain_id is not None:
        live = await extensions._domain_access(request.owner_user_id, request.owner_project_id, human=True)
        if live["domain_id"] != request.owner_domain_id:
            raise ExtensionForbidden("MCP OAuth request domain changed")


async def _recheck_state_scope(request: ChatMcpOAuthRequest, server: ChatMcpServer, payload: dict) -> None:
    """Check the server still has the exact personal or organization owner."""
    if server.scope == "domain":
        target = payload.get(_ORGANIZATION_DOMAIN, server.owner_domain_id)
        if not target or target != server.owner_domain_id:
            raise ExtensionForbidden("MCP OAuth organization changed")
        await extensions._domain_access(
            request.owner_user_id, request.owner_project_id, target, manage=True, human=True,
        )
    elif _ORGANIZATION_DOMAIN in payload or (server.scope == "user" and not personal_matches(
        server, request.owner_user_id, request.owner_project_id, domain_id=request.owner_domain_id,
    )):
        raise ExtensionForbidden("MCP OAuth request ownership changed")


class _RefreshLease:
    def __init__(self, row: ChatMcpOAuthConnection) -> None:
        self._row = row
        self.connection = _connection(row)

    async def save(self, token: dict[str, Any], expires_at: datetime | None) -> None:
        if self._row.status != "active":
            raise McpOAuthError("OAuth connection has been revoked")
        self._row.encrypted_tokens = _encrypt(token)
        self._row.expires_at = expires_at

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
            row = await _visible_server(
                session, server_id, namespace, domain_id=request_domain(namespace.user_id, namespace.project_id),
            )
            revisions = _discovered_revisions.get()
            if revisions is not None:
                revisions[server_id] = row.config_version
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
            row = await _visible_server(
                session, server_id, namespace, domain_id=request_domain(namespace.user_id, namespace.project_id),
                lock=True,
            )
            _require_discovered_revision(row)
            if row.scope == "user" and row.auth_mode != auth_mode:
                row.auth_mode = auth_mode
                row.config_version += 1

    async def create_state(
        self, *, state_hash: str, server_id: int, namespace: Namespace,
        payload: dict[str, Any], expires_at: datetime
    ) -> None:
        async with _session() as session:
            row = await _visible_server(
                session, server_id, namespace, domain_id=request_domain(namespace.user_id, namespace.project_id),
                lock=True, manage_organization=True,
            )
            if row.scope == "domain":
                payload = {**payload, _ORGANIZATION_DOMAIN: row.owner_domain_id}
            _require_discovered_revision(row)
            if not row.is_active or row.auth_mode != "oauth":
                raise McpOAuthError("This MCP server is not configured for OAuth")
            session.add(ChatMcpOAuthRequest(
                id=str(uuid.uuid4()),
                state_hash=state_hash,
                mcp_server_id=row.id,
                owner_user_id=namespace.user_id,
                owner_project_id=namespace.project_id,
                owner_domain_id=request_domain(namespace.user_id, namespace.project_id),
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
                # Only unrestricted human start may create this owner-only state.
                # Pre-cutover states require expiry/reapproval, not provenance inference.
                # Callback credentials are the persisted browser-bound state, not
                # request headers. Revalidate that owner's current management
                # authority before the plugin can exchange any authorization code.
                try:
                    await _recheck_state_owner(request)
                    server = await session.get(ChatMcpServer, request.mcp_server_id)
                    if not _oauth_configuration_matches(server, request.server_config_version):
                        raise McpOAuthError("MCP server configuration changed; start OAuth again")
                    payload = _decrypt(request.encrypted_payload)
                    await _recheck_state_scope(request, server, payload)
                except (HTTPException, ExtensionForbidden) as exc:
                    raise McpOAuthError("MCP OAuth management authority is no longer available") from exc
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
            server = await session.get(ChatMcpServer, request.mcp_server_id)
            if _oauth_configuration_matches(server, request.server_config_version):
                expected = _server_scope_revision(server)
                try:
                    await _recheck_state_owner(request)
                    await _recheck_state_scope(request, server, _decrypt(request.encrypted_payload))
                except (HTTPException, ExtensionForbidden) as exc:
                    raise McpOAuthError("MCP OAuth management authority is no longer available") from exc
                server = await session.get(
                    ChatMcpServer, request.mcp_server_id, with_for_update=True, populate_existing=True,
                )
                if server is not None and _server_scope_revision(server) != expected:
                    server = None
            if not _oauth_configuration_matches(server, request.server_config_version):
                request.status = "failed"
                request.completed_at = _now()
                failed_reason = "MCP server configuration changed; start OAuth again"
            else:
                namespace = Namespace(user_id=request.owner_user_id, project_id=request.owner_project_id)
                domain_id = request.owner_domain_id
                connection = await _connection_row(session, server, namespace, domain_id, lock=True)
                if server.scope == "domain":
                    namespace = Namespace(user_id=server.owner_domain_id, project_id=server.owner_domain_id)
                    domain_id = None
                if connection is None:
                    connection = ChatMcpOAuthConnection(
                        id=str(uuid.uuid4()),
                        mcp_server_id=request.mcp_server_id,
                        owner_user_id=namespace.user_id,
                        owner_project_id=namespace.project_id,
                        owner_domain_id=domain_id,
                        encrypted_tokens=_encrypt(token),
                        credential_version=1,
                        server_config_version=server.config_version,
                        status="active",
                        expires_at=expires_at,
                    )
                    session.add(connection)
                else:
                    connection.owner_domain_id = domain_id
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
        if not namespace.project_id:
            raise ExtensionForbidden("MCP OAuth requires a project")
        domain_id, _ = await runtime_mcp_domains(namespace.user_id, namespace.project_id, (server_id,))
        async with _session() as session:
            server = await _visible_server(session, server_id, namespace, domain_id=domain_id)
            row = await _connection_row(session, server, namespace, domain_id)
            if row is None:
                return None
            if _frozen_oauth.get() is not None:
                activations = await _owner_activations(
                    session, namespace.user_id, namespace.project_id, domain_id, server_ids=(server_id,),
                ) if server.scope != "user" else {}
                activation = activations.get(server_id)
                if not _mcp_effectively_active(server, activation) or not _matches_frozen(row, namespace, activation):
                    return None
            return _connection(row)

    async def connections(self, namespace: Namespace) -> tuple[McpConnection, ...]:
        if not namespace.project_id:
            raise ExtensionForbidden("MCP OAuth requires a project")
        frozen = _frozen_oauth.get()
        if frozen and frozen[0] != namespace:
            raise ExtensionForbidden("MCP OAuth execution namespace changed")
        selected_ids = tuple(frozen[1]) if frozen else None
        if selected_ids == ():
            return ()
        user_id, project_id = namespace.user_id, namespace.project_id
        domain_id, organization_domain = await runtime_mcp_domains(user_id, project_id, selected_ids)
        async with _session() as session:
            visible = (ChatMcpServer.scope == "global") | personal_clause("mcp", user_id, project_id, domain_id)
            if organization_domain:
                visible |= (ChatMcpServer.scope == "domain") & (ChatMcpServer.owner_domain_id == organization_domain)
            query = select(ChatMcpServer).where(
                visible, ChatMcpServer.auth_mode == "oauth", ChatMcpServer.is_active.is_(True),
            )
            if selected_ids is not None:
                query = query.where(ChatMcpServer.id.in_(selected_ids))
            servers = (await session.execute(query)).scalars().all()
            if not servers:
                return ()
            connections = await _mcp_connections(session, servers, user_id, project_id, domain_id, organization_domain)
            activations = await _owner_activations(
                session, user_id, project_id, domain_id, server_ids=connections.keys(),
            )
            return tuple(
                _connection(row)
                for server in servers
                if (row := connections.get(server.id)) is not None
                and row.status == "active" and row.server_config_version == server.config_version
                and _mcp_effectively_active(server, activations.get(server.id))
                and _matches_frozen(row, namespace, activations.get(server.id) if server.scope != "user" else None)
            )

    async def disconnect(self, server_id: int, namespace: Namespace) -> None:
        async with _session() as session:
            domain_id = request_domain(namespace.user_id, namespace.project_id)
            server = await _visible_server(
                session, server_id, namespace, domain_id=domain_id, lock=True, manage_organization=True,
            )
            row = await _connection_row(
                session, server, namespace, domain_id, lock=True,
            )
            if row is not None:
                await _RefreshLease(row).revoke()

    @asynccontextmanager
    async def locked_refresh(self, connection_id: str):
        """Lock the exact credential and activation across the plugin token exchange."""
        frozen = _frozen_oauth.get()
        namespace = frozen[0] if frozen else None
        domain_id, organization_domain = (
            await runtime_mcp_domains(namespace.user_id, namespace.project_id, tuple(frozen[1]))
            if frozen else (None, None)
        )
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
            if namespace is not None:
                if server.scope == "domain" and organization_domain != server.owner_domain_id:
                    raise McpOAuthError("OAuth connection domain changed")
                if server.scope == "user" and not personal_matches(
                    server, namespace.user_id, namespace.project_id, domain_id=domain_id,
                ):
                    raise McpOAuthError("OAuth connection domain changed")
                current = await _connection_row(session, server, namespace, domain_id, lock=True)
                if current is None or current.id != connection_id:
                    raise McpOAuthError("OAuth connection changed")
                user_id, project_id = namespace.user_id, namespace.project_id
            else:
                user_id, project_id = row.owner_user_id, row.owner_project_id
                domain_id = row.owner_domain_id
            if namespace is not None or server.scope != "domain":
                activations = {} if server.scope == "user" else await _owner_activations(
                    session, user_id, project_id, domain_id, server_ids=(server.id,), lock=True,
                )
                if not _mcp_effectively_active(server, activations.get(server.id)):
                    raise McpOAuthError("MCP activation was disabled or changed")
                if namespace is not None and not _matches_frozen(row, namespace, activations.get(server.id)):
                    raise McpOAuthError("OAuth connection or activation changed")
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
    with _discovering_revision():
        return await _oauth_provider().detect(server_id, _namespace(user_id, project_id))


async def begin(
    server_id: int, *, user_id: str, project_id: str, initiator_nonce: str, return_origin: str | None = None
) -> dict[str, str]:
    with _discovering_revision():
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
