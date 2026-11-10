"""The real ledger schema, damaged and repaired through the actual ``lumen-migrate`` CLI, equals the snapshot again."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from lumen.scripts import schema_repair

pytestmark = pytest.mark.integration


def _migrate(*args):
    result = subprocess.run(
        [sys.executable, "-m", "lumen.scripts.migrate", *args], env=dict(os.environ), capture_output=True, text=True
    )
    return result.returncode, result.stdout + result.stderr


async def _bodies(connection):
    names = (await connection.exec_driver_sql(
        "SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE()")).scalars().all()
    shapes = {}
    for name in names:
        if name != schema_repair.LEDGER_TABLE:
            shapes[name] = schema_repair.shape_from_ddl((await connection.exec_driver_sql(f"SHOW CREATE TABLE `{name}`")).one()[1])
    return shapes


async def _restore(connection, snapshot):
    """Put the shared integration database back from the snapshot, independent of the code under test."""
    live = await _bodies(connection)
    await connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS=0")
    for name, shape in snapshot.items():
        if name not in live:
            await connection.exec_driver_sql(shape.ddl.replace("%", "%%"))
            continue
        for column in shape.columns:
            if column not in live[name].columns:
                await connection.exec_driver_sql(f"ALTER TABLE `{name}` ADD COLUMN IF NOT EXISTS {shape.columns[column]}")
        for key in shape.keys:
            if key not in live[name].keys:
                await connection.exec_driver_sql(shape.key_ddl(key))
    await connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS=1")


async def test_damaged_real_schema_is_restored_exactly_by_the_cli():
    snapshot = schema_repair.load_snapshot()
    engine = create_async_engine(os.environ["DATABASE_URL"])
    try:
        async with engine.connect() as connection:
            referenced = set((await connection.exec_driver_sql(
                "SELECT DISTINCT REFERENCED_TABLE_NAME FROM information_schema.KEY_COLUMN_USAGE "
                "WHERE TABLE_SCHEMA = DATABASE() AND REFERENCED_TABLE_NAME IS NOT NULL")).scalars().all())
            before = await _bodies(connection)
        # Tables come back whole; one column is damaged on a table that stays, because columns are never patched
        # into protected tables (keys are: chat_agents and chat_runs lose keys below).
        column_table = "chat_gateway_device_grants"
        victims = sorted(schema_repair.RECREATABLE_EMPTY - referenced - {column_table})
        assert len(victims) == len(schema_repair.RECREATABLE_EMPTY) - 1  # real tables with defaults, datetime(6), FKs
        try:
            async with engine.begin() as connection:
                await connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS=0")
                for name in victims:
                    await connection.exec_driver_sql(f"DROP TABLE `{name}`")
                await connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS=1")
                await connection.exec_driver_sql(f"ALTER TABLE {column_table} DROP COLUMN denied_at")
                await connection.exec_driver_sql("ALTER TABLE chat_agents DROP INDEX idx_chat_agents_owner")
                await connection.exec_driver_sql("ALTER TABLE chat_runs DROP INDEX uq_chat_runs_idempotency")  # keys of sensitive tables
            code, output = _migrate()  # dry run: reports, changes nothing
            assert code != 0 and "schema-repair: create table" in output
            code, output = _migrate("--apply")
            assert code == 0, output
            assert f"schema repair: add column {column_table}.denied_at" in output
            assert "schema repair: add index chat_agents.idx_chat_agents_owner" in output
            assert all(f"schema repair: create table {name}" in output for name in victims)
            code, output = _migrate()
            assert code == 0, output  # no drift and nothing pending
            code, output = _migrate("--apply")
            assert code == 0 and "schema repair:" not in output  # idempotent
            async with engine.connect() as connection:
                after = await _bodies(connection)
            assert sorted(after) == sorted(before)
            assert [name for name in before if after[name].body != before[name].body] == []
            assert [name for name in victims if after[name].ddl != snapshot[name].ddl] == []  # exact, not merely equal bodies
        finally:
            async with engine.begin() as connection:
                await _restore(connection, snapshot)
    finally:
        await engine.dispose()


async def test_a_missing_sensitive_table_fails_the_cli_and_creates_nothing():
    snapshot = schema_repair.load_snapshot()
    assert "user_wallets" in schema_repair.protected_tables(snapshot)
    engine = create_async_engine(os.environ["DATABASE_URL"])
    try:
        async with engine.begin() as connection:
            await connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS=0")
            await connection.exec_driver_sql("DROP TABLE user_wallets")
            await connection.exec_driver_sql("DROP TABLE chat_scheduler_leases")
            await connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS=1")
        try:
            code, output = _migrate("--apply")
            assert code != 0 and "missing table user_wallets" in output
            async with engine.connect() as connection:
                tables = set((await connection.exec_driver_sql(
                    "SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE()")).scalars().all())
            assert "user_wallets" not in tables and "chat_scheduler_leases" not in tables  # all-or-nothing
        finally:
            async with engine.begin() as connection:
                await _restore(connection, snapshot)
    finally:
        await engine.dispose()
