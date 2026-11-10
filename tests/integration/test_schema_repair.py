"""Bootstrap schema repair against a real MariaDB, using uniquely named synthetic tables with SQL-only features."""

from __future__ import annotations

import os
from functools import partial
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from lumen.scripts import migrate as runner
from lumen.scripts.schema_repair import (
    SchemaRepairError,
    describe_drift,
    inspect_drift,
    repair_schema,
    shape_from_ddl,
)

pytestmark = pytest.mark.integration


class _Schema:
    """Parent and child tables written in plain SQL: defaults, datetime(6), named keys and a named foreign key."""

    def __init__(self, engine):
        self.engine = engine
        self.prefix = "zz_repair_" + uuid4().hex[:8] + "_"
        self.parent_name, self.child_name = self.prefix + "parent", self.prefix + "child"
        self.shapes = {}

    async def create(self):
        p = self.prefix
        await self.sql(f"""CREATE TABLE `{self.parent_name}` (
            id INT NOT NULL AUTO_INCREMENT, code VARCHAR(40) NOT NULL, note VARCHAR(20) DEFAULT NULL,
            alias VARCHAR(20) DEFAULT NULL, stamp DATETIME(6) NOT NULL DEFAULT current_timestamp(6),
            state VARCHAR(10) NOT NULL DEFAULT 'new',
            PRIMARY KEY (id), UNIQUE KEY uq_{p}code (code), UNIQUE KEY uq_{p}alias (alias), KEY idx_{p}note (note)
        ) ENGINE=InnoDB""")
        await self.sql(f"""CREATE TABLE `{self.child_name}` (
            id INT NOT NULL AUTO_INCREMENT, parent_id INT NOT NULL, kind VARCHAR(10) NOT NULL DEFAULT 'plain',
            extra VARCHAR(10) DEFAULT NULL, PRIMARY KEY (id), KEY idx_{p}child (kind, parent_id),
            CONSTRAINT fk_{p}child FOREIGN KEY (parent_id) REFERENCES `{self.parent_name}` (id)
        ) ENGINE=InnoDB""")
        for name in (self.parent_name, self.child_name):
            self.shapes[name] = shape_from_ddl((await self.sql(f"SHOW CREATE TABLE `{name}`"))[0][1])

    async def sql(self, statement, parameters=None):
        async with self.engine.begin() as connection:
            result = await connection.execute(text(statement), parameters or {})
            return result.all() if result.returns_rows else None

    async def shape(self, name):
        return shape_from_ddl((await self.sql(f"SHOW CREATE TABLE `{name}`"))[0][1])

    async def drift(self, protected=frozenset()):
        async with self.engine.connect() as connection:
            return await inspect_drift(connection, self.shapes, protected)

    async def repair(self, protected=frozenset()):
        return await repair_schema(self.engine, self.shapes, protected)

    async def keys(self, table):
        rows = await self.sql("SELECT DISTINCT index_name FROM information_schema.statistics WHERE table_schema=DATABASE() AND table_name=:t", {"t": table})
        return {row[0] for row in rows}


@pytest.fixture
async def schema():
    engine = create_async_engine(os.environ["DATABASE_URL"])
    schema = _Schema(engine)
    await schema.create()
    try:
        yield schema
    finally:
        async with engine.begin() as connection:
            await connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS=0")
            for name in (schema.child_name, schema.parent_name):
                await connection.exec_driver_sql(f"DROP TABLE IF EXISTS `{name}`")
            await connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS=1")
        await engine.dispose()


async def test_a_database_that_matches_the_snapshot_is_left_untouched(schema):
    assert (await schema.drift()).empty
    assert await schema.repair() == []


async def test_missing_keys_are_recreated_exactly_and_unique_keys_are_enforced(schema):
    p = schema.prefix
    await schema.sql(f"ALTER TABLE `{schema.parent_name}` DROP INDEX `idx_{p}note`, DROP INDEX `uq_{p}code`")
    await schema.sql(f"ALTER TABLE `{schema.child_name}` DROP INDEX `idx_{p}child`")  # the FK keeps its own index
    wanted = await describe_drift(schema.engine, schema.shapes, frozenset())
    assert sorted(wanted) == sorted([
        f"add index {schema.parent_name}.idx_{p}note (note)",
        f"add unique index {schema.parent_name}.uq_{p}code (code)",
        f"add index {schema.child_name}.idx_{p}child (kind, parent_id)",
    ])
    assert f"uq_{p}code" not in await schema.keys(schema.parent_name)  # the dry run changed nothing
    assert sorted(await schema.repair()) == sorted(wanted)
    for name in (schema.parent_name, schema.child_name):
        assert (await schema.shape(name)).body == schema.shapes[name].body
    await schema.sql(f"INSERT INTO `{schema.parent_name}` (code) VALUES ('a')")
    with pytest.raises(Exception, match="Duplicate entry"):
        await schema.sql(f"INSERT INTO `{schema.parent_name}` (code) VALUES ('a')")
    assert await schema.repair() == []  # idempotent


async def test_a_missing_table_is_recreated_empty_with_the_exact_ledger_ddl(schema):
    await schema.sql(f"DROP TABLE `{schema.child_name}`")
    assert await schema.repair() == [f"create table {schema.child_name}"]
    recreated = await schema.shape(schema.child_name)
    assert recreated.ddl == schema.shapes[schema.child_name].ddl  # defaults, key order and the FK name included
    assert await schema.sql(f"SELECT COUNT(*) FROM `{schema.child_name}`") == [(0,)]


async def test_missing_nullable_and_defaulted_columns_are_added_with_their_exact_definitions(schema):
    await schema.sql(f"ALTER TABLE `{schema.parent_name}` DROP COLUMN `note`, DROP COLUMN `stamp`, DROP COLUMN `state`")
    await schema.sql(f"ALTER TABLE `{schema.child_name}` DROP COLUMN `extra`, DROP INDEX `idx_{schema.prefix}child`, DROP COLUMN `kind`")
    await schema.sql(f"INSERT INTO `{schema.parent_name}` (id, code) VALUES (1, 'kept')")
    await schema.sql(f"INSERT INTO `{schema.child_name}` (id, parent_id) VALUES (1, 1)")
    changes = await schema.repair()
    for column in ("parent.note", "parent.stamp", "parent.state", "child.kind", "child.extra"):
        assert f"add column {schema.prefix}{column}" in changes
    for name in (schema.parent_name, schema.child_name):  # sorted body: ADD COLUMN appends, so order is not compared
        assert (await schema.shape(name)).body == schema.shapes[name].body
    assert await schema.sql(f"SELECT kind FROM `{schema.child_name}`") == [("plain",)]
    assert await schema.sql(f"SELECT state FROM `{schema.parent_name}`") == [("new",)]
    assert await schema.sql(f"SELECT code FROM `{schema.parent_name}`") == [("kept",)]


async def test_a_column_that_cannot_be_added_safely_fails_before_any_ddl(schema):
    await schema.sql(f"DROP TABLE `{schema.child_name}`")
    await schema.sql(f"ALTER TABLE `{schema.parent_name}` DROP COLUMN `code`")  # NOT NULL, no default
    with pytest.raises(SchemaRepairError, match=f"missing column {schema.parent_name}.code"):
        await schema.repair()
    tables = [row[0] for row in await schema.sql("SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE()")]
    assert schema.child_name not in tables  # the recreatable table was not created: all-or-nothing


async def test_duplicate_keys_block_a_unique_key_and_no_row_is_touched(schema):
    p = schema.prefix
    await schema.sql(f"ALTER TABLE `{schema.parent_name}` DROP INDEX `uq_{p}code`")
    await schema.sql(f"INSERT INTO `{schema.parent_name}` (id, code) VALUES (1, 'dup'), (2, 'dup'), (3, 'solo')")
    with pytest.raises(SchemaRepairError, match=f"duplicate keys block unique index {schema.parent_name}.uq_{p}code"):
        await schema.repair()
    assert await schema.sql(f"SELECT COUNT(*) FROM `{schema.parent_name}`") == [(3,)]
    assert f"uq_{p}code" not in await schema.keys(schema.parent_name)


async def test_null_keys_do_not_count_as_duplicates(schema):
    await schema.sql(f"ALTER TABLE `{schema.parent_name}` DROP INDEX `uq_{schema.prefix}alias`")
    await schema.sql(f"INSERT INTO `{schema.parent_name}` (id, code, alias) VALUES (1, 'x', NULL), (2, 'y', NULL)")
    assert f"add unique index {schema.parent_name}.uq_{schema.prefix}alias (alias)" in await schema.repair()


async def test_protected_tables_are_never_recreated_or_patched_but_their_keys_are_repaired(schema):
    protected = frozenset({schema.child_name, schema.parent_name})
    await schema.sql(f"DROP TABLE `{schema.child_name}`")
    with pytest.raises(SchemaRepairError, match=f"missing table {schema.child_name}"):
        await schema.repair(protected)
    await schema.sql(f"ALTER TABLE `{schema.parent_name}` DROP COLUMN `note`")
    with pytest.raises(SchemaRepairError, match=f"missing column {schema.parent_name}.note"):
        await schema.repair(protected)
    await schema.sql(f"CREATE TABLE `{schema.child_name}` (id INT NOT NULL AUTO_INCREMENT, parent_id INT NOT NULL, kind VARCHAR(10) NOT NULL DEFAULT 'plain', extra VARCHAR(10) DEFAULT NULL, PRIMARY KEY (id), KEY `idx_{schema.prefix}child` (kind, parent_id), CONSTRAINT `fk_{schema.prefix}child` FOREIGN KEY (parent_id) REFERENCES `{schema.parent_name}` (id)) ENGINE=InnoDB")
    await schema.sql(f"ALTER TABLE `{schema.parent_name}` ADD COLUMN `note` VARCHAR(20) DEFAULT NULL, DROP INDEX `uq_{schema.prefix}alias`")
    await schema.sql(f"ALTER TABLE `{schema.parent_name}` ADD KEY `idx_{schema.prefix}note` (note)")
    assert any("uq_" in change for change in await schema.repair(protected))


async def test_extra_database_objects_and_type_spelling_are_ignored(schema):
    await schema.sql(f"ALTER TABLE `{schema.parent_name}` ADD COLUMN `legacy` TEXT NULL, ADD INDEX `idx_{schema.prefix}legacy` (`legacy`(20)), MODIFY `note` VARCHAR(30) NULL")
    assert (await schema.drift()).empty


async def _runner_patched(schema, monkeypatch):
    monkeypatch.setattr(runner, "backfill_history", AsyncMock())
    monkeypatch.setattr(runner, "count_unready", AsyncMock(return_value=0))
    monkeypatch.setattr(runner, "_verify_active_paths", AsyncMock())
    monkeypatch.setattr(runner, "describe_drift", partial(describe_drift, shapes=schema.shapes, protected=frozenset()))
    monkeypatch.setattr(runner, "repair_schema", partial(repair_schema, shapes=schema.shapes, protected=frozenset()))


async def test_runner_dry_run_reports_and_apply_repairs_unless_disabled(schema, monkeypatch):
    await _runner_patched(schema, monkeypatch)
    url = os.environ["DATABASE_URL"]
    index = f"idx_{schema.prefix}note"
    await schema.sql(f"ALTER TABLE `{schema.parent_name}` DROP INDEX `{index}`")

    pending, _ = await runner.migrate(url, apply=False)
    assert pending == [f"schema-repair: add index {schema.parent_name}.{index} (note)"]
    assert index not in await schema.keys(schema.parent_name)

    await runner.migrate(url, apply=True, repair_schema_drift=False)
    assert index not in await schema.keys(schema.parent_name)

    await runner.migrate(url, apply=True)  # also proves the 028 exact-state check accepts the post-028 indexes
    assert index in await schema.keys(schema.parent_name)
    assert (await runner.migrate(url, apply=False))[0] == []


async def test_runner_rejects_unrepairable_drift_without_recording_anything(schema, monkeypatch):
    await _runner_patched(schema, monkeypatch)
    await schema.sql(f"ALTER TABLE `{schema.parent_name}` DROP COLUMN `code`")
    ledger = await schema.sql("SELECT COUNT(*) FROM schema_migrations")
    for apply in (False, True):
        with pytest.raises(runner.MigrationLedgerError, match="cannot be repaired automatically"):
            await runner.migrate(os.environ["DATABASE_URL"], apply=apply)
    assert await schema.sql("SELECT COUNT(*) FROM schema_migrations") == ledger
