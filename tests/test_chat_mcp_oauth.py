"""Core MCP OAuth lane: SQL-owned callback URL policy, config-revision matching,
and delegation of the protocol-level OAuth flow to the started remote-mcp plugin.

Discovery, PKCE, browser-bound state, and token refresh/rotation are owned by
the plugins/mcp-default package and exercised by its own test suite. This file
covers only what core itself is responsible for: the callback URL/origin policy
functions, the SQL config-revision matcher, delegation from the caller-facing
wrapper functions to the registry-resolved plugin, and the public callback route.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from lumen.api import extensions
from lumen.api import mcp_oauth as mcp_oauth_callback
from lumen.services import mcp_oauth


@pytest.fixture(autouse=True)
def _public_oauth_endpoints(monkeypatch):
    """Callback URL policy tests use reserved example hosts; configuration-time DNS is stubbed."""
    monkeypatch.setattr(mcp_oauth.ssrf, "validate_url", lambda url: url)


class TestMcpOAuthCallbackUrl:
    def test_uses_frontend_origin_when_public_api_base_is_unset(self, monkeypatch):
        monkeypatch.setattr(
            mcp_oauth,
            "get_settings",
            lambda: SimpleNamespace(public_api_base="", frontend_base_url="https://console.example"),
        )

        assert mcp_oauth._callback_url() == "https://console.example/api/v1/chat/mcp-oauth/callback"

    def test_uses_derived_loopback_http_callback_url_in_development(self, monkeypatch):
        callback_url = "http://127.0.0.1:8000/api/v1/chat/mcp-oauth/callback"
        monkeypatch.setenv("AFTERGLOW_ENV", "development")
        monkeypatch.setattr(
            mcp_oauth,
            "get_settings",
            lambda: SimpleNamespace(
                chat_mcp_oauth_callback_url="",
                public_api_base="http://127.0.0.1:8000",
                frontend_base_url="https://console.example",
            ),
        )

        assert mcp_oauth._callback_url() == callback_url
        assert mcp_oauth.callback_cookie_secure() is False

    def test_uses_explicit_configured_callback_url(self, monkeypatch):
        callback_url = "https://oauth.example.test/custom/mcp-callback"
        monkeypatch.setattr(
            mcp_oauth,
            "get_settings",
            lambda: SimpleNamespace(
                chat_mcp_oauth_callback_url=callback_url,
                public_api_base="https://api.example.test",
                frontend_base_url="https://console.example.test",
            ),
        )

        assert mcp_oauth._callback_url() == callback_url

    def test_uses_explicit_loopback_http_callback_url_in_development(self, monkeypatch):
        callback_url = "http://localhost:8000/api/v1/chat/mcp-oauth/callback"
        monkeypatch.setenv("AFTERGLOW_ENV", "development")
        monkeypatch.setattr(
            mcp_oauth,
            "get_settings",
            lambda: SimpleNamespace(chat_mcp_oauth_callback_url=callback_url),
        )

        assert mcp_oauth._callback_url() == callback_url
        assert mcp_oauth.callback_cookie_secure() is False

    def test_approves_only_allowlisted_browser_origin(self, monkeypatch):
        monkeypatch.setenv("AFTERGLOW_ENV", "development")
        monkeypatch.setattr(
            mcp_oauth,
            "get_settings",
            lambda: SimpleNamespace(
                cors_origin_list=("http://localhost:3080", "https://cloud.dmslab.re.kr"),
                frontend_base_url="https://cloud.dmslab.re.kr",
                public_api_base="http://localhost:8000",
            ),
        )

        assert mcp_oauth.approved_return_origin("http://localhost:3080") == "http://localhost:3080"
        assert mcp_oauth.approved_return_origin("https://cloud.dmslab.re.kr") == "https://cloud.dmslab.re.kr"
        assert mcp_oauth.approved_return_origin("https://attacker.example") is None
        assert mcp_oauth.approved_return_origin("https://cloud.dmslab.re.kr/redirect") is None

    def test_rejects_loopback_http_callback_url_in_production(self, monkeypatch):
        monkeypatch.setenv("AFTERGLOW_ENV", "production")
        monkeypatch.setattr(
            mcp_oauth,
            "get_settings",
            lambda: SimpleNamespace(chat_mcp_oauth_callback_url="http://localhost:8000/callback"),
        )

        with pytest.raises(mcp_oauth.McpOAuthError, match="public HTTPS"):
            mcp_oauth._callback_url()

    def test_rejects_malformed_loopback_callback_port(self, monkeypatch):
        monkeypatch.setenv("AFTERGLOW_ENV", "development")
        monkeypatch.setattr(
            mcp_oauth,
            "get_settings",
            lambda: SimpleNamespace(chat_mcp_oauth_callback_url="http://localhost:not-a-port/callback"),
        )

        with pytest.raises(mcp_oauth.McpOAuthError, match="URL is invalid"):
            mcp_oauth._callback_url()

    def test_local_callback_returns_to_local_chat(self, monkeypatch):
        monkeypatch.setattr(
            mcp_oauth_callback,
            "get_settings",
            lambda: SimpleNamespace(frontend_base_url="http://localhost:3080"),
        )

        assert mcp_oauth_callback._return_url(connected=True, server_id=7) == (
            "http://localhost:3080/dashboard/chat/settings?section=mcp&mcp_oauth=connected&mcp_server_id=7"
        )


class TestMcpOAuthConfigurationRevision:
    def test_pending_callback_and_connection_tokens_bind_to_server_revision(self):
        server = SimpleNamespace(is_active=True, auth_mode="oauth", config_version=4)

        assert mcp_oauth._oauth_configuration_matches(server, 4)
        server.config_version = 5  # URL or OAuth configuration was changed while the browser authorized.
        assert not mcp_oauth._oauth_configuration_matches(server, 4)


class _FakeOAuthProvider:
    """Records delegation from the caller-facing wrapper functions."""

    def __init__(self):
        self.calls: list[tuple[str, tuple, dict]] = []

    async def detect(self, server_id, namespace):
        self.calls.append(("detect", (server_id, namespace), {}))
        return {"auth_mode": "oauth", "oauth_required": True, "oauth_connection_available": True}

    async def begin_connect(self, server_id, namespace, *, initiator_nonce, return_origin=None):
        self.calls.append(("begin_connect", (server_id, namespace), {"initiator_nonce": initiator_nonce, "return_origin": return_origin}))
        return {"authorization_url": "https://auth.example/authorize?state=opaque"}

    async def complete_callback(self, *, state, code, error, iss, initiator_nonce):
        self.calls.append(("complete_callback", (), {"state": state, "code": code, "error": error, "iss": iss, "initiator_nonce": initiator_nonce}))
        return 7, "https://console.example"

    async def connection_status(self, server_id, namespace):
        self.calls.append(("connection_status", (server_id, namespace), {}))
        return {"mcp_server_id": server_id, "required": True, "connected": True, "expires_at": None}

    async def disconnect(self, server_id, namespace):
        self.calls.append(("disconnect", (server_id, namespace), {}))

    async def headers_for_user(self, namespace):
        self.calls.append(("headers_for_user", (namespace,), {}))
        return {7: {"Authorization": "Bearer token"}}


@pytest.fixture
def fake_oauth_provider(monkeypatch):
    provider = _FakeOAuthProvider()
    monkeypatch.setattr(mcp_oauth, "_oauth_provider", lambda: provider)
    return provider


class TestMcpOAuthCallerDelegation:
    """The caller-facing wrapper functions delegate to the registry-resolved plugin."""

    async def test_detect_delegates_to_started_plugin(self, fake_oauth_provider):
        result = await mcp_oauth.detect(7, user_id="user-1", project_id="project-1")

        assert result == {"auth_mode": "oauth", "oauth_required": True, "oauth_connection_available": True}
        [(name, args, kwargs)] = fake_oauth_provider.calls
        assert name == "detect"
        assert args[0] == 7
        assert (args[1].user_id, args[1].project_id) == ("user-1", "project-1")

    async def test_begin_delegates_initiator_nonce_and_return_origin(self, fake_oauth_provider):
        result = await mcp_oauth.begin(
            7, user_id="user-1", project_id="project-1", initiator_nonce="n" * 43, return_origin="https://console.example"
        )

        assert result == {"authorization_url": "https://auth.example/authorize?state=opaque"}
        [(name, args, kwargs)] = fake_oauth_provider.calls
        assert name == "begin_connect"
        assert args[0] == 7
        assert kwargs == {"initiator_nonce": "n" * 43, "return_origin": "https://console.example"}

    async def test_complete_delegates_callback_fields(self, fake_oauth_provider):
        result = await mcp_oauth.complete(state="opaque-state", code="oauth-code", error=None, iss=None, initiator_nonce="browser-nonce")

        assert result == (7, "https://console.example")
        [(name, args, kwargs)] = fake_oauth_provider.calls
        assert name == "complete_callback"
        assert kwargs == {"state": "opaque-state", "code": "oauth-code", "error": None, "iss": None, "initiator_nonce": "browser-nonce"}

    async def test_status_and_disconnect_delegate_with_namespace(self, fake_oauth_provider):
        status = await mcp_oauth.status(7, user_id="user-1", project_id="project-1")
        await mcp_oauth.disconnect(7, user_id="user-1", project_id="project-1")

        assert status == {"mcp_server_id": 7, "required": True, "connected": True, "expires_at": None}
        names = [call[0] for call in fake_oauth_provider.calls]
        assert names == ["connection_status", "disconnect"]

    async def test_headers_for_user_delegates_with_namespace(self, fake_oauth_provider):
        result = await mcp_oauth.headers_for_user(user_id="user-1", project_id="project-1")

        assert result == {7: {"Authorization": "Bearer token"}}
        [(name, args, kwargs)] = fake_oauth_provider.calls
        assert name == "headers_for_user"
        assert (args[0].user_id, args[0].project_id) == ("user-1", "project-1")


class TestMcpOAuthRoutes:
    def test_callback_returns_to_approved_initiator_origin(self, monkeypatch):
        settings = SimpleNamespace(
            cors_origin_list=("http://localhost:3080", "https://cloud.dmslab.re.kr"),
            frontend_base_url="https://cloud.dmslab.re.kr",
            public_api_base="http://localhost:8000",
        )
        monkeypatch.setenv("AFTERGLOW_ENV", "development")
        monkeypatch.setattr(mcp_oauth, "get_settings", lambda: settings)
        monkeypatch.setattr(mcp_oauth_callback, "get_settings", lambda: settings)

        assert (
            mcp_oauth_callback._return_url(connected=True, server_id=7, return_origin="http://localhost:3080")
            == "http://localhost:3080/dashboard/chat/settings?section=mcp&mcp_oauth=connected&mcp_server_id=7"
        )

    async def test_owner_can_start_oauth(self, client, monkeypatch):
        received_origins: list[str | None] = []

        async def fake_begin(server_id, *, user_id, project_id, initiator_nonce, return_origin):
            assert (server_id, user_id, project_id) == (7, "test-user-123", "test-project-123")
            assert len(initiator_nonce) >= 32
            received_origins.append(return_origin)
            return {"authorization_url": "https://auth.example/authorize?state=opaque"}

        monkeypatch.setattr(extensions.mcp_oauth, "begin", fake_begin)
        monkeypatch.setattr(extensions.mcp_oauth, "callback_cookie_secure", lambda: True)

        response = await client.post(
            "/api/v1/chat/mcp-servers/7/oauth/start", headers={"Origin": "http://localhost:3080"}
        )
        assert "httponly" in response.headers["set-cookie"].lower()
        assert "samesite=lax" in response.headers["set-cookie"].lower()
        assert "secure" in response.headers["set-cookie"].lower()
        assert response.status_code == 200
        assert response.json() == {"authorization_url": "https://auth.example/authorize?state=opaque"}
        assert received_origins == ["http://localhost:3080"]

    async def test_local_callback_uses_non_secure_initiator_cookie(self, client, monkeypatch):
        async def fake_begin(*_args, **_kwargs):
            return {"authorization_url": "https://auth.example/authorize?state=opaque"}

        monkeypatch.setattr(extensions.mcp_oauth, "begin", fake_begin)
        monkeypatch.setattr(extensions.mcp_oauth, "callback_cookie_secure", lambda: False)

        response = await client.post("/api/v1/chat/mcp-servers/7/oauth/start")

        assert response.status_code == 200
        assert "secure" not in response.headers["set-cookie"].lower()

    async def test_callback_passes_only_the_initiating_browser_cookie(self, client, monkeypatch):
        received: list[str | None] = []

        async def fake_complete(*, state, code, error, iss, initiator_nonce):
            assert (state, code, error, iss) == ("opaque-state", "oauth-code", None, None)
            received.append(initiator_nonce)
            return 7, "http://localhost:3080"

        monkeypatch.setattr(mcp_oauth_callback.mcp_oauth, "complete", fake_complete)
        client.cookies.set(mcp_oauth.INITIATOR_COOKIE, "initiator-browser-nonce")

        response = await client.get(
            "/api/v1/chat/mcp-oauth/callback?state=opaque-state&code=oauth-code",
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert (
            response.headers["location"]
            == "http://localhost:3080/dashboard/chat/settings?section=mcp&mcp_oauth=connected&mcp_server_id=7"
        )
        assert received == ["initiator-browser-nonce"]
        assert "max-age=0" in response.headers["set-cookie"].lower()

    async def test_callback_failure_returns_to_the_initiating_origin(self, client, monkeypatch):
        settings = SimpleNamespace(
            cors_origin_list=("http://localhost:3080",),
            frontend_base_url="https://cloud.dmslab.re.kr",
            public_api_base="http://localhost:8000",
        )

        async def fake_complete(**_kwargs):
            error = mcp_oauth.McpOAuthError("OAuth exchange failed")
            error.return_origin = "http://localhost:3080"
            raise error

        monkeypatch.setenv("AFTERGLOW_ENV", "development")
        monkeypatch.setattr(mcp_oauth, "get_settings", lambda: settings)
        monkeypatch.setattr(mcp_oauth_callback, "get_settings", lambda: settings)
        monkeypatch.setattr(mcp_oauth_callback.mcp_oauth, "complete", fake_complete)

        response = await client.get("/api/v1/chat/mcp-oauth/callback?state=opaque-state", follow_redirects=False)

        assert response.status_code == 303
        assert (
            response.headers["location"] == "http://localhost:3080/dashboard/chat/settings?section=mcp&mcp_oauth=failed"
        )

    async def test_owner_can_read_and_disconnect_oauth(self, client, monkeypatch):
        async def fake_status(server_id, *, user_id, project_id):
            assert (server_id, user_id, project_id) == (7, "test-user-123", "test-project-123")
            return {"mcp_server_id": 7, "required": True, "connected": True, "expires_at": None}

        disconnected: list[tuple[int, str, str]] = []

        async def fake_disconnect(server_id, *, user_id, project_id):
            disconnected.append((server_id, user_id, project_id))

        monkeypatch.setattr(extensions.mcp_oauth, "status", fake_status)
        monkeypatch.setattr(extensions.mcp_oauth, "disconnect", fake_disconnect)

        status = await client.get("/api/v1/chat/mcp-servers/7/oauth")
        deleted = await client.delete("/api/v1/chat/mcp-servers/7/oauth")

        assert status.status_code == 200
        assert status.json()["connected"] is True
        assert deleted.status_code == 204
        assert disconnected == [(7, "test-user-123", "test-project-123")]
