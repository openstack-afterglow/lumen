"""Transactional MySQL outbox claiming for pgvector memory mutations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import aliased

from lumen.config import get_settings
from lumen.crypto import decrypt_chat_content
from lumen.db import get_session_factory
from lumen.models.chat_db import ChatMemory
from lumen.models.chat_jobs import ChatMemoryOutbox
from lumen.services.memory_embeddings import embed_maintenance
from lumen.services.memory_index import MemoryVector
from lumen.services.memory_store import memory_content_fingerprint
from lumen.services.semantic_memory import configured_memory_index


@dataclass(frozen=True)
class ClaimedMemoryMutation:
    change_seq: int
    memory_id: int
    mutation: str
    content_hash: str
    required_generations: tuple[int, ...]
    plaintext: str | None
    user_id: str | None
    project_id: str | None
    workspace_id: int | None
    active: bool


def _snapshot(row: ChatMemoryOutbox, memory: ChatMemory | None) -> ClaimedMemoryMutation:
    return ClaimedMemoryMutation(
        change_seq=row.change_seq,
        memory_id=row.memory_id,
        mutation=row.mutation,
        content_hash=row.content_hash,
        required_generations=tuple(int(value) for value in row.required_generations),
        plaintext=decrypt_chat_content(memory.content) if memory and memory.content else None,
        user_id=memory.user_id if memory else None,
        project_id=memory.project_id if memory else None,
        workspace_id=memory.workspace_id if memory else None,
        active=bool(memory and memory.status == "active" and memory.is_active),
    )


async def activate_pending_generations(session, *, required_generations: list[int]) -> int:
    """Fence deferred mutations with the current active/target generation set after recovery."""
    if not required_generations:
        return 0
    rows = (
        (
            await session.execute(
                select(ChatMemoryOutbox)
                .where(ChatMemoryOutbox.status == "pending_generation")
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        row.required_generations = list(required_generations)
        row.applied_generations = []
        row.status = "queued"
    return len(rows)


async def claim_next(session, *, owner: str, lease_seconds: int = 60) -> ChatMemoryOutbox | None:
    """Claim the oldest queued/stale mutation under the caller's transaction."""
    now = datetime.now(UTC)
    prior = aliased(ChatMemoryOutbox)
    no_prior_mutation = (
        ~select(prior.change_seq)
        .where(
            prior.change_seq < ChatMemoryOutbox.change_seq,
            prior.status.in_(("pending_generation", "queued", "running")),
        )
        .exists()
    )
    row = (
        await session.execute(
            select(ChatMemoryOutbox)
            .where(
                (
                    (ChatMemoryOutbox.status == "queued")
                    | ((ChatMemoryOutbox.status == "running") & (ChatMemoryOutbox.lease_expires_at < now))
                ),
                no_prior_mutation,
            )
            .order_by(ChatMemoryOutbox.change_seq)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    row.status = "running"
    row.lease_owner = owner
    row.lease_expires_at = now + timedelta(seconds=lease_seconds)
    return row


async def claim_one(*, owner: str, lease_seconds: int = 60) -> ClaimedMemoryMutation | None:
    """Commit a MySQL lease and source snapshot before any embedding or vector I/O."""
    factory = get_session_factory()
    if factory is None:
        return None
    required_generations = await configured_memory_index().required_generations()
    async with factory() as session, session.begin():
        await activate_pending_generations(session, required_generations=required_generations)
        row = await claim_next(session, owner=owner, lease_seconds=lease_seconds)
        if row is None:
            return None
        memory = await session.get(ChatMemory, row.memory_id)
        return _snapshot(row, memory)


async def _finish_claim(
    claim: ClaimedMemoryMutation,
    *,
    owner: str,
    applied_generations: set[int],
    error_code: str | None = None,
) -> None:
    """Record only completed vector writes in a short owner-checked transaction."""
    factory = get_session_factory()
    if factory is None:
        return
    async with factory() as session, session.begin():
        row = await session.get(ChatMemoryOutbox, claim.change_seq, with_for_update=True)
        if row is None or row.status != "running" or row.lease_owner != owner:
            return

        current = await session.get(ChatMemory, row.memory_id)
        stale_upsert = row.mutation == "upsert" and (
            current is None
            or current.status != "active"
            or not current.content
            or memory_content_fingerprint(decrypt_chat_content(current.content)) != row.content_hash
        )
        generations = set(int(value) for value in row.required_generations)
        for generation in generations if stale_upsert else applied_generations:
            mark_generation_applied(row, generation=generation)
        if row.status == "completed":
            row.lease_owner = None
            row.lease_expires_at = None
            if row.mutation == "delete" and current is not None and current.status == "deleting":
                # The FK prevents ciphertext removal until this completed event is gone.
                await session.delete(row)
                await session.flush()
                await session.delete(current)
            return
        row.attempts += 1
        row.error_code = error_code or "memory_vector_apply_failed"
        row.status = "dead" if row.attempts >= 5 else "queued"
        row.lease_owner = None
        row.lease_expires_at = None


async def _apply_snapshot(claim: ClaimedMemoryMutation) -> set[int]:
    """Perform external work without retaining a MySQL row lock."""
    generations = set(claim.required_generations)
    if claim.mutation == "delete" or not claim.active or not claim.plaintext:
        for generation in generations:
            await configured_memory_index().delete(generation=generation, memory_id=claim.memory_id)
        return generations
    if memory_content_fingerprint(claim.plaintext) != claim.content_hash:
        return generations

    embedding = await embed_maintenance(claim.plaintext)
    applied: set[int] = set()
    for generation in generations:
        await configured_memory_index().upsert(
            MemoryVector(
                memory_id=claim.memory_id,
                generation=generation,
                user_id=claim.user_id or "",
                project_id=claim.project_id,
                workspace_id=claim.workspace_id,
                embedding=embedding,
                embedding_model=get_settings().chat_memory_embedding_model,
                content_hash=claim.content_hash,
            )
        )
        applied.add(generation)
    return applied


async def process_one(*, owner: str) -> bool:
    """Process one ordered mutation; false means no committed claim was available."""
    claim = await claim_one(owner=owner)
    if claim is None:
        return False
    try:
        applied = await _apply_snapshot(claim)
    except Exception:
        await _finish_claim(claim, owner=owner, applied_generations=set(), error_code="memory_vector_apply_failed")
        raise
    await _finish_claim(claim, owner=owner, applied_generations=applied)
    return True


def mark_generation_applied(row: ChatMemoryOutbox, *, generation: int) -> bool:
    """Record one generation exactly once and return whether the mutation is complete."""
    required = {int(value) for value in row.required_generations}
    if generation not in required:
        raise ValueError("generation is not required for this outbox mutation")
    applied = {int(value) for value in (row.applied_generations or [])}
    applied.add(generation)
    row.applied_generations = sorted(applied)
    if required <= applied:
        row.status = "completed"
        row.lease_owner = None
        row.lease_expires_at = None
        return True
    return False


async def apply_claimed(session, row: ChatMemoryOutbox) -> bool:
    """Apply an outbox row to all frozen generations using the maintenance route."""
    if row.status != "running" or not row.required_generations:
        raise ValueError("outbox row is not ready for vector application")
    memory = await session.get(ChatMemory, row.memory_id)
    index = configured_memory_index()
    for generation in row.required_generations:
        if row.mutation == "delete" or memory is None or memory.status != "active" or not memory.content:
            await index.delete(generation=int(generation), memory_id=row.memory_id)
        else:
            content = decrypt_chat_content(memory.content)
            if memory_content_fingerprint(content) != row.content_hash:
                mark_generation_applied(row, generation=int(generation))
                continue
            embedding = await embed_maintenance(content)
            await index.upsert(
                MemoryVector(
                    memory_id=memory.id,
                    generation=int(generation),
                    user_id=memory.user_id,
                    project_id=memory.project_id,
                    workspace_id=memory.workspace_id,
                    embedding=embedding,
                    embedding_model=get_settings().chat_memory_embedding_model,
                    content_hash=row.content_hash,
                )
            )
        mark_generation_applied(row, generation=int(generation))
    return row.status == "completed"
