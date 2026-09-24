"""Development-only Docker-backed sandbox provider for the system Compose controller.

Docker is a local cloud *simulation*, not a Nova/Zun or sandbox isolation proof.
Only the test controller has a Docker socket; this module is never installed into
production images. All mutations address one inspected, exactly labelled container.
"""

from __future__ import annotations

import http.client
import io
import json
import os
import socket
import tarfile
from pathlib import Path
from urllib.parse import quote, urlencode

from lumen.services.infrastructure.config import PoolConfig, RuntimeConfig
from lumen.services.infrastructure.providers import (
    DeleteResult,
    Observation,
    ResourceIntent,
    ResourceRef,
    checked_ref,
    owned,
    ownership,
)

_SOCKET = "/var/run/docker.sock"
_API = "/v1.41"


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self) -> None:
        super().__init__("localhost", timeout=10)

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(_SOCKET)


class DockerProvider:
    """One sandbox pool on the test engine, with exact-identity adoption/deletion."""

    requires_delivery = True

    def __init__(self, config: RuntimeConfig, pool: PoolConfig) -> None:
        if os.environ.get("AFTERGLOW_ENV", "development").strip().lower() != "development":
            raise ValueError("Docker provider is development-only")
        if pool.role != "sandbox" or pool.sandbox is None:
            raise ValueError("Docker test provider supports only sandbox pools")
        if config.tls is None or not config.controller_url.startswith("https://"):
            raise ValueError("Docker sandbox bootstrap needs controller TLS")
        self.pool = pool
        self.deployment_id = config.deployment_id
        self.controller_url = config.controller_url
        self.ca_pem = Path(config.tls.ca_file).read_bytes()
        self.image = os.environ["LUMEN_SANDBOX_IMAGE"]
        self.network = os.environ["LUMEN_SYSTEM_NETWORK"]
        if not self.image or not self.network or not self.ca_pem.startswith(b"-----BEGIN CERTIFICATE-----"):
            raise ValueError("Docker sandbox image, network and controller CA are required")

    @staticmethod
    def _request(method: str, path: str, body: object = None, *, archive: bool = False) -> tuple[int, object]:
        connection = _UnixConnection()
        try:
            data = body if archive else json.dumps(body).encode() if body is not None else None
            headers = {"Content-Type": "application/x-tar" if archive else "application/json"}
            connection.request(method, _API + path, body=data, headers=headers)
            response = connection.getresponse()
            raw = response.read()
            status = response.status
            payload = json.loads(raw) if raw and not archive else raw
            if status >= 400 and not (status == 404 and method in {"GET", "DELETE"}):
                raise RuntimeError(f"Docker {method} {path.split('?')[0]} failed ({status}): {payload}")
            return status, payload
        finally:
            connection.close()

    def preflight(self, pool: PoolConfig | None = None) -> None:
        if pool is not None and pool != self.pool:
            raise ValueError("pool does not match Docker provider")
        for path, kind in ((f"/networks/{quote(self.network, safe='')}", "network"),
                           (f"/images/{quote(self.image, safe='')}/json", "image")):
            status, _ = self._request("GET", path)
            if status != 200:
                raise ValueError(f"Docker sandbox {kind} unavailable")

    def _inspect(self, provider_id: str) -> dict | None:
        status, result = self._request("GET", f"/containers/{quote(provider_id, safe='')}/json")
        return result if status == 200 else None

    def _observation(self, container: dict) -> Observation:
        networks = container.get("NetworkSettings", {}).get("Networks") or {}
        address = (networks.get(self.network) or {}).get("IPAddress") or None
        state = container["State"]["Status"]
        if state in {"exited", "dead"}:
            state = "error"
        return Observation(
            provider_id=container["Id"], state=state, address=address, port=8013,
            metadata=dict(container["Config"].get("Labels") or {}),
        )

    def list_owned(self, pool_id: str) -> list[Observation]:
        if pool_id != self.pool.name:
            raise ValueError("pool does not match Docker provider")
        filters = {"label": [f"lumen_deployment={self.deployment_id}", f"lumen_pool={pool_id}"]}
        _, containers = self._request("GET", "/containers/json?" + urlencode({"all": "1", "filters": json.dumps(filters)}))
        observations = []
        for summary in containers:
            container = self._inspect(summary["Id"])
            if container is not None:
                labels = container["Config"].get("Labels") or {}
                if owned(labels, self.deployment_id, pool_id):
                    observations.append(self._observation(container))
        return observations

    def create(self, intent: ResourceIntent) -> Observation:
        if (intent.pool_id != self.pool.name or intent.role != self.pool.role
                or not intent.resource_id or intent.generation < 1 or intent.image_ref != self.image):
            raise ValueError("intent does not match Docker sandbox pool/image")
        labels = ownership(self.deployment_id, intent)
        if not owned(labels, self.deployment_id, self.pool.name):
            raise ValueError("incomplete Docker resource ownership intent")
        matches = [item for item in self.list_owned(intent.pool_id)
                   if item.metadata.get("lumen_resource") == intent.resource_id
                   and item.metadata.get("lumen_generation") == str(intent.generation)]
        if matches:
            if len(matches) != 1 or any(matches[0].metadata.get(key) != value for key, value in labels.items()):
                raise RuntimeError("Docker resource identity collision; refusing create")
            return matches[0]
        policy = self.pool.sandbox
        assert policy is not None
        name = f"lumen-{self.deployment_id}-{self.pool.name}-{intent.resource_id}-g{intent.generation}"
        environment = {
            "SANDBOX_BOOTSTRAP_FILE": "/var/lib/lumen/bootstrap/token",
            "SANDBOX_CONTROLLER_URL": self.controller_url,
            "SANDBOX_CONTROLLER_CA": "/var/lib/lumen/bootstrap/ca.pem",
            "SANDBOX_MEMORY_BYTES": str(policy.memory_bytes),
            "SANDBOX_WORKSPACE_BYTES": str(policy.workspace_bytes),
            "SANDBOX_CPU_QUOTA": str(policy.cpu_millis * 100),
            "SANDBOX_PIDS_LIMIT": str(policy.pids_limit),
        }
        body = {
            "Image": self.image, "Labels": labels,
            "Env": [f"{key}={value}" for key, value in environment.items()],
            "NetworkingConfig": {"EndpointsConfig": {self.network: {}}},
            "HostConfig": {
                "NetworkMode": self.network, "AutoRemove": False, "RestartPolicy": {"Name": "no"},
                "Memory": policy.memory_bytes, "PidsLimit": policy.pids_limit,
                "NanoCpus": policy.cpu_millis * 1_000_000,
                "CapAdd": ["NET_ADMIN", "SYS_ADMIN"],
                "SecurityOpt": ["no-new-privileges"],
            },
        }
        try:
            _, result = self._request("POST", "/containers/create?" + urlencode({"name": name}), body)
        except RuntimeError:
            # A lost create response or concurrent retry may already have installed this name.
            # Inspect rather than deleting/replacing the conflicting container.
            existing = self._inspect(name)
            if existing is None:
                raise
            checked_ref(existing["Config"].get("Labels") or {}, self.deployment_id,
                        ResourceRef(intent.resource_id, intent.generation, existing["Id"], intent.pool_id))
            if any((existing["Config"].get("Labels") or {}).get(key) != value for key, value in labels.items()):
                raise RuntimeError("Docker resource fingerprint/policy collision; refusing adoption")
            return self._observation(existing)
        container = self._inspect(result["Id"])
        if container is None:
            raise RuntimeError("Docker create response has no inspectable container")
        checked_ref(container["Config"].get("Labels") or {}, self.deployment_id,
                    ResourceRef(intent.resource_id, intent.generation, result["Id"], intent.pool_id))
        return self._observation(container)

    def observe(self, ref: ResourceRef) -> Observation:
        if ref.pool_id != self.pool.name:
            raise ValueError("pool does not match Docker provider")
        container = self._inspect(ref.provider_id)
        if container is None:
            return Observation(ref.provider_id, "absent", None, None, {})
        checked_ref(container["Config"].get("Labels") or {}, self.deployment_id, ref)
        return self._observation(container)

    def delete(self, ref: ResourceRef) -> DeleteResult:
        if self.observe(ref).state == "absent":
            return DeleteResult(absent=True)
        # Docker's DELETE endpoint targets precisely the inspected ID, never a label filter.
        status, _ = self._request("DELETE", f"/containers/{quote(ref.provider_id, safe='')}?force=1")
        return DeleteResult(absent=status == 404)

    def deliver_bootstrap(self, ref: ResourceRef, token: str) -> bool:
        if not token:
            raise ValueError("bootstrap token required")
        observation = self.observe(ref)
        if observation.state != "created":
            return False
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w") as tar:
            for directory in ("var/lib/lumen", "var/lib/lumen/bootstrap"):
                entry = tarfile.TarInfo(directory + "/")
                entry.type = tarfile.DIRTYPE
                entry.mode = 0o700
                tar.addfile(entry)
            for name, contents in (("token", token.encode("ascii")), ("ca.pem", self.ca_pem)):
                entry = tarfile.TarInfo(f"var/lib/lumen/bootstrap/{name}")
                entry.mode = 0o600
                entry.size = len(contents)
                tar.addfile(entry, io.BytesIO(contents))
        self._request("PUT", f"/containers/{quote(ref.provider_id, safe='')}/archive?path=/", archive.getvalue(), archive=True)
        self._request("POST", f"/containers/{quote(ref.provider_id, safe='')}/start")
        return True


def create(config: RuntimeConfig) -> dict[str, DockerProvider]:
    """Factory consumed by ``lumen.controller.build_providers`` in development only."""
    return {pool.name: DockerProvider(config, pool) for pool in config.pools if pool.enabled}
