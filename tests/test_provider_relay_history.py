"""Settled native allocation history from actual owned HTTP dispatch, not DTO echo."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
import test_chat_api_model_routing
from test_provider_accounts import _pool
from test_provider_transport import connect_runtime, http_fixture

from lumen.services import credit, provider_client
from lumen.services.provider_errors import InternalServerError
from lumen.services.provider_metadata import UsageCost
from lumen.services.providers import accounts, repository, routing
from lumen.services.providers.errors import ChatStorageUnavailable, ProviderSubscriptionError
from lumen.services.providers.relay_history import current_receipts, receipt_owner, safe_endpoint

provider_db = test_chat_api_model_routing.provider_db


def path(account_id, credential_id, provider_id=1):
    return f"/v1/admin/providers/{provider_id}/accounts/{account_id}/credentials/{credential_id}/relays"


async def settle(route, event):
    cost = UsageCost(
        raw_cost=Decimal("0.001"),
        input_cost=Decimal("0.001"),
        output_cost=Decimal("0"),
        pricing_status="priced",
        pricing_snapshot={},
    )
    async with routing._require_db()() as session, session.begin():
        await credit.apply_usage_in_transaction(
            session,
            event_id=event,
            user_id="private-customer",
            project_id="private-project",
            model_name="account-model",
            provider="openai",
            prompt_tokens=3,
            completion_tokens=1,
            usage_cost=cost,
            margin_multiplier=Decimal("1"),
            charge_wallet=False,
            execution_route=route,
        )


async def test_two_keys_actual_dispatch_and_settlement_keep_historical_destinations(
    provider_db, admin_client, monkeypatch
):
    account_id, key_a, key_b = await _pool(provider_db, admin_client, monkeypatch)
    reply = {"choices": [{"message": {"content": "private provider output"}}], "usage": {"prompt_tokens": 3}}
    replies = [
        {
            "body": reply,
            "headers": {
                "X-Lumen-Upstream-Endpoint": f"https://user:URLSECRET@relay-{key}.example/v1/chat/completions?token=QUERYSECRET#FRAGMENTSECRET",
                "X-Lumen-Upstream-Status": "200",
            },
        }
        for key in (key_a, key_b)
    ]
    async with http_fixture(*replies) as fixture:
        await connect_runtime(monkeypatch, fixture)

        @receipt_owner
        async def send_and_settle(key):
            await repository.update_provider(1, {"selection_policy": "explicit", "selected_credential_id": key})
            route = await routing.resolve_model("account-model")
            before = deepcopy(route)
            await provider_client.acompletion(
                route["model_name"],
                [{"role": "user", "content": "private prompt"}],
                custom_llm_provider="openai",
                provider_auth=route["provider_auth"],
            )
            assert route == before  # AuthRef and capability data are immutable.
            await settle(route, f"relay-key-{key}")

        await send_and_settle(key_a)
        await send_and_settle(key_b)
        assert [request["body"]["api_key"] for request in fixture.requests] == [
            "upstream-secret-A",
            "upstream-secret-B",
        ]
    await repository.update_provider(1, {"api_base": "https://changed-today.example/v1"})
    await accounts.update_credential(1, account_id, key_a, {"api_key": "new-generation-not-historical"})
    for key in (key_a, key_b):
        response = await admin_client.get(path(account_id, key))
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        page = response.json()
        assert page["boundary"] == "settled_usage" and page["credential_id"] == key
        assert len(page["items"]) == 1
        row = page["items"][0]
        assert row["generation"] == 0 and row["status"] == "settled"
        assert len(row["dispatches"]) == 1
        receipt = row["dispatches"][0]
        assert receipt["endpoint"] == f"https://relay-{key}.example/v1/chat/completions"
        assert receipt["operation"] == "chat" and receipt["status_code"] == 200
        assert set(receipt) == {"attempt_id", "endpoint", "model", "operation", "status", "status_code", "recorded_at"}
        for private in (
            "URLSECRET",
            "QUERYSECRET",
            "FRAGMENTSECRET",
            "upstream-secret",
            "private prompt",
            "private provider output",
            "private-customer",
            "private-project",
            "changed-today",
        ):
            assert private not in response.text
    assert current_receipts() == []


async def test_old_missing_dispatch_unknown_and_bounded_cursor_ownership(provider_db, admin_client, monkeypatch):
    account_id, key_a, key_b = await _pool(provider_db, admin_client, monkeypatch)
    route = await routing.resolve_model("account-model")
    await settle(route, "old-first")
    await settle(route, "old-second")
    first = await admin_client.get(path(account_id, key_a), params={"limit": 1})
    assert first.status_code == 200
    assert first.json()["items"][0]["dispatches"] is None
    cursor = first.json()["next_before_id"]
    assert cursor is not None
    second = await admin_client.get(path(account_id, key_a), params={"limit": 1, "before_id": cursor})
    assert second.json()["items"][0]["ledger_id"] < cursor
    assert second.json()["next_before_id"] is None
    assert (await admin_client.get(path(account_id, key_b))).json()["items"] == []
    for url in (path(9999, key_a), path(account_id, key_a, 9999), path(account_id, 9999)):
        assert (await admin_client.get(url)).status_code == 404
    for params in ({"limit": 0}, {"limit": 51}, {"before_id": -1}):
        assert (await admin_client.get(path(account_id, key_a), params=params)).status_code == 422


async def test_relay_history_member_cannot_read_provider_keys(client):
    assert (await client.get(path(1, 1))).status_code == 403


async def test_relay_history_storage_failure_is_not_an_empty_directory(admin_client, monkeypatch):
    monkeypatch.setattr(accounts, "credential_relays", AsyncMock(side_effect=ChatStorageUnavailable("private error")))
    response = await admin_client.get(path(1, 1))
    assert response.status_code == 503
    assert "private error" not in response.text


async def test_failed_owner_cannot_contaminate_later_settlement_and_rotation_fails_before_send(
    provider_db, admin_client, monkeypatch
):
    account_id, key_a, _ = await _pool(provider_db, admin_client, monkeypatch)
    route = await routing.resolve_model("account-model")
    before = deepcopy(route)

    @receipt_owner
    async def failed():
        await provider_client.acompletion(
            "account-model", [], custom_llm_provider="openai", provider_auth=route["provider_auth"]
        )

    async with http_fixture(
        {
            "status": 500,
            "body": {"error": {"code": "upstream_rejected"}},
            "headers": {
                "X-Lumen-Upstream-Endpoint": "https://rejected.example/v1/chat/completions",
                "X-Lumen-Upstream-Status": "500",
            },
        }
    ) as fixture:
        await connect_runtime(monkeypatch, fixture)
        with pytest.raises(InternalServerError):
            await failed()
        assert len(fixture.requests) == 1
    assert route == before and current_receipts() == []

    @receipt_owner
    async def fresh():
        await settle(route, "new-owner-no-send")

    await fresh()
    assert (await admin_client.get(path(account_id, key_a))).json()["items"][0]["dispatches"] is None
    await accounts.update_credential(1, account_id, key_a, {"api_key": "rotated"})
    async with http_fixture({"body": {}}) as fixture:
        await connect_runtime(monkeypatch, fixture)
        with pytest.raises(ProviderSubscriptionError) as rejected:
            await failed()
        assert rejected.value.code == "subscription_auth_required"
        assert fixture.requests == []


async def test_collector_stream_close_cancellation_and_settlement_retries_are_fenced():
    from lumen.services.providers.relay_history import record_dispatch

    ref = {"provider_id": 1, "account_id": 2, "credential_id": 3, "generation": 4}
    before = deepcopy(ref)

    @receipt_owner
    async def stream():
        try:
            record_dispatch(ref, endpoint="https://host/path", model="m", operation="chat", status_code=200)
            yield current_receipts()
        finally:
            assert len(current_receipts()) == 1

    iterator = stream()
    assert len(await anext(iterator)) == 1
    assert current_receipts() == []
    await iterator.aclose()
    assert current_receipts() == [] and ref == before

    @receipt_owner
    async def cancelled():
        record_dispatch(ref, endpoint="https://host/path", model="m", operation="chat")
        assert current_receipts("event-a") == current_receipts("event-a")
        assert current_receipts("event-b") == []
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await cancelled()
    assert current_receipts() == [] and ref == before


@pytest.mark.parametrize(
    "value,expected",
    [
        ("https://u:p@host:8443/v1?api_key=secret#secret", "https://host:8443/v1"),
        ("wss://host/realtime?key=secret", "wss://host/realtime"),
        ("javascript:secret", None),
        ("https://host:invalid/path", None),
    ],
)
def test_endpoint_projection_scrubs_url_secrets(value, expected):
    assert safe_endpoint(value) == expected


async def test_real_complete_once_owner_persists_receipts_without_test_local_owner(
    provider_db, admin_client, monkeypatch
):
    from lumen.services import completion_api

    account_id, key_a, _ = await _pool(provider_db, admin_client, monkeypatch)
    route = await routing.resolve_model("account-model")
    monkeypatch.setattr(completion_api, "_authorize_completion", AsyncMock())
    monkeypatch.setattr(credit, "_require_db", routing._require_db)
    actual_apply = credit.apply_usage

    async def apply_without_wallet(**kwargs):
        return await actual_apply(**{**kwargs, "charge_wallet": False})

    monkeypatch.setattr(credit, "apply_usage", apply_without_wallet)
    reply = {
        "choices": [{"message": {"content": "answer"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 1},
    }
    async with http_fixture(
        {
            "body": reply,
            "headers": {
                "X-Lumen-Upstream-Endpoint": "https://actual-owner.example/v1/chat/completions",
                "X-Lumen-Upstream-Status": "200",
            },
        }
    ) as fixture:
        await connect_runtime(monkeypatch, fixture)
        result = await completion_api.complete_once(
            resolved=route,
            messages=[{"role": "user", "content": "input"}],
            user_id="private-owner",
            project_id="private-project",
            api_key_id=None,
            max_tokens=32,
            temperature=0,
        )
        assert result["content"] == "answer" and len(fixture.requests) == 1
    page = await admin_client.get(path(account_id, key_a))
    assert page.status_code == 200, page.text
    assert page.json()["items"][0]["dispatches"][0]["endpoint"] == "https://actual-owner.example/v1/chat/completions"
    assert current_receipts() == []


async def test_nested_summary_owner_does_not_claim_parent_main_turn_receipts():
    from lumen.services.providers.relay_history import record_dispatch

    ref = {"provider_id": 1, "account_id": 2, "credential_id": 3, "generation": 4}

    @receipt_owner
    async def summary():
        record_dispatch(
            ref, endpoint="https://summary.example/send", model="same-model", operation="chat", status_code=200
        )
        return current_receipts("summary-event")

    @receipt_owner
    async def run():
        record_dispatch(
            ref, endpoint="https://main.example/send", model="same-model", operation="chat", status_code=200
        )
        nested = await summary()
        assert [row["endpoint"] for row in nested] == ["https://summary.example/send"]
        main = current_receipts("run-event")
        assert [row["endpoint"] for row in main] == ["https://main.example/send"]
        assert current_receipts("run-event") == main
        assert current_receipts("later-event") == []

    await run()
    assert current_receipts() == []
