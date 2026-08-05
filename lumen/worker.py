"""Dedicated durable chat-run worker.

Redis wakeups reduce queue latency; MySQL queued-state claims and periodic scans remain
the authority when Redis is unavailable or a publish is missed.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket

from lumen.config import get_settings
from lumen.db import init_db
from lumen.cache import _get_redis
from lumen.services.agent_workspace_runtime import configured_workspace_policy
from lumen.services.code_workspace_service import delete_pending_workspaces, provision_pending_workspaces
from lumen.services.durable_runs import (
    execute_queued_run,
    expire_pending_inputs,
    purge_expired_temp_threads,
    queued_run_ids,
    recover_stale_runs,
)

logger = logging.getLogger(__name__)
_QUEUE = "afterglow:chat:runs"


async def _next_run_ids() -> list[str]:
    try:
        redis = await _get_redis()
        item = await redis.brpop(_QUEUE, timeout=1)
        if item is not None:
            _, value = item
            return [value.decode("utf-8") if isinstance(value, bytes) else str(value)]
    except Exception:
        logger.debug("chat worker Redis wakeup unavailable", exc_info=True)
    return await queued_run_ids(limit=4)


async def serve() -> None:
    settings = get_settings()
    if not settings.database_url:
        raise RuntimeError("chat worker requires database_url")
    init_db(
        settings.database_url,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
        connect_timeout=settings.database_connect_timeout,
        pool_timeout=settings.database_pool_timeout,
        unhealthy_seconds=settings.database_unhealthy_seconds,
    )

    from lumen.services.checkpointer import chat_checkpointer

    if settings.chat_checkpointer_postgres_url:
        await chat_checkpointer.start(settings.chat_checkpointer_postgres_url)
    from lumen.services.memory_jobs import process_one as process_memory_extraction
    from lumen.services.memory_outbox import process_one as process_memory_outbox
    from lumen.services.semantic_memory import semantic_memory_available, setup_semantic_memory

    if settings.chat_semantic_memory_enabled:
        try:
            await setup_semantic_memory()
        except Exception:
            logger.warning("semantic memory worker is unavailable; retaining MySQL manual memory", exc_info=True)

    memory_outbox_enabled = semantic_memory_available()
    owner = f"{socket.gethostname()}:{os.getpid()}"
    semaphore = asyncio.Semaphore(4)
    next_memory_outbox_at = 0.0
    next_temp_purge_at = 0.0
    next_workspace_reconcile_at = 0.0
    next_input_expiry_at = 0.0
    memory_job_task: asyncio.Task[None] | None = None

    async def execute(run_id: str) -> None:
        async with semaphore:
            try:
                await execute_queued_run(run_id, owner=owner)
            except Exception:
                logger.exception("durable chat run failed run_id=%s", run_id)

    async def extract_memory() -> None:
        try:
            await process_memory_extraction(owner=owner)
        except Exception:
            logger.exception("durable memory extraction job failed")

    try:
        while True:
            now = asyncio.get_running_loop().time()
            if now >= next_temp_purge_at:
                try:
                    purged = await purge_expired_temp_threads()
                    if purged:
                        logger.info("purged expired temporary chat threads count=%d", purged)
                except Exception:
                    logger.exception("temporary chat retention cleanup failed")
                next_temp_purge_at = now + 3_600
            if now >= next_workspace_reconcile_at:
                workspace_policy = configured_workspace_policy(settings)
                if workspace_policy is not None:
                    try:
                        await provision_pending_workspaces(workspace_policy)
                        await delete_pending_workspaces(workspace_policy)
                    except Exception:
                        logger.exception("code workspace reconciliation failed")
                next_workspace_reconcile_at = now + 1.0
            if now >= next_input_expiry_at:
                try:
                    expired_run_ids = await expire_pending_inputs()
                    if expired_run_ids:
                        logger.info("expired durable chat inputs resumed count=%d", len(expired_run_ids))
                except Exception:
                    logger.exception("durable chat input expiry sweep failed")
                next_input_expiry_at = now + 1.0
            if memory_outbox_enabled and now >= next_memory_outbox_at:
                try:
                    # The outbox itself commits its lease before all provider/pgvector I/O.
                    await process_memory_outbox(owner=owner)
                except Exception:
                    logger.exception("semantic memory outbox processing failed")
                next_memory_outbox_at = now + 0.5
            if memory_job_task is None or memory_job_task.done():
                memory_job_task = asyncio.create_task(extract_memory())
            try:
                recovered_run_ids = await recover_stale_runs(owner=owner)
            except Exception:
                logger.exception("stale durable chat run recovery failed")
                recovered_run_ids = []
            run_ids = list(dict.fromkeys([*recovered_run_ids, *(await _next_run_ids())]))
            if not run_ids:
                await asyncio.sleep(0.5)
                continue
            await asyncio.gather(*(execute(run_id) for run_id in run_ids))
    finally:
        if memory_job_task is not None:
            memory_job_task.cancel()
            await asyncio.gather(memory_job_task, return_exceptions=True)
        from lumen.db import close_db

        await chat_checkpointer.close()
        await close_db()


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    asyncio.run(serve())


if __name__ == "__main__":
    main()
