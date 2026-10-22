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
        # Exercise SQL demand/target reconciliation directly; the separate real
        # Redis multiprocess suite covers the nonblocking assignment dispatcher.
        # The first high sample creates nothing; the second clamps each target.
        await c._joined(*(c._tick_pool(definition) for definition in definitions))
        assert await asyncio.gather(*(store.occupancy(pool_id) for pool_id in ids.values())) == [0] * 4
        await c._joined(*(c._tick_pool(definition) for definition in definitions))
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


class _Assignment:
    """Redis assignment seam; SQL fences, claims and row state stay real MariaDB."""

    def __init__(self, pool_name, owner):
        self.pool_name, self.assignments = pool_name, {pool_name: owner}

    async def current(self):
        return SimpleNamespace(assignments=dict(self.assignments))


def _owned_labels(pool, resource_id, fingerprint):
    return dict(lumen_deployment=pool.deployment_id, lumen_pool=pool.name, lumen_resource=resource_id,
                lumen_generation="1", lumen_fingerprint=fingerprint, lumen_policy=pool.profile_digest)


async def _operation(resource_id, action):
    async with get_session_factory()() as session:
        return await session.scalar(select(ChatResourceOperation).where(
            ChatResourceOperation.resource_id == resource_id, ChatResourceOperation.action == action))


@pytest.mark.parametrize("gate", ["redis-assignment", "sql-fence"])
async def test_pre_submit_gate_failure_returns_create_claim_for_one_later_submission(pool, monkeypatch, gate):
    from lumen.services.infrastructure import controller as controller_module
    from lumen.services.infrastructure.controller import PoolLeaseLost
    from lumen.services.infrastructure.providers import Observation

    current, fence = await store.claim_pool_lease(pool.id, "controller-a", 30)
    guest = await store.request_resource(current, role="worker", request_fingerprint="u" * 64,
        image_ref=current.image_ref, policy_digest=current.profile_digest,
        deadline=datetime.now(UTC) + timedelta(minutes=10), owner="controller-a", fence=fence)
    factory = get_session_factory()
    creates = []

    class Nova:
        requires_delivery = True
        def create(self, intent):
            creates.append(intent.resource_id)
            return Observation("owned-server", "BUILD", None, None,
                               _owned_labels(pool, intent.resource_id, "u" * 64))

    real_claim = store.claim_operation
    successor = {}

    async def claim_then_lose_gate(resource_id, generation, action, owner, fence_value):
        operation = await real_claim(resource_id, generation, action, owner, fence_value)
        async with factory() as session, session.begin():
            row = await session.get(ChatRuntimeResource, resource_id)
            # Simulates a pre-create token held only in this controller's memory.
            row.bootstrap_token_hash = "f" * 64
            row.bootstrap_expires_at = datetime.now(UTC) + timedelta(minutes=5)
            if gate == "sql-fence":
                (await session.get(ChatRuntimePool, pool.id)).reconcile_lease_expires_at = (
                    datetime.now(UTC) - timedelta(seconds=1))
        if gate == "sql-fence":
            successor["claim"] = await store.claim_pool_lease(pool.id, "controller-b", 30)
        else:
            a._coordinator.assignments[pool.name] = "controller-b"
        return operation

    a = ResourceController(_controller_config(), {}, owner="controller-a")
    b = ResourceController(_controller_config(), {}, owner="controller-b")
    a._coordinator = _Assignment(pool.name, "controller-a")
    target = controller_module._POOL.set(pool.name)
    policy = SimpleNamespace(name=pool.name)
    try:
        monkeypatch.setattr(store, "claim_operation", claim_then_lose_gate)
        [row] = await a._resources(pool.id)
        with pytest.raises(PoolLeaseLost):
            await a._reconcile_create(row, policy, fence, Nova())
        monkeypatch.setattr(store, "claim_operation", real_claim)
        assert creates == []
        operation = await _operation(guest.id, "create")
        assert operation.request_status == "claimed" and operation.owner == store._UNCLAIMED
        assert operation.attempts == 1 and operation.error_code == "not_submitted"
        [row] = await a._resources(pool.id)
        assert row.observed_state == "requested" and row.provider_id is None
        assert row.bootstrap_token_hash is None and row.bootstrap_expires_at is None

        if gate == "sql-fence":
            _, retry_fence = successor["claim"]
            retry = b
        else:
            a._coordinator.assignments[pool.name] = "controller-a"
            retry_fence, retry = fence, a
        await retry._reconcile_create(row, policy, retry_fence, Nova())
        assert creates == [guest.id]
        operation = await _operation(guest.id, "create")
        assert operation.request_status == "succeeded" and operation.owner == retry.owner
        [row] = await retry._resources(pool.id)
        assert row.provider_id == "owned-server" and row.observed_state == "creating"
        # The submitted create is one-shot: a further pass never resubmits it.
        await retry._reconcile_create(row, policy, retry_fence, Nova())
        assert creates == [guest.id]
    finally:
        controller_module._POOL.reset(target)
        a.executor.shutdown(wait=True)
        b.executor.shutdown(wait=True)


async def test_create_exception_after_sdk_entry_stays_unknown_and_is_never_resubmitted(pool):
    from lumen.services.infrastructure import controller as controller_module

    current, fence = await store.claim_pool_lease(pool.id, "controller-a", 30)
    guest = await store.request_resource(current, role="worker", request_fingerprint="v" * 64,
        image_ref=current.image_ref, policy_digest=current.profile_digest,
        deadline=datetime.now(UTC) + timedelta(minutes=10), owner="controller-a", fence=fence)
    creates = []

    class Nova:
        requires_delivery = True
        def create(self, intent):
            creates.append(intent.resource_id)
            raise TimeoutError("create response lost after submission")

    c = ResourceController(_controller_config(), {}, owner="controller-a")
    c._coordinator = _Assignment(pool.name, "controller-a")
    target = controller_module._POOL.set(pool.name)
    policy = SimpleNamespace(name=pool.name)
    try:
        [row] = await c._resources(pool.id)
        with pytest.raises(TimeoutError):
            await c._reconcile_create(row, policy, fence, Nova())
        operation = await _operation(guest.id, "create")
        assert creates == [guest.id]
        assert operation.request_status == "unknown" and operation.owner == "controller-a"
        assert operation.error_code is None
        [row] = await c._resources(pool.id)
        assert row.observed_state == "unknown"
        await c._reconcile_create(row, policy, fence, Nova())
        assert creates == [guest.id]
        assert await store.occupancy(pool.id) == 1
    finally:
        controller_module._POOL.reset(target)
        c.executor.shutdown(wait=True)


@pytest.mark.parametrize("submitted", [False, True], ids=["assignment-lost-before-sdk", "sdk-raised"])
async def test_delete_gate_failure_stays_retryable_but_submitted_delete_is_not_repeated(pool, monkeypatch, submitted):
    from lumen.services.infrastructure import controller as controller_module
    from lumen.services.infrastructure.controller import PoolLeaseLost
    from lumen.services.infrastructure.providers import DeleteResult, Observation

    current, fence = await store.claim_pool_lease(pool.id, "controller-a", 30)
    guest = await store.request_resource(current, role="worker", request_fingerprint="w" * 64,
        image_ref=current.image_ref, policy_digest=current.profile_digest, deadline=None,
        owner="controller-a", fence=fence)
    factory = get_session_factory()
    async with factory() as session, session.begin():
        row = await session.get(ChatRuntimeResource, guest.id)
        row.provider_id, row.observed_state = "owned-server", "booting"
        (await session.scalar(select(ChatResourceOperation).where(
            ChatResourceOperation.resource_id == guest.id, ChatResourceOperation.action == "create",
        ))).request_status = "succeeded"
    assert await store.request_delete(guest.id, "controller-a", fence) is not None
    deletes, fail = [], {"delete": submitted}

    class Nova:
        def observe(self, ref):
            return Observation(ref.provider_id, "ACTIVE", None, None, {})
        def delete(self, ref):
            deletes.append(ref.provider_id)
            if fail["delete"]:
                raise TimeoutError("delete response lost after submission")
            return DeleteResult(absent=True)

    c = ResourceController(_controller_config(), {}, owner="controller-a")
    c._coordinator = _Assignment(pool.name, "controller-a")
    real_claim = store.claim_operation

    async def claim_then_lose_assignment(*args):
        operation = await real_claim(*args)
        c._coordinator.assignments[pool.name] = "controller-b"
        return operation

    target = controller_module._POOL.set(pool.name)
    policy = SimpleNamespace(name=pool.name)
    try:
        [row] = await c._resources(pool.id)
        if submitted:
            with pytest.raises(TimeoutError):
                await c._reconcile_delete(row, policy, fence, Nova())
            operation = await _operation(guest.id, "delete")
            assert operation.request_status == "unknown" and operation.owner == "controller-a"
            fail["delete"] = False
            # Ambiguous acceptance is observed, never re-sent; ACTIVE is not absence.
            await c._reconcile_delete(row, policy, fence, Nova())
            assert deletes == ["owned-server"] and await store.occupancy(pool.id) == 1
            return
        monkeypatch.setattr(store, "claim_operation", claim_then_lose_assignment)
        with pytest.raises(PoolLeaseLost):
            await c._reconcile_delete(row, policy, fence, Nova())
        monkeypatch.setattr(store, "claim_operation", real_claim)
        assert deletes == []
        operation = await _operation(guest.id, "delete")
        assert operation.request_status == "claimed" and operation.owner == store._UNCLAIMED
        assert operation.attempts == 1 and operation.error_code == "not_submitted"
        c._coordinator.assignments[pool.name] = "controller-a"
        await c._reconcile_delete(row, policy, fence, Nova())
        assert deletes == ["owned-server"] and await store.occupancy(pool.id) == 0
    finally:
        controller_module._POOL.reset(target)
        c.executor.shutdown(wait=True)


@pytest.fixture
async def lazy_chat_sandbox(pool, monkeypatch):
    from lumen.models.chat_infrastructure import ChatAgentReservation, ChatProjectAgentQuota
    from lumen.services import sandbox_binding
    from lumen.services.infrastructure.config import PoolConfig, ProjectQuotaDefaults

    factory = get_session_factory()
    definition = PoolConfig.model_validate_json(json.dumps({
        "name": pool.name, "role": "sandbox", "backend": "kubernetes", "enabled": True,
        "image": "registry.example/sandbox@sha256:" + "a" * 64, "architecture": "x86_64",
        "min_replicas": 0, "max_replicas": 4,
        "sandbox": {"workspace_bytes": 128 * 1024**2, "memory_bytes": 1024**3,
                    "cpu_millis": 1000, "pids_limit": 256},
        "profile": {"backend": "kubernetes", "cluster_profile_id": "cluster",
                    "tool_manifest_file": "/test/tools.json", "tool_manifest_sha256": "b" * 64,
                    "controller_ingress_cidrs": ["192.0.2.11/32"], "bootstrap_egress_cidrs": ["192.0.2.11/32"]},
    }))
    runtime = SimpleNamespace(
        enabled=True, deployment_id=pool.deployment_id, pools=(definition,), pool=lambda _name: definition,
        project_quota_defaults=ProjectQuotaDefaults(max_active_sandboxes=1, max_sandbox_seconds=3600),
    )
    settings = SimpleNamespace(runtime_config=runtime)
    monkeypatch.setattr(sandbox_binding, "get_settings", lambda: settings)
    monkeypatch.setattr("lumen.config.get_settings", lambda: settings)
    async with factory() as session, session.begin():
        row = await session.get(ChatRuntimePool, pool.id)
        row.role, row.backend = "sandbox", "kubernetes"
        row.cloud_profile_id, row.project_id = None, None
        row.cluster_profile_id, row.namespace = "cluster", "runs"
        row.image_ref, row.profile_digest = definition.image, definition.digest()
    project = "lazy-it-" + uuid.uuid4().hex
    run_ids = []

    async def create_run(**values):
        run_id = str(uuid.uuid4())
        run_ids.append(run_id)
        run = ChatRun(
            id=run_id, run_scope="temp", run_kind="completion", execution_mode="chat",
            project_id=project, user_id=project, model_name="fixture-model", capability_snapshot={}, pricing_snapshot={},
            client_request_id=str(uuid.uuid4()), request_fingerprint=uuid.uuid4().hex,
            fingerprint_version=1, execution_protocol_version=2, status="running",
            lease_owner="worker#1", lease_fence=1, lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        for key, value in values.items():
            setattr(run, key, value)
        async with factory() as session, session.begin():
            session.add(run)
        return run_id

    try:
        yield SimpleNamespace(factory=factory, create_run=create_run, project=project, pool=definition)
    finally:
        async with factory() as session, session.begin():
            resource_ids = list((await session.scalars(select(ChatRuntimeResource.id).where(
                ChatRuntimeResource.run_id.in_(run_ids)
            ))).all())
            await session.execute(delete(ChatResourceOperation).where(ChatResourceOperation.resource_id.in_(resource_ids)))
            await session.execute(delete(ChatRuntimeResource).where(ChatRuntimeResource.id.in_(resource_ids)))
            await session.execute(delete(ChatAgentReservation).where(ChatAgentReservation.run_id.in_(run_ids)))
            await session.execute(delete(ChatRunEventRow).where(ChatRunEventRow.run_id.in_(run_ids)))
            for run_id in reversed(run_ids):
                await session.execute(delete(ChatRun).where(ChatRun.id == run_id))
            await session.execute(delete(ChatProjectAgentQuota).where(ChatProjectAgentQuota.project_id == project))


@pytest.mark.parametrize("condition", ["canceled", "terminal", "lease_expired", "takeover", "root_canceled", "deadline"])
async def test_lazy_allocation_rejects_stale_authority_before_mutation(lazy_chat_sandbox, condition):
    from lumen.services.sandbox_binding import SandboxDispatchError, ensure_sandbox_for_run

    f = lazy_chat_sandbox
    fields = {}
    if condition == "canceled":
        fields["cancel_requested_at"] = datetime.now(UTC)
    elif condition == "terminal":
        fields["status"] = "completed"
    elif condition == "lease_expired":
        fields["lease_expires_at"] = datetime.now(UTC) - timedelta(seconds=1)
    elif condition == "takeover":
        fields.update(lease_owner="worker#2", lease_fence=2)
    elif condition == "root_canceled":
        root_id = await f.create_run(cancel_requested_at=datetime.now(UTC))
        fields.update(root_run_id=root_id, parent_run_id=root_id)
    else:
        fields["deadline_at"] = datetime.now(UTC) - timedelta(seconds=1)
    run_id = await f.create_run(**fields)
    with pytest.raises(SandboxDispatchError) as error:
        await ensure_sandbox_for_run(run_id, lease_owner="worker#1", fence=1, pool_name=f.pool.name, wait_timeout=0)
    assert error.value.code == "run_lease_invalid"
    async with f.factory() as session:
        run = await session.get(ChatRun, run_id)
        assert run.assigned_resource_id is None and run.runtime_pool_id is None
        assert await session.scalar(select(ChatRuntimeResource.id).where(ChatRuntimeResource.run_id == run_id)) is None


@pytest.mark.parametrize("ceiling", [None, 30])
async def test_lazy_chat_sandbox_quota_and_cancellation_reuse(lazy_chat_sandbox, ceiling):
    from lumen.models.chat_infrastructure import ChatAgentReservation, ChatProjectAgentQuota
    from lumen.services.durable_runs import lifecycle
    from lumen.services.sandbox_binding import SandboxDispatchError, ensure_sandbox_for_run

    f = lazy_chat_sandbox
    first = await f.create_run(sandbox_seconds_ceiling=ceiling)
    second = await f.create_run(sandbox_seconds_ceiling=ceiling)
    with pytest.raises(SandboxDispatchError) as error:
        await ensure_sandbox_for_run(first, lease_owner="worker#1", fence=1, pool_name=f.pool.name, wait_timeout=0)
    assert error.value.code == "sandbox_timeout"
    async with f.factory() as session:
        run = await session.get(ChatRun, first)
        quota = await session.get(ChatProjectAgentQuota, f.project)
        assert run.credit_ceiling is None and run.sandbox_seconds_ceiling == ceiling
        assert quota.active_sandboxes == 1
        assert quota.sandbox_seconds_reserved == (ceiling or 1800)
    with pytest.raises(SandboxDispatchError) as error:
        await ensure_sandbox_for_run(second, lease_owner="worker#1", fence=1, pool_name=f.pool.name, wait_timeout=0)
    assert error.value.code == "child_budget_exhausted"
    async with f.factory() as session, session.begin():
        run = await session.get(ChatRun, first)
        run.status = "awaiting_input"
        resource = await session.get(ChatRuntimeResource, run.assigned_resource_id)
        resource.observed_state = "ready"
        resource.ready_at = datetime.now(UTC) - timedelta(seconds=5)
    response = await lifecycle.request_cancelled(run_id=first, project_id=f.project, user_id=f.project)
    assert response.status == "canceled"
    async with f.factory() as session:
        quota = await session.get(ChatProjectAgentQuota, f.project)
        run = await session.get(ChatRun, first)
        resource = await session.get(ChatRuntimeResource, run.assigned_resource_id)
        holds = (await session.scalars(select(ChatAgentReservation).where(ChatAgentReservation.run_id == first))).all()
        assert quota.active_sandboxes == 0 and quota.sandbox_seconds_reserved == 0
        assert resource.desired_state == "deleting"
        assert {hold.kind: hold.status for hold in holds} == {"sandbox_slot": "released", "sandbox_seconds": "settled"}
    with pytest.raises(SandboxDispatchError) as error:
        await ensure_sandbox_for_run(second, lease_owner="worker#1", fence=1, pool_name=f.pool.name, wait_timeout=0)
    assert error.value.code == "sandbox_timeout"
    async with f.factory() as session:
        assert (await session.get(ChatProjectAgentQuota, f.project)).active_sandboxes == 1
        assert (await session.get(ChatRun, second)).assigned_resource_id is not None


@pytest.mark.parametrize("forged_dispatch", [False, True])
async def test_expired_approval_denial_resumes_only_authenticated_dispatch(lazy_chat_sandbox, monkeypatch, forged_dispatch):
    from unittest.mock import AsyncMock

    from lumen import crypto
    from lumen.models.chat_runs import ChatToolApproval
    from lumen.services.durable_runs import interactions

    f = lazy_chat_sandbox
    now = datetime.now(UTC)
    monkeypatch.setattr(crypto, "_get_key", lambda: bytes.fromhex("ab" * 32))
    monkeypatch.setattr(interactions, "get_settings", lambda: SimpleNamespace(chat_execution_protocol_version=2))
    monkeypatch.setattr(interactions, "_now", lambda: now)
    monkeypatch.setattr(interactions, "wake_run", AsyncMock())
    run_id = await f.create_run()
    calls = [{
        "call_id": "expired-navigation", "name": "browser_navigate",
        "arguments": json.dumps({"url": "https://example.com/approval-expiry"}),
        "source": "mcp", "effect": "external_mutation", "tool_definition_hash": "a" * 64,
        "config_fingerprint": "b" * 64, "expected_state_revision": 7, "writer_fence": 11,
    }]
    await interactions._reconcile_v2_approval_interrupt(run_id=run_id, owner="worker#1", calls=calls, resume=False)
    async with f.factory() as session, session.begin():
        approval = await session.scalar(select(ChatToolApproval).where(ChatToolApproval.run_id == run_id))
        if forged_dispatch:
            approval.tool_name = "browser_delete_everything"
    now += timedelta(minutes=16)
    if forged_dispatch:
        assert run_id not in await interactions.expire_pending_inputs()
        async with f.factory() as session:
            run = await session.get(ChatRun, run_id)
            approval = await session.scalar(select(ChatToolApproval).where(ChatToolApproval.run_id == run_id))
            event_types = list((await session.scalars(select(ChatRunEventRow.event_type).where(
                ChatRunEventRow.run_id == run_id
            ).order_by(ChatRunEventRow.seq))).all())
            assert run.status == "awaiting_input" and run.lease_owner is None
            assert approval.status == "pending" and approval.decision_hmac is None
            assert "tool.approval_resolved" not in event_types
        return

    assert await interactions.expire_pending_inputs() == [run_id]
    async with f.factory() as session, session.begin():
        run = await session.get(ChatRun, run_id)
        approval = await session.scalar(select(ChatToolApproval).where(ChatToolApproval.run_id == run_id))
        assert run.status == "queued" and run.lease_owner is None and run.lease_expires_at is None
        assert approval.status == "denied" and approval.decided_by_user_id is None
        assert approval.decision_hmac and approval.expected_state_revision == 7 and approval.writer_fence == 11
        run.status, run.lease_owner, run.lease_fence = "running", "worker#2", 2
        run.lease_expires_at = now + timedelta(minutes=1)
    assert await interactions._reconcile_v2_approval_interrupt(
        run_id=run_id, owner="worker#2", calls=calls, resume=True
    ) == [{"call_id": "expired-navigation", "decision": "deny"}]
