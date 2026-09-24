"""Remote MCP OAuth 2.1: discovery, PKCE, browser-bound state, callback mapping, refresh.

Core supplies typed configuration, an encrypted/locked connection store, and a
DNS-pinned public HTTP transport (``lumen_plugin_api.hosts.PublicHttpAccess``).
This module holds no database session, no encryption key, and no application
settings — only the OAuth 2.1 (RFC 8414/9728) and RFC 7636/7591 protocol logic
and its safe error mapping back to the browser callback.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx
from lumen_plugin_api.contracts import Namespace, PluginError, PluginHost
from lumen_plugin_api.mcp import McpConnectionStore, McpOAuthConfig, McpOAuthError
from pydantic import ValidationError

logger = logging.getLogger(__name__)

_REQUEST_LIFETIME = timedelta(minutes=10)
_REFRESH_SKEW = timedelta(seconds=60)


def _now() -> datetime:
    return datetime.now(UTC)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _https_url(value: object, *, field: str, allow_query: bool = False) -> str:
    if not isinstance(value, str) or len(value) > 2048:
        raise McpOAuthError(f"OAuth {field} is invalid")
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise McpOAuthError(f"OAuth {field} is invalid") from exc
    if (
        parsed.scheme.lower() != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.fragment
        or (parsed.query and not allow_query)
    ):
        raise McpOAuthError(f"OAuth {field} must use a public HTTPS URL")
    return urlunsplit(("https", parsed.netloc, parsed.path or "/", parsed.query, ""))


def _well_known_candidates(origin: str, name: str) -> tuple[str, ...]:
    """Support MCP's origin metadata endpoint and RFC path-scoped issuer metadata."""
    parsed = urlsplit(_https_url(origin, field="metadata issuer"))
    root = urlunsplit(("https", parsed.netloc, f"/.well-known/{name}", "", ""))
    suffix = parsed.path.rstrip("/")
    if not suffix:
        return (root,)
    return (root, urlunsplit(("https", parsed.netloc, f"/.well-known/{name}{suffix}", "", "")))


def _pkce_verifier() -> str:
    # RFC 7636 unreserved charset; token_urlsafe only emits URL-safe unreserved characters.
    return secrets.token_urlsafe(64)


def _pkce_challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")


def _token_expiry(payload: dict[str, Any]) -> datetime | None:
    raw = payload.get("expires_in")
    if not isinstance(raw, (int, float)) or isinstance(raw, bool) or raw <= 0:
        return None
    return _now() + timedelta(seconds=min(float(raw), 365 * 24 * 60 * 60))


def _canonical_issuer(value: str) -> str:
    return value.rstrip("/")


def _callback_issuer_matches(expected: object, supplied: str | None) -> bool:
    """Validate a callback issuer without dereferencing callback-controlled URLs."""
    if supplied is None:
        return True
    if not isinstance(expected, str) or len(supplied) > 2048:
        return False
    try:
        parsed = urlsplit(supplied)
    except ValueError:
        return False
    if (
        parsed.scheme.lower() != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        return False
    normalized = urlunsplit(("https", parsed.netloc, parsed.path or "/", "", ""))
    return hmac.compare_digest(_canonical_issuer(expected), _canonical_issuer(normalized))


def _normalize_origin(value: object) -> str | None:
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
        not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or scheme not in {"https", "http"}
    ):
        return None
    host = f"[{hostname}]" if ":" in hostname and not hostname.startswith("[") else hostname
    if port is not None and not ((scheme == "https" and port == 443) or (scheme == "http" and port == 80)):
        host = f"{host}:{port}"
    return f"{scheme}://{host}"


def _approved_origin(config: McpOAuthConfig, value: object) -> str | None:
    """Return a configured browser origin only when it is explicitly allowlisted."""
    origin = _normalize_origin(value)
    if origin is None:
        return None
    return origin if origin in config.allowed_return_origins else None


def _verify_initiator_nonce(payload: dict[str, Any], initiator_nonce: str | None) -> None:
    expected_hash = payload.get("initiator_nonce_hash")
    if (
        not isinstance(initiator_nonce, str)
        or not isinstance(expected_hash, str)
        or not hmac.compare_digest(_hash(initiator_nonce), expected_hash)
    ):
        raise McpOAuthError("OAuth callback was not initiated by this browser")


class RemoteOAuth:
    """Remote MCP OAuth 2.1 client. Tokens and state never leave the host's encrypted store."""

    def __init__(self, host: PluginHost) -> None:
        if host.mcp_connections is None or host.public_http is None:
            raise PluginError("plugin_unavailable", "OAuth requires a connection store and public HTTP capability")
        try:
            self._config = McpOAuthConfig.model_validate(host.mcp_connections.oauth_configuration())
        except (ValidationError, ValueError, TypeError) as exc:
            raise PluginError("plugin_configuration_changed", "OAuth host configuration is invalid") from exc
        self._store: McpConnectionStore = host.mcp_connections
        self._http = host.public_http

    # -- discovery -----------------------------------------------------------

    async def _json_response(self, response: httpx.Response, *, operation: str) -> dict[str, Any]:
        if response.status_code < 200 or response.status_code >= 300:
            raise McpOAuthError(f"OAuth {operation} failed ({response.status_code})")
        try:
            payload = response.json()
        except ValueError as exc:
            raise McpOAuthError(f"OAuth {operation} returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise McpOAuthError(f"OAuth {operation} returned invalid JSON")
        return payload

    async def _metadata_document(
        self, client: httpx.AsyncClient, candidates: tuple[str, ...], *, operation: str
    ) -> dict[str, Any] | None:
        """Fetch metadata from explicitly validated locations; only 404 permits fallback."""
        for url in candidates:
            response = await client.get(url)
            if response.status_code == 404:
                continue
            return await self._json_response(response, operation=operation)
        return None

    async def _discover(self, server_url: str) -> dict[str, Any] | None:
        """Discover standards-compliant OAuth metadata through the host's DNS-pinned boundary."""
        self._http.validate_url(server_url)
        async with self._http.client() as client:
            protected = await self._metadata_document(
                client,
                _well_known_candidates(server_url, "oauth-protected-resource"),
                operation="protected-resource discovery",
            )
            if protected is None:
                return None
            authorization_servers = protected.get("authorization_servers")
            if not isinstance(authorization_servers, list) or not authorization_servers:
                raise McpOAuthError("This MCP server advertised invalid OAuth metadata")
            issuer = _https_url(authorization_servers[0], field="authorization server")
            self._http.validate_url(issuer)
            metadata = await self._metadata_document(
                client,
                _well_known_candidates(issuer, "oauth-authorization-server"),
                operation="authorization-server discovery",
            )
        if metadata is None:
            raise McpOAuthError("OAuth authorization server metadata is unavailable")
        advertised_issuer = _https_url(metadata.get("issuer"), field="authorization issuer")
        if not hmac.compare_digest(_canonical_issuer(advertised_issuer), _canonical_issuer(issuer)):
            raise McpOAuthError("OAuth authorization server issuer does not match its metadata")
        authorization_endpoint = _https_url(
            metadata.get("authorization_endpoint"), field="authorization endpoint", allow_query=True
        )
        token_endpoint = _https_url(metadata.get("token_endpoint"), field="token endpoint")
        raw_registration_endpoint = metadata.get("registration_endpoint")
        registration_endpoint = (
            _https_url(raw_registration_endpoint, field="registration endpoint")
            if raw_registration_endpoint is not None
            else None
        )
        resource = protected.get("resource")
        if resource is not None:
            resource = _https_url(resource, field="protected resource")
            self._http.validate_url(resource)
        for url in (advertised_issuer, authorization_endpoint, token_endpoint, registration_endpoint):
            if url is not None:
                self._http.validate_url(url)
        return {
            "issuer": _canonical_issuer(issuer),
            "authorization_endpoint": authorization_endpoint,
            "token_endpoint": token_endpoint,
            "registration_endpoint": registration_endpoint,
            "resource": resource or server_url,
        }

    async def _register_client(self, metadata: dict[str, Any]) -> dict[str, str]:
        registration_endpoint = metadata.get("registration_endpoint")
        if not isinstance(registration_endpoint, str):
            raise McpOAuthError(
                "This MCP server does not support dynamic client registration; "
                "configure an OAuth client in the administrator settings"
            )
        body = {
            "client_name": "Afterglow Chat",
            "redirect_uris": [self._config.callback_url],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        }
        self._http.validate_url(registration_endpoint)
        async with self._http.client() as client:
            response = await client.post(registration_endpoint, json=body)
            registered = await self._json_response(response, operation="dynamic client registration")
        client_id = registered.get("client_id")
        if not isinstance(client_id, str) or not client_id:
            raise McpOAuthError("OAuth dynamic client registration returned no client ID")
        client_secret = registered.get("client_secret")
        if client_secret is not None and not isinstance(client_secret, str):
            raise McpOAuthError("OAuth dynamic client registration returned an invalid client secret")
        return {"client_id": client_id, "client_secret": client_secret or ""}

    # -- public API ------------------------------------------------------------

    async def detect(self, server_id: int, namespace: Namespace) -> dict[str, Any]:
        """Detect standards-compliant OAuth without accepting user-provided credentials."""
        server = await self._store.server(server_id, namespace)
        server_url = _https_url(server["url"], field="MCP server URL")
        try:
            metadata = await self._discover(server_url)
        except McpOAuthError:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            raise McpOAuthError("OAuth discovery could not connect to the MCP server") from exc
        detected_mode = "oauth" if metadata is not None else "none"
        can_connect = (
            metadata is None
            or server.get("static_client") is not None
            or metadata.get("registration_endpoint") is not None
        )
        if server.get("scope") == "user":
            await self._store.set_auth_mode(server_id, namespace, detected_mode)
        return {
            "auth_mode": detected_mode,
            "oauth_required": detected_mode == "oauth",
            "oauth_connection_available": can_connect,
        }

    async def begin_connect(
        self, server_id: int, namespace: Namespace, *, initiator_nonce: str, return_origin: str | None = None
    ) -> dict[str, str]:
        """Create an OAuth authorization request bound to one user, project, server, and browser."""
        if not self._config.callback_url:
            raise McpOAuthError("OAuth callback URL is not configured")
        if not isinstance(initiator_nonce, str) or len(initiator_nonce) < 32:
            raise McpOAuthError("OAuth initiator binding is invalid")
        server = await self._store.server(server_id, namespace)
        if server.get("auth_mode") != "oauth":
            raise McpOAuthError("This MCP server is not configured for OAuth")
        server_url = _https_url(server["url"], field="MCP server URL")
        client = server.get("static_client")
        scopes = tuple(server.get("oauth_scopes") or ())
        try:
            metadata = await self._discover(server_url)
            if metadata is None:
                raise McpOAuthError("This MCP server does not require standards-compliant OAuth")
            if client is None:
                client = await self._register_client(metadata)
        except McpOAuthError:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            raise McpOAuthError("OAuth discovery could not connect to the MCP server") from exc
        verifier = _pkce_verifier()
        approved_origin = _approved_origin(self._config, return_origin)
        state = secrets.token_urlsafe(32)
        payload = {
            **metadata,
            **client,
            "code_verifier": verifier,
            "callback_url": self._config.callback_url,
            "initiator_nonce_hash": _hash(initiator_nonce),
            "return_origin": approved_origin,
            "scopes": list(scopes),
        }
        await self._store.create_state(
            state_hash=_hash(state),
            server_id=server_id,
            namespace=namespace,
            payload=payload,
            expires_at=_now() + _REQUEST_LIFETIME,
        )
        query = {
            "response_type": "code",
            "client_id": client["client_id"],
            "redirect_uri": self._config.callback_url,
            "code_challenge": _pkce_challenge(verifier),
            "code_challenge_method": "S256",
            "state": state,
            "resource": metadata["resource"],
        }
        if scopes:
            query["scope"] = " ".join(scopes)
        separator = "&" if "?" in metadata["authorization_endpoint"] else "?"
        return {"authorization_url": f"{metadata['authorization_endpoint']}{separator}{urlencode(query)}"}

    async def complete_callback(
        self, *, state: str, code: str | None, error: str | None, iss: str | None, initiator_nonce: str | None
    ) -> tuple[int, str | None]:
        """Consume a callback once, exchange its code, and save an encrypted connection."""
        if not state or len(state) > 512:
            raise McpOAuthError("OAuth callback state is invalid")
        state_hash = _hash(state)
        payload = await self._store.consume_state(state_hash)
        return_origin: str | None = None
        try:
            _verify_initiator_nonce(payload, initiator_nonce)
            return_origin = _approved_origin(self._config, payload.get("return_origin"))
            if not _callback_issuer_matches(payload.get("issuer"), iss):
                raise McpOAuthError("OAuth callback issuer does not match the authorization request")
            if error:
                raise McpOAuthError("OAuth authorization was cancelled or denied")
            if not isinstance(code, str) or not code or len(code) > 4096:
                raise McpOAuthError("OAuth callback did not include an authorization code")
            token_form = {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": payload["callback_url"],
                "client_id": payload["client_id"],
                "code_verifier": payload["code_verifier"],
                "resource": payload["resource"],
            }
            if payload.get("client_secret"):
                token_form["client_secret"] = payload["client_secret"]
            self._http.validate_url(payload["token_endpoint"])
            async with self._http.client() as client:
                response = await client.post(payload["token_endpoint"], data=token_form)
                tokens = await self._json_response(response, operation="token exchange")
            access_token = tokens.get("access_token")
            if not isinstance(access_token, str) or not access_token:
                raise McpOAuthError("OAuth token exchange returned no access token")
            stored_tokens = {
                "access_token": access_token,
                "refresh_token": tokens.get("refresh_token") if isinstance(tokens.get("refresh_token"), str) else "",
                "client_id": payload["client_id"],
                "client_secret": payload.get("client_secret") or "",
                "issuer": payload["issuer"],
                "token_endpoint": payload["token_endpoint"],
                "resource": payload["resource"],
            }
            expires_at = _token_expiry(tokens)
            server_id = await self._store.complete_state(state_hash, token=stored_tokens, expires_at=expires_at)
        except McpOAuthError as exc:
            await self._store.set_state_status(state_hash, "failed")
            if return_origin is not None:
                exc.return_origin = return_origin
            raise
        except (httpx.HTTPError, ValueError) as exc:
            await self._store.set_state_status(state_hash, "failed")
            wrapped = McpOAuthError("OAuth token exchange could not be completed")
            wrapped.return_origin = return_origin
            raise wrapped from exc
        except Exception:
            await self._store.set_state_status(state_hash, "failed")
            raise
        return server_id, return_origin

    async def connection_status(self, server_id: int, namespace: Namespace) -> dict[str, Any]:
        server = await self._store.server(server_id, namespace)
        connection = await self._store.connection(server_id, namespace)
        return {
            "mcp_server_id": server_id,
            "required": server.get("auth_mode") == "oauth",
            "connected": bool(connection and connection.status == "active"),
            "expires_at": connection.expires_at.isoformat() if connection and connection.expires_at else None,
        }

    async def disconnect(self, server_id: int, namespace: Namespace) -> None:
        await self._store.disconnect(server_id, namespace)

    async def refresh(self, connection_id: str) -> dict[str, Any] | None:
        """Refresh under the store's row lock so rotating refresh tokens cannot be replayed."""
        try:
            async with self._store.locked_refresh(connection_id) as lease:
                connection = lease.connection
                if connection.status != "active":
                    return None
                token = connection.token
                if connection.expires_at is None or connection.expires_at > _now() + _REFRESH_SKEW:
                    return token
                refresh_token = token.get("refresh_token")
                if not isinstance(refresh_token, str) or not refresh_token:
                    return None
                form = {
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": token.get("client_id", ""),
                    "resource": token.get("resource", ""),
                }
                if token.get("client_secret"):
                    form["client_secret"] = token["client_secret"]
                self._http.validate_url(token["token_endpoint"])
                async with self._http.client() as client:
                    response = await client.post(token["token_endpoint"], data=form)
                    tokens = await self._json_response(response, operation="token refresh")
                access_token = tokens.get("access_token")
                if not isinstance(access_token, str) or not access_token:
                    raise McpOAuthError("OAuth token refresh returned no access token")
                refreshed = {
                    **token,
                    "access_token": access_token,
                    "refresh_token": (
                        tokens.get("refresh_token") if isinstance(tokens.get("refresh_token"), str) else refresh_token
                    ),
                }
                await lease.save(refreshed, _token_expiry(tokens))
                return refreshed
        except (httpx.HTTPError, ValueError):
            logger.info("MCP OAuth token refresh requires reconnection id=%s", connection_id, exc_info=True)
            return None

    async def headers_for_user(self, namespace: Namespace) -> dict[int, dict[str, str]]:
        """Return current user OAuth bearer headers; storage failures fail closed."""
        result: dict[int, dict[str, str]] = {}
        for connection in await self._store.connections(namespace):
            token = connection.token
            expires_at = connection.expires_at
            if expires_at is not None and expires_at <= _now() + _REFRESH_SKEW:
                refreshed = await self.refresh(connection.id)
                if refreshed is None:
                    continue
                token = refreshed
            access_token = token.get("access_token")
            if isinstance(access_token, str) and access_token:
                result[connection.server_id] = {"Authorization": f"Bearer {access_token}"}
        return result
