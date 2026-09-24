"""Authenticated Zun REST adapter (not the SDK's unrelated Magnum proxy)."""

from __future__ import annotations

import base64
import io
import re
import tarfile
from urllib.parse import urlsplit

from keystoneauth1.exceptions.http import NotFound

from lumen.services.infrastructure.config import CloudProfile, PoolConfig, ZunProfile
from lumen.services.infrastructure.nova import cloud_connection, validate_controller_material
from lumen.services.infrastructure.providers import (
    DeleteResult,
    Observation,
    ResourceIntent,
    ResourceRef,
    addresses,
    checked_ref,
    owned,
    ownership,
)

_VERSION = re.compile(r"^1\.(\d+)$")
_ID = re.compile(r"^[a-zA-Z0-9_-]+$")
_BOOTSTRAP_DIR = "var/lib/lumen/bootstrap"
_BOOTSTRAP_FILE = f"{_BOOTSTRAP_DIR}/token"


class ZunProvider:
    requires_delivery = True
    def __init__(self, cloud: CloudProfile, pool: PoolConfig, deployment_id: str,
                 *, controller_url: str, controller_ca_pem: str):
        if pool.backend != "zun" or not isinstance(pool.profile, ZunProfile):
            raise ValueError("Zun provider requires Zun pool")
        if pool.cloud_profile_id != cloud.id or cloud.purpose != "sandbox" or pool.role != "sandbox":
            raise ValueError("Zun requires sandbox-isolated project")
        if pool.sandbox is None or pool.profile.memory_mib * 1024 * 1024 < pool.sandbox.memory_bytes:
            raise ValueError("Zun sandbox policy exceeds container memory")
        if not deployment_id:
            raise ValueError("deployment_id is required")
        validate_controller_material(controller_url, controller_ca_pem)
        self.cloud = cloud
        self.pool = pool
        self.deployment_id = deployment_id
        self.controller_url = controller_url
        self.controller_ca_pem = controller_ca_pem
        self.connection = cloud_connection(cloud)
        self.session = self.connection.session
        endpoint = self.session.get_endpoint(
            service_type="container", interface=cloud.interface, region_name=cloud.region_name,
        )
        if not endpoint or urlsplit(endpoint).scheme not in {"https", "http"}:
            raise ValueError("Zun endpoint missing or invalid in Keystone catalog")
        self.endpoint = endpoint.rstrip("/")
        self._microversion: str | None = None

    def _request(self, method: str, path: str, **kwargs):
        if self._microversion is None:
            self._negotiate()
        return self.session.request(
            method=method, url=f"{self.endpoint}/{path.lstrip('/')}",
            headers={"OpenStack-API-Version": f"container {self._microversion}"},
            authenticated=True, raise_exc=True, log=False, **kwargs,
        )

    def _negotiate(self) -> None:
        # The catalog endpoint can end in /v1 or /v1/<project_id>.
        parts = urlsplit(self.endpoint)
        segments = parts.path.rstrip("/").split("/")
        if "v1" not in segments:
            raise ValueError("Zun catalog endpoint must include v1")
        root_path = "/".join(segments[:segments.index("v1")]) or "/"
        root = parts._replace(path=root_path.rstrip("/") + "/", query="", fragment="").geturl()
        payload = self.session.get(root, authenticated=True, raise_exc=True).json()
        versions = payload.get("versions", [])
        if isinstance(versions, dict):
            versions = versions.get("values", [])
        if not isinstance(versions, list):
            raise ValueError("invalid Zun version discovery")
        required = 31 if self.pool.profile.registry_id else 25
        for version in versions:
            if not isinstance(version, dict) or not str(version.get("id", "")).lower().startswith("v1"):
                continue
            maximum = _VERSION.fullmatch(str(version.get("version", version.get("max_version", ""))))
            minimum = _VERSION.fullmatch(str(version.get("min_version", "1.1")))
            if minimum and maximum and int(minimum[1]) <= required <= int(maximum[1]):
                self._microversion = f"1.{required}"
                return
        raise ValueError(f"Zun requires documented microversion 1.{required} (1.25 archive, 1.31 registry)")

    def preflight(self, pool: PoolConfig | None = None) -> None:
        if pool is not None and pool != self.pool:
            raise ValueError("pool does not match provider")
        self._negotiate()
        if self.connection.network.find_network(self.pool.network_id) is None:
            raise ValueError("Zun network unavailable")
        for group_id in self.pool.security_group_ids:
            if self.connection.network.find_security_group(group_id) is None:
                raise ValueError("Zun security group unavailable")

    def _container(self, external_id: str) -> dict | None:
        if not _ID.fullmatch(external_id):
            raise ValueError("invalid container identifier")
        try:
            data = self._request("GET", f"containers/{external_id}").json()
        except NotFound:
            return None
        return data.get("container", data)

    def _observation(self, container: dict) -> Observation:
        ips = addresses(container.get("addresses"))
        return Observation(
            provider_id=container["uuid"], state=container["status"].lower(),
            address=ips[0] if ips else None, port=8013,
            metadata=dict(container.get("labels") or {}),
        )

    def create(self, intent: ResourceIntent) -> Observation:
        if intent.pool_id != self.pool.name or not intent.resource_id or intent.role != self.pool.role or intent.generation < 1:
            raise ValueError("intent does not match pool")
        profile = self.pool.profile
        body = {
            "name": f"lumen-{intent.resource_id}-g{intent.generation}",
            "image": intent.image_ref,
            "image_driver": profile.image_driver,
            "cpu": profile.cpu,
            "memory": profile.memory_mib,
            "labels": ownership(self.deployment_id, intent),
            "nets": [{"network": self.pool.network_id}],
            "security_groups": list(self.pool.security_group_ids),
            "restart_policy": {"Name": "no"},
            "auto_remove": False,
            "auto_heal": False,
            "environment": {
                "SANDBOX_BOOTSTRAP_FILE": "/" + _BOOTSTRAP_FILE,
                "SANDBOX_CONTROLLER_URL": self.controller_url,
                "SANDBOX_CONTROLLER_CA": "/" + _BOOTSTRAP_DIR + "/ca.pem",
                "SANDBOX_PORT": "8013",
                "SANDBOX_MEMORY_BYTES": str(self.pool.sandbox.memory_bytes),
                "SANDBOX_WORKSPACE_BYTES": str(self.pool.sandbox.workspace_bytes),
                "SANDBOX_CPU_QUOTA": str(self.pool.sandbox.cpu_millis * 100),
                "SANDBOX_PIDS_LIMIT": str(min(self.pool.sandbox.pids_limit, profile.pids_limit)),
            },
        }
        if profile.registry_id:
            body["registry"] = profile.registry_id
        # No run=true: the manager must never start before token delivery.
        data = self._request("POST", "containers/", json=body).json()
        return self._observation(data.get("container", data))

    def observe(self, ref: ResourceRef) -> Observation:
        if ref.pool_id != self.pool.name:
            raise ValueError("pool does not match provider")
        container = self._container(ref.provider_id)
        if container is None:
            return Observation(ref.provider_id, "absent", None, None, {})
        checked_ref(container.get("labels") or {}, self.deployment_id, ref)
        return self._observation(container)

    def list_owned(self, pool_id: str) -> list[Observation]:
        if pool_id != self.pool.name:
            raise ValueError("pool does not match provider")
        result = []
        marker = None
        while True:
            params = {"limit": 100}
            if marker:
                params["marker"] = marker
            data = self._request("GET", "containers/", params=params).json()
            batch = data["containers"]
            for container in batch:
                if owned(container.get("labels") or {}, self.deployment_id, pool_id):
                    result.append(self._observation(container))
            if len(batch) < 100:
                return result
            marker = batch[-1]["uuid"]

    def delete(self, ref: ResourceRef) -> DeleteResult:
        if ref.pool_id != self.pool.name:
            raise ValueError("pool does not match provider")
        container = self._container(ref.provider_id)
        if container is None:
            return DeleteResult(absent=True)
        checked_ref(container.get("labels") or {}, self.deployment_id, ref)
        try:
            self._request("DELETE", f"containers/{ref.provider_id}", params={"stop": "true"})
        except NotFound:
            return DeleteResult(absent=True)
        return DeleteResult(absent=False)

    def deliver_bootstrap(self, ref: ResourceRef, token: str) -> bool:
        """Copy a root-only token into a CREATED container, then start it."""
        if ref.pool_id != self.pool.name:
            raise ValueError("pool does not match provider")
        container = self._container(ref.provider_id)
        if container is None:
            raise ValueError("container no longer exists")
        checked_ref(container.get("labels") or {}, self.deployment_id, ref)
        if container["status"].lower() not in {"created", "stopped"}:
            return False
        if not token:
            raise ValueError("bootstrap token required")
        output = io.BytesIO()
        raw = token.encode("ascii")
        with tarfile.open(fileobj=output, mode="w") as archive:
            for directory in ("var/lib/lumen", _BOOTSTRAP_DIR):
                member = tarfile.TarInfo(directory + "/")
                member.type = tarfile.DIRTYPE
                member.mode = 0o700
                member.uid = member.gid = 0
                archive.addfile(member)
            for name, raw_content in (("token", raw), ("ca.pem", self.controller_ca_pem.encode("ascii"))):
                member = tarfile.TarInfo(f"{_BOOTSTRAP_DIR}/{name}")
                member.size = len(raw_content)
                member.mode = 0o600
                member.uid = member.gid = 0
                archive.addfile(member, io.BytesIO(raw_content))
        # Zun 1.25 requires base64 tar in the JSON data member. The actual
        # controller parameter is `path` (despite API-ref naming it destination_path).
        self._request(
            "POST", f"containers/{ref.provider_id}/put_archive",
            params={"path": "/"},
            json={"data": base64.b64encode(output.getvalue()).decode("ascii")},
        )
        self._request("POST", f"containers/{ref.provider_id}/start")
        return True
