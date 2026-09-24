"""Behavioral contract for the packaged remote OAuth client and its host capabilities."""
from __future__ import annotations

import asyncio
import base64
import hashlib
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from lumen_mcp_default.remote import create_remote_plugin
from lumen_plugin_api.contracts import Namespace, PluginHost
from lumen_plugin_api.mcp import McpConnection, McpOAuthError

CALLBACK = "https://lumen.example/api/v1/chat/mcp-oauth/callback"
FRONTEND = "https://app.example"
SERVER = "https://mcp.example/tools"
ISSUER = "https://auth.example"
USER = Namespace(user_id="alice", project_id="team")
NONCE = "0123456789abcdef0123456789abcdef"


class FakeHttp:
    def __init__(self):
        self.token_forms: list[dict[str, list[str]]] = []
        self.registration_count = 0
        self.protected_status = 200
        self.bad_issuer = False

    def validate_url(self, url: str) -> None:
        if urlsplit(url).scheme != "https" or urlsplit(url).hostname not in {"mcp.example", "auth.example"}:
            raise ValueError("URL is not public HTTPS")

    def client(self, *, timeout_seconds: float = 15, max_response_bytes: int = 65536):
        return httpx.AsyncClient(transport=httpx.MockTransport(self._response), trust_env=False)

    def _response(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/.well-known/oauth-protected-resource":
            return httpx.Response(
                self.protected_status,
                json={"authorization_servers": [ISSUER], "resource": SERVER},
            )
        if path == "/.well-known/oauth-authorization-server":
            return httpx.Response(200, json={
                "issuer": "https://impostor.example" if self.bad_issuer else ISSUER,
                "authorization_endpoint": f"{ISSUER}/authorize",
                "token_endpoint": f"{ISSUER}/token",
                "registration_endpoint": f"{ISSUER}/register",
            })
        if path == "/register":
            self.registration_count += 1
            return httpx.Response(201, json={"client_id": "dynamic-client"})
        if path == "/token":
            form = parse_qs(request.content.decode())
            self.token_forms.append(form)
            if form.get("grant_type") == ["authorization_code"]:
                return httpx.Response(200, json={
                    "access_token": "first-access", "refresh_token": "first-refresh", "expires_in": 1,
                })
            return httpx.Response(200, json={
                "access_token": "second-access", "refresh_token": "second-refresh", "expires_in": 3600,
            })
        raise AssertionError(f"unexpected public HTTP request: {request.method} {request.url}")


class FakeLease:
    def __init__(self, store: FakeStore, connection: McpConnection):
        self.store = store
        self.connection = connection

    async def save(self, token, expires_at):
        self.store._connection = self.connection.model_copy(update={
            "token": token, "expires_at": expires_at,
            "credential_epoch": self.connection.credential_epoch + 1,
        })

    async def revoke(self):
        self.store._connection = self.connection.model_copy(update={
            "token": {}, "expires_at": None, "status": "revoked",
            "credential_epoch": self.connection.credential_epoch + 1,
        })


class FakeStore:
    def __init__(self):
        self.auth_mode = "oauth"
        self.version = 3
        self.states: dict[str, dict] = {}
        self._connection: McpConnection | None = None
        self.lock = asyncio.Lock()

    def oauth_configuration(self):
        return {"callback_url": CALLBACK, "allowed_return_origins": (FRONTEND, "https://lumen.example")}

    async def server(self, server_id, namespace):
        assert server_id == 23 and namespace == USER
        return {
            "id": 23, "url": SERVER, "scope": "user", "auth_mode": self.auth_mode,
            "config_version": self.version, "static_client": None, "oauth_scopes": ["profile"],
        }

    async def set_auth_mode(self, server_id, namespace, auth_mode):
        await self.server(server_id, namespace)
        if self.auth_mode != auth_mode:
            self.auth_mode = auth_mode
            self.version += 1

    async def create_state(self, *, state_hash, server_id, namespace, payload, expires_at):
        await self.server(server_id, namespace)
        assert self.auth_mode == "oauth"
        self.states[state_hash] = {
            "status": "pending", "payload": payload, "expires_at": expires_at,
            "version": self.version,
        }

    async def consume_state(self, state_hash):
        async with self.lock:
            item = self.states.get(state_hash)
            if item is None or item["status"] != "pending" or item["expires_at"] <= datetime.now(UTC):
                raise McpOAuthError("OAuth authorization request has expired or was already used")
            item["status"] = "processing"
            return item["payload"]

    async def set_state_status(self, state_hash, status):
        item = self.states.get(state_hash)
        if item is not None and item["status"] == "processing":
            item["status"] = status

    async def complete_state(self, state_hash, *, token, expires_at):
        item = self.states[state_hash]
        if item["status"] != "processing":
            raise McpOAuthError("OAuth authorization request could not be completed")
        if item["version"] != self.version or self.auth_mode != "oauth":
            item["status"] = "failed"
            raise McpOAuthError("MCP server configuration changed; start OAuth again")
        epoch = self._connection.credential_epoch + 1 if self._connection is not None else 1
        self._connection = McpConnection(
            id="connection-23", server_id=23, user_id="alice", project_id="team",
            config_version=self.version, credential_epoch=epoch,
            status="active", token=token, expires_at=expires_at,
        )
        item["status"] = "completed"
        return 23

    async def connection(self, server_id, namespace):
        await self.server(server_id, namespace)
        return self._connection

    async def connections(self, namespace):
        assert namespace == USER
        row = self._connection
        return (row,) if row and row.status == "active" and row.config_version == self.version else ()

    async def disconnect(self, server_id, namespace):
        await self.server(server_id, namespace)
        if self._connection is not None:
            await FakeLease(self, self._connection).revoke()

    @asynccontextmanager
    async def locked_refresh(self, connection_id):
        async with self.lock:
            row = self._connection
            if row is None or row.id != connection_id or row.status != "active" or row.config_version != self.version:
                raise McpOAuthError("OAuth connection is no longer active")
            yield FakeLease(self, row)


async def provider():
    store, http = FakeStore(), FakeHttp()
    oauth = create_remote_plugin()
    await oauth.start(PluginHost(configuration={}, extensions=object(), mcp_connections=store, public_http=http))
    return oauth, store, http


def state_from_url(url: str) -> str:
    return parse_qs(urlsplit(url).query)["state"][0]


@pytest.mark.asyncio
async def test_discovery_and_browser_bound_pkce_callback_refresh_and_revoke():
    oauth, store, http = await provider()
    assert await oauth.detect(23, USER) == {
        "auth_mode": "oauth", "oauth_required": True, "oauth_connection_available": True,
    }
    started = await oauth.begin_connect(23, USER, initiator_nonce=NONCE, return_origin=FRONTEND)
    url = started["authorization_url"]
    params = parse_qs(urlsplit(url).query)
    assert params["code_challenge_method"] == ["S256"]
    assert params["redirect_uri"] == [CALLBACK]
    assert params["resource"] == [SERVER]
    assert params["scope"] == ["profile"]
    assert http.registration_count == 1
    state = state_from_url(url)
    assert await oauth.complete_callback(
        state=state, code="authorization-code", error=None, iss=ISSUER, initiator_nonce=NONCE,
    ) == (23, FRONTEND)
    verifier = http.token_forms[0]["code_verifier"][0]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode()
    assert params["code_challenge"] == [challenge]
    assert await oauth.connection_status(23, USER) == {
        "mcp_server_id": 23, "required": True, "connected": True,
        "expires_at": store._connection.expires_at.isoformat(),
    }
    assert await oauth.headers_for_user(USER) == {23: {"Authorization": "Bearer second-access"}}
    assert http.token_forms[1]["refresh_token"] == ["first-refresh"]
    assert store._connection.credential_epoch == 2
    assert await oauth.refresh(store._connection.id) == store._connection.token
    assert len(http.token_forms) == 2  # no second refresh of a valid token
    await oauth.disconnect(23, USER)
    assert await oauth.headers_for_user(USER) == {}
    assert (await oauth.connection_status(23, USER))["connected"] is False
    assert store._connection.credential_epoch == 3
    assert store._connection.token == {}
    with pytest.raises(McpOAuthError):
        await oauth.complete_callback(
            state=state, code="replayed", error=None, iss=ISSUER, initiator_nonce=NONCE,
        )


@pytest.mark.asyncio
async def test_wrong_browser_nonce_consumes_state_without_token_exchange_or_origin_redirect():
    oauth, store, http = await provider()
    state = state_from_url((await oauth.begin_connect(
        23, USER, initiator_nonce=NONCE, return_origin=FRONTEND,
    ))["authorization_url"])
    with pytest.raises(McpOAuthError) as failure:
        await oauth.complete_callback(
            state=state, code="secret-code", error=None, iss=ISSUER, initiator_nonce="other-browser",
        )
    assert failure.value.return_origin is None
    assert not http.token_forms
    assert store._connection is None
    assert next(iter(store.states.values()))["status"] == "failed"
    with pytest.raises(McpOAuthError):
        await oauth.complete_callback(
            state=state, code="secret-code", error=None, iss=ISSUER, initiator_nonce=NONCE,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs", [
    {"code": "secret-code", "error": None, "iss": "https://attacker.example"},
    {"code": None, "error": "access_denied", "iss": ISSUER},
    {"code": None, "error": None, "iss": ISSUER},
])
async def test_bad_callbacks_are_terminal_and_never_exchange_a_code(kwargs):
    oauth, store, http = await provider()
    state = state_from_url((await oauth.begin_connect(
        23, USER, initiator_nonce=NONCE, return_origin=FRONTEND,
    ))["authorization_url"])
    with pytest.raises(McpOAuthError) as failure:
        await oauth.complete_callback(state=state, initiator_nonce=NONCE, **kwargs)
    assert failure.value.return_origin == FRONTEND
    assert not http.token_forms
    assert store._connection is None
    assert next(iter(store.states.values()))["status"] == "failed"


@pytest.mark.asyncio
async def test_config_revision_changed_before_exchange_cannot_install_credential():
    oauth, store, http = await provider()
    state = state_from_url((await oauth.begin_connect(
        23, USER, initiator_nonce=NONCE, return_origin=FRONTEND,
    ))["authorization_url"])
    store.version += 1
    with pytest.raises(McpOAuthError):
        await oauth.complete_callback(
            state=state, code="secret-code", error=None, iss=ISSUER, initiator_nonce=NONCE,
        )
    assert http.token_forms  # exchange happened, but the stale grant was rejected at commit
    assert store._connection is None
    assert next(iter(store.states.values()))["status"] == "failed"


@pytest.mark.asyncio
async def test_discovery_fails_closed_on_advertised_issuer_mismatch():
    oauth, store, http = await provider()
    http.bad_issuer = True
    with pytest.raises(McpOAuthError):
        await oauth.detect(23, USER)
    assert store.auth_mode == "oauth"
    assert not store.states


@pytest.mark.asyncio
async def test_return_origin_must_be_explicitly_allowlisted():
    oauth, store, _ = await provider()
    state = state_from_url((await oauth.begin_connect(
        23, USER, initiator_nonce=NONCE, return_origin="https://evil.example",
    ))["authorization_url"])
    assert await oauth.complete_callback(
        state=state, code="secret-code", error=None, iss=ISSUER, initiator_nonce=NONCE,
    ) == (23, None)
    assert store._connection is not None


@pytest.mark.asyncio
async def test_concurrent_refresh_rotates_only_once_under_store_lock():
    oauth, store, http = await provider()
    state = state_from_url((await oauth.begin_connect(23, USER, initiator_nonce=NONCE))["authorization_url"])
    await oauth.complete_callback(state=state, code="code", error=None, iss=ISSUER, initiator_nonce=NONCE)
    first, second = await asyncio.gather(oauth.refresh("connection-23"), oauth.refresh("connection-23"))
    assert first == second == store._connection.token
    assert store._connection.credential_epoch == 2
    assert [form["refresh_token"] for form in http.token_forms if "refresh_token" in form] == [["first-refresh"]]
    await oauth.close()
