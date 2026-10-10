"""Trusted guest mTLS pins, one-hour rotation and role-scoped config delivery."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat
import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select

from lumen.db import get_session_factory
from lumen.models.chat_infrastructure import ChatRuntimePool, ChatRuntimeResource, ChatWorkerRegistration
from lumen.services.durable_runs.budgets import retry_deadlocks
from lumen.services.infrastructure.bootstrap import _ca, sign_resource_certificate
from lumen.services.infrastructure.config import RuntimeConfig
from lumen.services.infrastructure.transport import resource_identity


class IdentityRejected(ValueError):
    def __init__(self, code="identity_unavailable", status=403):
        super().__init__(code)
        self.code, self.status = code, status


def _now() -> datetime:
    return datetime.now(UTC)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _pending_fingerprint(resource, now):
    pending = getattr(resource, "pending_certificate_fingerprint", None)
    deadline = getattr(resource, "pending_certificate_expires_at", None)
    expiry = getattr(resource, "pending_certificate_not_after", None)
    return pending if pending and deadline and expiry and min(_utc(deadline), _utc(expiry)) > now else None


def accepted_fingerprints(resource, *, now: datetime | None = None, include_pending: bool = False) -> tuple[str, ...]:
    """Pins with known, unexpired authority; pending is opt-in for activation only."""
    now = now or _now()
    pins = []
    current = getattr(resource, "certificate_fingerprint", None)
    expiry = getattr(resource, "certificate_not_after", None)
    if current and expiry and _utc(expiry) > now:
        pins.append(current)
    previous = getattr(resource, "previous_certificate_fingerprint", None)
    overlap = getattr(resource, "previous_certificate_valid_until", None)
    if previous and overlap and _utc(overlap) > now:
        pins.append(previous)
    if include_pending:
        pending = _pending_fingerprint(resource, now)
        if pending:
            pins.append(pending)
    return tuple(dict.fromkeys(pins))


def accepts_fingerprint(resource, fingerprint: str | None, *, now: datetime | None = None) -> bool:
    return isinstance(fingerprint, str) and any(
        hmac.compare_digest(fingerprint, pin) for pin in accepted_fingerprints(resource, now=now)
    )


def peer_resource_id(identity: str) -> tuple[str, int, str]:
    if not isinstance(identity, str):
        raise IdentityRejected()
    match = re.fullmatch(r"spiffe://lumen/(api|worker)/([0-9a-fA-F-]{36})/([1-9][0-9]*)", identity)
    if not match:
        raise IdentityRejected()
    try:
        resource_id = str(UUID(match[2]))
        generation = int(match[3])
        if resource_identity(match[1], resource_id, generation) != identity:
            raise IdentityRejected()
    except (ValueError, TypeError) as exc:
        raise IdentityRejected() from exc
    return resource_id, generation, match[1]


def _factory():
    factory = get_session_factory()
    if factory is None:
        raise RuntimeError("database is not configured")
    return factory


async def _resource(session, identity, fingerprint, *, activation=False):
    resource_id, generation, role = peer_resource_id(identity)
    row = (await session.execute(select(ChatRuntimeResource).where(
        ChatRuntimeResource.id == resource_id,
    ).with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
    if (row is None or row.generation != generation or row.role != role
            or row.desired_state == "deleting" or row.observed_state in {"deleted", "failed"}
            or row.bootstrap_token_hash is not None):
        raise IdentityRejected()
    if not activation and not accepts_fingerprint(row, fingerprint):
        raise IdentityRejected()
    return row


async def _registrations(session, resource):
    return list((await session.execute(select(ChatWorkerRegistration).where(
        ChatWorkerRegistration.resource_id == resource.id,
        ChatWorkerRegistration.resource_generation == resource.generation,
    ).order_by(ChatWorkerRegistration.id).with_for_update()
      .execution_options(populate_existing=True))).scalars())


async def _live_work(session, row, registrations, now):
    if row.role == "worker":
        from lumen.services import auxiliary
        for registration in registrations:
            if (await auxiliary.lease_counts(session, registration.id)).total or registration.auxiliary_active:
                return True
        return False
    snapshot = row.api_counter_snapshot
    if (not isinstance(snapshot, dict) or row.api_counter_snapshot_at is None
            or _utc(row.api_counter_snapshot_at) < now - timedelta(seconds=20)):
        return False
    required = ("active_requests", "active_sse", "active_ws")
    if any(type(snapshot.get(key)) is not int or snapshot[key] < 0 for key in required):
        return False
    return snapshot["active_sse"] <= snapshot["active_requests"] and (
        snapshot["active_requests"] + snapshot["active_ws"] > 0)


def _request_id(value):
    try:
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise IdentityRejected("invalid_identity_request", 422) from exc


def _csr(pem):
    try:
        if not isinstance(pem, str) or len(pem) > 16384:
            raise ValueError()
        csr = x509.load_pem_x509_csr(pem.encode("ascii"))
        key = csr.public_key()
        if not csr.is_signature_valid or not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1):
            raise ValueError()
        digest = hashlib.sha256(csr.public_bytes(serialization.Encoding.DER)).hexdigest()
        return csr, digest
    except (ValueError, TypeError, UnicodeError) as exc:
        raise IdentityRejected("invalid_identity_request", 422) from exc


def _renewal_response(row, ca):
    return {"certificate_pem": row.pending_certificate_pem,
            "ca_pem": ca.public_bytes(serialization.Encoding.PEM).decode("ascii"),
            "not_after": int(_utc(row.pending_certificate_not_after).timestamp()),
            "pending_expires_at": int(_utc(row.pending_certificate_expires_at).timestamp())}


async def renew_identity(config, *, client_identity, client_fingerprint, renewal_request_id, csr_pem):
    request_id = _request_id(renewal_request_id)
    csr, digest = _csr(csr_pem)
    try:
        ca, key = _ca(config)
    except RuntimeError as exc:
        raise IdentityRejected("identity_signer_unavailable", 503) from exc

    async def transaction():
        from lumen.services.worker_routing import use_read_committed
        async with _factory()() as session, session.begin():
            await use_read_committed(session)
            row = await _resource(session, client_identity, client_fingerprint)
            now = _now()
            if not config.renewal.enabled:
                raise IdentityRejected("renewal_not_required", 409)
            if row.renewal_request_id == request_id:
                if row.renewal_csr_hash != digest:
                    raise IdentityRejected("renewal_conflict", 409)
                if row.pending_certificate_fingerprint is None:
                    raise IdentityRejected("renewal_already_activated", 409)
                if (row.pending_certificate_expires_at and _utc(row.pending_certificate_expires_at) > now
                        and row.pending_certificate_not_after and _utc(row.pending_certificate_not_after) > now):
                    return _renewal_response(row, ca)
            registrations = await _registrations(session, row)
            draining = (row.desired_state == "draining" or row.observed_state == "draining"
                        or row.drain_requested_at is not None or not row.accepting
                        or any(registration.draining for registration in registrations))
            if draining and not await _live_work(session, row, registrations, now):
                raise IdentityRejected("renewal_not_required", 409)
            expires = now + timedelta(seconds=config.renewal.leaf_ttl_seconds)
            if _utc(ca.not_valid_after_utc) < expires:
                raise IdentityRejected("identity_signer_unavailable", 503)
            cert = sign_resource_certificate(csr, row, ca, key, now, expires)
            row.pending_certificate_fingerprint = cert.fingerprint(hashes.SHA256()).hex()
            row.pending_certificate_pem = cert.public_bytes(serialization.Encoding.PEM).decode("ascii")
            row.pending_certificate_not_after = expires
            row.pending_certificate_expires_at = now + timedelta(seconds=config.renewal.overlap_seconds)
            row.renewal_request_id, row.renewal_csr_hash = request_id, digest
            return _renewal_response(row, ca)
    return await retry_deadlocks(transaction)


async def activate_identity(config, *, client_identity, client_fingerprint, renewal_request_id):
    request_id = _request_id(renewal_request_id)

    async def transaction():
        from lumen.services.worker_routing import use_read_committed
        async with _factory()() as session, session.begin():
            await use_read_committed(session)
            row = await _resource(session, client_identity, client_fingerprint, activation=True)
            now = _now()
            if row.renewal_request_id != request_id:
                raise IdentityRejected()
            if row.pending_certificate_fingerprint is None:
                if row.certificate_fingerprint != client_fingerprint or not accepts_fingerprint(row, client_fingerprint, now=now):
                    raise IdentityRejected()
                return {"activated": True, "not_after": int(_utc(row.certificate_not_after).timestamp())}
            if _pending_fingerprint(row, now) != client_fingerprint:
                raise IdentityRejected()
            registrations = await _registrations(session, row)
            row.previous_certificate_fingerprint = row.certificate_fingerprint
            row.previous_certificate_valid_until = (
                min(now + timedelta(seconds=config.renewal.overlap_seconds), _utc(row.certificate_not_after))
                if row.certificate_not_after is not None else None)
            row.certificate_fingerprint = row.pending_certificate_fingerprint
            row.certificate_not_after = row.pending_certificate_not_after
            for registration in registrations:
                registration.certificate_fingerprint = row.certificate_fingerprint
            row.pending_certificate_fingerprint = row.pending_certificate_pem = None
            row.pending_certificate_not_after = row.pending_certificate_expires_at = None
            return {"activated": True, "not_after": int(_utc(row.certificate_not_after).timestamp())}
    return await retry_deadlocks(transaction)


def _private_bytes(path):
    """Check the opened inode, not a pathname inspected before opening it."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 1024 * 1024:
            raise ValueError()
        data = handle.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            raise ValueError()
        return data


def _secret(ref):
    if ref.env is not None:
        value = os.environ.get(ref.env)
    else:
        value = _private_bytes(ref.file).decode("utf-8").rstrip("\r\n")
    if not value or "\x00" in value:
        raise ValueError()
    return value


def _profile_payload(config, row, pool):
    definition = next((item for item in config.pools if item.name == pool.name), None)
    profile = next((item for item in config.guest_profiles if item.id == row.guest_profile_id), None)
    if (pool.deployment_id != config.deployment_id or definition is None or profile is None
            or definition.guest_profile_id != profile.id or pool.guest_profile_id != profile.id
            or pool.guest_profile_digest != row.guest_profile_digest
            or profile.digest() != row.guest_profile_digest or profile.role != row.role
            or definition.role != row.role or profile.image != row.image_ref):
        raise IdentityRejected("guest_profile_changed", 409)
    try:
        raw = _private_bytes(profile.config_file)
        if hashlib.sha256(raw).hexdigest() != profile.config_sha256:
            raise ValueError()
        content = json.loads(raw) if profile.config_file.endswith(".json") else tomllib.loads(raw.decode("utf-8"))
        if not isinstance(content, dict):
            raise ValueError()
        content = content.get("lumen", content)
        if not isinstance(content, dict) or any(key not in content for key in profile.config_keys):
            raise ValueError()
        forbidden_names = {"runtime_config", "dispatch_key", "ca_key_file", "operator_client_key_file",
                           "cloud_profiles", "os_application_credential_id", "os_application_credential_secret", "os_auth_url"}
        def safe(value):
            if isinstance(value, dict):
                return all(str(key).lower() not in forbidden_names and safe(item) for key, item in value.items())
            if isinstance(value, list):
                return all(safe(item) for item in value)
            return True
        delivered = {key: content[key] for key in profile.config_keys}
        if not safe(delivered) or any(name.lower() in forbidden_names for name in profile.secret_env_names):
            raise ValueError()
        prohibited_refs = [cloud.application_credential_secret for cloud in config.cloud_profiles]
        if config.dispatch_key is not None:
            prohibited_refs.append(config.dispatch_key)
        prohibited_files = {config.tls.ca_key_file, config.tls.key_file, config.tls.operator_client_key_file} if config.tls else set()
        for ref in profile.secrets.values():
            if ref in prohibited_refs or (ref.file and any(path and Path(ref.file).resolve() == Path(path).resolve() for path in prohibited_files)):
                raise ValueError()
        secrets = {name: _secret(profile.secrets[name]) for name in profile.secret_env_names}
        probes = []
        if config.tls and config.tls.operator_client_cert_file:
            cert = x509.load_pem_x509_certificate(Path(config.tls.operator_client_cert_file).read_bytes())
            if not (_utc(cert.not_valid_before_utc) <= _now() < _utc(cert.not_valid_after_utc)):
                raise ValueError()
            probes.append(cert.fingerprint(hashes.SHA256()).hex())
    except (OSError, ValueError, TypeError, UnicodeError, KeyError) as exc:
        raise IdentityRejected("guest_profile_unavailable", 503) from exc
    return {"profile_id": profile.id, "profile_digest": profile.digest(), "role": row.role,
            "resource_id": row.id, "generation": row.generation, "image": profile.image,
            "plugin_digest": profile.plugin_digest, "schema_version": profile.schema_version,
            "protocol_version": profile.protocol_version, "workload_class": pool.workload_class if row.role == "worker" else None,
            "config": delivered, "config_keys": list(profile.config_keys), "secrets": secrets,
            "secret_env_names": list(profile.secret_env_names),
            "renewal": config.renewal.model_dump(exclude={"enabled"}), "operator_probe_fingerprints": probes}


async def guest_config(config, *, client_identity, client_fingerprint):
    async def transaction():
        from lumen.services.worker_routing import use_read_committed
        async with _factory()() as session, session.begin():
            await use_read_committed(session)
            row = await _resource(session, client_identity, client_fingerprint)
            pool = await session.get(ChatRuntimePool, row.pool_id)
            if pool is None:
                raise IdentityRejected()
            return _profile_payload(config, row, pool)
    return await retry_deadlocks(transaction)


def make_identity_router(config: RuntimeConfig):
    router = APIRouter()

    def peer(request):
        identity, fingerprint = request.scope.get("lumen_client_identity"), request.scope.get("lumen_client_fingerprint")
        if request.scope.get("scheme") != "https" or not identity or not fingerprint:
            raise IdentityRejected()
        return {"client_identity": identity, "client_fingerprint": fingerprint}

    async def payload(request, keys):
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > 20000:
                raise IdentityRejected("identity_payload_too_large", 413)
            raw.extend(chunk)
        try:
            value = json.loads(raw)
            if not isinstance(value, dict) or set(value) != keys:
                raise ValueError()
            return value
        except (ValueError, TypeError) as exc:
            raise IdentityRejected("invalid_identity_request", 422) from exc

    @router.post("/v1/runtime/identity/renew")
    async def renew(request: Request):
        try:
            identity = peer(request)
            body = await payload(request, {"renewal_request_id", "csr_pem"})
            return await renew_identity(config, **identity, **body)
        except IdentityRejected as exc:
            raise HTTPException(status_code=exc.status, detail=exc.code) from exc

    @router.post("/v1/runtime/identity/activate")
    async def activate(request: Request):
        try:
            identity = peer(request)
            body = await payload(request, {"renewal_request_id"})
            return await activate_identity(config, **identity, **body)
        except IdentityRejected as exc:
            raise HTTPException(status_code=exc.status, detail=exc.code) from exc

    @router.get("/v1/runtime/guest-config")
    async def configuration(request: Request):
        try:
            return await guest_config(config, **peer(request))
        except IdentityRejected as exc:
            raise HTTPException(status_code=exc.status, detail=exc.code) from exc
    return router
