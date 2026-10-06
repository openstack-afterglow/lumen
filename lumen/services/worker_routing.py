"""Durable worker routing: workload class, worker pool and claim eligibility.

One SQL predicate defines which queued runs a worker may see, claim and recover and
which runs count as a pool's queued demand. Worker pools are persisted runtime pool
rows resolved inside the caller's transaction; fixed (runtime-disabled) workers only
serve ``worker_pool_id IS NULL`` runs. Realtime runs are API-owned and never enter a
worker candidate set. ``worker_pool_id`` is unrelated to the sandbox ``runtime_pool_id``.

Global lock order for claim/renew/drain: batch (if any) -> trusted resource ->
registration -> run. Run finalization never locks its batch. Every transaction that
uses these gates must run under READ COMMITTED (``use_read_committed`` first): they
read identities before locking, and live-lease counts after a lock wait must observe
claims committed meanwhile rather than an older REPEATABLE READ snapshot.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import ColumnElement, exists, false, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen.config import get_settings
from lumen.models.chat_batches import ChatBatch
from lumen.models.chat_infrastructure import ChatRuntimePool, ChatRuntimeResource, ChatWorkerRegistration
from lumen.models.chat_runs import ChatRun, ChatRunSegment
from lumen.services.durable_runs.errors import DurableRunError
from lumen.services.infrastructure.identity import accepts_fingerprint

WORKER_WORKLOAD_CLASSES = ("online_text", "online_media", "batch")
WORKLOAD_CLASSES = (*WORKER_WORKLOAD_CLASSES, "realtime")
REGISTRATION_FRESH_SECONDS = 20
# Statuses whose owner may hold a live worker lease.
_LEASED_STATUSES = (
    "running",
    "awaiting_approval",
    "awaiting_input",
    "waiting_children",
    "waiting_resource",
    "finalizing",
)
_KIND_CLASSES: dict[str, frozenset[str]] = {
    "completion": frozenset({"online_text"}),
    "compaction": frozenset({"online_text"}),
    "image": frozenset({"online_media", "batch"}),
    "tts": frozenset({"online_media", "batch"}),
    "stt": frozenset({"online_media", "batch"}),
    "realtime": frozenset({"realtime"}),
    "api_completion": frozenset({"batch"}),
}
_DEFAULT_CLASS = {
    "completion": "online_text",
    "compaction": "online_text",
    "image": "online_media",
    "tts": "online_media",
    "stt": "online_media",
    "realtime": "realtime",
}


def _now() -> datetime:
    return datetime.now(UTC)


async def use_read_committed(session: AsyncSession) -> None:
    """Must be the first statement of the transaction (connection-level isolation)."""
    await session.connection(execution_options={"isolation_level": "READ COMMITTED"})


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


@dataclass(frozen=True)
class WorkerRoute:
    workload_class: str
    worker_pool_id: str | None


async def resolve_worker_route(
    session: AsyncSession, *, run_kind: str, workload_class: str | None = None, batch_id: str | None = None
) -> WorkerRoute:
    """Route a new run inside the caller's transaction; never invents or falls back across pools.

    The pool assignment depends only on persisted operator configuration, not on live
    registrations, so a min=0 pool still receives queued demand and can cold start.
    """
    allowed = _KIND_CLASSES.get(run_kind)
    if allowed is None:
        raise DurableRunError(f"unsupported run kind for worker routing: {run_kind}")
    selected = workload_class or _DEFAULT_CLASS.get(run_kind)
    if selected is None or selected not in allowed:
        raise DurableRunError("workload_class_not_allowed")
    if (batch_id is not None) != (selected == "batch"):
        raise DurableRunError("batch_workload_mismatch")
    if selected == "realtime":
        return WorkerRoute("realtime", None)
    config = get_settings().runtime_config
    pool_name = config.workload_pools.get(selected) if config.enabled else None
    if pool_name is None:
        return WorkerRoute(selected, None)
    pool = (await session.execute(select(ChatRuntimePool).where(
        ChatRuntimePool.deployment_id == config.deployment_id, ChatRuntimePool.name == pool_name,
    ))).scalar_one_or_none()
    if pool is None or not pool.enabled or pool.role != "worker" or pool.workload_class != selected:
        raise DurableRunError("worker_pool_unavailable")
    return WorkerRoute(selected, pool.id)


def batch_dispatchable(batch: ChatBatch | None, *, now: datetime | None = None) -> bool:
    return bool(batch is not None and batch.status == "in_progress" and _utc(batch.expires_at) > (now or _now()))


def _checkpointed_run() -> ColumnElement[bool]:
    return exists(select(ChatRunSegment.run_id).where(
        ChatRunSegment.run_id == ChatRun.id, ChatRunSegment.status == "completed",
    ))


def batch_gate(*, now: datetime) -> ColumnElement[bool]:
    """Non-batch runs pass; batch runs only while their batch is in progress and unexpired.

    A batch run that already holds a completed provider checkpoint stays claimable after
    cancel/expiry so its result is settled exactly once by replay, never re-inferred.
    """
    return or_(
        ChatRun.batch_id.is_(None),
        exists(select(ChatBatch.id).where(
            ChatBatch.id == ChatRun.batch_id, ChatBatch.status == "in_progress", ChatBatch.expires_at > now,
        )),
        _checkpointed_run(),
    )


async def has_provider_checkpoint(session: AsyncSession, run_id: str) -> bool:
    return (await session.execute(select(ChatRunSegment.run_id).where(
        ChatRunSegment.run_id == run_id, ChatRunSegment.status == "completed",
    ).limit(1))).scalar_one_or_none() is not None


def run_scope_filter(
    *,
    workload_classes: Iterable[str],
    pool_id: str | None,
    protocol_versions: Iterable[int] | None = None,
    plugin_digest: str | None = None,
) -> list[ColumnElement[bool]]:
    """Exact class + pinned pool (+ protocol/plugin when a registration is known)."""
    classes = [value for value in workload_classes if value in WORKER_WORKLOAD_CLASSES]
    conditions: list[ColumnElement[bool]] = [
        ChatRun.workload_class.in_(classes) if classes else false(),
        ChatRun.worker_pool_id.is_(None) if pool_id is None else ChatRun.worker_pool_id == pool_id,
    ]
    if protocol_versions is not None:
        versions = list(protocol_versions)
        conditions.append(ChatRun.execution_protocol_version.in_(versions) if versions else false())
    if plugin_digest is not None:
        conditions.append(or_(ChatRun.required_plugin_digest.is_(None), ChatRun.required_plugin_digest == plugin_digest))
    return conditions


def queued_filter(*, now: datetime) -> list[ColumnElement[bool]]:
    return [ChatRun.status == "queued", ChatRun.cancel_requested_at.is_(None), batch_gate(now=now)]


def registration_run_filter(registration: ChatWorkerRegistration) -> list[ColumnElement[bool]]:
    return run_scope_filter(
        workload_classes=registration.workload_classes,
        pool_id=registration.pool_id,
        protocol_versions=registration.protocol_versions,
        plugin_digest=registration.plugin_digest,
    )


def run_matches_registration(run: ChatRun, registration: ChatWorkerRegistration) -> bool:
    """Python mirror of ``registration_run_filter`` for an already-locked run row."""
    return (
        run.workload_class in WORKER_WORKLOAD_CLASSES
        and run.workload_class in registration.workload_classes
        and run.worker_pool_id == registration.pool_id
        and run.execution_protocol_version in registration.protocol_versions
        and (run.required_plugin_digest is None or run.required_plugin_digest == registration.plugin_digest)
    )


def live_lease_filter(registration_id: str, *, now: datetime) -> list[ColumnElement[bool]]:
    return [
        ChatRun.worker_registration_id == registration_id,
        ChatRun.status.in_(_LEASED_STATUSES),
        ChatRun.lease_owner.is_not(None),
        ChatRun.lease_expires_at > now,
    ]


async def live_run_leases(
    session: AsyncSession, registration_id: str, *, now: datetime | None = None, lock: bool = False
) -> int:
    """DB-authoritative running work for one registration; heartbeat counts never grant capacity.

    ``lock`` makes it a current (share-locking) read for the claim capacity fence, so the
    count reflects concurrently committed claims even under REPEATABLE READ; the caller
    must already hold the registration row lock (registration -> run order).
    """
    statement = select(func.count()).select_from(ChatRun).where(*live_lease_filter(registration_id, now=now or _now()))
    if lock:
        statement = statement.with_for_update(read=True)
    return int(await session.scalar(statement) or 0)


async def lock_open_batch(session: AsyncSession, batch_id: str) -> bool:
    """Share-lock a batch before any run lock; True only while new provider I/O is allowed."""
    batch = (await session.execute(
        select(ChatBatch).where(ChatBatch.id == batch_id).with_for_update(read=True)
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    return batch_dispatchable(batch)


def _trusted_identity_valid(registration: ChatWorkerRegistration, resource: ChatRuntimeResource | None) -> bool:
    if registration.resource_id is None:
        return registration.resource_generation is None and registration.certificate_fingerprint is None
    return bool(
        resource is not None
        and resource.role == "worker"
        and resource.desired_state != "deleting"
        and resource.observed_state not in {"deleted", "failed"}
        and resource.bootstrap_token_hash is None
        and resource.pool_id == registration.pool_id
        and resource.generation == registration.resource_generation
        and registration.certificate_fingerprint is not None
        and accepts_fingerprint(resource, registration.certificate_fingerprint)
    )


async def lock_registration_rows(
    session: AsyncSession, registration_id: str
) -> tuple[ChatWorkerRegistration | None, ChatRuntimeResource | None]:
    """Lock resource then registration without validating identity (drain/ack paths)."""
    resource_id = (await session.execute(select(ChatWorkerRegistration.resource_id).where(
        ChatWorkerRegistration.id == registration_id
    ))).scalar_one_or_none()
    resource = None
    if resource_id is not None:
        resource = (await session.execute(
            select(ChatRuntimeResource).where(ChatRuntimeResource.id == resource_id).with_for_update()
            .execution_options(populate_existing=True)
        )).scalar_one_or_none()
    registration = (await session.execute(
        select(ChatWorkerRegistration).where(ChatWorkerRegistration.id == registration_id).with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if registration is not None and registration.resource_id != resource_id:
        # Re-pointed between the identity read and the lock; never act on a stale pairing.
        return None, None
    return registration, resource


async def lock_registration(
    session: AsyncSession, registration_id: str, *, owner: str | None = None, require_accepting: bool = True
) -> tuple[ChatWorkerRegistration, ChatRuntimeResource | None] | None:
    """Lock trusted resource then registration; None unless the identity is still valid.

    ``require_accepting`` additionally demands a fresh, accepting, non-draining
    registration on a ready resource without a drain request (new claims/admission).
    Without it (heartbeat, owned-lease renewal) a draining worker keeps its authority.
    """
    registration, resource = await lock_registration_rows(session, registration_id)
    if registration is None:
        return None
    if owner is not None and registration.worker_identity != owner:
        return None
    if not _trusted_identity_valid(registration, resource):
        return None
    if require_accepting:
        now = _now()
        if (registration.draining or not registration.accepting
                or _utc(registration.heartbeat_at) < now - timedelta(seconds=REGISTRATION_FRESH_SECONDS)):
            return None
        if resource is not None and (
            resource.observed_state != "ready" or not resource.accepting or resource.drain_requested_at is not None
        ):
            return None
    return registration, resource


def run_hint_key(workload_class: str, worker_pool_id: str | None) -> str:
    """Redis wakeup list for exactly one class/pool; hints only, DB polling is authoritative."""
    return f"afterglow:chat:runs:{workload_class}:{worker_pool_id or 'fixed'}"


def pool_demand_filter(
    pool_id: str, workload_class: str, *, now: datetime,
    protocol_versions: Iterable[int], plugin_digest: str | None,
) -> list[ColumnElement[bool]]:
    """Queued demand for one declared pool; intentionally independent of live registrations.

    Protocol/plugin come from the release the pool boots (same image as the controller
    and the pool's guest profile), so dead or incompatible work never scales a pool.
    """
    return [
        *run_scope_filter(workload_classes=(workload_class,), pool_id=pool_id,
                          protocol_versions=protocol_versions, plugin_digest=plugin_digest),
        *queued_filter(now=now),
    ]


def pool_live_filter(pool_id: str, workload_class: str, *, now: datetime) -> list[ColumnElement[bool]]:
    """Runs of this pool currently holding a live worker lease."""
    return [
        *run_scope_filter(workload_classes=(workload_class,), pool_id=pool_id),
        ChatRun.status.in_(_LEASED_STATUSES),
        ChatRun.lease_owner.is_not(None),
        ChatRun.lease_expires_at > now,
        ChatRun.worker_registration_id.is_not(None),
    ]


__all__ = [
    "REGISTRATION_FRESH_SECONDS",
    "WORKER_WORKLOAD_CLASSES",
    "WORKLOAD_CLASSES",
    "WorkerRoute",
    "batch_dispatchable",
    "batch_gate",
    "has_provider_checkpoint",
    "live_lease_filter",
    "live_run_leases",
    "lock_open_batch",
    "lock_registration",
    "lock_registration_rows",
    "pool_demand_filter",
    "pool_live_filter",
    "queued_filter",
    "registration_run_filter",
    "resolve_worker_route",
    "run_hint_key",
    "run_matches_registration",
    "run_scope_filter",
    "use_read_committed",
]
