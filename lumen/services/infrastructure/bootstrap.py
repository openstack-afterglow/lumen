"""One-use resource bootstrap and short-lived, CA-signed resource identity.

The bootstrap token is returned only to the provisioning caller. The database holds
its SHA-256 digest; a row lock makes concurrent exchanges mutually exclusive.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed448, ed25519
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select

from lumen.db import get_session_factory
from lumen.models.chat_infrastructure import ChatRuntimeResource
from lumen.services.infrastructure.config import RuntimeConfig, SecretRef
from lumen.services.infrastructure.transport import derive_dispatch_key, encode_key, resource_identity

_BOOTSTRAP_SECONDS = 600


class BootstrapRejected(ValueError):
    """A deliberately non-diagnostic denial; never includes the token or CSR."""


def _factory():
    factory = get_session_factory()
    if factory is None:
        raise RuntimeError("database is not configured")
    return factory


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _ca(config: RuntimeConfig):
    if config.tls is None:
        raise RuntimeError("controller TLS CA is not configured")
    try:
        certificate = x509.load_pem_x509_certificate(Path(config.tls.ca_file).read_bytes())
        key = serialization.load_pem_private_key(Path(config.tls.ca_key_file).read_bytes(), password=None)
    except (OSError, ValueError, TypeError) as exc:
        raise RuntimeError("controller CA material is unavailable") from exc
    try:
        if not certificate.extensions.get_extension_for_class(x509.BasicConstraints).value.ca:
            raise RuntimeError("configured CA certificate is not a CA")
        usage = certificate.extensions.get_extension_for_class(x509.KeyUsage).value
        if not usage.key_cert_sign or not (_utc(certificate.not_valid_before_utc) <= datetime.now(UTC)
                                            < _utc(certificate.not_valid_after_utc)):
            raise RuntimeError("configured CA cannot currently sign certificates")
        if key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo) != certificate.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo):
            raise RuntimeError("configured CA key does not match the CA certificate")
    except x509.ExtensionNotFound as exc:
        raise RuntimeError("configured CA certificate is not a CA") from exc
    return certificate, key


async def issue_bootstrap_token(resource_id: str, generation: int, config: RuntimeConfig) -> str:
    """Issue once for a pending generation; never reissue a consumed token.

    The caller must pass the plaintext directly to its trusted provisioning channel.
    Reissuing while an existing token is active is intentionally forbidden: the
    original token cannot be recovered from its digest.
    """
    _ca(config)  # Fail before provisioning when no usable signer is installed.
    token = secrets.token_urlsafe(48)
    now = datetime.now(UTC)
    async with _factory()() as session, session.begin():
        resource = (await session.execute(select(ChatRuntimeResource).where(
            ChatRuntimeResource.id == resource_id, ChatRuntimeResource.generation == generation,
        ).with_for_update())).scalar_one_or_none()
        if (resource is None or resource.desired_state != "requested"
                or resource.observed_state not in {"requested", "creating", "booting", "unknown"}
                or resource.certificate_fingerprint or resource.bootstrap_token_hash):
            raise BootstrapRejected("bootstrap unavailable")
        resource.bootstrap_token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
        resource.bootstrap_expires_at = now + timedelta(seconds=_BOOTSTRAP_SECONDS)
    return token


async def exchange_bootstrap_token(token: str, csr_pem: str, config: RuntimeConfig, *,
                                   operator_key: SecretRef | bytes) -> dict[str, str | int | None]:
    """Atomically consume a token and sign only the assigned resource CSR.

    Failed CSR/CA validation leaves the token unconsumed. A successful exchange
    commits the certificate fingerprint and burns the token before returning secrets.
    """
    if not isinstance(token, str) or len(token) > 256 or len(token) < 32 or not token.isascii():
        raise BootstrapRejected("bootstrap unavailable")
    if not isinstance(csr_pem, str) or len(csr_pem) > 16_384:
        raise BootstrapRejected("bootstrap unavailable")
    try:
        csr = x509.load_pem_x509_csr(csr_pem.encode("ascii"))
        if (not csr.is_signature_valid or not isinstance(csr.public_key(), ec.EllipticCurvePublicKey)
                or not isinstance(csr.public_key().curve, ec.SECP256R1)):
            raise BootstrapRejected("bootstrap unavailable")
        # The controller, never the CSR, assigns identity and every certificate extension.
        ca_certificate, ca_key = _ca(config)
    except (ValueError, UnicodeError, TypeError) as exc:
        raise BootstrapRejected("bootstrap unavailable") from exc
    digest = hashlib.sha256(token.encode("ascii")).hexdigest()
    now = datetime.now(UTC)
    async with _factory()() as session, session.begin():
        resource = (await session.execute(select(ChatRuntimeResource).where(
            ChatRuntimeResource.bootstrap_token_hash == digest,
        ).with_for_update())).scalar_one_or_none()
        if (resource is None or not hmac.compare_digest(resource.bootstrap_token_hash, digest)
                or resource.bootstrap_expires_at is None or _utc(resource.bootstrap_expires_at) <= now
                or resource.desired_state == "deleting" or resource.observed_state in {"deleted", "failed"}
                or resource.certificate_fingerprint is not None):
            raise BootstrapRejected("bootstrap unavailable")
        # A sandbox certificate must cover its immutable run deadline. The CA
        # must remain valid throughout; never issue an identity doomed mid-run.
        if resource.role == "sandbox":
            if resource.deadline_at is None or _utc(resource.deadline_at) <= now:
                raise BootstrapRejected("bootstrap unavailable")
            required_until = _utc(resource.deadline_at) + timedelta(minutes=1)
        else:
            required_until = now + timedelta(hours=1)
        expires = min(required_until, now + timedelta(hours=25), _utc(ca_certificate.not_valid_after_utc))
        if expires < required_until:
            raise BootstrapRejected("bootstrap unavailable")
        identity = resource_identity(resource.role, resource.id, resource.generation)
        names: list[x509.GeneralName] = [x509.UniformResourceIdentifier(identity)]
        usages = [ExtendedKeyUsageOID.SERVER_AUTH]
        if resource.role in {"api", "worker"}:
            usages.append(ExtendedKeyUsageOID.CLIENT_AUTH)
        cert = (x509.CertificateBuilder()
                .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, identity)]))
                .issuer_name(ca_certificate.subject)
                .public_key(csr.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(minutes=1))
                .not_valid_after(expires)
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False,
                                             key_encipherment=False, data_encipherment=False,
                                             key_agreement=False, key_cert_sign=False, crl_sign=False,
                                             encipher_only=False, decipher_only=False), critical=True)
                .add_extension(x509.ExtendedKeyUsage(usages), critical=False)
                .add_extension(x509.SubjectAlternativeName(names), critical=False)
                .sign(ca_key, None if isinstance(ca_key, (ed25519.Ed25519PrivateKey, ed448.Ed448PrivateKey)) else hashes.SHA256()))
        fingerprint = cert.fingerprint(hashes.SHA256()).hex()
        response: dict[str, str | int | None] = {
            "resource_id": resource.id, "run_id": resource.run_id,
            "generation": resource.generation, "image_id": resource.image_ref,
            "policy_id": resource.policy_digest,
            "certificate_pem": cert.public_bytes(serialization.Encoding.PEM).decode("ascii"),
            "client_ca_pem": ca_certificate.public_bytes(serialization.Encoding.PEM).decode("ascii"),
        }
        if resource.role == "sandbox":
            response["run_deadline"] = int(_utc(resource.deadline_at).timestamp())
            response["dispatch_key"] = encode_key(derive_dispatch_key(
                operator_key, resource_id=resource.id,
                run_id=resource.run_id or "", generation=resource.generation))
        else:
            response["role"] = resource.role
        resource.bootstrap_token_hash = None
        resource.bootstrap_expires_at = None
        resource.certificate_fingerprint = fingerprint
        return response


def make_bootstrap_router(config: RuntimeConfig, operator_key: SecretRef | bytes):
    """Mount on a dedicated CA-verified HTTPS listener; never on the public API."""
    # Request must be module-global for postponed annotations to resolve.

    router = APIRouter()

    @router.post("/v1/sandbox/bootstrap")
    async def exchange(request: Request):
        if request.scope.get("scheme") != "https":
            raise HTTPException(status_code=403, detail="bootstrap unavailable")
        try:
            if int(request.headers.get("content-length", "0")) > 20_000:
                raise HTTPException(status_code=413, detail="bootstrap payload too large")
        except ValueError as exc:
            raise HTTPException(status_code=403, detail="bootstrap unavailable") from exc
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > 20_000:
                raise HTTPException(status_code=413, detail="bootstrap payload too large")
            body.extend(chunk)
        try:
            import json
            payload = json.loads(body)
            if not isinstance(payload, dict) or set(payload) != {"token", "csr_pem"}:
                raise BootstrapRejected("bootstrap unavailable")
            return await exchange_bootstrap_token(payload["token"], payload["csr_pem"], config,
                                                  operator_key=operator_key)
        except (BootstrapRejected, ValueError, TypeError) as exc:
            raise HTTPException(status_code=403, detail="bootstrap unavailable") from exc

    return router
