"""Real MariaDB evidence for prepaid holds, settlement, and global lock ordering."""
import asyncio
import os
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from sqlalchemy import delete, select, text

from lumen.api import domain_pools
from lumen.auth import get_principal, require_admin
from lumen.crypto import encrypt_chat_content
from lumen.db import close_db, get_session_factory, init_db
from lumen.models.billing import DomainCreditLedger, DomainCreditPool, Payer
from lumen.models.chat_db import ChatApiKey, ChatUsageLog, UserWallet
from lumen.models.chat_infrastructure import ChatAgentReservation, ChatProjectAgentQuota
from lumen.models.chat_runs import ChatModelCallReservation, ChatRun, ChatRunSegment
from lumen.models.stateless_reservations import StatelessCallReservation
from lumen.scripts.migrate import migrate
from lumen.services import credit, domain_credit, inference_authority, stateless_credit
from lumen.services.chat_admission import _run_snapshots
from lumen.services.durable_runs import budgets, execution
from lumen.services.durable_runs.errors import DurableRunInputError
from lumen.services.provider_metadata import UsageCost
from lumen.services.usage import user_usage_summary

pytestmark = pytest.mark.integration


async def test_domain_billing_migration_rechecks_without_replaying():
    # The immutable 028 preflight still checks the exact usage-table shape.
    # It must accept this verified 037 delta on both dry-run and API bootstrap.
    database_url = os.environ["DATABASE_URL"]
    assert await migrate(database_url, apply=False, repair_schema_drift=False) == ([], 0)
    assert await migrate(database_url, apply=True, repair_schema_drift=False) == ([], 0)


@pytest.fixture
async def pool_scope(monkeypatch):
    monkeypatch.setattr(inference_authority, "authorize_api_key_in_transaction", AsyncMock())
    init_db(os.environ["DATABASE_URL"], pool_size=8, max_overflow=0)
    factory = get_session_factory()
    scope = SimpleNamespace(id=uuid4().hex, factory=factory)
    scope.payer = Payer("domain", None, scope.id)
    scope.users = (f"u1-{scope.id}", f"u2-{scope.id}")
    scope.project = f"p-{scope.id}"
    try:
        yield scope
    finally:
        async with factory() as session, session.begin():
            run_ids = select(ChatRun.id).where(ChatRun.payer_domain_id == scope.id)
            await session.execute(delete(ChatModelCallReservation).where(ChatModelCallReservation.run_id.in_(run_ids)))
            await session.execute(delete(ChatAgentReservation).where(ChatAgentReservation.payer_domain_id == scope.id))
            await session.execute(delete(ChatRun).where(ChatRun.payer_domain_id == scope.id, ChatRun.parent_run_id.is_not(None)))
            await session.execute(delete(ChatRun).where(ChatRun.payer_domain_id == scope.id))
            await session.execute(delete(ChatProjectAgentQuota).where(ChatProjectAgentQuota.project_id == scope.project))
            await session.execute(delete(ChatUsageLog).where(ChatUsageLog.payer_domain_id == scope.id))
            await session.execute(delete(StatelessCallReservation).where(StatelessCallReservation.payer_domain_id == scope.id))
            await session.execute(delete(DomainCreditLedger).where(DomainCreditLedger.domain_id == scope.id))
            await session.execute(delete(DomainCreditPool).where(DomainCreditPool.domain_id == scope.id))
            await session.execute(delete(StatelessCallReservation).where(StatelessCallReservation.user_id.in_(scope.users)))
            await session.execute(delete(ChatUsageLog).where(ChatUsageLog.user_id.in_(scope.users)))
            await session.execute(delete(ChatApiKey).where(ChatApiKey.owner_user_id.in_(scope.users)))
            await session.execute(delete(UserWallet).where(UserWallet.user_id.in_(scope.users)))
        await close_db()


async def _fund(scope, amount, *, manager=None, ceiling=None):
    await domain_pools.top_up_domain_pool(scope.id, domain_pools.TopUp(amount_credits=Decimal(amount)),
                                         str(uuid4()), {"user_id": "billing-admin"})
    async with scope.factory() as session, session.begin():
        pool = await domain_credit.lock_pool(session, scope.id)
        pool.manager_monthly_limit_credits = None if manager is None else Decimal(manager)
        pool.admin_ceiling_credits = None if ceiling is None else Decimal(ceiling)


async def _hold(scope, actor, amount, *, key_id=None):
    event_id = str(uuid4())
    await stateless_credit.reserve(event_id=event_id, user_id=actor,
                                   project_id=scope.project if key_id is None else actor,
                                   api_key_id=key_id, bound=Decimal(amount), payer=scope.payer)
    return event_id


async def _run(scope, *, parent=None, snapshots=None):
    capability, pricing = snapshots if snapshots is not None else ({}, {})
    run_id = str(uuid4())
    async with scope.factory() as session, session.begin():
        session.add(ChatRun(
            id=run_id, run_scope="temp" if parent is None else "child", run_kind="completion",
            user_id=scope.users[0], project_id=scope.project, payer_domain_id=scope.id,
            model_name="synthetic-model", capability_snapshot=capability, pricing_snapshot=pricing,
            request_payload=encrypt_chat_content("{}"), client_request_id=str(uuid4()),
            request_fingerprint=run_id, fingerprint_version=1, execution_protocol_version=1,
            status="running", lease_owner="billing-worker", lease_expires_at=execution._now() + timedelta(minutes=1),
            parent_run_id=parent, root_run_id=parent, depth=0 if parent is None else 1,
            credit_ceiling=Decimal(10) if parent is not None else None,
        ))
    return run_id


def _hooks(run_id):
    return execution._DurableExecutionHooks(run_id=run_id, owner="billing-worker", credit_bounded=True)


async def _durable_hold(run_id, amount="6"):
    await _hooks(run_id)._start(segment_id="provider:0:0", ordinal=0, endpoint="chat_completions",
                               turn_ordinal=0, credit_bound=lambda _: Decimal(amount))


async def test_ordinary_chat_frozen_snapshot_reserves_and_charges_once(pool_scope, monkeypatch):
    scope = pool_scope
    monkeypatch.setattr(credit.get_settings(), "chat_credit_per_usd", Decimal(1))
    await _fund(scope, "10")
    snapshots = _run_snapshots({
        "provider_id": 1, "model_id": 1, "provider_name": "configured-test-provider",
        "provider_type": "openai", "model_name": "synthetic-model", "config_version_hash": "f" * 64,
        "input_price_per_token": Decimal("0.001"), "output_price_per_token": Decimal("0.002"),
        "capabilities": {"context_limit": 1024, "output_budget_enforced": True}, "price_source": "manual",
    }, {})
    run_id = await _run(scope, snapshots=snapshots)
    hooks = _hooks(run_id)
    await hooks.provider_started(round_index=0, attempt=0, max_tokens=20,
                                 messages=[{"role": "user", "content": "synthetic"}])
    async with scope.factory() as session:
        pool = await session.get(DomainCreditPool, scope.id)
        assert Decimal(1) < pool.reserved_credits < Decimal(2)
        assert pool.balance_credits == 10
    for _ in range(2):
        await hooks.provider_completed(round_index=0, attempt=0,
                                       usage={"prompt_tokens": 2, "completion_tokens": 3}, result_payload={})
    async with scope.factory() as session:
        pool = await session.get(DomainCreditPool, scope.id)
        assert (pool.balance_credits, pool.reserved_credits, pool.monthly_used_credits) == (
            Decimal("9.992"), 0, Decimal("0.008"),
        )
        assert (await session.get(UserWallet, scope.users[0])).used_quota_this_month == 0


@pytest.mark.parametrize("first", ["dashboard", "personal", "race"])
async def test_dashboard_and_personal_key_share_actor_monthly_budget(pool_scope, first):
    scope = pool_scope
    user_id = scope.users[0]
    await _fund(scope, "300")
    async with scope.factory() as session, session.begin():
        wallet = await credit.lock_wallet(session, user_id, scope.project)
        wallet.max_quota_monthly = Decimal(100)
        key = ChatApiKey(owner_user_id=user_id, owner_project_id=scope.project, name="personal billing test",
                         key_prefix="synthetic", key_hash=uuid4().hex * 2, scopes=["compat:completions:write"])
        session.add(key)
        await session.flush()
        key_id = key.id
    personal = Payer("user", user_id, None)
    run_id = await _run(scope)

    async def dashboard_hold():
        await _durable_hold(run_id, "90")

    async def key_hold():
        event_id = str(uuid4())
        await stateless_credit.reserve(event_id=event_id, user_id=user_id, project_id=f"lumen:key:{key_id}",
                                       api_key_id=key_id, bound=Decimal(90), payer=personal)
        return event_id

    if first == "race":
        results = await asyncio.gather(dashboard_hold(), key_hold(), return_exceptions=True)
        [error] = [result for result in results if isinstance(result, Exception)]
        assert isinstance(error, credit.QuotaExceeded) and "월 사용 한도" in str(error)
        async with scope.factory() as session:
            assert await domain_credit.pending(session, None, actor_user_id=user_id) == 90
        return

    if first == "dashboard":
        await dashboard_hold()
        try_next = key_hold
    else:
        key_event = await key_hold()
        try_next = dashboard_hold
    with pytest.raises(credit.QuotaExceeded, match="월 사용 한도"):
        await try_next()

    if first == "dashboard":
        async with scope.factory() as session, session.begin():
            quota, root, run = await _hooks(run_id)._credit_lineage(session)
            segment = await session.get(ChatRunSegment, (run_id, "provider:0:0"))
            await budgets.settle_call_credit(session, run=run, root=root, quota=quota, segment=segment, actual=Decimal(90))
    else:
        charge = UsageCost(Decimal(90), Decimal(90), Decimal(0), "priced", {})
        await stateless_credit.settle(
            **{**_usage(scope, key_event), "project_id": f"lumen:key:{key_id}", "api_key_id": key_id,
               "payer": personal, "usage_cost": charge, "source": "api"},
        )
    with pytest.raises(credit.QuotaExceeded, match="월 사용 한도"):
        await try_next()
    async with scope.factory() as session:
        wallet = await session.get(UserWallet, user_id)
        assert wallet.used_quota_this_month == (0 if first == "dashboard" else 90)


@pytest.mark.parametrize(("amount", "ceiling"), [("10", None), ("100", "10")])
async def test_two_domain_actors_race_one_prepaid_limit(pool_scope, amount, ceiling):
    scope = pool_scope
    with pytest.raises(credit.DomainCreditInsufficient, match="domain_credit_insufficient"):
        await credit.precheck(scope.users[0], scope.project, payer=scope.payer)
    await _fund(scope, amount, ceiling=ceiling)
    results = await asyncio.gather(*(_hold(scope, actor, "6") for actor in scope.users), return_exceptions=True)
    assert sum(isinstance(result, credit.DomainCreditInsufficient) for result in results) == 1
    [event_id] = [result for result in results if isinstance(result, str)]
    await stateless_credit.finish(event_id, uncertain=True, payer=scope.payer)
    with pytest.raises(credit.DomainCreditInsufficient):
        await _hold(scope, scope.users[0], "5")
    async with scope.factory() as session:
        pool = await session.get(DomainCreditPool, scope.id)
        assert (pool.balance_credits, pool.reserved_credits, pool.monthly_used_credits) == (Decimal(amount), 6, 0)


async def test_service_shaped_stateless_and_dashboard_provider_hold_race(pool_scope, monkeypatch):
    scope = pool_scope
    await _fund(scope, "10")
    # Credential resolution belongs to migration 038; accounting and hold SQL remain real here.
    monkeypatch.setattr(credit, "_domain_key", AsyncMock(return_value=SimpleNamespace(
        id=901, owner_monthly_credit_limit=None, admin_monthly_credit_limit=None, owner_weekly_credit_limit=None,
    )))
    run_id = await _run(scope)
    results = await asyncio.gather(_hold(scope, "lumen:key:901", "6", key_id=901), _durable_hold(run_id),
                                   return_exceptions=True)
    assert sum(isinstance(result, credit.DomainCreditInsufficient) for result in results) == 1
    assert results[1] is None or isinstance(results[0], str)
    with pytest.raises(credit.DomainCreditInsufficient):
        await _hold(scope, scope.users[1], "5")
    async with scope.factory() as session:
        assert (await session.get(DomainCreditPool, scope.id)).reserved_credits == 6
    # Either kind can coexist when both fit, and both contribute to the combined headroom.
    if results[1] is None:
        key_event = await _hold(scope, "lumen:key:901", "4", key_id=901)
    else:
        key_event = results[0]
        await _durable_hold(run_id, "4")
    async with scope.factory() as session:
        assert await domain_credit.pending(session, scope.id) == 10
        hold = await session.get(StatelessCallReservation, key_event)
        assert hold.user_id is None and hold.project_id is None and hold.api_key_id == 901
    await stateless_credit.settle(**_usage(scope, key_event, actor="lumen:key:901", key_id=901))
    async with scope.factory() as session:
        row = await session.scalar(select(ChatUsageLog).where(ChatUsageLog.event_id == key_event))
        assert (row.user_id, row.project_id, row.api_key_id, row.payer_domain_id) == (None, None, 901, scope.id)


@pytest.mark.parametrize("expired", [False, True])
async def test_slow_keystone_does_not_block_other_actor_billing(pool_scope, monkeypatch, expired):
    scope = pool_scope
    await _fund(scope, "10")
    run_id = await _run(scope)
    entered, release = asyncio.Event(), asyncio.Event()
    clock = [100]
    monkeypatch.setattr(inference_authority, "monotonic", lambda: clock[0])

    async def blocked_authority(*args, **kwargs):
        entered.set()
        await release.wait()

    monkeypatch.setattr(inference_authority, "authorize_api_key_in_transaction", blocked_authority)

    async def start():
        if expired:
            with pytest.raises(DurableRunInputError, match="inference_authority_revoked"):
                await _durable_hold(run_id)
        else:
            await _durable_hold(run_id)

    async with asyncio.TaskGroup() as group:
        group.create_task(start())
        await asyncio.wait_for(entered.wait(), 3)
        try:
            event_id = await asyncio.wait_for(_hold(scope, scope.users[1], "3"), 3)
            assert await asyncio.wait_for(
                stateless_credit.settle(**_usage(scope, event_id, actor=scope.users[1])), 3,
            ) == 3
            assert not release.is_set()
            if expired:
                clock[0] += 6
        finally:
            release.set()
    async with scope.factory() as session:
        pool = await session.get(DomainCreditPool, scope.id)
        assert pool.balance_credits == 7
        assert pool.reserved_credits == (0 if expired else 6)
        segment = await session.get(ChatRunSegment, (run_id, "provider:0:0"))
        if expired:
            assert segment is None
        else:
            assert segment.status == "provider_started"


async def test_canceled_run_commits_no_dispatch_hold(pool_scope):
    scope = pool_scope
    await _fund(scope, "10")
    run_id = await _run(scope)
    async with scope.factory() as session, session.begin():
        (await session.get(ChatRun, run_id)).cancel_requested_at = execution._now()
    with pytest.raises(execution.DurableRunError, match="canceled before provider start"):
        await _durable_hold(run_id)
    async with scope.factory() as session:
        assert (await session.get(DomainCreditPool, scope.id)).reserved_credits == 0
        assert await session.get(ChatRunSegment, (run_id, "provider:0:0")) is None


async def test_domain_lock_precedes_quota_in_real_dashboard_transaction(pool_scope, monkeypatch):
    scope = pool_scope
    await _fund(scope, "10")
    run_id = await _run(scope)
    attempted = asyncio.Event()
    original = domain_credit.lock_pool

    async def observe_lock(session, domain_id, **kwargs):
        attempted.set()
        return await original(session, domain_id, **kwargs)

    async with asyncio.TaskGroup() as group:
        async with scope.factory() as winner, winner.begin():
            await winner.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
            await original(winner, scope.id)
            monkeypatch.setattr(domain_credit, "lock_pool", observe_lock)
            task = group.create_task(_durable_hold(run_id))
            await asyncio.wait_for(attempted.wait(), 5)
            # If the dashboard holds the quota before waiting for the pool, this forms a cycle.
            await asyncio.wait_for(budgets.lock_project_quota(winner, scope.project, order=budgets.LockOrder()), 5)
    assert task.result() is None


async def test_run_allocation_and_its_call_never_double_count(pool_scope):
    scope = pool_scope
    await _fund(scope, "10")
    root_id = await _run(scope)
    child_id = await _run(scope, parent=root_id)
    async with scope.factory() as session, session.begin():
        await session.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
        await credit.lock_run_payer(session, root_id)
        order = budgets.LockOrder()
        quota = await budgets.lock_project_quota(session, scope.project, order=order)
        quota.max_credit_reservation = Decimal(10)
        root = await budgets.lock_run(session, root_id, order=order, lock_class="root")
        root.credit_ceiling = Decimal(10)
        await budgets.reserve(session, quota=quota, root=root, run_id=child_id, kind="credit", amount=6, order=order)
    await _durable_hold(child_id, "4")
    with pytest.raises(credit.DomainCreditInsufficient):
        await _hold(scope, scope.users[1], "5")
    await _hold(scope, scope.users[1], "4")
    async with scope.factory() as session:
        assert await domain_credit.pending(session, scope.id) == 10
    async with scope.factory() as session, session.begin():
        quota, root, run = await _hooks(child_id)._credit_lineage(session)
        segment = await session.get(ChatRunSegment, (child_id, "provider:0:0"))
        assert await budgets.settle_call_credit(session, run=run, root=root, quota=quota, segment=segment, actual=Decimal(2))
        assert not await budgets.settle_call_credit(session, run=run, root=root, quota=quota, segment=segment, actual=Decimal(2))
    async with scope.factory() as session:
        pool = await session.get(DomainCreditPool, scope.id)
        assert (pool.balance_credits, pool.reserved_credits, pool.monthly_used_credits) == (8, 8, 2)


def _usage(scope, event_id, *, actor=None, key_id=None):
    actor = actor or scope.users[0]
    return dict(event_id=event_id, payer=scope.payer, user_id=actor, project_id=scope.project if key_id is None else actor,
                api_key_id=key_id, model_name="synthetic", provider="synthetic", prompt_tokens=1,
                completion_tokens=0, usage_cost=UsageCost(Decimal(3), Decimal(3), Decimal(0), "priced", {}),
                margin_multiplier=Decimal(1), credit_per_usd=Decimal(1), source="web" if key_id is None else "api")


async def test_settle_release_replay_and_actor_quota(pool_scope):
    scope = pool_scope
    await _fund(scope, "20", manager="5")
    async with scope.factory() as session, session.begin():
        wallet = await credit.lock_wallet(session, scope.users[0], scope.project)
        wallet.max_quota_monthly = Decimal(3)
    event_id = await _hold(scope, scope.users[0], "3")
    await stateless_credit.finish(event_id, uncertain=True, payer=scope.payer)
    assert await asyncio.gather(*(stateless_credit.settle(**_usage(scope, event_id)) for _ in range(2))) == [3, 3]
    with pytest.raises(credit.QuotaExceeded, match="월 사용 한도"):
        await _hold(scope, scope.users[0], "1")
    released_id = await _hold(scope, scope.users[1], "2")
    await asyncio.gather(*(stateless_credit.finish(released_id, uncertain=False, payer=scope.payer) for _ in range(2)))
    with pytest.raises(credit.DomainCreditInsufficient):
        await _hold(scope, scope.users[1], "2.00000001")
    async with scope.factory() as session:
        pool = await session.get(DomainCreditPool, scope.id)
        rows = (await session.scalars(select(DomainCreditLedger).where(DomainCreditLedger.domain_id == scope.id)
                                      .order_by(DomainCreditLedger.id))).all()
        assert [(r.kind, r.amount_credits, r.balance_after) for r in rows] == [
            ("topup", 20, 20), ("charge", -3, 17), ("refund", 0, 17),
        ]
        assert (pool.balance_credits, pool.monthly_used_credits, pool.reserved_credits) == (17, 3, 0)
        assert (await session.get(UserWallet, scope.users[0])).used_quota_this_month == 0
    summary = await user_usage_summary(scope.users[1], "other-project", payer=scope.payer)
    assert (summary["total_credited_cost"], summary["quota_used"], summary["quota_max"]) == (3, 3, 5)


async def test_pool_routes_require_current_manager_and_serialize_admin_limits(pool_scope, monkeypatch):
    scope = pool_scope
    app = FastAPI()
    app.include_router(domain_pools.router, prefix="/v1")
    principal = {"credential_class": "keystone", "domain_id": scope.id,
                 "user_id": scope.users[0], "project_id": scope.project}
    app.dependency_overrides[get_principal] = lambda: principal
    app.dependency_overrides[require_admin] = lambda: {"user_id": "billing-admin"}
    authority = {"domain_id": scope.id, "is_member": True, "can_manage": True}
    resolver = AsyncMock(return_value=authority)
    monkeypatch.setattr(domain_pools.mcp_domain_authority, "resolve_domain_authority", resolver)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        missing = await client.get("/v1/domain-pool")
        assert missing.status_code == 200 and Decimal(missing.json()["balance_credits"]) == 0
        resolver.assert_awaited_once_with(scope.users[0], scope.project, scope.id, compute_manage=True, unrestricted_human=True)
        path = f"/v1/admin/domain-pools/{scope.id}"
        topups = await asyncio.gather(*(client.post(f"{path}/topups", json={"amount_credits": "10"},
                                                   headers={"Idempotency-Key": "one"}) for _ in range(2)))
        assert all(response.status_code == 200 and Decimal(response.json()["balance_credits"]) == 10 for response in topups)
        conflict = await client.post(f"{path}/topups", json={"amount_credits": "12"}, headers={"Idempotency-Key": "one"})
        assert conflict.status_code == 409
        assert (await client.patch("/v1/domain-pool", json={"manager_monthly_limit_credits": 8})).status_code == 200
        updated = await client.patch(path, json={"admin_ceiling_credits": 4})
        assert Decimal(updated.json()["effective_monthly_limit_credits"]) == 4
        denied = await client.patch("/v1/domain-pool", json={"manager_monthly_limit_credits": 5})
        assert denied.status_code == 409 and denied.json()["detail"] == "domain_credit_limit_exceeds_ceiling"
        authority["can_manage"] = False
        assert (await client.get("/v1/domain-pool")).status_code == 403
        resolver.side_effect = HTTPException(503, "Keystone unavailable")
        assert (await client.patch("/v1/domain-pool", json={"manager_monthly_limit_credits": None})).status_code == 503
        principal["credential_class"] = "service_key"
        before = resolver.await_count
        assert (await client.get("/v1/domain-pool")).status_code == 403
        assert resolver.await_count == before
        principal.update(credential_class="keystone", credential_restricted=True)
        assert (await client.get("/v1/domain-pool")).status_code == 403
        assert resolver.await_count == before
