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
    assert kwargs[0]["api_timeout"] == 15
    assert calls[0][1] == {"timeout": (5, 15), "connect_retries": 0, "status_code_retries": 0}


@pytest.mark.parametrize("selected_cloud", [None, "ambient", "envvars"])
def test_cloud_connection_real_sdk_ignores_ambient_auth_and_scope(monkeypatch, tmp_path, selected_cloud):
    import os

    from keystoneauth1.identity.v3 import ApplicationCredential
    from openstack.config import loader
    from requests.sessions import Session

    from lumen.services.infrastructure.config import CloudProfile, SecretRef

    # Redirect every SDK search path and clear inherited OS_* values so the
    # regression never depends on developer credentials or host clouds.yaml.
    for name in tuple(os.environ):
        if name.startswith("OS_"):
            monkeypatch.delenv(name)
    clouds_file = tmp_path / "clouds.yaml"
    clouds_file.write_text(json.dumps({"clouds": {"ambient": {
        "auth_type": "v3applicationcredential",
        "auth": {
            "auth_url": "https://file-keystone.invalid/v3",
            "application_credential_id": "file-credential",
            "application_credential_secret": "file-secret",
            "application_credential_name": "file-credential-name",
            "user_id": "file-user",
            "project_id": "file-project",
        },
        "region_name": "FileRegion",
        "interface": "admin",
        "insecure": True,
    }}}), encoding="utf-8")
    monkeypatch.setattr(loader, "CONFIG_FILES", [str(clouds_file)])
    monkeypatch.setattr(loader, "SECURE_FILES", [])
    monkeypatch.setattr(loader, "VENDOR_FILES", [])
    ambient = {
        "OS_CLIENT_CONFIG_FILE": str(clouds_file),
        "OS_AUTH_TYPE": "v3applicationcredential",
        "OS_AUTH_URL": "https://env-keystone.invalid/v3",
        "OS_APPLICATION_CREDENTIAL_ID": "env-credential",
        "OS_APPLICATION_CREDENTIAL_SECRET": "env-secret",
        "OS_APPLICATION_CREDENTIAL_NAME": "env-credential-name",
        "OS_USER_ID": "caller-user",
        "OS_PROJECT_ID": "caller-project",
        "OS_PROJECT_NAME": "caller-project-name",
        "OS_PROJECT_DOMAIN_ID": "caller-domain",
        "OS_REGION_NAME": "EnvRegion",
        "OS_INTERFACE": "public",
        "OS_INSECURE": "true",
    }
    if selected_cloud is not None:
        ambient["OS_CLOUD"] = selected_cloud
    for name, value in ambient.items():
        monkeypatch.setenv(name, value)

    # Prove these fixtures reach the installed SDK when ambient loading is on.
    ambient_config = loader.OpenStackConfig().get_one()
    source = "file" if selected_cloud == "ambient" else "env"
    assert ambient_config.get_auth_args()["application_credential_id"] == f"{source}-credential"
    assert ambient_config.get_auth().project_id == ("file-project" if source == "file" else "caller-project")

    def no_network(*args, **kwargs):
        raise AssertionError("cloud connection construction must not perform HTTP")

    monkeypatch.setattr(Session, "request", no_network)
    monkeypatch.setenv("SCALING_TEST_SECRET", "operator-secret")
    cloud = CloudProfile(
        id="operator", auth_url="https://operator-keystone.invalid/v3",
        project_id="operator-project", region_name="OperatorRegion", interface="internal",
        application_credential_id="operator-credential",
        application_credential_secret=SecretRef(env="SCALING_TEST_SECRET"), purpose="trusted",
    )
    conn = nova.cloud_connection(cloud)
    try:
        assert conn.config.get_auth_args() == {
            "auth_url": cloud.auth_url,
            "application_credential_id": cloud.application_credential_id,
            "application_credential_secret": "operator-secret",
        }
        auth = conn.session.auth
        assert isinstance(auth, ApplicationCredential)
        assert auth is conn.config.get_auth()
        assert auth.auth_url == cloud.auth_url
        # Application credentials supply their own project binding; neither
        # the profile's bookkeeping project nor caller scope is sent to auth.
        assert not auth.has_scope_parameters
        assert not auth.unscoped
        assert len(auth.auth_methods) == 1
        assert auth.auth_methods[0].get_auth_data(conn.session, auth, {}, {}) == (
            "application_credential", {"id": "operator-credential", "secret": "operator-secret"},
        )
        assert conn.config.region_name == cloud.region_name
        assert conn.config.config["interface"] == cloud.interface
        assert conn.session.verify is True
        assert conn.session.timeout == 15
        assert conn.config.get_connect_retries("compute") == 0
        assert conn.config.get_status_code_retries("compute") == 0
    finally:
        conn.close()


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
