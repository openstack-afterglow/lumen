"""Claude Code gateway device auth, credential, and native protocol contracts."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from lumen import auth
from lumen.main import app
from lumen.models.chat_db import ChatGatewayDeviceGrant
from lumen.services import api_key_store
from lumen.services import claude_gateway as gateway

_GATEWAY = "/v1/claude-gateway"
_GATEWAY_KEY = "sk-afgl-gateway-test"
_HEADERS = {"Authorization": f"Bearer {_GATEWAY_KEY}", "x-api-key": _GATEWAY_KEY}


async def _allow_rate(*args, **kwargs):
    return None


@pytest.fixture
def gateway_auth():
    async def principal():
        return {
            "auth_type": "api_key",
            "user_id": "user-1",
            "project_id": "project-1",
            "connection_project_id": "project-1",
            "api_key_id": 19,
            "scopes": gateway.GATEWAY_SCOPES,
            "credential_kind": "claude_gateway",
            "expires_at": datetime.now(UTC) + timedelta(hours=1),
            "source": "api",
            "roles": [],
            "is_system_admin": False,
        }

    previous = app.dependency_overrides.get(auth.get_principal)
    app.dependency_overrides[auth.get_principal] = principal
    yield
    if previous is None:
        app.dependency_overrides.pop(auth.get_principal, None)
    else:
        app.dependency_overrides[auth.get_principal] = previous


async def test_metadata_and_device_admission_are_oauth_shaped(client, monkeypatch):
    monkeypatch.setattr(
        gateway,
        "get_settings",
        lambda: SimpleNamespace(
            claude_gateway_base_url="https://lumen.example/v1/claude-gateway",
            frontend_base_url="https://afterglow.example",
        ),
    )
    monkeypatch.setattr(gateway, "enforce_rate_limit", _allow_rate)

    async def create_device_grant(*, client_id, scope):
        assert client_id == "claude-code"
        assert scope == gateway.GATEWAY_SCOPE
        return {
            "device_code": "dc_secret",
            "user_code": "ABCD-EFGH",
            "verification_uri": "https://afterglow.example/oauth/claude/authorize",
            "verification_uri_complete": "https://afterglow.example/oauth/claude/authorize?user_code=ABCD-EFGH",
            "expires_in": 600,
            "interval": 5,
        }

    monkeypatch.setattr(gateway, "create_device_grant", create_device_grant)
    metadata = (await client.get(f"{_GATEWAY}/.well-known/oauth-authorization-server")).json()
    assert metadata["device_authorization_endpoint"].endswith("/oauth/device/code")
    assert metadata["token_endpoint_auth_methods_supported"] == ["none"]

    response = await client.post(
        f"{_GATEWAY}/oauth/device/code",
        content=f"client_id=claude-code&scope={gateway.GATEWAY_SCOPE.replace(' ', '+')}",
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 200
    assert response.json()["user_code"] == "ABCD-EFGH"
    assert response.headers["cache-control"] == "no-store"


async def test_device_admission_rate_limiter_fails_closed(client, monkeypatch):
    async def unavailable(*args, **kwargs):
        raise gateway.GatewayError("temporarily_unavailable", status_code=503)

    monkeypatch.setattr(gateway, "enforce_rate_limit", unavailable)
    response = await client.post(
        f"{_GATEWAY}/oauth/device/code",
        content="client_id=claude-code",
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 503
    assert response.json() == {"error": "temporarily_unavailable"}


async def test_token_poll_preserves_oauth_device_errors(client, monkeypatch):
    async def pending(**kwargs):
        raise gateway.GatewayError("authorization_pending")

    monkeypatch.setattr(gateway, "exchange_device_code", pending)
    response = await client.post(
        f"{_GATEWAY}/oauth/token",
        content=(f"grant_type={gateway.DEVICE_GRANT_TYPE}&device_code=dc_secret&client_id=claude-code"),
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 400
    assert response.json() == {"error": "authorization_pending"}
    assert response.headers["cache-control"] == "no-store"


async def test_authorize_is_keystone_only_and_rate_limited(client, monkeypatch):
    token_override = app.dependency_overrides.pop(auth.require_token)
    try:
        unauthenticated = await client.post(
            f"{_GATEWAY}/authorize",
            json={"user_code": "ABCD-EFGH", "action": "approve"},
            headers={"X-Auth-Token": ""},
        )
    finally:
        app.dependency_overrides[auth.require_token] = token_override
    assert unauthenticated.status_code == 401

    monkeypatch.setattr(gateway, "enforce_rate_limit", _allow_rate)

    async def authorize(**kwargs):
        assert kwargs == {
            "user_code": "ABCD-EFGH",
            "approve": True,
            "owner_user_id": "test-user-123",
            "owner_project_id": "test-project-123",
        }
        return {"status": "approved"}

    monkeypatch.setattr(gateway, "authorize_user_code", authorize)
    approved = await client.post(
        f"{_GATEWAY}/authorize",
        json={"user_code": "ABCD-EFGH", "action": "approve"},
    )
    assert approved.status_code == 200
    assert approved.json() == {"status": "approved"}


async def test_identical_dual_headers_work_but_mismatch_is_rejected(client, gateway_auth, monkeypatch):
    monkeypatch.setattr(gateway, "configured_route", lambda: ("claude-sonnet", "anthropic"))

    async def resolve(model, *, provider=None):
        return {"model_name": model, "provider_name": provider, "display_name": "Claude Sonnet"}

    monkeypatch.setattr("lumen.api.claude_gateway.core.resolve_api", resolve)
    response = await client.get(f"{_GATEWAY}/v1/models", headers=_HEADERS)
    assert response.status_code == 200
    assert response.json()["data"][0]["id"] == "claude-sonnet"

    async def verify(raw):
        return {
            "user_id": "user-1",
            "project_id": "project-1",
            "api_key_id": 19,
            "scopes": gateway.GATEWAY_SCOPES,
            "credential_kind": "claude_gateway",
        }

    monkeypatch.setattr(api_key_store, "verify_key", verify)
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [
                (b"authorization", f"Bearer {_GATEWAY_KEY}".encode()),
                (b"x-api-key", _GATEWAY_KEY.encode()),
            ],
        }
    )
    assert (await auth.get_principal(request))["credential_kind"] == "claude_gateway"

    mismatched = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [
                (b"authorization", f"Bearer {_GATEWAY_KEY}".encode()),
                (b"x-api-key", b"sk-afgl-other"),
            ],
        }
    )
    with pytest.raises(HTTPException) as exc_info:
        await auth.get_principal(mismatched)
    assert exc_info.value.status_code == 400


async def test_managed_settings_support_conditional_etag(client, gateway_auth, monkeypatch):
    monkeypatch.setattr(gateway, "configured_route", lambda: ("claude-sonnet", "anthropic"))
    monkeypatch.setattr(gateway, "configured_base_url", lambda: "https://lumen.example/v1/claude-gateway")

    async def resolve(model, *, provider=None):
        return {"model_name": model, "provider_name": provider, "display_name": "Claude Sonnet"}

    monkeypatch.setattr("lumen.api.claude_gateway.core.resolve_api", resolve)
    first = await client.get(f"{_GATEWAY}/v1/managed-settings", headers=_HEADERS)
    assert first.status_code == 200
    assert first.json()["env"]["ANTHROPIC_BASE_URL"].endswith("/v1/claude-gateway")
    etag = first.headers["etag"]

    second = await client.get(f"{_GATEWAY}/v1/managed-settings", headers={**_HEADERS, "If-None-Match": etag})
    assert second.status_code == 304
    assert second.headers["etag"] == etag


async def test_gateway_messages_use_configured_route_and_preserve_native_blocks(client, gateway_auth, monkeypatch):
    monkeypatch.setattr(gateway, "configured_route", lambda: ("configured-claude", "anthropic"))
    captured = {}

    async def resolve(model, *, provider=None):
        assert (model, provider) == ("configured-claude", "anthropic")
        return {"model_name": model, "provider_name": provider}

    async def precheck(*args, **kwargs):
        return None

    async def complete(**kwargs):
        captured.update(kwargs)
        return {
            "id": "msg_native",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "thinking", "thinking": "trace"}, {"type": "text", "text": "done"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 3, "output_tokens": 2},
        }

    monkeypatch.setattr("lumen.api.claude_gateway.core.resolve_api", resolve)
    monkeypatch.setattr("lumen.api.claude_gateway.core.precheck", precheck)
    monkeypatch.setattr("lumen.api.claude_gateway.core.complete_anthropic", complete)
    response = await client.post(
        f"{_GATEWAY}/v1/messages",
        headers={
            **_HEADERS,
            "anthropic-beta": "context-management-2025-06-27",
            "anthropic-version": "2023-06-01",
        },
        json={
            "model": "client-requested-alias",
            "messages": [{"role": "user", "content": "hello"}],
            "max_tokens": 20000,
            "thinking": {"type": "enabled", "budget_tokens": 12000},
            "context_management": {"edits": [{"type": "clear_thinking_20251015", "keep": "all"}]},
            "output_config": {"effort": "high"},
            "tools": [{"name": "read", "description": "read", "input_schema": {"type": "object"}}],
        },
    )
    assert response.status_code == 200
    assert [block["type"] for block in response.json()["content"]] == ["thinking", "text"]
    assert captured["resolved"]["model_name"] == "configured-claude"
    assert captured["max_tokens"] == 20000
    assert captured["options"]["thinking"]["budget_tokens"] == 12000
    assert captured["options"]["tools"][0]["name"] == "read"
    assert captured["options"]["context_management"]["edits"][0]["keep"] == "all"
    assert captured["options"]["output_config"] == {"effort": "high"}
    assert captured["options"]["anthropic_headers"] == {
        "anthropic-beta": "context-management-2025-06-27",
        "anthropic-version": "2023-06-01",
    }


async def test_gateway_count_tokens_does_not_require_generation_budget(client, gateway_auth, monkeypatch):
    monkeypatch.setattr(gateway, "configured_route", lambda: ("configured-claude", "anthropic"))

    async def resolve(model, *, provider=None):
        return {"model_name": model, "provider_name": provider}

    async def count(*, resolved, payload, anthropic_headers):
        assert anthropic_headers == {}
        assert "max_tokens" not in payload
        assert payload["messages"][0]["content"] == "hello"
        return {"input_tokens": 17}

    monkeypatch.setattr("lumen.api.claude_gateway.core.resolve_api", resolve)
    monkeypatch.setattr("lumen.api.claude_gateway.core.count_anthropic_tokens", count)
    response = await client.post(
        f"{_GATEWAY}/v1/messages/count_tokens",
        headers=_HEADERS,
        json={"model": "client-alias", "messages": [{"role": "user", "content": "hello"}]},
    )
    assert response.status_code == 200
    assert response.json() == {"input_tokens": 17}


async def test_consumed_device_grant_requires_repair_after_lost_token_response(monkeypatch):
    now = datetime.now(UTC)
    row = ChatGatewayDeviceGrant(
        id="grant-1",
        device_code_hash=gateway._hash("dc_secret"),
        user_code_hash=gateway._hash("ABCDEFGH"),
        client_id_hash=gateway._hash("claude-code"),
        status="approved",
        created_at=now,
        expires_at=now + timedelta(minutes=5),
        next_poll_at=now - timedelta(seconds=1),
        poll_interval_seconds=5,
        owner_user_id="user-1",
        owner_project_id="project-1",
    )

    class Result:
        def scalar_one_or_none(self):
            return row

    class Session:
        def __init__(self):
            self.key = None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def begin(self):
            return self

        async def execute(self, statement):
            return Result()

        def add(self, value):
            self.key = value

        async def flush(self):
            self.key.id = 88

    session = Session()
    monkeypatch.setattr(gateway, "_factory", lambda: lambda: session)
    issued = await gateway.exchange_device_code(
        device_code="dc_secret", grant_type=gateway.DEVICE_GRANT_TYPE, client_id="claude-code"
    )
    assert issued["access_token"].startswith("sk-afgl-")
    assert "refresh_token" not in issued
    assert row.status == "consumed" and row.issued_api_key_id == 88
    assert session.key.credential_kind == "claude_gateway"
    assert session.key.expires_at <= now + timedelta(hours=24, seconds=2)

    with pytest.raises(gateway.GatewayError, match="pair again") as exc_info:
        await gateway.exchange_device_code(
            device_code="dc_secret", grant_type=gateway.DEVICE_GRANT_TYPE, client_id="claude-code"
        )
    assert exc_info.value.code == "invalid_grant"


async def test_redis_rate_limit_failure_is_fail_closed(monkeypatch):
    async def unavailable():
        raise ConnectionError("redis down")

    monkeypatch.setattr(gateway, "_get_redis", unavailable)
    with pytest.raises(gateway.GatewayError) as exc_info:
        await gateway.enforce_rate_limit("device-admission", "subject", limit=1)
    assert exc_info.value.status_code == 503
    assert exc_info.value.code == "temporarily_unavailable"
