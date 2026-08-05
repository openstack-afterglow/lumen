"""Remote MCP source policy and secret-handling unit tests."""

from types import SimpleNamespace

import pytest

from lumen.models.chat_db import ChatMcpServer
from lumen.services import extensions_store as es

_VALID_KEY_HEX = "a" * 64


@pytest.fixture(autouse=True)
def _crypto_key(monkeypatch):
    monkeypatch.setattr(
        "lumen.crypto.get_settings",
        lambda: SimpleNamespace(get_lumen_encryption_key=_VALID_KEY_HEX),
    )
    monkeypatch.setattr(es.ssrf, "validate_url", lambda url: url)


def _row(**kw) -> ChatMcpServer:
    row = ChatMcpServer()
    row.id = kw.pop("id", 1)
    row.scope = kw.pop("scope", "user")
    row.name = kw.pop("name", "srv")
    row.transport = kw.pop("transport", "http")
    row.url = kw.pop("url", "https://mcp.example/mcp")
    row.command = None
    row.headers = kw.pop("headers", None)
    row.encrypted_headers = kw.pop("encrypted_headers", None)
    row.auth_mode = kw.pop("auth_mode", "none")
    row.oauth_scopes = kw.pop("oauth_scopes", None)
    row.oauth_client_id = kw.pop("oauth_client_id", None)
    row.encrypted_oauth_client_secret = kw.pop("encrypted_oauth_client_secret", None)
    row.is_active = kw.pop("is_active", True)
    row.config_version = kw.pop("config_version", 1)
    row.created_at = None
    return row


class TestTransportPolicy:
    def test_only_streamable_http_allowed(self):
        row = _row()
        es._apply_fields("mcp", row, {"transport": "http"})
        assert row.transport == "http"
        for transport in ("sse", "stdio", "grpc"):
            with pytest.raises(es.ExtensionValidationError):
                es._apply_fields("mcp", row, {"transport": transport})

    def test_non_https_scheme_rejected(self):
        row = _row()
        for url in ("ftp://mcp.example", "http://mcp.example"):
            with pytest.raises(es.ExtensionValidationError):
                es._apply_fields("mcp", row, {"url": url})

    def test_private_url_is_rejected(self, monkeypatch):
        row = _row()
        monkeypatch.setattr(es.ssrf, "validate_url", lambda _url: (_ for _ in ()).throw(es.ssrf.SsrfBlocked("private")))
        with pytest.raises(es.ExtensionValidationError, match="공개 HTTPS"):
            es._apply_fields("mcp", row, {"url": "https://private.example/mcp"})


class TestAdminApprovedStaticAuthentication:
    def test_headers_are_encrypted_and_never_public(self):
        row = _row(auth_mode="admin", scope="global")
        es._apply_fields("mcp", row, {"headers": {"Authorization": "Bearer secret-token"}})
        public = es._public_mcp(row)

        assert row.encrypted_headers
        assert "secret-token" not in row.encrypted_headers
        assert public["has_headers"] is True
        assert "headers" not in public
        assert es._reveal_mcp(row)["headers"] == {"Authorization": "Bearer secret-token"}

    def test_header_injection_is_rejected(self):
        row = _row(auth_mode="admin")
        with pytest.raises(es.ExtensionValidationError, match="header"):
            es._apply_fields("mcp", row, {"headers": {"Authorization\r\nX-Evil": "Bearer token"}})

    def test_headers_require_explicit_administrator_mode(self):
        with pytest.raises(es.ExtensionValidationError, match="관리자 승인"):
            es._apply_fields("mcp", _row(), {"headers": {"Authorization": "Bearer token"}})

    def test_oauth_configuration_requires_oauth_mode(self):
        with pytest.raises(es.ExtensionValidationError, match="auth_mode=oauth"):
            es._apply_fields("mcp", _row(), {"oauth_client_id": "client-id"})

    def test_static_headers_stop_flowing_when_administrator_mode_is_removed(self):
        row = _row(auth_mode="admin", scope="global")
        es._apply_fields("mcp", row, {"headers": {"Authorization": "Bearer secret-token"}})
        es._apply_fields("mcp", row, {"auth_mode": "none"})

        assert row.encrypted_headers
        assert es._reveal_mcp(row)["headers"] == {}

    def test_legacy_static_headers_are_administrator_gated(self):
        row = _row(auth_mode="headers", headers={"Authorization": "Bearer legacy"})
        assert es._public_mcp(row)["auth_mode"] == "admin"
        assert es._reveal_mcp(row)["headers"] == {}


class TestOAuthConfiguration:
    def test_static_oauth_secret_is_encrypted_and_never_public(self):
        row = _row()
        es._apply_fields(
            "mcp",
            row,
            {
                "auth_mode": "oauth",
                "oauth_scopes": ["read", "write"],
                "oauth_client_id": "client-id",
                "oauth_client_secret": "client-secret",
            },
        )

        public = es._public_mcp(row)
        assert row.encrypted_oauth_client_secret
        assert "client-secret" not in row.encrypted_oauth_client_secret
        assert public["oauth_scopes"] == ["read", "write"]
        assert public["has_oauth_client"] is True
        assert public["has_oauth_client_secret"] is True
        assert "oauth_client_id" not in public
        assert "client-secret" not in str(public)

    async def test_url_update_revokes_oauth_connections(self, monkeypatch):
        row = _row(auth_mode="oauth")
        active = SimpleNamespace(status="active", credential_version=4, expires_at=object())
        already_revoked = SimpleNamespace(status="revoked", credential_version=8, expires_at=object())

        class Result:
            def scalars(self):
                return [active, already_revoked]

        class Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            def begin(self):
                class Transaction:
                    async def __aenter__(self):
                        return self

                    async def __aexit__(self, *_args):
                        return False

                return Transaction()

            async def execute(self, _statement):
                return Result()

            async def flush(self):
                return None

        session = Session()

        async def fake_authorized(*_args, **_kwargs):
            return row

        monkeypatch.setattr(es, "_require_db", lambda: lambda: session)
        monkeypatch.setattr(es, "_load_authorized", fake_authorized)

        await es.update("mcp", 1, {"url": "https://new-mcp.example/mcp"}, admin=True)

        assert row.url == "https://new-mcp.example/mcp"
        assert row.config_version == 2
        assert (active.status, active.credential_version, active.expires_at) == ("revoked", 5, None)
        assert (already_revoked.status, already_revoked.credential_version) == ("revoked", 8)

    def test_user_owned_sources_cannot_accept_auth_configuration(self):
        with pytest.raises(es.ExtensionValidationError, match="관리자 승인"):
            es._ensure_user_mcp_policy({"headers": {"Authorization": "Bearer token"}})
