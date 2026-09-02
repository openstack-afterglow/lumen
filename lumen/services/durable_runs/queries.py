"""Owner-scoped durable run and event readers."""

import json
from typing import Any

from sqlalchemy import select

from lumen.crypto import decrypt_chat_content
from lumen.models.chat_contracts import ChatRunDescriptor, ChatRunEvent, ChatRunResponse
from lumen.models.chat_runs import ChatRun, ChatTempThread
from lumen.services.run_store import NONTERMINAL, RunStoreError, load_owned_run, replay_events

from .common import _factory, _now, descriptor
from .errors import DurableRunCursorExpired, DurableRunError, DurableRunNotFound


async def owned_run_response(*, run_id: str, project_id: str, user_id: str) -> ChatRunResponse:
    factory = _factory()
    async with factory() as session:
        try:
            run = await load_owned_run(session, run_id, user_id=user_id, project_id=project_id)
        except RunStoreError as exc:
            raise DurableRunNotFound(str(exc)) from exc
        return ChatRunResponse(
            run_id=run.id,
            status=run.status,
            conversation_id=run.conversation_id,
            temp_thread_id=run.temp_thread_id,
            effective_features=(run.capability_snapshot or {}).get("effective_features", {}),
            last_seq=run.last_seq,
            terminal=run.status not in NONTERMINAL,
        )


async def active_run_descriptors(*, conversation_id: str, project_id: str, user_id: str) -> list[ChatRunDescriptor]:
    factory = _factory()
    async with factory() as session:
        rows = (
            (
                await session.execute(
                    select(ChatRun)
                    .where(
                        ChatRun.conversation_id == conversation_id,
                        ChatRun.project_id == project_id,
                        ChatRun.user_id == user_id,
                        ChatRun.status.in_(NONTERMINAL),
                    )
                    .order_by(ChatRun.created_at.desc())
                )
            )
            .scalars()
            .all()
        )
        return [descriptor(row) for row in rows]


async def active_run_descriptors_for_owner(*, project_id: str, user_id: str) -> list[ChatRunDescriptor]:
    """Return the owner's server-authoritative nonterminal run snapshot."""

    factory = _factory()
    async with factory() as session:
        rows = (
            (
                await session.execute(
                    select(ChatRun)
                    .where(
                        ChatRun.project_id == project_id,
                        ChatRun.user_id == user_id,
                        ChatRun.status.in_(NONTERMINAL),
                    )
                    .order_by(ChatRun.created_at.desc())
                )
            )
            .scalars()
            .all()
        )
        return [descriptor(row) for row in rows]


async def owned_temp_thread(*, thread_id: str, project_id: str, user_id: str) -> dict[str, Any]:
    factory = _factory()
    async with factory() as session:
        row = (
            await session.execute(
                select(ChatTempThread).where(
                    ChatTempThread.id == thread_id,
                    ChatTempThread.project_id == project_id,
                    ChatTempThread.user_id == user_id,
                    ChatTempThread.expires_at > _now(),
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise DurableRunNotFound("temporary chat thread was not found")
        try:
            history = json.loads(decrypt_chat_content(row.history))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise DurableRunError("temporary chat history is invalid") from exc
        if not isinstance(history, list):
            raise DurableRunError("temporary chat history is invalid")
        latest = (
            await session.execute(
                select(ChatRun)
                .where(ChatRun.temp_thread_id == row.id, ChatRun.project_id == project_id, ChatRun.user_id == user_id)
                .order_by(ChatRun.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        return {
            "temp_thread_id": row.id,
            "public_history": history,
            "active_run": descriptor(latest).model_dump(mode="json")
            if latest and latest.status in NONTERMINAL
            else None,
            "latest_run": descriptor(latest).model_dump(mode="json") if latest else None,
        }


async def owned_events(
    *, run_id: str, project_id: str, user_id: str, after_seq: int
) -> tuple[list[ChatRunEvent], bool]:
    factory = _factory()
    async with factory() as session:
        try:
            run = await load_owned_run(session, run_id, user_id=user_id, project_id=project_id)
            if run.run_scope == "temp" and run.temp_thread_id is None:
                raise DurableRunCursorExpired("temporary chat event journal has expired")
            events = await replay_events(session, run, after_seq=after_seq)
        except RunStoreError as exc:
            if "cursor" in str(exc):
                raise DurableRunCursorExpired(str(exc)) from exc
            raise DurableRunNotFound(str(exc)) from exc
        return events, run.status not in NONTERMINAL
