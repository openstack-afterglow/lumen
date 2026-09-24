"""Resource scheduler and fenced controller regression coverage."""

from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from lumen.services.infrastructure import controller as controller_module
from lumen.services.infrastructure.config import PoolConfig
from lumen.services.infrastructure.guest_api import dependency_ready
from lumen.services.infrastructure.providers import DeleteResult, Observation
from lumen.services.infrastructure.scheduler import api_desired, gate_scale, worker_desired


class FakeProvider:
    requires_delivery = True

    def __init__(self, *, response: Observation | None = None, error: Exception | None = None):
        self.response = response
        self.error = error
        self.creates = 0
        self.deletes = 0

    def create(self, intent):
        self.creates += 1
        if self.error:
            raise self.error
        return self.response

    def delete(self, ref):
        self.deletes += 1
        return DeleteResult(absent=True)
    def observe(self, ref):
        return self.response



def _controller():
    config = SimpleNamespace(max_parallel_cloud_operations=4, pools=(), tls=None)
    return controller_module.ResourceController(config, {}, owner="one")


def _pool(name="workers", deployment_id="deployment"):
    return SimpleNamespace(id="pool-1", name=name, deployment_id=deployment_id)


def _definition(name="workers"):
    return SimpleNamespace(name=name)


def _resource(state="requested", provider_id=None, *, desired="requested"):
    return SimpleNamespace(
        id="resource-1", generation=1, observed_state=state, desired_state=desired,
        provider_id=provider_id, request_fingerprint="fp-1", policy_digest="policy-1",
        role="worker", run_id=None, address=None, certificate_fingerprint=None,
        image_ref="worker-image", deadline_at=None, logical_project_id=None,
        logical_user_id=None, ingress_member_id=None,
    )


@pytest.mark.asyncio
async def test_competing_controllers_only_claim_one_create(monkeypatch):
    first, second = _controller(), _controller()
    provider = FakeProvider(response=Observation("cloud-1", "active", None, None, {}))
    claims = 0

    async def claim(*args):
        nonlocal claims
        claims += 1
        return SimpleNamespace(id="operation-1") if claims == 1 else None

    async def record(*args):
        return True

    monkeypatch.setattr(controller_module.store, "claim_operation", claim)
    monkeypatch.setattr(controller_module.store, "record_observation", record)
    definition = _definition()
    try:
        await asyncio.gather(
            first._reconcile_resource(_resource(), _pool(), definition, 1, provider, None),
            second._reconcile_resource(_resource(), _pool(), definition, 2, provider, None),
        )
        assert provider.creates == 1
    finally:
        first.executor.shutdown(wait=True)
        second.executor.shutdown(wait=True)


@pytest.mark.asyncio
async def test_lost_create_response_is_unknown_and_late_resource_adopted(monkeypatch):
    reconciler = _controller()
    provider = FakeProvider(error=TimeoutError("create response lost"))
    unknown = []
    observations = []

    async def claim(*args):
        return SimpleNamespace(id="operation-1")

    async def mark(*args, **kwargs):
        unknown.append(args[0])

    async def record(*args):
        observations.append(args[-1])

    monkeypatch.setattr(controller_module.store, "claim_operation", claim)
    monkeypatch.setattr(controller_module.store, "mark_unknown", mark)
    monkeypatch.setattr(controller_module.store, "record_observation", record)
    pool = _pool()
    definition = _definition()
    try:
        # Provider raises: the create is ambiguous, so it is recorded unknown and
        # never resubmitted, even once the operation is claimable again.
        await reconciler._reconcile_resource(_resource(), pool, definition, 1, provider, None)
        assert unknown == ["resource-1"] and provider.creates == 1

        # Empty eventually-consistent listings must not authorize another create.
        reconciler._retry.clear()
        await reconciler._reconcile_resource(_resource("unknown"), pool, definition, 1, provider, [])
        assert provider.creates == 1 and observations == []

        # A late resource surfaces by identity exactly once and is adopted, not recreated.
        late = Observation("cloud-1", "active", None, None, {
            "lumen_deployment": "deployment", "lumen_pool": "workers", "lumen_resource": "resource-1",
            "lumen_generation": "1", "lumen_fingerprint": "fp-1", "lumen_policy": "policy-1",
        })
        await reconciler._reconcile_resource(_resource("unknown"), pool, definition, 1, provider, [late])
        assert observations == [late] and provider.creates == 1
    finally:
        reconciler.executor.shutdown(wait=True)


@pytest.mark.asyncio
async def test_confirmed_absent_delete_releases_occupancy(monkeypatch):
    reconciler = _controller()
    provider = FakeProvider()
    released = []

    async def claim_delete(*args):
        return SimpleNamespace(id="operation-delete")

    async def prove(*args):
        released.append(args[0])

    monkeypatch.setattr(controller_module.store, "claim_operation", claim_delete)
    monkeypatch.setattr(controller_module.store, "prove_absent", prove)
    resource = _resource("ready", "cloud-1", desired="deleting")
    try:
        await reconciler._reconcile_resource(resource, _pool(), _definition(), 1, provider, None)
        assert provider.deletes == 1 and released == ["resource-1"]
    finally:
        reconciler.executor.shutdown(wait=True)


@pytest.mark.asyncio
async def test_asynchronous_delete_waits_for_explicit_absence(monkeypatch):
    reconciler = _controller()
    provider = FakeProvider(response=Observation("cloud-1", "deleting", None, None, {}))
    provider.delete = lambda ref: DeleteResult(absent=False)
    claims = 0
    released = []

    async def claim_delete(*args):
        nonlocal claims
        claims += 1
        return SimpleNamespace(id="operation-delete") if claims == 1 else None

    async def prove(*args):
        released.append(args[0])

    monkeypatch.setattr(controller_module.store, "claim_operation", claim_delete)
    monkeypatch.setattr(controller_module.store, "prove_absent", prove)
    resource = _resource("deleting", "cloud-1", desired="deleting")
    try:
        await reconciler._reconcile_resource(resource, _pool(), _definition(), 1, provider, None)
        assert released == []
        await reconciler._reconcile_resource(resource, _pool(), _definition(), 2, provider, None)
        assert released == []
        provider.response = Observation("cloud-1", "absent", None, None, {})
        await reconciler._reconcile_resource(resource, _pool(), _definition(), 3, provider, None)
        assert released == ["resource-1"] and claims == 3
    finally:
        reconciler.executor.shutdown(wait=True)

def test_worker_formula_and_provisioning_count():
    assert worker_desired(1, 12, 3, 3, 30, 10, 4, 2) == 3
    assert worker_desired(1, 12, 0, 0, 30, 10, 4, 3) == 3
    assert worker_desired(1, 2, 10, 100, 30, 10, 4, 0) == 2


def test_api_formula_uses_latency_only_when_busy():
    assert api_desired(1, 8, 8, 10, 900, 200, 0) == 2
    assert api_desired(1, 8, 1, 10, 900, 200, 0) == 1
    assert api_desired(1, 8, 0, 10, 0, 200, 2) == 2


def test_scaling_gate_hysteresis_and_connection_budget():
    first = gate_scale(target=3, current=1, high_samples=0, low_since=None, now=1,
                       low_utilization=False, safe_to_drain=False, pool_size=4, overflow=1,
                       db_connection_budget=20)
    assert first.desired == 1 and first.high_samples == 1
    blocked = gate_scale(target=5, current=1, high_samples=first.high_samples, low_since=None,
                         now=2, low_utilization=False, safe_to_drain=False, pool_size=4,
                         overflow=1, db_connection_budget=20)
    assert blocked.desired == 1 and blocked.reason == "db_connection_budget_exceeded"
    allowed = gate_scale(target=3, current=1, high_samples=first.high_samples, low_since=None,
                         now=2, low_utilization=False, safe_to_drain=False, pool_size=4,
                         overflow=1, db_connection_budget=20)
    assert allowed.desired == 3
    low = gate_scale(target=1, current=3, high_samples=0, low_since=None, now=10,
                     low_utilization=True, safe_to_drain=True, pool_size=4, overflow=1,
                     db_connection_budget=20)
    assert low.desired == 3 and low.low_since == 10
    elapsed = gate_scale(target=1, current=3, high_samples=0, low_since=low.low_since, now=311,
                         low_utilization=True, safe_to_drain=True, pool_size=4, overflow=1,
                         db_connection_budget=20)
    assert elapsed.desired == 1

def _configured_pool(role: str, backend: str = "nova", **overrides) -> dict:
    data = {
        "name": "managed", "role": role, "backend": backend, "cloud_profile_id": "trusted",
        "image": "guest", "architecture": "x86_64", "network_id": "managed-net",
        "security_group_ids": ["managed-group"], "min_replicas": 1, "max_replicas": 2,
        "profile": ({"backend": "nova", "flavor_id": "flavor", "guest_image_id": "image",
                     "guest_image_hash": "a" * 64} if backend == "nova" else
                    {"backend": "zun", "cpu": 1, "memory_mib": 1024, "pids_limit": 128}),
    }
    if role == "sandbox":
        data["sandbox"] = {"workspace_bytes": 1024 * 1024, "memory_bytes": 64 * 1024 * 1024,
                           "cpu_millis": 100, "pids_limit": 32}
    if role == "api":
        data["ingress"] = {"ingress_pool_id": "octavia", "ingress_vip": "10.0.0.1",
                           "ingress_subnet_id": "subnet", "ingress_member_port": 8012,
                           "api_target_active_requests": 8, "api_target_ttft_ms": 500}
    if role != "sandbox":
        data["db_connection_budget"] = 20
    data.update(overrides)
    return data


def test_pool_roles_and_certificate_budget_are_validated_before_provisioning():
    for role in ("api", "worker", "sandbox"):
        assert PoolConfig.model_validate(_configured_pool(role)).role == role
    for role in ("api", "worker", "sandbox"):
        with pytest.raises(ValueError, match="Zun"):
            PoolConfig.model_validate(_configured_pool(role, "zun"))
    with pytest.raises(ValueError, match="safety margin"):
        PoolConfig.model_validate(_configured_pool("api", max_lifetime_seconds=3000))
    with pytest.raises(ValueError, match="api_readiness_port"):
        PoolConfig.model_validate(_configured_pool("api", ingress={
            **_configured_pool("api")["ingress"], "api_readiness_port": 8012,
        }))


@pytest.mark.asyncio
async def test_api_requires_real_dependency_readiness_before_admission(monkeypatch):
    reconciler = _controller()
    responses = [b'{"status":"unavailable","database":false,"plugins":true}',
                 b'{"status":"ok","database":true,"plugins":true,"checkpointer":null,'
                 b'"active_requests":2,"active_sse":1,"p95_ttft_ms":320,"ttft_samples":1}']
    calls = []

    class Probe:
        async def request(self, **kwargs):
            calls.append(kwargs)
            return responses.pop(0)

    async def ready(*args, **kwargs):
        calls.append("admitted")
        return True

    reconciler.transport = Probe()
    monkeypatch.setattr(controller_module.store, "mark_ready", ready)
    async def record(*args, **kwargs):
        calls.append(("measured", kwargs["active_count"]))
        return True
    monkeypatch.setattr(controller_module.store, "record_api_load", record)
    resource = _resource("booting", "cloud-1")
    resource.role = "api"
    resource.address = "10.0.0.2"
    resource.port = 8013
    resource.certificate_fingerprint = "a" * 64
    resource.created_at = datetime.now(UTC)
    definition = SimpleNamespace(name="api", max_lifetime_seconds=1800)
    reconciler.config.listen_port = 8013
    try:
        await reconciler._advance_readiness(resource, _pool("api"), 1, definition)
        assert "admitted" not in calls
        reconciler._retry.clear()
        await reconciler._advance_readiness(resource, _pool("api"), 1, definition)
        assert "admitted" in calls and ("measured", 2) in calls
        assert reconciler._api_load[resource.id][1:] == (2, 320)
        assert all(call["port"] == 8013 for call in calls if isinstance(call, dict))
    finally:
        reconciler.executor.shutdown(wait=True)


@pytest.mark.asyncio
async def test_unhealthy_api_replica_withdraws_ingress_before_reprovision(monkeypatch):
    reconciler = _controller()
    withdrawn = []
    resource = _resource("ready", "cloud-1")
    resource.role, resource.address, resource.port = "api", "10.0.0.2", 8013
    resource.certificate_fingerprint = "a" * 64
    resource.ingress_member_id = "member-1"
    resource.created_at = datetime.now(UTC)
    definition = SimpleNamespace(name="api", max_lifetime_seconds=1800)
    reconciler.config.listen_port = 8013

    class Probe:
        async def request(self, **_kwargs):
            return b'{"status":"unavailable","database":false,"plugins":true}'

    async def cloud(fn, *args):
        fn(*args)

    async def unavailable(*_args):
        withdrawn.append("unavailable")
        return True

    reconciler.transport = Probe()
    reconciler.ingresses["api"] = SimpleNamespace(drain=lambda member: withdrawn.append(member))
    reconciler._cloud = cloud
    monkeypatch.setattr(controller_module.store, "mark_api_unavailable", unavailable)
    try:
        await reconciler._advance_readiness(resource, _pool("api"), 1, definition)
        assert resource.observed_state == "unavailable"
        assert withdrawn == ["member-1", "unavailable"]
        assert resource.id not in reconciler._api_load
    finally:
        reconciler.executor.shutdown(wait=True)


def test_api_health_body_rejects_false_and_malformed_status():
    assert dependency_ready(b'{"status":"ok","database":true,"plugins":true,"checkpointer":null}')
    assert not dependency_ready(b'{"status":"ok","database":true,"plugins":true,"checkpointer":false}')
    assert not dependency_ready(b'{"status":"unavailable","database":true,"plugins":true}')
    assert not dependency_ready(b'not json')


@pytest.mark.asyncio
async def test_retirement_preserves_active_http_requests_after_grace(monkeypatch):
    reconciler = _controller()
    drains = []
    deletions = []
    active = [1]

    class Ingress:
        def drain(self, member):
            drains.append(member)

        def register(self, *args):
            raise AssertionError("expired resource must not rejoin ingress")

    class Probe:
        async def request(self, **_kwargs):
            return ('{"status":"ok","database":true,"plugins":true,"checkpointer":null,'
                    f'"active_requests":{active[0]},"active_sse":0,"p95_ttft_ms":null,"ttft_samples":0}}').encode()

    async def record(*args, active_count):
        resource.active_slots = active_count
        return True

    async def delete(resource_id, owner, fence):
        deletions.append((resource_id, owner, fence))

    monkeypatch.setattr(controller_module.store, "record_api_load", record)
    monkeypatch.setattr(controller_module.store, "request_delete", delete)
    reconciler.transport = Probe()
    reconciler.config.listen_port = 8013
    reconciler.config.reconcile_interval_seconds = 5
    reconciler.ingresses["api"] = Ingress()
    resource = _resource("ready", "cloud-1")
    resource.role = "api"
    resource.address = "10.0.0.2"
    resource.port = 8013
    resource.certificate_fingerprint = "a" * 64
    resource.ingress_member_id = "member-1"
    resource.active_slots = 1
    definition = SimpleNamespace(name="api", max_lifetime_seconds=600, drain_seconds=120)
    try:
        resource.created_at = datetime.now(UTC) - timedelta(seconds=800)
        await reconciler._advance_readiness(resource, _pool("api"), 7, definition)
        await reconciler._retire_expired([resource], 7, definition)
        assert drains == ["member-1"] and deletions == []
        active[0] = 0
        await reconciler._advance_readiness(resource, _pool("api"), 7, definition)
        await reconciler._retire_expired([resource], 7, definition)
        assert deletions == [("resource-1", "one", 7)]
    finally:
        reconciler.executor.shutdown(wait=True)


@pytest.mark.asyncio
async def test_cancelled_fenced_ingress_write_is_held_until_sdk_thread_settles():
    reconciler = _controller()
    entered, release, effects = threading.Event(), threading.Event(), []

    def enable():
        entered.set()
        release.wait(10)
        effects.append("enabled")
        return "member-1"

    try:
        write = asyncio.create_task(reconciler._settled(enable))
        assert await asyncio.to_thread(entered.wait, 10)
        write.cancel()
        await asyncio.sleep(0.1)
        # The caller's pool fence transaction must stay open while the write can still land.
        assert not write.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await write
        assert effects == ["enabled"]
    finally:
        release.set()
        reconciler.executor.shutdown(wait=True)
