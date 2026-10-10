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

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, model_validator

Role = Literal["api", "worker", "sandbox"]
WorkloadClass = Literal["online_text", "online_media", "batch"]
Backend = Literal["nova", "zun"]


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


class RenewalPolicy(_Frozen):
    """Trusted leaf rotation; resource lifetime is a separate drain trigger."""

    enabled: bool = True
    leaf_ttl_seconds: Literal[3600] = 3600
    renew_interval_seconds: int = Field(default=1200, ge=60, le=3000)
    overlap_seconds: int = Field(default=120, ge=1, le=600)

    @model_validator(mode="after")
    def bounded_rotation(self) -> RenewalPolicy:
        if self.overlap_seconds >= self.renew_interval_seconds:
            raise ValueError("identity overlap must be shorter than the renewal interval")
        if self.renew_interval_seconds + self.overlap_seconds >= self.leaf_ttl_seconds:
            raise ValueError("renewal and overlap must complete before leaf expiry")
        return self


class GuestProfile(_Frozen):
    """Controller-only config file and role-scoped references served over mTLS.

    Delivery must verify the root-owned 0600 file, hash and both allowlists;
    config bytes are never embedded in cloud-init or public inventory.
    """

    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    role: Literal["api", "worker"]
    config_file: str = Field(pattern=r"^/", max_length=1024)
    config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    config_keys: tuple[str, ...] = Field(min_length=1)
    secret_env_names: tuple[str, ...] = ()
    secrets: dict[str, SecretRef] = Field(default_factory=dict)
    image: str = Field(pattern=r"^[^\s@]+@sha256:[0-9a-f]{64}$", max_length=255)
    plugin_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    schema_version: int = Field(ge=1, le=2147483647)
    protocol_version: Literal[1, 2]

    @model_validator(mode="after")
    def scoped_delivery(self) -> GuestProfile:
        if len(set(self.config_keys)) != len(self.config_keys):
            raise ValueError("guest config_keys must be unique")
        if len(set(self.secret_env_names)) != len(self.secret_env_names):
            raise ValueError("guest secret_env_names must be unique")
        if set(self.secrets) != set(self.secret_env_names):
            raise ValueError("guest secrets must exactly match secret_env_names")
        if not {"DATABASE_URL", "REDIS_URL", "LUMEN_ENCRYPTION_KEY"}.issubset(self.secrets):
            raise ValueError("trusted guest profile requires database, Redis and encryption secret references")
        forbidden = {"RUNTIME_CONFIG", "OS_APPLICATION_CREDENTIAL_ID",
                     "OS_APPLICATION_CREDENTIAL_SECRET", "OS_AUTH_URL"}
        for name in self.secret_env_names:
            if not name.isascii() or not name.replace("_", "").isalnum() or not name.isupper():
                raise ValueError("guest secret env names must be uppercase ASCII identifiers")
            if name[0].isdigit() or name in forbidden:
                raise ValueError("guest secret allowlist cannot expose controller credentials")
        if any(key.lower() == "runtime_config" for key in self.config_keys):
            raise ValueError("guest config allowlist cannot expose controller runtime_config")
        return self

    def digest(self) -> str:
        return hashlib.sha256(
            json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


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
    api_target_active_requests: int = Field(ge=1, le=100000)
    api_target_ttft_ms: int = Field(ge=1, le=3600000)
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
    min_replicas: int = Field(ge=0, le=10000)
    max_replicas: int = Field(ge=0, le=10000)
    max_surge: int = Field(default=1, ge=0, le=100)
    workload_class: WorkloadClass | None = None
    guest_profile_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    slots_per_worker: int = Field(default=4, ge=1, le=64)
    target_wait_seconds: int = Field(default=10, ge=1, le=86400)
    cold_service_time_ms: int | None = Field(default=None, ge=1, le=86400000)
    boot_timeout_seconds: int = Field(default=600, ge=30, le=3600)
    idle_seconds: int = Field(default=300, ge=1, le=86400)
    drain_seconds: int = Field(default=300, ge=1, le=86400)
    # Lifetime initiates replacement/drain; it must not terminate live work.
    max_lifetime_seconds: int = Field(default=86400, ge=60, le=2592000)
    db_connection_budget: int | None = Field(default=None, ge=1, le=1000000)
    pg_connection_budget: int | None = Field(default=None, ge=0, le=1000000)
    db_connections_per_process: int = Field(default=30, ge=1, le=10000)
    pg_connections_per_process: int = Field(default=0, ge=0, le=10000)
    profile: Annotated[NovaProfile | ZunProfile, Field(discriminator="backend")]
    sandbox: SandboxPolicy | None = None
    ingress: IngressConfig | None = None

    @model_validator(mode="before")
    @classmethod
    def worker_policy(cls, values: Any, info: ValidationInfo) -> Any:
        if not isinstance(values, dict):
            return values
        values = dict(values)
        # A before-validator returns Python containers, losing JSON's tuple allowance.
        # Normalize only the JSON array; strict validation still checks every ID.
        if info.mode == "json" and isinstance(values.get("security_group_ids"), list):
            values["security_group_ids"] = tuple(values["security_group_ids"])
        if values.get("role") == "sandbox":
            values.setdefault("max_lifetime_seconds", 1800)
        if values.get("role") == "worker":
            values.setdefault("workload_class", "online_text")
            values.setdefault("cold_service_time_ms", {
                "online_text": 30000, "online_media": 120000, "batch": 60000,
            }.get(values.get("workload_class"), 30000))
        elif any(values.get(key) is not None for key in (
            "workload_class", "cold_service_time_ms",
        )):
            raise ValueError("only worker pools accept worker workload policy")
        return values

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
        if self.role == "worker" and (self.workload_class is None or self.cold_service_time_ms is None):
            raise ValueError("worker pools require workload_class and cold_service_time_ms")
        if self.enabled:
            if self.max_replicas < 1 or self.max_replicas < self.min_replicas:
                raise ValueError("enabled pools require finite positive max_replicas >= min_replicas")
            if self.role in {"api", "worker"} and self.db_connection_budget is None:
                raise ValueError("enabled trusted pools require db_connection_budget")
            if self.role == "api" and self.ingress is None:
                raise ValueError("enabled api pools require operator ingress configuration")
            if self.role in {"api", "worker"} and self.guest_profile_id is None:
                raise ValueError("enabled trusted pools require guest_profile_id")
            if self.role == "api" and self.min_replicas == 0:
                raise ValueError("enabled api pools require min_replicas >= 1")
            if self.role == "worker" and self.workload_class != "batch" and self.min_replicas == 0:
                raise ValueError("online worker pools require min_replicas >= 1")
            if self.role in {"api", "worker"}:
                if self.pg_connection_budget is None:
                    raise ValueError("enabled trusted pools require pg_connection_budget")
                serving = self.min_replicas + self.max_surge
                if self.db_connection_budget < serving * self.db_connections_per_process:
                    raise ValueError("pool DB budget cannot cover minimum replicas and replacement surge")
                if self.pg_connection_budget < serving * self.pg_connections_per_process:
                    raise ValueError("pool PG budget cannot cover minimum replicas and replacement surge")
        if self.role == "sandbox" and self.guest_profile_id is not None:
            raise ValueError("sandbox pools cannot receive trusted guest profiles")
        if self.max_replicas < self.min_replicas:
            raise ValueError("max_replicas must be >= min_replicas")
        if self.role in {"api", "worker"} and self.max_lifetime_seconds <= (
            self.boot_timeout_seconds + self.drain_seconds
        ):
            raise ValueError("trusted lifetime must leave time for boot and replacement drain")
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
    reconcile_interval_seconds: int = Field(default=5, ge=1, le=300)
    pool_lease_seconds: int = Field(default=30, ge=5, le=300)
    dispatch_key: SecretRef | None = None
    # Operator-approved CIDRs the internal transport may reach; resource addresses outside
    # them are refused even when the controller observed them.
    managed_networks: tuple[str, ...] = ()
    max_parallel_cloud_operations: int = Field(default=4, ge=1, le=32)
    cloud_profiles: tuple[CloudProfile, ...] = ()
    pools: tuple[PoolConfig, ...] = ()
    project_quota_defaults: ProjectQuotaDefaults = Field(default_factory=ProjectQuotaDefaults)
    renewal: RenewalPolicy = Field(default_factory=RenewalPolicy)
    guest_profiles: tuple[GuestProfile, ...] = ()
    workload_pools: dict[WorkloadClass, str] = Field(default_factory=dict)
    db_connection_budget: int | None = Field(default=None, ge=1, le=1000000)
    pg_connection_budget: int | None = Field(default=None, ge=1, le=1000000)
    fixed_db_connection_reserve: int = Field(default=0, ge=0, le=1000000)
    controller_db_connection_reserve: int = Field(default=0, ge=0, le=1000000)
    fixed_pg_connection_reserve: int = Field(default=0, ge=0, le=1000000)
    controller_pg_connection_reserve: int = Field(default=0, ge=0, le=1000000)

    @model_validator(mode="after")
    def coherent(self) -> RuntimeConfig:
        profiles = {profile.id: profile for profile in self.cloud_profiles}
        if len(profiles) != len(self.cloud_profiles):
            raise ValueError("duplicate cloud profile id")
        names = [pool.name for pool in self.pools]
        if len(set(names)) != len(names):
            raise ValueError("duplicate pool name")
        guests = {profile.id: profile for profile in self.guest_profiles}
        if len(guests) != len(self.guest_profiles):
            raise ValueError("duplicate guest profile id")
        for pool in self.pools:
            if pool.guest_profile_id is not None:
                guest = guests.get(pool.guest_profile_id)
                if guest is None or guest.role != pool.role or guest.image != pool.image:
                    raise ValueError(f"pool {pool.name} requires a matching role/image guest profile")
        for workload, name in self.workload_pools.items():
            pool = next((pool for pool in self.pools if pool.name == name), None)
            if pool is None or pool.role != "worker" or pool.workload_class != workload:
                raise ValueError("workload_pools must map each class to its matching worker pool")
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
            if (any(pool.enabled for pool in self.pools)
                    and self.tls.operator_client_cert_file is None):
                raise ValueError("guest readiness/drain requires operator probe client certificate")
            for cidr in self.managed_networks:
                try:
                    ipaddress.ip_network(cidr, strict=True)
                except ValueError as exc:
                    raise ValueError(f"managed_networks entry {cidr!r} is not a valid CIDR") from exc
            if not self.controller_url.startswith("https://"):
                raise ValueError("enabled runtime requires an https controller_url")
            if not any(pool.enabled for pool in self.pools):
                raise ValueError("enabled runtime requires at least one enabled pool")
            workers = [pool for pool in self.pools if pool.enabled and pool.role == "worker"]
            classes = [pool.workload_class for pool in workers]
            if len(set(classes)) != len(classes):
                raise ValueError("each worker workload class must have exactly one enabled pool")
            for pool in workers:
                if self.workload_pools.get(pool.workload_class) != pool.name:
                    raise ValueError("enabled worker pools require explicit workload_pools mapping")
            if any(not self.pool(name).enabled for name in self.workload_pools.values()):
                raise ValueError("enabled runtime workload mappings must reference enabled pools")
            trusted_pools = [pool for pool in self.pools if pool.enabled and pool.role != "sandbox"]
            if trusted_pools:
                if not self.renewal.enabled:
                    raise ValueError("enabled trusted runtime requires identity renewal")
                if self.db_connection_budget is None or self.pg_connection_budget is None:
                    raise ValueError("enabled trusted runtime requires deployment DB and PG budgets")
                if self.controller_db_connection_reserve < 1:
                    raise ValueError("enabled trusted runtime requires controller DB reserve")
                db_reserved = sum(pool.db_connection_budget for pool in trusted_pools)
                pg_reserved = sum(pool.pg_connection_budget for pool in trusted_pools)
                if db_reserved + self.fixed_db_connection_reserve + self.controller_db_connection_reserve > self.db_connection_budget:
                    raise ValueError("pool and fixed/controller DB reservations exceed deployment budget")
                if pg_reserved + self.fixed_pg_connection_reserve + self.controller_pg_connection_reserve > self.pg_connection_budget:
                    raise ValueError("pool and fixed/controller PG reservations exceed deployment budget")
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
