"""Durable, idempotent post-response memory extraction jobs."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select

from lumen.models.chat_jobs import ChatJob
from lumen.models.chat_runs import ChatRun
from lumen.services import memory_store as ms
from lumen.services.memory_extract import generate_memory_if_applicable

_KIND = "memory_extract"
_LEASE_SECONDS = 120


def _now() -> datetime:
    return datetime.now(UTC)


async def enqueue_completed_run_in_transaction(session, run: ChatRun, *, memory_enabled: bool) -> None:
    """Queue one extraction only after its persistent top-level response committed."""
    if (
        not memory_enabled
        or run.status != "completed"
        or run.run_scope != "persistent"
        or run.parent_run_id is not None
        or run.conversation_id is None
    ):
        return
    idempotency_key = f"{_KIND}:{run.id}"
    existing = (
        await session.execute(select(ChatJob.id).where(ChatJob.idempotency_key == idempotency_key).limit(1))
    ).scalar_one_or_none()
    if existing is None:
        session.add(
            ChatJob(
                id=str(uuid.uuid4()),
                kind=_KIND,
                run_id=run.id,
                conversation_id=run.conversation_id,
                status="queued",
                idempotency_key=idempotency_key,
            )
        )


async def _claim_one(*, owner: str) -> tuple[str, str, str, str, str] | None:
    from lumen.db import get_session_factory

    factory = get_session_factory()
    if factory is None:
        return None
    now = _now()
    async with factory() as session, session.begin():
        job = (
            await session.execute(
                select(ChatJob)
                .where(
                    ChatJob.kind == _KIND,
                    or_(
                        (ChatJob.status == "queued") & (ChatJob.next_at <= now),
                        (ChatJob.status == "running")
                        & (ChatJob.lease_expires_at.is_not(None))
                        & (ChatJob.lease_expires_at < now),
                    ),
                )
                .order_by(ChatJob.next_at, ChatJob.created_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
        ).scalar_one_or_none()
        if job is None or job.run_id is None or job.conversation_id is None:
            return None
        run = await session.get(ChatRun, job.run_id)
        if run is None or run.conversation_id != job.conversation_id:
            job.status = "failed"
            job.error_code = "run_unavailable"
            return None
        job.status = "running"
        job.lease_owner = owner
        job.lease_expires_at = now + timedelta(seconds=_LEASE_SECONDS)
        job.attempts += 1
        return job.id, job.run_id, job.conversation_id, run.project_id, run.user_id


async def _apply_and_complete(
    job_id: str,
    *,
    owner: str,
    project_id: str,
    user_id: str,
    ops: list[dict] | None,
) -> bool:
    """Commit the owner-checked memory mutation and terminal job state together."""
    from lumen.db import get_session_factory

    factory = get_session_factory()
    if factory is None:
        raise RuntimeError("chat DB is unavailable")
    async with factory() as session, session.begin():
        job = await session.get(ChatJob, job_id, with_for_update=True)
        if job is None or job.status != "running" or job.lease_owner != owner:
            return False
        applied = {"add": 0, "update": 0, "delete": 0}
        if ops:
            applied = await ms.apply_automatic_ops_in_transaction(
                session,
                ops=ops,
                user_id=user_id,
                project_id=project_id,
            )
        job.status = "completed"
        job.lease_owner = None
        job.lease_expires_at = None
        job.progress = {"applied": applied}
        return True


async def _retry(job_id: str, *, owner: str) -> None:
    from lumen.db import get_session_factory

    factory = get_session_factory()
    if factory is None:
        return
    async with factory() as session, session.begin():
        job = await session.get(ChatJob, job_id, with_for_update=True)
        if job is None or job.status != "running" or job.lease_owner != owner:
            return
        job.status = "queued"
        job.lease_owner = None
        job.lease_expires_at = None
        job.error_code = "memory_extraction_failed"
        job.next_at = _now() + timedelta(seconds=min(60, 2 ** min(job.attempts, 6)))


async def process_one(*, owner: str) -> bool:
    """Claim and process at most one completed-response memory job."""
    claimed = await _claim_one(owner=owner)
    if claimed is None:
        return False
    job_id, run_id, conversation_id, project_id, user_id = claimed
    try:
        ops = await generate_memory_if_applicable(
            conversation_id=conversation_id,
            project_id=project_id,
            run_id=run_id,
            user_id=user_id,
        )
        await _apply_and_complete(
            job_id,
            owner=owner,
            project_id=project_id,
            user_id=user_id,
            ops=ops,
        )
    except Exception:
        await _retry(job_id, owner=owner)
    return True
