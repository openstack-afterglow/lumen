"""Lumen Redis client lifecycle."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any

import redis.asyncio as aioredis

from lumen.config import get_settings

logger = logging.getLogger(__name__)

_client: aioredis.Redis | None = None

ttl_fast = 60
ttl_normal = 300
ttl_slow = 1800
ttl_static = 86400


def _get_client() -> aioredis.Redis:
    global _client
    if _client is None:
        _client = aioredis.from_url(
            get_settings().redis_url,
            encoding="utf-8",
            decode_responses=False,
            socket_connect_timeout=3,
            socket_timeout=5,
            health_check_interval=30,
        )
    return _client


async def _get_redis() -> aioredis.Redis:
    return _get_client()


def _make_serializable(obj: Any) -> Any:
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if isinstance(obj, list):
        return [_make_serializable(item) for item in obj]
    if isinstance(obj, dict):
        return {k: _make_serializable(v) for k, v in obj.items()}
    return obj


async def cached_call(
    key: str,
    ttl: int,
    fn: Callable[[], Any],
    *,
    refresh: bool = False,
    enabled: bool = True,
) -> Any:
    if not enabled:
        if asyncio.iscoroutinefunction(fn):
            return await fn()
        return await asyncio.to_thread(fn)

    client = _get_client()

    if refresh:
        try:
            await client.delete(key)
        except Exception:
            pass

    try:
        raw = await client.get(key)
        if raw is not None:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            return json.loads(raw)
    except Exception as e:
        logger.warning("Cache get failed (%s): %s", key, e)

    if asyncio.iscoroutinefunction(fn):
        result = await fn()
    else:
        result = await asyncio.to_thread(fn)

    try:
        serializable = _make_serializable(result)
        payload = json.dumps(serializable, ensure_ascii=False)
        await client.setex(key, ttl, payload)
    except Exception as e:
        logger.warning("Cache set failed (%s): %s", key, e)

    return result


async def invalidate(pattern: str) -> None:
    try:
        client = _get_client()
        keys = []
        async for k in client.scan_iter(match=pattern):
            keys.append(k)
        if keys:
            await client.delete(*keys)
    except Exception as e:
        logger.warning("Cache invalidate failed (%s): %s", pattern, e)


async def close_cache() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
    _client = None
