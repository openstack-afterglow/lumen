"""Real SQL selection admission with an in-process Kubernetes API, never a live cluster.

Provider qualification, bootstrap identity, and readyz below are synthetic. SQL
reservation, controller fencing, Kubernetes object construction, and dispatch target
lookup use their real implementations.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import test_infrastructure_store as infrastructure_fixtures
import test_kubernetes_provider as kubernetes_fixtures
from sqlalchemy import delete, select

from lumen.crypto import decrypt_chat_content, encrypt_chat_content
from lumen.models.chat_contracts import ToolPolicy
from lumen.models.chat_db import ChatAgent
from lumen.models.chat_infrastructure import (
    ChatAgentReservation,
    ChatDelegationCall,
    ChatDelegationGroup,
    ChatProjectAgentQuota,
    ChatResourceOperation,
    ChatRuntimePool,
    ChatRuntimeResource,
)
from lumen.models.chat_runs import ChatRun, ChatRunEventRow
from lumen.services import chat_admission, sandbox_binding, sandbox_selection
from lumen.services.agent_policy import default_execution_policy
from lumen.services.durable_runs import admission, budgets, children, lifecycle
from lumen.services.durable_runs.errors import DurableRunInputError
from lumen.services.durable_runs.execution import _finish
from lumen.services.infrastructure import bootstrap, kubernetes, store
from lumen.services.infrastructure.controller import ResourceController

pytestmark = pytest.mark.integration

# Register the existing fixture objects explicitly without duplicating their SQL/API setup.
pool = infrastructure_fixtures.pool
lazy_chat_sandbox = infrastructure_fixtures.lazy_chat_sandbox
kubernetes_provider = kubernetes_fixtures.provider
_controller_config = infrastructure_fixtures._controller_config

_BUDGET = {"credit_ceiling": "1", "sandbox_seconds_ceiling": 30, "wall_time_seconds": 90}


async def _apply(fixture, run_id, *, agent_budget=None, execution_mode="chat"):
    """Exercise the actual admission transaction without provider/model setup."""
    async with fixture.factory() as session, session.begin():
        order = budgets.LockOrder()
        quota = await budgets.lock_project_quota(session, fixture.project, order=order)
        run = await budgets.lock_run(session, run_id, order=order, lock_class="root")
        waiting = await admission._apply_root_budget(
            session, run, agent_budget=agent_budget, execution_mode=execution_mode, quota=quota, order=order,
        )
        run.status = "waiting_resource" if waiting else "queued"
        return waiting


async def _new_run(fixture, *, scope="temp", selected=True, **values):
    policy = ToolPolicy(enabled_sandbox_tools=["sandbox_echo", "sandbox_status"] if selected else [])
    snapshot = {"sandbox_tools": sandbox_selection.freeze_selection(
        policy.enabled_sandbox_tools, config=fixture.config,
    )}
    return await fixture.create_run(
        run_scope=scope, status="queued", lease_owner=None, lease_expires_at=None, lease_fence=0,
        capability_snapshot=snapshot, **values,
    )


async def _state(fixture, run_id):
    async with fixture.factory() as session:
        run = await session.get(ChatRun, run_id)
        quota = await session.get(ChatProjectAgentQuota, fixture.project)
        resources = list((await session.scalars(select(ChatRuntimeResource).where(
            ChatRuntimeResource.pool_id == fixture.pool_id,
        ))).all())
        holds = list((await session.scalars(select(ChatAgentReservation).where(
            ChatAgentReservation.run_id == run_id,
        ))).all())
        return run, quota, resources, holds


async def _running(fixture, run_id):
    async with fixture.factory() as session, session.begin():
        run = await session.get(ChatRun, run_id)
        assert run.status == "queued"
        run.status = "running"
        run.lease_owner = "worker#1"
        run.lease_fence = 1
        run.lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)


@pytest.fixture
async def selected_controller(lazy_chat_sandbox, kubernetes_provider, monkeypatch):
    """Reuse real SQL and Kubernetes HTTP fixtures, replacing only signer/readyz peers."""
    f = lazy_chat_sandbox
    provider, api = kubernetes_provider
    provider.pool, provider.profile = f.pool, f.pool.profile
    provider.deployment_id = f.config.deployment_id
    provider.controller_url = "https://192.0.2.11:8013"
    for field in ("labels", "annotations"):
        for key, value in (("lumen_deployment", provider.deployment_id), ("lumen_pool", f.pool.name)):
            api.namespace["metadata"][field][kubernetes._KEYS[key]] = (
                kubernetes._label(value) if field == "labels" else value
            )
    report_file = Path(provider.cluster.qualification_file)
    report = json.loads(report_file.read_bytes())
    report["pool_digest"] = f.pool.digest()
    report_bytes = json.dumps(report).encode()
    report_file.write_bytes(report_bytes)
    provider.cluster = provider.cluster.model_copy(update={
        "qualification_sha256": hashlib.sha256(report_bytes).hexdigest(),
    })
    # Keep actual one-use bootstrap-token persistence; signing is not this proof's boundary.
    monkeypatch.setattr(bootstrap, "_ca", lambda _config: (None, None))
    f.config.max_parallel_cloud_operations = 4
    f.config.listen_port = 8013
    f.config.reconcile_interval_seconds = 5
    probes = []

    class ReadinessPeer:
        async def request(self, **request):
            assert request["path"] == "/readyz"
            probes.append(request)
            return b"{}"

    controllers = []
    state = SimpleNamespace(f=f, provider=provider, api=api, probes=probes)

    async def restart():
        if controllers:
            assert await store.release_pool_lease(f.pool_id, state.controller.owner, state.fence)
            state.controller.executor.shutdown(wait=True)
        controller = ResourceController(_controller_config(), {}, owner="selected-" + uuid.uuid4().hex)
        controllers.append(controller)
        controller.config = f.config
        controller.providers = {f.pool.name: provider}
        controller.transport = ReadinessPeer()
        state.controller = controller
        claimed = await store.claim_pool_lease(f.pool_id, controller.owner, 300)
        assert claimed is not None
        state.pool, state.fence = claimed

    async def reconcile():
        await state.controller.reconcile_pool(state.pool, state.fence, f.pool)

    async def provision(run_id):
        await reconcile()
        run, _, resources, _ = await _state(f, run_id)
        resource = next(row for row in resources if row.id == run.assigned_resource_id)
        assert run.status == "waiting_resource"
        assert resource.observed_state == "booting" and resource.ready_at is None
        assert resource.provider_id.startswith("k8s:")
        assert resource.bootstrap_token_hash is not None
        assert len([key for key in api.objects if key[0] == "pods"]) == 1
        return resource

    async def ready(run_id):
        # Simulate the sandbox exchanging its bootstrap token for the pinned identity.
        async with f.factory() as session, session.begin():
            run = await session.get(ChatRun, run_id)
            resource = await session.get(ChatRuntimeResource, run.assigned_resource_id)
            resource.bootstrap_token_hash = None
            resource.certificate_fingerprint = "c" * 64
            resource.certificate_not_after = datetime.now(UTC) + timedelta(hours=1)
        await reconcile()
        run, _, resources, _ = await _state(f, run_id)
        resource = next(row for row in resources if row.id == run.assigned_resource_id)
        assert run.status == "queued" and resource.observed_state == "ready"
        assert resource.ready_at is not None
        assert probes[-1]["resource_id"] == resource.id
        assert probes[-1]["certificate_fingerprints"] == frozenset({"c" * 64})
        return resource

    state.restart, state.reconcile, state.provision, state.ready = restart, reconcile, provision, ready
    try:
        await restart()
        yield state
    finally:
        for controller in controllers:
            controller.executor.shutdown(wait=True)


@pytest.mark.parametrize("scope", ["temp", "persistent"])
@pytest.mark.parametrize("agent_budget", [None, _BUDGET], ids=["no-budget", "explicit-budget"])
async def test_no_selection_never_reserves_or_creates_sandbox(lazy_chat_sandbox, monkeypatch, scope, agent_budget):
    f = lazy_chat_sandbox
    run_id = await _new_run(f, scope=scope, selected=False)

    def unexpected_manifest_read(*_args, **_kwargs):
        raise AssertionError("empty selection must not read configured manifests")

    monkeypatch.setattr(sandbox_selection, "load_tool_manifest", unexpected_manifest_read)
    assert not await _apply(f, run_id, agent_budget=agent_budget)
    run, quota, resources, holds = await _state(f, run_id)
    assert run.status == "queued"
    assert run.assigned_resource_id is None and run.runtime_pool_id is None
    assert resources == [] and holds == []
    assert (quota.active_sandboxes, quota.sandbox_seconds_reserved) == (0, 0)
    await _running(f, run_id)
    with pytest.raises(sandbox_binding.SandboxDispatchError) as error:
        await sandbox_binding.ensure_sandbox_for_run(
            run_id, lease_owner="worker#1", fence=1, pool_name=f.pool.name, wait_timeout=0,
        )
    assert error.value.code == "sandbox_tool_unselected"
    _, quota, resources, holds = await _state(f, run_id)
    assert resources == [] and holds == []
    assert (quota.active_sandboxes, quota.sandbox_seconds_reserved) == (0, 0)


@pytest.mark.parametrize("scope", ["temp", "persistent"])
@pytest.mark.parametrize("agent_budget", [None, _BUDGET], ids=["no-budget", "explicit-budget"])
async def test_selected_admission_waits_then_reuses_one_ready_pod(
    selected_controller, monkeypatch, scope, agent_budget,
):
    c, f = selected_controller, selected_controller.f
    run_id = await _new_run(f, scope=scope)
    assert await _apply(f, run_id, agent_budget=agent_budget)
    run, quota, resources, holds = await _state(f, run_id)
    assert run.status == "waiting_resource" and len(resources) == 1
    [resource] = resources
    assert resource.id == run.assigned_resource_id
    assert resource.run_id == run_id and resource.backend == "kubernetes"
    assert resource.logical_project_id == f.project and resource.logical_user_id == f.project
    assert resource.image_ref == f.pool.image and resource.policy_digest == f.pool.digest()
    assert {hold.kind: hold.status for hold in holds} == {"sandbox_slot": "reserved", "sandbox_seconds": "reserved"}
    reserved_seconds = next(hold.amount for hold in holds if hold.kind == "sandbox_seconds")
    assert quota.active_sandboxes == 1 and quota.sandbox_seconds_reserved == reserved_seconds
    assert run.credit_ceiling is None if agent_budget is None else run.credit_ceiling == 1
    assert c.api.calls == []
    await c.provision(run_id)
    # A controller restart must adopt the durable intent and Pod, not submit a second create.
    await c.restart()
    ready = await c.ready(run_id)
    await _running(f, run_id)
    submissions = []

    async def daemon_request(_transport, target, **request):
        assert target.resource_id == ready.id and target.generation == ready.generation
        if request["path"] == "/v1/workspace":
            return b'{"workspace_revision":7}'
        assert request["path"] == "/v1/mcp/executions" and request["method"] == "POST"
        submissions.append((target, request["body"]))
        return json.dumps({"id": request["body"]["call_id"], "state": "succeeded"}).encode()

    monkeypatch.setattr(sandbox_binding, "InternalTransport", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(sandbox_binding, "_authorized_request", daemon_request)
    for ordinal, tool in enumerate(("sandbox_echo", "sandbox_status", "sandbox_echo")):
        result = await sandbox_binding.execute_mcp_in_sandbox(
            run_id=run_id, lease_owner="worker#1", fence=1, call_id=f"call-{ordinal}",
            profile="local", tool=tool, arguments={}, timeout_seconds=5, pool_name=f.pool.name,
        )
        assert result["state"] == "succeeded"
    assert [body["tool"] for _, body in submissions] == ["sandbox_echo", "sandbox_status", "sandbox_echo"]
    assert {target.resource_id for target, _ in submissions} == {ready.id}
    assert {body["workspace_revision"] for _, body in submissions} == {7}
    _, quota, resources, holds = await _state(f, run_id)
    assert [(row.id, row.provider_id) for row in resources] == [(ready.id, ready.provider_id)]
    assert len(holds) == 2 and quota.active_sandboxes == 1
    assert quota.sandbox_seconds_reserved == reserved_seconds
    pod_posts = [call for call in c.api.calls if call[0] == "POST" and call[1].endswith("/pods")]
    assert len(pod_posts) == 1
    async with f.factory() as session:
        operations = list((await session.scalars(select(ChatResourceOperation).where(
            ChatResourceOperation.resource_id == ready.id, ChatResourceOperation.action == "create",
        ))).all())
        assert len(operations) == 1 and operations[0].attempts == 1


@pytest.mark.parametrize("denial", ["slot", "seconds", "pool-full"])
async def test_selected_quota_denial_rolls_back_all_new_holds(lazy_chat_sandbox, denial):
    f = lazy_chat_sandbox
    first = await _new_run(f)
    assert await _apply(f, first, agent_budget=_BUDGET)
    second = await _new_run(f)
    async with f.factory() as session, session.begin():
        quota = await session.get(ChatProjectAgentQuota, f.project)
        if denial == "seconds":
            quota.max_active_sandboxes = 2
            quota.max_sandbox_seconds = 15
        elif denial == "pool-full":
            quota.max_active_sandboxes = 2
            row = await session.get(ChatRuntimePool, f.pool_id)
            row.max_replicas = 1
    with pytest.raises(DurableRunInputError) as error:
        await _apply(f, second, agent_budget=_BUDGET)
    assert str(error.value) == ("sandbox_pool_full" if denial == "pool-full" else "child_budget_exhausted")
    run, quota, resources, holds = await _state(f, second)
    assert run.status == "queued" and run.assigned_resource_id is None and run.runtime_pool_id is None
    assert run.credit_ceiling is None and run.sandbox_seconds_ceiling is None
    assert holds == [] and len(resources) == 1 and resources[0].run_id == first
    assert (quota.active_sandboxes, quota.sandbox_seconds_reserved) == (1, 15)
    async with f.factory() as session:
        assert list((await session.scalars(select(ChatResourceOperation.resource_id).where(
            ChatResourceOperation.resource_id == resources[0].id,
        ))).all()) == [resources[0].id]


@pytest.fixture
async def selected_delegation(lazy_chat_sandbox, monkeypatch, current_project_authority):
    """Keep agent lookup, child preparation and every reservation on the real SQL path."""
    f = lazy_chat_sandbox
    policy = default_execution_policy().model_copy(update={
        "can_delegate_read": True, "can_delegate_write": True,
        "max_children": 2, "max_parallel_children": 2,
    })
    async with f.factory() as session, session.begin():
        parent_agent = ChatAgent(owner_user_id=f.project, project_id=f.project, name="Selected parent", is_active=True)
        child_agent = ChatAgent(owner_user_id=f.project, project_id=f.project, name="Selected child", is_active=True)
        session.add_all([parent_agent, child_agent])
        await session.flush()
        parent_agent.delegable_agent_ids = [child_agent.id]
        parent_agent_id, child_agent_id = parent_agent.id, child_agent.id
    root_id = await _new_run(
        f, scope="persistent", agent_id=parent_agent_id, execution_mode="plan",
        request_payload=encrypt_chat_content(json.dumps({
            "features": {"memory": False, "tool_policy": {
                "enabled_sandbox_tools": ["sandbox_echo", "sandbox_status"],
            }},
            "required_scopes": ["native:runs:write", "native:agents:use", "native:tools:execute"],
        })),
    )
    async with f.factory() as session, session.begin():
        root = await session.get(ChatRun, root_id)
        root.capability_snapshot = {
            **root.capability_snapshot, "execution_policy": policy.model_dump(mode="json"),
        }
        quota = ChatProjectAgentQuota(
            project_id=f.project, max_active_children=2, max_active_sandboxes=3,
            max_sandbox_seconds=3600, max_credit_reservation=Decimal("2"),
        )
        session.add(quota)
    route = AsyncMock(return_value={
        "provider_id": 1, "model_id": 1, "provider_name": "synthetic", "model_name": "fixture-child",
        "config_version_hash": "d" * 64, "input_price_per_token": "0", "output_price_per_token": "0",
    })
    monkeypatch.setattr("lumen.services.providers.routing.resolve_model_snapshot", route)
    settings = SimpleNamespace(runtime_config=f.config, chat_credit_per_usd=Decimal("1"))
    monkeypatch.setattr(children, "get_settings", lambda: settings)
    monkeypatch.setattr(chat_admission, "get_settings", lambda: settings)
    try:
        yield SimpleNamespace(f=f, root_id=root_id, child_agent_id=child_agent_id, route=route)
    finally:
        async with f.factory() as session, session.begin():
            child_ids = list((await session.scalars(select(ChatRun.id).where(
                ChatRun.root_run_id == root_id,
            ))).all())
            resource_ids = select(ChatRuntimeResource.id).where(ChatRuntimeResource.run_id.in_(child_ids))
            await session.execute(delete(ChatResourceOperation).where(ChatResourceOperation.resource_id.in_(resource_ids)))
            await session.execute(delete(ChatRuntimeResource).where(ChatRuntimeResource.run_id.in_(child_ids)))
            await session.execute(delete(ChatAgentReservation).where(ChatAgentReservation.run_id.in_(child_ids)))
            await session.execute(delete(ChatRunEventRow).where(ChatRunEventRow.run_id.in_(child_ids)))
            group_ids = select(ChatDelegationGroup.id).where(ChatDelegationGroup.parent_run_id == root_id)
            await session.execute(delete(ChatDelegationCall).where(ChatDelegationCall.group_id.in_(group_ids)))
            await session.execute(delete(ChatDelegationGroup).where(ChatDelegationGroup.parent_run_id == root_id))
            await session.execute(delete(ChatRun).where(ChatRun.id.in_(child_ids)))
            root = await session.get(ChatRun, root_id)
            root.agent_id = None
            await session.flush()
            await session.execute(delete(ChatAgent).where(ChatAgent.project_id == f.project))


def _delegation_call(fixture, *, seconds, access="read"):
    return children.DelegationCall(
        call_id="selected-child", agent_id=fixture.child_agent_id, task="Inspect the selected workspace",
        access=access, credit_budget=Decimal("0.5"), sandbox_seconds=seconds,
    )


@pytest.mark.parametrize(("total_seconds", "wall_seconds"), [(60, 120), (120, 180)])
async def test_selected_plan_root_tools_leave_budget_for_read_child(
    selected_controller, selected_delegation, monkeypatch, total_seconds, wall_seconds,
):
    c, d = selected_controller, selected_delegation
    f, root_id = d.f, d.root_id
    budget = {"credit_ceiling": "1", "sandbox_seconds_ceiling": total_seconds, "wall_time_seconds": wall_seconds}
    started = datetime.now(UTC)
    assert await _apply(f, root_id, agent_budget=budget, execution_mode="plan")
    admitted = datetime.now(UTC)
    root, quota, [root_resource], holds = await _state(f, root_id)
    root_wall = root.deadline_at.replace(tzinfo=UTC)
    root_share = min(300, max(1, total_seconds // 2), f.pool.max_lifetime_seconds, wall_seconds)
    assert root.execution_mode == "plan" and root.wall_time_seconds == wall_seconds
    assert started + timedelta(seconds=wall_seconds - 1) <= root_wall <= admitted + timedelta(seconds=wall_seconds)
    assert root.sandbox_seconds_ceiling == total_seconds
    assert root.sandbox_seconds_reserved == root_share
    assert next(hold.amount for hold in holds if hold.kind == "sandbox_seconds") == root_share
    assert root_resource.deadline_at.replace(tzinfo=UTC) <= admitted + timedelta(seconds=root_share)
    assert root_resource.deadline_at.replace(tzinfo=UTC) < root_wall
    assert quota.active_sandboxes == 1
    await c.provision(root_id)
    ready = await c.ready(root_id)
    await _running(f, root_id)
    submissions = []

    async def daemon_request(_transport, target, **request):
        assert target.resource_id == ready.id
        if request["path"] == "/v1/workspace":
            return b'{"workspace_revision":7}'
        assert request["path"] == "/v1/mcp/executions"
        submissions.append(request["body"])
        return json.dumps({"id": request["body"]["call_id"], "state": "succeeded"}).encode()

    monkeypatch.setattr(sandbox_binding, "InternalTransport", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(sandbox_binding, "_authorized_request", daemon_request)
    result = await sandbox_binding.execute_mcp_in_sandbox(
        run_id=root_id, lease_owner="worker#1", fence=1, call_id="root-tool",
        profile="local", tool="sandbox_echo", arguments={}, timeout_seconds=5, pool_name=f.pool.name,
    )
    assert result["state"] == "succeeded" and len(submissions) == 1
    remaining_seconds = total_seconds - root_share
    assert remaining_seconds > 0
    before_child = datetime.now(UTC)
    created = await children.prepare_delegations(
        parent_run_id=root_id, owner="worker#1", model_segment_id="selected-turn",
        calls=[_delegation_call(d, seconds=remaining_seconds)],
    )
    after_child = datetime.now(UTC)
    [created_child] = created["children"]
    child_id = created_child["child_run_id"]
    child, quota, resources, child_holds = await _state(f, child_id)
    root, _, _, root_holds = await _state(f, root_id)
    child_resource = next(resource for resource in resources if resource.run_id == child_id)
    assert root.deadline_at.replace(tzinfo=UTC) == root_wall
    assert child.execution_mode == "plan" and child.status == "waiting_resource"
    assert child.capability_snapshot["sandbox_tools"] == root.capability_snapshot["sandbox_tools"]
    assert child.wall_time_seconds == wall_seconds
    assert child.deadline_at.replace(tzinfo=UTC) == root_wall
    expiry = child_resource.deadline_at.replace(tzinfo=UTC)
    resource_seconds = min(remaining_seconds, f.pool.max_lifetime_seconds)
    assert before_child + timedelta(seconds=resource_seconds - 1) <= expiry
    assert expiry <= min(after_child + timedelta(seconds=resource_seconds), root_wall)
    assert expiry < child.deadline_at.replace(tzinfo=UTC)
    assert child_resource.backend == "kubernetes" and child_resource.pool_id == f.pool_id
    assert child_resource.image_ref == f.pool.image and child_resource.policy_digest == f.pool.digest()
    assert {hold.kind for hold in child_holds} == {"child_slot", "sandbox_slot", "credit", "sandbox_seconds"}
    assert all(hold.status == "reserved" for hold in child_holds)
    assert next(hold.amount for hold in child_holds if hold.kind == "sandbox_seconds") == remaining_seconds
    assert len(root_holds) == 2 and all(hold.status == "reserved" for hold in root_holds)
    assert root.sandbox_seconds_reserved == total_seconds
    assert (quota.active_children, quota.active_sandboxes, quota.sandbox_seconds_reserved) == (1, 2, total_seconds)
    assert quota.credits_reserved == Decimal("0.5")
    d.route.assert_awaited_once()
    async with f.factory() as session:
        operations = list((await session.scalars(select(ChatResourceOperation).where(
            ChatResourceOperation.resource_id == child_resource.id,
        ))).all())
        assert len(operations) == 1 and operations[0].action == "create"
    await c.reconcile()
    child, _, resources, _ = await _state(f, child_id)
    child_resource = next(resource for resource in resources if resource.run_id == child_id)
    assert child.status == "waiting_resource" and child_resource.observed_state == "booting"
    assert len([key for key in c.api.objects if key[0] == "pods"]) == 2
    await c.ready(child_id)
    child, _, _, _ = await _state(f, child_id)
    assert child.status == "queued" and child.deadline_at.replace(tzinfo=UTC) == root_wall
    await _running(f, child_id)
    now = datetime.now(UTC).replace(microsecond=0)
    async with f.factory() as session, session.begin():
        child_resource = await session.get(ChatRuntimeResource, child_resource.id)
        child_resource.ready_at = now - timedelta(seconds=remaining_seconds + 300)
        child_resource.deadline_at = now - timedelta(seconds=300)
    await c.reconcile()
    await c.reconcile()
    await c.reconcile()
    child, _, resources, _ = await _state(f, child_id)
    child_resource = next(resource for resource in resources if resource.run_id == child_id)
    assert child.status == "running" and child.deadline_at.replace(tzinfo=UTC) == root_wall
    assert child_resource.observed_state == "deleted" and child_resource.deleted_at is not None
    assert child_resource.deadline_at < child_resource.deleted_at
    await _finish(child_id, status="completed", message_id=None, owner="worker#1")
    child, quota, _, child_holds = await _state(f, child_id)
    root, _, _, root_holds = await _state(f, root_id)
    seconds_hold = next(hold for hold in child_holds if hold.kind == "sandbox_seconds")
    assert child.status == "completed" and child.deadline_at.replace(tzinfo=UTC) == root_wall
    assert seconds_hold.status == "settled" and seconds_hold.settled_amount == remaining_seconds
    assert next(hold.status for hold in child_holds if hold.kind == "child_slot") == "released"
    assert next(hold.status for hold in child_holds if hold.kind == "sandbox_slot") == "released"
    assert quota.active_children == 0 and quota.active_sandboxes == 1
    assert root.status == "running" and root.deadline_at.replace(tzinfo=UTC) == root_wall
    assert all(hold.status == "reserved" for hold in root_holds)


@pytest.mark.parametrize("failure", ["stale-image", "stale-digest", "selected-write"])
async def test_selected_child_failure_preserves_root_holds_without_sql_leaks(
    selected_delegation, monkeypatch, failure,
):
    d = selected_delegation
    f, root_id = d.f, d.root_id
    budget = {"credit_ceiling": "1", "sandbox_seconds_ceiling": 60, "wall_time_seconds": 120}
    assert await _apply(f, root_id, agent_budget=budget, execution_mode="plan")
    async with f.factory() as session, session.begin():
        root = await session.get(ChatRun, root_id)
        root.status, root.lease_owner = "running", "worker#1"
        root.lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)
        row = await session.get(ChatRuntimePool, f.pool_id)
        if failure == "stale-image":
            row.image_ref = "registry.example/sandbox@sha256:" + "e" * 64
        elif failure == "stale-digest":
            row.profile_digest = "f" * 64
    root, quota, [resource], holds = await _state(f, root_id)
    root_state = (root.deadline_at, root.sandbox_seconds_reserved, root.descendant_credits_reserved)
    quota_state = (quota.active_children, quota.active_sandboxes, quota.credits_reserved, quota.sandbox_seconds_reserved)
    hold_state = {(hold.id, hold.kind, hold.amount, hold.status) for hold in holds}
    async with f.factory() as session:
        operation_ids = set((await session.scalars(select(ChatResourceOperation.id).where(
            ChatResourceOperation.resource_id == resource.id,
        ))).all())
    if failure == "selected-write":
        d.route.side_effect = AssertionError("selected write child must fail before route resolution")
        provider = AsyncMock(side_effect=AssertionError("selected write child must not contact a provider"))
        monkeypatch.setattr("lumen.services.provider_client.acompletion", provider)
        monkeypatch.setattr(budgets, "reserve", AsyncMock(
            side_effect=AssertionError("selected write child must fail before reservations"),
        ))
    with pytest.raises(children.ChildFailure) as error:
        await children.prepare_delegations(
            parent_run_id=root_id, owner="worker#1", model_segment_id="rejected-turn",
            calls=[_delegation_call(d, seconds=30, access="write" if failure == "selected-write" else "read")],
        )
    expected = "sandbox_code_combination_unsupported" if failure == "selected-write" else "sandbox_configuration_changed"
    assert error.value.code == expected
    root, quota, resources, holds = await _state(f, root_id)
    assert (root.deadline_at, root.sandbox_seconds_reserved, root.descendant_credits_reserved) == root_state
    assert (quota.active_children, quota.active_sandboxes, quota.credits_reserved, quota.sandbox_seconds_reserved) == quota_state
    assert {(hold.id, hold.kind, hold.amount, hold.status) for hold in holds} == hold_state
    assert [row.id for row in resources] == [resource.id]
    async with f.factory() as session:
        assert list((await session.scalars(select(ChatRun.id).where(ChatRun.parent_run_id == root_id))).all()) == []
        assert list((await session.scalars(select(ChatDelegationGroup.id).where(
            ChatDelegationGroup.parent_run_id == root_id,
        ))).all()) == []
        assert list((await session.scalars(select(ChatDelegationCall.id).where(
            ChatDelegationCall.parent_run_id == root_id,
        ))).all()) == []
        assert set((await session.scalars(select(ChatAgentReservation.id).where(
            ChatAgentReservation.root_run_id == root_id,
        ))).all()) == {hold.id for hold in holds}
        pool_resources = select(ChatRuntimeResource.id).where(ChatRuntimeResource.pool_id == f.pool_id)
        assert set((await session.scalars(select(ChatResourceOperation.id).where(
            ChatResourceOperation.resource_id.in_(pool_resources),
        ))).all()) == operation_ids
    if failure == "selected-write":
        d.route.assert_not_awaited()
        provider.assert_not_awaited()


@pytest.mark.parametrize("foreign_field", ["run_id", "logical_project_id", "logical_user_id"])
async def test_selected_dispatch_refuses_foreign_resource_adoption(lazy_chat_sandbox, foreign_field):
    f = lazy_chat_sandbox
    run_id = await _new_run(f)
    assert await _apply(f, run_id)
    foreign_value = await _new_run(f) if foreign_field == "run_id" else str(uuid.uuid4())
    async with f.factory() as session, session.begin():
        run = await session.get(ChatRun, run_id)
        # Seed only the queue transition; this test isolates dispatch ownership checks.
        run.status = "queued"
        resource = await session.get(ChatRuntimeResource, run.assigned_resource_id)
        setattr(resource, foreign_field, foreign_value)
        resource.observed_state = "ready"
        resource.address, resource.port, resource.certificate_fingerprint = "192.0.2.40", 8013, "c" * 64
    await _running(f, run_id)
    with pytest.raises(sandbox_binding.SandboxDispatchError) as error:
        await sandbox_binding.ensure_sandbox_for_run(
            run_id, lease_owner="worker#1", fence=1, pool_name=f.pool.name, wait_timeout=1,
        )
    assert error.value.code == "assigned_resource_missing"
    _, quota, resources, holds = await _state(f, run_id)
    assert len(resources) == 1 and len(holds) == 2 and quota.active_sandboxes == 1
    # Restore fixture ownership so cleanup remains scoped to its seeded run.
    async with f.factory() as session, session.begin():
        resource = await session.get(ChatRuntimeResource, resources[0].id)
        setattr(resource, foreign_field, run_id if foreign_field == "run_id" else f.project)


@pytest.mark.parametrize("drift", ["unrepinned-bytes", "repinned-manifest"])
@pytest.mark.parametrize("phase", ["admission", "dispatch"])
async def test_selected_manifest_drift_refuses_without_new_holds(lazy_chat_sandbox, drift, phase):
    f = lazy_chat_sandbox
    run_id = await _new_run(f)
    if phase == "dispatch":
        assert await _apply(f, run_id)
        # Controller readiness has separate coverage; isolate selection revalidation here.
        async with f.factory() as session, session.begin():
            run = await session.get(ChatRun, run_id)
            run.status = "queued"
        await _running(f, run_id)
    _, _, old_resources, old_holds = await _state(f, run_id)
    changed = f.manifest_file.read_bytes().replace(b"Fixture sandbox_echo", b"Changed sandbox_echo")
    f.manifest_file.write_bytes(changed)
    if drift == "repinned-manifest":
        profile = f.pool.profile.model_copy(update={"tool_manifest_sha256": hashlib.sha256(changed).hexdigest()})
        f.config.pools = (f.pool.model_copy(update={"profile": profile}),)
        f.config.pool = lambda _name: f.config.pools[0]
    expected = "sandbox_configuration_changed" if drift == "repinned-manifest" else "sandbox_tool_unavailable"
    if phase == "admission":
        with pytest.raises(DurableRunInputError, match=f"^{expected}$"):
            await _apply(f, run_id)
    else:
        with pytest.raises(sandbox_binding.SandboxDispatchError) as error:
            await sandbox_binding.ensure_sandbox_for_run(
                run_id, lease_owner="worker#1", fence=1, pool_name=f.pool.name, wait_timeout=1,
            )
        assert error.value.code == expected
    run, quota, resources, holds = await _state(f, run_id)
    assert [(row.id, row.desired_state) for row in resources] == [
        (row.id, row.desired_state) for row in old_resources
    ]
    assert [(row.id, row.status) for row in holds] == [(row.id, row.status) for row in old_holds]
    if phase == "admission":
        assert run.status == "queued" and run.assigned_resource_id is None and quota is None
    else:
        assert run.status == "running" and quota.active_sandboxes == 1


@pytest.mark.parametrize("outcome", ["completed", "failed", "canceled", "expired"])
async def test_selected_terminal_cleanup_survives_restart_and_releases_project_slot(selected_controller, outcome):
    c, f = selected_controller, selected_controller.f
    run_id = await _new_run(f)
    assert await _apply(f, run_id)
    provisioned = await c.provision(run_id)
    if outcome == "completed":
        await c.ready(run_id)
        await _running(f, run_id)
        await _finish(run_id, status="completed", message_id=None, owner="worker#1")
    elif outcome == "canceled":
        assert (await lifecycle.request_cancelled(run_id=run_id, project_id=f.project, user_id=f.project)).status == "canceled"
        assert (await lifecycle.request_cancelled(run_id=run_id, project_id=f.project, user_id=f.project)).status == "canceled"
    elif outcome == "failed":
        assert await lifecycle.fail_waiting_run(run_id, error_code="sandbox_unavailable", safe_message="Unavailable")
        assert not await lifecycle.fail_waiting_run(run_id, error_code="sandbox_unavailable", safe_message="Unavailable")
    else:
        async with f.factory() as session, session.begin():
            run = await session.get(ChatRun, run_id)
            resource = await session.get(ChatRuntimeResource, run.assigned_resource_id)
            run.deadline_at = resource.deadline_at = datetime.now(UTC) - timedelta(seconds=1)
        await c.reconcile()
    run, quota, resources, holds = await _state(f, run_id)
    assert run.status == ("failed" if outcome == "expired" else outcome)
    assert run.reservation_released_at is not None
    assert (quota.active_sandboxes, quota.sandbox_seconds_reserved, run.sandbox_seconds_reserved) == (0, 0, 0)
    assert {hold.kind: hold.status for hold in holds} == {"sandbox_slot": "released", "sandbox_seconds": "settled"}
    if outcome != "completed":
        assert next(hold.settled_amount for hold in holds if hold.kind == "sandbox_seconds") == 0
    [resource] = resources
    assert resource.id == provisioned.id and resource.desired_state == "deleting"
    assert await store.occupancy(f.pool_id) == 1
    # Deletion must come from persisted intent after takeover, not the old controller's memory.
    await c.restart()
    await c.reconcile()
    await c.reconcile()
    run, quota, resources, replayed_holds = await _state(f, run_id)
    assert resources[0].observed_state == "deleted" and await store.occupancy(f.pool_id) == 0
    assert {(hold.id, hold.status, hold.settled_amount) for hold in replayed_holds} == {
        (hold.id, hold.status, hold.settled_amount) for hold in holds
    }
    assert set(c.api.objects) == {("networkpolicies", "lumen-default-deny")}
    async with f.factory() as session:
        terminal_events = list((await session.scalars(select(ChatRunEventRow.event_type).where(
            ChatRunEventRow.run_id == run_id,
            ChatRunEventRow.event_type.in_(("run.completed", "run.failed", "run.canceled")),
        ))).all())
        assert len(terminal_events) == 1
    replacement = await _new_run(f)
    assert await _apply(f, replacement)
    replacement_run, quota, resources, replacement_holds = await _state(f, replacement)
    assert replacement_run.assigned_resource_id != provisioned.id
    assert quota.active_sandboxes == 1 and len(replacement_holds) == 2
    assert len(resources) == 2


async def test_selected_ready_resource_expires_without_shortening_root_wall(
    selected_controller, selected_delegation, monkeypatch,
):
    c, d = selected_controller, selected_delegation
    f, root_id = d.f, d.root_id
    budget = {"credit_ceiling": "1", "sandbox_seconds_ceiling": 600, "wall_time_seconds": 1200}
    assert await _apply(f, root_id, agent_budget=budget, execution_mode="plan")
    await c.provision(root_id)
    await c.ready(root_id)
    await _running(f, root_id)
    now = datetime.now(UTC).replace(microsecond=0)
    root_wall = now + timedelta(seconds=1200)
    async with f.factory() as session, session.begin():
        root = await session.get(ChatRun, root_id)
        resource = await session.get(ChatRuntimeResource, root.assigned_resource_id)
        root.deadline_at = root_wall
        resource.ready_at = now - timedelta(seconds=600)
        resource.deadline_at = now - timedelta(seconds=300)
        resource_id = resource.id
    await c.reconcile()
    root, _, [resource], holds = await _state(f, root_id)
    assert root.status == "running" and root.deadline_at.replace(tzinfo=UTC) == root_wall
    assert root.reservation_released_at is None
    assert resource.desired_state == "deleting"
    assert all(hold.status == "reserved" for hold in holds)
    async with f.factory() as session:
        deletes = list((await session.scalars(select(ChatResourceOperation).where(
            ChatResourceOperation.resource_id == resource_id, ChatResourceOperation.action == "delete",
        ))).all())
        assert len(deletes) == 1
        # Intent is persisted under the live pool fence; I/O claims it next cycle.
        assert deletes[0].owner == "__unclaimed__" and deletes[0].fence == 0 and deletes[0].attempts == 0
    await c.reconcile()
    await c.reconcile()
    root, _, [resource], _ = await _state(f, root_id)
    assert root.status == "running" and root.deadline_at.replace(tzinfo=UTC) == root_wall
    assert resource.observed_state == "deleted" and resource.deleted_at is not None
    assert set(c.api.objects) == {("networkpolicies", "lumen-default-deny")}
    assert await store.occupancy(f.pool_id) == 0
    async with f.factory() as session:
        delete = await session.scalar(select(ChatResourceOperation).where(
            ChatResourceOperation.resource_id == resource_id, ChatResourceOperation.action == "delete",
        ))
        assert delete.owner == c.controller.owner and delete.fence == c.fence
        assert delete.attempts == 1 and delete.request_status == "succeeded"
    _, _, _, holds = await _state(f, root_id)
    hold_ids = {hold.id for hold in holds}
    api_calls = list(c.api.calls)
    daemon = AsyncMock(side_effect=AssertionError("expired resource must not reach the sandbox daemon"))
    monkeypatch.setattr(sandbox_binding, "_authorized_request", daemon)
    async with f.factory() as session:
        operation_ids = set((await session.scalars(select(ChatResourceOperation.id).where(
            ChatResourceOperation.resource_id == resource_id,
        ))).all())
    with pytest.raises(sandbox_binding.SandboxDispatchError) as error:
        await asyncio.wait_for(sandbox_binding.ensure_sandbox_for_run(
            root_id, lease_owner="worker#1", fence=1, pool_name=f.pool.name, wait_timeout=0,
        ), timeout=2)
    assert error.value.code == "sandbox_expired"
    with pytest.raises(sandbox_binding.SandboxDispatchError) as error:
        await asyncio.wait_for(sandbox_binding.execute_mcp_in_sandbox(
            run_id=root_id, lease_owner="worker#1", fence=1, call_id="expired-root-tool",
            profile="local", tool="sandbox_echo", arguments={}, timeout_seconds=1, pool_name=f.pool.name,
        ), timeout=2)
    assert error.value.code == "sandbox_expired"
    with pytest.raises(children.ChildFailure) as child_error:
        await children.prepare_delegations(
            parent_run_id=root_id, owner="worker#1", model_segment_id="expired-root-turn",
            calls=[_delegation_call(d, seconds=300)],
        )
    assert child_error.value.code == "sandbox_expired"
    daemon.assert_not_awaited()
    assert c.api.calls == api_calls
    root, _, resources, holds = await _state(f, root_id)
    assert root.status == "running" and root.deadline_at.replace(tzinfo=UTC) == root_wall
    assert root.assigned_resource_id == resource_id and [row.id for row in resources] == [resource_id]
    assert {hold.id for hold in holds} == hold_ids and all(hold.status == "reserved" for hold in holds)
    async with f.factory() as session:
        assert list((await session.scalars(select(ChatRun.id).where(ChatRun.parent_run_id == root_id))).all()) == []
        assert list((await session.scalars(select(ChatDelegationGroup.id).where(
            ChatDelegationGroup.parent_run_id == root_id,
        ))).all()) == []
        assert set((await session.scalars(select(ChatAgentReservation.id).where(
            ChatAgentReservation.root_run_id == root_id,
        ))).all()) == hold_ids
        pool_resources = select(ChatRuntimeResource.id).where(ChatRuntimeResource.pool_id == f.pool_id)
        assert set((await session.scalars(select(ChatResourceOperation.id).where(
            ChatResourceOperation.resource_id.in_(pool_resources),
        ))).all()) == operation_ids
    await _finish(root_id, status="completed", message_id=None, owner="worker#1")
    root, quota, _, holds = await _state(f, root_id)
    assert root.status == "completed" and root.deadline_at.replace(tzinfo=UTC) == root_wall
    assert root.reservation_released_at is not None
    seconds_hold = next(hold for hold in holds if hold.kind == "sandbox_seconds")
    assert seconds_hold.amount == 300 and seconds_hold.status == "settled"
    assert seconds_hold.settled_amount == 300
    assert next(hold.status for hold in holds if hold.kind == "sandbox_slot") == "released"
    assert (quota.active_sandboxes, quota.sandbox_seconds_reserved) == (0, 0)


async def test_selected_auxiliary_cleanup_holds_occupancy_until_service_absence(selected_controller):
    c, f = selected_controller, selected_controller.f
    run_id = await _new_run(f)
    assert await _apply(f, run_id)
    resource = await c.provision(run_id)
    name = c.provider._name(resource.id, resource.generation)
    service_uid = c.api.objects[("services", name)]["metadata"]["uid"]
    async with f.factory() as session, session.begin():
        pool_row = await session.get(ChatRuntimePool, f.pool_id)
        pool_row.max_replicas = 1
    assert (await lifecycle.request_cancelled(
        run_id=run_id, project_id=f.project, user_id=f.project,
    )).status == "canceled"
    await c.restart()
    await c.reconcile()
    assert ("pods", name) not in c.api.objects
    c.api.delay_delete_kind = "services"
    await c.reconcile()
    _, quota, resources, _ = await _state(f, run_id)
    assert resources[0].desired_state == "deleting" and resources[0].observed_state != "deleted"
    assert await store.occupancy(f.pool_id) == 1 and quota.active_sandboxes == 0
    assert set(c.api.objects) == {("networkpolicies", "lumen-default-deny"), ("services", name)}
    replacement = await _new_run(f)
    with pytest.raises(DurableRunInputError, match="^sandbox_pool_full$"):
        await _apply(f, replacement)
    # A new controller retries only the UID-bound auxiliary cleanup, not Pod creation/deletion.
    c.api.delay_delete_kind = None
    await c.restart()
    await c.reconcile()
    _, _, resources, _ = await _state(f, run_id)
    assert resources[0].observed_state == "deleted" and await store.occupancy(f.pool_id) == 0
    assert set(c.api.objects) == {("networkpolicies", "lumen-default-deny")}
    pod_deletes = [call for call in c.api.calls if call[0] == "DELETE" and call[1].endswith("/pods/" + name)]
    service_deletes = [call for call in c.api.calls if call[0] == "DELETE" and call[1].endswith("/services/" + name)]
    assert len(pod_deletes) == 1 and len(service_deletes) == 2
    assert all(call[2]["preconditions"] == {"uid": service_uid} for call in service_deletes)
    async with f.factory() as session:
        operation = await session.scalar(select(ChatResourceOperation).where(
            ChatResourceOperation.resource_id == resource.id, ChatResourceOperation.action == "delete",
        ))
        assert operation.attempts == 1 and operation.request_status == "succeeded"
    assert await _apply(f, replacement)


@pytest.mark.parametrize("retained_service", [False, True], ids=["immediate-cleanup", "service-finalizer"])
async def test_selected_vanished_pod_cleans_auxiliaries_before_releasing_occupancy(
    selected_controller, retained_service,
):
    c, f = selected_controller, selected_controller.f
    run_id = await _new_run(f)
    assert await _apply(f, run_id)
    resource = await c.provision(run_id)
    name = c.provider._name(resource.id, resource.generation)
    # Model an externally vanished Pod; its independent auxiliaries still exist.
    del c.api.objects[("pods", name)]
    if retained_service:
        c.api.delay_delete_kind = "services"
    await c.restart()
    await c.reconcile()
    run, quota, resources, holds = await _state(f, run_id)
    assert run.status == "failed"
    assert quota.active_sandboxes == 0 and quota.sandbox_seconds_reserved == 0
    assert {hold.kind: hold.status for hold in holds} == {"sandbox_slot": "released", "sandbox_seconds": "settled"}
    if retained_service:
        assert resources[0].observed_state != "deleted" and await store.occupancy(f.pool_id) == 1
        assert set(c.api.objects) == {("networkpolicies", "lumen-default-deny"), ("services", name)}
        c.api.delay_delete_kind = None
        await c.restart()
        await c.reconcile()
        _, _, resources, _ = await _state(f, run_id)
    assert resources[0].observed_state == "deleted" and await store.occupancy(f.pool_id) == 0
    assert set(c.api.objects) == {("networkpolicies", "lumen-default-deny")}
    assert not any(call[0] == "DELETE" and call[1].endswith("/pods/" + name) for call in c.api.calls)


@pytest.mark.parametrize("boundary", ["delayed-readyz", "ready-commit-before-wake", "queued-before-first-claim"])
async def test_selected_readiness_expiry_cannot_queue_parked_run(selected_controller, monkeypatch, boundary):
    c, f = selected_controller, selected_controller.f
    run_id = await _new_run(f)
    assert await _apply(f, run_id)
    allocated = await c.provision(run_id)
    root_wall = datetime.now(UTC) + timedelta(minutes=20)
    async with f.factory() as session, session.begin():
        run = await session.get(ChatRun, run_id)
        resource = await session.get(ChatRuntimeResource, allocated.id)
        run.deadline_at = root_wall
        resource.bootstrap_token_hash = None
        resource.certificate_fingerprint = "c" * 64
        resource.certificate_not_after = datetime.now(UTC) + timedelta(hours=1)
    actual_wake = store.wake_ready_runs
    if boundary == "delayed-readyz":
        ready_outcomes = []
        actual_ready = store.mark_ready

        async def tracked_ready(*args, **kwargs):
            result = await actual_ready(*args, **kwargs)
            ready_outcomes.append(result)
            return result

        class DelayedReadiness:
            async def request(self, **request):
                assert request["path"] == "/readyz"
                # The request began before expiry; its successful response arrives after it.
                async with f.factory() as session, session.begin():
                    resource = await session.get(ChatRuntimeResource, allocated.id)
                    resource.deadline_at = datetime.now(UTC) - timedelta(seconds=1)
                return b"{}"

        monkeypatch.setattr(store, "mark_ready", tracked_ready)
        c.controller.transport = DelayedReadiness()
        await c.reconcile()
        assert ready_outcomes == [False]
    else:
        async def interrupted_wake(_resource_id):
            return []

        with monkeypatch.context() as patch:
            if boundary == "ready-commit-before-wake":
                patch.setattr(store, "wake_ready_runs", interrupted_wake)
            assert await store.mark_ready(
                allocated.id, allocated.generation, c.controller.owner, c.fence, policy_verified=True,
            )
        run, _, [resource], _ = await _state(f, run_id)
        assert run.status == ("queued" if boundary == "queued-before-first-claim" else "waiting_resource")
        assert run.lease_fence == 0 and resource.ready_at is not None
        async with f.factory() as session, session.begin():
            resource = await session.get(ChatRuntimeResource, allocated.id)
            resource.deadline_at = datetime.now(UTC) - timedelta(seconds=1)
        await c.restart()
        assert await actual_wake(allocated.id) == []
        await c.reconcile()
    run, quota, [resource], holds = await _state(f, run_id)
    assert run.status == "failed" and run.deadline_at.replace(tzinfo=UTC) == root_wall
    assert run.reservation_released_at is not None
    assert (quota.active_sandboxes, quota.sandbox_seconds_reserved) == (0, 0)
    assert resource.desired_state == "deleting"
    assert (resource.ready_at is None) == (boundary == "delayed-readyz")
    assert {hold.kind: hold.status for hold in holds} == {"sandbox_slot": "released", "sandbox_seconds": "settled"}
    async with f.factory() as session:
        events = list((await session.scalars(select(ChatRunEventRow).where(
            ChatRunEventRow.run_id == run_id,
        ))).all())
        decoded = [(event.event_type, json.loads(decrypt_chat_content(event.payload))) for event in events]
        queued = [payload for kind, payload in decoded
                  if kind == "run.stage.changed" and payload.get("stage") == "queued"]
        assert len(queued) == (1 if boundary == "queued-before-first-claim" else 0)
        failures = [payload for kind, payload in decoded if kind == "run.failed"]
        assert len(failures) == 1 and failures[0]["error_code"] == "resource_deadline_exceeded"
    await c.reconcile()
    await c.reconcile()
    run, _, [resource], _ = await _state(f, run_id)
    assert run.status == "failed" and resource.observed_state == "deleted"
    assert await actual_wake(allocated.id) == []
    assert set(c.api.objects) == {("networkpolicies", "lumen-default-deny")}
    assert len([call for call in c.api.calls if call[0] == "POST" and call[1].endswith("/pods")]) == 1


async def test_selected_expired_root_replays_settled_child_group_after_join_requeue(
    selected_controller, selected_delegation,
):
    c, d = selected_controller, selected_delegation
    f, root_id = d.f, d.root_id
    assert await _apply(f, root_id, agent_budget={
        "credit_ceiling": "1", "sandbox_seconds_ceiling": 120, "wall_time_seconds": 180,
    }, execution_mode="plan")
    await c.provision(root_id)
    root_resource = await c.ready(root_id)
    await _running(f, root_id)
    calls = [_delegation_call(d, seconds=60)]
    group = await children.prepare_delegations(
        parent_run_id=root_id, owner="worker#1", model_segment_id="durable-replayed-turn", calls=calls,
    )
    child_id = group["children"][0]["child_run_id"]
    await c.reconcile()
    await c.ready(child_id)
    await _running(f, child_id)
    assert await children.mark_waiting_children(
        parent_run_id=root_id, owner="worker#1", wait_group_id=group["wait_group_id"], checkpoint_id="saved-children",
    ) == "waiting_children"
    async with f.factory() as session, session.begin():
        resource = await session.get(ChatRuntimeResource, root_resource.id)
        resource.deadline_at = datetime.now(UTC) - timedelta(seconds=1)
    c.api.delay_delete_kind = "services"
    await c.reconcile()
    await c.reconcile()
    await c.reconcile()
    root, _, _, _ = await _state(f, root_id)
    assert root.status == "waiting_children"
    name = c.provider._name(root_resource.id, root_resource.generation)
    assert ("pods", name) not in c.api.objects and ("services", name) in c.api.objects
    # Child completion requeues already-started orchestration after the root Pod is gone.
    await _finish(child_id, status="completed", message_id=None, owner="worker#1")
    root, _, _, _ = await _state(f, root_id)
    assert root.status == "queued" and root.lease_fence == 1
    await c.reconcile()
    root, _, _, _ = await _state(f, root_id)
    assert root.status == "queued" and root.reservation_released_at is None
    c.api.delay_delete_kind = None
    await c.restart()
    await c.reconcile()
    await c.reconcile()
    root, _, resources, _ = await _state(f, root_id)
    assert root.status == "queued"
    assert next(resource for resource in resources if resource.id == root_resource.id).observed_state == "deleted"
    await _running(f, root_id)
    async with f.factory() as session:
        resource_ids = set((await session.scalars(select(ChatRuntimeResource.id).where(
            ChatRuntimeResource.pool_id == f.pool_id,
        ))).all())
        reservation_ids = set((await session.scalars(select(ChatAgentReservation.id).where(
            ChatAgentReservation.root_run_id == root_id,
        ))).all())
        operation_ids = set((await session.scalars(select(ChatResourceOperation.id).where(
            ChatResourceOperation.resource_id.in_(resource_ids),
        ))).all())
    native_calls = list(c.api.calls)
    replayed = await children.prepare_delegations(
        parent_run_id=root_id, owner="worker#1", model_segment_id="durable-replayed-turn", calls=calls,
    )
    assert replayed["wait_group_id"] == group["wait_group_id"]
    assert [item["child_run_id"] for item in replayed["children"]] == [child_id]
    results = await children.delegation_results(
        parent_run_id=root_id, owner="worker#1", wait_group_id=group["wait_group_id"],
    )
    assert [(item["child_run_id"], item["status"]) for item in results["results"]] == [(child_id, "completed")]
    joined = await children.prepare_delegations(
        parent_run_id=root_id, owner="worker#1", model_segment_id="durable-replayed-turn", calls=calls,
    )
    assert joined["state"] == "joined"
    assert await children.delegation_results(
        parent_run_id=root_id, owner="worker#1", wait_group_id=group["wait_group_id"],
    ) == results
    with pytest.raises(children.ChildFailure) as error:
        await children.prepare_delegations(
            parent_run_id=root_id, owner="worker#1", model_segment_id="new-group-after-expiry", calls=calls,
        )
    assert error.value.code == "sandbox_expired"
    assert c.api.calls == native_calls
    async with f.factory() as session:
        assert set((await session.scalars(select(ChatRuntimeResource.id).where(
            ChatRuntimeResource.pool_id == f.pool_id,
        ))).all()) == resource_ids
        assert set((await session.scalars(select(ChatAgentReservation.id).where(
            ChatAgentReservation.root_run_id == root_id,
        ))).all()) == reservation_ids
        assert set((await session.scalars(select(ChatResourceOperation.id).where(
            ChatResourceOperation.resource_id.in_(resource_ids),
        ))).all()) == operation_ids
        assert len(list((await session.scalars(select(ChatDelegationGroup.id).where(
            ChatDelegationGroup.parent_run_id == root_id,
        ))).all())) == 1
