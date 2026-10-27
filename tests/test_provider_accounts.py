"""Administrator account selection, fresh credential authority and allocated billing."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import test_chat_api_model_routing
from sqlalchemy import select

from lumen import crypto
from lumen.models.chat_db import ChatProviderUsage, ChatUsageLog
from lumen.services import credit
from lumen.services.provider_metadata import UsageCost
from lumen.services.providers import accounts, credentials, repository, routing
from lumen.services.providers.errors import ProviderSubscriptionError

provider_db = test_chat_api_model_routing.provider_db


async def _pool(provider_db, admin_client, monkeypatch):
    add_provider, add_model, _ = provider_db
    add_provider(provider_type="openai")
    add_model("account-model")
    monkeypatch.setattr(credentials, "decrypt_llm_provider_key", crypto.decrypt_llm_provider_key)
    await accounts.update_credential(1, 1, 1, {"is_active": False})
    created = await admin_client.post("/v1/admin/providers/1/accounts", json={"name": "enterprise-account", "quota_group": "organization-A"})
    assert created.status_code == 201
    account_id = created.json()["id"]
    first = await admin_client.post(f"/v1/admin/providers/1/accounts/{account_id}/credentials",
                                    json={"name": "A", "api_key": "upstream-secret-A", "priority": 0})
    second = await admin_client.post(f"/v1/admin/providers/1/accounts/{account_id}/credentials",
                                     json={"name": "B", "api_key": "upstream-secret-B", "priority": 1})
    assert first.status_code == second.status_code == 201
    assert "upstream-secret" not in first.text + second.text + created.text
    return account_id, first.json()["id"], second.json()["id"]


async def test_additional_key_never_reassigns_an_admitted_route_and_revoke_fails_closed(provider_db, admin_client, monkeypatch):
    account_id, key_a, key_b = await _pool(provider_db, admin_client, monkeypatch)
    admitted = await routing.resolve_model("account-model")
    assert admitted["api_key"] is None
    assert admitted["provider_credential_id"] == key_a
    assert (await credentials.resolve_inference_credential(admitted["provider_auth"]))["api_key"] == "upstream-secret-A"
    await accounts.update_credential(1, account_id, key_b, {"priority": 0, "weight": 50})
    await repository.update_provider(1, {"selection_policy": "explicit", "selected_credential_id": key_b})
    assert (await routing.resolve_model("account-model"))["provider_credential_id"] == key_b
    restored = await routing.resolve_model_snapshot(admitted)
    assert restored["provider_credential_id"] == key_a
    assert restored["config_version_hash"] == admitted["config_version_hash"]
    await accounts.update_credential(1, account_id, key_a, {"is_active": False})
    assert await routing.resolve_model_snapshot(admitted) is None
    with pytest.raises(ProviderSubscriptionError):
        await credentials.resolve_inference_credential(admitted["provider_auth"])
    assert (await routing.resolve_model("account-model"))["provider_credential_id"] == key_b


async def test_weighted_policy_boundaries_choose_credentials_without_retry(provider_db, admin_client, monkeypatch):
    account_id, key_a, key_b = await _pool(provider_db, admin_client, monkeypatch)
    await accounts.update_credential(1, account_id, key_a, {"weight": 2})
    await accounts.update_credential(1, account_id, key_b, {"weight": 3})
    await repository.update_provider(1, {"selection_policy": "weighted"})
    for slot, expected in ((0, key_a), (1, key_a), (2, key_b), (4, key_b)):
        monkeypatch.setattr(accounts.secrets, "randbelow", lambda total, value=slot: value if total == 5 else pytest.fail("bad pool weights"))
        assert (await routing.resolve_model("account-model"))["provider_credential_id"] == expected


async def test_operation_and_model_restriction_are_revalidated_on_fixed_reference(provider_db, admin_client, monkeypatch):
    account_id, key_a, _ = await _pool(provider_db, admin_client, monkeypatch)
    await repository.update_provider(1, {"selection_policy": "explicit", "selected_credential_id": key_a})
    admitted = await routing.resolve_model("account-model")
    await accounts.update_account(1, account_id, {"allowed_capabilities": ["search"]})
    assert await routing.resolve_model_snapshot(admitted) is None
    with pytest.raises(ProviderSubscriptionError):
        await credentials.resolve_inference_credential(admitted["provider_auth"])
    assert await routing.resolve_model("account-model") is None
    await accounts.update_account(1, account_id, {"allowed_capabilities": ["chat"], "allowed_model_ids": [10]})
    assert (await routing.resolve_model("account-model"))["provider_credential_id"] == key_a
    assert await routing.resolve_api_model("account-model", required_capabilities=("responses",)) is None
    await accounts.update_account(1, account_id, {"allowed_model_ids": []})
    assert await routing.resolve_model("account-model") is None


async def test_expiration_and_operator_cooldown_do_not_use_another_fixed_key(provider_db, admin_client, monkeypatch):
    account_id, key_a, key_b = await _pool(provider_db, admin_client, monkeypatch)
    admitted = await routing.resolve_model("account-model")
    await accounts.update_credential(1, account_id, key_a, {"cooldown_until": datetime.now(UTC) + timedelta(minutes=5)})
    assert await routing.resolve_model_snapshot(admitted) is None
    assert (await routing.resolve_model("account-model"))["provider_credential_id"] == key_b
    await accounts.update_credential(1, account_id, key_b, {"expires_at": datetime.now(UTC) - timedelta(seconds=1)})
    assert await routing.resolve_model("account-model") is None


async def test_credential_expiry_and_cooldown_keep_utc_instants_across_http_edits(provider_db, admin_client, monkeypatch):
    account_id, key_a, _ = await _pool(provider_db, admin_client, monkeypatch)
    path = f"/v1/admin/providers/1/accounts/{account_id}/credentials/{key_a}"
    changed = await admin_client.patch(path, json={
        "expires_at": "2027-01-01T12:00:00+09:00", "cooldown_until": "2026-12-31T11:30:00+09:00",
    })
    assert changed.status_code == 200, changed.text
    response = await admin_client.get("/v1/admin/providers/1/accounts")
    saved = next(credential for account in response.json() for credential in account["credentials"] if credential["id"] == key_a)
    assert datetime.fromisoformat(saved["expires_at"]) == datetime(2027, 1, 1, 3, tzinfo=UTC)
    assert datetime.fromisoformat(saved["cooldown_until"]) == datetime(2026, 12, 31, 2, 30, tzinfo=UTC)
    repeated = await admin_client.patch(path, json={field: saved[field] for field in ("expires_at", "cooldown_until")})
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["expires_at"] == saved["expires_at"]
    assert repeated.json()["cooldown_until"] == saved["cooldown_until"]


async def test_cross_endpoint_credential_selection_and_private_errors_never_echo_key(provider_db, admin_client, monkeypatch):
    _, key_a, _ = await _pool(provider_db, admin_client, monkeypatch)
    add_provider, _, _ = provider_db
    add_provider(provider_id=2, provider_type="openai")
    denied = await admin_client.patch("/v1/admin/providers/2", json={"selection_policy": "explicit", "selected_credential_id": key_a})
    assert denied.status_code == 400
    invalid = await admin_client.post("/v1/admin/providers/1/accounts/1/credentials",
        json={"name": "bad", "api_key": {"secret": "NEVER-ECHO"}, "generation": 999})
    assert invalid.status_code == 422
    assert invalid.headers["cache-control"] == "no-store"
    assert "NEVER-ECHO" not in invalid.text


async def test_project_member_cannot_read_upstream_accounts(client):
    forbidden = await client.get("/v1/admin/providers/1/accounts")
    assert forbidden.status_code == 403


async def test_account_usage_allocates_advisor_without_a_second_customer_debit(provider_db, admin_client, monkeypatch):
    account_id, key_a, key_b = await _pool(provider_db, admin_client, monkeypatch)
    route = await routing.resolve_model("account-model")
    identity_a = {key: route[key] for key in ("provider_id", "provider_account_id", "provider_credential_id",
        "provider_credential_generation", "provider_backend", "provider_backend_version")}
    identity_b = {**identity_a, "provider_credential_id": key_b}
    components = [
        {"kind": "input_tokens", "unit": "token", "quantity": "100", "cost_usd": "0.001", "metadata": identity_a},
        {"kind": "output_tokens", "unit": "token", "quantity": "20", "cost_usd": "0.002", "metadata": identity_a},
        {"kind": "advisor_input_tokens", "unit": "token", "quantity": "30", "cost_usd": "0.003", "metadata": identity_b},
        {"kind": "advisor_output_tokens", "unit": "token", "quantity": "10", "cost_usd": "0.004", "metadata": identity_b},
    ]
    cost = UsageCost(raw_cost=Decimal("0.010"), input_cost=Decimal("0.004"), output_cost=Decimal("0.006"),
                     pricing_status="priced", pricing_snapshot={})
    async with routing._require_db()() as session, session.begin():
        charged = await credit.apply_usage_in_transaction(session, event_id="allocated-event", user_id="u1", project_id="p1",
            model_name="account-model", provider="openai", prompt_tokens=100, completion_tokens=20, usage_cost=cost,
            margin_multiplier=Decimal("1"), credit_per_usd=Decimal("1"), charge_wallet=False,
            usage_components=components, execution_route=route)
        assert charged == Decimal("0.01000000")
    async with routing._require_db()() as session:
        assert len((await session.execute(select(ChatUsageLog))).scalars().all()) == 1
        allocated = (await session.execute(select(ChatProviderUsage).order_by(ChatProviderUsage.provider_credential_id))).scalars().all()
        assert [(row.provider_credential_id, row.prompt_tokens, row.completion_tokens, row.raw_cost) for row in allocated] == [
            (key_a, 100, 20, Decimal("0.003")), (key_b, 30, 10, Decimal("0.007"))]
    usage = await admin_client.get(f"/v1/admin/providers/1/accounts/{account_id}/usage")
    assert usage.status_code == 200
    assert {row["credential_id"]: Decimal(row["raw_cost"]) for row in usage.json()["credentials"]} == {
        key_a: Decimal("0.003"), key_b: Decimal("0.007")}
