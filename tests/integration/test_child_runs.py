"""MariaDB-backed child lifecycle regressions; no model or cloud fake claims execution."""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import delete, select

from lumen.db import close_db, get_session_factory, init_db
from lumen.models.chat_infrastructure import (
    ChatAgentReservation,
    ChatDelegationCall,
    ChatDelegationGroup,
    ChatProjectAgentQuota,
)
from lumen.models.chat_runs import ChatModelCallReservation, ChatRun, ChatRunEventRow
from lumen.services.durable_runs import budgets, children, lifecycle
from lumen.services.durable_runs.errors import DurableRunError
from lumen.services.run_store import claim_queued_run, replay_events

pytestmark = pytest.mark.integration


@pytest.fixture
async def family():
    init_db(os.environ["DATABASE_URL"], pool_size=3, max_overflow=1)
    factory = get_session_factory()
    assert factory is not None
    nonce = uuid.uuid4().hex
    project_id = f"child-it-{nonce}"
    user_id = f"child-it-{nonce}"
    root_id, group_id, *child_ids = [str(uuid.uuid4()) for _ in range(4)]
    owner = f"worker-{nonce}#1"
    now = datetime.now(UTC)
    try:
        async with factory() as session, session.begin():
            session.add(ChatProjectAgentQuota(
                project_id=project_id, max_active_children=3, max_active_sandboxes=3,
                max_sandbox_seconds=100, max_credit_reservation=Decimal("10"),
            ))
            session.add(ChatRun(
                id=root_id, run_scope="persistent", project_id=project_id, user_id=user_id,
                model_name="scripted-parent", capability_snapshot={}, pricing_snapshot={},
                client_request_id=str(uuid.uuid4()), request_fingerprint=nonce, fingerprint_version=1,
                execution_protocol_version=2, status="running", lease_owner=owner,
                lease_expires_at=now + timedelta(minutes=5), credit_ceiling=Decimal("2"),
                sandbox_seconds_ceiling=60,
            ))
            await session.flush()
            session.add(ChatDelegationGroup(id=group_id, parent_run_id=root_id, model_segment_id="turn-1", state="prepared"))
            for ordinal, child_id in enumerate(child_ids):
                call_id = f"call-{ordinal}"
                session.add(ChatRun(
                    id=child_id, run_scope="child", project_id=project_id, user_id=user_id,
                    model_name="scripted-child", capability_snapshot={}, pricing_snapshot={},
                    client_request_id=str(uuid.uuid4()), request_fingerprint=f"{nonce}-{ordinal}",
                    fingerprint_version=1, execution_protocol_version=2, status="waiting_resource",
                    parent_run_id=root_id, root_run_id=root_id, delegation_call_id=call_id, depth=1,
                    credit_ceiling=Decimal("1"), sandbox_seconds_ceiling=30,
                ))
                await session.flush()
                session.add(ChatDelegationCall(
                    id=str(uuid.uuid4()), group_id=group_id, parent_run_id=root_id,
                    call_id=call_id, fingerprint="a" * 64, ordinal=ordinal,
                    child_run_id=child_id, state="prepared",
                ))
        async with factory() as session, session.begin():
            order = budgets.LockOrder()
            quota = await budgets.lock_project_quota(session, project_id, order=order)
            root = await budgets.lock_run(session, root_id, order=order, lock_class="root")
            for child_id in child_ids:
                for kind, amount in (("child_slot", 1), ("sandbox_slot", 1), ("credit", Decimal("1")), ("sandbox_seconds", 30)):
                    await budgets.reserve(session, quota=quota, root=root, run_id=child_id, kind=kind, amount=amount, order=order)
        yield factory, project_id, user_id, root_id, group_id, child_ids, owner
    finally:
        async with factory() as session, session.begin():
            ids = [root_id, *child_ids]
            await session.execute(delete(ChatRunEventRow).where(ChatRunEventRow.run_id.in_(ids)))
            await session.execute(delete(ChatModelCallReservation).where(ChatModelCallReservation.run_id.in_(ids)))
            await session.execute(delete(ChatAgentReservation).where(ChatAgentReservation.run_id.in_(ids)))
            await session.execute(delete(ChatDelegationCall).where(ChatDelegationCall.group_id == group_id))
            await session.execute(delete(ChatDelegationGroup).where(ChatDelegationGroup.id == group_id))
            await session.execute(delete(ChatRun).where(ChatRun.id.in_(child_ids)))
            await session.execute(delete(ChatRun).where(ChatRun.id == root_id))
            await session.execute(delete(ChatProjectAgentQuota).where(ChatProjectAgentQuota.project_id == project_id))
        await close_db()


async def _snapshot(factory, project_id, root_id, group_id, child_ids):
    async with factory() as session:
        quota = await session.get(ChatProjectAgentQuota, project_id)
        root = await session.get(ChatRun, root_id)
        group = await session.get(ChatDelegationGroup, group_id)
        calls = (await session.execute(select(ChatDelegationCall).where(ChatDelegationCall.group_id == group_id).order_by(ChatDelegationCall.ordinal))).scalars().all()
        reservations = (await session.execute(select(ChatAgentReservation).where(ChatAgentReservation.run_id.in_(child_ids)))).scalars().all()
        events = await replay_events(session, root, after_seq=0)
        return {
            "parent_status": root.status,
            "child_statuses": [(await session.get(ChatRun, child_id)).status for child_id in child_ids],
            "group_state": group.state,
            "checkpoint": group.checkpoint_id,
            "join_segment": group.join_segment_id,
            "call_states": [call.state for call in calls],
            "quota": (quota.active_children, quota.active_sandboxes, quota.credits_reserved, quota.sandbox_seconds_reserved),
            "root_budget": (root.descendant_credits_reserved, root.sandbox_seconds_reserved),
            "reservation_statuses": {(row.run_id, row.kind): row.status for row in reservations},
            "events": [(event.type, event.payload.model_dump(mode="json")) for event in events],
        }


@pytest.mark.parametrize("finish_before_wait", [False, True], ids=["wait-first", "children-first"])
async def test_children_settle_and_join_in_call_order_once(family, finish_before_wait):
    factory, project, user, root_id, group_id, child_ids, owner = family
    with pytest.raises(DurableRunError, match="resumable checkpoint"):
        await children.mark_waiting_children(parent_run_id=root_id, owner=owner, wait_group_id=group_id, checkpoint_id=None)
    assert (await _snapshot(factory, project, root_id, group_id, child_ids))["parent_status"] == "running"

    if not finish_before_wait:
        assert await children.mark_waiting_children(parent_run_id=root_id, owner=owner, wait_group_id=group_id, checkpoint_id="checkpoint-1") == "waiting_children"
        assert (await _snapshot(factory, project, root_id, group_id, child_ids))["parent_status"] == "waiting_children"

    # Complete in reverse order. One canceled child is a typed result, not a successful child.
    canceled = await lifecycle.request_cancelled(run_id=child_ids[1], project_id=project, user_id=user)
    assert canceled.status == "canceled"
    assert (await _snapshot(factory, project, root_id, group_id, child_ids))["parent_status"] == ("running" if finish_before_wait else "waiting_children")
    assert await lifecycle.fail_waiting_run(child_ids[0], error_code="sandbox_unavailable", safe_message="resource unavailable")
    assert not await lifecycle.fail_waiting_run(child_ids[0], error_code="sandbox_unavailable", safe_message="resource unavailable")
    assert (await lifecycle.request_cancelled(run_id=child_ids[1], project_id=project, user_id=user)).status == "canceled"

    if finish_before_wait:
        assert (await _snapshot(factory, project, root_id, group_id, child_ids))["parent_status"] == "running"
        assert await children.mark_waiting_children(parent_run_id=root_id, owner=owner, wait_group_id=group_id, checkpoint_id="checkpoint-1") == "queued"
    state = await _snapshot(factory, project, root_id, group_id, child_ids)
    assert state["parent_status"] == "queued"
    assert state["child_statuses"] == ["failed", "canceled"]
    assert state["call_states"] == ["join_ready", "canceled"]
    assert state["checkpoint"] == "checkpoint-1"
    assert state["quota"] == (0, 0, Decimal("0"), 0)
    assert state["root_budget"] == (Decimal("0"), 0)
    assert set(state["reservation_statuses"].values()) <= {"released", "settled"}
    assert len(state["reservation_statuses"]) == 8
    assert [item[1]["call_id"] for item in state["events"] if item[0] == "child.completed"] == ["call-1", "call-0"]

    async with factory() as session, session.begin():
        claimed = await claim_queued_run(session, root_id, owner="replacement-worker")
        assert claimed is not None
        replacement_owner = claimed.lease_owner
    assert await children.pending_wait_group(root_id) == group_id
    first = await children.delegation_results(parent_run_id=root_id, owner=replacement_owner, wait_group_id=group_id)
    second = await children.delegation_results(parent_run_id=root_id, owner=replacement_owner, wait_group_id=group_id)
    assert second == first
    assert [(row["call_id"], row["status"], row["error_code"]) for row in first["results"]] == [
        ("call-0", "failed", "sandbox_unavailable"), ("call-1", "canceled", "child_canceled")
    ]
    joined = await _snapshot(factory, project, root_id, group_id, child_ids)
    assert joined["group_state"] == "joined"
    assert joined["join_segment"] == f"join:{group_id}"
    assert joined["call_states"] == ["joined", "canceled"]
    assert len([event for event in joined["events"] if event[0] == "child.completed"]) == 2
    assert joined["quota"] == state["quota"]
    assert joined["reservation_statuses"] == state["reservation_statuses"]


async def test_root_budget_ceiling_and_cancellation_settle_every_child_once(family):
    factory, project, user, root_id, group_id, child_ids, owner = family
    before = await _snapshot(factory, project, root_id, group_id, child_ids)
    assert before["root_budget"] == (Decimal("2"), 60)
    assert before["quota"] == (2, 2, Decimal("2"), 60)
    async with factory() as session, session.begin():
        order = budgets.LockOrder()
        quota = await budgets.lock_project_quota(session, project, order=order)
        root = await budgets.lock_run(session, root_id, order=order, lock_class="root")
        # An unprovisioned child can wait for resources; that time is not sandbox use.
        pending = await session.get(ChatRun, child_ids[1])
        pending.created_at = datetime.now(UTC) - timedelta(seconds=120)
        with pytest.raises(budgets.BudgetExceeded) as error:
            await budgets.reserve(session, quota=quota, root=root, run_id=root_id, kind="credit", amount=Decimal("0.01"), order=order)
        assert error.value.code == "child_budget_exhausted"
    assert (await _snapshot(factory, project, root_id, group_id, child_ids))["quota"] == before["quota"]

    await children.mark_waiting_children(parent_run_id=root_id, owner=owner, wait_group_id=group_id, checkpoint_id="checkpoint-1")
    assert await lifecycle.fail_waiting_run(child_ids[1], error_code="sandbox_unavailable", safe_message="resource unavailable")
    assert (await lifecycle.request_cancelled(run_id=root_id, project_id=project, user_id=user)).status == "canceled"
    assert (await lifecycle.request_cancelled(run_id=root_id, project_id=project, user_id=user)).status == "canceled"
    assert not await lifecycle.fail_waiting_run(child_ids[0], error_code="sandbox_unavailable", safe_message="resource unavailable")
    after = await _snapshot(factory, project, root_id, group_id, child_ids)
    assert after["parent_status"] == "canceled"
    assert after["child_statuses"] == ["canceled", "failed"]
    assert after["quota"] == (0, 0, Decimal("0"), 0)
    assert after["root_budget"] == (Decimal("0"), 0)
    assert len([event for event in after["events"] if event[0] == "child.completed"]) == 2
    assert len([event for event in after["events"] if event[0] == "run.canceled"]) == 1
    assert len(after["reservation_statuses"]) == 8
    assert set(after["reservation_statuses"].values()) <= {"settled", "released"}

async def test_child_credit_overrun_blocks_child_and_parent_without_recharging_replay(family, monkeypatch):
    from lumen.models.chat_runs import ChatRunSegment
    from lumen.services import litellm_client
    from lumen.services.durable_runs.execution import _DurableExecutionHooks

    factory, project, _user, root_id, _group_id, child_ids, owner = family
    pricing = {
        "input_price_per_token": "0.6", "output_price_per_token": "0",
        "margin_multiplier": "1", "chat_credit_per_usd": "1", "component_prices": {},
    }
    async with factory() as session, session.begin():
        for run_id in (root_id, child_ids[0]):
            run = await session.get(ChatRun, run_id)
            run.pricing_snapshot = pricing
            run.capability_snapshot = {"capabilities": {"context_limit": 1}}
        child = await session.get(ChatRun, child_ids[0])
        child.status = "running"
        child.lease_owner = owner
        child.lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)
        root = await session.get(ChatRun, root_id)
        root.credit_ceiling = Decimal("3")

    monkeypatch.setattr(
        litellm_client, "count_context_tokens",
        lambda *_args: litellm_client.ContextTokenCount(tokens=1, measurement="tokenizer"),
    )
    child_hooks = _DurableExecutionHooks(run_id=child_ids[0], owner=owner, credit_bounded=True)
    call = {"round_index": 0, "attempt": 1, "messages": [{"role": "user", "content": "hi"}],
            "tool_schemas": [], "max_tokens": 1}
    assert await child_hooks.provider_started(**call) is None
    async with factory() as session:
        child = await session.get(ChatRun, child_ids[0])
        segment = await session.get(ChatRunSegment, (child_ids[0], "provider:0:1"))
        reservation = await session.get(ChatModelCallReservation, (child_ids[0], "provider:0:1"))
        assert child.reserved_credits == Decimal("0.60000001")
        assert reservation.bound_credits == Decimal("0.60000001")
        assert segment.status == "provider_started"
    result = {"text": "ok", "usage": {"prompt_tokens": 2, "completion_tokens": 0}}
    await child_hooks.provider_completed(
        round_index=0, attempt=1, usage=result["usage"], result_payload=result,
    )
    replay = await child_hooks.provider_started(**call)
    assert replay["_durable_replay"] is True
    assert replay["text"] == "ok"
    await child_hooks.provider_completed(round_index=0, attempt=1, usage=result["usage"], result_payload=result)
    with pytest.raises(budgets.BudgetExceeded, match="credit ceiling"):
        await child_hooks.provider_started(**{**call, "attempt": 2})
    parent_hooks = _DurableExecutionHooks(run_id=root_id, owner=owner, credit_bounded=True)
    assert await parent_hooks.provider_started(**call) is None
    await parent_hooks.provider_completed(round_index=0, attempt=1, usage=result["usage"], result_payload=result)
    assert (await parent_hooks.provider_started(**call))["_durable_replay"] is True
    with pytest.raises(budgets.BudgetExceeded, match="credit ceiling"):
        await parent_hooks.provider_started(**{**call, "attempt": 2})
    async with factory() as session:
        child = await session.get(ChatRun, child_ids[0])
        assert child.reserved_credits == Decimal("1.2")
        reservation = await session.get(ChatModelCallReservation, (child_ids[0], "provider:0:1"))
        assert reservation.actual_credits == Decimal("1.2")
        assert (await session.get(ChatProjectAgentQuota, project)).credits_reserved == Decimal("3.2")
        assert (await session.get(ChatRun, root_id)).reserved_credits == Decimal("1.2")


async def test_zero_price_provider_call_keeps_fence_without_credit_headroom(family, monkeypatch):
    from lumen.services import litellm_client
    from lumen.services.durable_runs.execution import _DurableExecutionHooks

    factory, _project, _user, root_id, _group_id, _child_ids, owner = family
    async with factory() as session, session.begin():
        root = await session.get(ChatRun, root_id)
        root.pricing_snapshot = {
            "input_price_per_token": "0", "output_price_per_token": "0",
            "margin_multiplier": "1", "chat_credit_per_usd": "1", "component_prices": {},
        }
        root.capability_snapshot = {"capabilities": {}}
    monkeypatch.setattr(
        litellm_client, "count_context_tokens",
        lambda *_args: litellm_client.ContextTokenCount(tokens=1, measurement="tokenizer"),
    )
    hooks = _DurableExecutionHooks(run_id=root_id, owner=owner, credit_bounded=True)
    call = {"round_index": 0, "attempt": 1, "messages": [{"role": "user", "content": "hi"}],
            "tool_schemas": [], "max_tokens": 1}
    assert await hooks.provider_started(**call) is None
    payload = {"text": "free", "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
    await hooks.provider_completed(round_index=0, attempt=1, usage=payload["usage"], result_payload=payload)
    assert (await hooks.provider_started(**call))["_durable_replay"] is True
    async with factory() as session:
        reservation = await session.get(ChatModelCallReservation, (root_id, "provider:0:1"))
        assert reservation.bound_credits == reservation.actual_credits == Decimal("0")
        assert (await session.get(ChatRun, root_id)).reserved_credits == Decimal("0")


async def test_unknown_provider_holds_bound_and_priced_tool_is_denied_before_dispatch(family, monkeypatch):
    from lumen.services import litellm_client
    from lumen.services.durable_runs.execution import _DurableExecutionHooks

    factory, _project, _user, _root_id, _group_id, child_ids, owner = family
    child_id = child_ids[0]
    async with factory() as session, session.begin():
        child = await session.get(ChatRun, child_id)
        child.status = "running"
        child.lease_owner = owner
        child.lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)
        child.pricing_snapshot = {
            "input_price_per_token": "0.6", "output_price_per_token": "0",
            "margin_multiplier": "1", "chat_credit_per_usd": "1",
            "component_prices": {"web_fetch_request_per_unit": "0.8", "web_fetch_context_per_unit": "0.4"},
        }
        child.capability_snapshot = {"capabilities": {"context_limit": 1}}
    monkeypatch.setattr(
        litellm_client, "count_context_tokens",
        lambda *_args: litellm_client.ContextTokenCount(tokens=1, measurement="tokenizer"),
    )
    hooks = _DurableExecutionHooks(run_id=child_id, owner=owner, credit_bounded=True)
    with pytest.raises(budgets.BudgetExceeded, match="child credit ceiling"):
        await hooks.tool_started(
            round_index=0, tool_index=0, tool_call_id="fetch-0", tool_name="managed_web_fetch",
        )
    async with factory() as session:
        assert await session.get(ChatModelCallReservation, (child_id, "tool:0:0")) is None

    call = {"round_index": 0, "attempt": 1, "messages": [{"role": "user", "content": "hi"}],
            "tool_schemas": [], "max_tokens": 1}
    assert await hooks.provider_started(**call) is None
    assert (await hooks.provider_started(**call))["_boundary_abort"] == "provider_result_unknown"
    assert (await hooks.provider_started(**call))["_boundary_abort"] == "provider_result_unknown"
    with pytest.raises(budgets.BudgetExceeded, match="child credit ceiling"):
        await hooks.provider_started(**{**call, "attempt": 2})
    async with factory() as session:
        reservation = await session.get(ChatModelCallReservation, (child_id, "provider:0:1"))
        assert reservation.status == "unknown"
        assert reservation.bound_credits == Decimal("0.60000001")
        assert (await session.get(ChatRun, child_id)).reserved_credits == Decimal("0.60000001")

async def test_expired_parent_lease_requeues_prepared_children_without_an_unresumable_wait(family):
    factory, project, user, root_id, group_id, child_ids, _owner = family
    async with factory() as session, session.begin():
        root = await session.get(ChatRun, root_id)
        root.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    recovered = await lifecycle.recover_stale_runs(owner="recovery-worker")
    assert root_id in recovered
    state = await _snapshot(factory, project, root_id, group_id, child_ids)
    assert state["parent_status"] == "queued"
    assert state["group_state"] == "prepared"
    assert state["checkpoint"] is None
    assert state["child_statuses"] == ["waiting_resource", "waiting_resource"]
    assert await children.pending_wait_group(root_id) is None
    async with factory() as session, session.begin():
        claimed = await claim_queued_run(session, root_id, owner="recovery-worker")
        assert claimed is not None
        replacement_owner = claimed.lease_owner
    assert await children.mark_waiting_children(
        parent_run_id=root_id, owner=replacement_owner, wait_group_id=group_id, checkpoint_id="recovered-checkpoint"
    ) == "waiting_children"
    assert (await lifecycle.request_cancelled(run_id=root_id, project_id=project, user_id=user)).status == "canceled"
    assert (await _snapshot(factory, project, root_id, group_id, child_ids))["quota"] == (0, 0, Decimal("0"), 0)


async def test_two_child_group_commits_resources_and_replays_without_duplicate_reservations(monkeypatch):
    """Two siblings must commit atomically despite ledger/resource lock-class boundaries."""
    from types import SimpleNamespace

    from lumen.crypto import encrypt_chat_content
    from lumen.models.chat_db import ChatAgent
    from lumen.models.chat_infrastructure import ChatResourceOperation, ChatRuntimePool, ChatRuntimeResource
    from lumen.services.agent_policy import default_execution_policy
    from lumen.services.durable_runs.errors import DurableRunConflict

    init_db(os.environ["DATABASE_URL"], pool_size=3, max_overflow=1)
    factory = get_session_factory()
    assert factory is not None
    nonce = uuid.uuid4().hex
    project = f"spawn-it-{nonce}"
    user = f"spawn-it-{nonce}"
    root_id, pool_id = str(uuid.uuid4()), str(uuid.uuid4())
    owner = f"spawn-worker-{nonce}#1"
    policy = default_execution_policy().model_copy(update={"can_delegate_read": True, "max_children": 2, "max_parallel_children": 2})
    try:
        async with factory() as session, session.begin():
            session.add(ChatProjectAgentQuota(
                project_id=project, max_active_children=2, max_active_sandboxes=2,
                max_sandbox_seconds=60, max_credit_reservation=Decimal("2"),
            ))
            session.add(ChatRuntimePool(
                id=pool_id, deployment_id="spawn-it", name=nonce[:32], role="sandbox", backend="nova",
                enabled=True, cloud_profile_id="sandbox", project_id="operator-sandbox",
                region_name="RegionOne", image_ref="sha256:" + "a" * 64,
                profile_digest="b" * 64, max_replicas=2,
            ))
            parent_agent = ChatAgent(owner_user_id=user, project_id=project, name="Parent", is_active=True)
            child_agent = ChatAgent(owner_user_id=user, project_id=project, name="Child", is_active=True)
            session.add_all([parent_agent, child_agent])
            await session.flush()
            parent_agent.delegable_agent_ids = [child_agent.id]
            session.add(ChatRun(
                id=root_id, run_scope="persistent", project_id=project, user_id=user,
                model_name="scripted-parent", capability_snapshot={"execution_policy": policy.model_dump(mode="json")},
                pricing_snapshot={}, request_payload=encrypt_chat_content("{}"),
                client_request_id=str(uuid.uuid4()), request_fingerprint=nonce, fingerprint_version=1,
                agent_id=parent_agent.id, execution_protocol_version=2, status="running", lease_owner=owner,
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
                credit_ceiling=Decimal("2"), sandbox_seconds_ceiling=60,
            ))

        async def resolved_inputs(_parent, _payload, _call, _agent):
            return {
                "policy": policy, "direct_effects": frozenset({"read"}),
                "route": {"model_name": "scripted-child"},
                "capability_snapshot": {}, "pricing_snapshot": {}, "extension_selection": {},
                "plugin_tool_snapshots": [], "skill_instructions": [], "skill_snapshot": [],
                "max_tokens": 128, "temperature": None,
            }

        monkeypatch.setattr(children, "_prepare_child_inputs", resolved_inputs)
        monkeypatch.setattr(children, "get_settings", lambda: SimpleNamespace(runtime_config=SimpleNamespace(
            deployment_id="spawn-it", pools=(SimpleNamespace(name=nonce[:32], role="sandbox", enabled=True),),
        )))
        calls = [children.DelegationCall(
            call_id=f"call-{ordinal}", agent_id=child_agent.id, task=f"Task {ordinal}",
            access="read", credit_budget=Decimal("1"), sandbox_seconds=30,
        ) for ordinal in range(2)]
        created = await children.prepare_delegations(parent_run_id=root_id, owner=owner, model_segment_id="turn-1", calls=calls)
        repeated = await children.prepare_delegations(parent_run_id=root_id, owner=owner, model_segment_id="turn-1", calls=calls)
        assert repeated == created
        assert len({item["child_run_id"] for item in created["children"]}) == 2
        with pytest.raises(DurableRunConflict, match="fingerprint"):
            await children.prepare_delegations(
                parent_run_id=root_id, owner=owner, model_segment_id="turn-1",
                calls=[calls[0], children.DelegationCall(
                    call_id="call-1", agent_id=child_agent.id, task="Different task",
                    access="read", credit_budget=Decimal("1"), sandbox_seconds=30,
                )],
            )
        async with factory() as session:
            child_ids = [item["child_run_id"] for item in created["children"]]
            rows = (await session.execute(select(ChatRun).where(ChatRun.parent_run_id == root_id))).scalars().all()
            resources = (await session.execute(select(ChatRuntimeResource).where(ChatRuntimeResource.run_id.in_(child_ids)))).scalars().all()
            reservations = (await session.execute(select(ChatAgentReservation).where(ChatAgentReservation.run_id.in_(child_ids)))).scalars().all()
            quota = await session.get(ChatProjectAgentQuota, project)
            root = await session.get(ChatRun, root_id)
            assert {row.id for row in rows} == set(child_ids)
            assert len(resources) == 2 and {row.run_id for row in resources} == set(child_ids)
            assert all(row.assigned_resource_id in {resource.id for resource in resources} for row in rows)
            assert len(reservations) == 8 and all(row.status == "reserved" for row in reservations)
            assert (quota.active_children, quota.active_sandboxes, quota.credits_reserved, quota.sandbox_seconds_reserved) == (2, 2, Decimal("2"), 60)
            assert (root.descendant_credits_reserved, root.sandbox_seconds_reserved) == (Decimal("2"), 60)
            events = await replay_events(session, root, after_seq=0)
            assert [event.payload.call_id for event in events if event.type == "child.created"] == ["call-0", "call-1"]
    finally:
        async with factory() as session, session.begin():
            child_ids = list((await session.execute(select(ChatRun.id).where(ChatRun.parent_run_id == root_id))).scalars())
            ids = [root_id, *child_ids]
            await session.execute(delete(ChatRunEventRow).where(ChatRunEventRow.run_id.in_(ids)))
            resource_ids = select(ChatRuntimeResource.id).where(ChatRuntimeResource.run_id.in_(child_ids))
            await session.execute(delete(ChatResourceOperation).where(ChatResourceOperation.resource_id.in_(resource_ids)))
            await session.execute(delete(ChatRuntimeResource).where(ChatRuntimeResource.run_id.in_(child_ids)))
            await session.execute(delete(ChatAgentReservation).where(ChatAgentReservation.run_id.in_(ids)))
            group_ids = list((await session.execute(select(ChatDelegationGroup.id).where(ChatDelegationGroup.parent_run_id == root_id))).scalars())
            if group_ids:
                await session.execute(delete(ChatDelegationCall).where(ChatDelegationCall.group_id.in_(group_ids)))
                await session.execute(delete(ChatDelegationGroup).where(ChatDelegationGroup.id.in_(group_ids)))
            await session.execute(delete(ChatRun).where(ChatRun.id.in_(child_ids)))
            await session.execute(delete(ChatRun).where(ChatRun.id == root_id))
            await session.execute(delete(ChatAgent).where(ChatAgent.project_id == project))
            await session.execute(delete(ChatRuntimePool).where(ChatRuntimePool.id == pool_id))
            await session.execute(delete(ChatProjectAgentQuota).where(ChatProjectAgentQuota.project_id == project))
        await close_db()


async def test_code_roots_reuse_one_project_sandbox_slot_after_terminal_paths(monkeypatch):
    from types import SimpleNamespace

    from lumen.models.chat_infrastructure import ChatResourceOperation, ChatRuntimePool, ChatRuntimeResource
    from lumen.models.chat_runs import ChatRunSegment
    from lumen.services.durable_runs.admission import _apply_root_budget
    from lumen.services.durable_runs.execution import _finish
    from lumen.services.infrastructure.config import ProjectQuotaDefaults

    init_db(os.environ["DATABASE_URL"], pool_size=3, max_overflow=1)
    factory = get_session_factory()
    assert factory is not None
    nonce = uuid.uuid4().hex
    project = f"code-it-{nonce}"
    pool_id = str(uuid.uuid4())
    run_ids = [str(uuid.uuid4()) for _ in range(4)]
    defaults = ProjectQuotaDefaults(max_active_sandboxes=1, max_sandbox_seconds=120, max_credit_reservation="1")
    runtime_config = SimpleNamespace(
        deployment_id="code-it", project_quota_defaults=defaults,
        pools=(SimpleNamespace(name=nonce[:32], enabled=True, role="sandbox"),),
    )
    monkeypatch.setattr("lumen.config.get_settings", lambda: SimpleNamespace(runtime_config=runtime_config))
    try:
        async with factory() as session, session.begin():
            session.add(ChatRuntimePool(
                id=pool_id, deployment_id="code-it", name=nonce[:32], role="sandbox", backend="nova",
                enabled=True, cloud_profile_id="sandbox", project_id="operator-sandbox",
                region_name="RegionOne", image_ref="sha256:" + "a" * 64,
                profile_digest="b" * 64, max_replicas=4,
            ))
        for index, run_id in enumerate(run_ids):
            async with factory() as session, session.begin():
                session.add(ChatRun(
                    id=run_id, run_scope="persistent", run_kind="completion", execution_mode="code",
                    project_id=project, user_id=project, model_name="scripted-code",
                    capability_snapshot={}, pricing_snapshot={}, client_request_id=str(uuid.uuid4()),
                    request_fingerprint=nonce, fingerprint_version=1, execution_protocol_version=2,
                    status="queued",
                ))
                await session.flush()
                order = budgets.LockOrder()
                quota = await budgets.lock_project_quota(session, project, order=order)
                assert (quota.max_active_sandboxes, quota.max_sandbox_seconds, quota.max_credit_reservation) == (1, 120, Decimal("1"))
                root = await budgets.lock_run(session, run_id, order=order, lock_class="root")
                assert await _apply_root_budget(
                    session, root, agent_budget={
                        "credit_ceiling": "1", "sandbox_seconds_ceiling": 30, "wall_time_seconds": 90,
                    }, execution_mode="code", quota=quota, order=order,
                )
                root.status = "waiting_resource"
                assert quota.active_sandboxes == 1
            if index == 0:
                assert (await lifecycle.request_cancelled(run_id=run_id, project_id=project, user_id=project)).status == "canceled"
                assert (await lifecycle.request_cancelled(run_id=run_id, project_id=project, user_id=project)).status == "canceled"
            elif index == 1:
                assert await lifecycle.fail_waiting_run(run_id, error_code="sandbox_unavailable", safe_message="unavailable")
                assert not await lifecycle.fail_waiting_run(run_id, error_code="sandbox_unavailable", safe_message="unavailable")
            else:
                async with factory() as session, session.begin():
                    order = budgets.LockOrder()
                    quota = await budgets.lock_project_quota(session, project, order=order)
                    root = await budgets.lock_run(session, run_id, order=order, lock_class="root")
                    segment = ChatRunSegment(run_id=run_id, segment_id="provider:0:1", ordinal=1, endpoint="provider")
                    session.add(segment)
                    await session.flush()
                    bound = Decimal("0.6") if index == 2 else Decimal("0.8")
                    spent = Decimal("0.4") if index == 2 else Decimal("0.7")
                    await budgets.reserve_call_credit(session, run=root, root=root, quota=quota, segment=segment, amount=bound)
                    await budgets.settle_call_credit(session, run=root, root=root, quota=quota, segment=segment, actual=spent)
                    segment.status = "completed"
                    assert quota.credits_reserved == spent
                async with factory() as session, session.begin():
                    root = await session.get(ChatRun, run_id)
                    root.status = "running"
                    root.lease_owner = "worker#1"
                    root.lease_expires_at = datetime.now(UTC) + timedelta(minutes=2)
                    resource = await session.get(ChatRuntimeResource, root.assigned_resource_id)
                    resource.ready_at = datetime.now(UTC) - timedelta(seconds=5)
                    resource.observed_state = "ready"
                await _finish(run_id, status="completed", message_id=None, owner="worker#1")
            async with factory() as session:
                quota = await session.get(ChatProjectAgentQuota, project)
                root = await session.get(ChatRun, run_id)
                reservations = (await session.execute(select(ChatAgentReservation).where(
                    ChatAgentReservation.run_id == run_id
                ))).scalars().all()
                assert quota.active_sandboxes == 0
                assert quota.credits_reserved == 0
                assert root.reservation_released_at is not None
                if index >= 2:
                    assert root.reserved_credits == (Decimal("0.4") if index == 2 else Decimal("0.7"))
                    call = await session.get(ChatModelCallReservation, (run_id, "provider:0:1"))
                    assert call.actual_credits == root.reserved_credits
                assert {row.kind: row.status for row in reservations} == {
                    "sandbox_slot": "released", "sandbox_seconds": "settled",
                }
                assert root.sandbox_seconds_reserved == 0
                assert quota.sandbox_seconds_reserved == 0
                seconds = next(row for row in reservations if row.kind == "sandbox_seconds")
                if index >= 2:
                    assert 4 <= seconds.settled_amount <= 8
                else:
                    assert seconds.settled_amount == 0
        runtime_config.project_quota_defaults = ProjectQuotaDefaults(
            max_active_sandboxes=9, max_sandbox_seconds=999, max_credit_reservation="9"
        )
        async with factory() as session, session.begin():
            quota = await budgets.lock_project_quota(session, project, order=budgets.LockOrder())
            assert (quota.max_active_sandboxes, quota.max_sandbox_seconds, quota.max_credit_reservation) == (
                1, 120, Decimal("1"),
            )
    finally:
        async with factory() as session, session.begin():
            await session.execute(delete(ChatRunEventRow).where(ChatRunEventRow.run_id.in_(run_ids)))
            await session.execute(delete(ChatModelCallReservation).where(ChatModelCallReservation.run_id.in_(run_ids)))
            await session.execute(delete(ChatRunSegment).where(ChatRunSegment.run_id.in_(run_ids)))
            resource_ids = select(ChatRuntimeResource.id).where(ChatRuntimeResource.run_id.in_(run_ids))
            await session.execute(delete(ChatResourceOperation).where(ChatResourceOperation.resource_id.in_(resource_ids)))
            await session.execute(delete(ChatRuntimeResource).where(ChatRuntimeResource.run_id.in_(run_ids)))
            await session.execute(delete(ChatAgentReservation).where(ChatAgentReservation.run_id.in_(run_ids)))
            await session.execute(delete(ChatRun).where(ChatRun.id.in_(run_ids)))
            await session.execute(delete(ChatRuntimePool).where(ChatRuntimePool.id == pool_id))
            await session.execute(delete(ChatProjectAgentQuota).where(ChatProjectAgentQuota.project_id == project))
        await close_db()


@pytest.mark.parametrize("ready_seconds", [None, 5], ids=["never-ready", "ready-after-provisioning"])
async def test_child_provisioning_wait_is_not_sandbox_usage(family, ready_seconds):
    from lumen.models.chat_infrastructure import ChatResourceOperation, ChatRuntimePool, ChatRuntimeResource

    factory, project, user, root_id, group_id, child_ids, owner = family
    pool_id, resource_id = str(uuid.uuid4()), str(uuid.uuid4())
    try:
        async with factory() as session, session.begin():
            session.add(ChatRuntimePool(
                id=pool_id, deployment_id="child-it", name=uuid.uuid4().hex, role="sandbox", backend="nova",
                enabled=True, cloud_profile_id="sandbox", project_id="operator-sandbox",
                region_name="RegionOne", image_ref="sha256:" + "a" * 64,
                profile_digest="b" * 64, max_replicas=1,
            ))
            resource = ChatRuntimeResource(
                id=resource_id, pool_id=pool_id, role="sandbox", backend="nova", run_id=child_ids[0],
                request_fingerprint="a" * 64, cloud_profile_id="sandbox", cloud_project_id="operator-sandbox",
                image_ref="sha256:" + "a" * 64, policy_digest="b" * 64,
                observed_state="ready" if ready_seconds is not None else "creating",
                created_at=datetime.now(UTC) - timedelta(seconds=120),
                ready_at=datetime.now(UTC) - timedelta(seconds=ready_seconds) if ready_seconds is not None else None,
            )
            session.add(resource)
            (await session.get(ChatRun, child_ids[0])).assigned_resource_id = resource_id
        assert await lifecycle.fail_waiting_run(child_ids[0], error_code="sandbox_unavailable", safe_message="unavailable")
        async with factory() as session:
            quota = await session.get(ChatProjectAgentQuota, project)
            root = await session.get(ChatRun, root_id)
            row = (await session.execute(select(ChatAgentReservation).where(
                ChatAgentReservation.run_id == child_ids[0], ChatAgentReservation.kind == "sandbox_seconds"
            ))).scalar_one()
            assert row.status == "settled"
            if ready_seconds is None:
                assert row.settled_amount == 0
                assert quota.sandbox_seconds_reserved == root.sandbox_seconds_reserved == 30
            else:
                assert 4 <= row.settled_amount <= 8
                assert quota.sandbox_seconds_reserved == root.sandbox_seconds_reserved == 30 + int(row.settled_amount)
        await children.mark_waiting_children(
            parent_run_id=root_id, owner=owner, wait_group_id=group_id, checkpoint_id="checkpoint-provision"
        )
        assert (await lifecycle.request_cancelled(run_id=root_id, project_id=project, user_id=user)).status == "canceled"
        async with factory() as session:
            quota = await session.get(ChatProjectAgentQuota, project)
            root = await session.get(ChatRun, root_id)
            assert quota.sandbox_seconds_reserved == root.sandbox_seconds_reserved == 0
            row = (await session.execute(select(ChatAgentReservation).where(
                ChatAgentReservation.run_id == child_ids[0], ChatAgentReservation.kind == "sandbox_seconds"
            ))).scalar_one()
            assert row.status == "settled"
            assert row.released_at is not None
    finally:
        async with factory() as session, session.begin():
            await session.execute(delete(ChatResourceOperation).where(ChatResourceOperation.resource_id == resource_id))
            await session.execute(delete(ChatRuntimeResource).where(ChatRuntimeResource.id == resource_id))
            await session.execute(delete(ChatRuntimePool).where(ChatRuntimePool.id == pool_id))


async def test_root_terminal_releases_spent_credit_without_erasing_call_ledger(family):
    from lumen.models.chat_runs import ChatRunSegment

    factory, project, user, root_id, group_id, child_ids, owner = family
    try:
        async with factory() as session, session.begin():
            order = budgets.LockOrder()
            quota = await budgets.lock_project_quota(session, project, order=order)
            root = await budgets.lock_run(session, root_id, order=order, lock_class="root")
            child = await budgets.lock_run(session, child_ids[0], order=order, lock_class="child")
            root.credit_ceiling = Decimal("3")
            root_call = ChatRunSegment(run_id=root_id, segment_id="credit-root", ordinal=1, endpoint="provider")
            child_call = ChatRunSegment(run_id=child.id, segment_id="credit-child", ordinal=1, endpoint="provider")
            session.add_all([root_call, child_call])
            await session.flush()
            await budgets.reserve_call_credit(
                session, run=root, root=root, quota=quota, segment=root_call, amount=Decimal("0.6")
            )
            await budgets.settle_call_credit(
                session, run=root, root=root, quota=quota, segment=root_call, actual=Decimal("0.4")
            )
            await budgets.reserve_call_credit(
                session, run=child, root=root, quota=quota, segment=child_call, amount=Decimal("0.6")
            )
            await budgets.settle_call_credit(
                session, run=child, root=root, quota=quota, segment=child_call, actual=Decimal("0.5")
            )
            root_call.status = child_call.status = "completed"
        assert await lifecycle.fail_waiting_run(child_ids[0], error_code="done", safe_message="done")
        async with factory() as session:
            quota = await session.get(ChatProjectAgentQuota, project)
            assert quota.credits_reserved == Decimal("1.9")
        await children.mark_waiting_children(
            parent_run_id=root_id, owner=owner, wait_group_id=group_id, checkpoint_id="checkpoint-credit"
        )
        assert (await lifecycle.request_cancelled(run_id=root_id, project_id=project, user_id=user)).status == "canceled"
        assert (await lifecycle.request_cancelled(run_id=root_id, project_id=project, user_id=user)).status == "canceled"
        async with factory() as session:
            quota = await session.get(ChatProjectAgentQuota, project)
            root = await session.get(ChatRun, root_id)
            child_row = (await session.execute(select(ChatAgentReservation).where(
                ChatAgentReservation.run_id == child_ids[0], ChatAgentReservation.kind == "credit"
            ))).scalar_one()
            assert quota.credits_reserved == 0
            assert root.descendant_credits_reserved == 0
            assert root.reserved_credits == Decimal("0.4")
            assert root.reservation_released_at is not None
            assert child_row.status == "settled" and child_row.settled_amount == Decimal("0.5")
            assert child_row.released_at is not None
            assert (await session.get(ChatModelCallReservation, (root_id, "credit-root"))).actual_credits == Decimal("0.4")
            assert (await session.get(ChatModelCallReservation, (child_ids[0], "credit-child"))).actual_credits == Decimal("0.5")
    finally:
        async with factory() as session, session.begin():
            await session.execute(delete(ChatModelCallReservation).where(
                ChatModelCallReservation.run_id.in_((root_id, child_ids[0]))
            ))
            await session.execute(delete(ChatRunSegment).where(ChatRunSegment.run_id.in_((root_id, child_ids[0]))))


async def test_terminal_root_keeps_running_child_holds_until_child_finishes(family):
    from lumen.services.durable_runs.execution import _finish

    factory, project, user, root_id, group_id, child_ids, owner = family
    async with factory() as session, session.begin():
        child = await session.get(ChatRun, child_ids[0])
        child.status = "running"
        child.lease_owner = owner
        child.lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)
    await children.mark_waiting_children(
        parent_run_id=root_id, owner=owner, wait_group_id=group_id, checkpoint_id="checkpoint-late-child"
    )
    assert (await lifecycle.request_cancelled(run_id=root_id, project_id=project, user_id=user)).status == "canceled"
    async with factory() as session:
        quota = await session.get(ChatProjectAgentQuota, project)
        root = await session.get(ChatRun, root_id)
        assert root.reservation_released_at is not None
        assert (quota.active_children, quota.active_sandboxes, quota.credits_reserved, quota.sandbox_seconds_reserved) == (
            1, 1, Decimal("1"), 30,
        )
    await _finish(child_ids[0], status="canceled", message_id=None, owner=owner)
    async with factory() as session:
        quota = await session.get(ChatProjectAgentQuota, project)
        assert (quota.active_children, quota.active_sandboxes, quota.credits_reserved, quota.sandbox_seconds_reserved) == (
            0, 0, Decimal("0"), 0,
        )
