"""Cache lifecycle against the installed Redis client API, without network I/O."""

from unittest.mock import AsyncMock

import pytest
import redis.asyncio as aioredis

from lumen import cache


@pytest.mark.asyncio
async def test_close_cache_disconnects_real_client_pool_and_clears_state(monkeypatch):
    client = aioredis.from_url("redis://127.0.0.1:6379/0")
    disconnect = AsyncMock()
    monkeypatch.setattr(client.connection_pool, "disconnect", disconnect)
    monkeypatch.setattr(cache, "_client", client)

    await cache.close_cache()

    disconnect.assert_awaited_once_with()
    assert cache._client is None

    await cache.close_cache()

    disconnect.assert_awaited_once_with()
    assert cache._client is None
