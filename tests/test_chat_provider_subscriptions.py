from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from lumen.main import app
from lumen.models.chat_db import LlmProvider, LlmProviderAuthAttempt
from lumen.services.providers import repository, subscriptions
from lumen.services.providers.errors import (
    ProviderAuthAttemptConflict,
    ProviderSubscriptionError,
    ProviderValidationError,
)


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _Session:
    def __init__(self, provider, attempt=None):
        self.provider = provider
        self.attempt = attempt

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    def begin(self):
        return _Transaction()

    async def execute(self, statement):
        entity = statement.column_descriptions[0].get("entity")
        if entity is LlmProviderAuthAttempt:
            return _ScalarResult(self.attempt)
        if entity is LlmProvider:
            return _ScalarResult(self.provider)
        raise AssertionError(f"unexpected query entity: {entity}")

    def add(self, value):
        assert isinstance(value, LlmProviderAuthAttempt)
        self.attempt = value

    async def flush(self):
        return None


def _provider(**over):
    values = {
        "id": 7,
        "name": "subscription",
        "provider_type": "openai",
        "api_base": None,
        "encrypted_api_key": None,
        "api_key_env": None,
        "auth_mode": "chatgpt_device",
        "encrypted_subscription_tokens": None,
        "subscription_status": "disconnected",
        "subscription_expires_at": None,
        "subscription_generation": 0,
        "is_active": True,
        "margin_multiplier": 1,
        "models_dev_provider_id": None,
        "created_at": None,
        "updated_at": None,
    }
    values.update(over)
    return SimpleNamespace(**values)


def _attempt(provider, **over):
    now = datetime.now(UTC)
    values = {
        "provider_id": provider.id,
        "id": "attempt-1",
        "initiated_by_user_id": "admin-1",
        "initiated_by_project_id": "project-1",
        "provider_generation": provider.subscription_generation,
        "encrypted_payload": subscriptions._encrypt(
            {"device_auth_id": "device-1", "user_code": "ABCD-EFGH", "interval": 5}
        ),
        "status": "pending",
        "expires_at": now + timedelta(minutes=5),
        "next_poll_at": now - timedelta(seconds=1),
        "created_at": now,
    }
    values.update(over)
    return SimpleNamespace(**values)


def _jwt(*, expires_at: datetime, account_id: str | None = None) -> str:
    payload = {"exp": int(expires_at.timestamp())}
    if account_id is not None:
        payload["https://api.openai.com/auth"] = {"chatgpt_account_id": account_id}
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"header.{encoded}.signature"


def _configure(monkeypatch, provider, attempt=None):
    session = _Session(provider, attempt)
    monkeypatch.setattr(subscriptions, "_require_db", lambda: lambda: session)

    async def lock(_session, *, provider_id, model_ids=None):
        assert _session is session
        assert provider_id == provider.id
        assert model_ids is None
        return provider, []

    monkeypatch.setattr(subscriptions, "_lock_mutable_route", lock)
    return session


def _response(status: int, payload: dict | None = None, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(status, json=payload, headers=headers)


@pytest.mark.asyncio
async def test_device_start_persists_encrypted_attempt_and_is_idempotent(monkeypatch):
    provider = _provider()
    session = _configure(monkeypatch, provider)
    calls = []

    async def post(url, **kwargs):
        calls.append((url, kwargs))
        return _response(200, {"device_auth_id": "device-1", "user_code": "ABCD-EFGH", "interval": 2})

    monkeypatch.setattr(subscriptions, "_post", post)
    created = await subscriptions.begin_device_auth(provider.id, user_id="admin-1", project_id="project-1")
    repeated = await subscriptions.begin_device_auth(provider.id, user_id="admin-1", project_id="project-1")

    assert created["_created"] is True
    assert repeated["_created"] is False
    assert created["attempt_id"] == repeated["attempt_id"]
    assert repeated["user_code"] == "ABCD-EFGH"
    assert repeated["interval_seconds"] == 5
    assert len(calls) == 1
    assert "ABCD-EFGH" not in session.attempt.encrypted_payload
    assert subscriptions._decrypt(session.attempt.encrypted_payload)["device_auth_id"] == "device-1"


@pytest.mark.asyncio
async def test_device_start_does_not_reveal_another_initiators_pending_code(monkeypatch):
    provider = _provider()
    attempt = _attempt(provider)
    _configure(monkeypatch, provider, attempt)

    with pytest.raises(ProviderAuthAttemptConflict):
        await subscriptions.begin_device_auth(provider.id, user_id="admin-2", project_id="project-1")


@pytest.mark.asyncio
async def test_device_poll_before_interval_does_not_call_upstream(monkeypatch):
    provider = _provider()
    attempt = _attempt(provider, next_poll_at=datetime.now(UTC) + timedelta(seconds=30))
    _configure(monkeypatch, provider, attempt)

    async def forbidden(*args, **kwargs):
        raise AssertionError("upstream must not be polled before next_poll_at")

    monkeypatch.setattr(subscriptions, "_post", forbidden)
    result = await subscriptions.poll_device_auth(
        provider.id,
        attempt.id,
        user_id="admin-1",
        project_id="project-1",
    )

    assert result["status"] == "pending"


@pytest.mark.asyncio
async def test_device_poll_connects_and_clears_attempt_payload(monkeypatch):
    provider = _provider()
    attempt = _attempt(provider)
    _configure(monkeypatch, provider, attempt)
    expiry = datetime.now(UTC) + timedelta(hours=1)
    access_token = _jwt(expires_at=expiry)
    id_token = _jwt(expires_at=expiry, account_id="account-1")
    responses = [
        _response(200, {"authorization_code": "authorization-code", "code_verifier": "verifier"}),
        _response(
            200,
            {"access_token": access_token, "refresh_token": "refresh-token", "id_token": id_token},
        ),
    ]

    async def post(url, **kwargs):
        return responses.pop(0)

    monkeypatch.setattr(subscriptions, "_post", post)
    result = await subscriptions.poll_device_auth(
        provider.id,
        attempt.id,
        user_id="admin-1",
        project_id="project-1",
    )

    assert result["status"] == "connected"
    assert provider.subscription_generation == 1
    assert provider.subscription_status == "configured"
    assert attempt.encrypted_payload is None
    bundle = subscriptions._decrypt(provider.encrypted_subscription_tokens)
    assert bundle["account_id"] == "account-1"
    assert bundle["refresh_token"] == "refresh-token"


@pytest.mark.asyncio
async def test_cancel_clears_pending_payload_without_disconnect(monkeypatch):
    provider = _provider(subscription_status="configured", encrypted_subscription_tokens="existing")
    attempt = _attempt(provider)
    _configure(monkeypatch, provider, attempt)

    await subscriptions.cancel_device_auth(
        provider.id,
        attempt.id,
        user_id="admin-1",
        project_id="project-1",
    )

    assert attempt.status == "cancelled"
    assert attempt.encrypted_payload is None
    assert provider.encrypted_subscription_tokens == "existing"


@pytest.mark.asyncio
async def test_claude_token_is_validated_encrypted_and_resolved(monkeypatch):
    provider = _provider(provider_type="anthropic", auth_mode="anthropic_subscription")
    _configure(monkeypatch, provider)
    monkeypatch.setattr(subscriptions, "_provider_public", lambda row: {"id": row.id})
    token = "sk-ant-oat01-valid_subscription_token"

    result = await subscriptions.set_subscription_token(provider.id, token=token, expires_at=None)
    credential = await subscriptions.resolve_subscription_credential(
        {"provider_id": provider.id, "generation": 1, "auth_mode": "anthropic_subscription"}
    )

    assert result == {"id": provider.id}
    assert token not in provider.encrypted_subscription_tokens
    assert credential["access_token"] == token
    assert credential["_fingerprint"] == subscriptions._credential_fingerprint(token)


@pytest.mark.asyncio
async def test_refresh_rotates_tokens_without_changing_generation(monkeypatch):
    old_expiry = datetime.now(UTC) + timedelta(seconds=10)
    provider = _provider(
        subscription_status="configured",
        subscription_generation=4,
        subscription_expires_at=old_expiry,
        encrypted_subscription_tokens=subscriptions._encrypt(
            {
                "access_token": _jwt(expires_at=old_expiry),
                "refresh_token": "old-refresh",
                "id_token": _jwt(expires_at=old_expiry, account_id="account-1"),
                "account_id": "account-1",
                "expires_at": old_expiry.isoformat(),
            }
        ),
    )
    _configure(monkeypatch, provider)
    new_expiry = datetime.now(UTC) + timedelta(hours=1)
    new_access = _jwt(expires_at=new_expiry)
    new_id = _jwt(expires_at=new_expiry, account_id="account-1")

    async def post(url, **kwargs):
        return _response(200, {"access_token": new_access, "id_token": new_id})

    monkeypatch.setattr(subscriptions, "_post", post)
    credential = await subscriptions.resolve_subscription_credential(
        {"provider_id": provider.id, "generation": 4, "auth_mode": "chatgpt_device"}
    )

    assert credential["access_token"] == new_access
    assert credential["refresh_token"] == "old-refresh"
    assert provider.subscription_generation == 4
    assert subscriptions._decrypt(provider.encrypted_subscription_tokens)["access_token"] == new_access


@pytest.mark.asyncio
async def test_invalid_refresh_marks_current_provider_reauth_required(monkeypatch):
    expiry = datetime.now(UTC) + timedelta(seconds=10)
    provider = _provider(
        subscription_status="configured",
        subscription_generation=3,
        encrypted_subscription_tokens=subscriptions._encrypt(
            {
                "access_token": _jwt(expires_at=expiry),
                "refresh_token": "invalid-refresh",
                "id_token": _jwt(expires_at=expiry, account_id="account-1"),
                "account_id": "account-1",
                "expires_at": expiry.isoformat(),
            }
        ),
    )
    _configure(monkeypatch, provider)

    async def post(url, **kwargs):
        return _response(400, {"error": "invalid_grant"})

    monkeypatch.setattr(subscriptions, "_post", post)
    with pytest.raises(ProviderSubscriptionError) as raised:
        await subscriptions.resolve_subscription_credential(
            {"provider_id": provider.id, "generation": 3, "auth_mode": "chatgpt_device"}
        )

    assert raised.value.code == "subscription_auth_required"
    assert provider.subscription_status == "reauth_required"


@pytest.mark.asyncio
async def test_stale_rejection_cannot_overwrite_rotated_credential(monkeypatch):
    provider = _provider(
        provider_type="anthropic",
        auth_mode="anthropic_subscription",
        subscription_status="configured",
        subscription_generation=8,
        encrypted_subscription_tokens=subscriptions._encrypt(
            {"access_token": "sk-ant-oat01-current_subscription_token", "expires_at": None}
        ),
    )
    _configure(monkeypatch, provider)
    ref = {"provider_id": provider.id, "generation": 8, "auth_mode": "anthropic_subscription"}

    await subscriptions._mark_subscription_credential_rejected(
        ref,
        subscriptions._credential_fingerprint("sk-ant-oat01-stale_subscription_token"),
    )
    assert provider.subscription_status == "configured"

    await subscriptions._mark_subscription_credential_rejected(
        ref,
        subscriptions._credential_fingerprint("sk-ant-oat01-current_subscription_token"),
    )
    assert provider.subscription_status == "reauth_required"


def _public_subscription_provider(**over):
    values = {
        "id": 7,
        "name": "subscription",
        "provider_type": "anthropic",
        "api_base": None,
        "auth_mode": "anthropic_subscription",
        "has_credentials": True,
        "auth_status": "configured",
        "auth_expires_at": None,
        "has_api_key": False,
        "api_key_source": None,
        "api_key_env": None,
        "is_active": True,
        "margin_multiplier": 1.0,
        "models_dev_provider_id": None,
        "created_at": None,
        "updated_at": None,
    }
    values.update(over)
    return values


@pytest.mark.asyncio
async def test_subscription_auth_routes_require_admin(non_admin_client):
    forbidden = await non_admin_client.post("/api/v1/chat/admin/providers/7/auth/device")
    assert forbidden.status_code == 403


@pytest.mark.asyncio
async def test_subscription_auth_routes_require_authentication():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as raw_client:
        unauthenticated = await raw_client.post("/v1/admin/providers/7/auth/device")
    assert unauthenticated.status_code == 401

@pytest.mark.asyncio
async def test_device_start_route_binds_initiator_and_preserves_no_store(admin_client, monkeypatch):
    calls = []

    async def begin(provider_id, *, user_id, project_id):
        calls.append((provider_id, user_id, project_id))
        return {
            "attempt_id": "attempt-1",
            "status": "pending",
            "verification_uri": "https://auth.openai.com/codex/device",
            "user_code": "ABCD-EFGH",
            "expires_at": "2026-01-01T00:15:00+00:00",
            "interval_seconds": 5,
            "_created": len(calls) == 1,
        }

    monkeypatch.setattr(subscriptions, "begin_device_auth", begin)
    created = await admin_client.post("/api/v1/chat/admin/providers/7/auth/device")
    repeated = await admin_client.post("/api/v1/chat/admin/providers/7/auth/device")

    assert created.status_code == 201
    assert repeated.status_code == 200
    assert created.headers["cache-control"] == "no-store"
    assert calls == [
        (7, "test-user-123", "test-project-123"),
        (7, "test-user-123", "test-project-123"),
    ]
    assert "_created" not in created.json()


@pytest.mark.asyncio
async def test_device_poll_attempt_mismatch_is_safe_404(admin_client, monkeypatch):
    async def poll(*args, **kwargs):
        raise subscriptions.ProviderNotFoundError("인증 요청을 찾을 수 없습니다")

    monkeypatch.setattr(subscriptions, "poll_device_auth", poll)
    response = await admin_client.post("/api/v1/chat/admin/providers/7/auth/device/other/poll")

    assert response.status_code == 404
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"detail": "인증 요청을 찾을 수 없습니다"}


@pytest.mark.asyncio
async def test_subscription_token_route_never_reflects_unknown_secret_field(admin_client):
    token = "sk-ant-oat01-never-reflect-this-token"
    response = await admin_client.put(
        "/api/v1/chat/admin/providers/7/auth/token",
        json={"token": token, token: "unknown-key"},
    )

    assert response.status_code == 422
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"detail": "프로바이더 요청 형식이 올바르지 않습니다"}
    assert token not in response.text


@pytest.mark.asyncio
async def test_subscription_token_and_disconnect_routes_keep_credentials_server_side(admin_client, monkeypatch):
    token = "sk-ant-oat01-valid_subscription_token"
    captured = {}

    async def set_token(provider_id, *, token, expires_at):
        captured["set"] = (provider_id, token, expires_at)
        return _public_subscription_provider()

    async def disconnect(provider_id):
        captured["disconnect"] = provider_id

    monkeypatch.setattr(subscriptions, "set_subscription_token", set_token)
    monkeypatch.setattr(subscriptions, "disconnect_subscription", disconnect)
    stored = await admin_client.put(
        "/api/v1/chat/admin/providers/7/auth/token",
        json={"token": token, "expires_at": None},
    )
    disconnected = await admin_client.delete("/api/v1/chat/admin/providers/7/auth")

    assert stored.status_code == 200
    assert stored.headers["cache-control"] == "no-store"
    assert token not in stored.text
    assert captured["set"] == (7, token, None)
    assert disconnected.status_code == 204
    assert disconnected.headers["cache-control"] == "no-store"
    assert captured["disconnect"] == 7


@pytest.mark.asyncio
async def test_subscription_provider_create_rejects_api_key_without_reflection(admin_client):
    token = "sk-ant-oat01-never-reflect-create-token"
    response = await admin_client.post(
        "/api/v1/chat/admin/providers",
        json={
            "name": "unsafe-subscription",
            "provider_type": "anthropic",
            "auth_mode": "anthropic_subscription",
            "api_key": token,
        },
    )

    assert response.status_code == 422
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"detail": "프로바이더 요청 형식이 올바르지 않습니다"}
    assert token not in response.text


@pytest.mark.asyncio
async def test_subscription_provider_create_passes_explicit_auth_contract(admin_client, monkeypatch):
    captured = {}

    async def create_provider(**kwargs):
        captured.update(kwargs)
        return _public_subscription_provider(
            name=kwargs["name"],
            provider_type="chatgpt",
            auth_mode="chatgpt_device",
            has_credentials=False,
            auth_status="disconnected",
        )

    monkeypatch.setattr(repository, "create_provider", create_provider)
    response = await admin_client.post(
        "/api/v1/chat/admin/providers",
        json={"name": "shared-chatgpt", "provider_type": "chatgpt", "auth_mode": "chatgpt_device"},
    )

    assert response.status_code == 201
    assert captured["auth_mode"] == "chatgpt_device"
    assert captured["api_key"] is None
    assert response.json()["has_credentials"] is False
    assert response.json()["has_api_key"] is False


@pytest.mark.asyncio
async def test_repository_rejects_patch_transition_into_subscription(monkeypatch):
    provider = _provider(auth_mode="api_key", provider_type="anthropic")
    session = _Session(provider)
    monkeypatch.setattr(repository, "_require_db", lambda: lambda: session)

    async def lock(_session, *, provider_id, model_ids=None):
        return provider, []

    monkeypatch.setattr(repository, "_lock_mutable_route", lock)
    with pytest.raises(ProviderValidationError, match="PATCH"):
        await repository.update_provider(provider.id, {"auth_mode": "anthropic_subscription"})
