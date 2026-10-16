"""Resource scheduler and fenced controller regression coverage."""

from __future__ import annotations

import asyncio
import json
import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from lumen.config import Settings
from lumen.services.infrastructure import controller as controller_module
from lumen.services.infrastructure.config import GuestProfile, PoolConfig, RenewalPolicy, RuntimeConfig
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
    reconciler = controller_module.ResourceController(config, {}, owner="one")
    # Provider lifecycle tests isolate DB authorization; real-DB tests cover fences.
    async def fenced(resource, fence, function, *args, **kwargs):
        return await reconciler._cloud(function, *args)
    reconciler._fenced_cloud = fenced
    return reconciler


def _pool(name="workers", deployment_id="deployment"):
    return SimpleNamespace(id="pool-1", name=name, deployment_id=deployment_id)


def _definition(name="workers"):
    return SimpleNamespace(name=name)


def _resource(state="requested", provider_id=None, *, desired="requested"):
    return SimpleNamespace(
        id="resource-1", generation=1, observed_state=state, desired_state=desired,
        provider_id=provider_id, request_fingerprint="fp-1", policy_digest="policy-1",
        role="worker", run_id=None, address=None, certificate_fingerprint=None,
        certificate_not_after=datetime.now(UTC) + timedelta(hours=1),
        image_ref="worker-image", deadline_at=None, logical_project_id=None,
        logical_user_id=None, ingress_member_id=None,
        pool_id="pool-1", active_slots=0, accepting=True, idle_since=None,
        drain_requested_at=None, drain_ack_at=None, drain_reason=None, ready_at=None,
        bootstrap_token_hash=None, heartbeat_at=None, failure_code=None,
        created_at=datetime.now(UTC),
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
    assert blocked.desired == 4 and blocked.reason == "db_budget"
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


def test_pool_roles_and_renewable_lifetime_policy_are_validated_before_provisioning():
    for role in ("api", "worker", "sandbox"):
        assert PoolConfig.model_validate(_configured_pool(role)).role == role
    for role in ("api", "worker", "sandbox"):
        with pytest.raises(ValueError, match="Zun"):
            PoolConfig.model_validate(_configured_pool(role, "zun"))
    api = PoolConfig.model_validate(_configured_pool("api"))
    assert (api.idle_seconds, api.max_lifetime_seconds, api.boot_timeout_seconds, api.drain_seconds) == (
        300, 86400, 600, 300,
    )
    assert PoolConfig.model_validate(_configured_pool("api", max_lifetime_seconds=7200)).max_lifetime_seconds == 7200
    assert PoolConfig.model_validate(_configured_pool("sandbox")).max_lifetime_seconds == 1800
    with pytest.raises(ValueError, match="boot and replacement drain"):
        PoolConfig.model_validate(_configured_pool("api", max_lifetime_seconds=900))
    with pytest.raises(ValueError, match="api_readiness_port"):
        PoolConfig.model_validate(_configured_pool("api", ingress={
            **_configured_pool("api")["ingress"], "api_readiness_port": 8012,
        }))


@pytest.mark.parametrize("workload,estimate", [
    ("online_text", 30000), ("online_media", 120000), ("batch", 60000),
])
def test_worker_class_selects_cold_estimate(workload, estimate):
    pool = PoolConfig.model_validate(_configured_pool("worker", workload_class=workload))
    assert pool.workload_class == workload
    assert pool.cold_service_time_ms == estimate
    assert PoolConfig.model_validate(pool.model_dump()).workload_class == workload
    assert PoolConfig.model_validate(_configured_pool("api")).workload_class is None


@pytest.mark.parametrize("role", ["api", "sandbox"])
@pytest.mark.parametrize("policy", [{"workload_class": "batch"}, {"cold_service_time_ms": 60000}])
def test_worker_policy_cannot_leak_into_other_roles(role, policy):
    with pytest.raises(ValueError, match="only worker"):
        PoolConfig.model_validate(_configured_pool(role, **policy))


@pytest.mark.parametrize("policy", [
    {"workload_class": "realtime"}, {"idle_seconds": 0}, {"drain_seconds": 0},
    {"boot_timeout_seconds": 3601}, {"max_lifetime_seconds": 2592001},
    {"max_surge": -1}, {"cold_service_time_ms": 0}, {"max_replicas": float("inf")},
    {"workload_class": None}, {"cold_service_time_ms": None},
])
def test_pool_policy_has_finite_typed_bounds(policy):
    with pytest.raises(ValueError):
        PoolConfig.model_validate(_configured_pool("worker", **policy))


def _guest_profile(role="worker"):
    names = ["DATABASE_URL", "REDIS_URL", "LUMEN_ENCRYPTION_KEY"]
    return {
        "id": "service", "role": role, "config_file": "/etc/lumen/runtime/service.conf",
        "config_sha256": "b" * 64, "config_keys": ["service_chat_enabled"],
        "secret_env_names": names, "secrets": {name: {"env": name} for name in names},
        "image": "registry.example/lumen@sha256:" + "a" * 64,
        "plugin_digest": "c" * 64, "schema_version": 22, "protocol_version": 2,
    }


def _enabled_runtime():
    guest = _guest_profile()
    pool = _configured_pool(
        "worker", enabled=True, guest_profile_id=guest["id"], image=guest["image"],
        db_connection_budget=100, pg_connection_budget=20, pg_connections_per_process=5,
    )
    return {
        "enabled": True, "controller_url": "https://controller.example:8013",
        "tls": {"ca_file": "/ca", "ca_key_file": "/ca-key", "cert_file": "/cert",
                "key_file": "/key", "operator_client_cert_file": "/probe-cert",
                "operator_client_key_file": "/probe-key"},
        "dispatch_key": {"file": "/dispatch-key"}, "managed_networks": ["10.0.0.0/24"],
        "cloud_profiles": [{"id": "trusted", "purpose": "trusted", "project_id": "trusted-project",
                            "auth_url": "https://keystone.example/v3", "region_name": "RegionOne",
                            "application_credential_id": "credential",
                            "application_credential_secret": {"file": "/cloud-secret"}}],
        "pools": [pool], "guest_profiles": [guest], "workload_pools": {"online_text": "managed"},
        "db_connection_budget": 200, "pg_connection_budget": 100,
        "fixed_db_connection_reserve": 30, "controller_db_connection_reserve": 30,
        "fixed_pg_connection_reserve": 10, "controller_pg_connection_reserve": 5,
    }


def test_enabled_runtime_requires_renewal_and_partitioned_connection_budgets():
    runtime = RuntimeConfig.model_validate(_enabled_runtime())
    assert runtime.renewal.leaf_ttl_seconds == 3600
    assert runtime.renewal.renew_interval_seconds == 1200
    assert runtime.renewal.overlap_seconds == 120
    assert runtime.pool("managed").max_lifetime_seconds == 86400
    assert runtime.pool("managed").max_surge == 1
    assert runtime.public_view()["pools"][0]["workload_class"] == "online_text"
    assert "guest_profiles" not in runtime.public_view()
    assert RuntimeConfig().enabled is False


@pytest.mark.parametrize("policy,reason", [
    ({"renewal": {"enabled": False}}, "identity renewal"),
    ({"db_connection_budget": None}, "deployment DB and PG budgets"),
    ({"pg_connection_budget": None}, "deployment DB and PG budgets"),
    ({"db_connection_budget": 159}, "reservations exceed deployment"),
    ({"pg_connection_budget": 34}, "reservations exceed deployment"),
    ({"controller_db_connection_reserve": 0}, "controller DB reserve"),
    ({"workload_pools": {}}, "explicit workload_pools"),
    ({"workload_pools": {"batch": "managed"}}, "matching worker pool"),
    ({"managed_networks": []}, "managed_networks"),
    ({"controller_url": "http://controller.example"}, "https controller_url"),
])
def test_enabled_runtime_rejects_missing_or_conflicting_prerequisites(policy, reason):
    with pytest.raises(ValueError, match=reason):
        RuntimeConfig.model_validate({**_enabled_runtime(), **policy})


def test_pool_minima_and_replacement_reservations_preserve_online_capacity():
    values = _enabled_runtime()["pools"][0]
    assert PoolConfig.model_validate({**values, "workload_class": "batch", "min_replicas": 0}).min_replicas == 0
    for workload in ("online_text", "online_media"):
        with pytest.raises(ValueError, match="online worker pools"):
            PoolConfig.model_validate({**values, "workload_class": workload, "min_replicas": 0})
    with pytest.raises(ValueError, match="minimum replicas and replacement surge"):
        PoolConfig.model_validate({**values, "db_connection_budget": 59})
    with pytest.raises(ValueError, match="minimum replicas and replacement surge"):
        PoolConfig.model_validate({**values, "pg_connection_budget": 9})
    with pytest.raises(ValueError, match="guest_profile_id"):
        PoolConfig.model_validate({**values, "guest_profile_id": None})
    api = _configured_pool("api", enabled=True, guest_profile_id="service", min_replicas=0)
    with pytest.raises(ValueError, match="api pools require min_replicas"):
        PoolConfig.model_validate(api)


@pytest.mark.parametrize("policy", [
    {"overlap_seconds": 0}, {"overlap_seconds": 120, "renew_interval_seconds": 120},
    {"leaf_ttl_seconds": 7200}, {"renew_interval_seconds": 3001},
    {"overlap_seconds": 600, "renew_interval_seconds": 3000},
])
def test_renewal_windows_are_finite_and_leave_unexpired_activation_time(policy):
    with pytest.raises(ValueError):
        RenewalPolicy.model_validate(policy)


def test_guest_profiles_require_reference_only_scoped_delivery():
    guest = GuestProfile.model_validate(_guest_profile())
    assert guest.secrets["DATABASE_URL"].env == "DATABASE_URL"
    assert len(guest.digest()) == 64
    with pytest.raises(ValueError, match="exactly match"):
        GuestProfile.model_validate({**_guest_profile(), "secret_env_names": []})
    with pytest.raises(ValueError, match="secret references"):
        GuestProfile.model_validate({**_guest_profile(), "secret_env_names": [], "secrets": {}})
    with pytest.raises(ValueError, match="runtime_config"):
        GuestProfile.model_validate({**_guest_profile(), "config_keys": ["runtime_config"]})
    values = _guest_profile()
    values["secret_env_names"].append("OS_APPLICATION_CREDENTIAL_SECRET")
    values["secrets"]["OS_APPLICATION_CREDENTIAL_SECRET"] = {"file": "/cloud-secret"}
    with pytest.raises(ValueError, match="controller credentials"):
        GuestProfile.model_validate(values)
    with pytest.raises(ValueError):
        GuestProfile.model_validate({**_guest_profile(), "secrets": {"DATABASE_URL": "plaintext"}})
    with pytest.raises(ValueError, match="matching role/image guest profile"):
        RuntimeConfig.model_validate({**_enabled_runtime(), "guest_profiles": [_guest_profile("api")]})


def test_enabled_runtime_rejects_duplicate_class_pool_and_unknown_guest_profile():
    values = _enabled_runtime()
    values["pools"].append({**values["pools"][0], "name": "duplicate"})
    with pytest.raises(ValueError, match="exactly one enabled pool"):
        RuntimeConfig.model_validate(values)
    values = _enabled_runtime()
    values["pools"][0]["guest_profile_id"] = "missing"
    with pytest.raises(ValueError, match="matching role/image guest profile"):
        RuntimeConfig.model_validate(values)


def test_managed_batch_requires_online_coordinator_but_not_in_every_worker_process():
    values = _enabled_runtime()
    with pytest.raises(ValueError, match="online_text coordinator and batch pools"):
        Settings(runtime_config=values, batch_enabled=True, worker_workload_classes=["batch"])
    values["pools"].append({
        **values["pools"][0], "name": "batch", "workload_class": "batch", "min_replicas": 0,
    })
    values["workload_pools"]["batch"] = "batch"
    values["db_connection_budget"] = 300
    settings = Settings(runtime_config=values, batch_enabled=True, worker_workload_classes=["batch"])
    assert settings.runtime_config.pool("batch").min_replicas == 0
    assert settings.worker_workload_classes == ["batch"]
    values["pools"] = [values["pools"][1]]
    values["workload_pools"] = {"batch": "batch"}
    with pytest.raises(ValueError, match="online_text coordinator"):
        Settings(runtime_config=values, batch_enabled=True, worker_workload_classes=["batch"])



@pytest.mark.asyncio
async def test_api_requires_real_dependency_readiness_before_admission(monkeypatch):
    reconciler = _controller()
    responses = [b'{"status":"unavailable","database":false,"plugins":true}',
                 json.dumps({"ready": True, "draining": False, "load": {
                     "active_requests": 2, "active_sse": 1, "active_ws": 0,
                     "observed_at": datetime.now(UTC).isoformat(), "p95_ttft_ms": 320,
                     "ttft_samples": 1}}).encode()]
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
        assert "admitted" not in calls and ("measured", 2) in calls
        assert resource.observed_state == "booting"  # ACTIVE ingress is still required.
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
    reconciler.ingresses["api"] = SimpleNamespace(drain=lambda member, *identity: withdrawn.append(member))
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
async def test_retirement_waits_for_ready_replacement_before_drain(monkeypatch):
    reconciler = _controller()
    resource = _resource("ready", "cloud-1")
    resource.created_at = datetime.now(UTC) - timedelta(seconds=800)
    replacement = _resource("booting", "cloud-2")
    replacement.id = "resource-2"
    replacement.drain_reason = "replacement:" + resource.id
    drains = []
    async def begin(*args, **kwargs):
        drains.append(args[0])
        return resource
    monkeypatch.setattr(controller_module.store, "begin_drain", begin)
    healthy_ids = {resource.id}
    async def healthy(*args):
        return healthy_ids
    monkeypatch.setattr(controller_module.store, "healthy_worker_resources", healthy)
    definition = SimpleNamespace(name="workers", role="worker", max_lifetime_seconds=600,
                                 boot_timeout_seconds=60, drain_seconds=120, max_surge=1)
    try:
        await reconciler._retire_expired([resource, replacement], 7, definition)
        assert drains == []
        replacement.observed_state = "ready"
        await reconciler._retire_expired([resource, replacement], 7, definition)
        assert drains == []  # An observed-ready guest with a stale registration cannot replace capacity.
        healthy_ids.add(replacement.id)
        await reconciler._retire_expired([resource, replacement], 7, definition)
        assert drains == [resource.id]
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
