"""Registration-owned online maintenance, durable leases, and drain evidence.

Auxiliary work is independent of run capacity. Only online_text registrations
may start it; drain keeps already admitted work and its lease renewal alive.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import wraps

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen.db import get_session_factory
from lumen.models.chat_batches import ChatBatch, ChatBatchProjectQueue
from lumen.models.chat_infrastructure import ChatWorkerRegistration
from lumen.models.chat_jobs import ChatJob, ChatMemoryOutbox
from lumen.services.durable_runs.budgets import retry_deadlocks
from lumen.services.worker_routing import live_run_leases, lock_registration, use_read_committed

logger = logging.getLogger(__name__)
LEASE_SECONDS = 120
RENEW_SECONDS = 20


async def lock_admission(session: AsyncSession, registration_id: str) -> ChatWorkerRegistration | None:
    """Lock trusted resource -> registration before a job; no legacy owner path."""
    # The caller must select READ COMMITTED before any query in its transaction.

    locked = await lock_registration(session, registration_id, require_accepting=True)
    if locked is None:
        return None
    registration, _resource = locked
    if "online_text" not in registration.workload_classes:
        return None
    return registration


@dataclass(frozen=True)
class LeaseCounts:
    runs: int = 0
    titles: int = 0
    memories: int = 0
    outbox: int = 0
    batches: int = 0
    project_queues: int = 0

    @property
    def auxiliary(self) -> int:
        return self.titles + self.memories + self.outbox + self.batches + self.project_queues

    @property
    def total(self) -> int:
        return self.runs + self.auxiliary


async def lease_counts(session: AsyncSession, registration_id: str) -> LeaseCounts:
    """Count DB live leases, not heartbeat active_count; caller may hold registration."""
    now = datetime.now(UTC)
    runs = await live_run_leases(session, registration_id, now=now)
    jobs = dict((await session.execute(select(ChatJob.kind, func.count()).where(
        ChatJob.lease_owner == registration_id,
        ChatJob.status == "running", ChatJob.lease_expires_at > now,
    ).group_by(ChatJob.kind))).all())
    outbox = await session.scalar(select(func.count()).select_from(ChatMemoryOutbox).where(
        ChatMemoryOutbox.lease_owner == registration_id,
        ChatMemoryOutbox.status == "running", ChatMemoryOutbox.lease_expires_at > now,
    ))
    batches = await session.scalar(select(func.count()).select_from(ChatBatch).where(
        ChatBatch.lease_owner == registration_id, ChatBatch.lease_expires_at > now,
    ))
    queues = await session.scalar(select(func.count()).select_from(ChatBatchProjectQueue).where(
        ChatBatchProjectQueue.lease_owner == registration_id, ChatBatchProjectQueue.lease_expires_at > now,
    ))
    return LeaseCounts(int(runs or 0), int(jobs.get("title_generate", 0)),
                       int(jobs.get("memory_extract", 0)), int(outbox or 0),
                       int(batches or 0), int(queues or 0))


async def worker_status(registration_id: str) -> tuple[int, LeaseCounts] | None:
    """Return persisted local-step count and independently counted live leases."""
    factory = get_session_factory()
    if factory is None:
        return None
    async with factory() as session:
        await use_read_committed(session)
        row = await session.get(ChatWorkerRegistration, registration_id)
        if row is None:
            return None
        return row.auxiliary_active, await lease_counts(session, registration_id)


def retry_db(function):
    """Retry a rollback-safe DB-only function; never decorate external I/O."""
    @wraps(function)
    async def wrapped(*args, **kwargs):
        return await retry_deadlocks(lambda: function(*args, **kwargs))
    return wrapped


@retry_db
async def renew_lease(*, owner: str, model: type[ChatJob] | type[ChatMemoryOutbox], key: str | int) -> bool:
    """Renew exactly one admitted job/outbox lease even after a normal drain fence."""
    if model not in (ChatJob, ChatMemoryOutbox):
        raise ValueError("unsupported auxiliary lease model")

    factory = get_session_factory()
    if factory is None:
        return False
    async with factory() as session, session.begin():
        await use_read_committed(session)
        locked = await lock_registration(session, owner, require_accepting=False)
        if locked is None:
            return False
        row = await session.get(model, key, with_for_update=True, populate_existing=True)
        now = datetime.now(UTC)
        if row is None or row.status != "running" or row.lease_owner != owner or row.lease_expires_at is None:
            return False
        expires = row.lease_expires_at
        expires = expires.replace(tzinfo=UTC) if expires.tzinfo is None else expires.astimezone(UTC)
        if expires <= now:
            return False
        row.lease_expires_at = now + timedelta(seconds=LEASE_SECONDS)
        return True


async def _renew_loop(owner: str, model: type[ChatJob] | type[ChatMemoryOutbox], key: str | int,
                      stopped: asyncio.Event) -> None:
    while not stopped.is_set():
        try:
            await asyncio.wait_for(stopped.wait(), timeout=RENEW_SECONDS)
        except TimeoutError:
            try:
                if not await renew_lease(owner=owner, model=model, key=key):
                    logger.warning("auxiliary renewal lost registration=%s", owner)
                    return
            except Exception:
                # Never cancel an in-flight provider/storage call on a transient
                # renewal failure. Expiry and owner checks still fence DB writes.
                logger.warning("auxiliary renewal unavailable registration=%s", owner, exc_info=True)


@asynccontextmanager
async def keep_lease(*, owner: str, model: type[ChatJob] | type[ChatMemoryOutbox], key: str | int):
    """Keep only this in-flight step's lease alive, never unrelated orphan work."""
    if model not in (ChatJob, ChatMemoryOutbox):
        raise ValueError("unsupported auxiliary lease model")
    stopped = asyncio.Event()
    renewer = asyncio.create_task(_renew_loop(owner, model, key, stopped))
    try:
        yield
    finally:
        stopped.set()
        await renewer


@retry_db
async def _begin_step(owner: str) -> bool:
    factory = get_session_factory()
    if factory is None:
        return False
    async with factory() as session, session.begin():
        await use_read_committed(session)
        row = await lock_admission(session, owner)
        if row is None:
            return False
        row.auxiliary_active += 1
        return True


@retry_db
async def _end_step(owner: str) -> None:
    factory = get_session_factory()
    if factory is None:
        raise RuntimeError("chat DB unavailable during auxiliary completion")
    async with factory() as session, session.begin():
        await use_read_committed(session)
        locked = await lock_registration(session, owner, require_accepting=False)
        # Even identity loss must not hide completion of this registration's
        # local work. The gate already locks its resource before registration.
        row = locked[0] if locked is not None else await session.get(
            ChatWorkerRegistration, owner, with_for_update=True, populate_existing=True,
        )
        if row is not None:
            if row.auxiliary_active < 1:
                raise RuntimeError("auxiliary busy counter underflow")
            row.auxiliary_active -= 1


@asynccontextmanager
async def step(*, owner: str):
    """Gate and track one online auxiliary step, including non-leased maintenance.

    Job processors use worker_step themselves. Worker integration uses this only
    for non-job maintenance (e.g. workspace/S3 reconciliation), never heartbeat.
    """
    if not await _begin_step(owner):
        yield False
        return
    try:
        yield True
    finally:
        await _end_step(owner)


async def finish_inflight(awaitable):
    """Await admitted work to completion even if its enclosing loop is cancelled."""
    task = asyncio.ensure_future(awaitable)
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError
    return result


def worker_step(function):
    """Online-only registration gate, busy accounting, and finish-inflight wrapper."""
    @wraps(function)
    async def wrapped(*, owner: str):
        async def run():
            async with step(owner=owner) as admitted:
                if not admitted:
                    return False
                return await function(owner=owner)
        return await finish_inflight(run())
    return wrapped
