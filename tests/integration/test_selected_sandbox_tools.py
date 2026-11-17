"""Real SQL selection admission with an in-process Kubernetes API, never a live cluster.

Provider qualification, bootstrap identity, and readyz below are synthetic. SQL
reservation, controller fencing, Kubernetes object construction, and dispatch target
lookup use their real implementations.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_infrastructure_store as infrastructure_fixtures
import test_kubernetes_provider as kubernetes_fixtures
from sqlalchemy import select

from lumen.models.chat_contracts import ToolPolicy
from lumen.models.chat_infrastructure import (
    ChatAgentReservation,
    ChatProjectAgentQuota,
    ChatResourceOperation,
    ChatRuntimePool,
    ChatRuntimeResource,
)
from lumen.models.chat_runs import ChatRun, ChatRunEventRow
from lumen.services import sandbox_binding, sandbox_selection
from lumen.services.durable_runs import admission, budgets, lifecycle
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


async def _apply(fixture, run_id, *, agent_budget=None):
    """Exercise the actual admission transaction without provider/model setup."""
    async with fixture.factory() as session, session.begin():
        order = budgets.LockOrder()
        quota = await budgets.lock_project_quota(session, fixture.project, order=order)
        run = await budgets.lock_run(session, run_id, order=order, lock_class="root")
        waiting = await admission._apply_root_budget(
            session, run, agent_budget=agent_budget, execution_mode="chat", quota=quota, order=order,
        )
        run.status = "waiting_resource" if waiting else "queued"
        return waiting


async def _new_run(fixture, *, scope="temp", selected=True, **values):
    policy = ToolPolicy(enabled_sandbox_tools=["sandbox_echo", "sandbox_status"] if selected else [])
    snapshot = {"sandbox_tools": sandbox_selection.freeze_selection(
        policy.enabled_sandbox_tools, config=fixture.config,
    )}
    return await fixture.create_run(
        run_scope=scope, status="queued", lease_owner=None, lease_expires_at=None,
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
            quota.max_sandbox_seconds = 30
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
    assert (quota.active_sandboxes, quota.sandbox_seconds_reserved) == (1, 30)
    async with f.factory() as session:
        assert list((await session.scalars(select(ChatResourceOperation.resource_id).where(
            ChatResourceOperation.resource_id == resources[0].id,
        ))).all()) == [resources[0].id]


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
