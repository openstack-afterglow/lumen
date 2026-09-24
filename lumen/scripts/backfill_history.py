"""Resumable active-history projection backfill for migration 012."""

from __future__ import annotations

import argparse
import asyncio
import logging

from lumen_plugin_api.database import DatabaseConfig
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from lumen.config import get_settings
from lumen.models import ChatConversation, ChatConversationActivePath
from lumen.plugins.registry import get_plugin
from lumen.services.message_graph import MessageGraphError, ancestor_message_ids

logger = logging.getLogger(__name__)


class HistoryBackfillError(RuntimeError):
    """A conversation graph cannot be projected safely."""


async def count_unready(engine: AsyncEngine) -> int:
    async with engine.connect() as connection:
        value = await connection.scalar(
            select(func.count()).select_from(ChatConversation).where(ChatConversation.history_index_ready.is_(False))
        )
        return int(value or 0)


async def backfill_history(
    engine: AsyncEngine,
    *,
    conversation_batch_size: int = 100,
    insert_batch_size: int = 1000,
) -> int:
    """Project unready conversations in keyset order and return the completed count."""
    if conversation_batch_size < 1:
        raise ValueError("conversation_batch_size must be positive")
    if not 1 <= insert_batch_size <= 1000:
        raise ValueError("insert_batch_size must be between 1 and 1000")

    completed = 0
    after_id = ""
    while True:
        async with engine.connect() as connection:
            conversation_ids = list(
                (
                    await connection.execute(
                        select(ChatConversation.id)
                        .where(
                            ChatConversation.history_index_ready.is_(False),
                            ChatConversation.id > after_id,
                        )
                        .order_by(ChatConversation.id.asc())
                        .limit(conversation_batch_size)
                    )
                ).scalars()
            )
        if not conversation_ids:
            return completed

        for conversation_id in conversation_ids:
            async with AsyncSession(engine, expire_on_commit=False) as session, session.begin():
                conversation = (
                    await session.execute(
                        select(ChatConversation).where(ChatConversation.id == conversation_id).with_for_update()
                    )
                ).scalar_one_or_none()
                if conversation is None or conversation.history_index_ready:
                    continue
                try:
                    message_ids = await ancestor_message_ids(
                        session,
                        conversation_id=conversation_id,
                        leaf_id=conversation.active_leaf_id,
                    )
                except MessageGraphError as exc:
                    logger.error("history projection failed for conversation_id=%s", conversation_id)
                    raise HistoryBackfillError(
                        f"history projection failed for conversation_id={conversation_id}"
                    ) from exc

                await session.execute(
                    delete(ChatConversationActivePath).where(
                        ChatConversationActivePath.conversation_id == conversation_id
                    )
                )
                for start in range(0, len(message_ids), insert_batch_size):
                    chunk = message_ids[start : start + insert_batch_size]
                    session.add_all(
                        ChatConversationActivePath(
                            conversation_id=conversation_id,
                            position=start + offset,
                            message_id=message_id,
                        )
                        for offset, message_id in enumerate(chunk)
                    )
                    await session.flush()
                conversation.history_index_ready = True
                await session.flush()
                completed += 1
        after_id = conversation_ids[-1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default="")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    database_url = args.database_url or get_settings().database_url
    if not database_url:
        raise SystemExit("database URL is required")

    async def run() -> tuple[int, int]:
        handle = get_plugin("database").open(DatabaseConfig(url=database_url))
        try:
            completed = await backfill_history(handle.engine) if args.apply else 0
            return completed, await count_unready(handle.engine)
        finally:
            await handle.close()

    completed, unready = asyncio.run(run())
    if unready:
        raise SystemExit(f"unready conversation histories: {unready}; backfilled: {completed}")


if __name__ == "__main__":  # pragma: no cover
    main()
