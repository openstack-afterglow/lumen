"""Real MariaDB account cutover, current credential authority and atomic billing.

Provider/OAuth material is explicitly synthetic. No upstream inference is sent.
"""
from __future__ import annotations

import asyncio
import os
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import delete, event, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from lumen import crypto
from lumen.models.chat_db import (
    ChatProviderUsage,
    ChatUsageLog,
    LlmModel,
    LlmProvider,
    LlmProviderAuthAttempt,
    LlmProviderCredential,
    UserWallet,
)
from lumen.services import credit
from lumen.services.provider_metadata import UsageCost
from lumen.services.providers import accounts, credentials, repository, routing, subscriptions
from lumen.services.providers.errors import (
    ProviderAuthAttemptConflict,
    ProviderConfigurationChangedError,
    ProviderNotFoundError,
    ProviderSubscriptionError,
    ProviderValidationError,
)

pytestmark = pytest.mark.integration


@pytest.fixture(params=["ON", "OFF"])
async def account_db(request, monkeypatch):
    engine = create_async_engine(os.environ["DATABASE_URL"], pool_size=4, max_overflow=0)

    def configure_snapshot(connection, record):
        cursor = connection.cursor()
        cursor.execute(f"SET SESSION innodb_snapshot_isolation = {request.param}")
        cursor.close()

    event.listen(engine.sync_engine, "connect", configure_snapshot)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    for module in (repository, routing, subscriptions, credit):
        monkeypatch.setattr(module, "_require_db", lambda: factory)
    db = SimpleNamespace(engine=engine, factory=factory, providers=[], user="account-it-" + uuid4().hex)
    async with factory() as session:
        assert bool((await session.execute(text("SELECT @@SESSION.innodb_snapshot_isolation"))).scalar_one()) == (
            request.param == "ON"
        )
    try:
        yield db
    finally:
        async with factory() as session, session.begin():
            await session.execute(delete(ChatProviderUsage).where(ChatProviderUsage.provider_id.in_(db.providers)))
            await session.execute(delete(ChatUsageLog).where(ChatUsageLog.user_id == db.user))
            await session.execute(delete(UserWallet).where(UserWallet.user_id == db.user))
            await session.execute(delete(LlmProvider).where(LlmProvider.id.in_(db.providers)))
        await engine.dispose()


async def _endpoint(db, *, mode="api_key"):
    family = {"api_key": "openai", "chatgpt_device": "chatgpt", "anthropic_subscription": "anthropic"}[mode]
    provider = await repository.create_provider(
        name="account-it-" + uuid4().hex, provider_type=family, auth_mode=mode,
        api_key="synthetic-api-key-A" if mode == "api_key" else None,
        billing_admin_key="synthetic-billing-admin" if mode == "api_key" else None,
    )
    db.providers.append(provider["id"])
    account = (await accounts.list_accounts(provider["id"]))[0]
    credential = account["credentials"][0]
    name = {"api_key": "account-it-model", "chatgpt_device": "chatgpt/account-it-model",
            "anthropic_subscription": "anthropic-subscription/account-it-model"}[mode]
    async with db.factory() as session, session.begin():
        model = LlmModel(provider_id=provider["id"], model_name=name, input_price=Decimal("0.000001"),
                         output_price=Decimal("0.000002"), price_source="manual", capabilities={"tool_call": True})
        session.add(model)
        await session.flush()
        model_id = model.id
    return provider["id"], account["id"], credential["id"], model_id, name




async def test_pool_edits_preserve_selected_identity_but_current_operation_denial_blocks_it(account_db, monkeypatch):
    db = account_db
    provider_id, account_id, key_a, model_id, name = await _endpoint(db)
    admitted = await routing.resolve_model_by_id(model_id)
    assert admitted["api_key"] is None
    assert admitted["provider_auth"]["required_capabilities"] == ["chat"]
    assert "synthetic-" not in str(admitted)
    other = await accounts.create_account(provider_id, {"name": "second-account", "priority": 0, "weight": 99})
    key_b = await accounts.create_credential(provider_id, other["id"], {"name": "B", "api_key": "synthetic-api-key-B", "weight": 99})
    await accounts.update_account(provider_id, account_id, {"priority": 100, "weight": 1})
    await repository.update_provider(provider_id, {"selection_policy": "weighted"})
    monkeypatch.setattr(accounts.secrets, "randbelow", lambda total: 0)
    assert (await routing.resolve_model_by_id(model_id))["provider_credential_id"] == key_b["id"]
    restored = await routing.resolve_model_snapshot(admitted)
    assert restored["provider_auth"] == admitted["provider_auth"]
    assert restored["config_version_hash"] == admitted["config_version_hash"]
    assert (await credentials.resolve_inference_credential(restored["provider_auth"]))["api_key"] == "synthetic-api-key-A"
    await accounts.delete_account(provider_id, other["id"])
    assert (await routing.resolve_model_snapshot(admitted))["provider_credential_id"] == key_a

    await accounts.update_account(provider_id, account_id, {"allowed_capabilities": ["chat"], "allowed_model_ids": [model_id]})
    assert await routing.resolve_api_model(name, provider_id=provider_id, required_capabilities=("responses",)) is None
    assert await routing.resolve_model_snapshot(admitted) is not None
    await accounts.update_account(provider_id, account_id, {"allowed_capabilities": []})
    assert await routing.resolve_model_by_id(model_id) is None
    assert await routing.resolve_model_snapshot(admitted) is None
    with pytest.raises(ProviderSubscriptionError):
        await credentials.resolve_inference_credential(admitted["provider_auth"])
    await accounts.update_account(provider_id, account_id, {"allowed_capabilities": ["chat"], "allowed_model_ids": []})
    assert await routing.resolve_model_by_id(model_id) is None
    await accounts.update_account(provider_id, account_id, {"allowed_model_ids": [model_id]})
    await accounts.update_credential(provider_id, account_id, key_a, {"allowed_capabilities": []})
    assert await routing.resolve_model_by_id(model_id) is None


async def test_rotation_environment_and_delayed_rejection_never_use_billing_admin_or_old_generation(account_db, monkeypatch):
    db = account_db
    provider_id, account_id, key_a, model_id, name = await _endpoint(db)
    admitted = await routing.resolve_model_by_id(model_id)
    material = await credentials.resolve_inference_credential(admitted["provider_auth"])
    rotated = await accounts.update_credential(provider_id, account_id, key_a, {"api_key": "synthetic-api-key-rotated"})
    assert rotated["generation"] == admitted["provider_auth"]["generation"] + 1
    await credentials.mark_inference_credential_rejected(admitted["provider_auth"], material["_fingerprint"])
    assert await routing.resolve_model_snapshot(admitted) is None
    with pytest.raises(ProviderSubscriptionError):
        await credentials.resolve_inference_credential(admitted["provider_auth"])
    current = await routing.resolve_model_by_id(model_id)
    assert (await credentials.resolve_inference_credential(current["provider_auth"]))["api_key"] == "synthetic-api-key-rotated"
    monkeypatch.setenv("LUMEN_ACCEPTANCE_ROTATION_KEY", "synthetic-environment-1")
    await accounts.update_credential(provider_id, account_id, key_a, {"api_key": None, "api_key_env": "LUMEN_ACCEPTANCE_ROTATION_KEY"})
    env_route = await routing.resolve_model_by_id(model_id)
    old_env = await credentials.resolve_inference_credential(env_route["provider_auth"])
    monkeypatch.setenv("LUMEN_ACCEPTANCE_ROTATION_KEY", "synthetic-environment-2")
    await credentials.mark_inference_credential_rejected(env_route["provider_auth"], old_env["_fingerprint"])
    assert (await credentials.resolve_inference_credential(env_route["provider_auth"]))["api_key"] == "synthetic-environment-2"
    monkeypatch.delenv("LUMEN_ACCEPTANCE_ROTATION_KEY")
    with pytest.raises(ProviderSubscriptionError):
        await credentials.resolve_inference_credential(env_route["provider_auth"])
    assert await routing.resolve_model_by_id(model_id) is None
    async with db.factory() as session:
        provider = await session.get(LlmProvider, provider_id)
        stored = await session.get(LlmProviderCredential, key_a)
        assert crypto.decrypt_llm_provider_billing_admin_key(provider.encrypted_billing_admin_key) == "synthetic-billing-admin"
        assert stored.encrypted_api_key is None and stored.encrypted_subscription_tokens is None


async def test_revoke_committed_during_lock_wait_is_seen_before_credential_resolution(account_db):
    db = account_db
    provider_id, account_id, key_a, model_id, name = await _endpoint(db)
    route = await routing.resolve_model_by_id(model_id)
    lock_timed_out = asyncio.Event()

    def bound_lock_wait(connection, cursor, statement, parameters, context, executemany):
        if "FROM llm_providers" in statement and "FOR UPDATE" in statement:
            cursor.execute("SET SESSION innodb_lock_wait_timeout = 1")

    def observe_lock_timeout(context):
        if context.original_exception.args[0] == 1205:
            lock_timed_out.set()

    task = None
    try:
        async with db.factory() as writer:
            async with writer.begin():
                _, _, credential = await accounts.lock_credential(writer, provider_id, account_id=account_id, credential_id=key_a)
                credential.is_active = False
                credential.generation += 1
                await writer.flush()
                event.listen(db.engine.sync_engine, "before_cursor_execute", bound_lock_wait)
                event.listen(db.engine.sync_engine, "handle_error", observe_lock_timeout)
                task = asyncio.create_task(credentials.resolve_inference_credential(route["provider_auth"]))
                # Observe MariaDB's real1205, not just a scheduled Python task.
                # The production pure-DB retry must then reread the committed revoke.
                await asyncio.wait_for(lock_timed_out.wait(), timeout=10)
                assert not task.done()
            with pytest.raises(ProviderSubscriptionError):
                await asyncio.wait_for(task, timeout=10)
    finally:
        if event.contains(db.engine.sync_engine, "before_cursor_execute", bound_lock_wait):
            event.remove(db.engine.sync_engine, "before_cursor_execute", bound_lock_wait)
        if event.contains(db.engine.sync_engine, "handle_error", observe_lock_timeout):
            event.remove(db.engine.sync_engine, "handle_error", observe_lock_timeout)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert await routing.resolve_model_snapshot(route) is None


async def test_device_attempts_are_caller_bound_and_credential_scoped_in_one_endpoint(account_db, monkeypatch):
    db = account_db
    provider_id, account_id, key_a, model_id, name = await _endpoint(db, mode="chatgpt_device")
    key_b = await accounts.create_credential(provider_id, account_id, {"name": "second-device"})
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"device_auth_id": "synthetic-device-" + str(len(requests)),
                                        "user_code": "SYNTHETIC-" + str(len(requests)), "interval": 5, "expires_in": 600})

    monkeypatch.setattr(subscriptions, "_http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(respond)))
    first = await subscriptions.begin_device_auth(provider_id, user_id="admin-A", project_id="project-A", account_id=account_id, credential_id=key_a)
    replay = await subscriptions.begin_device_auth(provider_id, user_id="admin-A", project_id="project-A", account_id=account_id, credential_id=key_a)
    assert replay["attempt_id"] == first["attempt_id"] and replay["_created"] is False
    with pytest.raises(ProviderAuthAttemptConflict):
        await subscriptions.begin_device_auth(provider_id, user_id="admin-B", project_id="project-A", account_id=account_id, credential_id=key_a)
    second = await subscriptions.begin_device_auth(provider_id, user_id="admin-B", project_id="project-B", account_id=account_id, credential_id=key_b["id"])
    assert second["attempt_id"] != first["attempt_id"] and len(requests) == 2
    with pytest.raises(ProviderNotFoundError):
        await subscriptions.poll_device_auth(provider_id, first["attempt_id"], user_id="admin-A", project_id="project-A", account_id=account_id, credential_id=key_b["id"])
    with pytest.raises(ProviderNotFoundError):
        await subscriptions.cancel_device_auth(provider_id, first["attempt_id"], user_id="admin-A", project_id="foreign-project", account_id=account_id, credential_id=key_a)
    await subscriptions.cancel_device_auth(provider_id, first["attempt_id"], user_id="admin-A", project_id="project-A", account_id=account_id, credential_id=key_a)
    async with db.factory() as session:
        a = await session.get(LlmProviderAuthAttempt, key_a)
        b = await session.get(LlmProviderAuthAttempt, key_b["id"])
        assert a.provider_id == b.provider_id == provider_id
        assert a.status == "cancelled" and a.encrypted_payload is None
        assert b.status == "pending" and "synthetic-device" not in b.encrypted_payload
        assert subscriptions._decrypt(b.encrypted_payload)["device_auth_id"] == "synthetic-device-2"
    await accounts.update_credential(provider_id, account_id, key_b["id"], {"is_active": False})
    with pytest.raises(ProviderConfigurationChangedError):
        await subscriptions.poll_device_auth(provider_id, second["attempt_id"], user_id="admin-B", project_id="project-B", account_id=account_id, credential_id=key_b["id"])
    async with db.factory() as session:
        fenced = await session.get(LlmProviderAuthAttempt, key_b["id"])
        assert fenced.status == "error" and fenced.encrypted_payload is None
    assert len(requests) == 2


async def test_subscription_rotation_and_disconnect_fence_exact_grant_and_stale_rejection(account_db):
    db = account_db
    provider_id, account_id, key_a, model_id, name = await _endpoint(db, mode="anthropic_subscription")
    with pytest.raises(ProviderValidationError):
        await accounts.update_credential(provider_id, account_id, key_a, {"api_key": "synthetic-forbidden"})
    await subscriptions.set_subscription_token(provider_id, token="sk-ant-oat-synthetic-A", expires_at=None, account_id=account_id, credential_id=key_a)
    route = await routing.resolve_model_by_id(model_id)
    original = await credentials.resolve_inference_credential(route["provider_auth"])
    await subscriptions.set_subscription_token(provider_id, token="sk-ant-oat-synthetic-B", expires_at=None, account_id=account_id, credential_id=key_a)
    await credentials.mark_inference_credential_rejected(route["provider_auth"], original["_fingerprint"])
    with pytest.raises(ProviderSubscriptionError):
        await credentials.resolve_inference_credential(route["provider_auth"])
    current = await routing.resolve_model_by_id(model_id)
    assert (await credentials.resolve_inference_credential(current["provider_auth"]))["access_token"] == "sk-ant-oat-synthetic-B"
    await subscriptions.disconnect_subscription(provider_id, account_id=account_id, credential_id=key_a)
    assert await routing.resolve_model_snapshot(current) is None
    with pytest.raises(ProviderSubscriptionError):
        await credentials.resolve_inference_credential(current["provider_auth"])
    async with db.factory() as session:
        stored = await session.get(LlmProviderCredential, key_a)
        assert stored.encrypted_subscription_tokens is None and stored.encrypted_api_key is None
        assert stored.subscription_status == "disconnected"


async def test_account_allocations_wallet_and_usage_commit_atomically_with_one_debit(account_db, admin_client):
    db = account_db
    provider_id, account_id, key_a, model_id, name = await _endpoint(db)
    route = await routing.resolve_model_by_id(model_id)
    second = await accounts.create_account(provider_id, {"name": "advisor-account"})
    key_b = await accounts.create_credential(provider_id, second["id"], {"name": "advisor", "api_key": "synthetic-advisor-key"})
    identity_a = {key: route[key] for key in ("provider_id", "provider_account_id", "provider_credential_id",
                                             "provider_credential_generation", "provider_backend", "provider_backend_version")}
    identity_b = {**identity_a, "provider_account_id": second["id"], "provider_credential_id": key_b["id"], "provider_credential_generation": key_b["generation"]}
    components = [
        {"kind": "input_tokens", "unit": "token", "quantity": "100", "cost_usd": "0.001", "metadata": identity_a},
        {"kind": "output_tokens", "unit": "token", "quantity": "20", "cost_usd": "0.002", "metadata": identity_a},
        {"kind": "advisor_input_tokens", "unit": "token", "quantity": "30", "cost_usd": "0.003", "metadata": identity_b},
        {"kind": "advisor_output_tokens", "unit": "token", "quantity": "10", "cost_usd": "0.004", "metadata": identity_b},
    ]
    cost = UsageCost(raw_cost=Decimal("0.010"), input_cost=Decimal("0.004"), output_cost=Decimal("0.006"), pricing_status="priced", pricing_snapshot={})
    event_id = "account-event-" + uuid4().hex
    kwargs = dict(event_id=event_id, user_id=db.user, project_id="account-it-project", model_name=name,
                  provider="openai", prompt_tokens=100, completion_tokens=20, usage_cost=cost,
                  margin_multiplier=Decimal("2"), credit_per_usd=Decimal("100"), execution_route=route)

    class RollbackAfterBilling(Exception):
        pass

    with pytest.raises(RollbackAfterBilling):
        async with db.factory() as session, session.begin():
            assert await credit.apply_usage_in_transaction(session, **kwargs, usage_components=components) == Decimal("2")
            raise RollbackAfterBilling
    async with db.factory() as session:
        assert (await session.execute(select(ChatUsageLog).where(ChatUsageLog.event_id == event_id))).scalar_one_or_none() is None
        assert (await session.execute(select(ChatProviderUsage).where(ChatProviderUsage.event_id == event_id))).all() == []
        assert await session.get(UserWallet, db.user) is None

    async with db.factory() as session, session.begin():
        assert await credit.apply_usage_in_transaction(session, **kwargs, usage_components=components) == Decimal("2")
    # Public replay must return the committed customer charge, not add a second
    # wallet delta or replace the already committed multi-account allocations.
    assert await credit.apply_usage(**kwargs) == Decimal("2")
    async with db.factory() as session:
        usage = (await session.execute(select(ChatUsageLog).where(ChatUsageLog.event_id == event_id))).scalar_one()
        assert usage.credited_cost == Decimal("2") and usage.raw_cost == Decimal("0.010")
        assert usage.provider_account_id == account_id and usage.provider_credential_id == key_a
        allocated = (await session.execute(select(ChatProviderUsage).where(ChatProviderUsage.event_id == event_id).order_by(ChatProviderUsage.provider_account_id))).scalars().all()
        assert [(a.provider_account_id, a.provider_credential_id, a.prompt_tokens, a.completion_tokens, a.raw_cost) for a in allocated] == [
            (account_id, key_a, 100, 20, Decimal("0.003")),
            (second["id"], key_b["id"], 30, 10, Decimal("0.007")),
        ]
        assert (await session.get(UserWallet, db.user)).used_quota_this_month == Decimal("2")
    for aid, expected in ((account_id, Decimal("0.003")), (second["id"], Decimal("0.007"))):
        response = await admin_client.get(f"/v1/admin/providers/{provider_id}/accounts/{aid}/usage")
        assert response.status_code == 200
        assert len(response.json()["credentials"]) == 1
        assert Decimal(response.json()["credentials"][0]["raw_cost"]) == expected
        assert response.json()["credentials"][0]["requests"] == 1

    # The DB's allocation CHECK must roll back the associated usage too.
    bad_event = "account-invalid-" + uuid4().hex
    with pytest.raises(DBAPIError):
        async with db.factory() as session, session.begin():
            await credit.apply_usage_in_transaction(session, **{**kwargs, "event_id": bad_event})
            session.add(ChatProviderUsage(event_id=bad_event, **{**identity_b, "raw_cost": Decimal("-1")}))
            await session.flush()
    async with db.factory() as session:
        assert (await session.execute(select(ChatUsageLog).where(ChatUsageLog.event_id == bad_event))).scalar_one_or_none() is None
        assert (await session.execute(select(ChatProviderUsage).where(ChatProviderUsage.event_id == bad_event))).all() == []
        assert (await session.get(UserWallet, db.user)).used_quota_this_month == Decimal("2")

    # Two actual first writers race on a fresh event's unique ledger key.
    concurrent_event = "account-concurrent-" + uuid4().hex
    assert await asyncio.gather(*(
        credit.apply_usage(**{**kwargs, "event_id": concurrent_event}) for _ in range(2)
    )) == [Decimal("2"), Decimal("2")]
    async with db.factory() as session:
        assert len((await session.execute(select(ChatUsageLog).where(ChatUsageLog.event_id == concurrent_event))).scalars().all()) == 1
        allocation = (await session.execute(select(ChatProviderUsage).where(ChatProviderUsage.event_id == concurrent_event))).scalar_one()
        assert allocation.provider_credential_id == key_a and allocation.raw_cost == Decimal("0.010")
        assert (await session.get(UserWallet, db.user)).used_quota_this_month == Decimal("4")
