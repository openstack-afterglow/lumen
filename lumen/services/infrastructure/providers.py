"""Common cloud resource contract; immutable identity survives controller restarts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from lumen.services.infrastructure.config import PoolConfig, Role

# Identical keys in Nova server metadata and Zun container labels. Every key is
# required before a resource can be considered owned or safely deleted.
OWNER_DEPLOYMENT = "lumen_deployment"
OWNER_POOL = "lumen_pool"
OWNER_RESOURCE = "lumen_resource"
OWNER_GENERATION = "lumen_generation"
OWNER_FINGERPRINT = "lumen_fingerprint"
OWNER_POLICY = "lumen_policy"
OWNERSHIP_KEYS = (
    OWNER_DEPLOYMENT, OWNER_POOL, OWNER_RESOURCE, OWNER_GENERATION,
    OWNER_FINGERPRINT, OWNER_POLICY,
)


@dataclass(frozen=True)
class ResourceIntent:
    resource_id: str
    generation: int
    pool_id: str
    role: Role
    image_ref: str
    policy_digest: str
    request_fingerprint: str
    deadline: datetime | None = None
    run_id: str | None = None
    logical_project_id: str | None = None
    logical_user_id: str | None = None
    bootstrap_token: str | None = None


@dataclass(frozen=True)
class ResourceRef:
    resource_id: str
    generation: int
    provider_id: str
    pool_id: str


@dataclass(frozen=True)
class Observation:
    provider_id: str
    state: str
    address: str | None
    port: int | None
    metadata: Mapping[str, str]


@dataclass(frozen=True)
class DeleteResult:
    absent: bool


class ComputeProvider(Protocol):
    requires_delivery: bool
    def preflight(self, pool: PoolConfig | None = None) -> None: ...
    def create(self, intent: ResourceIntent) -> Observation: ...
    def observe(self, ref: ResourceRef) -> Observation: ...
    def list_owned(self, pool_id: str) -> list[Observation]: ...
    def delete(self, ref: ResourceRef) -> DeleteResult: ...
    def deliver_bootstrap(self, ref: ResourceRef, token: str) -> bool: ...


def ownership(deployment_id: str, intent: ResourceIntent) -> dict[str, str]:
    return {
        OWNER_DEPLOYMENT: deployment_id,
        OWNER_POOL: intent.pool_id,
        OWNER_RESOURCE: intent.resource_id,
        OWNER_GENERATION: str(intent.generation),
        OWNER_FINGERPRINT: intent.request_fingerprint,
        OWNER_POLICY: intent.policy_digest,
    }


def owned(labels: Mapping[str, str], deployment_id: str, pool_id: str) -> bool:
    return (
        labels.get(OWNER_DEPLOYMENT) == deployment_id
        and labels.get(OWNER_POOL) == pool_id
        and all(labels.get(key) for key in OWNERSHIP_KEYS)
        and str(labels[OWNER_GENERATION]).isascii()
        and str(labels[OWNER_GENERATION]).isdigit()
        and int(labels[OWNER_GENERATION]) > 0
    )


def checked_ref(labels: Mapping[str, str], deployment_id: str, ref: ResourceRef) -> None:
    if not owned(labels, deployment_id, ref.pool_id) or (
        labels[OWNER_RESOURCE] != ref.resource_id
        or labels[OWNER_GENERATION] != str(ref.generation)
    ):
        raise RuntimeError("cloud resource ownership mismatch; refusing operation")


def addresses(value: object) -> tuple[str, ...]:
    """Extract addresses from Nova/Zun network-name to address-list responses."""
    if not isinstance(value, dict):
        return ()
    return tuple(
        entry["addr"]
        for network in value.values() if isinstance(network, list)
        for entry in network if isinstance(entry, dict) and isinstance(entry.get("addr"), str)
    )
