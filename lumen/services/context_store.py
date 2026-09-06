"""Durable, owner-scoped source projections for context planning and compaction."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen.crypto import decrypt_chat_content, encrypt_chat_content
from lumen.db import get_session_factory, is_db_available
from lumen.models.chat_agent_platform import ChatContextCheckpoint
from lumen.models.chat_db import ChatMessage
from lumen.models.chat_runs import ChatRun, ChatTempThread
from lumen.services import conversation_store as cs

_ALLOWED_METADATA = {
    "version",
    "scope",
    "projection_version",
    "source_revision",
    "model_name",
    "tokenizer",
    "tool_schema_hash",
    "input_budget",
    "before_tokens",
    "after_tokens",
    "cause",
    # Optional private branch fence used when a caller has one. It is never
    # returned to an API client and lets a fork fail closed rather than reuse a
    # checkpoint from a sibling leaf.
    "active_leaf_id",
}


def _project_message(message: dict[str, Any]) -> dict[str, Any]:
    """Keep only provider-safe message fields; never expose database IDs."""
    projected: dict[str, Any] = {"role": message.get("role"), "content": message.get("content") or ""}
    for key in ("tool_calls", "tool_call_id", "name"):
        if message.get(key) is not None:
            projected[key] = message[key]
    return projected


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _source_revision(
    active_leaf_id: str | None,
    message_ids: list[str],
    source_hashes: list[str],
    checkpoint_id: str | None,
) -> str:
    return _digest(
        {
            "active_leaf_id": active_leaf_id,
            "message_ids": message_ids,
            "source_hashes": source_hashes,
            "checkpoint_id": checkpoint_id,
        }
    )


def _decode_temp_history(ciphertext: str) -> list[dict[str, Any]]:
    try:
        history = json.loads(decrypt_chat_content(ciphertext))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("temporary chat history is invalid") from exc
    if not isinstance(history, list):
        raise ValueError("temporary chat history is invalid")
    result: list[dict[str, Any]] = []
    for turn in history:
        if not isinstance(turn, dict) or turn.get("role") not in {"user", "assistant", "tool"}:
            raise ValueError("temporary chat history is invalid")
        parts = turn.get("parts")
        if isinstance(parts, list):
            text = "\n".join(str(p.get("text", "")) for p in parts if isinstance(p, dict) and p.get("type") == "text")
        else:
            text = str(turn.get("content") or "")
        if text:
            result.append({"role": turn["role"], "content": text})
    return result


@asynccontextmanager
async def _session_scope(session: AsyncSession | None) -> AsyncIterator[AsyncSession]:
    if session is not None:
        yield session
        return
    if not is_db_available() or get_session_factory() is None:
        raise RuntimeError("chat DB is unavailable")
    async with get_session_factory()() as created:
        yield created


def _same_prefix(stored: list[str], current: list[str]) -> bool:
    return bool(stored) and len(stored) <= len(current) and stored == current[: len(stored)]


async def _checkpoint_for_source(
    session: AsyncSession,
    *,
    conversation_id: str | None,
    temp_thread_id: str | None,
    user_id: str,
    project_id: str,
    message_ids: list[str],
    source_hashes: list[str],
    active_leaf_id: str | None = None,
) -> ChatContextCheckpoint | None:
    """Find the longest valid compacted prefix owned by this source scope."""
    query = (
        select(ChatContextCheckpoint)
        .join(ChatRun, ChatRun.id == ChatContextCheckpoint.run_id)
        .where(ChatRun.user_id == user_id, ChatRun.project_id == project_id)
    )
    if conversation_id is not None:
        query = query.where(
            ChatContextCheckpoint.conversation_id == conversation_id,
            ChatContextCheckpoint.temp_thread_id.is_(None),
        )
    else:
        query = query.join(ChatTempThread, ChatTempThread.id == ChatContextCheckpoint.temp_thread_id).where(
            ChatContextCheckpoint.temp_thread_id == temp_thread_id,
            ChatContextCheckpoint.conversation_id.is_(None),
            ChatTempThread.user_id == user_id,
            ChatTempThread.project_id == project_id,
            ChatTempThread.expires_at > datetime.now(UTC),
        )
    rows = (await session.execute(query.order_by(ChatContextCheckpoint.created_at.desc()))).scalars().all()
    best: ChatContextCheckpoint | None = None
    best_len = 0
    for checkpoint in rows:
        metadata = checkpoint.context_metadata
        if not isinstance(metadata, Mapping):
            # Legacy checkpoints lack the provenance needed for safe reuse.
            continue
        stored_ids = [str(item) for item in (checkpoint.source_message_ids or [])]
        stored_hashes = [str(item) for item in (checkpoint.source_hashes or [])]
        if not stored_ids or len(stored_ids) != len(stored_hashes):
            continue
        if not _same_prefix(stored_ids, message_ids) or not _same_prefix(stored_hashes, source_hashes):
            continue
        fence = metadata.get("active_leaf_id")
        if fence is not None and str(fence) != str(active_leaf_id):
            continue
        if len(stored_ids) > best_len:
            best, best_len = checkpoint, len(stored_ids)
    return best


async def load_context_source(
    *,
    conversation_id: str | None,
    temp_thread_id: str | None,
    user_id: str,
    project_id: str,
    session: AsyncSession | None = None,
    leaf_id: int | None = None,
) -> dict[str, Any]:
    """Load an immutable active source and project a matching summary+suffix."""
    if (conversation_id is None) == (temp_thread_id is None):
        raise ValueError("exactly one conversation_id or temp_thread_id is required")
    async with _session_scope(session) as db:
        active_leaf_id: str | None = None
        if conversation_id is not None:
            conversation = await cs._load_owned(db, conversation_id, user_id, project_id)
            rows = (
                (await db.execute(select(ChatMessage).where(ChatMessage.conversation_id == conversation_id)))
                .scalars()
                .all()
            )
            selected_leaf = leaf_id if leaf_id is not None else conversation.active_leaf_id
            path = cs._backtrack(list(rows), selected_leaf)
            raw_messages = [cs._msg_public(row) for row in path]
            active_leaf_id = str(selected_leaf) if selected_leaf is not None else None
        else:
            thread = (
                await db.execute(
                    select(ChatTempThread).where(
                        ChatTempThread.id == temp_thread_id,
                        ChatTempThread.user_id == user_id,
                        ChatTempThread.project_id == project_id,
                        ChatTempThread.expires_at > datetime.now(UTC),
                    )
                )
            ).scalar_one_or_none()
            if thread is None:
                raise LookupError("temporary chat thread was not found")
            raw_messages = _decode_temp_history(thread.history)
        messages = [_project_message(message) for message in raw_messages]
        message_ids = [
            str(message.get("id")) if message.get("id") is not None else f"temp:{temp_thread_id}:{index}"
            for index, message in enumerate(raw_messages)
        ]
        source_hashes = [_digest(message) for message in messages]
        checkpoint = await _checkpoint_for_source(
            db,
            conversation_id=conversation_id,
            temp_thread_id=temp_thread_id,
            user_id=user_id,
            project_id=project_id,
            message_ids=message_ids,
            source_hashes=source_hashes,
            active_leaf_id=active_leaf_id,
        )
        checkpoint_id = str(checkpoint.id) if checkpoint is not None else None
        revision = _source_revision(active_leaf_id, message_ids, source_hashes, checkpoint_id)
        checkpoint_data = None
        if checkpoint is not None:
            stored_ids = [str(item) for item in (checkpoint.source_message_ids or [])]
            prefix_len = len(stored_ids)
            try:
                summary = decrypt_chat_content(checkpoint.summary_ciphertext)
            except (TypeError, ValueError):
                summary = ""
            if not summary:
                checkpoint = None
                checkpoint_id = None
            else:
                suffix = messages[prefix_len:]
                instructions = [m for m in suffix if m.get("role") in {"system", "developer"}]
                body = [m for m in suffix if m.get("role") not in {"system", "developer"}]
                messages = [*instructions, {"role": "user", "content": f"[context summary-data]\n{summary}"}, *body]
                checkpoint_data = {
                    "id": checkpoint_id,
                    "source_message_ids": stored_ids,
                    "source_hashes": [str(item) for item in (checkpoint.source_hashes or [])],
                    "context_metadata": dict(checkpoint.context_metadata or {}),
                    "summary": summary,
                    "token_estimate": checkpoint.token_estimate,
                    "context_limit": checkpoint.context_limit,
                }
        revision = _source_revision(active_leaf_id, message_ids, source_hashes, checkpoint_id)
        return {
            "messages": messages,
            "message_ids": message_ids,
            "source_hashes": source_hashes,
            "active_leaf_id": active_leaf_id,
            "revision": revision,
            "checkpoint_id": checkpoint_id,
            "checkpoint": checkpoint_data,
        }


async def persist_context_checkpoint(
    session: AsyncSession,
    *,
    run: ChatRun,
    source: Mapping[str, Any],
    summary: str,
    source_message_ids: list[str] | tuple[str, ...],
    source_hashes: list[str] | tuple[str, ...],
    token_estimate: int,
    context_limit: int,
    context_metadata: Mapping[str, Any],
) -> ChatContextCheckpoint:
    """Persist one encrypted summary inside the caller's existing transaction.

    The caller owns parent/run locks and transaction boundaries; this helper only
    validates ownership/provenance, deduplicates an identical prefix, and adds a
    checkpoint row so title/context.updated can be committed atomically beside it.
    """
    if run.conversation_id is None and run.temp_thread_id is None:
        raise ValueError("checkpoint scope is missing")
    if run.conversation_id is not None and run.temp_thread_id is not None:
        raise ValueError("checkpoint scope is ambiguous")
    if run.temp_thread_id is not None:
        thread = (
            await session.execute(
                select(ChatTempThread).where(
                    ChatTempThread.id == run.temp_thread_id,
                    ChatTempThread.user_id == run.user_id,
                    ChatTempThread.project_id == run.project_id,
                    ChatTempThread.expires_at > datetime.now(UTC),
                )
            )
        ).scalar_one_or_none()
        if thread is None:
            raise LookupError("temporary chat thread has expired")
    ids = [str(item) for item in source_message_ids]
    hashes = [str(item) for item in source_hashes]
    if not ids or len(ids) != len(hashes) or not summary or len(summary) > 16_000:
        raise ValueError("checkpoint provenance is invalid")
    source_ids = [str(item) for item in source.get("message_ids", [])]
    source_hash_list = [str(item) for item in source.get("source_hashes", [])]
    if not _same_prefix(ids, source_ids) or not _same_prefix(hashes, source_hash_list):
        raise ValueError("checkpoint provenance does not match source")
    metadata = {key: value for key, value in dict(context_metadata).items() if key in _ALLOWED_METADATA}
    metadata.setdefault("version", 1)
    metadata.setdefault("projection_version", 1)
    metadata.setdefault("scope", "conversation" if run.conversation_id is not None else "temp")
    existing = await _checkpoint_for_source(
        session,
        conversation_id=run.conversation_id,
        temp_thread_id=run.temp_thread_id,
        user_id=str(run.user_id),
        project_id=str(run.project_id),
        message_ids=ids,
        source_hashes=hashes,
        active_leaf_id=metadata.get("active_leaf_id"),
    )
    if existing is not None and [str(item) for item in (existing.source_message_ids or [])] == ids:
        return existing
    row = ChatContextCheckpoint(
        id=str(uuid.uuid4()),
        run_id=str(run.id),
        conversation_id=run.conversation_id,
        temp_thread_id=run.temp_thread_id,
        source_anchor_message_id=int(ids[-1]) if ids[-1].isdigit() else None,
        source_hashes=hashes,
        source_message_ids=ids,
        source_message_count=len(ids),
        previous_checkpoint_id=(str(existing.id) if existing is not None else None),
        context_metadata=metadata,
        summary_ciphertext=encrypt_chat_content(summary),
        token_estimate=max(0, int(token_estimate)),
        context_limit=max(0, int(context_limit)),
    )
    session.add(row)
    await session.flush()
    return row
