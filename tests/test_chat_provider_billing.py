from types import SimpleNamespace

from lumen.services.providers import billing


async def test_provider_billing_route_requires_admin(non_admin_client):
    response = await non_admin_client.get("/v1/admin/providers/1/billing")
    assert response.status_code == 403


async def test_provider_billing_route_returns_safe_snapshot(admin_client, monkeypatch):
    async def fake_snapshot(provider_id: int):
        assert provider_id == 3
        return {
            "provider_id": 3,
            "provider_type": "openrouter",
            "capability": "openrouter_key",
            "status": "available",
            "reason": None,
            "fetched_at": "2026-09-11T00:00:00+00:00",
            "is_available": None,
            "is_free_tier": False,
            "limit": "100",
            "remaining": "75",
            "usage_total": "25",
            "usage_daily": "1",
            "usage_weekly": "5",
            "usage_monthly": "20",
            "balances": [],
        }

    monkeypatch.setattr(billing, "get_provider_billing", fake_snapshot)
    response = await admin_client.get("/v1/admin/providers/3/billing")

    assert response.status_code == 200
    assert response.json()["remaining"] == "75"
    assert "api_key" not in response.text


def test_only_documented_inference_key_billing_endpoints_are_supported():
    assert billing.billing_capability_for("openrouter") == "openrouter_key"
    assert billing.billing_capability_for("deepseek") == "deepseek_balance"
    assert billing.billing_capability_for("openai") is None
    assert billing.billing_capability_for("openrouter", "chatgpt_device") is None


def test_openrouter_snapshot_maps_period_usage_and_limit():
    provider = SimpleNamespace(id=1, provider_type="openrouter")
    base = billing._base(provider, "openrouter_key")

    snapshot = billing._openrouter_snapshot(
        base,
        {
            "data": {
                "limit": 100,
                "limit_remaining": 72.5,
                "usage": 27.5,
                "usage_daily": 1.25,
                "usage_weekly": 4.5,
                "usage_monthly": 10.5,
                "is_free_tier": False,
            }
        },
    )

    assert snapshot["status"] == "available"
    assert snapshot["remaining"] == "72.5"
    assert snapshot["usage_weekly"] == "4.5"
    assert snapshot["usage_monthly"] == "10.5"


def test_deepseek_snapshot_keeps_purchased_and_granted_balances_separate():
    provider = SimpleNamespace(id=2, provider_type="deepseek")
    base = billing._base(provider, "deepseek_balance")

    snapshot = billing._deepseek_snapshot(
        base,
        {
            "is_available": True,
            "balance_infos": [
                {
                    "currency": "USD",
                    "total_balance": "110.00",
                    "granted_balance": "10.00",
                    "topped_up_balance": "100.00",
                }
            ],
        },
    )

    assert snapshot["is_available"] is True
    assert snapshot["balances"] == [{"currency": "USD", "total": "110.00", "granted": "10.00", "purchased": "100.00"}]


async def test_unsupported_provider_never_performs_outbound_request(monkeypatch):
    provider = SimpleNamespace(id=4, provider_type="openai", auth_mode="api_key")

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def get(self, _model, _provider_id):
            return provider

    called = False

    async def fail_request(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("unsupported provider must not issue a billing request")

    monkeypatch.setattr(billing, "is_db_available", lambda: True)
    monkeypatch.setattr(billing, "get_session_factory", lambda: lambda: Session())
    monkeypatch.setattr(billing, "_request_json", fail_request)

    result = await billing.get_provider_billing(4)

    assert result["status"] == "unsupported"
    assert result["reason"] == "billing_endpoint_unsupported"
    assert called is False


async def test_provider_authorization_failure_is_sanitized(monkeypatch):
    provider = SimpleNamespace(
        id=5,
        provider_type="openrouter",
        auth_mode="api_key",
        encrypted_api_key="ciphertext",
        api_key_env=None,
    )

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def get(self, _model, _provider_id):
            return provider

    async def rejected(*_args, **_kwargs):
        request = billing.httpx.Request("GET", "https://openrouter.ai/api/v1/key")
        response = billing.httpx.Response(401, request=request, text="secret upstream body")
        raise billing.httpx.HTTPStatusError("unauthorized", request=request, response=response)

    monkeypatch.setattr(billing, "is_db_available", lambda: True)
    monkeypatch.setattr(billing, "get_session_factory", lambda: lambda: Session())
    monkeypatch.setattr(billing, "resolve_api_key", lambda _provider: "secret-key")
    monkeypatch.setattr(billing, "_request_json", rejected)

    result = await billing.get_provider_billing(5)

    assert result["status"] == "unavailable"
    assert result["reason"] == "provider_authorization_failed"
    assert "secret" not in str(result)
