"""MariaDB authority for infrastructure intent, lease fences and worker membership.

Cloud I/O must only follow a committed, exclusively claimed operation. An ambiguous
create is observed by identity; neither a lease takeover nor a timeout repeats it.
"""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import uuid4

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen.config import get_settings
from lumen.db import get_session_factory
from lumen.models.chat_infrastructure import (
    OCCUPYING_STATES,
    ChatResourceOperation,
    ChatRuntimePool,
    ChatRuntimeResource,
    ChatWorkerRegistration,
)
from lumen.models.chat_runs import ChatRun, ChatRunEventRow
from lumen.services import worker_routing
from lumen.services.durable_runs.budgets import LockOrder, retry_deadlocks
from lumen.services.execution_protocol import SUPPORTED_EXECUTION_PROTOCOL_VERSIONS
from lumen.services.infrastructure.config import RuntimeConfig
from lumen.services.infrastructure.identity import accepts_fingerprint
from lumen.services.infrastructure.providers import Observation

_UNCLAIMED = "__unclaimed__"
_HEARTBEAT_SECONDS = worker_routing.REGISTRATION_FRESH_SECONDS


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
            guest = next((item for item in config.guest_profiles if item.id == definition.guest_profile_id), None)
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
                          workload_class=definition.workload_class, max_surge=definition.max_surge,
                          guest_profile_id=guest.id if guest else None,
                          guest_profile_digest=guest.digest() if guest else None,
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


async def pool_lease_owned(pool_id: str, owner: str, fence: int) -> bool:
    async with _factory()() as session:
        pool = await session.get(ChatRuntimePool, pool_id)
        return _owns(pool, owner, fence)


def _owns(pool: ChatRuntimePool | None, owner: str, fence: int) -> bool:
    return bool(pool and pool.reconcile_lease_owner == owner and pool.reconcile_fence == fence
                and pool.reconcile_lease_expires_at and _utc(pool.reconcile_lease_expires_at) > _now())


async def _locked_resource(session: AsyncSession, resource_id: str, generation: int | None = None):
    pool_id = (await session.execute(select(ChatRuntimeResource.pool_id).where(
        ChatRuntimeResource.id == resource_id))).scalar_one_or_none()
    if pool_id is None:
        return None, None
    pool = (await session.execute(select(ChatRuntimePool).where(ChatRuntimePool.id == pool_id)
                                  .with_for_update().execution_options(populate_existing=True))).scalar_one()
    resource = (await session.execute(select(ChatRuntimeResource).where(ChatRuntimeResource.id == resource_id)
                                      .with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
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
                                   guest_profile_id=pool.guest_profile_id if role in {"api", "worker"} else None,
                                   guest_profile_digest=pool.guest_profile_digest if role in {"api", "worker"} else None,
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
                           fence: int | None = None, replacement_for: str | None = None,
                           physical_limit: int | None = None) -> ChatRuntimeResource:
    async with _factory()() as session, session.begin():
        await worker_routing.use_read_committed(session)
        locked = (await session.execute(select(ChatRuntimePool).where(ChatRuntimePool.id == pool.id)
                                        .with_for_update().execution_options(populate_existing=True))).scalar_one()
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
        limit = locked.max_replicas
        if replacement_for is not None:
            if role not in {"api", "worker"} or owner is None or locked.max_surge == 0:
                raise ResourceUnavailable("pool_capacity_exceeded")
            old = (await session.execute(select(ChatRuntimeResource).where(
                ChatRuntimeResource.id == replacement_for, ChatRuntimeResource.pool_id == locked.id,
            ).with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
            if (old is None or old.observed_state != "ready" or old.drain_requested_at is not None
                    or old.desired_state == "deleting" or (_now() - _utc(old.created_at)).total_seconds()
                    < locked.max_lifetime_seconds - locked.boot_timeout_seconds - locked.drain_seconds):
                raise ResourceUnavailable("replacement_not_due")
            prior = await session.scalar(select(ChatRuntimeResource.id).where(
                ChatRuntimeResource.pool_id == locked.id,
                ChatRuntimeResource.drain_reason == "replacement:" + replacement_for,
                ChatRuntimeResource.observed_state != "deleted",
            ))
            if prior is not None:
                raise ResourceUnavailable("replacement_pending")
            limit += locked.max_surge
        if physical_limit is not None:
            limit = min(limit, physical_limit)
        if count >= limit:
            raise ResourceUnavailable("pool_capacity_exceeded")
        resource = await _insert_resource(session, locked, role=role, request_fingerprint=request_fingerprint,
                                          image_ref=image_ref, policy_digest=policy_digest, deadline=deadline,
                                          run_id=run_id, logical_project_id=logical_project_id,
                                          logical_user_id=logical_user_id)
        if replacement_for is not None:
            resource.drain_reason = "replacement:" + replacement_for
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
    async def transaction():
        async with _factory()() as session, session.begin():
            await worker_routing.use_read_committed(session)
            resource, pool = await _locked_resource(session, resource_id)
            if not resource or not _owns(pool, owner, fence) or resource.observed_state == "deleted":
                return None
            if resource.role in {"api", "worker"} and (
                resource.ready_at is not None or resource.drain_requested_at is not None
                or resource.observed_state in {"ready", "draining"}
            ):
                if resource.drain_requested_at is None or resource.drain_ack_at is None:
                    return None
                if resource.role == "api":
                    import json

                    from lumen.services.infrastructure.guest_api import verified_load
                    load = verified_load(json.dumps(resource.api_counter_snapshot).encode())
                    if (load is None or any(load[key] for key in ("active_requests", "active_sse", "active_ws"))):
                        return None
            return await _request_delete(session, resource)
    return await retry_deadlocks(transaction)


async def begin_drain(resource_id: str, generation: int, owner: str, fence: int, *,
                      reason: str, idle_seconds: int | None = None) -> ChatRuntimeResource | None:
    """Resource lock orders claim-vs-drain; idle is rechecked under that same lock."""
    async def transaction():
        async with _factory()() as session, session.begin():
            await worker_routing.use_read_committed(session)
            resource, pool = await _locked_resource(session, resource_id, generation)
            exposed_api = (resource is not None and resource.role == "api"
                           and resource.observed_state == "unavailable" and resource.ingress_member_id is not None)
            if (resource is None or not _owns(pool, owner, fence) or resource.desired_state == "deleting"
                    or (resource.observed_state not in {"ready", "draining"} and not exposed_api)):
                return None
            if (resource.drain_requested_at is None and idle_seconds is not None
                    and (resource.idle_since is None or (_now() - _utc(resource.idle_since)).total_seconds() < idle_seconds)):
                return None
            if resource.role == "worker":
                from lumen.services import auxiliary
                registrations = list((await session.execute(select(ChatWorkerRegistration).where(
                    ChatWorkerRegistration.resource_id == resource.id,
                ).order_by(ChatWorkerRegistration.id).with_for_update()
                  .execution_options(populate_existing=True))).scalars())
                if idle_seconds is not None and any([
                    (await auxiliary.lease_counts(session, row.id)).total or row.auxiliary_active
                    for row in registrations
                ]):
                    resource.idle_since = None
                    return None
                for row in registrations:
                    row.draining = True
                    row.accepting = False
                    row.drain_started_at = row.drain_started_at or _now()
            resource.accepting = False
            resource.observed_state = "draining"
            resource.drain_requested_at = resource.drain_requested_at or _now()
            resource.drain_reason = reason
            return resource
    return await retry_deadlocks(transaction)


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
                          schema_version: int, workload_classes: list[str] | tuple[str, ...],
                          resource_id: str | None = None,
                          resource_generation: int | None = None,
                          certificate_fingerprint: str | None = None) -> str:
    """Register one worker boot; managed workers serve exactly their pool's workload class."""
    classes = list(workload_classes)
    if capacity <= 0 or not protocol_versions:
        raise ValueError("invalid worker capacity or protocols")
    if (not classes or len(set(classes)) != len(classes)
            or any(value not in worker_routing.WORKER_WORKLOAD_CLASSES for value in classes)
            or ("batch" in classes and len(classes) != 1)):
        raise ValueError("invalid worker workload classes")
    if resource_id is None and (resource_generation is not None or certificate_fingerprint is not None):
        raise ResourceUnavailable("worker_identity_mismatch")
    async with _factory()() as session, session.begin():
        await worker_routing.use_read_committed(session)
        # resource -> registration lock order, matching claim/heartbeat/drain.
        resource = (await session.execute(select(ChatRuntimeResource).where(
            ChatRuntimeResource.id == resource_id).with_for_update()
            .execution_options(populate_existing=True))).scalar_one_or_none() if resource_id else None
        previous = list((await session.execute(select(ChatWorkerRegistration).where(
            ChatWorkerRegistration.worker_identity == worker_identity).order_by(ChatWorkerRegistration.id)
            .with_for_update().execution_options(populate_existing=True))).scalars())
        if resource_id and (
            resource is None or resource.role != "worker" or resource.desired_state == "deleting"
            or resource.observed_state in {"deleted", "failed"} or resource.bootstrap_token_hash is not None
            or resource_generation is None or resource_generation != resource.generation
            or not accepts_fingerprint(resource, certificate_fingerprint)
            or (resource.drain_requested_at is not None and resource.drain_ack_at is not None)
        ):
            raise ResourceUnavailable("worker_resource_unavailable")
        if resource is not None:
            pool = await session.get(ChatRuntimePool, resource.pool_id)
            if pool is None or pool.role != "worker" or classes != [pool.workload_class]:
                raise ResourceUnavailable("worker_workload_mismatch")
        row = next((item for item in previous if item.boot_id == boot_id), None)
        if row is None:
            row = ChatWorkerRegistration(id=str(uuid4()), worker_identity=worker_identity, boot_id=boot_id,
                                         resource_id=resource_id, pool_id=resource.pool_id if resource else None,
                                         resource_generation=resource_generation,
                                         certificate_fingerprint=resource.certificate_fingerprint if resource else None,
                                         protocol_versions=list(protocol_versions), plugin_digest=plugin_digest,
                                         schema_version=schema_version, capacity=capacity,
                                         workload_classes=classes)
            session.add(row)
        elif (row.resource_id != resource_id or row.resource_generation != resource_generation
              or (resource is None and row.certificate_fingerprint != certificate_fingerprint)
              or row.plugin_digest != plugin_digest
              or row.protocol_versions != list(protocol_versions) or row.schema_version != schema_version
              or row.workload_classes != classes or row.capacity != capacity):
            raise ResourceUnavailable("worker_identity_mismatch")
        if resource is not None and resource.drain_requested_at is not None:
            row.draining = True
            row.accepting = False
            row.drain_started_at = row.drain_started_at or resource.drain_requested_at
        for old in previous:
            if old.boot_id != boot_id:
                old.accepting = False
                old.draining = True
                old.drain_started_at = old.drain_started_at or _now()
        if resource is not None:
            resource.heartbeat_at = _now()
        return row.id


async def registration_pool_id(registration_id: str) -> str | None:
    """The worker pool a registration serves (None for fixed workers); selects its hint queues."""
    async with _factory()() as session:
        return (await session.execute(select(ChatWorkerRegistration.pool_id).where(
            ChatWorkerRegistration.id == registration_id
        ))).scalar_one_or_none()


async def heartbeat_worker(registration_id: str, *, accepting: bool,
                           certificate_fingerprint: str | None = None,
                           resource_generation: int | None = None) -> Literal["accepting", "draining"] | None:
    """Refresh identity and return the persisted admission state to the worker.

    ``active_count``/``active_slots`` are projections of DB live run leases, never the
    worker's self-report; ``auxiliary_active`` belongs to the auxiliary step accounting.
    """
    async def transaction() -> Literal["accepting", "draining"] | None:
        async with _factory()() as session, session.begin():
            await worker_routing.use_read_committed(session)
            locked = await worker_routing.lock_registration(session, registration_id, require_accepting=False)
            if locked is None:
                return None
            row, resource = locked
            if (certificate_fingerprint is not None or resource_generation is not None) and (
                resource is None or not accepts_fingerprint(resource, certificate_fingerprint)
                or (resource_generation is not None and resource.generation != resource_generation)
            ):
                return None
            now = _now()
            live = await worker_routing.live_run_leases(session, row.id, now=now)
            if resource is not None:
                resource.heartbeat_at = now
                resource.active_slots = live
                # Include maintenance in per-resource idleness; heartbeats are not delete proof.
                from lumen.services import auxiliary
                busy = (await auxiliary.lease_counts(session, row.id)).total or row.auxiliary_active
                resource.idle_since = (resource.idle_since or now) if not busy else None
            row.heartbeat_at = now
            row.active_count = live
            row.accepting = accepting and not row.draining
            return "draining" if row.draining else "accepting"
    return await retry_deadlocks(transaction)


async def start_drain(registration_id: str) -> bool:
    """Durable drain fence: no new run or auxiliary claim passes ``lock_registration`` after commit."""
    async def transaction() -> bool:
        async with _factory()() as session, session.begin():
            await worker_routing.use_read_committed(session)
            row, _resource = await worker_routing.lock_registration_rows(session, registration_id)
            if row is None:
                return False
            row.draining = True
            row.accepting = False
            row.drain_started_at = row.drain_started_at or _now()
            if _resource is not None:
                _resource.accepting = False
                _resource.observed_state = "draining"
                _resource.drain_requested_at = _resource.drain_requested_at or _now()
            return True
    return await retry_deadlocks(transaction)


async def acknowledge_drain(registration_id: str) -> bool:
    """Record drain completion only when DB proves no live run/auxiliary lease or step remains.

    The resource is acknowledged only when every other registration it hosted (earlier
    boots, already fenced by re-registration) is draining and holds no live lease.
    """
    from lumen.services import auxiliary

    async def transaction() -> bool:
        async with _factory()() as session, session.begin():
            await worker_routing.use_read_committed(session)
            row, resource = await worker_routing.lock_registration_rows(session, registration_id)
            if row is None or not row.draining:
                return False
            counts = await auxiliary.lease_counts(session, row.id)
            if counts.total or row.auxiliary_active:
                return False
            now = _now()
            row.accepting = False
            row.active_count = 0
            row.drain_ack_at = row.drain_ack_at or now
            if resource is not None:
                siblings = list((await session.execute(select(ChatWorkerRegistration).where(
                    ChatWorkerRegistration.resource_id == resource.id, ChatWorkerRegistration.id != row.id,
                ).with_for_update().execution_options(populate_existing=True))).scalars())
                for sibling in siblings:
                    if (not sibling.draining or sibling.auxiliary_active
                            or (await auxiliary.lease_counts(session, sibling.id)).total):
                        return True
                resource.active_slots = 0
                resource.accepting = False
                resource.drain_requested_at = resource.drain_requested_at or now
                resource.drain_ack_at = resource.drain_ack_at or now
            return True
    return await retry_deadlocks(transaction)


async def list_stale_workers(*, stale_seconds: int = _HEARTBEAT_SECONDS) -> list[str]:
    cutoff = _now() - timedelta(seconds=stale_seconds)
    async with _factory()() as session, session.begin():
        rows = list((await session.execute(select(ChatWorkerRegistration).where(
            ChatWorkerRegistration.heartbeat_at < cutoff, ChatWorkerRegistration.accepting.is_(True)
        ).order_by(ChatWorkerRegistration.id).with_for_update())).scalars())
        for row in rows:
            row.accepting = False
        return [row.id for row in rows]


async def eligible_worker_demand(pool_id: str) -> tuple[int, int, datetime | None]:
    """Pool-owned demand as SQL aggregates: (live leased runs, queued runs, oldest queued).

    Queued demand needs no live registration so a min=0 pool can cold start; it uses the
    same class/pool/protocol/plugin/batch predicate as worker candidates and claims.
    Active work is DB live leases, never heartbeat counts.
    """
    now = _now()
    async with _factory()() as session:
        pool = await session.get(ChatRuntimePool, pool_id)
        if pool is None or pool.role != "worker" or pool.workload_class is None:
            return 0, 0, None
        config = get_settings().runtime_config
        guest = next((profile for profile in config.guest_profiles if profile.id == pool.guest_profile_id), None)
        queued, oldest = (await session.execute(select(func.count(), func.min(ChatRun.created_at)).where(
            *worker_routing.pool_demand_filter(
                pool.id, pool.workload_class, now=now,
                protocol_versions=SUPPORTED_EXECUTION_PROTOCOL_VERSIONS,
                plugin_digest=guest.plugin_digest if guest is not None else None,
            )
        ))).one()
        active = await session.scalar(select(func.count()).select_from(ChatRun).where(
            *worker_routing.pool_live_filter(pool.id, pool.workload_class, now=now)
        ))
        return int(active or 0), int(queued or 0), oldest


async def _mark_ready_locked(session: AsyncSession, resource: ChatRuntimeResource | None,
                             pool: ChatRuntimePool | None, owner: str, fence: int, *,
                             policy_verified: bool = False) -> bool:
    """Shared readiness gate; ingress can commit ACTIVE evidence under its write lock."""
    if (not resource or not _owns(pool, owner, fence)
            or (resource.observed_state not in {"booting", "ready"}
                and not (resource.role == "api" and policy_verified and resource.observed_state == "unavailable"))):
        return False
    if policy_verified and resource.role in {"sandbox", "api"}:
        resource.heartbeat_at = _now()
    if (resource.desired_state == "deleting" or resource.drain_requested_at is not None
        or resource.bootstrap_token_hash is not None or not resource.certificate_fingerprint or not resource.heartbeat_at
        or (resource.role in {"api", "worker"} and not accepts_fingerprint(resource, resource.certificate_fingerprint))
        or _utc(resource.heartbeat_at) < _now() - timedelta(seconds=_HEARTBEAT_SECONDS)):
        return False
    if resource.role in {"sandbox", "api"} and not policy_verified:
        return False
    if resource.role == "api" and resource.ingress_member_id is None:
        return False
    if resource.role == "worker":
        registered = (await session.execute(select(ChatWorkerRegistration).where(
            ChatWorkerRegistration.resource_id == resource.id,
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
    return True


async def mark_ready(resource_id: str, generation: int, owner: str, fence: int, *, policy_verified: bool = False) -> bool:
    """Bootstrap identity and explicit health verification gate application readiness."""
    async with _factory()() as session, session.begin():
        resource, pool = await _locked_resource(session, resource_id, generation)
        if not await _mark_ready_locked(session, resource, pool, owner, fence, policy_verified=policy_verified):
            return False
    await wake_ready_runs(resource_id)
    return True


async def record_api_load(resource_id: str, generation: int, owner: str, fence: int, *,
                          active_count: int, snapshot: dict | None = None,
                          drain_acknowledged: bool = False, drain_fence: int | None = None) -> bool:
    if type(active_count) is not int or active_count < 0:
        return False
    if snapshot is not None:
        import json

        from lumen.services.infrastructure.guest_api import verified_load
        load = verified_load(json.dumps(snapshot).encode())
        if load is None or active_count != load["active_requests"] + load["active_ws"]:
            return False
    async def transaction():
        async with _factory()() as session, session.begin():
            await worker_routing.use_read_committed(session)
            resource, pool = await _locked_resource(session, resource_id, generation)
            if (not resource or not _owns(pool, owner, fence) or resource.role != "api"
                    or resource.observed_state not in {"ready", "draining", "unavailable", "booting"}
                    or resource.desired_state == "deleting"):
                return False
            resource.active_slots = active_count
            resource.heartbeat_at = _now()
            if snapshot is not None:
                resource.api_counter_snapshot = snapshot
                resource.api_counter_snapshot_at = _now()
                resource.idle_since = (resource.idle_since or _now()) if not active_count else None
                if (drain_acknowledged and type(drain_fence) is int and drain_fence == fence
                        and resource.drain_requested_at is not None):
                    resource.drain_ack_at = _now()
            return True
    return await retry_deadlocks(transaction)


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


async def observe_service_time(pool_id: str, owner: str, fence: int, *, cold_ms: int) -> int:
    """Consume at most 128 terminal-run samples, once by (completion time, run ID).

    Only scalar SQL columns are loaded. Alpha=1/5, elapsed is bounded to 1 ms–24 h;
    an estimate without a completion in the last 24 h falls back to class policy.
    """
    async def transaction():
        async with _factory()() as session, session.begin():
            await worker_routing.use_read_committed(session)
            pool = (await session.execute(select(ChatRuntimePool).where(ChatRuntimePool.id == pool_id)
                .with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
            if not _owns(pool, owner, fence):
                raise ResourceUnavailable("pool_lease_lost")
            now = _now()
            cutoff = now - timedelta(days=1)
            completions = (select(ChatRun.id.label("run_id"), ChatRun.provider_started_at.label("started"),
                                  func.max(ChatRunEventRow.created_at).label("completed"))
                .join(ChatRunEventRow, ChatRunEventRow.run_id == ChatRun.id)
                .where(ChatRun.worker_pool_id == pool_id, ChatRun.workload_class == pool.workload_class,
                       ChatRun.status == "completed", ChatRun.provider_started_at.is_not(None),
                       ChatRunEventRow.event_type == "run.completed")
                .group_by(ChatRun.id, ChatRun.provider_started_at).subquery())
            statement = select(completions).where(completions.c.completed >= cutoff,
                                                   completions.c.completed <= now)
            if pool.service_time_cursor_at is not None:
                statement = statement.where(or_(completions.c.completed > pool.service_time_cursor_at,
                    and_(completions.c.completed == pool.service_time_cursor_at,
                         completions.c.run_id > (pool.service_time_cursor_id or ""))))
            samples = (await session.execute(statement.order_by(completions.c.completed, completions.c.run_id)
                .limit(128))).all()
            estimate = (pool.service_time_estimate_ms if pool.service_time_cursor_at is not None
                        and _utc(pool.service_time_cursor_at) >= cutoff else cold_ms)
            for run_id, started, completed in samples:
                elapsed = max(1, min(86400000, int((_utc(completed) - _utc(started)).total_seconds() * 1000)))
                estimate = max(1, (4 * estimate + elapsed) // 5)
                pool.service_time_cursor_at = completed
                pool.service_time_cursor_id = run_id
            pool.service_time_estimate_ms = estimate
            return estimate
    return await retry_deadlocks(transaction)


async def update_worker_idle(pool_id: str, owner: str, fence: int) -> None:
    """Idleness includes DB run/auxiliary leases and the registered local busy count."""
    from lumen.services import auxiliary

    async def transaction():
        async with _factory()() as session, session.begin():
            await worker_routing.use_read_committed(session)
            pool = (await session.execute(select(ChatRuntimePool).where(ChatRuntimePool.id == pool_id)
                .with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
            if not _owns(pool, owner, fence):
                return
            resources = list((await session.execute(select(ChatRuntimeResource).where(
                ChatRuntimeResource.pool_id == pool_id, ChatRuntimeResource.observed_state == "ready",
            ).order_by(ChatRuntimeResource.id).with_for_update()
              .execution_options(populate_existing=True))).scalars())
            for resource in resources:
                registrations = list((await session.execute(select(ChatWorkerRegistration).where(
                    ChatWorkerRegistration.resource_id == resource.id,
                ).order_by(ChatWorkerRegistration.id).with_for_update()
                  .execution_options(populate_existing=True))).scalars())
                busy = not registrations
                for registration in registrations:
                    busy = bool(busy or registration.auxiliary_active
                                or _utc(registration.heartbeat_at) < _now() - timedelta(seconds=_HEARTBEAT_SECONDS)
                                or (await auxiliary.lease_counts(session, registration.id)).total)
                resource.idle_since = None if busy else resource.idle_since or _now()
    await retry_deadlocks(transaction)


async def write_projection(pool_id: str, owner: str, fence: int, *, desired: int, ready: int,
                           provisioning: int, draining: int, queued: int, oldest: datetime | None,
                           reason: str | None, high_samples: int, low_since: float | None) -> bool:
    async def transaction():
        async with _factory()() as session, session.begin():
            await worker_routing.use_read_committed(session)
            pool = (await session.execute(select(ChatRuntimePool).where(ChatRuntimePool.id == pool_id)
                .with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
            if not _owns(pool, owner, fence):
                return False
            pool.desired_replicas = desired
            pool.ready_replicas = ready
            pool.provisioning_replicas = provisioning
            pool.draining_replicas = draining
            pool.queued_count = queued
            pool.oldest_queued_seconds = max(0, int((_now() - _utc(oldest)).total_seconds())) if oldest else None
            pool.last_scale_reason = reason
            pool.last_reconciled_at = _now()
            pool.high_demand_samples = high_samples
            pool.low_demand_since = datetime.fromtimestamp(low_since, UTC) if low_since is not None else None
            return True
    return await retry_deadlocks(transaction)


async def claim_ingress_create(resource_id: str, generation: int, owner: str, fence: int) -> bool:
    """Commit the one-shot member create intent BEFORE Octavia; restart may only adopt."""
    async def transaction():
        async with _factory()() as session, session.begin():
            await worker_routing.use_read_committed(session)
            resource, pool = await _locked_resource(session, resource_id, generation)
            if (resource is None or not _owns(pool, owner, fence) or resource.role != "api"
                    or resource.drain_requested_at is not None or resource.desired_state == "deleting"):
                return False
            operation = await session.scalar(select(ChatResourceOperation).where(
                ChatResourceOperation.resource_id == resource_id,
                ChatResourceOperation.generation == generation,
                ChatResourceOperation.action == "ingress_create",
            ).with_for_update().execution_options(populate_existing=True))
            if operation is not None:
                if (operation.request_status != "failed" or operation.attempts != 0
                        or operation.owner != _UNCLAIMED or operation.provider_request_id is not None):
                    return False
                operation.request_status = "claimed"
                operation.owner, operation.fence, operation.attempts = owner, fence, 1
                operation.error_code = None
            else:
                session.add(ChatResourceOperation(id=str(uuid4()), resource_id=resource_id, generation=generation,
                    action="ingress_create", request_status="claimed", owner=owner, fence=fence, attempts=1))
            return True
    return await retry_deadlocks(transaction)


async def defer_unsubmitted_ingress_create(resource_id: str, generation: int, owner: str, fence: int) -> bool:
    """Release this owner/fence's claim after it certifies no create I/O occurred.

    The pool lease may already be lost: only the claimant knows the write never
    started, and a new owner keeps treating the claim as ambiguous until released.
    """
    async def transaction():
        async with _factory()() as session, session.begin():
            await worker_routing.use_read_committed(session)
            resource, pool = await _locked_resource(session, resource_id, generation)
            if resource is None or pool is None or resource.ingress_member_id is not None:
                return False
            operation = await session.scalar(select(ChatResourceOperation).where(
                ChatResourceOperation.resource_id == resource_id,
                ChatResourceOperation.generation == generation,
                ChatResourceOperation.action == "ingress_create",
            ).with_for_update().execution_options(populate_existing=True))
            if (operation is None or operation.owner != owner or operation.fence != fence
                    or operation.request_status != "claimed" or operation.provider_request_id is not None):
                return False
            operation.request_status = "failed"
            operation.owner, operation.attempts = _UNCLAIMED, 0
            operation.error_code = "ingress_pool_busy"
            return True
    return await retry_deadlocks(transaction)


async def unresolved_ingress_create(pool_id: str, *, resource_id: str | None = None) -> bool:
    async with _factory()() as session:
        statement = select(ChatResourceOperation.id).join(ChatRuntimeResource,
            ChatRuntimeResource.id == ChatResourceOperation.resource_id).where(
            ChatRuntimeResource.pool_id == pool_id,
            ChatRuntimeResource.observed_state != "deleted",
            ChatRuntimeResource.ingress_member_id.is_(None),
            ChatResourceOperation.action == "ingress_create",
            ChatResourceOperation.attempts > 0,
            ChatResourceOperation.provider_request_id.is_(None))
        if resource_id is not None:
            statement = statement.where(ChatRuntimeResource.id == resource_id)
        return await session.scalar(statement.limit(1)) is not None


async def record_ingress_member(resource_id: str, generation: int, owner: str, fence: int,
                                member_id: str) -> bool:
    async def transaction():
        async with _factory()() as session, session.begin():
            await worker_routing.use_read_committed(session)
            resource, pool = await _locked_resource(session, resource_id, generation)
            if resource is None or not _owns(pool, owner, fence):
                return False
            resource.ingress_member_id = member_id
            operation = await session.scalar(select(ChatResourceOperation).where(
                ChatResourceOperation.resource_id == resource_id,
                ChatResourceOperation.generation == generation,
                ChatResourceOperation.action == "ingress_create",
            ).with_for_update().execution_options(populate_existing=True))
            if operation is not None:
                operation.provider_request_id = member_id
                operation.request_status = "succeeded"
            return True
    return await retry_deadlocks(transaction)


async def healthy_worker_resources(pool_id: str) -> set[str]:
    """Scalar capacity projection; stale heartbeats and revoked identities are not serving."""
    now = _now()
    cutoff = now - timedelta(seconds=_HEARTBEAT_SECONDS)
    async with _factory()() as session:
        return set((await session.execute(select(ChatRuntimeResource.id).distinct()
            .join(ChatWorkerRegistration, ChatWorkerRegistration.resource_id == ChatRuntimeResource.id)
            .where(ChatRuntimeResource.pool_id == pool_id, ChatRuntimeResource.role == "worker",
                   ChatRuntimeResource.observed_state == "ready", ChatRuntimeResource.desired_state != "deleting",
                   ChatRuntimeResource.accepting.is_(True), ChatRuntimeResource.drain_requested_at.is_(None),
                   ChatRuntimeResource.bootstrap_token_hash.is_(None),
                   ChatRuntimeResource.certificate_not_after > now, ChatRuntimeResource.heartbeat_at >= cutoff,
                   ChatWorkerRegistration.pool_id == pool_id,
                   ChatWorkerRegistration.resource_generation == ChatRuntimeResource.generation,
                   ChatWorkerRegistration.certificate_fingerprint == ChatRuntimeResource.certificate_fingerprint,
                   ChatWorkerRegistration.accepting.is_(True), ChatWorkerRegistration.draining.is_(False),
                   ChatWorkerRegistration.heartbeat_at >= cutoff))).scalars())
