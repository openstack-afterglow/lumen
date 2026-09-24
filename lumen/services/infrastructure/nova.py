"""Nova compute adapter using the installed openstacksdk compute/network/image proxies."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from openstack.connection import Connection
from openstack.exceptions import ResourceNotFound

from lumen.services.infrastructure.config import CloudProfile, NovaProfile, PoolConfig
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


def cloud_connection(cloud: CloudProfile) -> Connection:
    """Create an application-credential connection without logging or caching secrets."""
    secret_ref = cloud.application_credential_secret
    secret = os.environ[secret_ref.env] if secret_ref.env else Path(secret_ref.file).read_text(encoding="utf-8").strip()
    if not secret:
        raise ValueError("empty application credential secret")
    return Connection(
        auth_type="v3applicationcredential",
        auth_url=cloud.auth_url,
        application_credential_id=cloud.application_credential_id,
        application_credential_secret=secret,
        region_name=cloud.region_name,
        interface=cloud.interface,
        verify=cloud.ca_file or True,
    )

def validate_controller_material(controller_url: str, controller_ca_pem: str) -> None:
    url = urlsplit(controller_url)
    if (url.scheme != "https" or not url.hostname or url.username or url.password
            or url.query or url.fragment or any(char.isspace() for char in controller_url)):
        raise ValueError("controller URL must be a clean HTTPS URL")
    if not controller_ca_pem.startswith("-----BEGIN CERTIFICATE-----\n"):
        raise ValueError("controller CA certificate PEM is required")


class NovaProvider:
    requires_delivery = False

    def __init__(self, cloud: CloudProfile, pool: PoolConfig, deployment_id: str,
                 *, controller_url: str, controller_ca_pem: str,
                 managed_networks: tuple[str, ...] = ()):
        if pool.backend != "nova" or not isinstance(pool.profile, NovaProfile):
            raise ValueError("Nova provider requires Nova pool")
        if pool.cloud_profile_id != cloud.id or (pool.role == "sandbox") != (cloud.purpose == "sandbox"):
            raise ValueError("pool/cloud identity mismatch")
        if not deployment_id:
            raise ValueError("deployment_id is required")
        validate_controller_material(controller_url, controller_ca_pem)
        if pool.role == "worker" and not managed_networks:
            raise ValueError("trusted worker requires managed network CIDRs")
        self.cloud = cloud
        self.pool = pool
        self.deployment_id = deployment_id
        self.controller_url = controller_url
        self.controller_ca_pem = controller_ca_pem
        self.managed_networks = managed_networks
        self.connection = cloud_connection(cloud)
        self.compute = self.connection.compute

    def preflight(self, pool: PoolConfig | None = None) -> None:
        pool = pool or self.pool
        if pool != self.pool:
            raise ValueError("pool does not match provider")
        profile = self.pool.profile
        assert isinstance(profile, NovaProfile)
        if self.compute.find_flavor(profile.flavor_id) is None:
            raise ValueError("Nova flavor unavailable")
        image = self.connection.image.get_image(profile.guest_image_id)
        if image.status != "active" or image.hash_algo != "sha256" or image.hash_value != profile.guest_image_hash.removeprefix("sha256:"):
            raise ValueError("Nova guest image hash/status mismatch")
        if image.architecture and image.architecture != pool.architecture:
            raise ValueError("Nova image architecture mismatch")
        if self.connection.network.find_network(pool.network_id) is None:
            raise ValueError("Nova network unavailable")
        self._security_group_names = []
        for group_id in pool.security_group_ids:
            group = self.connection.network.find_security_group(group_id)
            if group is None:
                raise ValueError("Nova security group unavailable")
            self._security_group_names.append(group.name)

    def _observation(self, provider_id: str, state: str, labels: dict, network: object) -> Observation:
        ips = addresses(network)
        return Observation(
            provider_id=provider_id, state=state.lower(), address=ips[0] if ips else None,
            port=(self.pool.ingress.api_readiness_port if self.pool.ingress else None),
            metadata=dict(labels),
        )

    def create(self, intent: ResourceIntent) -> Observation:
        if intent.pool_id != self.pool.name or not intent.resource_id or intent.role != self.pool.role or intent.generation < 1:
            raise ValueError("intent does not match pool")
        if not intent.bootstrap_token:
            raise ValueError("Nova bootstrap token required before provisioning")
        profile = self.pool.profile
        assert isinstance(profile, NovaProfile)
        if not hasattr(self, "_security_group_names"):
            self.preflight()
        metadata = ownership(self.deployment_id, intent)
        attrs = {
            "name": f"lumen-{intent.resource_id}-g{intent.generation}",
            "image_id": profile.guest_image_id,
            "flavor_id": profile.flavor_id,
            "networks": [{"uuid": self.pool.network_id}],
            "security_groups": [{"name": name} for name in self._security_group_names],
            "metadata": metadata,
        }
        # Cloud-init writes root-only files. The image entrypoint reads environment
        # as data (not shell code), then exchanges the token before launching Lumen.
        guest_environment = (
            f"{'SANDBOX' if intent.role == 'sandbox' else 'LUMEN'}_CONTROLLER_URL={self.controller_url}\n"
            f"{'SANDBOX' if intent.role == 'sandbox' else 'LUMEN'}_CONTROLLER_CA=/var/lib/lumen/bootstrap/ca.pem\n"
            f"{'SANDBOX' if intent.role == 'sandbox' else 'LUMEN'}_BOOTSTRAP_FILE=/var/lib/lumen/bootstrap/token\n"
        )
        files = {"token": intent.bootstrap_token, "ca.pem": self.controller_ca_pem,
                 "environment": guest_environment}
        if intent.role == "worker":
            files["runtime.json"] = json.dumps({"controller_url": self.controller_url,
                                                 "managed_networks": self.managed_networks})
        if intent.role == "api":
            files["api-readiness.json"] = json.dumps({
                "readiness_port": self.pool.ingress.api_readiness_port,
                "service_port": self.pool.ingress.ingress_member_port,
            })
        cloud_init = (
            "#cloud-config\n"
            "bootcmd:\n  - [mkdir, -p, -m, '0700', /var/lib/lumen/bootstrap]\n"
            "write_files:\n" + "".join(
                f"  - path: /var/lib/lumen/bootstrap/{name}\n"
                "    owner: root:root\n    permissions: '0600'\n"
                f"    content: {json.dumps(content)}\n"
                for name, content in files.items()
            )
        )
        attrs["user_data"] = base64.b64encode(cloud_init.encode("utf-8")).decode("ascii")
        server = self.compute.create_server(**attrs)
        return self._observation(server.id, server.status, metadata, server.addresses)

    def observe(self, ref: ResourceRef) -> Observation:
        if ref.pool_id != self.pool.name:
            raise ValueError("pool does not match provider")
        try:
            server = self.compute.get_server(ref.provider_id)
        except ResourceNotFound:
            return Observation(ref.provider_id, "absent", None, None, {})
        labels = server.metadata or {}
        checked_ref(labels, self.deployment_id, ref)
        return self._observation(server.id, server.status, labels, server.addresses)

    def list_owned(self, pool_id: str) -> list[Observation]:
        if pool_id != self.pool.name:
            raise ValueError("pool does not match provider")
        result = []
        for server in self.compute.servers():
            labels = server.metadata or {}
            if owned(labels, self.deployment_id, pool_id):
                result.append(self._observation(server.id, server.status, labels, server.addresses))
        return result

    def delete(self, ref: ResourceRef) -> DeleteResult:
        if ref.pool_id != self.pool.name:
            raise ValueError("pool does not match provider")
        try:
            server = self.compute.get_server(ref.provider_id)
        except ResourceNotFound:
            return DeleteResult(absent=True)
        checked_ref(server.metadata or {}, self.deployment_id, ref)
        try:
            self.compute.delete_server(server.id, ignore_missing=False)
        except ResourceNotFound:
            return DeleteResult(absent=True)
        return DeleteResult(absent=False)

    def deliver_bootstrap(self, ref: ResourceRef, token: str) -> bool:
        """Nova already delivered the token atomically in create(user_data)."""
        return True
