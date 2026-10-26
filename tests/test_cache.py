"""Installed client factory, authority routing and owned-pool lifecycle."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from redis.asyncio.sentinel import MasterNotFoundError
from redis.exceptions import ConnectionError

from lumen import cache
from lumen.config import Settings


@pytest.fixture
def isolated_factory(monkeypatch):
    monkeypatch.setattr(cache, "_client", None)
    monkeypatch.setattr(cache, "_sentinel", None)


def sentinel_settings(**overrides):
    return Settings(
        _env_file=None,
        **{
            "redis_url": "redis://data-user:data-secret@old-seed.invalid:6379/8",
            "redis_sentinel_addresses": (("sentinel.invalid", 26379),),
            "redis_sentinel_master": "kolla",
            "redis_sentinel_username": "discovery-user",
            "redis_sentinel_password": "discovery-secret",
            **overrides,
        },
    )


def test_sentinel_password_is_redacted_in_settings_serialization():
    settings = sentinel_settings()
    assert "discovery-secret" not in settings.model_dump_json()
    assert "discovery-secret" not in repr(settings)


@pytest.mark.parametrize("overrides", [
    {"redis_sentinel_addresses": ()},
    {"redis_sentinel_master": ""},
    {"redis_sentinel_master": "bad master"},
    {"redis_sentinel_master": "x" * 256},
    {"redis_sentinel_addresses": (("sentinel.invalid", 0),)},
    {"redis_sentinel_addresses": (("sentinel.invalid", 65536),)},
    {"redis_sentinel_addresses": (("sentinel.invalid/path", 26379),)},
    {"redis_sentinel_addresses": (("host:26379", 26379),)},
    {"redis_sentinel_addresses": (("same", 26379), ("same", 26379))},
    {"redis_sentinel_addresses": tuple((f"host-{i}", 26379) for i in range(17))},
    {"redis_sentinel_username": "bad\nusername"},
    {"redis_url": "unix:///var/run/valkey.sock"},
    {"redis_url": "redis://localhost/8?socket_timeout=nan"},
    {"redis_url": "redis://localhost/8?socket_connect_timeout=inf"},
    {"redis_url": "redis://localhost/8?socket_timeout=0"},
    {"redis_url": "redis://localhost/8?socket_connect_timeout=31"},
    {"redis_url": "redis://localhost/8?connection_class=unsafe"},
])
def test_sentinel_settings_reject_incoherent_or_unbounded_configuration(overrides):
    with pytest.raises(ValidationError):
        sentinel_settings(**overrides)


@pytest.mark.asyncio
async def test_close_attempts_every_owned_pool_and_surfaces_failure(monkeypatch, isolated_factory):
    failure = RuntimeError("owned pool close failed")
    client = SimpleNamespace(close=AsyncMock(side_effect=failure))
    discoveries = [SimpleNamespace(close=AsyncMock()), SimpleNamespace(close=AsyncMock())]
    monkeypatch.setattr(cache, "_client", client)
    monkeypatch.setattr(cache, "_sentinel", SimpleNamespace(sentinels=discoveries))
    with pytest.raises(RuntimeError, match="owned pool close failed"):
        await cache.close_cache()
    for owned in [client, *discoveries]:
        owned.close.assert_awaited_once()
    assert cache._client is None and cache._sentinel is None


async def _read_command(reader):
    header = await reader.readline()
    if not header:
        return []
    count = int(header[1:])
    command = []
    for _ in range(count):
        size = int((await reader.readline())[1:])
        command.append(await reader.readexactly(size))
        await reader.readexactly(2)
    return command


@pytest.mark.asyncio
async def test_close_cache_causes_peer_observed_tcp_eof(monkeypatch, isolated_factory):
    eof = asyncio.Event()

    async def serve(reader, writer):
        try:
            while command := await _read_command(reader):
                writer.write(b"+PONG\r\n" if command[0] == b"PING" else b"+OK\r\n")
                await writer.drain()
            eof.set()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    address = server.sockets[0].getsockname()[:2]
    monkeypatch.setattr(cache, "get_settings", lambda: sentinel_settings())
    client = cache._get_client()
    monkeypatch.setattr(cache._sentinel, "discover_master", AsyncMock(return_value=address))
    try:
        assert await client.ping()
        await cache.close_cache()
        await asyncio.wait_for(eof.wait(), timeout=1)
        assert cache._client is None and cache._sentinel is None
        await cache.close_cache()
    finally:
        await cache.close_cache()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [None, b"-READONLY previous primary is now a replica\r\n"])
async def test_lost_or_readonly_command_is_not_replayed(monkeypatch, isolated_factory, response):
    commands = []
    writers = []

    async def serve(reader, writer):
        writers.append(writer)
        try:
            while command := await _read_command(reader):
                if command[0] == b"INCR":
                    commands.append(command)
                    if response is not None:
                        writer.write(response)
                        await writer.drain()
                    break
                writer.write(b"+OK\r\n")
                await writer.drain()
        except asyncio.IncompleteReadError:
            pass
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    address = server.sockets[0].getsockname()[:2]
    settings = sentinel_settings(redis_url="redis://ignored.invalid:6379/8?health_check_interval=0")
    monkeypatch.setattr(cache, "get_settings", lambda: settings)
    client = cache._get_client()
    discovery = AsyncMock(return_value=address)
    monkeypatch.setattr(cache._sentinel, "discover_master", discovery)
    try:
        with pytest.raises(ConnectionError):
            await asyncio.wait_for(client.execute_command("INCR", "uncertain-write"), timeout=2)
        assert commands == [[b"INCR", b"uncertain-write"]]
    finally:
        await cache.close_cache()
        server.close()
        await server.wait_closed()
        for writer in writers:
            writer.close()


@pytest.mark.asyncio
async def test_failed_discovery_never_falls_back_to_data_seed(monkeypatch, isolated_factory):
    monkeypatch.setattr(cache, "get_settings", lambda: sentinel_settings())
    client = cache._get_client()
    discovery = AsyncMock(side_effect=MasterNotFoundError("no authoritative primary"))
    monkeypatch.setattr(cache._sentinel, "discover_master", discovery)
    with pytest.raises(MasterNotFoundError, match="no authoritative primary"):
        await client.set("must-not-use-seed", "value")
    await cache.close_cache()
