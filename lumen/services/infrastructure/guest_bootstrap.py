"""Root guest supervisor entrypoint; cloud-init data is never sourced as shell."""
from __future__ import annotations

import argparse
import json
import os
import pwd
import re
import ssl
import stat
import tempfile
import uuid
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
RUN_DIR = Path("/run/lumen")
_MAX_RESPONSE = 256 * 1024
_BOOT_ENV = {"LUMEN_CONTROLLER_URL", "LUMEN_CONTROLLER_CA", "LUMEN_BOOTSTRAP_FILE",
             "LUMEN_GUEST_ROLE", "LUMEN_GUEST_PROFILE_ID", "LUMEN_GUEST_PROFILE_DIGEST", "LUMEN_GUEST_IMAGE"}


def service_account():
    account = pwd.getpwnam("appuser")
    if account.pw_uid == 0 or account.pw_gid == 0:
        raise RuntimeError("guest service account must not be root")
    return account


def _read_checked(path: Path, *, mode: int, parent_mode: int, gid: int | None = None) -> bytes:
    # O_NOFOLLOW and fstat bind the checked metadata to the bytes actually read.
    parent = path.parent.lstat()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
                or stat.S_IMODE(info.st_mode) != mode or info.st_size > 65_536
                or not stat.S_ISDIR(parent.st_mode) or parent.st_uid != 0
                or stat.S_IMODE(parent.st_mode) != parent_mode
                or (gid is not None and (info.st_gid != gid or parent.st_gid != gid))):
            raise RuntimeError("guest private material permissions invalid")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read(65_537)
        if len(data) > 65_536:
            raise RuntimeError("guest private material too large")
        return data
    finally:
        os.close(fd)


def _bootstrap_file(path: Path) -> bytes:
    return _read_checked(path, mode=0o600, parent_mode=0o700)


def _identity_file(path: Path) -> bytes:
    return _read_checked(path, mode=0o440, parent_mode=0o750, gid=service_account().pw_gid)


def _atomic_file(path: Path, content: bytes, *, shared: bool = False) -> None:
    gid = service_account().pw_gid if shared else 0
    path.parent.mkdir(mode=0o750 if shared else 0o700, parents=True, exist_ok=True)
    parent = path.parent.lstat()
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != 0:
        raise RuntimeError("guest state directory invalid")
    os.chown(path.parent, 0, gid)
    os.chmod(path.parent, 0o750 if shared else 0o700)
    fd, temporary = tempfile.mkstemp(prefix=".guest-", dir=path.parent)
    try:
        os.fchown(fd, 0, gid)
        os.fchmod(fd, 0o440 if shared else 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _sync_dir(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _sync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def identity_version(directory: Path) -> Path:
    """Resolve current once, so a rotation cannot mix a key, CA and certificate."""
    if (directory / "current").is_symlink():
        target = os.readlink(directory / "current")
        if Path(target).name != target or target in {".", ".."}:
            raise RuntimeError("guest identity pointer invalid")
        version = directory / target
        if version.is_symlink():
            raise RuntimeError("guest identity version invalid")
        return version
    if (directory / "identity.json").is_file():
        return directory
    raise RuntimeError("guest identity unavailable")


def load_identity(directory: Path, *, role: str, resource_id: str | None = None,
                  generation: int | None = None) -> dict:
    directory = identity_version(directory)
    manifest = json.loads(_identity_file(directory / "identity.json"))
    if (not isinstance(manifest, dict) or manifest.get("role") != role
            or not isinstance(manifest.get("resource_id"), str)
            or type(manifest.get("generation")) is not int
            or (resource_id is not None and manifest["resource_id"] != resource_id)
            or (generation is not None and manifest["generation"] != generation)):
        raise RuntimeError("guest resource identity mismatch")
    identity = resource_identity(role, manifest["resource_id"], manifest["generation"])
    cert = x509.load_pem_x509_certificate(_identity_file(directory / "cert.pem"))
    ca = x509.load_pem_x509_certificate(_identity_file(directory / "ca.pem"))
    key = serialization.load_pem_private_key(_identity_file(directory / "key.pem"), password=None)
    now = datetime.now(UTC)
    if (cert.issuer != ca.subject or cert.not_valid_before_utc > now or cert.not_valid_after_utc <= now
            or ca.not_valid_before_utc > now or ca.not_valid_after_utc <= now
            or not ca.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
            or cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
            or cert.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
            != key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)):
        raise RuntimeError("guest certificate is invalid")
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
            or ExtendedKeyUsageOID.CLIENT_AUTH not in usage or ExtendedKeyUsageOID.SERVER_AUTH not in usage):
        raise RuntimeError("guest certificate identity mismatch")
    return {**manifest, "certificate_fingerprint": cert.fingerprint(hashes.SHA256()).hex()}


def write_identity(directory: Path, manifest: dict, *, key_pem: bytes, ca_pem: bytes,
                   cert_pem: bytes, version: str | None = None) -> Path:
    version = version or uuid.uuid4().hex
    if not re.fullmatch(r"[a-f0-9-]{32,36}", version):
        raise RuntimeError("invalid identity version")
    directory.mkdir(mode=0o750, parents=True, exist_ok=True)
    os.chown(directory, 0, service_account().pw_gid)
    os.chmod(directory, 0o750)
    target = directory / version
    for name, content in {"key.pem": key_pem, "ca.pem": ca_pem, "cert.pem": cert_pem,
                          "identity.json": json.dumps(manifest).encode()}.items():
        _atomic_file(target / name, content, shared=True)
    load_identity(target, role=manifest["role"], resource_id=manifest["resource_id"], generation=manifest["generation"])
    return target


def activate_version(directory: Path, version: Path) -> None:
    if version.parent != directory:
        raise RuntimeError("invalid identity activation")
    temporary = directory / (".current-" + uuid.uuid4().hex)
    try:
        temporary.symlink_to(version.name)
        os.replace(temporary, directory / "current")
        _sync_dir(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _endpoint(url: str) -> str:
    endpoint = urlsplit(url)
    if (endpoint.scheme != "https" or not endpoint.hostname or endpoint.username or endpoint.password
            or endpoint.query or endpoint.fragment or endpoint.path not in {"", "/"}):
        raise RuntimeError("invalid guest controller URL")
    return url.rstrip("/")


def controller_request(url: str, path: str, directory: Path, *, body: dict | None = None) -> dict:
    version = identity_version(directory)
    load_identity(version, role=os.environ["LUMEN_GUEST_ROLE"])
    context = ssl.create_default_context(cafile=str(version / "ca.pem"))
    context.load_cert_chain(str(version / "cert.pem"), str(version / "key.pem"))
    return _request(_endpoint(url) + path, context, body)


def _request(url: str, verify, body: dict | None) -> dict:
    with (httpx.Client(verify=verify, trust_env=False, follow_redirects=False, timeout=10) as client,
          client.stream("POST" if body is not None else "GET", url, json=body) as response):
        if response.status_code != 200:
            raise RuntimeError("guest controller request rejected")
        data = bytearray()
        for chunk in response.iter_bytes():
            if len(data) + len(chunk) > _MAX_RESPONSE:
                raise RuntimeError("guest controller response too large")
            data.extend(chunk)
    parsed = json.loads(data)
    if not isinstance(parsed, dict):
        raise RuntimeError("guest controller response invalid")
    return parsed


def bootstrap(*, role: str, controller_url: str, ca_file: Path, token_file: Path,
              identity_dir: Path = IDENTITY_DIR) -> dict:
    _require_root()
    if role not in {"worker", "api"}:
        raise RuntimeError("invalid trusted guest role")
    ca_pem = _bootstrap_file(ca_file)
    key = ec.generate_private_key(ec.SECP256R1())
    csr = x509.CertificateSigningRequestBuilder().subject_name(x509.Name([])).sign(key, hashes.SHA256())
    data = _request(_endpoint(controller_url) + "/v1/sandbox/bootstrap", str(ca_file),
                    {"token": _bootstrap_file(token_file).decode("ascii").strip(),
                     "csr_pem": csr.public_bytes(serialization.Encoding.PEM).decode("ascii")})
    if (data.get("role") != role or type(data.get("generation")) is not int
            or not isinstance(data.get("resource_id"), str) or data.get("client_ca_pem") != ca_pem.decode("ascii")):
        raise RuntimeError("guest bootstrap identity mismatch")
    manifest = {name: data[name] for name in ("role", "resource_id", "generation")}
    version = write_identity(identity_dir, manifest, key_pem=key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()),
        ca_pem=ca_pem, cert_pem=data["certificate_pem"].encode("ascii"))
    activate_version(identity_dir, version)
    token_file.unlink()
    return load_identity(identity_dir, role=role)


def verify_guest_config(data: dict, identity: dict, expected: dict, running_image: str) -> dict:
    matches = {"profile_id": expected.get("LUMEN_GUEST_PROFILE_ID"),
               "profile_digest": expected.get("LUMEN_GUEST_PROFILE_DIGEST"),
               "image": expected.get("LUMEN_GUEST_IMAGE"), "role": expected.get("LUMEN_GUEST_ROLE"),
               "resource_id": identity["resource_id"], "generation": identity["generation"]}
    if (any(value is None or data.get(key) != value for key, value in matches.items())
            or matches["role"] != identity["role"] or not running_image or running_image != matches["image"]
            or not re.fullmatch(r"[^\s@]+@sha256:[a-f0-9]{64}", running_image)):
        raise RuntimeError("guest profile identity mismatch")
    if type(data.get("generation")) is not int:
        raise RuntimeError("guest profile identity mismatch")
    config, secrets = data.get("config"), data.get("secrets")
    required = {"DATABASE_URL", "REDIS_URL", "LUMEN_ENCRYPTION_KEY"} | set(data.get("secret_env_names", ()))
    if (not isinstance(config, dict) or not isinstance(secrets, dict) or not required.issubset(secrets)
            or any(not isinstance(v, str) or not v for v in secrets.values())
            or any(not re.fullmatch(r"[A-Z][A-Z0-9_]*", k) for k in secrets)
            or any(k.lower() in {"runtime_config", "os_auth_url", "os_application_credential_id",
                                   "os_application_credential_secret"} for k in (*config, *secrets))):
        raise RuntimeError("guest profile config invalid")
    if ("config_keys" in data and set(config) != set(data["config_keys"])) or (
            "secret_env_names" in data and set(secrets) != set(data["secret_env_names"])):
        raise RuntimeError("guest profile allowlist mismatch")
    renewal = data.get("renewal")
    if (not isinstance(renewal, dict) or any(type(renewal.get(k)) is not int or renewal[k] <= 0
            for k in ("leaf_ttl_seconds", "renew_interval_seconds", "overlap_seconds"))
            or renewal["renew_interval_seconds"] >= renewal["leaf_ttl_seconds"] - 120):
        raise RuntimeError("guest renewal policy invalid")
    pins = data.get("operator_probe_fingerprints")
    if (not isinstance(pins, list) or any(not isinstance(pin, str) or not re.fullmatch(r"[a-f0-9]{64}", pin) for pin in pins)
            or (identity["role"] == "api" and not pins)):
        raise RuntimeError("guest operator pins invalid")
    if identity["role"] == "worker" and data.get("workload_class") not in {"online_text", "online_media", "batch"}:
        raise RuntimeError("guest workload class invalid")
    return data


def _toml(value) -> str:
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=True)
    if type(value) is bool:
        return "true" if value else "false"
    if type(value) in {int, float}:
        return json.dumps(value, allow_nan=False)
    if isinstance(value, list):
        return "[" + ", ".join(_toml(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(json.dumps(k) + " = " + _toml(v) for k, v in value.items()) + " }"
    raise RuntimeError("guest config value invalid")


def install_config(data: dict, run_dir: Path = RUN_DIR) -> dict[str, str]:
    content = "[lumen]\n" + "\n".join(json.dumps(k) + " = " + _toml(v) for k, v in data["config"].items()) + "\n"
    _atomic_file(run_dir / "lumen.conf", content.encode(), shared=True)
    _atomic_file(run_dir / "secrets.json", json.dumps(data["secrets"]).encode(), shared=True)
    environment = {**data["secrets"], "LUMEN_CONFIG_FILE": str(run_dir / "lumen.conf")}
    if data["role"] == "worker":
        environment["WORKER_WORKLOAD_CLASSES"] = json.dumps([data["workload_class"]])
    return environment


def _require_root() -> None:
    if os.geteuid() != 0:
        raise RuntimeError("trusted guest bootstrap requires root")


def main() -> None:
    _require_root()
    parser = argparse.ArgumentParser(description="Start a verified trusted guest")
    parser.add_argument("--role", required=True, choices=("worker", "api"))
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command and args.command[0] == "--" else args.command
    if not command:
        parser.error("a command after -- is required")
    # The image runner independently verifies RepoDigests; never overwrite this evidence
    # with the cloud-init claim before comparing it.
    running_image = os.environ.get("LUMEN_GUEST_IMAGE", "")
    expected = {}
    for line in _bootstrap_file(BOOTSTRAP_DIR / "environment").decode().splitlines():
        name, separator, value = line.partition("=")
        if not separator or name not in _BOOT_ENV or name in expected:
            raise RuntimeError("invalid guest bootstrap environment")
        expected[name] = value
    if expected.get("LUMEN_GUEST_ROLE") != args.role:
        raise RuntimeError("guest role mismatch")
    os.environ.update(expected)
    identity_dir = Path(os.environ.get("LUMEN_GUEST_IDENTITY_DIR", str(IDENTITY_DIR)))
    token_file = Path(os.environ["LUMEN_BOOTSTRAP_FILE"])
    if token_file.exists() and not (identity_dir / "current").exists():
        manifest = bootstrap(role=args.role, controller_url=os.environ["LUMEN_CONTROLLER_URL"],
                             ca_file=Path(os.environ["LUMEN_CONTROLLER_CA"]), token_file=token_file, identity_dir=identity_dir)
    else:
        # A committed activation may have outlived the old pin's overlap while
        # this supervisor was down. Recover with the persisted pending certificate
        # BEFORE guest-config authenticates using the local current pointer.
        if (identity_dir / ".renewal" / "request.json").exists():
            from lumen.services.infrastructure.guest_api import RenewalAgent
            previous_manifest = json.loads(_identity_file(identity_version(identity_dir) / "identity.json"))
            if previous_manifest.get("role") != args.role:
                raise RuntimeError("guest resource identity mismatch")
            resource_identity(args.role, previous_manifest["resource_id"], previous_manifest["generation"])
            renewal = RenewalAgent(identity_dir, previous_manifest, os.environ["LUMEN_CONTROLLER_URL"])
            recovered = renewal.renew()
            activate_version(identity_dir, recovered)
            renewal.complete()
        manifest = load_identity(identity_dir, role=args.role)
    data = verify_guest_config(controller_request(os.environ["LUMEN_CONTROLLER_URL"],
                               "/v1/runtime/guest-config", identity_dir), manifest, expected, running_image)
    os.environ.update(install_config(data))
    runtime_file = BOOTSTRAP_DIR / "runtime.json"
    if args.role == "worker":
        runtime = json.loads(_bootstrap_file(runtime_file))
        if not isinstance(runtime, dict) or runtime.get("controller_url") != os.environ["LUMEN_CONTROLLER_URL"]:
            raise RuntimeError("guest runtime configuration mismatch")
        os.environ["RUNTIME_CONFIG"] = json.dumps(runtime)
    os.environ.update(LUMEN_RESOURCE_ID=manifest["resource_id"], LUMEN_RESOURCE_GENERATION=str(manifest["generation"]),
                      LUMEN_GUEST_IDENTITY_DIR=str(identity_dir))
    from lumen.services.infrastructure.guest_api import run, run_worker
    if args.role == "api":
        raise SystemExit(run(command, identity_dir, BOOTSTRAP_DIR / "api-readiness.json", guest_config=data))
    raise SystemExit(run_worker(command, identity_dir, guest_config=data))


if __name__ == "__main__":
    main()
