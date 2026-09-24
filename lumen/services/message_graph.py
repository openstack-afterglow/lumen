"""Transactional helpers for the indexed active conversation path.

The immutable ``chat_messages`` rows remain the graph source of truth.  Writers
must lock the owning ``ChatConversation`` row before calling these helpers so a
projection update is atomic with ``active_leaf_id``.
"""

from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen.models import ChatConversationActivePath, ChatMessage


class MessageGraphError(ValueError):
    """The requested leaf does not form a valid path in its conversation."""


async def ancestor_message_ids(
    session: AsyncSession,
    *,
    conversation_id: str,
    leaf_id: int | None,
) -> list[int]:
    """Return root-to-leaf ids, rejecting missing parents, cross-session links, and cycles."""
    if leaf_id is None:
        return []

    reversed_ids: list[int] = []
    seen: set[int] = set()
    current_id: int | None = leaf_id
    while current_id is not None:
        if current_id in seen:
            raise MessageGraphError("message ancestry contains a cycle")
        seen.add(current_id)
        row = (
            await session.execute(
                select(ChatMessage.id, ChatMessage.parent_id, ChatMessage.conversation_id).where(
                    ChatMessage.id == current_id
                )
            )
        ).one_or_none()
        if row is None:
            raise MessageGraphError("message ancestry contains a missing parent")
        if row.conversation_id != conversation_id:
            raise MessageGraphError("message ancestry crosses conversations")
        reversed_ids.append(int(row.id))
        current_id = row.parent_id

    reversed_ids.reverse()
    return reversed_ids


async def projected_message_ids(
    session: AsyncSession,
    *,
    conversation_id: str,
) -> list[int]:
    rows = await session.execute(
        select(ChatConversationActivePath.message_id)
        .where(ChatConversationActivePath.conversation_id == conversation_id)
        .order_by(ChatConversationActivePath.position.asc())
    )
    return [int(message_id) for message_id in rows.scalars().all()]


async def replace_active_path(
    session: AsyncSession,
    *,
    conversation_id: str,
    leaf_id: int | None,
) -> list[int]:
    """Replace only the divergent projection suffix and return the full new path."""
    target_ids = await ancestor_message_ids(
        session,
        conversation_id=conversation_id,
        leaf_id=leaf_id,
    )
    current_ids = await projected_message_ids(session, conversation_id=conversation_id)

    shared = 0
    shared_limit = min(len(current_ids), len(target_ids))
    while shared < shared_limit and current_ids[shared] == target_ids[shared]:
        shared += 1

    if shared < len(current_ids):
        await session.execute(
            delete(ChatConversationActivePath).where(
                ChatConversationActivePath.conversation_id == conversation_id,
                ChatConversationActivePath.position >= shared,
            )
        )
    for position, message_id in enumerate(target_ids[shared:], start=shared):
        session.add(
            ChatConversationActivePath(
                conversation_id=conversation_id,
                position=position,
                message_id=message_id,
            )
        )
    return target_ids


async def append_active_message(
    session: AsyncSession,
    *,
    conversation_id: str,
    parent_id: int | None,
    message_id: int,
) -> None:
    """Append one message when its parent is the current projected leaf.

    The caller owns the conversation lock and has already validated that the
    conversation's ``active_leaf_id`` equals ``parent_id``.
    """
    terminal = (
        await session.execute(
            select(
                ChatConversationActivePath.position,
                ChatConversationActivePath.message_id,
            )
            .where(ChatConversationActivePath.conversation_id == conversation_id)
            .order_by(ChatConversationActivePath.position.desc())
            .limit(1)
        )
    ).one_or_none()
    if parent_id is None:
        if terminal is not None:
            raise MessageGraphError("root append requires an empty active path")
        position = 0
    else:
        if terminal is None or int(terminal.message_id) != parent_id:
            raise MessageGraphError("append parent is not the projected active leaf")
        position = int(terminal.position) + 1
    session.add(
        ChatConversationActivePath(
            conversation_id=conversation_id,
            position=position,
            message_id=message_id,
        )
    )
