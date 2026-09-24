"""MariaDB authority for infrastructure intent, lease fences and worker membership.

Cloud I/O must only follow a committed, exclusively claimed operation. An ambiguous
create is observed by identity; neither a lease takeover nor a timeout repeats it.
"""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen.db import get_session_factory
from lumen.models.chat_infrastructure import (
    OCCUPYING_STATES,
    ChatResourceOperation,
    ChatRuntimePool,
    ChatRuntimeResource,
    ChatWorkerRegistration,
)
from lumen.models.chat_runs import ChatRun
from lumen.services.durable_runs.budgets import LockOrder, retry_deadlocks
from lumen.services.infrastructure.config import RuntimeConfig
from lumen.services.infrastructure.providers import Observation

_UNCLAIMED = "__unclaimed__"
_HEARTBEAT_SECONDS = 20


class ResourceUnavailable(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _now() -> datetime:
    return datetime.now(UTC)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _factory():
    factory = get_session_factory()
    if factory is None:
        raise RuntimeError("database is not configured")
    return factory


async def sync_pools(config: RuntimeConfig) -> list[ChatRuntimePool]:
    """Sync operator config without resetting leases or demand history."""
    rows = []
    async with _factory()() as session, session.begin():
        existing = list((await session.execute(select(ChatRuntimePool).where(
            ChatRuntimePool.deployment_id == config.deployment_id).with_for_update()
        )).scalars())
        by_name = {row.name: row for row in existing}
        for definition in config.pools:
            profile = config.profile(definition.cloud_profile_id)
            row = by_name.get(definition.name)
            if row is None:
                row = ChatRuntimePool(id=str(uuid4()), deployment_id=config.deployment_id, name=definition.name)
                session.add(row)
            values = dict(role=definition.role, backend=definition.backend, enabled=definition.enabled,
                          cloud_profile_id=definition.cloud_profile_id, project_id=profile.project_id,
                          region_name=profile.region_name, image_ref=definition.image,
                          profile_digest=definition.digest(), min_replicas=definition.min_replicas,
                          max_replicas=definition.max_replicas, slots_per_worker=definition.slots_per_worker,
                          target_wait_seconds=definition.target_wait_seconds,
                          boot_timeout_seconds=definition.boot_timeout_seconds, idle_seconds=definition.idle_seconds,
                          drain_seconds=definition.drain_seconds, max_lifetime_seconds=definition.max_lifetime_seconds,
                          db_connection_budget=definition.db_connection_budget,
                          ingress_pool_id=definition.ingress.ingress_pool_id if definition.ingress else None,
                          ingress_vip=definition.ingress.ingress_vip if definition.ingress else None)
            if row in existing and any(getattr(row, key) != value for key, value in values.items()):
                row.desired_revision += 1
            for key, value in values.items():
                setattr(row, key, value)
            rows.append(row)
        for row in existing:
            if row.name not in {definition.name for definition in config.pools} and row.enabled:
                row.enabled = False
                row.desired_revision += 1
    return rows


async def claim_pool_lease(pool_id: str, owner: str, lease_seconds: int) -> tuple[ChatRuntimePool, int] | None:
    if not owner or owner == _UNCLAIMED:
        raise ValueError("invalid controller owner")
    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be positive")
    now = _now()
    async with _factory()() as session, session.begin():
        row = (await session.execute(select(ChatRuntimePool).where(
            ChatRuntimePool.id == pool_id).with_for_update())).scalar_one_or_none()
        if row is None or not row.enabled:
            return None
        if row.reconcile_lease_expires_at and _utc(row.reconcile_lease_expires_at) > now and row.reconcile_lease_owner != owner:
            return None
        row.reconcile_fence += 1
        row.reconcile_lease_owner = owner
        row.reconcile_lease_expires_at = now + timedelta(seconds=lease_seconds)
        return row, row.reconcile_fence


async def renew_pool_lease(pool_id: str, owner: str, fence: int, lease_seconds: int) -> bool:
    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be positive")
    async with _factory()() as session, session.begin():
        pool = (await session.execute(select(ChatRuntimePool).where(ChatRuntimePool.id == pool_id).with_for_update())).scalar_one_or_none()
        if not _owns(pool, owner, fence):
            return False
        pool.reconcile_lease_expires_at = _now() + timedelta(seconds=lease_seconds)
        return True


def _owns(pool: ChatRuntimePool | None, owner: str, fence: int) -> bool:
    return bool(pool and pool.reconcile_lease_owner == owner and pool.reconcile_fence == fence
                and pool.reconcile_lease_expires_at and _utc(pool.reconcile_lease_expires_at) > _now())


async def _locked_resource(session: AsyncSession, resource_id: str, generation: int | None = None):
    pool_id = (await session.execute(select(ChatRuntimeResource.pool_id).where(
        ChatRuntimeResource.id == resource_id))).scalar_one_or_none()
    if pool_id is None:
        return None, None
    pool = (await session.execute(select(ChatRuntimePool).where(ChatRuntimePool.id == pool_id)
                                  .with_for_update())).scalar_one()
    resource = (await session.execute(select(ChatRuntimeResource).where(ChatRuntimeResource.id == resource_id)
                                      .with_for_update())).scalar_one_or_none()
    if resource is None or resource.pool_id != pool_id or (generation is not None and resource.generation != generation):
        return None, None
    return resource, pool


async def _insert_resource(session: AsyncSession, pool: ChatRuntimePool, *, role: str, request_fingerprint: str,
                           image_ref: str, policy_digest: str, deadline: datetime | None,
                           run_id: str | None = None, logical_project_id: str | None = None,
                           logical_user_id: str | None = None) -> ChatRuntimeResource:
    resource = ChatRuntimeResource(id=str(uuid4()), pool_id=pool.id, generation=1, role=role, backend=pool.backend,
                                   logical_project_id=logical_project_id, logical_user_id=logical_user_id,
                                   run_id=run_id, desired_state="requested", observed_state="requested",
                                   request_fingerprint=request_fingerprint, cloud_profile_id=pool.cloud_profile_id,
                                   cloud_project_id=pool.project_id, image_ref=image_ref, policy_digest=policy_digest,
                                   deadline_at=deadline)
    session.add(resource)
    # No ORM relationship joins the intent to its operation. MariaDB enforces
    # their foreign key, so persist the parent before the child operation.
    await session.flush()
    session.add(ChatResourceOperation(id=str(uuid4()), resource_id=resource.id, generation=1, action="create",
                                      request_status="claimed", owner=_UNCLAIMED, fence=0, attempts=0))
    return resource


async def request_resource(pool: ChatRuntimePool, *, role: str, request_fingerprint: str,
                           image_ref: str, policy_digest: str, deadline: datetime | None,
                           run_id: str | None = None, logical_project_id: str | None = None,
                           logical_user_id: str | None = None, owner: str | None = None,
                           fence: int | None = None) -> ChatRuntimeResource:
    async with _factory()() as session, session.begin():
        locked = (await session.execute(select(ChatRuntimePool).where(ChatRuntimePool.id == pool.id)
                                        .with_for_update())).scalar_one()
        if not locked.enabled or locked.role != role:
            raise ResourceUnavailable("pool_unavailable")
        if owner is not None and (fence is None or not _owns(locked, owner, fence)):
            raise ResourceUnavailable("pool_lease_lost")
        if run_id:
            prior = (await session.execute(select(ChatRuntimeResource).where(ChatRuntimeResource.run_id == run_id)
                                           .order_by(ChatRuntimeResource.generation.desc()).with_for_update())).scalars().first()
            if prior is not None:
                return prior
        count = await occupancy(locked.id, session=session)
        if count >= locked.max_replicas:
            raise ResourceUnavailable("pool_capacity_exceeded")
        resource = await _insert_resource(session, locked, role=role, request_fingerprint=request_fingerprint,
                                          image_ref=image_ref, policy_digest=policy_digest, deadline=deadline,
                                          run_id=run_id, logical_project_id=logical_project_id,
                                          logical_user_id=logical_user_id)
        await session.flush()
        return resource


async def claim_operation(resource_id: str, generation: int, action: str, owner: str, fence: int) -> ChatResourceOperation | None:
    """One-shot claim: an expired lease never authorizes repeating external I/O."""
    if not owner or owner == _UNCLAIMED:
        raise ValueError("invalid operation owner")
    async def transaction() -> ChatResourceOperation | None:
        async with _factory()() as session, session.begin():
            resource, pool = await _locked_resource(session, resource_id, generation)
            if not resource or not _owns(pool, owner, fence) or resource.observed_state == "deleted":
                return None
            operation = (await session.execute(select(ChatResourceOperation).where(
                ChatResourceOperation.resource_id == resource_id, ChatResourceOperation.generation == generation,
                ChatResourceOperation.action == action).with_for_update())).scalar_one_or_none()
            if operation is None or operation.request_status != "claimed" or operation.owner != _UNCLAIMED:
                return None
            if action == "create" and (resource.observed_state != "requested" or resource.desired_state != "requested"):
                return None
            if action == "delete" and resource.desired_state != "deleting":
                return None
            operation.owner = owner
            operation.fence = fence
            operation.attempts = 1
            if action == "create":
                # A crash after committing this claim is ambiguous even before the
                # provider call starts. The next owner may discover but never resubmit.
                resource.observed_state = "unknown"
            return operation

    return await retry_deadlocks(transaction)

async def abort_unsubmitted_create(resource_id: str, generation: int, owner: str, fence: int,
                                   error_code: str) -> bool:
    """Release a claimed create only when the caller certifies no cloud call occurred."""
    async with _factory()() as session, session.begin():
        resource, pool = await _locked_resource(session, resource_id, generation)
        if not resource or not _owns(pool, owner, fence) or resource.provider_id is not None:
            return False
        operation = (await session.execute(select(ChatResourceOperation).where(
            ChatResourceOperation.resource_id == resource_id, ChatResourceOperation.generation == generation,
            ChatResourceOperation.action == "create").with_for_update())).scalar_one_or_none()
        if not operation or operation.owner != owner or operation.fence != fence or operation.request_status != "claimed":
            return False
        operation.request_status = "failed"
        operation.error_code = error_code
        resource.failure_code = error_code
        resource.desired_state = "deleting"
        resource.observed_state = "deleted"
        resource.deleted_at = _now()
        return True



async def record_observation(resource_id: str, generation: int, owner: str, fence: int, observation: Observation) -> bool:
    """Persist a provider observation under the current lease; cloud ACTIVE is booting, not ready."""
    async with _factory()() as session, session.begin():
        resource, pool = await _locked_resource(session, resource_id, generation)
        if not resource or not _owns(pool, owner, fence) or resource.observed_state == "deleted":
            return False
        if not isinstance(observation, Observation) or not observation.provider_id:
            return False
        identity = {"lumen_deployment": pool.deployment_id, "lumen_pool": pool.name,
                    "lumen_resource": resource.id, "lumen_generation": str(generation),
                    "lumen_fingerprint": resource.request_fingerprint, "lumen_policy": resource.policy_digest}
        if any(str(observation.metadata.get(key)) != value for key, value in identity.items()):
            return False
        if resource.provider_id and resource.provider_id != observation.provider_id:
            return False
        resource.provider_id = observation.provider_id
        if observation.address is not None:
            resource.address = observation.address
        if observation.port is not None:
            resource.port = observation.port
        state = observation.state.lower()
        if resource.desired_state == "deleting":
            resource.observed_state = "deleting"
        elif state in {"error", "failed"}:
            resource.observed_state = "unavailable"
        elif state in {"active", "running", "ready", "booting"} and resource.observed_state not in {"ready", "draining"}:
            resource.observed_state = "booting"
        elif state in {"build", "creating", "created", "stopped"} and resource.observed_state not in {"ready", "draining"}:
            resource.observed_state = "creating"
        operation = (await session.execute(select(ChatResourceOperation).where(
            ChatResourceOperation.resource_id == resource_id, ChatResourceOperation.generation == generation,
            ChatResourceOperation.action == "create").with_for_update())).scalar_one_or_none()
        if operation and operation.request_status in {"claimed", "unknown", "submitted"}:
            operation.request_status = "succeeded"
            operation.provider_request_id = observation.provider_id
        return True


async def mark_unknown(resource_id: str, generation: int, owner: str, fence: int, action: str = "create") -> bool:
    async with _factory()() as session, session.begin():
        resource, pool = await _locked_resource(session, resource_id, generation)
        if not resource or not _owns(pool, owner, fence):
            return False
        op = (await session.execute(select(ChatResourceOperation).where(
            ChatResourceOperation.resource_id == resource_id, ChatResourceOperation.generation == generation,
            ChatResourceOperation.action == action).with_for_update())).scalar_one_or_none()
        if not op or op.owner != owner or op.fence != fence or op.request_status not in {"claimed", "submitted"}:
            return False
        op.request_status = "unknown"
        if action == "create":
            resource.observed_state = "unknown"
        return True


async def mark_failed_with_cleanup(resource_id: str, generation: int, owner: str, fence: int, error_code: str) -> bool:
    async with _factory()() as session, session.begin():
        resource, pool = await _locked_resource(session, resource_id, generation)
        if not resource or not _owns(pool, owner, fence) or resource.observed_state == "deleted":
            return False
        resource.failure_code = error_code
        resource.observed_state = "failed"
        resource.desired_state = "deleting"
        await _request_delete(session, resource)
        return True


async def _request_delete(session: AsyncSession, resource: ChatRuntimeResource) -> ChatResourceOperation:
    resource.desired_state = "deleting"
    op = (await session.execute(select(ChatResourceOperation).where(
        ChatResourceOperation.resource_id == resource.id, ChatResourceOperation.generation == resource.generation,
        ChatResourceOperation.action == "delete").with_for_update())).scalar_one_or_none()
    if op is None:
        op = ChatResourceOperation(id=str(uuid4()), resource_id=resource.id, generation=resource.generation,
                                   action="delete", request_status="claimed", owner=_UNCLAIMED, fence=0, attempts=0)
        session.add(op)
    return op


async def request_delete(resource_id: str, owner: str, fence: int) -> ChatResourceOperation | None:
    async with _factory()() as session, session.begin():
        resource, pool = await _locked_resource(session, resource_id)
        if not resource or not _owns(pool, owner, fence) or resource.observed_state == "deleted":
            return None
        return await _request_delete(session, resource)


async def prove_absent(resource_id: str, generation: int, owner: str, fence: int) -> bool:
    """Only caller-supplied positive provider absence is sufficient; never infer from a listing."""
    async with _factory()() as session, session.begin():
        resource, pool = await _locked_resource(session, resource_id, generation)
        if not resource or not _owns(pool, owner, fence) or resource.desired_state != "deleting":
            return False
        create = (await session.execute(select(ChatResourceOperation).where(
            ChatResourceOperation.resource_id == resource_id, ChatResourceOperation.generation == generation,
            ChatResourceOperation.action == "create").with_for_update())).scalar_one_or_none()
        if resource.provider_id is None and create and (create.owner != _UNCLAIMED or create.request_status != "claimed"):
            return False
        delete = await _request_delete(session, resource)
        if resource.provider_id is not None and delete.owner == _UNCLAIMED:
            return False
        resource.observed_state = "deleted"
        resource.deleted_at = _now()
        delete.request_status = "succeeded"
        return True


async def occupancy(pool_id: str, *, session: AsyncSession | None = None) -> int:
    if session is None:
        async with _factory()() as own:
            return await occupancy(pool_id, session=own)
    return int((await session.scalar(select(func.count()).select_from(ChatRuntimeResource).where(
        ChatRuntimeResource.pool_id == pool_id, ChatRuntimeResource.observed_state.in_(OCCUPYING_STATES)
    ))) or 0)


async def assign_sandbox(session: AsyncSession, *, run: ChatRun, config: RuntimeConfig,
                         deadline_at: datetime, order: LockOrder) -> ChatRuntimeResource:
    """Caller-owned transaction: run and create intent commit or roll back together."""
    order.take("resource")
    if run.assigned_resource_id:
        existing = (await session.execute(select(ChatRuntimeResource).where(
            ChatRuntimeResource.id == run.assigned_resource_id).with_for_update())).scalar_one_or_none()
        if existing:
            return existing
        raise ResourceUnavailable("assigned_resource_missing")
    pools = [p for p in config.pools if p.enabled and p.role == "sandbox"]
    if not pools:
        raise ResourceUnavailable("sandbox_pool_unavailable")
    for definition in pools:
        pool = (await session.execute(select(ChatRuntimePool).where(
            ChatRuntimePool.deployment_id == config.deployment_id,
            ChatRuntimePool.name == definition.name).with_for_update())).scalar_one_or_none()
        if not pool or not pool.enabled or pool.role != "sandbox":
            continue
        if await occupancy(pool.id, session=session) >= pool.max_replicas:
            continue
        fingerprint = hashlib.sha256(f"{run.id}:{pool.id}:1".encode()).hexdigest()
        resource = await _insert_resource(session, pool, role="sandbox", request_fingerprint=fingerprint,
                                          image_ref=pool.image_ref, policy_digest=pool.profile_digest,
                                          deadline=deadline_at, run_id=run.id,
                                          logical_project_id=run.project_id, logical_user_id=run.user_id)
        run.assigned_resource_id = resource.id
        run.runtime_pool_id = pool.id
        run.deadline_at = deadline_at
        await session.flush()
        return resource
    raise ResourceUnavailable("sandbox_pool_full")


async def release_sandbox_intent(session: AsyncSession, *, run_id: str, order: LockOrder) -> None:
    """Irreversible deletion intent, while preserving occupancy until absence proof."""
    order.take("resource")
    resource = (await session.execute(select(ChatRuntimeResource).where(ChatRuntimeResource.run_id == run_id)
                                      .order_by(ChatRuntimeResource.generation.desc()).with_for_update())).scalars().first()
    if resource and resource.observed_state != "deleted":
        await _request_delete(session, resource)


async def register_worker(*, worker_identity: str, boot_id: str, capacity: int,
                          protocol_versions: list[int] | tuple[int, ...], plugin_digest: str,
                          schema_version: int, resource_id: str | None = None,
                          resource_generation: int | None = None,
                          certificate_fingerprint: str | None = None) -> str:
    if capacity <= 0 or not protocol_versions:
        raise ValueError("invalid worker capacity or protocols")
    if resource_id is None and (resource_generation is not None or certificate_fingerprint is not None):
        raise ResourceUnavailable("worker_identity_mismatch")
    async with _factory()() as session, session.begin():
        previous = list((await session.execute(select(ChatWorkerRegistration).where(
            ChatWorkerRegistration.worker_identity == worker_identity).with_for_update())).scalars())
        resource = (await session.execute(select(ChatRuntimeResource).where(
            ChatRuntimeResource.id == resource_id).with_for_update())).scalar_one_or_none() if resource_id else None
        if resource_id and (
            resource is None or resource.role != "worker" or resource.desired_state == "deleting"
            or resource.observed_state in {"deleted", "failed"} or resource.bootstrap_token_hash is not None
            or resource_generation is None or resource_generation != resource.generation
            or certificate_fingerprint is None or certificate_fingerprint != resource.certificate_fingerprint
        ):
            raise ResourceUnavailable("worker_resource_unavailable")
        row = next((item for item in previous if item.boot_id == boot_id), None)
        if row is None:
            row = ChatWorkerRegistration(id=str(uuid4()), worker_identity=worker_identity, boot_id=boot_id,
                                         resource_id=resource_id, pool_id=resource.pool_id if resource else None,
                                         resource_generation=resource_generation,
                                         certificate_fingerprint=certificate_fingerprint,
                                         protocol_versions=list(protocol_versions), plugin_digest=plugin_digest,
                                         schema_version=schema_version, capacity=capacity)
            session.add(row)
        elif (row.resource_id != resource_id or row.resource_generation != resource_generation
              or row.certificate_fingerprint != certificate_fingerprint or row.plugin_digest != plugin_digest
              or row.protocol_versions != list(protocol_versions) or row.schema_version != schema_version):
            raise ResourceUnavailable("worker_identity_mismatch")
        for old in previous:
            if old.boot_id != boot_id:
                old.accepting = False
                old.draining = True
                old.drain_started_at = old.drain_started_at or _now()
        if resource is not None:
            resource.heartbeat_at = _now()
        return row.id


async def heartbeat_worker(registration_id: str, *, active_count: int, accepting: bool) -> bool:
    async with _factory()() as session, session.begin():
        row = (await session.execute(select(ChatWorkerRegistration).where(
            ChatWorkerRegistration.id == registration_id).with_for_update())).scalar_one_or_none()
        if row is None or active_count < 0 or active_count > row.capacity:
            return False
        if row.resource_id is None and (row.resource_generation is not None or row.certificate_fingerprint is not None):
            return False
        now = _now()
        if row.resource_id:
            resource = (await session.execute(select(ChatRuntimeResource).where(
                ChatRuntimeResource.id == row.resource_id).with_for_update())).scalar_one_or_none()
            if (not resource or resource.role != "worker" or resource.desired_state == "deleting"
                    or resource.observed_state in {"deleted", "failed"}
                    or resource.bootstrap_token_hash is not None
                    or resource.generation != row.resource_generation
                    or resource.certificate_fingerprint != row.certificate_fingerprint):
                return False
            resource.heartbeat_at = now
            resource.active_slots = active_count
        row.heartbeat_at = now
        row.active_count = active_count
        row.accepting = accepting and not row.draining
        return True


async def start_drain(registration_id: str) -> bool:
    async with _factory()() as session, session.begin():
        row = (await session.execute(select(ChatWorkerRegistration).where(
            ChatWorkerRegistration.id == registration_id).with_for_update())).scalar_one_or_none()
        if row is None:
            return False
        row.draining = True
        row.accepting = False
        row.drain_started_at = row.drain_started_at or _now()
        return True


async def list_stale_workers(*, stale_seconds: int = _HEARTBEAT_SECONDS) -> list[str]:
    cutoff = _now() - timedelta(seconds=stale_seconds)
    async with _factory()() as session, session.begin():
        rows = list((await session.execute(select(ChatWorkerRegistration).where(
            ChatWorkerRegistration.heartbeat_at < cutoff, ChatWorkerRegistration.accepting.is_(True)
        ).with_for_update())).scalars())
        for row in rows:
            row.accepting = False
        return [row.id for row in rows]


async def eligible_worker_demand(pool_id: str) -> tuple[int, int, datetime | None]:
    """Count queued work and only fresh, accepting, compatible worker activity."""
    now = _now()
    async with _factory()() as session:
        registrations = list((await session.execute(select(ChatWorkerRegistration).where(
            ChatWorkerRegistration.pool_id == pool_id, ChatWorkerRegistration.accepting.is_(True),
            ChatWorkerRegistration.draining.is_(False), ChatWorkerRegistration.heartbeat_at >= now - timedelta(seconds=_HEARTBEAT_SECONDS)
        ))).scalars())
        # runtime_pool_id identifies the per-run sandbox after assignment; it
        # does not constrain which compatible shared trusted worker may execute.
        queued = list((await session.execute(select(ChatRun).where(
            ChatRun.status == "queued"
        ))).scalars())
        # Demand must remain visible when zero compatible workers exist; otherwise a
        # cold pool can never scale out. Incompatible live registrations do not erase it.
        eligible = [run for run in queued if not registrations or any(
            run.execution_protocol_version in registration.protocol_versions
            and (not run.required_plugin_digest or run.required_plugin_digest == registration.plugin_digest)
            for registration in registrations)]
        active = sum(registration.active_count for registration in registrations)
        return active, len(eligible), min((run.created_at for run in eligible), default=None)


async def mark_ready(resource_id: str, generation: int, owner: str, fence: int, *, policy_verified: bool = False) -> bool:
    """Bootstrap identity and explicit health verification gate application readiness."""
    async with _factory()() as session, session.begin():
        resource, pool = await _locked_resource(session, resource_id, generation)
        if (not resource or not _owns(pool, owner, fence)
                or (resource.observed_state not in {"booting", "ready"}
                    and not (resource.role == "api" and policy_verified and resource.observed_state == "unavailable"))):
            return False
        # The successful authenticated sandbox readyz (or API health/ingress
        # probe) is a heartbeat; sandbox guests cannot call back after bootstrap.
        if policy_verified and resource.role in {"sandbox", "api"}:
            resource.heartbeat_at = _now()
        if (resource.desired_state == "deleting" or resource.bootstrap_token_hash is not None
            or not resource.certificate_fingerprint or not resource.heartbeat_at
            or _utc(resource.heartbeat_at) < _now() - timedelta(seconds=_HEARTBEAT_SECONDS)):
            return False
        if resource.role in {"sandbox", "api"} and not policy_verified:
            return False
        if resource.role == "worker":
            registered = (await session.execute(select(ChatWorkerRegistration).where(
                ChatWorkerRegistration.resource_id == resource_id,
                ChatWorkerRegistration.resource_generation == resource.generation,
                ChatWorkerRegistration.certificate_fingerprint == resource.certificate_fingerprint,
                ChatWorkerRegistration.pool_id == resource.pool_id,
                ChatWorkerRegistration.accepting.is_(True), ChatWorkerRegistration.draining.is_(False),
                ChatWorkerRegistration.heartbeat_at >= _now() - timedelta(seconds=_HEARTBEAT_SECONDS)
            ))).scalars().first()
            if registered is None or not registered.protocol_versions or not registered.plugin_digest:
                return False
        resource.observed_state = "ready"
        resource.ready_at = resource.ready_at or _now()
    await wake_ready_runs(resource_id)
    return True


async def record_api_load(resource_id: str, generation: int, owner: str, fence: int, *, active_count: int) -> bool:
    if type(active_count) is not int or active_count < 0:
        return False
    async with _factory()() as session, session.begin():
        resource, pool = await _locked_resource(session, resource_id, generation)
        if (not resource or not _owns(pool, owner, fence) or resource.role != "api"
                or resource.observed_state != "ready" or resource.desired_state == "deleting"):
            return False
        resource.active_slots = active_count
        resource.heartbeat_at = _now()
        return True


async def mark_api_unavailable(resource_id: str, generation: int, owner: str, fence: int) -> bool:
    async with _factory()() as session, session.begin():
        resource, pool = await _locked_resource(session, resource_id, generation)
        if (not resource or not _owns(pool, owner, fence) or resource.role != "api"
                or resource.observed_state != "ready" or resource.desired_state == "deleting"):
            return False
        resource.observed_state = "unavailable"
        return True


async def wake_ready_runs(resource_id: str) -> list[str]:
    """Append run stage journal in the same transaction, publish hints after commit."""
    from lumen.services import run_store
    from lumen.services.durable_runs.common import _event, wake_run
    async with _factory()() as session, session.begin():
        runs = list((await session.execute(select(ChatRun).where(
            ChatRun.assigned_resource_id == resource_id, ChatRun.status == "waiting_resource"
        ).with_for_update())).scalars())
        if not runs:
            return []
        resource = (await session.execute(select(ChatRuntimeResource).where(
            ChatRuntimeResource.id == resource_id,
            ChatRuntimeResource.observed_state == "ready", ChatRuntimeResource.desired_state != "deleting"
        ).with_for_update())).scalar_one_or_none()
        if resource is None:
            return []
        for run in runs:
            run.status = "queued"
            await run_store.append_event(session, run, _event(run, "run.stage.changed", {"stage": "queued"}))
        ids = [run.id for run in runs]
    for run_id in ids:
        await wake_run(run_id)
    return ids
