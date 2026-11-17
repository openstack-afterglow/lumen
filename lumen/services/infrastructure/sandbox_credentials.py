"""Controller-only, renewable Drover namespace credentials. No cluster provisioning.

Each refresh obtains a fresh native password-scoped token. No broad kubeconfig,
ambient credentials, redirects, response text logging or request replay exists.
"""
from __future__ import annotations

import hashlib
import json
import os
import ssl
import stat
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes

from lumen.services.infrastructure.config import KubernetesClusterProfile

_CONTROLLER_ROOT = Path("/etc/lumen/controller")
_MAX_BODY = 262144
_MAX_TOKEN = 16384


class SandboxCredentialError(RuntimeError):
    """Stable local reason only; never upstream URLs, headers, bodies or secrets."""


def read_controller_file(path: str, *, password: bool = False) -> bytes:
    """Walk the protected subtree with directory descriptors; never follow links."""
    root = _CONTROLLER_ROOT
    try:
        parts = Path(path).relative_to(root).parts
        if not parts or parts[0] != "drover" or any(part in {"", ".", ".."} for part in parts):
            raise ValueError()
        descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            metadata = os.fstat(descriptor)
            if metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) & 0o022:
                raise ValueError()
            for part in parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
                metadata = os.fstat(descriptor)
                if metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) & 0o022:
                    raise ValueError()
            descriptor_file = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
            with os.fdopen(descriptor_file, "rb") as source:
                metadata = os.fstat(source.fileno())
                modes = {0o600} if password else {0o400, 0o600, 0o444, 0o644}
                if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0 or metadata.st_gid != 0
                        or stat.S_IMODE(metadata.st_mode) not in modes):
                    raise ValueError()
                data = source.read((_MAX_TOKEN if password else 65536) + 1)
        finally:
            os.close(descriptor)
        if not data or len(data) > (_MAX_TOKEN if password else 65536):
            raise ValueError()
        return data
    except (OSError, ValueError):
        raise SandboxCredentialError("sandbox_credential_material_invalid") from None


def _token(value: object) -> str:
    if (not isinstance(value, str) or not 0 < len(value) <= _MAX_TOKEN
            or any(ord(char) < 33 or ord(char) > 126 for char in value)):
        raise SandboxCredentialError("sandbox_credential_token_invalid")
    return value


def _expiry(value: object) -> datetime:
    try:
        if not isinstance(value, str) or len(value) > 64:
            raise ValueError()
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError()
        return result.astimezone(UTC)
    except (ValueError, OverflowError):
        raise SandboxCredentialError("sandbox_credential_expiry_invalid") from None


def ca_identity(data: bytes) -> tuple[bytes, ...]:
    try:
        if len(data) > 65536 or b"PRIVATE KEY" in data:
            raise ValueError()
        certificates = x509.load_pem_x509_certificates(data)
        if not certificates or any(not cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
                                   for cert in certificates):
            raise ValueError()
        return tuple(sorted(cert.fingerprint(hashes.SHA256()) for cert in certificates))
    except (ValueError, x509.ExtensionNotFound):
        raise SandboxCredentialError("sandbox_credential_ca_invalid") from None


def _object(data: bytes | bytearray) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError()
            result[key] = value
        return result

    def constant(_value):
        raise ValueError()

    try:
        result = json.loads(data, object_pairs_hook=unique, parse_constant=constant)
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except (ValueError, UnicodeError, RecursionError):
        raise SandboxCredentialError("sandbox_credential_response_invalid") from None


@dataclass(frozen=True)
class _Lease:
    token: str = field(repr=False)
    expires_at: datetime
    deadline: float


class DroverSandboxCredentials:
    """Synchronous singleflight source for the controller's threaded Kube transport."""

    def __init__(self, cluster: KubernetesClusterProfile, deployment_id: str, pool_name: str, *,
                 transport: httpx.BaseTransport | None = None, clock=None, monotonic=None):
        if cluster.drover is None:
            raise ValueError("Drover credential source required")
        self.cluster, self.source = cluster, cluster.drover
        self.deployment_id, self.pool_name = deployment_id, pool_name
        self._clock = clock or (lambda: datetime.now(UTC))
        self._monotonic = monotonic or time.monotonic
        self._lock = threading.Condition()
        self._refreshing = False
        self._failure: str | None = None
        self._lease: _Lease | None = None
        self._closed = False
        try:
            self._ca = ca_identity(Path(cluster.ca_file).read_bytes())
            trust = read_controller_file(self.source.ca_file)
            ca_identity(trust)
            context = ssl.create_default_context(cadata=trust.decode("ascii"))
        except (OSError, ValueError, UnicodeError, ssl.SSLError):
            raise SandboxCredentialError("sandbox_credential_material_invalid") from None
        self._trust_digest = hashlib.sha256(trust).digest()
        self._client = httpx.Client(
            verify=context, trust_env=False, follow_redirects=False,
            timeout=httpx.Timeout(self.source.request_timeout_seconds, connect=5, pool=5),
            transport=transport or httpx.HTTPTransport(verify=context, retries=0, trust_env=False,
                limits=httpx.Limits(max_connections=2, max_keepalive_connections=2)),
        )

    def _post(self, url: str, body: dict, *, token: str | None = None, status: int) -> tuple[dict, str | None]:
        headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
        if token is not None:
            headers["X-Auth-Token"] = token
        started = self._monotonic()
        try:
            with self._client.stream("POST", url, json=body, headers=headers) as response:
                if response.status_code != status:
                    raise SandboxCredentialError("sandbox_credential_request_rejected")
                if (response.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json"
                        or response.headers.get("content-encoding", "identity").lower() != "identity"):
                    raise SandboxCredentialError("sandbox_credential_response_invalid")
                raw = bytearray()
                for chunk in response.iter_bytes():
                    if (len(raw) + len(chunk) > _MAX_BODY
                            or self._monotonic() - started > self.source.request_timeout_seconds):
                        raise SandboxCredentialError("sandbox_credential_response_invalid")
                    raw.extend(chunk)
                return _object(raw), response.headers.get("X-Subject-Token")
        except httpx.HTTPError:
            raise SandboxCredentialError("sandbox_credential_transport_unavailable") from None

    def token(self) -> str:
        with self._lock:
            if self._refreshing:
                if not self._lock.wait_for(lambda: not self._refreshing or self._closed,
                                           timeout=2 * self.source.request_timeout_seconds + 5):
                    raise SandboxCredentialError("sandbox_credential_refresh_busy")
                if self._failure is not None:
                    raise SandboxCredentialError(self._failure)
            if self._closed:
                raise SandboxCredentialError("sandbox_credential_source_closed")
            now, tick = self._clock(), self._monotonic()
            if (self._lease is not None and (self._lease.expires_at - now).total_seconds() > self.source.refresh_margin_seconds
                    and self._lease.deadline - tick > self.source.refresh_margin_seconds):
                return self._lease.token
            # A failed renewal cannot fall back to a previously issued token.
            self._lease = None
            self._failure = None
            self._refreshing = True
        try:
            lease = self._refresh()
        except BaseException as exc:
            with self._lock:
                self._failure = str(exc) if isinstance(exc, SandboxCredentialError) else "sandbox_credential_refresh_failed"
                self._refreshing = False
                self._lock.notify_all()
            raise
        with self._lock:
            self._refreshing = False
            self._lock.notify_all()
            if self._closed:
                raise SandboxCredentialError("sandbox_credential_source_closed")
            self._lease = lease
            return lease.token

    def _refresh(self) -> _Lease:
        source = self.source
        trust = read_controller_file(source.ca_file)
        if hashlib.sha256(trust).digest() != self._trust_digest:
            raise SandboxCredentialError("sandbox_credential_material_changed")
        try:
            password = read_controller_file(source.password.file, password=True).decode("utf-8").removesuffix("\n")
            if not password or any(ord(char) < 32 or ord(char) == 127 for char in password):
                raise ValueError()
        except (ValueError, UnicodeError):
            raise SandboxCredentialError("sandbox_credential_material_invalid") from None
        auth_body = {"auth": {"identity": {"methods": ["password"], "password": {
            "user": {"id": source.user_id, "password": password},
        }}, "scope": {"project": {"id": source.project_id}}}}
        auth, native_token = self._post(source.auth_url.rstrip("/") + "/auth/tokens", auth_body, status=201)
        del password, auth_body
        try:
            native = auth["token"]
            native_expiry = _expiry(native["expires_at"])
            if (native["user"]["id"] != source.user_id or native["project"]["id"] != source.project_id
                    or native.get("methods") != ["password"] or native.get("system") is not None
                    or native.get("domain") is not None or native.get("OS-TRUST:trust") is not None
                    or (native_expiry - self._clock()).total_seconds() < 600):
                raise ValueError()
            native_token = _token(native_token)
        except (KeyError, TypeError, ValueError):
            raise SandboxCredentialError("sandbox_credential_identity_rejected") from None
        endpoint = source.endpoint.rstrip("/")
        if not endpoint.endswith("/v1"):
            endpoint += "/v1"
        result, _ = self._post(endpoint + "/clusters/" + quote(source.cluster_id, safe="") + "/sandbox-connection",
            {"deployment_id": self.deployment_id, "pool_name": self.pool_name,
             "runtime_class_name": self.cluster.runtime_class_name}, token=native_token, status=200)
        received_at, received_tick = self._clock(), self._monotonic()
        del native_token, auth
        expected = {"server": self.cluster.server.rstrip("/"), "namespace": self.cluster.namespace,
                    "namespace_uid": source.namespace_uid, "service_account": source.service_account,
                    "runtime_class_name": self.cluster.runtime_class_name}
        try:
            if (set(result) != set(expected) | {"ca_pem", "token", "expires_at"}
                    or any(result.get(key) != value for key, value in expected.items())
                    or not isinstance(result["ca_pem"], str)
                    or ca_identity(result["ca_pem"].encode("ascii")) != self._ca):
                raise ValueError()
            expiry = _expiry(result["expires_at"])
            remaining = (expiry - received_at).total_seconds()
            if (remaining <= source.refresh_margin_seconds or remaining > 900 or expiry > native_expiry):
                raise ValueError()
            value = _token(result["token"])
        except (KeyError, TypeError, ValueError, UnicodeError):
            raise SandboxCredentialError("sandbox_credential_scope_rejected") from None
        return _Lease(value, expiry, received_tick + remaining)

    def invalidate(self, token: str) -> None:
        """401 invalidates only the token used by that request, never a newer lease."""
        with self._lock:
            if self._lease is not None and self._lease.token == token:
                self._lease = None

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._lease = None
            self._lock.notify_all()
            self._lock.wait_for(lambda: not self._refreshing)
        self._client.close()
