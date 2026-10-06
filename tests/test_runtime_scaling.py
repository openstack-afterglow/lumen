"""Scheduling/scale-in behavior; cloud and DB effects are isolated in unit tests."""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from lumen.api.agent_runtime import _pool_view
from lumen.services.infrastructure import controller as module
from lumen.services.infrastructure import nova, store
from lumen.services.infrastructure.providers import Observation
from lumen.services.infrastructure.scheduler import capacity_limit, gate_scale


def definition(role="worker", **overrides):
    values = dict(name="pool", role=role, max_replicas=8, max_surge=1,
                  boot_timeout_seconds=60, drain_seconds=120, max_lifetime_seconds=600,
                  idle_seconds=90, db_connections_per_process=30, db_connection_budget=180,
                  pg_connections_per_process=2, pg_connection_budget=12, cold_service_time_ms=30000,
                  ingress=SimpleNamespace(api_target_active_requests=8, api_target_ttft_ms=500))
    return SimpleNamespace(**(values | overrides))


def resource(state="ready", **overrides):
    now = datetime.now(UTC)
    values = dict(id="resource", pool_id="pool", generation=1, role="worker", provider_id="server",
                  observed_state=state, desired_state="requested", accepting=True, active_slots=0,
                  idle_since=now - timedelta(seconds=100), created_at=now, deadline_at=now + timedelta(seconds=60),
                  drain_requested_at=None, drain_ack_at=None, drain_reason=None, failure_code=None,
                  ready_at=now if state == "ready" else None, address="10.0.0.2", port=8013,
                  certificate_fingerprint="a" * 64, certificate_not_after=now + timedelta(hours=1),
                  pending_certificate_fingerprint=None, previous_certificate_fingerprint=None,
                  bootstrap_token_hash=None, bootstrap_expires_at=None, run_id=None)
    return SimpleNamespace(**(values | overrides))


def controller():
    config = SimpleNamespace(max_parallel_cloud_operations=4, pools=(), tls=None,
                             listen_port=8013, reconcile_interval_seconds=5)
    return module.ResourceController(config, {}, owner="controller")


@pytest.mark.parametrize("db,pg,expected", [(180, 100, 5), (1000, 8, 3), (30, 0, 0)])
def test_reserved_db_and_pg_partial_targets(db, pg, expected):
    assert capacity_limit(maximum=8, db_budget=db, db_per_process=30,
                          pg_budget=pg, pg_per_process=2, surge=1) == expected
    decision = gate_scale(target=8, current=0, high_samples=1, low_since=None, now=10,
                          low_utilization=False, safe_to_drain=False, pool_size=30, overflow=0,
                          db_connection_budget=db, pg_connection_budget=pg,
                          pg_connections_per_process=2, replacement_reserve=1)
    assert decision.desired == expected and decision.reason == "db_budget"


def test_hysteresis_resets_high_and_requires_full_low_window():
    args = dict(current=3, high_samples=0, pool_size=30, overflow=0, db_connection_budget=300,
                low_utilization=True, safe_to_drain=True)
    assert gate_scale(target=1, low_since=10, now=309, **args).desired == 3
    assert gate_scale(target=1, low_since=10, now=310, **args).desired == 1
    args["low_utilization"] = False
    assert gate_scale(target=1, low_since=10, now=400, **args).low_since is None
    args.update(current=1, high_samples=0)
    first = gate_scale(target=4, low_since=None, now=1, **args)
    assert first.desired == 1 and first.high_samples == 1
    interrupted = gate_scale(target=1, low_since=None, now=2, **(args | {"high_samples": 1}))
    assert interrupted.high_samples == 0


def test_replacement_threshold_and_budget_does_not_authorize_demand_surge():
    now = datetime.now(UTC)
    guest = resource(created_at=now - timedelta(seconds=419))
    assert not module.ResourceController._retirement_due(guest, definition(), now=now)
    guest.created_at = now - timedelta(seconds=420)
    assert module.ResourceController._retirement_due(guest, definition(), now=now)
    assert module.ResourceController._physical_limit(definition()) == 6
    assert capacity_limit(maximum=8, db_budget=180, db_per_process=30,
                          pg_budget=12, pg_per_process=2, surge=1) == 5


@pytest.mark.asyncio
async def test_one_idle_guest_can_drain_while_other_guest_is_busy(monkeypatch):
    c = controller()
    idle = resource(id="idle")
    busy = resource(id="busy", idle_since=None, active_slots=1)
    short = resource(id="short-idle", idle_since=datetime.now(UTC) - timedelta(seconds=89))
    resources = [idle, busy, short]
    pool = SimpleNamespace(id="pool", min_replicas=1, max_replicas=8, high_demand_samples=0,
                           low_demand_since=datetime.now(UTC) - timedelta(seconds=301),
                           slots_per_worker=4, target_wait_seconds=10)
    drains, projections = [], []
    async def estimate(*args, **kwargs):
        return 30000
    async def demand(*args):
        return 1, 0, None
    async def healthy(*args):
        return {guest.id for guest in resources}
    async def snapshot(*args):
        return resources
    async def begin(resource_id, *args, **kwargs):
        drains.append((resource_id, kwargs))
        idle.observed_state = "draining"
        idle.drain_requested_at = datetime.now(UTC)
        idle.accepting = False
        return idle
    async def advance(*args):
        pass
    async def project(*args, **kwargs):
        projections.append(kwargs)
    monkeypatch.setattr(store, "observe_service_time", estimate)
    monkeypatch.setattr(store, "eligible_worker_demand", demand)
    monkeypatch.setattr(store, "healthy_worker_resources", healthy)
    monkeypatch.setattr(store, "begin_drain", begin)
    monkeypatch.setattr(store, "write_projection", project)
    c._resources, c._advance_drain = snapshot, advance
    try:
        await c._scale(pool, 1, definition(), resources)
        assert drains == [("idle", {"reason": "idle_window", "idle_seconds": 90})]
        assert projections[-1]["ready"] == 2
        assert projections[-1]["draining"] == 1
        assert projections[-1]["reason"] == "drain_pending"
    finally:
        c.executor.shutdown(wait=True)


@pytest.mark.asyncio
async def test_stale_worker_is_occupancy_but_not_healthy_capacity(monkeypatch):
    c = controller()
    resources = [resource(id="live", idle_since=None), resource(id="stale", idle_since=None)]
    pool = SimpleNamespace(id="pool", min_replicas=1, max_replicas=8, high_demand_samples=0,
                           low_demand_since=None, slots_per_worker=4, target_wait_seconds=10)
    projections = []
    async def estimate(*args, **kwargs):
        return 30000
    async def demand(*args):
        return 0, 0, None
    async def healthy(*args):
        return {"live"}
    async def snapshot(*args):
        return resources
    async def project(*args, **kwargs):
        projections.append(kwargs)
    monkeypatch.setattr(store, "observe_service_time", estimate)
    monkeypatch.setattr(store, "eligible_worker_demand", demand)
    monkeypatch.setattr(store, "healthy_worker_resources", healthy)
    monkeypatch.setattr(store, "write_projection", project)
    c._resources = snapshot
    try:
        await c._scale(pool, 1, definition(), resources)
        assert projections[-1]["desired"] == 1
        assert projections[-1]["ready"] == 1
        assert projections[-1]["reason"] == "telemetry_stale"
        assert all(guest.desired_state == "requested" for guest in resources)
    finally:
        c.executor.shutdown(wait=True)


@pytest.mark.asyncio
async def test_boot_timeout_needs_verified_ownership_and_never_reclaims_serving_guest(monkeypatch):
    c = controller()
    reclaimed = []
    verified = [False]
    async def record(*args):
        return verified[0]
    async def cleanup(*args):
        reclaimed.append(args)
        return True
    class Provider:
        requires_delivery = False
        def observe(self, ref):
            return Observation(ref.provider_id, "ACTIVE", None, None, {})
    monkeypatch.setattr(store, "record_observation", record)
    monkeypatch.setattr(store, "mark_failed_with_cleanup", cleanup)
    guest = resource("booting", deadline_at=datetime.now(UTC) - timedelta(seconds=1))
    try:
        await c._reconcile_observe(guest, definition(), 3, Provider())
        assert reclaimed == []
        verified[0] = True
        await c._reconcile_observe(guest, definition(), 3, Provider())
        assert reclaimed[-1][-1] == "boot_timeout"
        reclaimed.clear()
        guest.ready_at = datetime.now(UTC)
        await c._reconcile_observe(guest, definition(), 3, Provider())
        assert reclaimed == []
    finally:
        c.executor.shutdown(wait=True)


@pytest.mark.asyncio
async def test_api_counts_http_plus_ws_without_double_counting_sse(monkeypatch):
    c = controller()
    guest = resource(role="api")
    measured = []
    class Probe:
        async def request(self, **kwargs):
            return json.dumps({"ready": True, "draining": False, "load": {
                "active_requests": 2, "active_sse": 2, "active_ws": 3, "ttft_samples": 0,
                "p95_ttft_ms": None, "observed_at": datetime.now(UTC).isoformat()}}).encode()
    async def record(*args, **kwargs):
        measured.append(kwargs["active_count"])
        return True
    c.transport = Probe()
    monkeypatch.setattr(store, "record_api_load", record)
    try:
        assert await c._probe_api_load(guest, 1, definition("api"))
        assert measured == [5] and c._api_load[guest.id][1] == 5
    finally:
        c.executor.shutdown(wait=True)


@pytest.mark.asyncio
async def test_independent_api_and_three_worker_pool_reconciles(monkeypatch):
    c = controller()
    definitions = [SimpleNamespace(name=name, enabled=True) for name in ("api", "text", "media", "batch")]
    c.config.pools = definitions
    c.providers = {d.name: object() for d in definitions}
    c._pool_ids = {d.name: d.name for d in definitions}
    entered = set()
    all_entered = asyncio.Event()
    async def claim(pool_id, owner, seconds):
        assert seconds == 30
        return SimpleNamespace(id=pool_id, name=pool_id), 1
    async def reconcile(pool, fence, policy):
        entered.add(pool.name)
        if len(entered) == 4:
            all_entered.set()
        await asyncio.wait_for(all_entered.wait(), 1)
        if pool.name == "media":
            raise RuntimeError("one pool failing must not suppress the others")
    monkeypatch.setattr(store, "claim_pool_lease", claim)
    c.reconcile_pool = reconcile
    try:
        await c.tick()
        assert entered == {"api", "text", "media", "batch"}
    finally:
        c.executor.shutdown(wait=True)


@pytest.mark.asyncio
async def test_lease_loss_refuses_new_cloud_io(monkeypatch):
    c = controller()
    effects = []
    async def lost(*args):
        return False
    monkeypatch.setattr(store, "pool_lease_owned", lost)
    token = module._LEASE.set(("pool", 1))
    try:
        with pytest.raises(module.PoolLeaseLost):
            await c._cloud(lambda: effects.append("called"))
        assert effects == []
    finally:
        module._LEASE.reset(token)
        c.executor.shutdown(wait=True)


def test_cloud_http_boundary_overrides_sdk_write_retry_defaults(monkeypatch):
    calls = []
    class Session:
        def request(self, *args, **kwargs):
            calls.append((args, kwargs))
            return "response"
    session = Session()
    kwargs = []
    def connection(**values):
        kwargs.append(values)
        return SimpleNamespace(session=session)
    monkeypatch.setattr(nova, "Connection", connection)
    monkeypatch.setenv("SCALING_TEST_SECRET", "test-only")
    cloud = SimpleNamespace(application_credential_secret=SimpleNamespace(env="SCALING_TEST_SECRET"),
                            auth_url="https://keystone.invalid", application_credential_id="credential",
                            region_name="RegionOne", interface="internal", ca_file=None)
    conn = nova.cloud_connection(cloud)
    assert conn.session.request("/servers", "POST", connect_retries=1, status_code_retries=3) == "response"
    assert kwargs[0]["api_timeout"] == (5, 15)
    assert calls[0][1] == {"timeout": (5, 15), "connect_retries": 0, "status_code_retries": 0}


def test_admin_pool_view_exposes_projection_without_secret_material():
    now = datetime.now(UTC)
    values = dict(id="pool", name="batch", role="worker", backend="nova", enabled=True,
                  cloud_profile_id="trusted", region_name="region", image_ref="image", profile_digest="digest",
                  min_replicas=0, max_replicas=8, max_surge=1, workload_class="batch", idle_seconds=90,
                  desired_replicas=3, ready_replicas=1, provisioning_replicas=2, draining_replicas=1,
                  queued_count=8, oldest_queued_seconds=11, last_scale_reason="demand_high", last_reconciled_at=now,
                  slots_per_worker=4, target_wait_seconds=10, desired_revision=1, reconcile_lease_owner="leader",
                  reconcile_fence=7, service_time_estimate_ms=60000, updated_at=now)
    view = _pool_view(SimpleNamespace(**values))
    for name in ("desired_replicas", "ready_replicas", "provisioning_replicas", "draining_replicas",
                 "queued_count", "oldest_queued_seconds", "last_scale_reason"):
        assert view[name] == values[name]
    assert view["last_reconciled_at"] == now.isoformat()
    assert not any("secret" in key or "exception" in key for key in view)
