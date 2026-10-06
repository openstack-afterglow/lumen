"""Portable fixtures: ownership emulated, real files/modes, keys and TLS retained."""
import ipaddress
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from lumen.services.infrastructure import guest_api, guest_bootstrap
from lumen.services.infrastructure.transport import resource_identity


def ca_material():
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "runtime test CA")])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
            .not_valid_after(datetime.now(UTC) + timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), True).sign(key, hashes.SHA256()))
    return key, cert


def pem_key(key):
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())


def issue(ca_key, ca_cert, public_key, *, identity=None, seconds=3600, hostname=False):
    names = [x509.UniformResourceIdentifier(identity)] if identity else []
    if hostname:
        names.extend([x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))])
    cert = (x509.CertificateBuilder().subject_name(x509.Name([])).issuer_name(ca_cert.subject).public_key(public_key)
            .serial_number(x509.random_serial_number()).not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
            .not_valid_after(datetime.now(UTC) + timedelta(seconds=seconds))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
            .add_extension(x509.SubjectAlternativeName(names), False)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH, ExtendedKeyUsageOID.CLIENT_AUTH]), False)
            .sign(ca_key, hashes.SHA256()))
    return cert


def portable_permissions(monkeypatch):
    """Retain production atomic writer/reader checks except host UID/GID emulation.

    This makes tests runnable by a developer, without creating appuser or chowning
    host files. OS metadata is rewritten only in returned stat values.
    """
    from pathlib import Path
    account = SimpleNamespace(pw_uid=10001, pw_gid=10001, pw_dir="/home/appuser", pw_name="appuser")
    monkeypatch.setattr(guest_bootstrap, "service_account", lambda: account)
    monkeypatch.setattr(guest_api, "service_account", lambda: account)
    monkeypatch.setattr(os, "chown", lambda *args: None)
    monkeypatch.setattr(os, "fchown", lambda *args: None)
    original_lstat = Path.lstat
    original_fstat = os.fstat

    def ownership(info):
        values = list(info)
        values[4], values[5] = 0, 10001
        return os.stat_result(values)

    def lstat(path):
        return ownership(original_lstat(path))

    monkeypatch.setattr(Path, "lstat", lstat)
    monkeypatch.setattr(os, "fstat", lambda fd: ownership(original_fstat(fd)))
    return account


def identity_fixture(tmp_path, monkeypatch, *, role="api"):
    portable_permissions(monkeypatch)
    monkeypatch.setenv("LUMEN_GUEST_ROLE", role)
    monkeypatch.setenv("LUMEN_CONTROLLER_URL", "https://localhost")
    ca_key, ca_cert = ca_material()
    key = ec.generate_private_key(ec.SECP256R1())
    identity = {"role": role, "resource_id": str(uuid4()), "generation": 1}
    cert = issue(ca_key, ca_cert, key.public_key(), identity=resource_identity(**identity))
    directory = tmp_path / "identity"
    version = guest_bootstrap.write_identity(directory, identity, key_pem=pem_key(key),
                ca_pem=ca_cert.public_bytes(serialization.Encoding.PEM), cert_pem=cert.public_bytes(serialization.Encoding.PEM))
    guest_bootstrap.activate_version(directory, version)
    return directory, identity, ca_key, ca_cert


def guest_config(identity):
    return {**identity, "profile_id": "api-profile", "profile_digest": "a" * 64,
            "image": "registry/lumen@sha256:" + "b" * 64,
            "config": {"api_max_active_requests": 256, "enabled": True, "plugins": ["chat"]},
            "secrets": {"DATABASE_URL": "mysql://private", "REDIS_URL": "redis://private", "LUMEN_ENCRYPTION_KEY": "private-key"},
            "secret_env_names": ["DATABASE_URL", "REDIS_URL", "LUMEN_ENCRYPTION_KEY"],
            "renewal": {"leaf_ttl_seconds": 3600, "renew_interval_seconds": 1200, "overlap_seconds": 120},
            "operator_probe_fingerprints": ["c" * 64], "workload_class": "batch" if identity["role"] == "worker" else None}


def counters(**overrides):
    return {"status": "ok", "database": True, "plugins": True, "checkpointer": None,
            "active_requests": 0, "active_sse": 0, "active_ws": 0, "ttft_samples": 0,
            "p95_ttft_ms": None, "observed_at": datetime.now(UTC).isoformat(), **overrides}
