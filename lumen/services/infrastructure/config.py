"""Operator deployment values for autoscaling pools, cloud profiles and quotas.

Every enabled pool must carry finite positive maxima and a complete typed backend
profile; missing prerequisites fail startup/preflight with a stable reason instead of
disappearing through permissive parsing. Secrets are file/env references only.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Role = Literal["api", "worker", "sandbox"]
Backend = Literal["nova", "zun"]
# Bootstrap tokens expire after ten minutes; trusted guest identities live one hour.
# Reserve time for boot, ingress drain and controller retries before that deadline.
BOOTSTRAP_TOKEN_TTL_SECONDS = 600
TRUSTED_CERTIFICATE_LIFETIME_SECONDS = 3600
TRUSTED_CERTIFICATE_SAFETY_SECONDS = 60


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SecretRef(_Frozen):
    """Reference to operator secret material; never the material itself."""

    env: str | None = Field(default=None, min_length=1, max_length=190)
    file: str | None = Field(default=None, min_length=1, max_length=1024)

    @model_validator(mode="after")
    def exactly_one(self) -> SecretRef:
        if (self.env is None) == (self.file is None):
            raise ValueError("secret reference needs exactly one of env or file")
        return self


class TlsMaterial(_Frozen):
    """Controller listener certificate plus the internal CA that signs bootstrap CSRs."""

    ca_file: str = Field(min_length=1, max_length=1024)
    ca_key_file: str = Field(min_length=1, max_length=1024)
    cert_file: str = Field(min_length=1, max_length=1024)
    key_file: str = Field(min_length=1, max_length=1024)
    # Optional dedicated client identity for controller-to-guest readiness probes.
    # The HTTPS listener's server key must never be reused as a client credential.
    operator_client_cert_file: str | None = Field(default=None, min_length=1, max_length=1024)
    operator_client_key_file: str | None = Field(default=None, min_length=1, max_length=1024)

    @model_validator(mode="after")
    def client_pair(self) -> TlsMaterial:
        if (self.operator_client_cert_file is None) != (self.operator_client_key_file is None):
            raise ValueError("operator probe client certificate and key must be configured together")
        if (self.operator_client_cert_file == self.cert_file
                or self.operator_client_key_file == self.key_file):
            raise ValueError("operator probe client material must differ from listener material")
        return self


class CloudProfile(_Frozen):
    """One least-privilege application credential bound to a single OpenStack project."""

    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    auth_url: str = Field(min_length=1, max_length=2048)
    project_id: str = Field(min_length=1, max_length=64)
    region_name: str = Field(min_length=1, max_length=190)
    interface: Literal["public", "internal", "admin"] = "internal"
    ca_file: str | None = Field(default=None, max_length=1024)
    application_credential_id: str = Field(min_length=1, max_length=190)
    application_credential_secret: SecretRef
    purpose: Literal["trusted", "sandbox"]


class NovaProfile(_Frozen):
    backend: Literal["nova"] = "nova"
    flavor_id: str = Field(min_length=1, max_length=190)
    guest_image_id: str = Field(min_length=1, max_length=190)
    guest_image_hash: str = Field(pattern=r"^(sha256:)?[0-9a-f]{64}$")
    registry_proxy: str | None = Field(default=None, max_length=2048)


class ZunProfile(_Frozen):
    backend: Literal["zun"] = "zun"
    cpu: float = Field(gt=0, le=64)
    memory_mib: int = Field(ge=256, le=262144)
    pids_limit: int = Field(ge=16, le=65536)
    registry_id: str | None = Field(default=None, max_length=190)
    image_driver: Literal["docker", "glance"] = "docker"


class SandboxPolicy(_Frozen):
    workspace_bytes: int = Field(ge=1024 * 1024, le=64 * 1024 * 1024 * 1024)
    memory_bytes: int = Field(ge=64 * 1024 * 1024)
    cpu_millis: int = Field(ge=100)
    pids_limit: int = Field(ge=16)
    required_capabilities: tuple[str, ...] = (
        "user_namespace",
        "pid_namespace",
        "mount_namespace",
        "network_namespace",
        "cgroup_v2",
        "no_new_privs",
    )

    @model_validator(mode="after")
    def bounded_workspace(self) -> SandboxPolicy:
        if self.workspace_bytes > self.memory_bytes // 4:
            raise ValueError("workspace_bytes must not exceed memory_bytes/4")
        return self


class IngressConfig(_Frozen):
    """Operator-created Octavia pool membership; never per-request load balancers."""

    ingress_pool_id: str = Field(min_length=1, max_length=190)
    ingress_vip: str = Field(min_length=1, max_length=190)
    ingress_subnet_id: str = Field(min_length=1, max_length=190)
    ingress_member_port: int = Field(ge=1, le=65535)
    # Guest-only mTLS probe; never the public Octavia member port.
    api_readiness_port: int = Field(default=8013, ge=1, le=65535)
    api_target_active_requests: int = Field(ge=1)
    api_target_ttft_ms: int = Field(ge=1)
    @model_validator(mode="after")
    def distinct_readiness_port(self) -> IngressConfig:
        if self.api_readiness_port == self.ingress_member_port:
            raise ValueError("api_readiness_port must differ from public ingress_member_port")
        return self


class PoolConfig(_Frozen):
    name: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    role: Role
    backend: Backend
    enabled: bool = False
    cloud_profile_id: str = Field(min_length=1, max_length=64)
    image: str = Field(min_length=1, max_length=255)
    architecture: Literal["x86_64", "aarch64"]
    network_id: str = Field(min_length=1, max_length=190)
    security_group_ids: tuple[str, ...] = Field(min_length=1)
    min_replicas: int = Field(ge=0)
    max_replicas: int = Field(ge=0)
    slots_per_worker: int = Field(default=4, ge=1, le=64)
    target_wait_seconds: int = Field(default=10, ge=1)
    boot_timeout_seconds: int = Field(default=600, ge=30)
    idle_seconds: int = Field(default=300, ge=0)
    drain_seconds: int = Field(default=300, ge=0)
    # Measured from resource intent; boot and drain must also fit within the certificate lifetime.
    max_lifetime_seconds: int = Field(default=1800, ge=60)
    db_connection_budget: int | None = Field(default=None, ge=1)
    profile: Annotated[NovaProfile | ZunProfile, Field(discriminator="backend")]
    sandbox: SandboxPolicy | None = None
    ingress: IngressConfig | None = None

    @model_validator(mode="after")
    def enabled_pool_is_complete(self) -> PoolConfig:
        if self.profile.backend != self.backend:
            raise ValueError("pool backend must match its profile backend")
        if self.backend == "zun":
            if self.role != "sandbox":
                raise ValueError("Zun supports only sandbox pools; trusted API/worker require Nova")
            raise ValueError("Zun sandbox isolation policy is not enforced by the provider; use Nova until Zun isolation is implemented")
        if self.role == "sandbox" and self.sandbox is None:
            raise ValueError("sandbox pools require an explicit sandbox policy")
        if self.role != "sandbox" and self.sandbox is not None:
            raise ValueError("only sandbox pools accept a sandbox policy")
        if self.role != "api" and self.ingress is not None:
            raise ValueError("only api pools accept ingress configuration")
        if self.enabled:
            if self.max_replicas < 1 or self.max_replicas < self.min_replicas:
                raise ValueError("enabled pools require finite positive max_replicas >= min_replicas")
            if self.role in {"api", "worker"} and self.db_connection_budget is None:
                raise ValueError("enabled trusted pools require db_connection_budget")
            if self.role == "api" and self.ingress is None:
                raise ValueError("enabled api pools require operator ingress configuration")
        if (self.role in {"api", "worker"} and
                self.boot_timeout_seconds + self.max_lifetime_seconds + self.drain_seconds
                + TRUSTED_CERTIFICATE_SAFETY_SECONDS > TRUSTED_CERTIFICATE_LIFETIME_SECONDS):
            raise ValueError("trusted pool boot, lifetime, drain and safety margin must fit within the 3600s certificate lifetime")
        return self

    def digest(self) -> str:
        return hashlib.sha256(
            json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


class ProjectQuotaDefaults(_Frozen):
    max_active_children: int = Field(default=0, ge=0)
    max_active_sandboxes: int = Field(default=0, ge=0)
    max_sandbox_seconds: int = Field(default=0, ge=0)
    max_credit_reservation: str = Field(default="0", pattern=r"^\d+(\.\d{1,8})?$")


class RuntimeConfig(_Frozen):
    enabled: bool = False
    deployment_id: str = Field(default="lumen", pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    controller_url: str = Field(default="", max_length=2048)
    listen_host: str = "127.0.0.1"
    listen_port: int = Field(default=8013, ge=1, le=65535)
    tls: TlsMaterial | None = None
    reconcile_interval_seconds: int = Field(default=5, ge=1)
    pool_lease_seconds: int = Field(default=30, ge=5)
    dispatch_key: SecretRef | None = None
    # Operator-approved CIDRs the internal transport may reach; resource addresses outside
    # them are refused even when the controller observed them.
    managed_networks: tuple[str, ...] = ()
    max_parallel_cloud_operations: int = Field(default=4, ge=1, le=32)
    cloud_profiles: tuple[CloudProfile, ...] = ()
    pools: tuple[PoolConfig, ...] = ()
    project_quota_defaults: ProjectQuotaDefaults = Field(default_factory=ProjectQuotaDefaults)

    @model_validator(mode="after")
    def coherent(self) -> RuntimeConfig:
        profiles = {profile.id: profile for profile in self.cloud_profiles}
        if len(profiles) != len(self.cloud_profiles):
            raise ValueError("duplicate cloud profile id")
        names = [pool.name for pool in self.pools]
        if len(set(names)) != len(names):
            raise ValueError("duplicate pool name")
        for pool in self.pools:
            profile = profiles.get(pool.cloud_profile_id)
            if profile is None:
                raise ValueError(f"pool {pool.name} references unknown cloud profile")
            expected = "sandbox" if pool.role == "sandbox" else "trusted"
            if profile.purpose != expected:
                raise ValueError(f"pool {pool.name} must use a {expected} cloud profile")
        trusted = {profile.project_id for profile in self.cloud_profiles if profile.purpose == "trusted"}
        sandbox = {profile.project_id for profile in self.cloud_profiles if profile.purpose == "sandbox"}
        if trusted & sandbox:
            raise ValueError("trusted and sandbox pools must use different OpenStack projects")
        if self.enabled:
            if not self.tls:
                raise ValueError("enabled runtime requires controller TLS material")
            if self.dispatch_key is None:
                raise ValueError("enabled runtime requires a dispatch_key secret reference")
            if not self.managed_networks:
                raise ValueError("enabled runtime requires explicit managed_networks CIDRs")
            if (any(pool.enabled and pool.role in {"api", "sandbox"} for pool in self.pools)
                    and self.tls.operator_client_cert_file is None):
                raise ValueError("api/sandbox readiness requires operator probe client certificate")
            for cidr in self.managed_networks:
                try:
                    ipaddress.ip_network(cidr, strict=True)
                except ValueError as exc:
                    raise ValueError(f"managed_networks entry {cidr!r} is not a valid CIDR") from exc
            if not self.controller_url.startswith("https://"):
                raise ValueError("enabled runtime requires an https controller_url")
            if not any(pool.enabled for pool in self.pools):
                raise ValueError("enabled runtime requires at least one enabled pool")
        return self

    def pool(self, name: str) -> PoolConfig:
        for candidate in self.pools:
            if candidate.name == name:
                return candidate
        raise KeyError(name)

    def profile(self, profile_id: str) -> CloudProfile:
        for candidate in self.cloud_profiles:
            if candidate.id == profile_id:
                return candidate
        raise KeyError(profile_id)

    def public_view(self) -> dict[str, Any]:
        """Serialization for admin inventory; credential references are redacted."""
        return {
            "enabled": self.enabled,
            "deployment_id": self.deployment_id,
            "pools": [
                {
                    **pool.model_dump(mode="json", exclude={"profile"}),
                    "backend_profile": pool.profile.model_dump(mode="json", exclude={"registry_proxy"}),
                }
                for pool in self.pools
            ],
            "cloud_profiles": [
                profile.model_dump(mode="json", exclude={"application_credential_id", "application_credential_secret"})
                for profile in self.cloud_profiles
            ],
        }
