"""035 ownership enforcement on real migrated MariaDB, using synthetic rows only."""
from __future__ import annotations

import os

import pytest
from sqlalchemy import insert, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from lumen.models.chat_db import ChatMcpServer
from lumen.scripts.migrate import MIGRATIONS, _statements, load_manifest, migrate

pytestmark = pytest.mark.integration
CHECK = "ck_chat_mcp_servers_domain_owner"


@pytest.fixture
async def connection():
    engine = create_async_engine(os.environ["DATABASE_URL"])
    try:
        async with engine.connect() as connection, connection.begin():
            yield connection
            await connection.rollback()
    finally:
        await engine.dispose()


@pytest.mark.parametrize("scope,domain", [("domain", None), ("domain", "synthetic-domain"),
                                         ("global", None), ("user", None)])
async def test_database_enforces_domain_owner_on_insert(connection, scope, domain):
    statement = insert(ChatMcpServer).values(scope=scope, owner_domain_id=domain, name="035 synthetic")
    if scope == "domain" and domain is None:
        with pytest.raises(DBAPIError, match=CHECK):
            await connection.execute(statement)
    else:
        result = await connection.execute(statement)
        identity = result.inserted_primary_key[0]
        row = (await connection.execute(select(ChatMcpServer.scope, ChatMcpServer.owner_domain_id)
                                        .where(ChatMcpServer.id == identity))).one()
        assert tuple(row) == (scope, domain)


@pytest.mark.parametrize("scope", ["domain", "global", "user"])
async def test_database_rejects_removing_or_bypassing_domain_owner(connection, scope):
    result = await connection.execute(insert(ChatMcpServer).values(
        scope=scope, owner_domain_id="synthetic-domain" if scope == "domain" else None, name="035 update",
    ))
    identity = result.inserted_primary_key[0]
    with pytest.raises(DBAPIError, match=CHECK):
        await connection.execute(update(ChatMcpServer).where(ChatMcpServer.id == identity)
                                 .values(scope="domain", owner_domain_id=None))
    row = (await connection.execute(select(ChatMcpServer.scope, ChatMcpServer.owner_domain_id)
                                    .where(ChatMcpServer.id == identity))).one()
    assert tuple(row) == (scope, "synthetic-domain" if scope == "domain" else None)
    await connection.execute(update(ChatMcpServer).where(ChatMcpServer.id == identity).values(name="035 allowed update"))
    assert (await connection.execute(select(ChatMcpServer.name).where(ChatMcpServer.id == identity))).scalar_one() == (
        "035 allowed update"
    )


async def test_migration_rejects_existing_violations_without_rewriting_rows(connection):
    # A connection-local temporary table reproduces the pre-035 schema without touching the shared migrated table.
    table = "synthetic_mcp_owner_035"
    await connection.exec_driver_sql(f"CREATE TEMPORARY TABLE {table} (scope VARCHAR(10), owner_domain_id VARCHAR(64))")
    try:
        await connection.exec_driver_sql(f"INSERT INTO {table} VALUES ('domain', NULL), ('global', NULL), ('user', NULL)")
        migration = next(item for item in load_manifest() if item.logical_id == "035-mcp-domain-owner-check")
        statement, = _statements(MIGRATIONS / migration.relative_path)
        with pytest.raises(DBAPIError, match=CHECK):
            await connection.exec_driver_sql(statement.replace("chat_mcp_servers", table, 1))
        assert (await connection.exec_driver_sql(f"SELECT scope, owner_domain_id FROM {table} ORDER BY scope")).all() == [
            ("domain", None), ("global", None), ("user", None),
        ]
    finally:
        await connection.exec_driver_sql(f"DROP TEMPORARY TABLE {table}")


async def test_successful_migration_reapplication_uses_verified_ledger():
    pending, unready = await migrate(os.environ["DATABASE_URL"], apply=True, repair_schema_drift=False)
    assert pending == []
    assert unready == 0
