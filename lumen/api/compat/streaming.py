"""Shared SSE iteration that keeps upstream reads alive across 15-second pings."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import anyio

from lumen.services.infrastructure.api_load import close_stream_iterator


async def _drain(pending: asyncio.Task, iterator: AsyncIterator[Any]) -> None:
    try:
        await pending
    except StopAsyncIteration:
        return
    async for _ in iterator:
        pass


async def events_with_ping(source: AsyncIterator[dict], *, ping_seconds: float = 15.0) -> AsyncIterator[dict | None]:
    """Yield native events and ``None`` pings without cancelling a pending upstream read."""
    iterator = source.__aiter__()
    pending: asyncio.Task | None = None
    try:
        while True:
            if pending is None:
                pending = asyncio.create_task(anext(iterator))
            try:
                item = await asyncio.wait_for(asyncio.shield(pending), timeout=ping_seconds)
            except TimeoutError:
                yield None
                continue
            except StopAsyncIteration:
                return
            pending = None
            yield item
    finally:
        # Disconnect stops delivery, not ownership of an already-started provider
        # read or its settlement. Keep the ASGI owner alive until both complete.
        with anyio.CancelScope(shield=True):
            try:
                if pending is not None:
                    await _drain(pending, iterator)
            finally:
                await close_stream_iterator(iterator)
