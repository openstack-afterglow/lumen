"""Lumen Redis client lifecycle."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any

import redis.asyncio as aioredis
from redis.asyncio.connection import SSLConnection, parse_url
from redis.asyncio.retry import Retry
from redis.asyncio.sentinel import Sentinel
from redis.backoff import NoBackoff

from lumen.config import get_settings

logger = logging.getLogger(__name__)

_client: aioredis.Redis | None = None
_sentinel: Sentinel | None = None

ttl_fast = 60
ttl_normal = 300
ttl_slow = 1800
ttl_static = 86400


def _get_client() -> aioredis.Redis:
    global _client, _sentinel
    if _client is None:
        settings = get_settings()
        if settings.redis_sentinel_addresses:
            options = dict(parse_url(settings.redis_url))
            # Keep URL credentials/DB/options, never its seed or connection class.
            options.pop("host", None)
            options.pop("port", None)
            if options.pop("connection_class", None) is SSLConnection:
                options["ssl"] = True
            options.setdefault("encoding", "utf-8")
            options.setdefault("decode_responses", False)
            options.setdefault("socket_connect_timeout", 3)
            options.setdefault("socket_timeout", 5)
            options.setdefault("health_check_interval", 30)
            # A lost command response is uncertain, not permission to replay it.
            options["retry"] = Retry(NoBackoff(), 0)
            sentinel_options = {
                name: value for name, value in options.items() if name.startswith(("socket_", "ssl"))
            }
            sentinel_options["retry"] = Retry(NoBackoff(), 0)
            sentinel_options["username"] = settings.redis_sentinel_username or None
            sentinel_options["password"] = settings.redis_sentinel_password.get_secret_value() or None
            sentinel = Sentinel(settings.redis_sentinel_addresses, sentinel_kwargs=sentinel_options)
            client = sentinel.master_for(settings.redis_sentinel_master, **options)
            # Construction has no awaits or network I/O: sync consumers share the
            # same atomic event-loop-local publication as async consumers.
            _client, _sentinel = client, sentinel
        else:
            _client = aioredis.from_url(
                settings.redis_url,
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
    global _client, _sentinel
    client, sentinel = _client, _sentinel
    _client, _sentinel = None, None
    owned = ([client] if client is not None else []) + (sentinel.sentinels if sentinel is not None else [])
    # redis-py 5 has no Sentinel close method; own every discovery pool too.
    results = await asyncio.gather(
        *(item.close(close_connection_pool=True) for item in owned), return_exceptions=True,
    )
    for result in results:
        if isinstance(result, BaseException):
            raise result
