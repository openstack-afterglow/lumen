"""Operator-pinned mTLS identities for fixed (non-cloud-managed) workers.

These leaves authorize only a live registration's own fenced run. They are not
controller probe identities and carry no cluster credential or dispatch key.
"""
from __future__ import annotations

import hmac
import os
import stat
from datetime import UTC, datetime

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.x509.oid import ExtendedKeyUsageOID

from lumen.services.infrastructure.config import RuntimeConfig


class FixedWorkerIdentityError(ValueError):
    """A fixed worker has no currently approved dedicated identity."""


def approved_fingerprint(config: RuntimeConfig, fingerprint: str | None) -> bool:
    return isinstance(fingerprint, str) and any(
        hmac.compare_digest(item.certificate_fingerprint, fingerprint) for item in config.fixed_workers
    )


def approved_peer(config: RuntimeConfig, identity: str, fingerprint: str) -> bool:
    return any(
        identity == f"spiffe://lumen/fixed-worker/{item.id}"
        and hmac.compare_digest(item.certificate_fingerprint, fingerprint)
        for item in config.fixed_workers
    )


def _read(path: str, *, private: bool) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        mode = stat.S_IMODE(info.st_mode)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid not in {0, os.geteuid()}
                or info.st_size > 16384 or mode & 0o022
                or (private and mode not in {0o400, 0o440, 0o600, 0o640})):
            raise FixedWorkerIdentityError("fixed worker identity unavailable")
        data = os.read(fd, 16385)
        if len(data) > 16384:
            raise FixedWorkerIdentityError("fixed worker identity unavailable")
        return data
    finally:
        os.close(fd)


def load_identity(config: RuntimeConfig) -> str | None:
    """Return a fresh approved leaf fingerprint, or None when fixed mTLS is disabled."""
    tls = config.tls
    if tls is None or tls.worker_client_cert_file is None:
        return None
    try:
        leaves = x509.load_pem_x509_certificates(_read(tls.worker_client_cert_file, private=False))
        if len(leaves) != 1 or tls.worker_client_key_file is None:
            raise FixedWorkerIdentityError("fixed worker identity unavailable")
        _read(tls.worker_client_key_file, private=True)
        leaf = leaves[0]
        fingerprint = leaf.fingerprint(hashes.SHA256()).hex()
        names = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        uris = names.get_values_for_type(x509.UniformResourceIdentifier)
        usage = leaf.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
        constraints = leaf.extensions.get_extension_for_class(x509.BasicConstraints).value
        now = datetime.now(UTC)
        if (len(uris) != 1 or not approved_peer(config, uris[0], fingerprint)
                or constraints.ca or ExtendedKeyUsageOID.CLIENT_AUTH not in usage
                or not leaf.not_valid_before_utc <= now < leaf.not_valid_after_utc):
            raise FixedWorkerIdentityError("fixed worker identity unavailable")
        return fingerprint
    except (OSError, ValueError, x509.ExtensionNotFound) as exc:
        raise FixedWorkerIdentityError("fixed worker identity unavailable") from exc
