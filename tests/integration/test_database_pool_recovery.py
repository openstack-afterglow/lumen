"""Real MariaDB/aiomysql pool recovery on the production uvloop event loop."""

from __future__ import annotations

import asyncio
import os

import pytest
import uvloop
from sqlalchemy import text

from lumen.db import check_db, close_db, get_session_factory, init_db

pytestmark = pytest.mark.integration


async def _idle_pooled_connection():
    """Use the single pooled connection once; it then idles in the pool."""
    async with get_session_factory()() as session:
        connection = await session.connection()
        connection_id = await connection.scalar(text("SELECT CONNECTION_ID()"))
        driver = (await connection.get_raw_connection()).driver_connection
    return connection_id, driver


async def _current_connection_id():
    async with get_session_factory()() as session:
        return await (await session.connection()).scalar(text("SELECT CONNECTION_ID()"))


def _on_uvloop(scenario):
    async def run():
        init_db(os.environ["DATABASE_URL"], pool_size=1, max_overflow=0)
        try:
            return await scenario()
        finally:
            await close_db()

    with asyncio.Runner(loop_factory=uvloop.new_event_loop) as runner:
        return runner.run(run())


def test_readiness_renews_a_pooled_connection_whose_transport_closed_while_idle():
    async def scenario():
        first_id, driver = await _idle_pooled_connection()
        # The same closed-transport state a peer reset leaves behind on an idle socket.
        driver._writer.transport.abort()
        await asyncio.sleep(0)
        ready = await check_db()
        return ready, first_id, await _current_connection_id()

    ready, first_id, renewed_id = _on_uvloop(scenario)
    assert ready is True
    assert renewed_id != first_id


def test_readiness_does_not_treat_unrelated_ping_runtime_errors_as_disconnects():
    async def scenario():
        _, driver = await _idle_pooled_connection()

        async def failing_ping(reconnect: bool = True) -> None:
            raise RuntimeError("unrelated driver failure")

        driver.ping = failing_ping
        return await check_db()

    assert _on_uvloop(scenario) is False
