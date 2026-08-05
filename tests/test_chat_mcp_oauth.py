"""Remote MCP OAuth discovery and chat-extension routing contracts."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from lumen.api import extensions
from lumen.api import mcp_oauth as mcp_oauth_callback
from lumen.services import mcp_oauth


async def _async_value(value):
    return value


@pytest.fixture(autouse=True)
def _public_oauth_endpoints(monkeypatch):
    monkeypatch.setattr(mcp_oauth.ssrf, "validate_url", lambda url: url)


class _AsyncNullContext:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _OAuthBeginSession:
    def __init__(self, server):
        self.server = server
        self.requests = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def begin(self):
        return _AsyncNullContext()

    async def get(self, _model, _server_id):
        return self.server

    def add(self, request):
        self.requests.append(request)


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

    def test_rejects_victim_browser_nonce_for_attacker_request(self):
        attacker_nonce = "a" * 43
        with pytest.raises(mcp_oauth.McpOAuthError, match="not initiated by this browser"):
            mcp_oauth._verify_initiator_nonce(
                {"initiator_nonce_hash": mcp_oauth._hash(attacker_nonce)},
                "victim-browser-nonce",
            )


class TestMcpOAuthDiscovery:
    async def test_discovers_https_metadata_and_registration_endpoint(self, monkeypatch):
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            payloads = {
                "https://mcp.example/.well-known/oauth-protected-resource": {
                    "resource": "https://mcp.example/mcp",
                    "authorization_servers": ["https://auth.example"],
                },
                "https://auth.example/.well-known/oauth-authorization-server": {
                    "issuer": "https://auth.example",
                    "authorization_endpoint": "https://auth.example/authorize",
                    "token_endpoint": "https://auth.example/token",
                    "registration_endpoint": "https://auth.example/register",
                },
            }
            return httpx.Response(200, json=payloads[str(request.url)])

        monkeypatch.setattr(
            mcp_oauth,
            "_http_client",
            lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://unused.example"),
        )

        metadata = await mcp_oauth._discover("https://mcp.example/mcp")

        assert metadata == {
            "issuer": "https://auth.example",
            "authorization_endpoint": "https://auth.example/authorize",
            "token_endpoint": "https://auth.example/token",
            "registration_endpoint": "https://auth.example/register",
            "resource": "https://mcp.example/mcp",
        }
        assert seen == [
            "https://mcp.example/.well-known/oauth-protected-resource",
            "https://auth.example/.well-known/oauth-authorization-server",
        ]

    async def test_allows_metadata_without_dynamic_registration(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "mcp.example":
                return httpx.Response(200, json={"authorization_servers": ["https://auth.example"]})
            return httpx.Response(
                200,
                json={
                    "issuer": "https://auth.example",
                    "authorization_endpoint": "https://auth.example/authorize",
                    "token_endpoint": "https://auth.example/token",
                },
            )

        monkeypatch.setattr(
            mcp_oauth,
            "_http_client",
            lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://unused.example"),
        )

        metadata = await mcp_oauth._discover("https://mcp.example/mcp")

        assert metadata["registration_endpoint"] is None

    async def test_rejects_authorization_metadata_with_wrong_issuer(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "mcp.example":
                return httpx.Response(200, json={"authorization_servers": ["https://auth.example"]})
            return httpx.Response(
                200,
                json={
                    "issuer": "https://other-auth.example",
                    "authorization_endpoint": "https://auth.example/authorize",
                    "token_endpoint": "https://auth.example/token",
                },
            )

        monkeypatch.setattr(
            mcp_oauth,
            "_http_client",
            lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://unused.example"),
        )

        with pytest.raises(mcp_oauth.McpOAuthError, match="issuer does not match"):
            await mcp_oauth._discover("https://mcp.example/mcp")

    def test_rejects_callback_issuer_that_differs_from_bound_issuer(self):
        assert mcp_oauth._callback_issuer_matches("https://auth.example", "https://auth.example/")
        assert not mcp_oauth._callback_issuer_matches("https://auth.example", "https://attacker.example")
        assert not mcp_oauth._callback_issuer_matches("https://auth.example", "http://auth.example")

    def test_pending_callback_and_connection_tokens_bind_to_server_revision(self):
        server = SimpleNamespace(is_active=True, auth_mode="oauth", config_version=4)

        assert mcp_oauth._oauth_configuration_matches(server, 4)
        server.config_version = 5  # URL or OAuth configuration was changed while the browser authorized.
        assert not mcp_oauth._oauth_configuration_matches(server, 4)


class TestMcpOAuthAuthorizationOwnership:
    @pytest.mark.parametrize("scope", ("global", "user"))
    async def test_begin_preserves_detected_oauth_mode(self, monkeypatch, scope):
        server = SimpleNamespace(
            id=7,
            scope=scope,
            is_active=True,
            url="https://mcp.example/mcp",
            owner_user_id="user-1",
            owner_project_id="project-1",
            auth_mode="oauth",
            config_version=4,
        )
        session = _OAuthBeginSession(server)
        metadata = {
            "issuer": "https://auth.example",
            "authorization_endpoint": "https://auth.example/authorize",
            "token_endpoint": "https://auth.example/token",
            "registration_endpoint": "https://auth.example/register",
            "resource": "https://mcp.example/mcp",
        }

        monkeypatch.setattr(mcp_oauth, "get_session_factory", lambda: lambda: session)
        monkeypatch.setattr(mcp_oauth, "_discover", lambda _url: _async_value(metadata))
        monkeypatch.setattr(
            mcp_oauth,
            "_register_client",
            lambda _metadata, _callback_url: _async_value({"client_id": "client-1", "client_secret": ""}),
        )
        monkeypatch.setattr(mcp_oauth, "_callback_url", lambda: "https://console.example/oauth/callback")
        monkeypatch.setattr(mcp_oauth, "_encrypt", lambda _payload: "encrypted")

        result = await mcp_oauth.begin(7, user_id="user-1", project_id="project-1", initiator_nonce="n" * 43)

        assert result["authorization_url"].startswith("https://auth.example/authorize?")
        assert server.auth_mode == "oauth"
        assert len(session.requests) == 1

    async def test_detect_marks_user_source_oauth_without_accepting_credentials(self, monkeypatch):
        server = SimpleNamespace(
            id=7,
            scope="user",
            is_active=True,
            url="https://mcp.example/mcp",
            owner_user_id="user-1",
            owner_project_id="project-1",
            auth_mode="none",
            config_version=4,
        )
        session = _OAuthBeginSession(server)
        metadata = {
            "issuer": "https://auth.example",
            "authorization_endpoint": "https://auth.example/authorize",
            "token_endpoint": "https://auth.example/token",
            "registration_endpoint": "https://auth.example/register",
            "resource": "https://mcp.example/mcp",
        }

        monkeypatch.setattr(mcp_oauth, "get_session_factory", lambda: lambda: session)
        monkeypatch.setattr(mcp_oauth, "_discover", lambda _url: _async_value(metadata))

        result = await mcp_oauth.detect(7, user_id="user-1", project_id="project-1")

        assert result == {
            "auth_mode": "oauth",
            "oauth_required": True,
            "oauth_connection_available": True,
        }
        assert server.auth_mode == "oauth"
        assert server.config_version == 5

    async def test_begin_uses_admin_static_client_and_declared_scopes(self, monkeypatch):
        server = SimpleNamespace(
            id=7,
            scope="global",
            is_active=True,
            url="https://mcp.example/mcp",
            auth_mode="oauth",
            oauth_client_id="static-client",
            encrypted_oauth_client_secret="encrypted-secret",
            oauth_scopes=["read", "write"],
            config_version=4,
        )
        session = _OAuthBeginSession(server)
        metadata = {
            "issuer": "https://auth.example",
            "authorization_endpoint": "https://auth.example/authorize",
            "token_endpoint": "https://auth.example/token",
            "registration_endpoint": None,
            "resource": "https://mcp.example/mcp",
        }
        encrypted_payloads: list[dict] = []

        monkeypatch.setattr(mcp_oauth, "get_session_factory", lambda: lambda: session)
        monkeypatch.setattr(mcp_oauth, "_discover", lambda _url: _async_value(metadata))
        monkeypatch.setattr(mcp_oauth, "decrypt_llm_provider_key", lambda _blob: "static-secret")
        monkeypatch.setattr(mcp_oauth, "_callback_url", lambda: "https://console.example/oauth/callback")
        monkeypatch.setattr(mcp_oauth, "_encrypt", lambda payload: encrypted_payloads.append(payload) or "encrypted")
        monkeypatch.setattr(
            mcp_oauth,
            "get_settings",
            lambda: SimpleNamespace(
                cors_origin_list=("https://console.example",),
                frontend_base_url="https://console.example",
                public_api_base="https://api.example",
            ),
        )

        result = await mcp_oauth.begin(
            7,
            user_id="user-1",
            project_id="project-1",
            initiator_nonce="n" * 43,
            return_origin="https://console.example",
        )
        assert "client_id=static-client" in result["authorization_url"]
        assert "scope=read+write" in result["authorization_url"]
        assert encrypted_payloads == [
            {
                **metadata,
                "client_id": "static-client",
                "client_secret": "static-secret",
                "code_verifier": encrypted_payloads[0]["code_verifier"],
                "callback_url": "https://console.example/oauth/callback",
                "initiator_nonce_hash": mcp_oauth._hash("n" * 43),
                "scopes": ["read", "write"],
                "return_origin": "https://console.example",
            }
        ]

    def test_local_callback_returns_to_local_chat(self, monkeypatch):
        monkeypatch.setattr(
            mcp_oauth_callback,
            "get_settings",
            lambda: SimpleNamespace(frontend_base_url="http://localhost:3080"),
        )

        assert mcp_oauth_callback._return_url(connected=True, server_id=7) == (
            "http://localhost:3080/dashboard/chat?mcp_oauth=connected&mcp_server_id=7"
        )


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
            == "http://localhost:3080/dashboard/chat?mcp_oauth=connected&mcp_server_id=7"
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
            response.headers["location"] == "http://localhost:3080/dashboard/chat?mcp_oauth=connected&mcp_server_id=7"
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
        assert response.headers["location"] == "http://localhost:3080/dashboard/chat?mcp_oauth=failed"

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
