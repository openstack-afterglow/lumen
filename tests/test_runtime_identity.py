"""Guest delivery and pin boundaries; DB state transitions live in integration tests."""
from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from fastapi import FastAPI

from lumen.services.infrastructure import identity
from lumen.services.infrastructure.config import GuestProfile, RenewalPolicy, RuntimeConfig, SecretRef
from lumen.services.infrastructure.transport import resource_identity
from lumen.services.worker_routing import _trusted_identity_valid


@pytest.fixture
def delivery(tmp_path, monkeypatch):
    path = tmp_path / "guest.json"
    path.write_text(json.dumps({"lumen": {"chat_default_model": "model", "batch_enabled": True,
                              "runtime_config": {"cloud_secret": "NEVER-DELIVER"}, "unlisted": "NO"}}))
    path.chmod(0o600)
    refs = {name: SecretRef(env="GUEST_" + name) for name in
            ("DATABASE_URL", "REDIS_URL", "LUMEN_ENCRYPTION_KEY")}
    for name, ref in refs.items():
        monkeypatch.setenv(ref.env, "value-" + name)
    profile = GuestProfile(id="worker", role="worker", config_file=str(path),
        config_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        config_keys=("chat_default_model", "batch_enabled"), secrets=refs,
        secret_env_names=tuple(refs), image="registry/worker@sha256:" + "a" * 64,
        plugin_digest="b" * 64, schema_version=1, protocol_version=2)
    config = RuntimeConfig.model_construct(
        deployment_id="test", guest_profiles=(profile,), renewal=RenewalPolicy(),
        pools=(SimpleNamespace(name="workers", role="worker", guest_profile_id="worker"),),
        cloud_profiles=(), dispatch_key=SecretRef(env="CONTROLLER_DISPATCH"), tls=None)
    resource = SimpleNamespace(id=str(uuid4()), role="worker", generation=1,
        guest_profile_id=profile.id, guest_profile_digest=profile.digest(), image_ref=profile.image)
    pool = SimpleNamespace(name="workers", deployment_id="test", workload_class="batch",
        guest_profile_id=profile.id, guest_profile_digest=profile.digest())
    original = os.fstat
    ownership = SimpleNamespace(uid=0)

    def as_controller(fd):
        info = original(fd)
        return SimpleNamespace(st_mode=info.st_mode, st_size=info.st_size, st_uid=ownership.uid)

    monkeypatch.setattr(identity.os, "fstat", as_controller)
    return SimpleNamespace(config=config, resource=resource, pool=pool, path=path,
                           profile=profile, ownership=ownership)


def test_delivery_is_exact_allowlist_and_never_controller_config(delivery):
    d = delivery
    result = identity._profile_payload(d.config, d.resource, d.pool)
    assert result["config"] == {"chat_default_model": "model", "batch_enabled": True}
    assert result["secrets"] == {name: "value-" + name for name in d.profile.secret_env_names}
    assert result["config_keys"] == list(d.profile.config_keys)
    assert result["secret_env_names"] == list(d.profile.secret_env_names)
    assert result["workload_class"] == "batch"
    assert result["renewal"] == {"leaf_ttl_seconds": 3600, "renew_interval_seconds": 1200, "overlap_seconds": 120}
    assert "NEVER-DELIVER" not in json.dumps(result) and "unlisted" not in result["config"]
    d.resource.role = "api"
    d.profile = d.profile.model_copy(update={"role": "api"})
    d.config = d.config.model_copy(update={"guest_profiles": (d.profile,),
        "pools": (SimpleNamespace(name="workers", role="api", guest_profile_id="worker"),)})
    d.resource.guest_profile_digest = d.pool.guest_profile_digest = d.profile.digest()
    assert identity._profile_payload(d.config, d.resource, d.pool)["workload_class"] is None


@pytest.mark.parametrize("fault", ["mode", "owner", "hash", "symlink", "missing_secret", "missing_key", "invalid_json"])
def test_delivery_fails_closed_for_untrusted_material(delivery, monkeypatch, fault):
    d = delivery
    if fault == "mode":
        d.path.chmod(0o640)
    elif fault == "owner":
        d.ownership.uid = 1000
    elif fault == "hash":
        d.path.write_text("changed")
    elif fault == "symlink":
        real = d.path.with_suffix(".real")
        d.path.rename(real)
        d.path.symlink_to(real)
    elif fault == "missing_secret":
        monkeypatch.delenv("GUEST_DATABASE_URL")
    else:
        raw = b"[]" if fault == "invalid_json" else b'{}'
        d.path.write_bytes(raw)
        d.profile = d.profile.model_copy(update={"config_sha256": hashlib.sha256(raw).hexdigest()})
        d.config = d.config.model_copy(update={"guest_profiles": (d.profile,)})
        d.resource.guest_profile_digest = d.pool.guest_profile_digest = d.profile.digest()
    with pytest.raises(identity.IdentityRejected) as denied:
        identity._profile_payload(d.config, d.resource, d.pool)
    assert (denied.value.status, denied.value.code) == (503, "guest_profile_unavailable")
    assert "value-" not in str(denied.value)


@pytest.mark.parametrize("fault", ["resource_profile", "resource_digest", "pool_profile", "pool_digest", "foreign_deployment", "image", "role"])
def test_delivery_detects_profile_drift_and_foreign_pool(delivery, fault):
    d = delivery
    if fault == "resource_profile":
        d.resource.guest_profile_id = "other"
    elif fault == "resource_digest":
        d.resource.guest_profile_digest = "f" * 64
    elif fault == "pool_profile":
        d.pool.guest_profile_id = "other"
    elif fault == "pool_digest":
        d.pool.guest_profile_digest = "f" * 64
    elif fault == "foreign_deployment":
        d.pool.deployment_id = "foreign"
    elif fault == "image":
        d.resource.image_ref = "registry/different@sha256:" + "f" * 64
    else:
        d.resource.role = "sandbox"
    with pytest.raises(identity.IdentityRejected) as denied:
        identity._profile_payload(d.config, d.resource, d.pool)
    assert (denied.value.status, denied.value.code) == (409, "guest_profile_changed")


def test_secret_allowlist_cannot_alias_controller_dispatch_ref(delivery):
    d = delivery
    profile = d.profile.model_copy(update={"secrets": {**d.profile.secrets,
                                                       "DATABASE_URL": d.config.dispatch_key}})
    d.config = d.config.model_copy(update={"guest_profiles": (profile,)})
    d.resource.guest_profile_digest = d.pool.guest_profile_digest = profile.digest()
    with pytest.raises(identity.IdentityRejected, match="guest_profile_unavailable"):
        identity._profile_payload(d.config, d.resource, d.pool)


def test_recursive_controller_keys_cannot_be_delivered_under_plugin_config(delivery):
    d = delivery
    raw = json.dumps({"plugin_config": {"ca_key_file": "CONTROLLER-PRIVATE"}}).encode()
    d.path.write_bytes(raw)
    profile = d.profile.model_copy(update={"config_keys": ("plugin_config",),
                                          "config_sha256": hashlib.sha256(raw).hexdigest()})
    d.config = d.config.model_copy(update={"guest_profiles": (profile,)})
    d.resource.guest_profile_digest = d.pool.guest_profile_digest = profile.digest()
    with pytest.raises(identity.IdentityRejected, match="guest_profile_unavailable"):
        identity._profile_payload(d.config, d.resource, d.pool)


def test_pins_expire_at_the_boundary_and_pending_is_activation_only():
    now = datetime.now(UTC)
    row = SimpleNamespace(certificate_fingerprint="a" * 64, certificate_not_after=now + timedelta(hours=1),
        previous_certificate_fingerprint="b" * 64, previous_certificate_valid_until=now + timedelta(seconds=120),
        pending_certificate_fingerprint="c" * 64, pending_certificate_expires_at=now + timedelta(seconds=120),
        pending_certificate_not_after=now + timedelta(hours=1))
    assert identity.accepted_fingerprints(row, now=now) == ("a" * 64, "b" * 64)
    assert identity.accepted_fingerprints(row, now=now, include_pending=True) == ("a" * 64, "b" * 64, "c" * 64)
    assert identity.accepted_fingerprints(row, now=now + timedelta(seconds=120), include_pending=True) == ("a" * 64,)
    assert identity.accepted_fingerprints(row, now=now + timedelta(hours=1)) == ()


def test_worker_routing_keeps_registration_valid_during_overlap(monkeypatch):
    now = datetime.now(UTC)
    resource = SimpleNamespace(role="worker", desired_state="draining", observed_state="draining",
        bootstrap_token_hash=None, pool_id="pool", generation=1, certificate_fingerprint="a" * 64,
        certificate_not_after=now + timedelta(hours=1), previous_certificate_fingerprint="b" * 64,
        previous_certificate_valid_until=now + timedelta(seconds=120))
    registration = SimpleNamespace(resource_id="resource", pool_id="pool", resource_generation=1,
                                   certificate_fingerprint="b" * 64)
    monkeypatch.setattr(identity, "_now", lambda: now)
    assert _trusted_identity_valid(registration, resource)
    monkeypatch.setattr(identity, "_now", lambda: now + timedelta(seconds=120))
    assert not _trusted_identity_valid(registration, resource)


def test_csr_is_p256_signed_and_identity_is_not_chosen_by_csr():
    for key in (ec.generate_private_key(ec.SECP384R1()), rsa.generate_private_key(public_exponent=65537, key_size=2048)):
        csr = x509.CertificateSigningRequestBuilder().subject_name(x509.Name([])).sign(key, hashes.SHA256())
        with pytest.raises(identity.IdentityRejected, match="invalid_identity_request"):
            identity._csr(csr.public_bytes(serialization.Encoding.PEM).decode())
    for bad in (None, {}, "invalid", "a" * 16385):
        with pytest.raises(identity.IdentityRejected):
            identity._csr(bad)


async def test_internal_routes_reject_http_header_impersonation_and_sandbox():
    app = FastAPI()
    app.include_router(identity.make_identity_router(RuntimeConfig()))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://controller") as client:
        for endpoint in ("/v1/runtime/guest-config", "/v1/runtime/identity/renew", "/v1/runtime/identity/activate"):
            method = "GET" if endpoint.endswith("guest-config") else "POST"
            response = await client.request(method, endpoint, headers={"x-client-identity": "claimed", "x-client-fingerprint": "a" * 64})
            assert response.status_code == 403 and response.json() == {"detail": "identity_unavailable"}
    for role in ("sandbox", "operator"):
        with pytest.raises(identity.IdentityRejected):
            identity.peer_resource_id(f"spiffe://lumen/{role}/{uuid4()}/1")
    with pytest.raises(identity.IdentityRejected):
        identity.peer_resource_id(resource_identity("worker", str(uuid4()), 1) + "/suffix")


def test_missing_current_expiry_never_grants_authority():
    row = SimpleNamespace(certificate_fingerprint="a" * 64, certificate_not_after=None)
    assert identity.accepted_fingerprints(row) == ()
    assert not identity.accepts_fingerprint(row, "a" * 64)


def test_delivery_resolves_root_private_secret_files_and_toml(delivery):
    d = delivery
    secret = d.path.with_name("database.secret")
    secret.write_text("database-value\n")
    secret.chmod(0o600)
    toml = d.path.with_suffix(".toml")
    toml.write_text('[lumen]\nchat_default_model="model"\nbatch_enabled=true\nnonallowlisted="hidden"\n')
    toml.chmod(0o600)
    profile = d.profile.model_copy(update={"config_file": str(toml),
        "config_sha256": hashlib.sha256(toml.read_bytes()).hexdigest(),
        "secrets": {**d.profile.secrets, "DATABASE_URL": SecretRef(file=str(secret))}})
    d.config = d.config.model_copy(update={"guest_profiles": (profile,)})
    d.resource.guest_profile_digest = d.pool.guest_profile_digest = profile.digest()
    result = identity._profile_payload(d.config, d.resource, d.pool)
    assert result["secrets"]["DATABASE_URL"] == "database-value"
    assert "nonallowlisted" not in result["config"]
    secret.chmod(0o644)
    with pytest.raises(identity.IdentityRejected, match="guest_profile_unavailable"):
        identity._profile_payload(d.config, d.resource, d.pool)


@pytest.mark.parametrize("protected", ["ca", "operator", "cloud"])
def test_guest_allowlist_cannot_resolve_controller_credentials(delivery, protected):
    d = delivery
    ca_key = str(d.path.with_name("ca-key.pem"))
    operator_key = str(d.path.with_name("operator-key.pem"))
    cloud_ref = SecretRef(env="CLOUD_APPLICATION_CREDENTIAL")
    d.config = d.config.model_copy(update={
        "tls": SimpleNamespace(ca_key_file=ca_key, key_file=str(d.path.with_name("listener-key.pem")),
                               operator_client_key_file=operator_key, operator_client_cert_file=None),
        "cloud_profiles": (SimpleNamespace(application_credential_secret=cloud_ref),)})
    ref = cloud_ref if protected == "cloud" else SecretRef(file=ca_key if protected == "ca" else operator_key)
    profile = d.profile.model_copy(update={"secrets": {**d.profile.secrets, "DATABASE_URL": ref}})
    d.config = d.config.model_copy(update={"guest_profiles": (profile,)})
    d.resource.guest_profile_digest = d.pool.guest_profile_digest = profile.digest()
    with pytest.raises(identity.IdentityRejected) as denied:
        identity._profile_payload(d.config, d.resource, d.pool)
    assert (denied.value.status, denied.value.code) == (503, "guest_profile_unavailable")


def test_delivery_includes_only_configured_probe_certificate_fingerprint(delivery):
    d = delivery
    now = datetime.now(UTC)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1)).sign(key, hashes.SHA256()))
    path = d.path.with_name("probe.pem")
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    d.config = d.config.model_copy(update={"tls": SimpleNamespace(
        ca_key_file=str(d.path.with_name("ca-key.pem")), key_file=str(d.path.with_name("listener-key.pem")),
        operator_client_key_file=str(d.path.with_name("probe-key.pem")), operator_client_cert_file=str(path))})
    result = identity._profile_payload(d.config, d.resource, d.pool)
    assert result["operator_probe_fingerprints"] == [cert.fingerprint(hashes.SHA256()).hex()]
    assert "key_file" not in result and "operator_client_key_file" not in result


async def test_identity_payload_is_bounded_and_schema_errors_are_safe():
    app = FastAPI()
    app.include_router(identity.make_identity_router(RuntimeConfig()))

    async def verified_scope(scope, receive, send):
        scope["lumen_client_identity"] = resource_identity("worker", str(uuid4()), 1)
        scope["lumen_client_fingerprint"] = "a" * 64
        await app(scope, receive, send)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=verified_scope), base_url="https://controller") as client:
        for raw, status, code in (("x" * 20001, 413, "identity_payload_too_large"),
                                  ("[]", 422, "invalid_identity_request"),
                                  ('{"renewal_request_id":"secret-invalid-id"}', 422, "invalid_identity_request")):
            response = await client.post("/v1/runtime/identity/activate", content=raw)
            assert response.status_code == status and response.json() == {"detail": code}
            assert "secret-invalid-id" not in response.text
