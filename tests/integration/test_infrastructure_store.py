"""Real MariaDB tests for durable resource intent and worker registrations."""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, event, select, text

from lumen.db import close_db, get_session_factory, init_db
from lumen.models.chat_infrastructure import (
    ChatResourceOperation,
    ChatRuntimePool,
    ChatRuntimeResource,
    ChatWorkerRegistration,
)
from lumen.models.chat_runs import ChatRun, ChatRunEventRow
from lumen.services.infrastructure import store
from lumen.services.infrastructure.controller import ResourceController
from lumen.services.infrastructure.ingress import MemberState
from lumen.services.infrastructure.store import ResourceUnavailable
from lumen.services.run_store import claim_queued_run

pytestmark = pytest.mark.integration


@pytest.fixture(params=["ON", "OFF"])
async def pool(request):
    init_db(os.environ["DATABASE_URL"], pool_size=4, max_overflow=1)
    factory = get_session_factory()
    engine = factory.kw["bind"]
    def snapshot_mode(connection, _record):
        cursor = connection.cursor()
        cursor.execute(f"SET SESSION innodb_snapshot_isolation={request.param}")
        cursor.close()
    event.listen(engine.sync_engine, "connect", snapshot_mode)
    pool_id = str(uuid.uuid4())
    row = ChatRuntimePool(id=pool_id, deployment_id="it-" + uuid.uuid4().hex,
                          name="workers", role="worker", backend="nova", enabled=True,
                          cloud_profile_id="trusted", project_id="operator-project",
                          region_name="RegionOne", image_ref="sha256:" + "a" * 64,
                          profile_digest="b" * 64, min_replicas=0, max_replicas=4, workload_class="online_text")
    async with factory() as session, session.begin():
        assert bool(await session.scalar(text("SELECT @@SESSION.innodb_snapshot_isolation"))) == (request.param == "ON")
        session.add(row)
    try:
        yield row
    finally:
        async with factory() as session, session.begin():
            registrations = (await session.execute(select(ChatWorkerRegistration).where(
                ChatWorkerRegistration.pool_id == pool_id))).scalars().all()
            for registration in registrations:
                await session.delete(registration)
            resources = (await session.execute(select(ChatRuntimeResource).where(
                ChatRuntimeResource.pool_id == pool_id))).scalars().all()
            for resource in resources:
                operations = (await session.execute(select(ChatResourceOperation).where(
                    ChatResourceOperation.resource_id == resource.id))).scalars().all()
                for operation in operations:
                    await session.delete(operation)
                await session.delete(resource)
            await session.delete(await session.get(ChatRuntimePool, pool_id))
        await close_db()
        event.remove(engine.sync_engine, "connect", snapshot_mode)


async def test_competing_claims_ambiguous_create_and_proven_absence(pool):
    resource = await store.request_resource(pool, role="worker", request_fingerprint="c" * 64,
                                            image_ref=pool.image_ref, policy_digest=pool.profile_digest,
                                            deadline=datetime.now(UTC) + timedelta(minutes=10))
    lease = await store.claim_pool_lease(pool.id, "controller-a", 30)
    assert lease is not None
    _, fence = lease
    claimed = await asyncio.gather(*[
        store.claim_operation(resource.id, 1, "create", "controller-a", fence) for _ in range(2)
    ])
    assert sum(op is not None for op in claimed) == 1
    assert await store.mark_unknown(resource.id, 1, "controller-a", fence)
    # Empty eventually-consistent listings must not authorize another create.
    assert await store.claim_operation(resource.id, 1, "create", "controller-a", fence) is None
    assert await store.occupancy(pool.id) == 1
    assert await store.request_delete(resource.id, "controller-a", fence) is not None
    # A lost response may create a VM later; absence cannot be inferred from no provider id.
    assert not await store.prove_absent(resource.id, 1, "controller-a", fence)


async def test_worker_registration_stales_after_twenty_seconds(pool):
    registration = await store.register_worker(worker_identity="it-" + uuid.uuid4().hex,
                                                boot_id=str(uuid.uuid4()), capacity=4,
                                                protocol_versions=[2], plugin_digest="d" * 64,
                                                schema_version=1, workload_classes=["online_text"])
    assert await store.heartbeat_worker(registration, accepting=True)
    assert registration not in await store.list_stale_workers(stale_seconds=20)
    factory = get_session_factory()
    async with factory() as session, session.begin():
        row = await session.get(ChatWorkerRegistration, registration)
        row.heartbeat_at = datetime.now(UTC) - timedelta(seconds=21)
    assert registration in await store.list_stale_workers(stale_seconds=20)
    async with factory() as session, session.begin():
        await session.delete(await session.get(ChatWorkerRegistration, registration))


async def _worker_resource(pool):
    resource = await store.request_resource(
        pool, role="worker", request_fingerprint="c" * 64,
        image_ref=pool.image_ref, policy_digest=pool.profile_digest, deadline=None,
    )
    factory = get_session_factory()
    async with factory() as session, session.begin():
        (await session.get(ChatRuntimeResource, resource.id)).certificate_not_after = datetime.now(UTC) + timedelta(hours=1)
    return resource.id


def _registration(**overrides):
    return {
        "worker_identity": "it-" + uuid.uuid4().hex,
        "boot_id": str(uuid.uuid4()), "capacity": 4,
        "protocol_versions": [2], "plugin_digest": "d" * 64,
        "schema_version": 1, "workload_classes": ["online_text"], **overrides,
    }


async def test_worker_registration_requires_consumed_bootstrap_and_matching_identity(pool):
    resource_id = await _worker_resource(pool)
    identity = _registration(resource_id=resource_id, resource_generation=1,
                             certificate_fingerprint="a" * 64)
    factory = get_session_factory()
    async with factory() as session, session.begin():
        resource = await session.get(ChatRuntimeResource, resource_id)
        resource.bootstrap_token_hash = "b" * 64

    with pytest.raises(ResourceUnavailable, match="worker_resource_unavailable"):
        await store.register_worker(**identity)

    # Even a fingerprint present on a pending resource does not consume its token.
    async with factory() as session, session.begin():
        (await session.get(ChatRuntimeResource, resource_id)).certificate_fingerprint = "a" * 64
    with pytest.raises(ResourceUnavailable, match="worker_resource_unavailable"):
        await store.register_worker(**identity)

    async with factory() as session, session.begin():
        (await session.get(ChatRuntimeResource, resource_id)).bootstrap_token_hash = None

    for mismatch in ({"resource_generation": 2}, {"certificate_fingerprint": "e" * 64}):
        with pytest.raises(ResourceUnavailable, match="worker_resource_unavailable"):
            await store.register_worker(**(identity | mismatch))

    registration = await store.register_worker(**identity)
    async with factory() as session:
        row = await session.get(ChatWorkerRegistration, registration)
        assert (row.resource_id, row.resource_generation, row.certificate_fingerprint, row.pool_id) == (
            resource_id, 1, "a" * 64, pool.id,
        )
    assert await store.heartbeat_worker(registration, accepting=True)
    async with factory() as session:
        resource = await session.get(ChatRuntimeResource, resource_id)
        # Slot projection is DB live leases, never a worker self-report.
        assert resource.active_slots == 0


@pytest.mark.parametrize("changed_field,new_value", [
    ("generation", 2),
    ("certificate_fingerprint", "e" * 64),
    ("bootstrap_token_hash", "b" * 64),
    ("desired_state", "deleting"),
])
async def test_worker_heartbeat_rejects_resource_after_identity_or_lifecycle_changes(pool, changed_field, new_value):
    resource_id = await _worker_resource(pool)
    factory = get_session_factory()
    async with factory() as session, session.begin():
        (await session.get(ChatRuntimeResource, resource_id)).certificate_fingerprint = "a" * 64
    registration = await store.register_worker(**_registration(
        resource_id=resource_id, resource_generation=1, certificate_fingerprint="a" * 64))
    assert await store.heartbeat_worker(registration, accepting=True)

    async with factory() as session, session.begin():
        setattr(await session.get(ChatRuntimeResource, resource_id), changed_field, new_value)
    assert not await store.heartbeat_worker(registration, accepting=False)
    async with factory() as session:
        worker = await session.get(ChatWorkerRegistration, registration)
        resource = await session.get(ChatRuntimeResource, resource_id)
        assert (worker.active_count, worker.accepting, resource.active_slots) == (0, True, 0)


async def test_worker_heartbeat_rejects_orphaned_registration(pool):
    resource_id = await _worker_resource(pool)
    factory = get_session_factory()
    async with factory() as session, session.begin():
        (await session.get(ChatRuntimeResource, resource_id)).certificate_fingerprint = "a" * 64
    registration = await store.register_worker(**_registration(
        resource_id=resource_id, resource_generation=1, certificate_fingerprint="a" * 64))
    assert await store.heartbeat_worker(registration, accepting=True)

    async with factory() as session, session.begin():
        operations = (await session.execute(select(ChatResourceOperation).where(
            ChatResourceOperation.resource_id == resource_id))).scalars().all()
        for operation in operations:
            await session.delete(operation)
        await session.flush()
        await session.delete(await session.get(ChatRuntimeResource, resource_id))

    assert not await store.heartbeat_worker(registration, accepting=False)
    async with factory() as session:
        worker = await session.get(ChatWorkerRegistration, registration)
        assert (worker.resource_id, worker.resource_generation, worker.active_count, worker.accepting) == (
            None, 1, 0, True,
        )


async def test_recycled_worker_generation_requires_new_boot_registration(pool):
    resource_id = await _worker_resource(pool)
    factory = get_session_factory()
    async with factory() as session, session.begin():
        (await session.get(ChatRuntimeResource, resource_id)).certificate_fingerprint = "a" * 64
    identity = _registration(resource_id=resource_id, resource_generation=1,
                             certificate_fingerprint="a" * 64)
    old_registration = await store.register_worker(**identity)
    assert await store.heartbeat_worker(old_registration, accepting=True)

    async with factory() as session, session.begin():
        resource = await session.get(ChatRuntimeResource, resource_id)
        resource.generation = 2
        resource.certificate_fingerprint = "e" * 64
    assert not await store.heartbeat_worker(old_registration, accepting=False)
    next_identity = identity | {"resource_generation": 2, "certificate_fingerprint": "e" * 64}
    with pytest.raises(ResourceUnavailable, match="worker_identity_mismatch"):
        await store.register_worker(**next_identity)

    new_registration = await store.register_worker(**(next_identity | {"boot_id": str(uuid.uuid4())}))
    assert new_registration != old_registration
    assert await store.heartbeat_worker(new_registration, accepting=True)
    async with factory() as session:
        old = await session.get(ChatWorkerRegistration, old_registration)
        new = await session.get(ChatWorkerRegistration, new_registration)
        assert (old.accepting, old.draining) == (False, True)
        assert (new.resource_generation, new.certificate_fingerprint, new.active_count, new.accepting) == (
            2, "e" * 64, 0, True,
        )


async def test_static_worker_registration_has_no_resource_fence(pool):
    registration = await store.register_worker(**_registration())
    assert await store.heartbeat_worker(registration, accepting=True)
    async with get_session_factory()() as session:
        row = await session.get(ChatWorkerRegistration, registration)
        assert (row.resource_id, row.resource_generation, row.certificate_fingerprint, row.pool_id) == (
            None, None, None, None,
        )
        assert (row.active_count, row.accepting) == (0, True)
    async with get_session_factory()() as session, session.begin():
        await session.delete(await session.get(ChatWorkerRegistration, registration))

async def test_worker_claim_requires_live_compatible_registration_and_ready_resource(pool):
    resource_id = await _worker_resource(pool)
    factory = get_session_factory()
    worker_identity = "it-" + uuid.uuid4().hex
    run_id = str(uuid.uuid4())
    async with factory() as session, session.begin():
        resource = await session.get(ChatRuntimeResource, resource_id)
        resource.certificate_fingerprint = "a" * 64
        resource.observed_state = "ready"
        session.add(ChatRun(
            id=run_id, run_scope="temp", project_id="it-" + uuid.uuid4().hex,
            user_id="worker-claim", model_name="model", capability_snapshot={}, pricing_snapshot={},
            client_request_id=str(uuid.uuid4()), request_fingerprint=uuid.uuid4().hex,
            fingerprint_version=1, execution_protocol_version=2, status="queued",
            required_plugin_digest="e" * 64, runtime_pool_id=pool.id, worker_pool_id=pool.id,
        ))
    try:
        registration = await store.register_worker(**_registration(
            worker_identity=worker_identity, resource_id=resource_id, resource_generation=1,
            certificate_fingerprint="a" * 64))

        async def attempt():
            async with factory() as session, session.begin():
                return await claim_queued_run(session, run_id, owner=worker_identity,
                                              registration_id=registration)

        assert await attempt() is None  # The frozen plugin set is incompatible.
        async with factory() as session, session.begin():
            (await session.get(ChatRun, run_id)).required_plugin_digest = "d" * 64
        assert (await attempt()).lease_owner == worker_identity + "#1"
        async with factory() as session, session.begin():
            run = await session.get(ChatRun, run_id)
            run.status, run.lease_owner, run.lease_expires_at = "queued", None, None
        assert await store.start_drain(registration)
        assert await attempt() is None  # A draining worker may renew but not claim.
        async with factory() as session, session.begin():
            worker = await session.get(ChatWorkerRegistration, registration)
            worker.draining, worker.accepting = False, True
            (await session.get(ChatRuntimeResource, resource_id)).observed_state = "unavailable"
        assert await attempt() is None  # Cloud readiness cannot be inferred from heartbeat alone.
    finally:
        async with factory() as session, session.begin():
            await session.delete(await session.get(ChatRun, run_id))


async def test_measured_api_load_scales_twice_then_stale_samples_hold_capacity(pool):
    factory = get_session_factory()
    async with factory() as session, session.begin():
        row = await session.get(ChatRuntimePool, pool.id)
        row.role = "api"
        row.min_replicas = 1
        row.db_connection_budget = 24
    claim = await store.claim_pool_lease(pool.id, "api-controller", 30)
    assert claim is not None
    definition = SimpleNamespace(
        role="api", name=pool.name, boot_timeout_seconds=600, idle_seconds=300,
        max_replicas=4, max_surge=1, db_connection_budget=24, db_connections_per_process=4,
        pg_connection_budget=0, pg_connections_per_process=0,
        ingress=SimpleNamespace(api_target_active_requests=8, api_target_ttft_ms=500),
    )
    controller = ResourceController(
        SimpleNamespace(max_parallel_cloud_operations=4, pools=(), tls=None, reconcile_interval_seconds=5),
        {}, owner="api-controller",
    )
    try:
        current, fence = claim
        resource = await store.request_resource(
            current, role="api", request_fingerprint="a" * 64,
            image_ref=current.image_ref, policy_digest=current.profile_digest, deadline=None,
            owner="api-controller", fence=fence,
        )
        async with factory() as session, session.begin():
            row = await session.get(ChatRuntimeResource, resource.id)
            row.observed_state = "ready"
            row.ingress_member_id = "member-1"
        assert await store.record_api_load(resource.id, resource.generation, "api-controller", fence, active_count=18)
        controller._api_load[resource.id] = (time.monotonic(), 18, 700)
        await controller._scale(current, fence, definition, await controller._resources(pool.id))
        assert await store.occupancy(pool.id) == 1
        async with factory() as session:
            assert (await session.get(ChatRuntimePool, pool.id)).high_demand_samples == 1

        current, fence = await store.claim_pool_lease(pool.id, "api-controller", 30)
        await controller._scale(current, fence, definition, await controller._resources(pool.id))
        assert await store.occupancy(pool.id) == 4
        async with factory() as session:
            projected = await session.get(ChatRuntimePool, pool.id)
            assert (projected.desired_replicas, projected.ready_replicas, projected.provisioning_replicas,
                    projected.draining_replicas, projected.queued_count) == (4, 1, 3, 0, 0)
            assert projected.last_scale_reason == "demand_high" and projected.last_reconciled_at is not None
        controller._api_load[resource.id] = (time.monotonic() - 60, 0, None)
        current, fence = await store.claim_pool_lease(pool.id, "api-controller", 30)
        await controller._scale(current, fence, definition, await controller._resources(pool.id))
        assert await store.occupancy(pool.id) == 4
        async with factory() as session:
            rows = (await session.execute(select(ChatRuntimeResource).where(
                ChatRuntimeResource.pool_id == pool.id))).scalars().all()
            assert all(row.desired_state != "deleting" for row in rows)
            assert (await session.get(ChatRuntimePool, pool.id)).last_scale_reason == "telemetry_stale"
    finally:
        controller.executor.shutdown(wait=True)


async def test_overdue_worker_drain_preserves_active_lease_until_live_leases_clear(pool):
    resource_id = await _worker_resource(pool)
    factory = get_session_factory()
    async with factory() as session, session.begin():
        resource = await session.get(ChatRuntimeResource, resource_id)
        resource.certificate_fingerprint = "a" * 64
        resource.observed_state = "ready"
        resource.created_at = datetime.now(UTC) - timedelta(seconds=800)
    registration = await store.register_worker(**_registration(
        resource_id=resource_id, resource_generation=1, certificate_fingerprint="a" * 64,
    ))
    run_id = str(uuid.uuid4())
    async with factory() as session, session.begin():
        session.add(ChatRun(
            id=run_id, run_scope="temp", project_id="it-" + uuid.uuid4().hex,
            user_id="worker-drain", model_name="model", capability_snapshot={}, pricing_snapshot={},
            client_request_id=str(uuid.uuid4()), request_fingerprint=uuid.uuid4().hex,
            fingerprint_version=1, execution_protocol_version=2, status="running",
            worker_pool_id=pool.id, worker_registration_id=registration, lease_fence=1,
            lease_owner="drain-worker#1", lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
        ))
    assert await store.heartbeat_worker(registration, accepting=True)
    claim = await store.claim_pool_lease(pool.id, "worker-controller", 30)
    assert claim is not None
    _, fence = claim
    controller = ResourceController(
        SimpleNamespace(max_parallel_cloud_operations=4, pools=(), tls=None),
        {}, owner="worker-controller",
    )
    definition = SimpleNamespace(name=pool.name)
    try:
        assert await store.begin_drain(resource_id, 1, "worker-controller", fence, reason="replacement")
        assert await store.heartbeat_worker(registration, accepting=True) == "draining"
        await controller._advance_drain((await controller._resources(pool.id))[0], fence, definition)
        async with factory() as session:
            row = await session.get(ChatRuntimeResource, resource_id)
            worker = await session.get(ChatWorkerRegistration, registration)
            assert row.desired_state != "deleting"
            assert row.active_slots == 1
            assert worker.draining and not worker.accepting

        async with factory() as session, session.begin():
            (await session.get(ChatRun, run_id)).status = "completed"
        assert await store.heartbeat_worker(registration, accepting=False) == "draining"
        await controller._advance_drain((await controller._resources(pool.id))[0], fence, definition)
        async with factory() as session:
            assert (await session.get(ChatRuntimeResource, resource_id)).desired_state != "deleting"
        assert await store.acknowledge_drain(registration)
        await controller._advance_drain((await controller._resources(pool.id))[0], fence, definition)
        async with factory() as session:
            assert (await session.get(ChatRuntimeResource, resource_id)).desired_state == "deleting"
    finally:
        controller.executor.shutdown(wait=True)
        async with factory() as session, session.begin():
            await session.delete(await session.get(ChatRun, run_id))


class _Octavia:
    """One fake Octavia member whose create/enable can be held open inside the SDK thread."""

    def __init__(self, existing: str | None):
        self.member_id = existing
        self.enabled = False
        self.calls: list = []
        self.entered = threading.Event()
        self.release = threading.Event()

    def inspect(self, resource_id, generation, address):
        return None if self.member_id is None else MemberState(self.member_id, True, self.enabled, 1, "ACTIVE")

    def register(self, resource_id, generation, address, member_id):
        self.calls.append("register")
        self.entered.set()
        if not self.release.wait(30):
            raise TimeoutError("register was never released")
        self.member_id, self.enabled = "member-1", True
        self.calls.append("enabled")
        return MemberState("member-1", True, True, 1, "ACTIVE")

    def drain(self, member_id, resource_id, generation, address):
        if member_id == self.member_id:
            self.enabled = False
        self.calls.append(("drain", member_id))


@pytest.mark.parametrize("existing", [None, "member-1"], ids=["create", "re-enable"])
async def test_lease_takeover_drain_always_follows_in_flight_ingress_enable(pool, monkeypatch, existing):
    factory = get_session_factory()
    async with factory() as session, session.begin():
        (await session.get(ChatRuntimePool, pool.id)).role = "api"
    current, fence_a = await store.claim_pool_lease(pool.id, "controller-a", 30)
    resource = await store.request_resource(
        current, role="api", request_fingerprint="d" * 64, image_ref=current.image_ref,
        policy_digest=current.profile_digest, deadline=None, owner="controller-a", fence=fence_a,
    )
    async with factory() as session, session.begin():
        row = await session.get(ChatRuntimeResource, resource.id)
        row.observed_state, row.address, row.ingress_member_id = "ready", "10.0.0.2", existing
        row.certificate_fingerprint = "a" * 64
        row.certificate_not_after = datetime.now(UTC) + timedelta(hours=1)
    octavia = _Octavia(existing)
    definition = SimpleNamespace(name=pool.name, max_lifetime_seconds=3600)
    config = SimpleNamespace(max_parallel_cloud_operations=4, pools=(), tls=None)
    first = ResourceController(config, {}, owner="controller-a")
    second = ResourceController(config, {}, owner="controller-b")
    first.ingresses[pool.name] = second.ingresses[pool.name] = octavia
    try:
        [snapshot] = await first._resources(pool.id)
        enabling = asyncio.create_task(first._ensure_ingress(snapshot, current, fence_a, definition))
        assert await asyncio.to_thread(octavia.entered.wait, 10)

        # Controller A's lease lapses while its Octavia write is still in flight.
        real_now = store._now
        monkeypatch.setattr(store, "_now", lambda: real_now() + timedelta(minutes=5))

        async def take_over() -> int:
            taken = await store.claim_pool_lease(pool.id, "controller-b", 30)
            assert taken is not None
            _, fence_b = taken
            [row] = await second._resources(pool.id)
            assert row.ingress_member_id == "member-1"
            drained = await store.begin_drain(row.id, row.generation, "controller-b", fence_b, reason="idle_window")
            assert drained is not None
            await second._fenced_cloud(drained, fence_b, octavia.drain, row.ingress_member_id,
                                       row.id, row.generation, row.address)
            assert await store.record_api_load(row.id, row.generation, "controller-b", fence_b,
                active_count=0, snapshot={"active_requests": 0, "active_sse": 0, "active_ws": 0,
                    "ttft_samples": 0, "p95_ttft_ms": None, "observed_at": datetime.now(UTC).isoformat()},
                drain_acknowledged=True, drain_fence=fence_b)
            assert await store.request_delete(row.id, "controller-b", fence_b) is not None
            return fence_b

        takeover = asyncio.create_task(take_over())
        await asyncio.sleep(0.5)
        assert not takeover.done()
        assert octavia.calls == ["register"]
        octavia.release.set()
        await enabling
        fence_b = await takeover
        assert octavia.calls == ["register", "enabled", ("drain", "member-1")]
        assert not octavia.enabled
        assert snapshot.ingress_member_id == "member-1"

        # The stale owner's retry and the new owner's pass both leave the drain in force.
        await first._ensure_ingress(snapshot, current, fence_a, definition)
        [row] = await second._resources(pool.id)
        assert row.desired_state == "deleting"
        await second._ensure_ingress(row, current, fence_b, definition)
        assert octavia.calls == ["register", "enabled", ("drain", "member-1")]
        assert not octavia.enabled
    finally:
        octavia.release.set()
        first.executor.shutdown(wait=True)
        second.executor.shutdown(wait=True)


def _controller_config():
    return SimpleNamespace(max_parallel_cloud_operations=4, pools=(), tls=None,
                           listen_port=8013, reconcile_interval_seconds=5)


async def _ready_worker(pool):
    resource_id = await _worker_resource(pool)
    factory = get_session_factory()
    async with factory() as session, session.begin():
        row = await session.get(ChatRuntimeResource, resource_id)
        row.observed_state = "ready"
        row.ready_at = datetime.now(UTC)
        row.certificate_fingerprint = "a" * 64
        row.provider_id = "owned-server"
        row.idle_since = datetime.now(UTC) - timedelta(seconds=600)
    registration = await store.register_worker(**_registration(
        resource_id=resource_id, resource_generation=1, certificate_fingerprint="a" * 64))
    return resource_id, registration


@pytest.mark.parametrize("target,column,value", [
    ("registration", "heartbeat_at", timedelta(seconds=-30)),
    ("registration", "accepting", False),
    ("registration", "draining", True),
    ("registration", "resource_generation", 2),
    ("registration", "certificate_fingerprint", "b" * 64),
    ("resource", "heartbeat_at", timedelta(seconds=-30)),
    ("resource", "certificate_not_after", timedelta(seconds=-1)),
    ("resource", "bootstrap_token_hash", "b" * 64),
    ("resource", "desired_state", "deleting"),
    ("resource", "drain_requested_at", timedelta()),
])
async def test_healthy_capacity_requires_fresh_accepting_identity(pool, target, column, value):
    resource_id, registration_id = await _ready_worker(pool)
    assert await store.healthy_worker_resources(pool.id) == {resource_id}
    factory = get_session_factory()
    async with factory() as session, session.begin():
        model, key = ((ChatWorkerRegistration, registration_id) if target == "registration"
                      else (ChatRuntimeResource, resource_id))
        row = await session.get(model, key)
        setattr(row, column, datetime.now(UTC) + value if isinstance(value, timedelta) else value)
    assert await store.healthy_worker_resources(pool.id) == set()
    assert await store.occupancy(pool.id) == 1  # Revocation/staleness is not Nova absence proof.


async def test_claim_vs_idle_scale_in_race_is_serialized_on_resource(pool):
    resource_id, registration = await _ready_worker(pool)
    factory = get_session_factory()
    worker_identity = None
    async with factory() as session:
        worker_identity = (await session.get(ChatWorkerRegistration, registration)).worker_identity
    run_id = str(uuid.uuid4())
    async with factory() as session, session.begin():
        session.add(ChatRun(id=run_id, run_scope="temp", project_id="race-" + uuid.uuid4().hex,
            user_id="claim-drain", model_name="fixture", capability_snapshot={}, pricing_snapshot={},
            client_request_id=str(uuid.uuid4()), request_fingerprint="r" * 64, fingerprint_version=1,
            execution_protocol_version=2, workload_class="online_text", worker_pool_id=pool.id,
            required_plugin_digest="d" * 64, status="queued"))
    _, fence = await store.claim_pool_lease(pool.id, "race-controller", 30)
    async def claim():
        async def transaction():
            async with factory() as session, session.begin():
                from lumen.services.worker_routing import use_read_committed
                await use_read_committed(session)
                return await claim_queued_run(session, run_id, owner=worker_identity, registration_id=registration)
        return await store.retry_deadlocks(transaction)
    try:
        claimed, drained = await asyncio.gather(claim(), store.begin_drain(resource_id, 1,
            "race-controller", fence, reason="idle_window", idle_seconds=1))
        # Either the claim wins and cancels idle eligibility, or drain wins and fences it.
        assert (claimed is None) == (drained is not None)
        async with factory() as session:
            row = await session.get(ChatRuntimeResource, resource_id)
            run = await session.get(ChatRun, run_id)
            if drained is not None:
                assert not row.accepting and run.status == "queued"
            else:
                assert run.lease_owner == worker_identity + "#1" and row.drain_requested_at is None
        assert await store.request_delete(resource_id, "race-controller", fence) is None
    finally:
        async with factory() as session, session.begin():
            await session.execute(delete(ChatRunEventRow).where(ChatRunEventRow.run_id == run_id))
            await session.delete(await session.get(ChatRun, run_id))


async def test_worker_drain_survives_controller_restart_and_zero_slots_is_not_ack(pool):
    resource_id, registration = await _ready_worker(pool)
    _, fence = await store.claim_pool_lease(pool.id, "old-controller", 30)
    assert await store.begin_drain(resource_id, 1, "old-controller", fence, reason="idle_window")
    factory = get_session_factory()
    async with factory() as session, session.begin():
        lease = await session.get(ChatRuntimePool, pool.id)
        lease.reconcile_lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    _, fence = await store.claim_pool_lease(pool.id, "restarted-controller", 30)
    restarted = ResourceController(_controller_config(), {}, owner="restarted-controller")
    from lumen.services.infrastructure.providers import DeleteResult, Observation
    deletes = []
    class Nova:
        def observe(self, ref):
            return Observation(ref.provider_id, "ACTIVE", None, None, {})
        def delete(self, ref):
            deletes.append(ref.provider_id)
            return DeleteResult(absent=True)
    try:
        [row] = await restarted._resources(pool.id)
        assert row.observed_state == "draining" and row.active_slots == 0
        await restarted._advance_drain(row, fence, SimpleNamespace(name=pool.name))
        async with factory() as session:
            assert (await session.get(ChatRuntimeResource, resource_id)).desired_state != "deleting"
        await restarted._reconcile_delete(row, SimpleNamespace(name=pool.name), fence, Nova())
        assert deletes == []
        async with factory() as session, session.begin():
            (await session.get(ChatWorkerRegistration, registration)).auxiliary_active = 1
        assert not await store.acknowledge_drain(registration)
        async with factory() as session, session.begin():
            (await session.get(ChatWorkerRegistration, registration)).auxiliary_active = 0
        assert await store.acknowledge_drain(registration)
        await restarted._advance_drain(row, fence, SimpleNamespace(name=pool.name))
        async with factory() as session:
            row = await session.get(ChatRuntimeResource, resource_id)
            assert row.drain_ack_at is not None and row.desired_state == "deleting"
        await restarted._reconcile_delete(row, SimpleNamespace(name=pool.name), fence, Nova())
        assert deletes == ["owned-server"] and await store.occupancy(pool.id) == 0
    finally:
        restarted.executor.shutdown(wait=True)


async def test_replacement_is_one_shot_and_only_replacements_spend_surge(pool):
    factory = get_session_factory()
    async with factory() as session, session.begin():
        row = await session.get(ChatRuntimePool, pool.id)
        row.max_replicas, row.max_surge = 1, 1
        row.max_lifetime_seconds, row.boot_timeout_seconds, row.drain_seconds = 600, 60, 120
    current, fence = await store.claim_pool_lease(pool.id, "replacement-controller", 30)
    old = await store.request_resource(current, role="worker", request_fingerprint="o" * 64,
        image_ref=current.image_ref, policy_digest=current.profile_digest, deadline=None,
        owner="replacement-controller", fence=fence)
    async with factory() as session, session.begin():
        row = await session.get(ChatRuntimeResource, old.id)
        row.observed_state = "ready"
        row.created_at = datetime.now(UTC) - timedelta(seconds=419)
    async def request(**kwargs):
        return await store.request_resource(current, role="worker", request_fingerprint=uuid.uuid4().hex * 2,
            image_ref=current.image_ref, policy_digest=current.profile_digest,
            deadline=datetime.now(UTC) + timedelta(seconds=60), owner="replacement-controller", fence=fence,
            physical_limit=2, **kwargs)
    with pytest.raises(ResourceUnavailable, match="pool_capacity_exceeded"):
        await request()
    with pytest.raises(ResourceUnavailable, match="replacement_not_due"):
        await request(replacement_for=old.id)
    async with factory() as session, session.begin():
        (await session.get(ChatRuntimeResource, old.id)).created_at = datetime.now(UTC) - timedelta(seconds=421)
    outcomes = await asyncio.gather(request(replacement_for=old.id), request(replacement_for=old.id),
                                    return_exceptions=True)
    replacements = [row for row in outcomes if isinstance(row, ChatRuntimeResource)]
    assert len(replacements) == 1
    assert replacements[0].drain_reason == "replacement:" + old.id
    assert await store.occupancy(pool.id) == 2
    with pytest.raises(ResourceUnavailable, match="replacement_pending"):
        await request(replacement_for=old.id)
    async with factory() as session:
        assert (await session.get(ChatRuntimeResource, old.id)).drain_requested_at is None


async def test_service_ewma_terminal_cursor_ties_bounds_and_cold_fallback(pool):
    factory = get_session_factory()
    _, fence = await store.claim_pool_lease(pool.id, "ewma-controller", 30)
    assert await store.observe_service_time(pool.id, "ewma-controller", fence, cold_ms=30000) == 30000
    completed = datetime.now(UTC) - timedelta(seconds=1)
    ids = sorted(str(uuid.uuid4()) for _ in range(2))
    async def sample(run_id, elapsed_ms, at):
        async with factory() as session, session.begin():
            session.add(ChatRun(id=run_id, run_scope="temp", project_id="ewma-" + uuid.uuid4().hex,
                user_id="service", model_name="fixture", capability_snapshot={}, pricing_snapshot={},
                client_request_id=str(uuid.uuid4()), request_fingerprint="e" * 64, fingerprint_version=1,
                worker_pool_id=pool.id, workload_class="online_text", status="completed",
                provider_started_at=at - timedelta(milliseconds=elapsed_ms)))
            await session.flush()
            session.add(ChatRunEventRow(run_id=run_id, seq=1, event_type="run.completed", payload="{}", created_at=at))
    long_id = str(uuid.uuid4())
    try:
        await sample(ids[0], 1000, completed)
        await sample(ids[1], 100000, completed)
        estimate = await store.observe_service_time(pool.id, "ewma-controller", fence, cold_ms=30000)
        assert estimate == 39360
        async with factory() as session:
            row = await session.get(ChatRuntimePool, pool.id)
            assert row.service_time_cursor_id == ids[1]
        assert await store.observe_service_time(pool.id, "ewma-controller", fence, cold_ms=30000) == estimate
        async with factory() as session, session.begin():
            (await session.get(ChatRun, ids[0])).updated_at = datetime.now(UTC)
        assert await store.observe_service_time(pool.id, "ewma-controller", fence, cold_ms=30000) == estimate
        await sample(long_id, 100 * 86400000, datetime.now(UTC))
        bounded = await store.observe_service_time(pool.id, "ewma-controller", fence, cold_ms=30000)
        assert bounded == (4 * estimate + 86400000) // 5
        async with factory() as session, session.begin():
            await session.execute(delete(ChatRunEventRow).where(ChatRunEventRow.run_id.in_([*ids, long_id])))
            row = await session.get(ChatRuntimePool, pool.id)
            row.service_time_cursor_at = datetime.now(UTC) - timedelta(days=2)
        assert await store.observe_service_time(pool.id, "ewma-controller", fence, cold_ms=120000) == 120000
    finally:
        async with factory() as session, session.begin():
            await session.execute(delete(ChatRunEventRow).where(ChatRunEventRow.run_id.in_([*ids, long_id])))
            await session.execute(delete(ChatRun).where(ChatRun.id.in_([*ids, long_id])))


async def _api_guest(pool, owner="api-controller"):
    factory = get_session_factory()
    async with factory() as session, session.begin():
        row = await session.get(ChatRuntimePool, pool.id)
        row.role, row.workload_class = "api", None
    current, fence = await store.claim_pool_lease(pool.id, owner, 30)
    guest = await store.request_resource(current, role="api", request_fingerprint="g" * 64,
        image_ref=current.image_ref, policy_digest=current.profile_digest, deadline=None, owner=owner, fence=fence)
    async with factory() as session, session.begin():
        row = await session.get(ChatRuntimeResource, guest.id)
        row.observed_state, row.address, row.port = "ready", "10.0.0.2", 8013
        row.ready_at = datetime.now(UTC)
        row.certificate_fingerprint = "a" * 64
        row.certificate_not_after = datetime.now(UTC) + timedelta(hours=1)
        row.ingress_member_id = "member-1"
    return guest.id, fence


class _DrainOctavia:
    def __init__(self, calls, *, pending=False):
        self.calls = calls
        self.member = "member-1"
        self.pending = pending
        self.entered, self.release = threading.Event(), threading.Event()
    def drain(self, member_id, resource_id, generation, address):
        if self.member is None:
            return
        self.calls.append("weight=0")
        self.entered.set()
        if self.pending and not self.release.wait(10):
            raise TimeoutError("ACTIVE not reached")
        self.calls.append("ACTIVE")
    def delete(self, member_id, resource_id, generation, address):
        if self.member is not None:
            self.calls.append("member.delete")
            self.member = None


class _DrainProbe:
    def __init__(self, calls, *, active_ws=0, stale=False, acknowledged=True, stale_fence=False):
        self.calls, self.active_ws, self.stale = calls, active_ws, stale
        self.acknowledged, self.stale_fence, self.fence = acknowledged, stale_fence, None
    async def request(self, **kwargs):
        assert kwargs["certificate_fingerprints"] == frozenset({"a" * 64})
        if kwargs["method"] == "POST":
            self.calls.append("guest.private.drain")
            self.fence = kwargs["body"]["fence"]
            return json.dumps({"draining": True, "drain_fence": self.fence}).encode()
        self.calls.append("counters")
        return json.dumps({"ready": False, "draining": True,
            "drain_fence": self.fence - int(self.stale_fence), "drain_acknowledged": self.acknowledged,
            "load": {"active_requests": 0, "active_sse": 0, "active_ws": self.active_ws,
                "ttft_samples": 0, "p95_ttft_ms": None,
                "observed_at": (datetime.now(UTC) - timedelta(seconds=11 if self.stale else 0)).isoformat()}}).encode()


async def test_api_drain_restart_waits_active_then_ack_member_delete_before_nova(pool):
    from lumen.services.infrastructure.providers import DeleteResult
    resource_id, fence = await _api_guest(pool)
    assert await store.begin_drain(resource_id, 1, "api-controller", fence, reason="idle_window")
    factory = get_session_factory()
    async with factory() as session, session.begin():
        row = await session.get(ChatRuntimePool, pool.id)
        row.reconcile_lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        guest = await session.get(ChatRuntimeResource, resource_id)
        guest.provider_id = "owned-server"
        assert guest.drain_requested_at is not None and not guest.accepting
    _, fence = await store.claim_pool_lease(pool.id, "restarted-api-controller", 30)
    c = ResourceController(_controller_config(), {}, owner="restarted-api-controller")
    calls = ["durable"]
    octavia = _DrainOctavia(calls, pending=True)
    c.ingresses[pool.name], c.transport = octavia, _DrainProbe(calls)
    class Nova:
        def delete(self, ref):
            calls.append("nova.delete")
            return DeleteResult(absent=True)
    try:
        [guest] = await c._resources(pool.id)
        draining = asyncio.create_task(c._advance_drain(guest, fence, SimpleNamespace(name=pool.name)))
        assert await asyncio.to_thread(octavia.entered.wait, 5)
        assert calls == ["durable", "guest.private.drain", "weight=0"]
        assert not draining.done()
        octavia.release.set()
        await draining
        assert calls == ["durable", "guest.private.drain", "weight=0", "ACTIVE", "counters", "member.delete"]
        [guest] = await c._resources(pool.id)
        assert guest.drain_ack_at is not None and guest.desired_state == "deleting"
        await c._reconcile_delete(guest, SimpleNamespace(name=pool.name), fence, Nova())
        assert calls[-1] == "nova.delete" and calls.index("member.delete") < calls.index("nova.delete")
        assert await store.occupancy(pool.id) == 0
    finally:
        octavia.release.set()
        c.executor.shutdown(wait=True)


@pytest.mark.parametrize("load", [{"active_ws": 1}, {"stale": True}, {"acknowledged": False}, {"stale_fence": True}])
async def test_api_scale_in_requires_zero_fresh_counters_and_current_fence_ack(pool, load):
    resource_id, fence = await _api_guest(pool)
    assert await store.begin_drain(resource_id, 1, "api-controller", fence, reason="idle_window")
    c = ResourceController(_controller_config(), {}, owner="api-controller")
    calls = []
    c.transport, c.ingresses[pool.name] = _DrainProbe(calls, **load), _DrainOctavia(calls)
    try:
        [guest] = await c._resources(pool.id)
        await c._advance_drain(guest, fence, SimpleNamespace(name=pool.name))
        assert "member.delete" not in calls
        async with get_session_factory()() as session:
            assert (await session.get(ChatRuntimeResource, resource_id)).desired_state != "deleting"
    finally:
        c.executor.shutdown(wait=True)


async def test_ambiguous_ingress_create_restart_may_only_adopt(pool):
    resource_id, fence = await _api_guest(pool)
    factory = get_session_factory()
    async with factory() as session, session.begin():
        row = await session.get(ChatRuntimeResource, resource_id)
        row.ingress_member_id = None
        row.observed_state = "booting"
    assert await store.claim_ingress_create(resource_id, 1, "api-controller", fence)
    assert await store.unresolved_ingress_create(pool.id, resource_id=resource_id)
    async with factory() as session, session.begin():
        (await session.get(ChatRuntimePool, pool.id)).reconcile_lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    current, fence = await store.claim_pool_lease(pool.id, "restarted-controller", 30)
    c = ResourceController(_controller_config(), {}, owner="restarted-controller")
    creates = []
    class Ingress:
        def inspect(self, *args):
            return None
        def register(self, *args):
            creates.append(args)
            raise AssertionError("unknown member create must never be repeated")
    c.ingresses[pool.name] = Ingress()
    try:
        [guest] = await c._resources(pool.id)
        await c._ensure_ingress(guest, current, fence, SimpleNamespace(name=pool.name))
        assert creates == []
        assert await store.record_ingress_member(resource_id, 1, "restarted-controller", fence, "owned-late-member")
        assert not await store.unresolved_ingress_create(pool.id, resource_id=resource_id)
    finally:
        c.executor.shutdown(wait=True)


async def test_busy_pool_deferral_without_member_write_allows_one_later_create(pool):
    resource_id, fence = await _api_guest(pool)
    factory = get_session_factory()
    async with factory() as session, session.begin():
        row = await session.get(ChatRuntimeResource, resource_id)
        row.ingress_member_id = None
        row.observed_state = "booting"
    async with factory() as session:
        current = await session.get(ChatRuntimePool, pool.id)
    c = ResourceController(_controller_config(), {}, owner="api-controller")
    calls = []
    class BusyThenActive:
        busy = True
        def inspect(self, *args):
            return None
        def register(self, *args):
            calls.append("register")
            if self.busy:
                from lumen.services.infrastructure.ingress import IngressCreateDeferred
                raise IngressCreateDeferred("ingress pool is not ACTIVE; create deferred")
            return MemberState("member-late", True, True, 1, "ACTIVE")
    ingress = BusyThenActive()
    c.ingresses[pool.name] = ingress
    try:
        [guest] = await c._resources(pool.id)
        await c._ensure_ingress(guest, current, fence, SimpleNamespace(name=pool.name))
        # Proven no-write deferral is not an ambiguous create.
        assert not await store.unresolved_ingress_create(pool.id, resource_id=resource_id)
        # A different fence cannot release the reclaimed intent.
        assert await store.claim_ingress_create(resource_id, 1, "api-controller", fence)
        assert not await store.defer_unsubmitted_ingress_create(resource_id, 1, "api-controller", fence + 1)
        assert not await store.claim_ingress_create(resource_id, 1, "api-controller", fence)
        assert await store.defer_unsubmitted_ingress_create(resource_id, 1, "api-controller", fence)
        ingress.busy = False
        [guest] = await c._resources(pool.id)
        await c._ensure_ingress(guest, current, fence, SimpleNamespace(name=pool.name))
        assert calls == ["register", "register"]
        async with factory() as session:
            assert (await session.get(ChatRuntimeResource, resource_id)).ingress_member_id == "member-late"
            operation = await session.scalar(select(ChatResourceOperation).where(
                ChatResourceOperation.resource_id == resource_id,
                ChatResourceOperation.action == "ingress_create"))
            assert operation.request_status == "succeeded" and operation.attempts == 1
    finally:
        c.executor.shutdown(wait=True)


async def test_drain_between_ingress_claim_and_lock_releases_unsubmitted_claim(pool, monkeypatch):
    resource_id, fence = await _api_guest(pool)
    factory = get_session_factory()
    async with factory() as session, session.begin():
        row = await session.get(ChatRuntimeResource, resource_id)
        row.ingress_member_id = None
        row.observed_state = "booting"
    async with factory() as session:
        current = await session.get(ChatRuntimePool, pool.id)
    real_claim = store.claim_ingress_create

    async def claim_then_drain(*args):
        assert await real_claim(*args)
        async with factory() as session, session.begin():
            # A concurrent scale-in/replacement fence lands before the locked recheck.
            (await session.get(ChatRuntimeResource, resource_id)).drain_requested_at = datetime.now(UTC)
        return True

    monkeypatch.setattr(store, "claim_ingress_create", claim_then_drain)
    c = ResourceController(_controller_config(), {}, owner="api-controller")
    class NeverCreate:
        def inspect(self, *args):
            return None
        def register(self, *args):
            raise AssertionError("drained guest must not be exposed")
    c.ingresses[pool.name] = NeverCreate()
    try:
        [guest] = await c._resources(pool.id)
        await c._ensure_ingress(guest, current, fence, SimpleNamespace(name=pool.name))
        # Withdrawal/deletion is no longer blocked by a claim that never reached Octavia.
        assert not await store.unresolved_ingress_create(pool.id, resource_id=resource_id)
    finally:
        c.executor.shutdown(wait=True)


async def test_four_pools_reconcile_owned_sql_demand_with_batch_zero_cold_start(pool):
    factory = get_session_factory()
    ids = {"text": pool.id, **{name: str(uuid.uuid4()) for name in ("api", "media", "batch")}}
    cold = {"text": 30000, "media": 120000, "batch": 60000}
    classes = {"text": "online_text", "media": "online_media", "batch": "batch"}
    definitions = []
    async with factory() as session, session.begin():
        original = await session.get(ChatRuntimePool, pool.id)
        original.min_replicas, original.max_replicas, original.slots_per_worker = 1, 8, 4
        for name in ("api", "media", "batch"):
            session.add(ChatRuntimePool(id=ids[name], deployment_id=pool.deployment_id,
                name=name, role="api" if name == "api" else "worker", workload_class=classes.get(name),
                backend="nova", enabled=True, cloud_profile_id="trusted", project_id="operator-project",
                region_name="RegionOne", image_ref=pool.image_ref, profile_digest=pool.profile_digest,
                min_replicas=2 if name == "api" else int(name != "batch"), max_replicas=8,
                slots_per_worker=4, max_surge=1))
    for name, pool_id in ids.items():
        definitions.append(SimpleNamespace(name=name, enabled=True, role="api" if name == "api" else "worker",
            max_replicas=8, max_surge=1, boot_timeout_seconds=60, drain_seconds=120,
            max_lifetime_seconds=600, idle_seconds=90, db_connections_per_process=30,
            db_connection_budget=180, pg_connections_per_process=1, pg_connection_budget=1000,
            cold_service_time_ms=cold.get(name),
            ingress=SimpleNamespace(api_target_active_requests=8, api_target_ttft_ms=500)))
    run_ids = []
    async with factory() as session, session.begin():
        for name, count in (("text", 2), ("media", 1), ("batch", 4)):
            for _ in range(count):
                run_id = str(uuid.uuid4())
                run_ids.append(run_id)
                session.add(ChatRun(id=run_id, run_scope="temp", project_id="demand-" + uuid.uuid4().hex,
                    user_id="four-pools", model_name="fixture", capability_snapshot={}, pricing_snapshot={},
                    client_request_id=str(uuid.uuid4()), request_fingerprint="q" * 64, fingerprint_version=1,
                    execution_protocol_version=2, worker_pool_id=ids[name], workload_class=classes[name],
                    run_kind="api_completion" if name == "batch" else "completion", status="queued",
                    created_at=datetime.now(UTC) - timedelta(seconds=10)))
        # Other class, fixed-worker, incompatible-protocol and API-owned work never inflate text demand.
        for extra in ({"workload_class": "online_media"}, {"worker_pool_id": None},
                      {"execution_protocol_version": 999}, {"workload_class": "realtime"}):
            run_id = str(uuid.uuid4())
            run_ids.append(run_id)
            values = dict(id=run_id, run_scope="temp", project_id="other-" + uuid.uuid4().hex,
                user_id="four-pools", model_name="fixture", capability_snapshot={}, pricing_snapshot={},
                client_request_id=str(uuid.uuid4()), request_fingerprint="z" * 64, fingerprint_version=1,
                execution_protocol_version=2, worker_pool_id=ids["text"], workload_class="online_text", status="queued")
            session.add(ChatRun(**(values | extra)))
    c = ResourceController(_controller_config(), {}, owner="four-pool-controller")
    c.config.pools = definitions
    c.providers = {name: object() for name in ids}
    c._pool_ids = ids
    try:
        active, queued, oldest = await store.eligible_worker_demand(ids["text"])
        assert (active, queued) == (0, 2) and oldest is not None
        active, queued, oldest = await store.eligible_worker_demand(ids["batch"])
        assert (active, queued) == (0, 4) and oldest is not None
        # The first high sample creates nothing; the second produces independently clamped targets.
        await c.tick()
        assert await asyncio.gather(*(store.occupancy(pool_id) for pool_id in ids.values())) == [0] * 4
        await c.tick()
        expected = {"api": 2, "text": 2, "media": 3, "batch": 5}
        async with factory() as session:
            for name, count in expected.items():
                row = await session.get(ChatRuntimePool, ids[name])
                assert (row.desired_replicas, row.provisioning_replicas, row.ready_replicas) == (count, count, 0)
                assert row.queued_count == {"api": 0, "text": 2, "media": 1, "batch": 4}[name]
                assert row.last_scale_reason == ("db_budget" if name == "batch" else "demand_high")
                assert row.last_reconciled_at is not None
    finally:
        c.executor.shutdown(wait=True)
        async with factory() as session, session.begin():
            await session.execute(delete(ChatRun).where(ChatRun.id.in_(run_ids)))
            resource_ids = select(ChatRuntimeResource.id).where(ChatRuntimeResource.pool_id.in_(ids.values()))
            await session.execute(delete(ChatResourceOperation).where(ChatResourceOperation.resource_id.in_(resource_ids)))
            await session.execute(delete(ChatRuntimeResource).where(ChatRuntimeResource.pool_id.in_(ids.values())))
            await session.execute(delete(ChatRuntimePool).where(ChatRuntimePool.id.in_([ids[n] for n in ("api", "media", "batch")])))


async def test_known_boot_timeout_reclaim_requires_full_owned_observation(pool):
    from lumen.services.infrastructure.providers import DeleteResult, Observation
    current, fence = await store.claim_pool_lease(pool.id, "boot-controller", 30)
    guest = await store.request_resource(current, role="worker", request_fingerprint="t" * 64,
        image_ref=current.image_ref, policy_digest=current.profile_digest,
        deadline=datetime.now(UTC) - timedelta(seconds=1), owner="boot-controller", fence=fence)
    factory = get_session_factory()
    async with factory() as session, session.begin():
        row = await session.get(ChatRuntimeResource, guest.id)
        row.provider_id, row.observed_state = "owned-server", "booting"
    labels, deletes = {}, []
    class Nova:
        requires_delivery = False
        def observe(self, ref):
            return Observation(ref.provider_id, "ACTIVE", None, None, labels)
        def delete(self, ref):
            deletes.append(ref.provider_id)
            return DeleteResult(absent=True)
    c = ResourceController(_controller_config(), {}, owner="boot-controller")
    policy = SimpleNamespace(name=pool.name)
    try:
        [row] = await c._resources(pool.id)
        await c._reconcile_observe(row, policy, fence, Nova())
        async with factory() as session:
            assert (await session.get(ChatRuntimeResource, row.id)).failure_code is None
        assert deletes == []
        labels.update(lumen_deployment=pool.deployment_id, lumen_pool=pool.name, lumen_resource=row.id,
                      lumen_generation="1", lumen_fingerprint="t" * 64, lumen_policy=pool.profile_digest)
        await c._reconcile_observe(row, policy, fence, Nova())
        [row] = await c._resources(pool.id)
        assert row.failure_code == "boot_timeout" and row.desired_state == "deleting"
        await c._reconcile_delete(row, policy, fence, Nova())
        assert deletes == ["owned-server"] and await store.occupancy(pool.id) == 0
    finally:
        c.executor.shutdown(wait=True)
