"""Scheduling/dispatch contracts; SQL and Redis are synthetic in this module.

Real Redis TIME, Lua, SQL fences and spawned processes are covered separately in
integration/test_infrastructure_coordination_integration.py. Existing provider lifecycle
regressions remain in test_chat_infrastructure.py rather than being duplicated.
"""
from __future__ import annotations

import asyncio
import copy
import itertools
import threading
from collections import Counter
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from lumen.services.infrastructure import controller as controller_module
from lumen.services.infrastructure._coordination import PoolCoordinator, Snapshot, assign_pools
from lumen.services.infrastructure.controller import PoolLeaseLost, ResourceController


def _registry(count=3):
    return {f"p{index}": {"id": f"sql-{index}", "digest": f"digest-{index}"} for index in range(count)}


def _eligible(registry):
    return {name: target["digest"] for name, target in registry.items()}


def _assert_balanced(assignments, owners):
    loads = Counter(assignments.values())
    counts = [loads[owner] for owner in owners]
    assert max(counts) - min(counts) <= 1


@pytest.mark.parametrize("count", [3, 4, 5, 7, 12, 31])
def test_assignment_is_complete_stable_balanced_and_input_order_independent(count):
    registry = _registry(count)
    members = {owner: _eligible(registry) for owner in ("A", "B", "C")}
    before = copy.deepcopy((registry, members))
    expected = assign_pools(registry, members)
    assert set(expected) == set(registry)
    _assert_balanced(expected, members)
    for owners in itertools.permutations(members):
        reordered = {owner: dict(reversed(list(members[owner].items()))) for owner in owners}
        assert assign_pools(dict(reversed(list(registry.items()))), reordered) == expected
        assert assign_pools(registry, members) == expected
    assert (registry, members) == before


def test_assignment_requires_exact_pool_name_and_digest():
    registry = _registry(4)
    members = {
        "A": {"p0": "digest-0", "p1": "wrong", "not-registered": "digest-3"},
        "B": {"p0": "wrong", "p1": "digest-1", "p2": "wrong"},
        "C": {"p2": "digest-2", "p3": "wrong"},
        "empty": {},
    }
    assert assign_pools(registry, members) == {"p0": "A", "p1": "B", "p2": "C"}
    assert assign_pools(registry, {}) == {}
    assert assign_pools({}, members) == {}


def test_flexible_incumbents_do_not_strand_eligible_joiners():
    registry = {name: {"id": name, "digest": "d"} for name in ("p1", "p2", "p3")}
    members = {
        "A": {name: "d" for name in registry},
        "B": {"p1": "d"},
        "C": {"p2": "d", "p3": "d"},
    }
    assignments = assign_pools(registry, members)
    assert assignments["p1"] == "B"
    assert set(assignments) == set(registry)
    assert Counter(assignments.values()) == {"A": 1, "B": 1, "C": 1}


def test_all_three_pool_three_owner_eligibility_graphs_preserve_possible_full_balance():
    """Enumerate all 512 graphs, including those requiring alternating paths."""
    names, owners = ("p1", "p2", "p3"), ("A", "B", "C")
    registry = {name: {"id": name, "digest": "d"} for name in names}
    for mask in range(1 << 9):
        members = {
            owner: {name: "d" for j, name in enumerate(names) if mask & (1 << (i * 3 + j))}
            for i, owner in enumerate(owners)
        }
        assignments = assign_pools(registry, members)
        assert set(assignments) == {name for name in names if any(name in eligible for eligible in members.values())}
        assert all(members[owner].get(name) == "d" for name, owner in assignments.items())
        perfect_matching = any(
            all(name in members[owner] for name, owner in zip(names, ordering, strict=True))
            for ordering in itertools.permutations(owners)
        )
        if perfect_matching:
            assert Counter(assignments.values()) == {"A": 1, "B": 1, "C": 1}, (mask, assignments)
        assert assign_pools(dict(reversed(list(registry.items()))), dict(reversed(list(members.items())))) == assignments


def test_late_join_rebalances_without_permanently_pinning_existing_pools():
    registry = _registry(9)
    members = {"A": _eligible(registry)}
    assert set(assign_pools(registry, members).values()) == {"A"}
    members["B"] = _eligible(registry)
    two = assign_pools(registry, members)
    _assert_balanced(two, members)
    members["C"] = _eligible(registry)
    three = assign_pools(registry, members)
    assert Counter(three.values()) == {"A": 3, "B": 3, "C": 3}
    assert any(three[name] == "C" and two[name] != "C" for name in registry)
    del members["A"]
    after_leave = assign_pools(registry, members)
    assert set(after_leave) == set(registry)
    assert "A" not in after_leave.values()
    _assert_balanced(after_leave, members)


def test_coordinator_keys_are_deployment_scoped_and_share_cluster_hash_tag():
    first = PoolCoordinator("deployment-one", "A", {})
    second = PoolCoordinator("deployment-one", "B", {})
    foreign = PoolCoordinator("deployment-two", "A", {})
    assert first.keys == second.keys
    assert set(first.keys).isdisjoint(foreign.keys)
    assert len({key.split("{", 1)[1].split("}", 1)[0] for key in first.keys}) == 1


class CoordinatorSeam:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.heartbeat = AsyncMock(side_effect=self._current)
        self.current = AsyncMock(side_effect=self._current)
        self.publish_registry = AsyncMock(return_value=True)
        self.leave = AsyncMock()

    async def _current(self):
        return self.snapshot


@pytest.fixture
async def controller_factory():
    controllers = []

    def make(*, owner="A", registry=None, leader="A"):
        registry = _registry() if registry is None else registry
        definitions = tuple(
            SimpleNamespace(name=name, enabled=True, role="worker", digest=lambda digest=target["digest"]: digest)
            for name, target in registry.items()
        )
        config = SimpleNamespace(
            deployment_id="test-deployment", max_parallel_cloud_operations=4,
            reconcile_interval_seconds=60, pools=definitions, tls=None,
        )
        controller = ResourceController(config, dict.fromkeys(registry, object()), owner=owner)
        controller._coordinator = CoordinatorSeam(Snapshot(leader, registry, dict.fromkeys(registry, owner)))
        controllers.append(controller)
        return controller

    yield make
    for controller in controllers:
        controller.stop()
        tasks = list(controller._pool_tasks.values())
        if controller._registry_task is not None:
            tasks.append(controller._registry_task)
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        controller.executor.shutdown(wait=True)


async def test_tick_returns_while_registry_and_sdk_are_blocked_and_never_duplicates_pool_tasks(
    monkeypatch, controller_factory,
):
    controller = controller_factory()
    registry_started, registry_release = asyncio.Event(), asyncio.Event()
    sdk_started, sdk_release = threading.Event(), threading.Event()
    entered = []

    async def sync(config, *, leader_check):
        await leader_check()
        registry_started.set()
        await registry_release.wait()
        await leader_check()
        return []

    def slow_sdk():
        sdk_started.set()
        if not sdk_release.wait(10):
            raise TimeoutError("test did not release synthetic SDK")

    async def pool_work(definition):
        entered.append(definition.name)
        await controller._cloud(slow_sdk)

    monkeypatch.setattr(controller_module.store, "sync_pools", sync)
    monkeypatch.setattr(controller, "_tick_pool", pool_work)
    try:
        await asyncio.wait_for(controller.tick(), 1)
        await asyncio.wait_for(registry_started.wait(), 1)
        assert await asyncio.to_thread(sdk_started.wait, 1)
        first_tasks = dict(controller._pool_tasks)
        registry_task = controller._registry_task
        for _ in range(3):
            # Even an elapsed scheduling deadline cannot duplicate an active task.
            controller._pool_due.clear()
            controller._registry_due = 0
            await asyncio.wait_for(controller.tick(), 1)
        assert controller._pool_tasks == first_tasks
        assert controller._registry_task is registry_task
        assert sorted(entered) == sorted(_registry())
        assert controller._coordinator.heartbeat.await_count == 4
        assert all(not task.done() for task in first_tasks.values())
    finally:
        sdk_release.set()
        registry_release.set()
        await asyncio.gather(*controller._pool_tasks.values(), controller._registry_task)


async def test_tick_dispatches_only_assigned_pools_and_respects_completed_task_interval(monkeypatch, controller_factory):
    controller = controller_factory(leader="B")
    coordinator = controller._coordinator
    coordinator.snapshot = Snapshot("B", _registry(), {"p0": "A", "p1": "B"})
    work = AsyncMock()
    monkeypatch.setattr(controller, "_tick_pool", work)
    await controller.tick()
    await asyncio.gather(*controller._pool_tasks.values())
    assert controller._pool_ids == {"p0": "sql-0"}
    assert controller._registry_task is None
    assert [call.args[0].name for call in work.await_args_list] == ["p0"]
    await controller.tick()
    assert controller._pool_tasks == {}
    assert work.await_count == 1
    controller._pool_due["p0"] = 0
    await controller.tick()
    await asyncio.gather(*controller._pool_tasks.values())
    assert work.await_count == 2


async def test_only_leader_syncs_sql_and_publication_uses_enabled_definition_digest(monkeypatch, controller_factory):
    controller = controller_factory()
    registry = _registry()
    rows = [SimpleNamespace(name=name, id=target["id"], enabled=True) for name, target in registry.items()]
    rows += [SimpleNamespace(name="removed", id="foreign", enabled=True)]
    rows += [SimpleNamespace(name="disabled", id="disabled", enabled=False)]
    checks = []

    async def sync(config, *, leader_check):
        await leader_check()
        checks.append(config)
        return rows

    monkeypatch.setattr(controller_module.store, "sync_pools", sync)
    follower = controller_factory(owner="B", leader="A")
    await follower._sync_registry()
    assert checks == []
    follower._coordinator.publish_registry.assert_not_awaited()
    await controller._sync_registry()
    assert checks == [controller.config]
    controller._coordinator.publish_registry.assert_awaited_once_with(registry)


async def test_sql_leader_check_rejects_loss_during_sync_and_does_not_publish(monkeypatch, controller_factory):
    controller = controller_factory()
    coordinator = controller._coordinator
    reached = []

    async def sync(config, *, leader_check):
        await leader_check()
        reached.append("started")
        coordinator.snapshot = Snapshot("B", _registry(), {})
        await leader_check()
        raise AssertionError("lost leader must not continue SQL sync")

    monkeypatch.setattr(controller_module.store, "sync_pools", sync)
    await controller._sync_registry()
    assert reached == ["started"]
    coordinator.publish_registry.assert_not_awaited()


async def test_leader_transition_resets_registry_due_time(monkeypatch, controller_factory):
    controller = controller_factory(leader="B")
    monkeypatch.setattr(controller, "_tick_pool", AsyncMock())
    sync = AsyncMock()
    monkeypatch.setattr(controller, "_sync_registry", sync)
    await controller.tick()
    await asyncio.gather(*controller._pool_tasks.values())
    assert controller._registry_task is None
    controller._registry_due = float("inf")
    controller._coordinator.snapshot = Snapshot("A", _registry(), {})
    await controller.tick()
    await controller._registry_task
    sync.assert_awaited_once()


@pytest.mark.parametrize("failure", ["unassigned", "redis-down", "stopping"])
async def test_assignment_check_fails_closed_before_any_sdk_submission(controller_factory, failure):
    controller = controller_factory()
    if failure == "unassigned":
        controller._coordinator.snapshot = Snapshot("B", _registry(), {"p0": "B"})
    elif failure == "redis-down":
        controller._coordinator.current.side_effect = ConnectionError("synthetic Redis unavailable")
    else:
        controller.stop()
    calls = []
    token = controller_module._POOL.set("p0")
    try:
        with pytest.raises(PoolLeaseLost):
            await controller._cloud(lambda: calls.append("submitted"))
        with pytest.raises(PoolLeaseLost):
            await controller._renewed_effect(SimpleNamespace(), lambda: calls.append("write"))
        assert calls == []
    finally:
        controller_module._POOL.reset(token)


@pytest.mark.parametrize("action", ["create", "delete"])
@pytest.mark.parametrize("failure", ["unassigned", "redis-down", "cancel-before", "in-flight", "cancel-in-flight"])
async def test_claimed_operation_distinguishes_preflight_loss_from_started_sdk(
    monkeypatch, controller_factory, action, failure,
):
    controller = controller_factory()
    resource = SimpleNamespace(
        id="resource-1", generation=1, desired_state="requested" if action == "create" else "deleting",
        role="worker", ready_at=None, drain_requested_at=None, observed_state="unknown" if action == "create" else "booting",
        run_id=None,
    )
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session.begin.return_value = session
    monkeypatch.setattr(controller_module, "get_session_factory", lambda: lambda: session)
    monkeypatch.setattr(controller_module.store, "_locked_resource", AsyncMock(return_value=(resource, SimpleNamespace())))
    monkeypatch.setattr(controller_module.store, "_owns", lambda *_args: True)
    deferred, unknown = AsyncMock(return_value=True), AsyncMock(return_value=True)
    monkeypatch.setattr(controller_module.store, "defer_unsubmitted_operation", deferred)
    monkeypatch.setattr(controller_module.store, "mark_unknown", unknown)
    monkeypatch.setattr(controller, "_intent", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(controller, "_ref", lambda *_args: object())
    monkeypatch.setattr(controller, "_withdraw_ingress", AsyncMock(return_value=True))

    async def claim(*_args):
        if failure == "unassigned":
            controller._coordinator.snapshot = Snapshot("B", _registry(), {"p0": "B"})
        elif failure == "redis-down":
            controller._coordinator.current.side_effect = ConnectionError("synthetic Redis unavailable")
        elif failure == "cancel-before":
            controller._coordinator.current.side_effect = asyncio.CancelledError()
        return SimpleNamespace(id="operation-1")

    monkeypatch.setattr(controller_module.store, "claim_operation", claim)
    calls = []

    def effect(_request):
        calls.append(action)
        if failure == "cancel-in-flight":
            raise asyncio.CancelledError()
        raise TimeoutError("synthetic SDK response lost")

    provider = SimpleNamespace(requires_delivery=True, create=effect, delete=effect)
    token = controller_module._POOL.set("p0")
    try:
        expected = asyncio.CancelledError if failure.startswith("cancel-") else (
            TimeoutError if failure == "in-flight" else PoolLeaseLost)
        with pytest.raises(expected):
            await getattr(controller, "_reconcile_" + action)(resource, SimpleNamespace(name="p0"), 17, provider)
        if failure in {"in-flight", "cancel-in-flight"}:
            assert calls == [action]
            unknown.assert_awaited_once_with(resource.id, 1, "A", 17, action=action)
            deferred.assert_not_awaited()
        else:
            assert calls == []
            deferred.assert_awaited_once_with(resource.id, 1, action, "A", 17)
            unknown.assert_not_awaited()
    finally:
        controller_module._POOL.reset(token)


async def test_assignment_is_rechecked_after_waiting_for_cloud_limiter(controller_factory):
    controller = controller_factory()
    controller.limiter = asyncio.Semaphore(1)
    await controller.limiter.acquire()
    calls = []
    token = controller_module._POOL.set("p0")
    task = asyncio.create_task(controller._cloud(lambda: calls.append("submitted")))
    controller_module._POOL.reset(token)
    try:
        await asyncio.sleep(0)
        assert not task.done()
        controller._coordinator.snapshot = Snapshot("B", _registry(), {"p0": "B"})
        controller.limiter.release()
        with pytest.raises(PoolLeaseLost):
            await task
        assert calls == []
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("cancel", [False, True], ids=["assignment-loss", "cancelled-owner"])
async def test_current_effect_drains_before_sql_release_and_no_subsequent_cloud_step(
    monkeypatch, controller_factory, cancel,
):
    controller = controller_factory(registry=_registry(1))
    definition = controller.config.pools[0]
    controller._pool_ids = {"p0": "sql-0"}
    entered, release = threading.Event(), threading.Event()
    events = []
    pool = SimpleNamespace(id="sql-0", name="p0")
    monkeypatch.setattr(controller_module.store, "claim_pool_lease", AsyncMock(return_value=(pool, 17)))
    monkeypatch.setattr(controller_module.store, "pool_lease_owned", AsyncMock(return_value=True))

    async def release_sql(*args):
        assert release.is_set()
        events.append(("release", args))
        return True

    monkeypatch.setattr(controller_module.store, "release_pool_lease", release_sql)

    def sdk():
        events.append("effect-start")
        entered.set()
        if not release.wait(10):
            raise TimeoutError("test did not release synthetic SDK")
        events.append("effect-end")

    async def reconcile(*args):
        await controller._cloud(sdk)
        await controller._cloud(lambda: events.append("forbidden-next-step"))

    monkeypatch.setattr(controller, "reconcile_pool", reconcile)
    task = asyncio.create_task(controller._tick_pool(definition))
    controller._pool_tasks[definition.name] = task
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        controller._coordinator.snapshot = Snapshot("B", _registry(1), {"p0": "B"})
        if cancel:
            task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert events == ["effect-start"]
        release.set()
        result = (await asyncio.gather(task, return_exceptions=True))[0]
        assert isinstance(result, asyncio.CancelledError) if cancel else result is None
        assert events == ["effect-start", "effect-end", ("release", ("sql-0", "A", 17))]
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_joined_drains_sibling_after_failure_before_propagation():
    entered, release = asyncio.Event(), asyncio.Event()

    async def failed():
        await entered.wait()
        raise PoolLeaseLost()

    async def slow():
        entered.set()
        await release.wait()

    task = asyncio.create_task(ResourceController._joined(failed(), slow()))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(PoolLeaseLost):
            await task
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_run_shutdown_drains_work_before_leaving_redis(monkeypatch, controller_factory):
    controller = controller_factory(registry=_registry(1))
    started, release = asyncio.Event(), asyncio.Event()
    events = []

    async def work():
        started.set()
        await release.wait()
        events.append("drained")

    async def tick():
        if not controller._pool_tasks:
            controller._pool_tasks["p0"] = asyncio.create_task(work())

    async def leave():
        events.append("leave")

    controller._coordinator.leave.side_effect = leave
    monkeypatch.setattr(controller, "tick", tick)
    run = asyncio.create_task(controller.run())
    try:
        await asyncio.wait_for(started.wait(), 1)
        controller.stop()
        await asyncio.sleep(0)
        assert not run.done()
        assert events == []
        release.set()
        await asyncio.wait_for(run, 1)
        assert events == ["drained", "leave"]
    finally:
        release.set()
        controller.stop()
        await asyncio.gather(run, return_exceptions=True)


async def test_tick_redis_failure_never_falls_back_to_sql_discovery_or_claim(monkeypatch, controller_factory):
    controller = controller_factory()
    controller._coordinator.heartbeat.side_effect = ConnectionError("synthetic Redis outage")
    sync, claim = AsyncMock(), AsyncMock()
    monkeypatch.setattr(controller_module.store, "sync_pools", sync)
    monkeypatch.setattr(controller_module.store, "claim_pool_lease", claim)
    with pytest.raises(ConnectionError):
        await controller.tick()
    assert controller._pool_tasks == {} and controller._registry_task is None
    sync.assert_not_awaited()
    claim.assert_not_awaited()


async def test_tick_advertises_only_enabled_locally_available_pool_definitions(monkeypatch, controller_factory):
    controller = controller_factory()
    controller.config.pools[1].enabled = False
    del controller.providers["p2"]
    controller._coordinator = None
    constructed = []

    def coordinator(deployment, owner, eligible):
        constructed.append((deployment, owner, eligible))
        return CoordinatorSeam(Snapshot(None, {}, {}))

    monkeypatch.setattr(controller_module, "PoolCoordinator", coordinator)
    await controller.tick()
    assert constructed == [("test-deployment", "A", {"p0": "digest-0"})]
    assert controller._pool_tasks == {}


async def test_assigned_controller_with_sql_claim_refused_does_no_work_or_release(monkeypatch, controller_factory):
    controller = controller_factory(registry=_registry(1))
    controller._pool_ids = {"p0": "sql-0"}
    claim, reconcile, release = AsyncMock(return_value=None), AsyncMock(), AsyncMock()
    monkeypatch.setattr(controller_module.store, "claim_pool_lease", claim)
    monkeypatch.setattr(controller_module.store, "release_pool_lease", release)
    monkeypatch.setattr(controller, "reconcile_pool", reconcile)
    await controller._tick_pool(controller.config.pools[0])
    claim.assert_awaited_once_with("sql-0", "A", 30)
    reconcile.assert_not_awaited()
    release.assert_not_awaited()


async def test_redis_assignment_cannot_override_lost_sql_fence(monkeypatch, controller_factory):
    controller = controller_factory()
    owned = AsyncMock(return_value=False)
    monkeypatch.setattr(controller_module.store, "pool_lease_owned", owned)
    calls = []
    lease = controller_module._LEASE.set(("sql-0", 17))
    target = controller_module._POOL.set("p0")
    try:
        with pytest.raises(PoolLeaseLost):
            await controller._cloud(lambda: calls.append("forbidden"))
        owned.assert_awaited_once_with("sql-0", "A", 17)
        assert calls == []
    finally:
        controller_module._POOL.reset(target)
        controller_module._LEASE.reset(lease)


@pytest.mark.parametrize("method,args", [
    ("_reconcile_resource", (None, None, None, 1, None, None)),
    ("_reconcile_create", (None, None, 1, None)),
    ("_advance_readiness", (None, None, 1, None)),
    ("_advance_drain", (None, 1, None)),
    ("_scale", (None, 1, None, [])),
])
async def test_every_reconciliation_phase_refuses_lost_assignment_before_work(controller_factory, method, args):
    controller = controller_factory()
    controller._coordinator.snapshot = Snapshot("B", _registry(), {"p0": "B"})
    token = controller_module._POOL.set("p0")
    try:
        with pytest.raises(PoolLeaseLost):
            await getattr(controller, method)(*args)
    finally:
        controller_module._POOL.reset(token)


async def test_joined_cancellation_does_not_detach_sibling_work():
    entered, release = asyncio.Event(), asyncio.Event()
    events = []

    async def slow():
        entered.set()
        await release.wait()
        events.append("drained")

    task = asyncio.create_task(ResourceController._joined(slow()))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and events == []
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert events == ["drained"]
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
