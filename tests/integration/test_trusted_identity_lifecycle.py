"""Real MariaDB trusted identity transitions under both snapshot isolation modes."""
from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from sqlalchemy import delete, event, select, text, update

from lumen.db import close_db, get_session_factory, init_db
from lumen.models.chat_infrastructure import (
    ChatResourceOperation,
    ChatRuntimePool,
    ChatRuntimeResource,
    ChatWorkerRegistration,
)
from lumen.models.chat_runs import ChatRun
from lumen.services.infrastructure import identity, store
from lumen.services.infrastructure.config import RenewalPolicy, RuntimeConfig, TlsMaterial
from lumen.services.infrastructure.transport import resource_identity

pytestmark = pytest.mark.integration


def _csr():
    key = ec.generate_private_key(ec.SECP256R1())
    # Untrusted CSR identity extensions must never survive issuance.
    return (x509.CertificateSigningRequestBuilder().subject_name(x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "attacker")]))
        .add_extension(x509.SubjectAlternativeName([x509.UniformResourceIdentifier("spiffe://attacker")]), critical=False)
        .sign(key, hashes.SHA256()).public_bytes(serialization.Encoding.PEM).decode())


@pytest.fixture(params=["ON", "OFF"])
async def identity_db(request, tmp_path):
    now = datetime.now(UTC)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Identity test CA")])
    ca = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=2))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=False,
                       data_encipherment=False, key_agreement=False, key_cert_sign=True, crl_sign=True,
                       encipher_only=False, decipher_only=False), critical=True).sign(key, hashes.SHA256()))
    ca_path, key_path = tmp_path / "ca.pem", tmp_path / "ca-key.pem"
    ca_path.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                         serialization.NoEncryption()))
    key_path.chmod(0o600)
    config = RuntimeConfig(tls=TlsMaterial(ca_file=str(ca_path), ca_key_file=str(key_path),
                          cert_file=str(ca_path), key_file=str(key_path)), renewal=RenewalPolicy())
    init_db(os.environ["DATABASE_URL"], pool_size=4, max_overflow=0)
    factory = get_session_factory()
    engine = factory.kw["bind"]

    def configure_snapshot(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute(f"SET SESSION innodb_snapshot_isolation = {request.param}")
        cursor.close()

    event.listen(engine.sync_engine, "connect", configure_snapshot)
    pool = ChatRuntimePool(id=str(uuid4()), deployment_id="identity-it-" + uuid4().hex,
        name="workers", role="worker", backend="nova", enabled=True, cloud_profile_id="trusted",
        project_id="operator", region_name="RegionOne", image_ref="registry/worker@sha256:" + "a" * 64,
        profile_digest="b" * 64, min_replicas=1, max_replicas=4, workload_class="online_text",
        guest_profile_id="worker", guest_profile_digest="e" * 64)
    resource = ChatRuntimeResource(id=str(uuid4()), pool_id=pool.id, generation=1, role="worker", backend="nova",
        desired_state="requested", observed_state="ready", request_fingerprint="c" * 64,
        cloud_profile_id="trusted", cloud_project_id="operator", image_ref=pool.image_ref,
        policy_digest=pool.profile_digest, certificate_fingerprint="a" * 64,
        certificate_not_after=now + timedelta(hours=1), guest_profile_id="worker", guest_profile_digest="e" * 64)
    registration = ChatWorkerRegistration(id=str(uuid4()), worker_identity="identity-it-" + uuid4().hex,
        boot_id=str(uuid4()), resource_id=resource.id, resource_generation=1, pool_id=pool.id,
        certificate_fingerprint="a" * 64, protocol_versions=[2], workload_classes=["online_text"],
        plugin_digest="d" * 64, schema_version=1, capacity=2)
    async with factory() as session, session.begin():
        actual = (await session.execute(text("SELECT @@SESSION.innodb_snapshot_isolation"))).scalar_one()
        assert bool(actual) == (request.param == "ON")
        session.add(pool)
        await session.flush()
        session.add(resource)
        await session.flush()
        session.add(registration)
    peer = resource_identity("worker", resource.id, resource.generation)

    async def renew(request_id=None, csr=None, pin="a" * 64, peer_identity=None):
        return await identity.renew_identity(config, client_identity=peer_identity or peer,
            client_fingerprint=pin, renewal_request_id=request_id or str(uuid4()), csr_pem=csr or _csr())

    async def activate(request_id, pin, peer_identity=None):
        return await identity.activate_identity(config, client_identity=peer_identity or peer,
            client_fingerprint=pin, renewal_request_id=request_id)

    async def change(**values):
        async with factory() as session, session.begin():
            row = await session.get(ChatRuntimeResource, resource.id)
            for name, value in values.items():
                setattr(row, name, value)

    try:
        yield SimpleNamespace(factory=factory, config=config, pool=pool, resource=resource,
                              registration=registration, peer=peer, renew=renew, activate=activate,
                              change=change, now=now)
    finally:
        async with factory() as session, session.begin():
            await session.execute(update(ChatRuntimeResource).where(
                ChatRuntimeResource.pool_id == pool.id).values(run_id=None))
            await session.execute(delete(ChatRun).where(ChatRun.worker_pool_id == pool.id))
            await session.execute(delete(ChatWorkerRegistration).where(ChatWorkerRegistration.pool_id == pool.id))
            await session.execute(delete(ChatResourceOperation).where(ChatResourceOperation.resource_id.in_(
                select(ChatRuntimeResource.id).where(ChatRuntimeResource.pool_id == pool.id))))
            await session.execute(delete(ChatRuntimeResource).where(ChatRuntimeResource.pool_id == pool.id))
            await session.execute(delete(ChatRuntimePool).where(ChatRuntimePool.id == pool.id))
        event.remove(engine.sync_engine, "connect", configure_snapshot)
        await close_db()


async def test_renew_is_concurrently_idempotent_replaces_pending_and_rejects_csr_reuse(identity_db):
    db = identity_db
    request_id, csr = str(uuid4()), _csr()
    results = await asyncio.gather(db.renew(request_id, csr), db.renew(request_id, csr))
    assert results[0] == results[1]
    cert = x509.load_pem_x509_certificate(results[0]["certificate_pem"].encode())
    assert cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(
        x509.UniformResourceIdentifier) == [db.peer]
    usage = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert ExtendedKeyUsageOID.CLIENT_AUTH in usage and ExtendedKeyUsageOID.SERVER_AUTH in usage
    assert results[0]["not_after"] - results[0]["pending_expires_at"] == 3480
    with pytest.raises(identity.IdentityRejected) as mismatch:
        await db.renew(request_id, _csr())
    assert (mismatch.value.status, mismatch.value.code) == (409, "renewal_conflict")
    replacement_id = str(uuid4())
    replacement = await db.renew(replacement_id)
    assert replacement["certificate_pem"] != results[0]["certificate_pem"]
    old_pin = cert.fingerprint(hashes.SHA256()).hex()
    with pytest.raises(identity.IdentityRejected):
        await db.activate(request_id, old_pin)
    async with db.factory() as session:
        row = await session.get(ChatRuntimeResource, db.resource.id)
        assert row.certificate_fingerprint == "a" * 64
        assert row.renewal_request_id == replacement_id


async def test_activation_is_pending_only_atomic_and_replay_safe(identity_db, monkeypatch):
    db = identity_db
    request_id = str(uuid4())
    pending = await db.renew(request_id)
    pin = x509.load_pem_x509_certificate(pending["certificate_pem"].encode()).fingerprint(hashes.SHA256()).hex()
    with pytest.raises(identity.IdentityRejected):
        await db.activate(request_id, "a" * 64)
    with pytest.raises(identity.IdentityRejected):
        await db.activate(request_id, "f" * 64)
    activated = await db.activate(request_id, pin)
    assert activated == await db.activate(request_id, pin)
    async with db.factory() as session:
        row = await session.get(ChatRuntimeResource, db.resource.id)
        registration = await session.get(ChatWorkerRegistration, db.registration.id)
        assert row.certificate_fingerprint == registration.certificate_fingerprint == pin
        assert row.previous_certificate_fingerprint == "a" * 64
        assert row.pending_certificate_fingerprint is row.pending_certificate_pem is None
        assert row.pending_certificate_not_after is row.pending_certificate_expires_at is None
        assert row.renewal_request_id == request_id
        boundary = identity._utc(row.previous_certificate_valid_until)
    assert await store.heartbeat_worker(db.registration.id, accepting=True, certificate_fingerprint="a" * 64)
    monkeypatch.setattr(identity, "_now", lambda: boundary)
    assert not await store.heartbeat_worker(db.registration.id, accepting=True, certificate_fingerprint="a" * 64)
    assert await store.heartbeat_worker(db.registration.id, accepting=True, certificate_fingerprint=pin)
    with pytest.raises(identity.IdentityRejected) as already:
        await db.renew(request_id, csr="invalid", pin=pin)
    assert already.value.code == "invalid_identity_request"


async def test_expired_pending_cannot_activate_and_same_request_can_recover(identity_db):
    db = identity_db
    request_id, csr = str(uuid4()), _csr()
    first = await db.renew(request_id, csr)
    pin = x509.load_pem_x509_certificate(first["certificate_pem"].encode()).fingerprint(hashes.SHA256()).hex()
    await db.change(pending_certificate_expires_at=datetime.now(UTC) - timedelta(seconds=1))
    with pytest.raises(identity.IdentityRejected):
        await db.activate(request_id, pin)
    second = await db.renew(request_id, csr)
    assert second["certificate_pem"] != first["certificate_pem"]
    new_pin = x509.load_pem_x509_certificate(second["certificate_pem"].encode()).fingerprint(hashes.SHA256()).hex()
    await db.activate(request_id, new_pin)
    with pytest.raises(identity.IdentityRejected) as already:
        await db.renew(request_id, csr, pin=new_pin)
    assert (already.value.status, already.value.code) == (409, "renewal_already_activated")


async def test_draining_worker_requires_live_lease_or_active_auxiliary_step(identity_db):
    db = identity_db
    await db.change(desired_state="draining", observed_state="draining", accepting=False)
    with pytest.raises(identity.IdentityRejected) as empty:
        await db.renew()
    assert (empty.value.status, empty.value.code) == (409, "renewal_not_required")
    run = ChatRun(id=str(uuid4()), run_scope="temp", user_id="owner", project_id="identity-it",
        model_name="fixture", capability_snapshot={}, pricing_snapshot={}, client_request_id=str(uuid4()),
        request_fingerprint="f" * 64, fingerprint_version=1, status="running", workload_class="online_text",
        worker_pool_id=db.pool.id, worker_registration_id=db.registration.id, lease_owner="owned#1",
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=5))
    async with db.factory() as session, session.begin():
        session.add(run)
    assert (await db.renew())["certificate_pem"]
    async with db.factory() as session, session.begin():
        (await session.get(ChatRun, run.id)).lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    with pytest.raises(identity.IdentityRejected, match="renewal_not_required"):
        await db.renew()
    async with db.factory() as session, session.begin():
        (await session.get(ChatWorkerRegistration, db.registration.id)).auxiliary_active = 1
    assert (await db.renew())["certificate_pem"]


async def test_draining_api_requires_verified_fresh_counter_snapshot(identity_db):
    db = identity_db
    await db.change(role="api", desired_state="draining", observed_state="draining", accepting=False,
                    api_counter_snapshot={"active_requests": 1, "active_sse": 1, "active_ws": 0},
                    api_counter_snapshot_at=datetime.now(UTC))
    api_peer = resource_identity("api", db.resource.id, 1)
    assert (await db.renew(peer_identity=api_peer))["certificate_pem"]
    for snapshot, age in ((None, 0), ({"active_requests": 0, "active_sse": 0, "active_ws": 0}, 0),
                          ({"active_requests": 1, "active_sse": 1, "active_ws": 0}, 21),
                          ({"active_requests": 1, "active_sse": 2, "active_ws": 0}, 0)):
        await db.change(api_counter_snapshot=snapshot,
                        api_counter_snapshot_at=datetime.now(UTC) - timedelta(seconds=age))
        with pytest.raises(identity.IdentityRejected, match="renewal_not_required"):
            await db.renew(peer_identity=api_peer)


@pytest.mark.parametrize("state", ["deleting", "deleted", "failed"])
async def test_invalid_resource_states_cannot_renew(identity_db, state):
    db = identity_db
    await db.change(**({"desired_state": state} if state == "deleting" else {"observed_state": state}))
    with pytest.raises(identity.IdentityRejected, match="identity_unavailable"):
        await db.renew()


async def test_previous_peer_reregistration_preserves_current_registration_pin(identity_db):
    db = identity_db
    request_id = str(uuid4())
    result = await db.renew(request_id)
    pin = x509.load_pem_x509_certificate(result["certificate_pem"].encode()).fingerprint(hashes.SHA256()).hex()
    await db.activate(request_id, pin)
    registration_id = await store.register_worker(
        worker_identity=db.registration.worker_identity, boot_id=db.registration.boot_id,
        capacity=2, protocol_versions=[2], plugin_digest="d" * 64, schema_version=1,
        workload_classes=["online_text"], resource_id=db.resource.id, resource_generation=1,
        certificate_fingerprint="a" * 64)
    async with db.factory() as session:
        registration = await session.get(ChatWorkerRegistration, registration_id)
        assert registration.certificate_fingerprint == pin


async def test_new_resources_freeze_profile_identity_from_locked_pool(identity_db):
    db = identity_db
    created = await store.request_resource(db.pool, role="worker", request_fingerprint="f" * 64,
        image_ref=db.pool.image_ref, policy_digest=db.pool.profile_digest, deadline=None)
    assert (created.guest_profile_id, created.guest_profile_digest) == ("worker", "e" * 64)
    async with db.factory() as session, session.begin():
        pool = await session.get(ChatRuntimePool, db.pool.id)
        pool.guest_profile_digest = "f" * 64
    async with db.factory() as session:
        frozen = await session.get(ChatRuntimeResource, created.id)
        assert frozen.guest_profile_digest == "e" * 64


async def test_guest_config_denies_pending_foreign_sandbox_and_changed_profile(identity_db):
    db = identity_db
    result = await db.renew()
    pending_pin = x509.load_pem_x509_certificate(result["certificate_pem"].encode()).fingerprint(hashes.SHA256()).hex()
    peers = ((db.peer, pending_pin), (db.peer, "f" * 64),
             (resource_identity("worker", str(uuid4()), 1), "a" * 64),
             (resource_identity("worker", db.resource.id, 2), "a" * 64),
             (resource_identity("sandbox", db.resource.id, 1), "a" * 64))
    for peer, pin in peers:
        with pytest.raises(identity.IdentityRejected) as denied:
            await identity.guest_config(db.config, client_identity=peer, client_fingerprint=pin)
        assert (denied.value.status, denied.value.code) == (403, "identity_unavailable")
    with pytest.raises(identity.IdentityRejected) as changed:
        await identity.guest_config(db.config, client_identity=db.peer, client_fingerprint="a" * 64)
    assert (changed.value.status, changed.value.code) == (409, "guest_profile_changed")
    await db.change(certificate_not_after=None)
    with pytest.raises(identity.IdentityRejected, match="identity_unavailable"):
        await db.renew()


async def test_activation_and_heartbeat_serialize_without_reverting_current_pin(identity_db):
    db = identity_db
    request_id = str(uuid4())
    result = await db.renew(request_id)
    pin = x509.load_pem_x509_certificate(result["certificate_pem"].encode()).fingerprint(hashes.SHA256()).hex()
    activated, heartbeat = await asyncio.gather(db.activate(request_id, pin),
        store.heartbeat_worker(db.registration.id, accepting=True, certificate_fingerprint="a" * 64,
                               resource_generation=1))
    assert activated["activated"] and heartbeat
    async with db.factory() as session:
        row = await session.get(ChatRuntimeResource, db.resource.id)
        registration = await session.get(ChatWorkerRegistration, db.registration.id)
        assert row.certificate_fingerprint == registration.certificate_fingerprint == pin
    assert not await store.heartbeat_worker(db.registration.id, accepting=True,
                                            certificate_fingerprint=pin, resource_generation=2)


async def test_dispatch_requires_exact_claiming_registration_even_when_worker_names_collide(identity_db, monkeypatch):
    from lumen.services.infrastructure.config import SecretRef
    from lumen.services.infrastructure.dispatch import DispatchRejected, authorize_dispatch

    db = identity_db
    now, run_id = datetime.now(UTC), str(uuid4())
    other = ChatRuntimeResource(id=str(uuid4()), pool_id=db.pool.id, generation=1, role="worker", backend="nova",
        desired_state="requested", observed_state="ready", request_fingerprint="d" * 64,
        cloud_profile_id="trusted", cloud_project_id="operator", image_ref=db.pool.image_ref,
        policy_digest=db.pool.profile_digest, certificate_fingerprint="b" * 64,
        certificate_not_after=now + timedelta(hours=1))
    other_registration = ChatWorkerRegistration(id=str(uuid4()), worker_identity=db.registration.worker_identity,
        boot_id=str(uuid4()), resource_id=other.id, resource_generation=1, pool_id=db.pool.id,
        certificate_fingerprint="b" * 64, protocol_versions=[2], workload_classes=["online_text"],
        plugin_digest="d" * 64, schema_version=1, capacity=2, heartbeat_at=now)
    run = ChatRun(id=run_id, run_scope="temp", user_id="owner", project_id="identity-it",
        model_name="fixture", capability_snapshot={}, pricing_snapshot={}, client_request_id=str(uuid4()),
        request_fingerprint="f" * 64, fingerprint_version=1, status="running", workload_class="online_text",
        worker_pool_id=db.pool.id, worker_registration_id=other_registration.id,
        lease_owner=db.registration.worker_identity + "#1", lease_fence=1,
        lease_expires_at=now + timedelta(minutes=5))
    sandbox = ChatRuntimeResource(id=str(uuid4()), pool_id=db.pool.id, generation=1, role="sandbox", backend="nova",
        run_id=run_id, desired_state="requested", observed_state="ready", request_fingerprint="s" * 64,
        cloud_profile_id="sandbox", cloud_project_id="sandbox", image_ref=db.pool.image_ref,
        policy_digest=db.pool.profile_digest, certificate_fingerprint="c" * 64,
        certificate_not_after=now + timedelta(hours=1), address="10.42.0.10", port=8013)
    async with db.factory() as session, session.begin():
        session.add(other)
        await session.flush()
        session.add(other_registration)
        await session.flush()
        session.add(run)
        await session.flush()
        session.add(sandbox)
        await session.flush()
        run.assigned_resource_id = sandbox.id
        (await session.get(ChatWorkerRegistration, db.registration.id)).heartbeat_at = now

    monkeypatch.setenv("LUMEN_TEST_DISPATCH_KEY", "test-only-operator-key-" + "x" * 32)
    config = db.config.model_copy(update={"dispatch_key": SecretRef(env="LUMEN_TEST_DISPATCH_KEY")})
    arguments = dict(client_identity=db.peer, client_fingerprint="a" * 64,
        run_id=run.id, lease_owner=run.lease_owner, resource_id=sandbox.id, generation=1,
        method="GET", path="/v1/artifacts/" + str(uuid4()))
    with pytest.raises(DispatchRejected, match="dispatch unavailable"):
        await authorize_dispatch(config, **arguments)

    async with db.factory() as session, session.begin():
        (await session.get(ChatRun, run.id)).worker_registration_id = db.registration.id
    grant = await authorize_dispatch(config, **arguments)
    assert grant.address == sandbox.address and grant.capability


@pytest.fixture
async def selected_dispatch_db(identity_db, tmp_path, monkeypatch):
    """Reuse real identity rows; exercise only controller capability authorization."""
    from lumen.services.infrastructure import dispatch, transport
    from lumen.services.infrastructure.config import KubernetesClusterProfile, PoolConfig, SecretRef
    from lumen.services.sandbox_selection import freeze_selection

    db = identity_db
    now = db.now.replace(microsecond=0)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now if tz is not None else now.replace(tzinfo=None)

    monkeypatch.setattr(dispatch, "datetime", Clock)
    monkeypatch.setattr(dispatch, "time", SimpleNamespace(time=lambda: now.timestamp()))
    monkeypatch.setattr(transport, "time", SimpleNamespace(time=lambda: now.timestamp()))
    monkeypatch.setenv("LUMEN_SELECTED_DISPATCH_KEY", "test-only-selected-dispatch-" + "x" * 32)
    manifest = {"version": 1, "profiles": [{
        "id": "playwright", "command": ["/usr/bin/node", "/srv/playwright.js"], "environment": {},
        "tools": [{"name": name, "description": "Isolated browser fixture",
                   "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
                   "effect": "external_mutation"} for name in ("browser_navigate", "browser_click")],
    }]}
    manifest_path = tmp_path / "dispatch-tools.json"
    manifest_bytes = json.dumps(manifest, separators=(",", ":")).encode()
    manifest_path.write_bytes(manifest_bytes)
    configured = PoolConfig.model_validate({
        "name": "selected-browser", "role": "sandbox", "backend": "kubernetes", "enabled": True,
        "image": "registry.example/sandbox@sha256:" + "a" * 64, "architecture": "x86_64",
        "min_replicas": 0, "max_replicas": 2,
        "sandbox": {"workspace_bytes": 128 * 1024**2, "memory_bytes": 1024**3,
                    "cpu_millis": 1000, "pids_limit": 256},
        "profile": {"backend": "kubernetes", "cluster_profile_id": "dispatch-cluster",
                    "tool_manifest_file": str(manifest_path),
                    "tool_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
                    "controller_ingress_cidrs": ("192.0.2.11/32",),
                    "bootstrap_egress_cidrs": ("192.0.2.11/32",)},
    })
    cluster = KubernetesClusterProfile(
        id="dispatch-cluster", server="https://192.0.2.1:6443", ca_file="/test/ca.pem",
        bearer_token=SecretRef(env="UNUSED_DISPATCH_CLUSTER_TOKEN"), namespace="dispatch-runs",
        runtime_class_name="gvisor", runtime_class_handler="runsc", cluster_cidrs=("10.42.0.0/16",),
        qualification_file="/test/qualification.json", qualification_sha256="b" * 64,
    )
    config = RuntimeConfig(
        enabled=True, deployment_id=db.pool.deployment_id, controller_url="https://192.0.2.11:8013",
        tls=db.config.tls.model_copy(update={"operator_client_cert_file": str(tmp_path / "dispatch-probe.pem"),
                                          "operator_client_key_file": str(tmp_path / "dispatch-probe.key")}),
        dispatch_key=SecretRef(env="LUMEN_SELECTED_DISPATCH_KEY"), managed_networks=("192.0.2.0/24",),
        pools=(configured,), kubernetes_clusters=(cluster,),
    )
    selection = freeze_selection(["browser_navigate"], config=config)
    pool = ChatRuntimePool(
        id=str(uuid4()), deployment_id=config.deployment_id, name=configured.name, role="sandbox",
        backend="kubernetes", enabled=True, cluster_profile_id=cluster.id, namespace=cluster.namespace,
        image_ref=configured.image, profile_digest=configured.digest(), min_replicas=0, max_replicas=2,
    )

    def make_run(**values):
        return ChatRun(
            id=str(uuid4()), run_scope="temp", user_id="owner", project_id="identity-it",
            model_name="fixture", capability_snapshot={"sandbox_tools": copy.deepcopy(selection)}, pricing_snapshot={},
            client_request_id=str(uuid4()), request_fingerprint="f" * 64, fingerprint_version=1,
            workload_class="online_text", worker_pool_id=db.pool.id,
            deadline_at=now + timedelta(minutes=4), **values,
        )

    root = make_run(status="waiting_children")
    run = make_run(
        status="running", root_run_id=root.id, worker_registration_id=db.registration.id,
        lease_owner=db.registration.worker_identity + "#1", lease_fence=1,
        lease_expires_at=now + timedelta(minutes=3),
    )
    sandbox = ChatRuntimeResource(
        id=str(uuid4()), pool_id=pool.id, generation=1, role="sandbox", backend="kubernetes", run_id=run.id,
        desired_state="requested", observed_state="ready", request_fingerprint="c" * 64,
        cluster_profile_id=cluster.id, namespace=cluster.namespace, image_ref=configured.image,
        policy_digest=configured.digest(), certificate_fingerprint="c" * 64,
        certificate_not_after=now + timedelta(minutes=5), deadline_at=now + timedelta(minutes=2),
        address="10.42.0.10", port=8013,
    )
    async with db.factory() as session, session.begin():
        session.add(pool)
        session.add(root)
        await session.flush()
        session.add(run)
        await session.flush()
        session.add(sandbox)
        run.assigned_resource_id = sandbox.id
        (await session.get(ChatWorkerRegistration, db.registration.id)).heartbeat_at = now
    body = {"run_id": run.id, "call_id": "selected-browser-call", "fence": 1,
            "profile": "playwright", "tool": "browser_navigate", "arguments": {},
            "timeout_seconds": 30, "workspace_revision": 0}
    arguments = dict(
        client_identity=db.peer, client_fingerprint="a" * 64, run_id=run.id, lease_owner=run.lease_owner,
        resource_id=sandbox.id, generation=1, method="POST", path="/v1/mcp/executions",
        call_id=body["call_id"], workspace_revision=0, body=body,
    )
    signed = []
    sign = dispatch.sign_dispatch_capability

    def tracked_sign(*args, **kwargs):
        signed.append(copy.deepcopy(kwargs))
        return sign(*args, **kwargs)

    monkeypatch.setattr(dispatch, "sign_dispatch_capability", tracked_sign)
    try:
        yield SimpleNamespace(
            db=db, config=config, pool=pool, configured=configured, run=run, root=root, sandbox=sandbox,
            arguments=arguments, body=body, signed=signed, now=now, manifest_path=manifest_path,
            selection=selection,
        )
    finally:
        async with db.factory() as session, session.begin():
            await session.execute(delete(ChatRuntimeResource).where(ChatRuntimeResource.id == sandbox.id))
            await session.execute(delete(ChatRun).where(ChatRun.id == run.id))
            await session.execute(delete(ChatRun).where(ChatRun.id == root.id))
            await session.execute(delete(ChatRuntimePool).where(ChatRuntimePool.id == pool.id))


async def test_selected_dispatch_signs_only_frozen_tool_and_exact_body(selected_dispatch_db):
    from lumen.services.infrastructure.dispatch import authorize_dispatch

    d = selected_dispatch_db
    grant = await authorize_dispatch(d.config, **d.arguments)
    assert grant.address == d.sandbox.address and grant.exp == int(d.now.timestamp()) + 15
    payload = json.loads(base64.urlsafe_b64decode(grant.capability.split(".")[0] + "==="))
    canonical_body = json.dumps(d.body, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    assert payload["fingerprint"] == hashlib.sha256(canonical_body.encode()).hexdigest()
    assert payload["run_id"] == d.run.id and payload["resource_id"] == d.sandbox.id
    assert payload["fence"] == 1 and payload["call_id"] == d.body["call_id"]
    assert payload["workspace_revision"] == 0 and payload["exp"] == grant.exp
    assert len(d.signed) == 1 and d.signed[0]["body"] == d.body


@pytest.mark.parametrize("fault", [
    "missing", "empty", "null", "malformed", "extra_field",
    "name", "profile", "pool", "image", "pool_digest", "manifest_sha256",
])
async def test_selected_dispatch_rejects_missing_or_changed_snapshot(selected_dispatch_db, fault):
    from lumen.services.infrastructure.dispatch import DispatchRejected, authorize_dispatch

    d = selected_dispatch_db
    snapshot = {"sandbox_tools": copy.deepcopy(d.selection)}
    if fault == "missing":
        snapshot = {}
    elif fault in {"empty", "null", "malformed"}:
        snapshot["sandbox_tools"] = {"empty": [], "null": None, "malformed": {}}[fault]
    elif fault == "extra_field":
        snapshot["sandbox_tools"][0]["command"] = ["/untrusted"]
    else:
        snapshot["sandbox_tools"][0][fault] = "changed"
    async with d.db.factory() as session, session.begin():
        (await session.get(ChatRun, d.run.id)).capability_snapshot = snapshot
    with pytest.raises(DispatchRejected, match="^dispatch unavailable$"):
        await authorize_dispatch(d.config, **d.arguments)
    assert d.signed == []


@pytest.mark.parametrize(("target", "field", "value"), [
    ("pool", "name", "not-selected"),
    ("pool", "deployment_id", "other-deployment"),
    ("pool", "enabled", False),
    ("pool", "role", "worker"),
    ("pool", "backend", "nova"),
    ("pool", "image_ref", "registry.example/changed@sha256:" + "d" * 64),
    ("pool", "profile_digest", "d" * 64),
    ("pool", "cluster_profile_id", "other-cluster"),
    ("pool", "namespace", "other-namespace"),
    ("sandbox", "backend", "nova"),
    ("sandbox", "image_ref", "registry.example/changed@sha256:" + "d" * 64),
    ("sandbox", "policy_digest", "d" * 64),
    ("sandbox", "cluster_profile_id", "other-cluster"),
    ("sandbox", "namespace", "other-namespace"),
    ("sandbox", "pool_id", "worker-pool"),
    ("root", "status", "completed"),
])
async def test_selected_dispatch_rejects_current_identity_drift(selected_dispatch_db, target, field, value):
    from lumen.services.infrastructure.dispatch import DispatchRejected, authorize_dispatch

    d = selected_dispatch_db
    row = getattr(d, target)
    if value == "worker-pool":
        value = d.db.pool.id
    async with d.db.factory() as session, session.begin():
        setattr(await session.get(type(row), row.id), field, value)
    with pytest.raises(DispatchRejected, match="^dispatch unavailable$"):
        await authorize_dispatch(d.config, **d.arguments)
    assert d.signed == []


@pytest.mark.parametrize(("target", "field"), [
    ("run", "lease_expires_at"), ("run", "deadline_at"), ("root", "deadline_at"),
    ("sandbox", "deadline_at"), ("sandbox", "certificate_not_after"),
])
@pytest.mark.parametrize("remaining", [None, 0, -1, 3])
async def test_selected_dispatch_enforces_and_caps_every_deadline(selected_dispatch_db, target, field, remaining):
    from lumen.services.infrastructure.dispatch import DispatchRejected, authorize_dispatch

    d = selected_dispatch_db
    row = getattr(d, target)
    deadline = d.now + timedelta(seconds=remaining) if remaining is not None else None
    async with d.db.factory() as session, session.begin():
        setattr(await session.get(type(row), row.id), field, deadline)
    if remaining == 3:
        grant = await authorize_dispatch(d.config, **d.arguments)
        assert grant.exp == int(deadline.timestamp())
        assert d.signed[0]["ttl_seconds"] == 3
    else:
        with pytest.raises(DispatchRejected, match="^dispatch unavailable$"):
            await authorize_dispatch(d.config, **d.arguments)
        assert d.signed == []


@pytest.mark.parametrize("fault", [
    "unselected_tool", "profile", "manifest", "manifest_missing", "current_manifest",
    "current_profile", "current_name", "disabled", "current_image", "raw_code",
])
async def test_selected_dispatch_rejects_ungranted_execution(selected_dispatch_db, fault):
    from lumen.services.infrastructure.dispatch import DispatchRejected, authorize_dispatch

    d = selected_dispatch_db
    config = d.config
    if fault == "unselected_tool":
        d.body["tool"] = "browser_click"
    elif fault == "profile":
        d.body["profile"] = "not-selected"
    elif fault == "manifest":
        d.manifest_path.write_bytes(b'{"version":1,"profiles":[]}')
    elif fault == "manifest_missing":
        d.manifest_path.unlink()
    elif fault in {"current_manifest", "current_profile", "current_name"}:
        manifest = json.loads(d.manifest_path.read_bytes())
        profile = manifest["profiles"][0]
        if fault == "current_manifest":
            profile["command"] = ["/usr/bin/node", "/srv/changed.js"]
        elif fault == "current_profile":
            profile["id"] = "changed-profile"
        else:
            profile["tools"][0]["name"] = "changed_tool"
        manifest_bytes = json.dumps(manifest, separators=(",", ":")).encode()
        d.manifest_path.write_bytes(manifest_bytes)
        updated_profile = d.configured.profile.model_copy(update={
            "tool_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        })
        config = config.model_copy(update={
            "pools": (d.configured.model_copy(update={"profile": updated_profile}),),
        })
    elif fault in {"disabled", "current_image"}:
        change = {"enabled": False} if fault == "disabled" else {
            "image": "registry.example/changed@sha256:" + "d" * 64,
        }
        config = config.model_copy(update={"pools": (d.configured.model_copy(update=change),)})
    else:
        d.arguments["path"] = "/v1/executions"
        d.arguments["body"] = {
            key: value for key, value in d.body.items() if key not in {"profile", "tool", "arguments"}
        } | {"language": "python", "source": "print(1)"}
    with pytest.raises(DispatchRejected, match="^dispatch unavailable$"):
        await authorize_dispatch(config, **d.arguments)
    assert d.signed == []


@pytest.mark.parametrize("path", ["/v1/workspace", "/v1/executions/", "/v1/artifacts/"])
async def test_selected_dispatch_reads_do_not_need_tool_selection(selected_dispatch_db, path):
    from lumen.services.infrastructure.dispatch import authorize_dispatch

    d = selected_dispatch_db
    async with d.db.factory() as session, session.begin():
        (await session.get(ChatRun, d.run.id)).capability_snapshot = {}
    arguments = d.arguments | {
        "method": "GET", "path": path + str(uuid4()) if path.endswith("/") else path,
        "body": None, "call_id": None, "workspace_revision": None,
    }
    grant = await authorize_dispatch(d.config, **arguments)
    assert grant.capability and len(d.signed) == 1


async def test_selected_dispatch_rejects_a_deadline_crossed_during_signing(selected_dispatch_db, monkeypatch):
    from lumen.services.infrastructure import dispatch, transport

    d = selected_dispatch_db
    async with d.db.factory() as session, session.begin():
        (await session.get(ChatRuntimeResource, d.sandbox.id)).deadline_at = d.now + timedelta(seconds=3)
    clock = [d.now.timestamp()]
    monkeypatch.setattr(dispatch, "time", SimpleNamespace(time=lambda: clock[0]))
    monkeypatch.setattr(transport, "time", SimpleNamespace(time=lambda: clock[0]))
    sign = dispatch.sign_dispatch_capability

    def delayed_sign(*args, **kwargs):
        clock[0] += 1
        return sign(*args, **kwargs)

    monkeypatch.setattr(dispatch, "sign_dispatch_capability", delayed_sign)
    with pytest.raises(dispatch.DispatchRejected, match="^dispatch unavailable$"):
        await dispatch.authorize_dispatch(d.config, **d.arguments)
    assert len(d.signed) == 1
