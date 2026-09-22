"""Shared SSE iteration that keeps upstream reads alive across 15-second pings."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

_BACKGROUND_DRAINS: set[asyncio.Task] = set()


async def _drain(pending: asyncio.Task, iterator: AsyncIterator[Any]) -> None:
    try:
        await pending
        async for _ in iterator:
            pass
    except Exception:
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
        if pending is not None:
            drain = asyncio.create_task(_drain(pending, iterator))
            _BACKGROUND_DRAINS.add(drain)
            drain.add_done_callback(_BACKGROUND_DRAINS.discard)
