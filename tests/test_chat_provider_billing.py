from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from lumen.services.providers import billing


def _provider(
    provider_id: int,
    name: str,
    provider_type: str,
    *,
    auth_mode: str = "api_key",
    api_base: str | None = None,
    has_billing_admin_key: bool = False,
):
    return SimpleNamespace(
        id=provider_id,
        name=name,
        provider_type=provider_type,
        auth_mode=auth_mode,
        api_base=api_base,
        encrypted_api_key="ciphertext",
        encrypted_billing_admin_key="billing-ciphertext" if has_billing_admin_key else None,
        api_key_env=None,
    )


def _usage(*, monthly_cost: str = "0") -> dict:
    return {
        "currency": "USD",
        "requests": {"daily": "1", "weekly": "2", "monthly": "3", "total": "4"},
        "tokens": {"daily": "10", "weekly": "20", "monthly": "30", "total": "40"},
        "raw_cost": {"daily": "0.1", "weekly": "0.2", "monthly": monthly_cost, "total": "0.4"},
    }


async def test_provider_billing_route_requires_admin(non_admin_client):
    response = await non_admin_client.get("/v1/admin/providers/billing")
    assert response.status_code == 403


async def test_provider_billing_route_returns_safe_bulk_snapshot(admin_client, monkeypatch):
    async def fake_snapshots():
        return [
            {
                "provider_id": 3,
                "provider_name": "openrouter-prod",
                "provider_type": "openrouter",
                "capability": "openrouter_key",
                "status": "available",
                "reason": None,
                "fetched_at": "2026-09-11T00:00:00+00:00",
                "billing_url": "https://openrouter.ai/settings/credits",
                "usage_url": "https://openrouter.ai/activity",
                "has_billing_admin_key": False,
                "provider_usage": None,
                "local_usage": _usage(monthly_cost="12.5"),
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
        ]

    monkeypatch.setattr(billing, "list_provider_billing", fake_snapshots)
    response = await admin_client.get("/v1/admin/providers/billing")

    assert response.status_code == 200
    assert response.json()[0]["remaining"] == "75"
    assert response.json()[0]["local_usage"]["raw_cost"]["monthly"] == "12.5"
    assert "api_key" not in response.text
    assert "ciphertext" not in response.text


def test_only_documented_provider_billing_endpoints_are_supported():
    assert billing.billing_capability_for("openrouter") == "openrouter_key"
    assert billing.billing_capability_for("deepseek") == "deepseek_balance"
    assert billing.billing_capability_for("openai") == "openai_admin_usage"
    assert billing.billing_capability_for("anthropic") == "anthropic_admin_usage"
    assert billing.billing_capability_for("openai", api_base="https://proxy.example/v1") is None
    assert billing.billing_capability_for("openrouter", "chatgpt_device") is None


async def test_openai_admin_reports_normalize_current_period_cost_requests_and_tokens(monkeypatch):
    now = datetime(2026, 9, 13, 12, tzinfo=UTC)
    today = int(datetime(2026, 9, 13, tzinfo=UTC).timestamp())
    yesterday = int(datetime(2026, 9, 12, tzinfo=UTC).timestamp())
    calls: list[tuple[str, str, dict]] = []

    async def fake_request(endpoint: str, key: str, *, params=None):
        calls.append((endpoint, key, params))
        if endpoint == "openai_costs":
            return {
                "data": [
                    {"start_time": yesterday, "results": [{"amount": {"value": "1.25", "currency": "usd"}}]},
                    {"start_time": today, "results": [{"amount": {"value": "2.75", "currency": "usd"}}]},
                ]
            }
        return {
            "data": [
                {
                    "start_time": today,
                    "results": [
                        {"num_model_requests": 3, "input_tokens": 100, "output_tokens": 40},
                        {"num_model_requests": 2, "input_tokens": 20, "output_tokens": 10},
                    ],
                }
            ]
        }

    monkeypatch.setattr(billing, "_request_json", fake_request)
    base = billing._base(_provider(1, "openai", "openai", has_billing_admin_key=True), "openai_admin_usage")

    snapshot = await billing._organization_usage_snapshot(base, "openai_admin_usage", "admin-secret", now)

    assert snapshot["status"] == "available"
    assert snapshot["provider_usage"] == {
        "source": "openai_admin_usage",
        "currency": "USD",
        "cost": {"daily": "2.75", "weekly": "4.00", "monthly": "4.00", "total": None},
        "requests": {"daily": "5", "weekly": "5", "monthly": "5", "total": None},
        "tokens": {"daily": "170", "weekly": "170", "monthly": "170", "total": None},
    }
    assert {call[0] for call in calls} == {"openai_costs", "openai_usage"}
    assert all(call[1] == "admin-secret" for call in calls)
    assert all(call[2]["bucket_width"] == "1d" for call in calls)


async def test_anthropic_admin_reports_convert_minor_units_and_sum_token_classes(monkeypatch):
    now = datetime(2026, 9, 13, 12, tzinfo=UTC)
    starting_at = "2026-09-13T00:00:00Z"

    async def fake_request(endpoint: str, _key: str, *, params=None):
        assert params["bucket_width"] == "1d"
        if endpoint == "anthropic_costs":
            return {"data": [{"starting_at": starting_at, "results": [{"amount": "250", "currency": "USD"}]}]}
        return {
            "data": [
                {
                    "starting_at": starting_at,
                    "results": [
                        {
                            "uncached_input_tokens": 100,
                            "cache_read_input_tokens": 20,
                            "cache_creation": {
                                "ephemeral_1h_input_tokens": 5,
                                "ephemeral_5m_input_tokens": 10,
                            },
                            "output_tokens": 30,
                        }
                    ],
                }
            ]
        }

    monkeypatch.setattr(billing, "_request_json", fake_request)
    base = billing._base(
        _provider(2, "anthropic", "anthropic", has_billing_admin_key=True),
        "anthropic_admin_usage",
    )

    snapshot = await billing._organization_usage_snapshot(base, "anthropic_admin_usage", "admin-secret", now)

    assert snapshot["provider_usage"]["cost"]["monthly"] == "2.5"
    assert snapshot["provider_usage"]["requests"] is None
    assert snapshot["provider_usage"]["tokens"]["daily"] == "165"


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
async def test_organization_week_includes_prior_month_usage(monkeypatch, provider):
    now = datetime(2026, 9, 1, 12, tzinfo=UTC)
    days = [(datetime(2026, 8, 31, tzinfo=UTC), 10), (datetime(2026, 9, 1, tzinfo=UTC), 1)]

    async def report(endpoint, _key, *, params):
        if provider == "openai":
            lower_bound = datetime.fromtimestamp(params["start_time"], UTC)
        else:
            lower_bound = datetime.fromisoformat(params["starting_at"].replace("Z", "+00:00"))
        buckets = []
        for day, amount in days:
            if day < lower_bound:
                continue
            if provider == "openai":
                result = (
                    {"amount": {"value": str(amount), "currency": "usd"}}
                    if endpoint.endswith("costs")
                    else {"num_model_requests": amount, "input_tokens": amount, "output_tokens": 0}
                )
                buckets.append({"start_time": int(day.timestamp()), "results": [result]})
            else:
                result = (
                    {"amount": str(amount * 100), "currency": "USD"}
                    if endpoint.endswith("costs")
                    else {
                        "uncached_input_tokens": amount,
                        "cache_read_input_tokens": 0,
                        "cache_creation": {"ephemeral_1h_input_tokens": 0, "ephemeral_5m_input_tokens": 0},
                        "output_tokens": 0,
                    }
                )
                buckets.append({"starting_at": day.isoformat(), "results": [result]})
        return {"data": buckets}

    monkeypatch.setattr(billing, "_request_json", report)
    capability = f"{provider}_admin_usage"
    base = billing._base(_provider(1, provider, provider, has_billing_admin_key=True), capability)
    snapshot = await billing._organization_usage_snapshot(base, capability, "admin-secret", now)

    assert snapshot["status"] == "available"
    assert snapshot["provider_usage"]["cost"]["weekly"] == "11"
    assert snapshot["provider_usage"]["cost"]["monthly"] == "1"
    assert snapshot["provider_usage"]["tokens"]["weekly"] == "11"
    assert snapshot["provider_usage"]["tokens"]["monthly"] == "1"
    if provider == "openai":
        assert snapshot["provider_usage"]["requests"]["weekly"] == "11"


async def test_admin_report_partial_failure_preserves_success_without_exposing_upstream_body(monkeypatch):
    now = datetime(2026, 9, 13, 12, tzinfo=UTC)

    async def fake_request(endpoint: str, _key: str, *, params=None):
        if endpoint == "openai_costs":
            request = billing.httpx.Request("GET", "https://api.openai.com/v1/organization/costs")
            response = billing.httpx.Response(401, request=request, text="secret upstream body")
            raise billing.httpx.HTTPStatusError("failed", request=request, response=response)
        return {
            "data": [
                {
                    "start_time": int(datetime(2026, 9, 13, tzinfo=UTC).timestamp()),
                    "results": [{"num_model_requests": 1, "input_tokens": 10, "output_tokens": 5}],
                }
            ]
        }

    monkeypatch.setattr(billing, "_request_json", fake_request)
    base = billing._base(_provider(3, "openai", "openai", has_billing_admin_key=True), "openai_admin_usage")

    snapshot = await billing._organization_usage_snapshot(base, "openai_admin_usage", "admin-secret", now)

    assert snapshot["status"] == "available"
    assert snapshot["reason"] == "partial_provider_data"
    assert snapshot["provider_usage"]["cost"] is None
    assert snapshot["provider_usage"]["tokens"]["monthly"] == "15"
    assert "secret" not in str(snapshot)


async def test_admin_report_requires_separate_admin_credential():
    snapshot = await billing._provider_snapshot(_provider(4, "openai", "openai"), None)

    assert snapshot["status"] == "unavailable"
    assert snapshot["reason"] == "admin_credential_not_configured"
    assert snapshot["has_billing_admin_key"] is False


def test_provider_portals_are_fixed_and_exclude_custom_or_subscription_routes():
    assert billing.billing_portals_for(_provider(1, "openai", "openai"))[0] == (
        "https://platform.openai.com/settings/organization/billing/overview"
    )
    assert billing.billing_portals_for(_provider(2, "local", "openai", api_base="https://llm.internal.example/v1")) == (
        None,
        None,
    )
    assert billing.billing_portals_for(
        _provider(3, "claude-subscription", "anthropic", auth_mode="anthropic_subscription")
    ) == (None, None)
    assert billing.billing_portals_for(_provider(4, "unknown", "custom")) == (None, None)
    assert billing.billing_portals_for(
        _provider(5, "claude-proxy", "anthropic", api_base="https://proxy.example/v1")
    ) == (None, None)


def test_openrouter_snapshot_maps_period_usage_and_limit():
    provider = _provider(1, "openrouter", "openrouter")
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
    provider = _provider(2, "deepseek", "deepseek")
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


async def test_local_usage_aggregates_all_periods_and_keeps_missing_providers_zero(monkeypatch):
    providers = [_provider(1, "openai-prod", "openai"), _provider(2, "claude-prod", "anthropic")]
    rows = [
        (
            "openai-prod",
            8,
            800,
            Decimal("8.0"),
            1,
            100,
            Decimal("1.0"),
            3,
            300,
            Decimal("3.0"),
            5,
            500,
            Decimal("5.0"),
        )
    ]

    class Result:
        def __init__(self, values):
            self.values = values

        def scalars(self):
            return self

        def all(self):
            return self.values

    class Session:
        calls = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, _statement):
            self.calls += 1
            return Result(providers if self.calls == 1 else rows)

    monkeypatch.setattr(billing, "is_db_available", lambda: True)
    monkeypatch.setattr(billing, "get_session_factory", lambda: lambda: Session())

    loaded, usage_by_name = await billing._load_providers_and_usage()

    assert loaded == providers
    assert usage_by_name["openai-prod"] == {
        "currency": "USD",
        "requests": {"total": "8", "daily": "1", "weekly": "3", "monthly": "5"},
        "tokens": {"total": "800", "daily": "100", "weekly": "300", "monthly": "500"},
        "raw_cost": {"total": "8.0", "daily": "1.0", "weekly": "3.0", "monthly": "5.0"},
    }
    assert "claude-prod" not in usage_by_name


async def test_bulk_status_isolates_live_failure_and_never_calls_unsupported_provider(monkeypatch):
    providers = [
        _provider(1, "gemini-prod", "gemini"),
        _provider(2, "openrouter-prod", "openrouter"),
        _provider(3, "deepseek-prod", "deepseek"),
    ]

    async def fake_load():
        return providers, {"gemini-prod": _usage(monthly_cost="2.5")}

    calls: list[str] = []

    async def fake_request(capability: str, _api_key: str):
        calls.append(capability)
        if capability == "openrouter_key":
            request = billing.httpx.Request("GET", "https://openrouter.ai/api/v1/key")
            response = billing.httpx.Response(503, request=request, text="secret upstream body")
            raise billing.httpx.HTTPStatusError("failed", request=request, response=response)
        return {
            "is_available": True,
            "balance_infos": [
                {
                    "currency": "USD",
                    "total_balance": "9",
                    "granted_balance": "1",
                    "topped_up_balance": "8",
                }
            ],
        }

    monkeypatch.setattr(billing, "_load_providers_and_usage", fake_load)
    monkeypatch.setattr(billing, "resolve_api_key", lambda _provider: "secret-key")
    monkeypatch.setattr(billing, "_request_json", fake_request)

    result = await billing.list_provider_billing()

    assert [item["provider_id"] for item in result] == [1, 2, 3]
    assert result[0]["status"] == "unsupported"
    assert result[0]["reason"] == "provider_console_only"
    assert result[0]["local_usage"]["raw_cost"]["monthly"] == "2.5"
    assert result[0]["billing_url"].startswith("https://aistudio.google.com/")
    assert result[1]["status"] == "unavailable"
    assert result[1]["reason"] == "provider_request_failed"
    assert result[2]["status"] == "available"
    assert result[2]["balances"][0]["total"] == "9"
    assert sorted(calls) == ["deepseek_balance", "openrouter_key"]
    assert "secret" not in str(result)
