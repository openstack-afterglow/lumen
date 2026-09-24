"""Real MariaDB tests for durable resource intent and worker registrations."""

from __future__ import annotations

import asyncio
import os
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from lumen.db import close_db, get_session_factory, init_db
from lumen.models.chat_infrastructure import (
    ChatResourceOperation,
    ChatRuntimePool,
    ChatRuntimeResource,
    ChatWorkerRegistration,
)
from lumen.models.chat_runs import ChatRun
from lumen.services.infrastructure import store
from lumen.services.infrastructure.controller import ResourceController
from lumen.services.infrastructure.ingress import MemberState
from lumen.services.infrastructure.store import ResourceUnavailable
from lumen.services.run_store import claim_queued_run

pytestmark = pytest.mark.integration


@pytest.fixture
async def pool():
    init_db(os.environ["DATABASE_URL"], pool_size=3, max_overflow=1)
    factory = get_session_factory()
    pool_id = str(uuid.uuid4())
    row = ChatRuntimePool(id=pool_id, deployment_id="it-" + uuid.uuid4().hex,
                          name="workers", role="worker", backend="nova", enabled=True,
                          cloud_profile_id="trusted", project_id="operator-project",
                          region_name="RegionOne", image_ref="sha256:" + "a" * 64,
                          profile_digest="b" * 64, min_replicas=0, max_replicas=4)
    async with factory() as session, session.begin():
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
                                                schema_version=1)
    assert await store.heartbeat_worker(registration, active_count=2, accepting=True)
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
    return resource.id


def _registration(**overrides):
    return {
        "worker_identity": "it-" + uuid.uuid4().hex,
        "boot_id": str(uuid.uuid4()), "capacity": 4,
        "protocol_versions": [2], "plugin_digest": "d" * 64,
        "schema_version": 1, **overrides,
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
    assert await store.heartbeat_worker(registration, active_count=2, accepting=True)
    async with factory() as session:
        resource = await session.get(ChatRuntimeResource, resource_id)
        assert resource.active_slots == 2


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
    assert await store.heartbeat_worker(registration, active_count=1, accepting=True)

    async with factory() as session, session.begin():
        setattr(await session.get(ChatRuntimeResource, resource_id), changed_field, new_value)
    assert not await store.heartbeat_worker(registration, active_count=3, accepting=False)
    async with factory() as session:
        worker = await session.get(ChatWorkerRegistration, registration)
        resource = await session.get(ChatRuntimeResource, resource_id)
        assert (worker.active_count, worker.accepting, resource.active_slots) == (1, True, 1)


async def test_worker_heartbeat_rejects_orphaned_registration(pool):
    resource_id = await _worker_resource(pool)
    factory = get_session_factory()
    async with factory() as session, session.begin():
        (await session.get(ChatRuntimeResource, resource_id)).certificate_fingerprint = "a" * 64
    registration = await store.register_worker(**_registration(
        resource_id=resource_id, resource_generation=1, certificate_fingerprint="a" * 64))
    assert await store.heartbeat_worker(registration, active_count=1, accepting=True)

    async with factory() as session, session.begin():
        operations = (await session.execute(select(ChatResourceOperation).where(
            ChatResourceOperation.resource_id == resource_id))).scalars().all()
        for operation in operations:
            await session.delete(operation)
        await session.flush()
        await session.delete(await session.get(ChatRuntimeResource, resource_id))

    assert not await store.heartbeat_worker(registration, active_count=2, accepting=False)
    async with factory() as session:
        worker = await session.get(ChatWorkerRegistration, registration)
        assert (worker.resource_id, worker.resource_generation, worker.active_count, worker.accepting) == (
            None, 1, 1, True,
        )


async def test_recycled_worker_generation_requires_new_boot_registration(pool):
    resource_id = await _worker_resource(pool)
    factory = get_session_factory()
    async with factory() as session, session.begin():
        (await session.get(ChatRuntimeResource, resource_id)).certificate_fingerprint = "a" * 64
    identity = _registration(resource_id=resource_id, resource_generation=1,
                             certificate_fingerprint="a" * 64)
    old_registration = await store.register_worker(**identity)
    assert await store.heartbeat_worker(old_registration, active_count=1, accepting=True)

    async with factory() as session, session.begin():
        resource = await session.get(ChatRuntimeResource, resource_id)
        resource.generation = 2
        resource.certificate_fingerprint = "e" * 64
    assert not await store.heartbeat_worker(old_registration, active_count=2, accepting=False)
    next_identity = identity | {"resource_generation": 2, "certificate_fingerprint": "e" * 64}
    with pytest.raises(ResourceUnavailable, match="worker_identity_mismatch"):
        await store.register_worker(**next_identity)

    new_registration = await store.register_worker(**(next_identity | {"boot_id": str(uuid.uuid4())}))
    assert new_registration != old_registration
    assert await store.heartbeat_worker(new_registration, active_count=2, accepting=True)
    async with factory() as session:
        old = await session.get(ChatWorkerRegistration, old_registration)
        new = await session.get(ChatWorkerRegistration, new_registration)
        assert (old.active_count, old.accepting, old.draining) == (1, False, True)
        assert (new.resource_generation, new.certificate_fingerprint, new.active_count, new.accepting) == (
            2, "e" * 64, 2, True,
        )


async def test_static_worker_registration_has_no_resource_fence(pool):
    registration = await store.register_worker(**_registration())
    assert await store.heartbeat_worker(registration, active_count=2, accepting=True)
    async with get_session_factory()() as session:
        row = await session.get(ChatWorkerRegistration, registration)
        assert (row.resource_id, row.resource_generation, row.certificate_fingerprint, row.pool_id) == (
            None, None, None, None,
        )
        assert (row.active_count, row.accepting) == (2, True)
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
            required_plugin_digest="e" * 64, runtime_pool_id=pool.id,
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
        role="api", name=pool.name, boot_timeout_seconds=600,
        ingress=SimpleNamespace(api_target_active_requests=8, api_target_ttft_ms=500),
    )
    controller = ResourceController(
        SimpleNamespace(max_parallel_cloud_operations=4, pools=(), tls=None, reconcile_interval_seconds=5),
        {}, owner="api-controller", db_pool_size=3, db_overflow=1,
    )
    try:
        current, fence = claim
        resource = await store.request_resource(
            current, role="api", request_fingerprint="a" * 64,
            image_ref=current.image_ref, policy_digest=current.profile_digest, deadline=None,
            owner="api-controller", fence=fence,
        )
        async with factory() as session, session.begin():
            (await session.get(ChatRuntimeResource, resource.id)).observed_state = "ready"
        assert await store.record_api_load(resource.id, resource.generation, "api-controller", fence, active_count=18)
        controller._api_load[resource.id] = (time.monotonic(), 18, 700)
        await controller._scale(current, fence, definition, await controller._resources(pool.id))
        assert await store.occupancy(pool.id) == 1
        async with factory() as session:
            assert (await session.get(ChatRuntimePool, pool.id)).high_demand_samples == 1

        current, fence = await store.claim_pool_lease(pool.id, "api-controller", 30)
        await controller._scale(current, fence, definition, await controller._resources(pool.id))
        assert await store.occupancy(pool.id) == 4
        controller._api_load[resource.id] = (time.monotonic() - 60, 0, None)
        current, fence = await store.claim_pool_lease(pool.id, "api-controller", 30)
        await controller._scale(current, fence, definition, await controller._resources(pool.id))
        assert await store.occupancy(pool.id) == 4
        async with factory() as session:
            rows = (await session.execute(select(ChatRuntimeResource).where(
                ChatRuntimeResource.pool_id == pool.id))).scalars().all()
            assert all(row.desired_state != "deleting" for row in rows)
    finally:
        controller.executor.shutdown(wait=True)


async def test_overdue_worker_drain_preserves_active_lease_until_heartbeat_reports_idle(pool):
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
    assert await store.heartbeat_worker(registration, active_count=1, accepting=True)
    claim = await store.claim_pool_lease(pool.id, "worker-controller", 30)
    assert claim is not None
    _, fence = claim
    controller = ResourceController(
        SimpleNamespace(max_parallel_cloud_operations=4, pools=(), tls=None),
        {}, owner="worker-controller",
    )
    definition = SimpleNamespace(name=pool.name, max_lifetime_seconds=600, drain_seconds=120)
    try:
        await controller._retire_expired(await controller._resources(pool.id), fence, definition)
        async with factory() as session:
            row = await session.get(ChatRuntimeResource, resource_id)
            worker = await session.get(ChatWorkerRegistration, registration)
            assert row.desired_state != "deleting"
            assert row.active_slots == 1
            assert worker.draining and not worker.accepting

        assert await store.heartbeat_worker(registration, active_count=0, accepting=False)
        await controller._retire_expired(await controller._resources(pool.id), fence, definition)
        async with factory() as session:
            assert (await session.get(ChatRuntimeResource, resource_id)).desired_state == "deleting"
    finally:
        controller.executor.shutdown(wait=True)


class _Octavia:
    """One fake Octavia member whose create/enable can be held open inside the SDK thread."""

    def __init__(self, existing: str | None):
        self.member_id = existing
        self.enabled = False
        self.calls: list = []
        self.entered = threading.Event()
        self.release = threading.Event()

    def inspect(self, resource_id, address):
        return None if self.member_id is None else MemberState(self.member_id, True, self.enabled)

    def register(self, resource_id, address):
        self.calls.append("register")
        self.entered.set()
        if not self.release.wait(30):
            raise TimeoutError("register was never released")
        self.member_id, self.enabled = "member-1", True
        self.calls.append("enabled")
        return MemberState("member-1", True, True)

    def drain(self, member_id):
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
            await second._cloud(octavia.drain, row.ingress_member_id)
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
