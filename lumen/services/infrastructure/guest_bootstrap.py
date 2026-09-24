"""Guest entrypoint: exchange a root-only one-use token before starting the service.

Invoke as ``python -m lumen.services.infrastructure.guest_bootstrap --role worker|api -- COMMAND``.
Trusted guests stop the command before identity expiry; API guests also serve private mTLS readiness.
Nova writes /var/lib/lumen/bootstrap/{token,ca.pem,environment}; the image's
entrypoint reads environment as KEY=VALUE lines (never sources it as shell).
Zun sets the same variables directly and delivers token/ca.pem before start.
"""
from __future__ import annotations

import argparse
import json
import os
import stat
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed448, ed25519, padding, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID

from lumen.services.infrastructure.transport import resource_identity

BOOTSTRAP_DIR = Path("/var/lib/lumen/bootstrap")
IDENTITY_DIR = Path("/var/lib/lumen/identity")
_MAX_RESPONSE = 32_768


def _private_file(path: Path) -> bytes:
    info = path.lstat()
    parent = path.parent.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077
            or not stat.S_ISDIR(parent.st_mode) or parent.st_uid != 0 or parent.st_mode & 0o077
            or info.st_size > 65_536):
        raise RuntimeError("guest bootstrap material must be a root-only regular file")
    return path.read_bytes()


def _atomic_file(path: Path, content: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent = path.parent.lstat()
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.geteuid() or parent.st_mode & 0o077:
        raise RuntimeError("guest identity directory is not private")
    fd, temporary = tempfile.mkstemp(prefix=".bootstrap-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_identity(directory: Path, *, role: str, resource_id: str | None = None,
                  generation: int | None = None) -> dict:
    """Refuse stale, swapped, expired, or identity-mismatched persisted credentials."""
    manifest = json.loads(_private_file(directory / "identity.json"))
    if (not isinstance(manifest, dict) or manifest.get("role") != role
            or not isinstance(manifest.get("resource_id"), str)
            or type(manifest.get("generation")) is not int
            or (resource_id is not None and manifest["resource_id"] != resource_id)
            or (generation is not None and manifest["generation"] != generation)):
        raise RuntimeError("guest resource identity mismatch")
    identity = resource_identity(role, manifest["resource_id"], manifest["generation"])
    cert = x509.load_pem_x509_certificate(_private_file(directory / "cert.pem"))
    ca = x509.load_pem_x509_certificate(_private_file(directory / "ca.pem"))
    key = serialization.load_pem_private_key(_private_file(directory / "key.pem"), password=None)
    if (cert.issuer != ca.subject or cert.not_valid_before_utc > datetime.now(UTC)
            or cert.not_valid_after_utc <= datetime.now(UTC)
            or ca.not_valid_before_utc > datetime.now(UTC) or ca.not_valid_after_utc <= datetime.now(UTC)
            or not ca.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
            or cert.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
            != key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)):
        raise RuntimeError("guest certificate is invalid")
    try:
        public_key = ca.public_key()
        if isinstance(public_key, rsa.RSAPublicKey):
            public_key.verify(cert.signature, cert.tbs_certificate_bytes, padding.PKCS1v15(), cert.signature_hash_algorithm)
        elif isinstance(public_key, ec.EllipticCurvePublicKey):
            public_key.verify(cert.signature, cert.tbs_certificate_bytes, ec.ECDSA(cert.signature_hash_algorithm))
        elif isinstance(public_key, (ed25519.Ed25519PublicKey, ed448.Ed448PublicKey)):
            public_key.verify(cert.signature, cert.tbs_certificate_bytes)
        else:
            raise RuntimeError("unsupported guest CA key")
        names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        usage = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
        if (names.get_values_for_type(x509.UniformResourceIdentifier) != [identity]
                or ExtendedKeyUsageOID.CLIENT_AUTH not in usage):
            raise RuntimeError("guest certificate identity mismatch")
    except (x509.ExtensionNotFound, ValueError) as exc:
        raise RuntimeError("guest certificate identity mismatch") from exc
    return {**manifest, "certificate_fingerprint": cert.fingerprint(hashes.SHA256()).hex()}


def _require_root() -> None:
    if os.geteuid() != 0:
        raise RuntimeError("trusted guest bootstrap requires root")


def bootstrap(*, role: str, controller_url: str, ca_file: Path, token_file: Path,
              identity_dir: Path = IDENTITY_DIR) -> dict:
    """Exchange once; persist credentials before scrubbing the consumed token."""
    _require_root()
    if role not in {"worker", "api"}:
        raise RuntimeError("trusted guest bootstrap requires an explicit trusted role")
    endpoint = urlsplit(controller_url)
    if (endpoint.scheme != "https" or not endpoint.hostname or endpoint.username or endpoint.password
            or endpoint.query or endpoint.fragment or endpoint.path not in {"", "/"}):
        raise RuntimeError("invalid bootstrap controller URL")
    token = _private_file(token_file).decode("ascii").strip()
    ca_pem = _private_file(ca_file)
    x509.load_pem_x509_certificate(ca_pem)
    key = ec.generate_private_key(ec.SECP256R1())
    csr = x509.CertificateSigningRequestBuilder().subject_name(x509.Name([])).sign(key, hashes.SHA256())
    payload = {"token": token, "csr_pem": csr.public_bytes(serialization.Encoding.PEM).decode("ascii")}
    with (
        httpx.Client(verify=str(ca_file), trust_env=False, follow_redirects=False, timeout=10.0) as client,
        client.stream("POST", controller_url.rstrip("/") + "/v1/sandbox/bootstrap", json=payload) as response,
    ):
        if response.status_code != 200:
            raise RuntimeError("guest bootstrap rejected")
        raw = bytearray()
        for chunk in response.iter_bytes():
            if len(raw) + len(chunk) > _MAX_RESPONSE:
                raise RuntimeError("guest bootstrap response too large")
            raw.extend(chunk)
    data = json.loads(raw)
    if (not isinstance(data, dict) or data.get("role") != role
            or not isinstance(data.get("resource_id"), str)
            or type(data.get("generation")) is not int
            or data.get("client_ca_pem") != ca_pem.decode("ascii")):
        raise RuntimeError("guest bootstrap identity mismatch")
    resource_identity(role, data["resource_id"], data["generation"])
    identity_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    _atomic_file(identity_dir / "key.pem", key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    _atomic_file(identity_dir / "ca.pem", ca_pem)
    _atomic_file(identity_dir / "cert.pem", data["certificate_pem"].encode("ascii"))
    manifest = {"role": role, "resource_id": data["resource_id"], "generation": data["generation"]}
    _atomic_file(identity_dir / "identity.json", json.dumps(manifest).encode("ascii"))
    identity = load_identity(identity_dir, role=role)
    token_file.unlink()
    return identity


def main() -> None:
    parser = argparse.ArgumentParser(description="Exchange a one-use guest token before exec")
    parser.add_argument("--role", required=True, choices=("worker", "api"))
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command and args.command[0] == "--" else args.command
    if not command:
        parser.error("a command after -- is required")
    environment = BOOTSTRAP_DIR / "environment"
    if environment.exists():
        for line in _private_file(environment).decode("utf-8").splitlines():
            name, separator, value = line.partition("=")
            if not separator or name not in {"LUMEN_CONTROLLER_URL", "LUMEN_CONTROLLER_CA", "LUMEN_BOOTSTRAP_FILE"}:
                raise RuntimeError("invalid guest bootstrap environment")
            os.environ[name] = value
    controller_url = os.environ["LUMEN_CONTROLLER_URL"]
    ca_file = Path(os.environ["LUMEN_CONTROLLER_CA"])
    token_file = Path(os.environ["LUMEN_BOOTSTRAP_FILE"])
    identity_dir = Path(os.environ.get("LUMEN_GUEST_IDENTITY_DIR", str(IDENTITY_DIR)))
    if token_file.exists():
        manifest = bootstrap(role=args.role, controller_url=controller_url, ca_file=ca_file,
                             token_file=token_file, identity_dir=identity_dir)
    else:
        manifest = load_identity(identity_dir, role=args.role)
    runtime_file = BOOTSTRAP_DIR / "runtime.json"
    if runtime_file.exists():
        runtime = json.loads(_private_file(runtime_file))
        if not isinstance(runtime, dict) or runtime.get("controller_url") != os.environ.get("LUMEN_CONTROLLER_URL"):
            raise RuntimeError("guest runtime configuration mismatch")
        os.environ["RUNTIME_CONFIG"] = json.dumps(runtime)
    os.environ["LUMEN_RESOURCE_ID"] = manifest["resource_id"]
    os.environ["LUMEN_RESOURCE_GENERATION"] = str(manifest["generation"])
    os.environ["LUMEN_GUEST_IDENTITY_DIR"] = str(identity_dir)
    from lumen.services.infrastructure.guest_api import run, run_worker
    if args.role == "api":
        raise SystemExit(run(command, identity_dir, BOOTSTRAP_DIR / "api-readiness.json"))
    raise SystemExit(run_worker(command, identity_dir))


if __name__ == "__main__":
    main()
