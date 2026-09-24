"""Dedicated durable chat-run worker.

Redis wakeups reduce queue latency; MariaDB queued-state claims and periodic scans remain
the authority when Redis is unavailable or a publish is missed.

Scheduling model (plan Step 3): a bounded set of active run tasks is refilled as soon as
any slot frees, independent bounded maintenance never blocks polling or lease renewal,
and drain stops new claims while renewing the leases of work already in flight.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
import uuid
from collections.abc import Awaitable, Callable

from lumen.cache import _get_redis
from lumen.config import get_settings
from lumen.db import init_db
from lumen.plugins.host import build_host
from lumen.plugins.registry import get_registry
from lumen.services.agent_workspace_runtime import configured_workspace_policy
from lumen.services.code_workspace_service import delete_pending_workspaces, provision_pending_workspaces
from lumen.services.durable_runs.execution import execute_queued_run, queued_run_ids
from lumen.services.durable_runs.interactions import expire_pending_inputs
from lumen.services.durable_runs.lifecycle import purge_expired_temp_threads, recover_stale_runs
from lumen.services.execution_protocol import SUPPORTED_EXECUTION_PROTOCOL_VERSIONS

logger = logging.getLogger(__name__)
_QUEUE = "afterglow:chat:runs"
_REGISTRATION_SCHEMA_VERSION = 1


async def _next_run_ids(limit: int) -> list[str]:
    if limit < 1:
        await asyncio.sleep(0.5)
        return []
    try:
        redis = await _get_redis()
        item = await redis.brpop(_QUEUE, timeout=1)
        if item is not None:
            _, value = item
            return [value.decode("utf-8") if isinstance(value, bytes) else str(value)]
    except Exception:
        logger.debug("chat worker Redis wakeup unavailable", exc_info=True)
    return await queued_run_ids(limit=limit)


async def _title_processor_loop(processor, *, owner: str) -> None:
    """Continuously drain title jobs independently of long-running completions."""
    while True:
        try:
            worked = await processor(owner=owner)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("durable title generation job failed")
            worked = False
        if not worked:
            await asyncio.sleep(0.5)


class _Maintenance:
    """One bounded background job; a slow iteration never blocks the claim loop."""

    def __init__(self, name: str, fn: Callable[[], Awaitable[None]], *, interval: float) -> None:
        self.name = name
        self.fn = fn
        self.interval = interval
        self.next_at = 0.0
        self.task: asyncio.Task[None] | None = None

    def tick(self, now: float) -> None:
        if now < self.next_at or (self.task is not None and not self.task.done()):
            return
        self.next_at = now + self.interval
        self.task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        try:
            await self.fn()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("worker maintenance job failed name=%s", self.name)

    async def stop(self) -> None:
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)


class WorkerLoop:
    """Bounded active tasks, immediate slot refill, registration heartbeat and drain."""

    def __init__(self, *, owner: str, capacity: int, heartbeat_seconds: int, drain_seconds: int) -> None:
        self.owner = owner
        self.capacity = capacity
        self.heartbeat_seconds = heartbeat_seconds
        self.drain_seconds = drain_seconds
        self.active: dict[str, asyncio.Task[None]] = {}
        self.draining = asyncio.Event()
        self.drain_started_at: float | None = None
        self.registration_id: str | None = None
        self.boot_id = str(uuid.uuid4())

    @property
    def free_slots(self) -> int:
        return 0 if self.draining.is_set() else max(0, self.capacity - len(self.active))

    def start_drain(self) -> None:
        if not self.draining.is_set():
            self.draining.set()
            self.drain_started_at = asyncio.get_running_loop().time()
            logger.info("worker drain started owner=%s active=%d", self.owner, len(self.active))

    def drain_overdue(self) -> bool:
        return (
            self.drain_started_at is not None
            and asyncio.get_running_loop().time() - self.drain_started_at > self.drain_seconds
        )

    def launch(self, run_id: str) -> None:
        if run_id in self.active or self.free_slots < 1:
            return

        async def execute() -> None:
            try:
                await execute_queued_run(run_id, owner=self.owner, registration_id=self.registration_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("durable chat run failed run_id=%s", run_id)
            finally:
                self.active.pop(run_id, None)

        self.active[run_id] = asyncio.create_task(execute())

    async def register(self) -> None:
        from lumen.services.infrastructure import store

        guest_dir = os.environ.get("LUMEN_GUEST_IDENTITY_DIR")
        resource_id = os.environ.get("LUMEN_RESOURCE_ID")
        if guest_dir or resource_id or os.environ.get("LUMEN_BOOTSTRAP_FILE"):
            if not guest_dir or not resource_id:
                raise RuntimeError("worker bootstrap identity missing")
            from pathlib import Path

            from lumen.services.infrastructure.guest_bootstrap import load_identity

            generation = int(os.environ["LUMEN_RESOURCE_GENERATION"])
            identity = load_identity(Path(guest_dir), role="worker", resource_id=resource_id, generation=generation)
        self.registration_id = await store.register_worker(
            worker_identity=self.owner,
            boot_id=self.boot_id,
            capacity=self.capacity,
            protocol_versions=sorted(SUPPORTED_EXECUTION_PROTOCOL_VERSIONS),
            plugin_digest=get_registry().digest,
            schema_version=_REGISTRATION_SCHEMA_VERSION,
            resource_id=resource_id,
            **({"resource_generation": generation,
                "certificate_fingerprint": identity["certificate_fingerprint"]} if guest_dir else {}),
        )
    async def heartbeat(self) -> None:
        from lumen.services.infrastructure import store

        if self.registration_id is None:
            return
        guest_dir = os.environ.get("LUMEN_GUEST_IDENTITY_DIR")
        if guest_dir and not self.draining.is_set():
            from pathlib import Path

            from lumen.services.infrastructure.guest_bootstrap import load_identity
            try:
                load_identity(Path(guest_dir), role="worker", resource_id=os.environ["LUMEN_RESOURCE_ID"],
                              generation=int(os.environ["LUMEN_RESOURCE_GENERATION"]))
            except Exception:
                logger.error("worker certificate expired or identity changed; entering drain")
                self.start_drain()
        alive = await store.heartbeat_worker(
            self.registration_id, active_count=len(self.active), accepting=not self.draining.is_set()
        )
        if not alive:
            # A lost registration means the controller may already count this worker as gone;
            # stop claiming so no run is dispatched to a worker nobody tracks.
            logger.warning("worker registration lost owner=%s; entering drain", self.owner)
            self.start_drain()

    async def mark_draining(self) -> None:
        from lumen.services.infrastructure import store

        if self.registration_id is not None:
            await store.start_drain(self.registration_id)


async def serve() -> None:
    settings = get_settings()
    if not settings.database_url:
        raise RuntimeError("chat worker requires database_url")
    registry = get_registry()
    registry.load()
    init_db(
        settings.database_url,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
        connect_timeout=settings.database_connect_timeout,
        pool_timeout=settings.database_pool_timeout,
        unhealthy_seconds=settings.database_unhealthy_seconds,
    )
    await registry.start(build_host())

    from lumen.services.checkpointer import chat_checkpointer

    if settings.chat_checkpointer_postgres_url:
        await chat_checkpointer.start(settings.chat_checkpointer_postgres_url)
    from lumen.services.memory_jobs import process_one as process_memory_extraction
    from lumen.services.memory_outbox import process_one as process_memory_outbox
    from lumen.services.semantic_memory import semantic_memory_available, setup_semantic_memory
    from lumen.services.title_jobs import process_one as process_title_generation

    if settings.chat_semantic_memory_enabled:
        try:
            await setup_semantic_memory()
        except Exception:
            logger.warning("semantic memory worker is unavailable; retaining MySQL manual memory", exc_info=True)

    memory_outbox_enabled = semantic_memory_available()
    owner = f"{socket.gethostname()}:{os.getpid()}"
    loop = WorkerLoop(
        owner=owner,
        capacity=settings.worker_concurrency,
        heartbeat_seconds=settings.worker_heartbeat_seconds,
        drain_seconds=settings.worker_drain_seconds,
    )
    await loop.register()

    async def extract_memory() -> None:
        await process_memory_extraction(owner=owner)

    async def reconcile_workspaces() -> None:
        workspace_policy = configured_workspace_policy(settings)
        if workspace_policy is not None:
            await provision_pending_workspaces(workspace_policy)
            await delete_pending_workspaces(workspace_policy)

    async def purge_temp() -> None:
        purged = await purge_expired_temp_threads()
        if purged:
            logger.info("purged expired temporary chat threads count=%d", purged)

    async def expire_inputs() -> None:
        expired_run_ids = await expire_pending_inputs()
        if expired_run_ids:
            logger.info("expired durable chat inputs resumed count=%d", len(expired_run_ids))

    async def outbox() -> None:
        # The outbox itself commits its lease before all provider/pgvector I/O.
        await process_memory_outbox(owner=owner)

    maintenance = [
        _Maintenance("temp_purge", purge_temp, interval=3_600),
        _Maintenance("workspace_reconcile", reconcile_workspaces, interval=1.0),
        _Maintenance("input_expiry", expire_inputs, interval=1.0),
        _Maintenance("memory_extraction", extract_memory, interval=0.5),
        _Maintenance("heartbeat", loop.heartbeat, interval=float(loop.heartbeat_seconds)),
    ]
    if memory_outbox_enabled:
        maintenance.append(_Maintenance("memory_outbox", outbox, interval=0.5))
    title_job_task = asyncio.create_task(_title_processor_loop(process_title_generation, owner=owner))

    running_loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            running_loop.add_signal_handler(signum, loop.start_drain)
        except (NotImplementedError, RuntimeError):
            pass

    drain_recorded = False
    try:
        while True:
            now = running_loop.time()
            for job in maintenance:
                job.tick(now)
            if loop.draining.is_set():
                if not drain_recorded:
                    drain_recorded = True
                    try:
                        await loop.mark_draining()
                    except Exception:
                        logger.exception("worker drain registration failed")
                if not loop.active:
                    break
                if loop.drain_overdue():
                    # Overdue drain retains the worker and keeps renewing leases: indeterminate
                    # calls are never force-reassigned to satisfy scale-in.
                    logger.warning("worker drain overdue owner=%s active=%d", owner, len(loop.active))
                await asyncio.wait(set(loop.active.values()), timeout=1.0, return_when=asyncio.FIRST_COMPLETED)
                continue
            if loop.free_slots < 1:
                await asyncio.wait(set(loop.active.values()), timeout=1.0, return_when=asyncio.FIRST_COMPLETED)
                continue
            try:
                recovered_run_ids = await recover_stale_runs(owner=owner)
            except Exception:
                logger.exception("stale durable chat run recovery failed")
                recovered_run_ids = []
            run_ids = list(dict.fromkeys([*recovered_run_ids, *(await _next_run_ids(loop.free_slots))]))
            if not run_ids:
                await asyncio.sleep(0.2)
                continue
            for run_id in run_ids[: loop.free_slots]:
                loop.launch(run_id)
    finally:
        for job in maintenance:
            await job.stop()
        title_job_task.cancel()
        await asyncio.gather(title_job_task, return_exceptions=True)
        if loop.active:
            await asyncio.gather(*loop.active.values(), return_exceptions=True)
        from lumen.db import close_db

        await chat_checkpointer.close()
        try:
            await registry.close()
        except Exception:
            logger.exception("plugin shutdown failed")
        await close_db()


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    asyncio.run(serve())


if __name__ == "__main__":
    main()
