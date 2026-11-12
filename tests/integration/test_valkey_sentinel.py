"""Opt-in, destructive ONLY to an explicitly owned disposable Valkey topology.

Run inside its unique Docker network with LUMEN_TEST_OWNED_VALKEY_TOPOLOGY JSON:
{sentinel_host, sentinel_port, sentinel_password, data_hosts, data_port,
 data_username, data_password, master, tls_ca?}. Never point this at native data.
"""
from __future__ import annotations

import asyncio
import json
import os
import ssl
from urllib.parse import urlencode

import pytest
import redis.asyncio as redis
from redis.asyncio.sentinel import MasterNotFoundError
from redis.exceptions import AuthenticationError, ConnectionError, ReadOnlyError

from lumen import cache
from lumen.config import Settings

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.environ.get("LUMEN_TEST_OWNED_VALKEY_TOPOLOGY"), reason="requires explicitly owned disposable Valkey topology",
)]


async def _eventually(predicate, *, seconds=30):
    deadline = asyncio.get_running_loop().time() + seconds
    while True:
        try:
            result = await predicate()
            if result:
                return result
        except (ConnectionError, MasterNotFoundError):
            pass
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("owned Valkey topology did not reach expected state")
        await asyncio.sleep(0.1)


@pytest.mark.asyncio
async def test_actual_valkey_primary_switch_auth_no_seed_fallback_and_socket_closure(monkeypatch):
    topology = json.loads(os.environ["LUMEN_TEST_OWNED_VALKEY_TOPOLOGY"])
    hosts = topology["data_hosts"]
    assert len(hosts) == 2 and all(host.startswith("lumen-owned-") for host in hosts)
    assert topology["sentinel_host"].startswith("lumen-owned-")
    tls = bool(topology.get("tls_ca"))
    options = {"socket_timeout": 1, "socket_connect_timeout": 1, "health_check_interval": 0,
               "client_name": "lumen-owned-cache-primary"}
    if tls:
        options.update(ssl_ca_certs=topology["tls_ca"], ssl_check_hostname=True)
    url = (
        f"{'rediss' if tls else 'redis'}://{topology['data_username']}:{topology['data_password']}"
        f"@{hosts[0]}:{topology['data_port']}/8?{urlencode(options)}"
    )
    settings = Settings(redis_url=url, redis_sentinel_addresses=((topology["sentinel_host"], topology["sentinel_port"]),),
                        redis_sentinel_master=topology["master"], redis_sentinel_password=topology["sentinel_password"])
    monkeypatch.setattr(cache, "get_settings", lambda: settings)
    await cache.close_cache()
    admin_options = dict(options)
    admin_options.pop("client_name")
    if tls:
        admin_options["ssl"] = True
    admin_options["username"] = topology["data_username"]
    admin_options["password"] = topology["data_password"]
    admin_options["db"] = 8
    nodes = [redis.Redis(host=host, port=topology["data_port"], **admin_options) for host in hosts]
    sentinel_options = {key: value for key, value in admin_options.items() if key.startswith(("ssl", "socket_"))}
    sentinel_options["password"] = topology["sentinel_password"]
    sentinel = redis.Redis(host=topology["sentinel_host"], port=topology["sentinel_port"], **sentinel_options)
    bad_auth = redis.Redis(host=topology["sentinel_host"], port=topology["sentinel_port"],
                           **{**sentinel_options, "password": topology["data_password"]})
    # Hold one public single-connection client across failover so pool checkout
    # cannot proactively replace a closed idle socket before the old-primary test.
    client = cache._get_client().client()
    try:
        for node in nodes:
            info = await node.info("server")
            assert info.get("valkey_version", info.get("redis_version")) == "7.2.13"
        if tls:
            untrusted = redis.Redis(host=hosts[0], port=topology["data_port"], ssl=True,
                                    username=topology["data_username"], password=topology["data_password"],
                                    socket_connect_timeout=1, socket_timeout=1)
            try:
                with pytest.raises(ConnectionError) as denied:
                    await untrusted.ping()
                assert isinstance(denied.value.__context__, ssl.SSLCertVerificationError)
            finally:
                await untrusted.close(close_connection_pool=True)
        with pytest.raises(AuthenticationError):
            await bad_auth.ping()
        assert await client.set("lumen:owned:primary-switch", "before")
        assert await client.get("lumen:owned:primary-switch") == b"before"
        await _eventually(lambda: _replica_ready(nodes[1]))
        assert await sentinel.execute_command("SENTINEL", "FAILOVER", topology["master"])
        await _eventually(lambda: _is_primary(nodes[1]))
        await _eventually(lambda: _is_replica(nodes[0]))
        # The still-open old-primary connection must fail visibly, not replay.
        with pytest.raises(ConnectionError):
            await client.set("lumen:owned:old-primary-must-not-write", "value")
        # A new independent operation reconnects and discovers the new primary.
        assert await client.set("lumen:owned:primary-switch", "after")
        assert await nodes[1].get("lumen:owned:primary-switch") == b"after"
        assert client.connection_pool.master_address[0] == hosts[1]
        with pytest.raises(ReadOnlyError):
            await nodes[0].set("lumen:owned:direct-seed", "forbidden")
        data_clients = {row["id"] for row in await nodes[1].client_list()
                        if row.get("name") == "lumen-owned-cache-primary"}
        discovery_clients = {row["id"] for row in await sentinel.client_list()
                             if row.get("cmd") == "sentinel|masters"}
        assert data_clients and discovery_clients
        await cache.close_cache()
        remaining_data = {row["id"] for row in await nodes[1].client_list()}
        remaining_discovery = {row["id"] for row in await sentinel.client_list()}
        assert data_clients.isdisjoint(remaining_data)
        assert discovery_clients.isdisjoint(remaining_discovery)
        # Cold discovery denial must not fall back to the URL's healthy replica seed.
        invalid = settings.model_copy(update={"redis_sentinel_password": settings.redis_sentinel_password.__class__("wrong")})
        monkeypatch.setattr(cache, "get_settings", lambda: invalid)
        with pytest.raises(MasterNotFoundError):
            await cache._get_client().set("lumen:owned:no-fallback", "forbidden")
        assert await nodes[1].get("lumen:owned:no-fallback") is None
    finally:
        await cache.close_cache()
        await asyncio.gather(*(item.close(close_connection_pool=True) for item in [client, *nodes, sentinel, bad_auth]))


async def _replica_ready(node):
    info = await node.info("replication")
    return info["role"] == "slave" and info["master_link_status"] == "up"


async def _is_primary(node):
    return (await node.info("replication"))["role"] == "master"


async def _is_replica(node):
    return (await node.info("replication"))["role"] == "slave"
