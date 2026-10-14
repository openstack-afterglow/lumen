"""Real Redis/MariaDB coordination regressions and a native spawned-process smoke.

Set REDIS_URL and DATABASE_URL to disposable, migrated native test services, or
use `uv run lumen-test integration`. Missing/unreachable services skip explicitly;
missing schema and product failures do not. No cloud, guest, or paid API is used.
The multiprocess test uses REAL SQL leases/operations and REAL Redis Lua; only the
provider and the narrowed reconcile_pool workload are synthetic. There is no
synthetic-SQL fallback that can turn an unavailable MariaDB into a passed smoke.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import multiprocessing
import os
import traceback
import uuid
from collections import Counter
from types import SimpleNamespace

import pytest
import test_infrastructure_store
from redis import Redis as SyncRedis
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import delete, event, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import create_async_engine

from lumen.db import close_db, get_session_factory, init_db
from lumen.models.chat_infrastructure import ChatResourceOperation, ChatRuntimePool, ChatRuntimeResource
from lumen.services.infrastructure import _coordination, store
from lumen.services.infrastructure._coordination import PoolCoordinator
from lumen.services.infrastructure.config import PoolConfig
from lumen.services.infrastructure.controller import PoolLeaseLost, ResourceController
from lumen.services.infrastructure.providers import Observation

pytestmark = pytest.mark.integration
pool = test_infrastructure_store.pool  # reuse native ON/OFF SQL fixture


@pytest.fixture(autouse=True)
async def _require_native_sql_when_requested(request):
    """Run before the imported fixture, which intentionally requires DATABASE_URL."""
    if "pool" not in request.fixturenames:
        return
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("real MariaDB unavailable: set DATABASE_URL to a disposable migrated native test schema")
    engine = create_async_engine(url, pool_size=1, max_overflow=0, connect_args={"connect_timeout": 3})
    try:
        try:
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        except (OperationalError, OSError) as exc:
            pytest.skip(f"real MariaDB unreachable ({type(exc).__name__}); no synthetic SQL fallback")
    finally:
        await engine.dispose()


@pytest.fixture
async def real_redis(monkeypatch):
    url = os.environ.get("REDIS_URL")
    if not url:
        pytest.skip("real Redis unavailable: set REDIS_URL; fakeredis cannot prove Lua Redis TIME")
    client = Redis.from_url(url, socket_connect_timeout=3, socket_timeout=3)
    try:
        try:
            await client.ping()
        except (RedisError, OSError) as exc:
            pytest.skip(f"real Redis unreachable ({type(exc).__name__}); no fakeredis fallback")
        monkeypatch.setattr("lumen.cache._client", client)
        monkeypatch.setattr(_coordination, "_get_redis", lambda: _redis_value(client))
        yield client
    finally:
        await client.close()


async def _redis_value(client):
    return client


@pytest.fixture
async def coordinators(real_redis):
    created = []
    deployment = "coordination-test-" + uuid.uuid4().hex

    def make(owner, eligible):
        coordinator = PoolCoordinator(deployment, owner, eligible)
        created.append(coordinator)
        return coordinator

    yield make
    if created:
        await real_redis.delete(*created[0].keys)


def _registry(count=3):
    return {f"p{index}": {"id": f"sql-{index}", "digest": f"d{index}"} for index in range(count)}


def _eligibility(registry):
    return {name: target["digest"] for name, target in registry.items()}


async def _redis_now(client):
    seconds, micros = await client.time()
    return seconds + micros / 1_000_000


async def test_redis_time_expiration_is_atomic_and_current_never_resurrects_owner(real_redis, coordinators, monkeypatch):
    registry = _registry()
    first, second = (coordinators(owner, _eligibility(registry)) for owner in ("A", "B"))
    before = await _redis_now(real_redis)
    await first.heartbeat()
    after = await _redis_now(real_redis)
    deadline = await real_redis.zscore(first.keys[0], "A")
    assert before + _coordination.PARTICIPATION_SECONDS <= deadline <= after + _coordination.PARTICIPATION_SECONDS
    await second.heartbeat()
    assert await first.publish_registry(registry)
    # Accelerate just this test-owned score, relative to Redis TIME, not Python's clock.
    await real_redis.zadd(first.keys[0], {"A": await _redis_now(real_redis) - 1})
    expired = await first.current()
    assert expired.leader == "B"
    assert set(expired.assignments.values()) == {"B"}
    assert await real_redis.zscore(first.keys[0], "A") is None
    assert await real_redis.hget(first.keys[1], "A") is None
    assert not await first.publish_registry({})
    assert (await second.current()).registry == registry
    assert (await first.heartbeat()).leader == "A"


async def test_only_lexical_live_leader_publishes_and_registry_survives_all_members_leaving(real_redis, coordinators):
    registry = _registry(7)
    members = [coordinators(owner, _eligibility(registry)) for owner in ("C", "B", "A")]
    for member in members:
        await member.heartbeat()
    assert not await members[0].publish_registry({"untrusted": {"id": "x", "digest": "x"}})
    assert await members[2].publish_registry(registry)
    snapshots = await asyncio.gather(*(member.current() for member in members))
    assert all(snapshot.leader == "A" and snapshot.registry == registry for snapshot in snapshots)
    assert all(snapshot.assignments == snapshots[0].assignments for snapshot in snapshots)
    counts = Counter(snapshots[0].assignments.values())
    assert max(counts.values()) - min(counts.values()) <= 1
    await members[2].leave()
    assert (await members[1].current()).leader == "B"
    assert not await members[2].publish_registry({})
    await members[1].leave()
    await members[0].leave()
    empty = await members[0].current()
    assert empty.leader is None and empty.assignments == {} and empty.registry == registry
    assert await real_redis.ttl(members[0].keys[2]) == -1


async def test_late_join_and_digest_changes_reassign_only_eligible_members(real_redis, coordinators):
    registry = _registry(9)
    first = coordinators("A", _eligibility(registry))
    second = coordinators("B", _eligibility(registry))
    third = coordinators("C", _eligibility(registry))
    await first.heartbeat()
    assert await first.publish_registry(registry)
    assert set((await first.current()).assignments.values()) == {"A"}
    await second.heartbeat()
    before = (await second.current()).assignments
    assert set(before.values()) == {"A", "B"}
    joined = await third.heartbeat()
    assert Counter(joined.assignments.values()) == {"A": 3, "B": 3, "C": 3}
    third.eligible = {name: "incompatible" for name in registry}
    changed = await third.heartbeat()
    assert "C" not in changed.assignments.values()
    assert set(changed.assignments) == set(registry)
    first.eligible = {}
    await first.heartbeat()
    assert set((await second.current()).assignments.values()) == {"B"}


async def test_orphan_eligibility_and_participant_without_hash_are_neither_leader_nor_assignee(real_redis, coordinators):
    registry = _registry()
    member = coordinators("B", _eligibility(registry))
    await member.heartbeat()
    assert await member.publish_registry(registry)
    await real_redis.hset(member.keys[1], "A-orphan", json.dumps(_eligibility(registry)))
    await real_redis.zadd(member.keys[0], {"A-no-hash": await _redis_now(real_redis) + 30})
    snapshot = await member.current()
    assert snapshot.leader == "B"
    assert set(snapshot.assignments.values()) == {"B"}


async def test_deployments_do_not_share_participation_or_registry(real_redis, coordinators):
    registry = _registry()
    first = coordinators("A", _eligibility(registry))
    foreign = PoolCoordinator("foreign-" + uuid.uuid4().hex, "B", _eligibility(registry))
    try:
        await first.heartbeat()
        await foreign.heartbeat()
        assert await first.publish_registry(registry)
        snapshot = await foreign.current()
        assert snapshot.leader == "B" and snapshot.registry == {} and snapshot.assignments == {}
    finally:
        await real_redis.delete(*foreign.keys)


async def test_sql_release_is_exact_owner_and_fence_cas_and_preserves_fence_monotonicity(pool):
    _, first = await store.claim_pool_lease(pool.id, "A", 30)
    assert not await store.release_pool_lease(pool.id, "B", first)
    assert not await store.release_pool_lease(pool.id, "A", first + 1)
    assert await store.pool_lease_owned(pool.id, "A", first)
    # Same incarnation can obtain a newer claim; the old claim cannot clear it.
    _, second = await store.claim_pool_lease(pool.id, "A", 30)
    assert second > first
    assert not await store.release_pool_lease(pool.id, "A", first)
    assert await store.pool_lease_owned(pool.id, "A", second)
    assert await store.release_pool_lease(pool.id, "A", second)
    assert not await store.release_pool_lease(pool.id, "A", second)
    _, third = await store.claim_pool_lease(pool.id, "B", 30)
    assert third > second
    assert not await store.release_pool_lease(pool.id, "A", second)
    assert await store.pool_lease_owned(pool.id, "B", third)
    assert not await store.release_pool_lease(str(uuid.uuid4()), "A", first)


async def test_redis_crash_takeover_refuses_sql_claim_until_sql_expiry(pool, real_redis):
    registry = {pool.name: {"id": pool.id, "digest": pool.profile_digest}}
    first = PoolCoordinator(pool.deployment_id, "A", _eligibility(registry))
    second = PoolCoordinator(pool.deployment_id, "B", _eligibility(registry))
    try:
        await first.heartbeat()
        assert await first.publish_registry(registry)
        _, old_fence = await store.claim_pool_lease(pool.id, "A", 30)
        await second.heartbeat()
        # Simulate a crashed incarnation: no leave/release, only Redis membership expires.
        await real_redis.zadd(first.keys[0], {"A": await _redis_now(real_redis) - 1})
        assert (await second.current()).assignments == {pool.name: "B"}
        assert await store.claim_pool_lease(pool.id, "B", 30) is None
        assert await store.pool_lease_owned(pool.id, "A", old_fence)
        # Advance only this owned SQL fixture deadline; avoid a 30-second wall-clock sleep.
        async with get_session_factory()() as session, session.begin():
            await session.execute(text(
                "UPDATE chat_runtime_pools SET reconcile_lease_expires_at = UTC_TIMESTAMP() - INTERVAL 1 SECOND "
                "WHERE id = :id"
            ), {"id": pool.id})
        _, new_fence = await store.claim_pool_lease(pool.id, "B", 30)
        assert new_fence > old_fence
        assert not await store.pool_lease_owned(pool.id, "A", old_fence)
        assert not await store.renew_pool_lease(pool.id, "A", old_fence, 30)
        assert not await store.release_pool_lease(pool.id, "A", old_fence)
        assert await store.pool_lease_owned(pool.id, "B", new_fence)
    finally:
        await real_redis.delete(*first.keys)


def _runtime_config(deployment_id, names=("workers", "media", "batch")):
    definitions = tuple(PoolConfig.model_validate({
        "name": name, "enabled": True, "role": "worker", "backend": "nova",
        "cloud_profile_id": "trusted", "image": "synthetic-worker-image", "architecture": "x86_64",
        "network_id": "synthetic-network", "security_group_ids": ("synthetic-group",),
        "min_replicas": 1, "max_replicas": 4, "workload_class": workload,
        "guest_profile_id": "synthetic-worker", "db_connection_budget": 120, "pg_connection_budget": 0,
        "profile": {"backend": "nova", "flavor_id": "synthetic-flavor", "guest_image_id": "synthetic-image",
                    "guest_image_hash": "a" * 64},
    }) for name, workload in zip(names, ("online_text", "online_media", "batch"), strict=False))
    # Only a typed pool/SQL/controller seam, not a claim of full runtime preflight.
    return SimpleNamespace(
        deployment_id=deployment_id, max_parallel_cloud_operations=4, pools=definitions,
        reconcile_interval_seconds=0.01, tls=None, guest_profiles=(),
        profile=lambda _: SimpleNamespace(project_id="operator-project", region_name="RegionOne"),
    )


@pytest.mark.parametrize("lost_at", [1, 2, 3], ids=["before-discovery", "after-row-lock", "before-commit"])
async def test_sql_registry_leader_checks_rollback_every_write_boundary(pool, lost_at):
    original = pool.image_ref
    config = _runtime_config(pool.deployment_id, (pool.name,))
    checks = 0

    async def leader_check():
        nonlocal checks
        checks += 1
        if checks == lost_at:
            raise PoolLeaseLost()

    with pytest.raises(PoolLeaseLost):
        await store.sync_pools(config, leader_check=leader_check)
    assert checks == lost_at
    async with get_session_factory()() as session:
        row = await session.get(ChatRuntimePool, pool.id)
        assert row.image_ref == original
        assert row.enabled


async def test_sql_registry_rechecks_leader_after_actual_row_lock_wait(pool):
    factory = get_session_factory()
    config = _runtime_config(pool.deployment_id, (pool.name,))
    entered = asyncio.Event()
    lost = False
    checks = 0

    async def leader_check():
        nonlocal checks
        checks += 1
        entered.set()
        if lost:
            raise PoolLeaseLost()

    task = None
    try:
        async with factory() as session, session.begin():
            await session.execute(select(ChatRuntimePool).where(ChatRuntimePool.id == pool.id).with_for_update())
            task = asyncio.create_task(store.sync_pools(config, leader_check=leader_check))
            await asyncio.wait_for(entered.wait(), 3)
            await asyncio.sleep(0.05)
            assert not task.done()
            assert checks == 1
            lost = True
        with pytest.raises(PoolLeaseLost):
            await asyncio.wait_for(task, 5)
        assert checks == 2
        async with factory() as session:
            assert (await session.get(ChatRuntimePool, pool.id)).image_ref == pool.image_ref
    finally:
        if task is not None:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_sql_release_waits_for_in_flight_fenced_sdk_effect(pool):
    import threading

    _, fence = await store.claim_pool_lease(pool.id, "A", 30)
    resource = await store.request_resource(
        pool, role="worker", request_fingerprint="e" * 64,
        image_ref=pool.image_ref, policy_digest=pool.profile_digest, deadline=None,
    )
    controller = ResourceController(_runtime_config(pool.deployment_id, (pool.name,)), {}, owner="A")
    entered, release = threading.Event(), threading.Event()
    events = []

    def effect():
        entered.set()
        if not release.wait(10):
            raise TimeoutError("test did not release synthetic provider")
        events.append("settled")

    work = asyncio.create_task(controller._fenced_cloud(resource, fence, effect))
    releasing = None
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        releasing = asyncio.create_task(store.release_pool_lease(pool.id, "A", fence))
        await asyncio.sleep(0.05)
        assert not releasing.done()
        assert events == []
        release.set()
        await asyncio.wait_for(work, 5)
        assert await asyncio.wait_for(releasing, 5)
        assert events == ["settled"]
        _, new_fence = await store.claim_pool_lease(pool.id, "B", 30)
        assert new_fence > fence
    finally:
        release.set()
        await asyncio.gather(work, *([releasing] if releasing is not None else []), return_exceptions=True)
        controller.executor.shutdown(wait=True)


class _SyntheticProvider:
    """No cloud connection: creates are recorded in the test-owned Redis namespace."""
    requires_delivery = True

    def __init__(self, redis_url, namespace, deployment, owner):
        self.redis = SyncRedis.from_url(redis_url, socket_connect_timeout=3, socket_timeout=3)
        self.namespace, self.deployment, self.owner = namespace, deployment, owner

    def create(self, intent):
        self.redis.hincrby(self.namespace + ":creates", intent.pool_id, 1)
        self.redis.hset(self.namespace + ":creators", intent.pool_id, self.owner)
        if intent.pool_id == "batch":
            # Accepted-but-response-lost must not produce a repeated create on tick 2.
            raise TimeoutError("synthetic accepted create response lost")
        return Observation("synthetic-" + intent.resource_id, "active", None, None, {
            "lumen_deployment": self.deployment, "lumen_pool": intent.pool_id,
            "lumen_resource": intent.resource_id, "lumen_generation": str(intent.generation),
            "lumen_fingerprint": intent.request_fingerprint, "lumen_policy": intent.policy_digest,
        })

    def observe(self, ref):
        raise AssertionError("the narrowed smoke never observes an already-recorded create")

    def list_owned(self, pool_name):
        return []


def _controller_process(pipe, database_url, redis_url, deployment, namespace, owner, snapshot_mode):
    """Spawn target: imports native code in a fresh interpreter, never forks DB pools."""
    async def run():
        redis = Redis.from_url(redis_url, socket_connect_timeout=3, socket_timeout=3)
        provider = _SyntheticProvider(redis_url, namespace, deployment, owner)
        init_db(database_url, pool_size=3, max_overflow=0)
        engine = get_session_factory().kw["bind"]

        def pin_snapshot(connection, _record):
            cursor = connection.cursor()
            cursor.execute(f"SET SESSION innodb_snapshot_isolation={snapshot_mode}")
            cursor.close()

        event.listen(engine.sync_engine, "connect", pin_snapshot)
        config = _runtime_config(deployment)
        controller = ResourceController(config, {definition.name: provider for definition in config.pools}, owner=owner)
        controller._coordinator = PoolCoordinator(deployment, owner, {
            definition.name: definition.digest() for definition in config.pools
        })
        # Assign the real Redis connection only in this child interpreter.
        original_redis = _coordination._get_redis
        _coordination._get_redis = lambda: _redis_value(redis)

        async def reconcile(current_pool, fence, definition):
            # Real SQL claims, operation journal, fenced SDK thread and observation
            # writes. Readiness/scaling/guests are intentionally outside this smoke.
            rows = await controller._resources(current_pool.id)
            for resource in rows:
                if resource.observed_state in {"requested", "unknown"} and not resource.provider_id:
                    owned = await controller._cloud(provider.list_owned, definition.name)
                    await controller._reconcile_resource(resource, current_pool, definition, fence, provider, owned)

        controller.reconcile_pool = reconcile
        try:
            await controller._coordinator.heartbeat()
            pipe.send({"ready": owner, "pid": os.getpid()})
            while True:
                command = await asyncio.to_thread(pipe.recv)
                if command == "stop":
                    break
                if command != "tick":
                    raise AssertionError(f"unknown smoke command: {command}")
                controller._pool_due.clear()
                controller._registry_due = 0
                # Exercise unknown recovery, not merely its short retry backoff.
                controller._retry.clear()
                await controller.tick()
                assigned = sorted(controller._pool_ids)
                await controller._joined(*controller._pool_tasks.values(), *(
                    [controller._registry_task] if controller._registry_task is not None else []))
                pipe.send({"owner": owner, "assigned": assigned})
        finally:
            controller.stop()
            await controller._joined(*controller._pool_tasks.values(), *(
                [controller._registry_task] if controller._registry_task is not None else []))
            await controller._coordinator.leave()
            controller.executor.shutdown(wait=True)
            event.remove(engine.sync_engine, "connect", pin_snapshot)
            _coordination._get_redis = original_redis
            provider.redis.close()
            await redis.close()
            await close_db()

    try:
        asyncio.run(run())
    except BaseException:
        with contextlib.suppress(BrokenPipeError, EOFError, OSError):
            pipe.send({"error": traceback.format_exc()})
        raise
    finally:
        pipe.close()


async def _receive(pipe, process):
    if not await asyncio.to_thread(pipe.poll, 30):
        pytest.fail(f"controller process {process.pid} did not respond; exitcode={process.exitcode}")
    try:
        result = pipe.recv()
    except EOFError:
        pytest.fail(f"controller process {process.pid} closed its pipe; exitcode={process.exitcode}")
    assert "error" not in result, result.get("error")
    return result


async def test_real_redis_real_sql_multiprocess_synthetic_provider_smoke(pool, real_redis, request):
    """Three processes, three pools, SQL fences, exactly one CREATE including unknown."""
    config = _runtime_config(pool.deployment_id)
    namespace = "lumen:coordination-smoke:" + uuid.uuid4().hex
    coordinator = PoolCoordinator(pool.deployment_id, "A", {})
    factory = get_session_factory()
    processes, pipes, extra_pool_ids = [], [], []
    context = multiprocessing.get_context("spawn")
    resource_ids = []

    async def fixture_leader_check():
        # Test setup only: controller children use real current-leader checks.
        return None

    try:
        rows = await store.sync_pools(config, leader_check=fixture_leader_check)
        extra_pool_ids = [row.id for row in rows if row.id != pool.id]
        registry = {}
        for row, definition in zip(rows, config.pools, strict=True):
            registry[row.name] = {"id": row.id, "digest": definition.digest()}
            resource = await store.request_resource(
                row, role="worker", request_fingerprint=uuid.uuid4().hex * 2,
                image_ref=row.image_ref, policy_digest=row.profile_digest, deadline=None,
            )
            resource_ids.append(resource.id)
        for owner in ("A", "B", "C"):
            parent, child = context.Pipe()
            process = context.Process(target=_controller_process, args=(
                child, os.environ["DATABASE_URL"], os.environ["REDIS_URL"], pool.deployment_id,
                namespace, owner, request.node.callspec.params["pool"],
            ))
            process.start()
            child.close()
            processes.append(process)
            pipes.append(parent)
        ready = await asyncio.gather(*(_receive(pipe, process) for pipe, process in zip(pipes, processes, strict=True)))
        assert {result["ready"] for result in ready} == {"A", "B", "C"}
        assert len({result["pid"] for result in ready}) == 3
        assert await coordinator.publish_registry(registry)
        snapshot = await coordinator.current()
        assert Counter(snapshot.assignments.values()) == {"A": 1, "B": 1, "C": 1}
        for _ in range(2):
            for pipe in pipes:
                pipe.send("tick")
            results = await asyncio.gather(*(
                _receive(pipe, process) for pipe, process in zip(pipes, processes, strict=True)
            ))
            assert all(len(result["assigned"]) == 1 for result in results)
            assert {name: result["owner"] for result in results for name in result["assigned"]} == snapshot.assignments
        counts = await real_redis.hgetall(namespace + ":creates")
        assert {name.decode(): int(value) for name, value in counts.items()} == {"workers": 1, "media": 1, "batch": 1}
        creators = await real_redis.hgetall(namespace + ":creators")
        assert {name.decode(): owner.decode() for name, owner in creators.items()} == snapshot.assignments
        async with factory() as session:
            resources = list((await session.execute(select(ChatRuntimeResource).where(
                ChatRuntimeResource.id.in_(resource_ids)))).scalars())
            by_pool = {resource.pool_id: resource for resource in resources}
            batch_id = registry["batch"]["id"]
            assert by_pool[batch_id].observed_state == "unknown"
            assert by_pool[batch_id].provider_id is None
            assert all(resource.provider_id for resource in resources if resource.pool_id != batch_id)
            operations = list((await session.execute(select(ChatResourceOperation).where(
                ChatResourceOperation.resource_id.in_(resource_ids), ChatResourceOperation.action == "create"
            ))).scalars())
            assert len(operations) == 3
            assert sorted(operation.request_status for operation in operations) == ["succeeded", "succeeded", "unknown"]
            for row in rows:
                current = await session.get(ChatRuntimePool, row.id)
                assert current.reconcile_fence >= 1
                assert current.reconcile_lease_expires_at is None
    finally:
        for pipe, process in zip(pipes, processes, strict=True):
            if process.is_alive():
                with contextlib.suppress(BrokenPipeError, OSError):
                    pipe.send("stop")
        for process in processes:
            await asyncio.to_thread(process.join, 10)
            if process.is_alive():
                process.terminate()
                await asyncio.to_thread(process.join, 5)
        for pipe in pipes:
            pipe.close()
        await real_redis.delete(*coordinator.keys, namespace + ":creates", namespace + ":creators")
        async with factory() as session, session.begin():
            if resource_ids:
                await session.execute(delete(ChatResourceOperation).where(ChatResourceOperation.resource_id.in_(resource_ids)))
                await session.execute(delete(ChatRuntimeResource).where(ChatRuntimeResource.id.in_(resource_ids)))
            if extra_pool_ids:
                await session.execute(delete(ChatRuntimePool).where(ChatRuntimePool.id.in_(extra_pool_ids)))
    assert all(process.exitcode == 0 for process in processes)


async def test_real_redis_elapsed_participation_ttl_expires_without_score_edits(real_redis, coordinators, monkeypatch):
    # Shorten only this test's participation TTL. Production remains 30 seconds.
    monkeypatch.setattr(_coordination, "PARTICIPATION_SECONDS", 1)
    registry = _registry()
    first = coordinators("A", _eligibility(registry))
    second = coordinators("B", _eligibility(registry))
    await first.heartbeat()
    await second.heartbeat()
    assert await first.publish_registry(registry)
    await asyncio.sleep(1.1)
    snapshot = await second.heartbeat()
    assert snapshot.leader == "B"
    assert snapshot.registry == registry
    assert set(snapshot.assignments.values()) == {"B"}
    assert await real_redis.zscore(first.keys[0], "A") is None
    assert await real_redis.hget(first.keys[1], "A") is None
    assert (await first.current()).leader == "B"
    assert await real_redis.zscore(first.keys[0], "A") is None


async def test_assignment_loss_inside_sql_lock_refuses_guest_drain_post(pool):
    from datetime import UTC, datetime
    from unittest.mock import AsyncMock

    from lumen.services.infrastructure.controller import _POOL

    _, fence = await store.claim_pool_lease(pool.id, "A", 30)
    resource = await store.request_resource(
        pool, role="worker", request_fingerprint="f" * 64,
        image_ref=pool.image_ref, policy_digest=pool.profile_digest, deadline=None,
    )
    factory = get_session_factory()
    async with factory() as session, session.begin():
        row = await session.get(ChatRuntimeResource, resource.id)
        row.role, row.address, row.observed_state = "api", "127.0.0.1", "draining"
        row.drain_requested_at = datetime.now(UTC)
    resource.role, resource.address = "api", "127.0.0.1"
    controller = ResourceController(_runtime_config(pool.deployment_id, (pool.name,)), {}, owner="A")
    current = AsyncMock(side_effect=[
        SimpleNamespace(assignments={pool.name: "A"}),
        SimpleNamespace(assignments={pool.name: "B"}),
    ])
    controller._coordinator = SimpleNamespace(current=current)
    controller.transport = SimpleNamespace(request=AsyncMock())
    controller.ingresses[pool.name] = SimpleNamespace(drain=AsyncMock())
    token = _POOL.set(pool.name)
    try:
        with pytest.raises(PoolLeaseLost):
            await controller._advance_drain(resource, fence, SimpleNamespace(name=pool.name))
        assert current.await_count == 2
        controller.transport.request.assert_not_awaited()
        controller.ingresses[pool.name].drain.assert_not_awaited()
        assert await store.pool_lease_owned(pool.id, "A", fence)
    finally:
        _POOL.reset(token)
        controller.executor.shutdown(wait=True)
