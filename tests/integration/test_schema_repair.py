"""Bootstrap schema repair against a real MariaDB, using uniquely named synthetic tables."""

from __future__ import annotations

import os
from functools import partial
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import Column, ForeignKey, Index, Integer, MetaData, String, Table, UniqueConstraint, text
from sqlalchemy.ext.asyncio import create_async_engine

from lumen.scripts import migrate as runner
from lumen.scripts.schema_repair import (
    SchemaRepairError,
    describe_drift,
    inspect_drift,
    repair_schema,
)

pytestmark = pytest.mark.integration


class _Schema:
    def __init__(self, engine):
        self.engine = engine
        prefix = "zz_repair_" + uuid4().hex[:8] + "_"
        self.parent_name, self.child_name = prefix + "parent", prefix + "child"
        self.metadata = MetaData()
        self.parent = Table(
            self.parent_name, self.metadata,
            Column("id", Integer, primary_key=True),
            Column("code", String(40), nullable=False),
            Column("note", String(20), nullable=True),
            Column("alias", String(20), nullable=True),
            UniqueConstraint("code", name="uq_" + prefix + "code"),
            UniqueConstraint("alias", name="uq_" + prefix + "alias"),
            Index("idx_" + prefix + "note", "note"),
        )
        self.child = Table(
            self.child_name, self.metadata,
            Column("id", Integer, primary_key=True),
            Column("parent_id", Integer, ForeignKey(self.parent_name + ".id"), nullable=False),
            Column("kind", String(10), nullable=False, server_default="plain"),
            Column("extra", String(10), nullable=True),
            Index("idx_" + prefix + "child", "kind", "parent_id"),
        )
        self.prefix = prefix

    async def sql(self, statement, parameters=None):
        async with self.engine.begin() as connection:
            result = await connection.execute(text(statement), parameters or {})
            return result.all() if result.returns_rows else None

    async def create(self):
        async with self.engine.begin() as connection:
            await connection.run_sync(self.metadata.create_all)

    async def drift(self, protected=frozenset()):
        async with self.engine.connect() as connection:
            return await inspect_drift(connection, self.metadata, protected)

    async def repair(self, protected=frozenset()):
        return await repair_schema(self.engine, self.metadata, protected)


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


async def _index_names(schema, table):
    rows = await schema.sql("SELECT DISTINCT index_name FROM information_schema.statistics WHERE table_schema=DATABASE() AND table_name=:t", {"t": table})
    return {row[0] for row in rows}


async def test_a_database_that_matches_the_orm_is_left_untouched(schema):
    assert (await schema.drift()).empty
    assert await schema.repair() == []


async def test_missing_indexes_are_detected_then_recreated_and_keys_are_enforced(schema):
    await schema.sql(f"ALTER TABLE `{schema.parent_name}` DROP INDEX `idx_{schema.prefix}note`, DROP INDEX `uq_{schema.prefix}code`")
    await schema.sql(f"ALTER TABLE `{schema.child_name}` DROP INDEX `idx_{schema.prefix}child`")
    assert f"uq_{schema.prefix}code" not in await _index_names(schema, schema.parent_name)
    wanted = await describe_drift(schema.engine, schema.metadata, frozenset())
    assert sorted(wanted) == sorted([
        f"add index {schema.parent_name}.idx_{schema.prefix}note (note)",
        f"add unique index {schema.parent_name}.uq_{schema.prefix}code (code)",
        f"add index {schema.child_name}.idx_{schema.prefix}child (kind, parent_id)",
    ])
    assert f"uq_{schema.prefix}code" not in await _index_names(schema, schema.parent_name)  # dry run changed nothing
    assert sorted(await schema.repair()) == sorted(wanted)
    assert (await schema.drift()).empty
    await schema.sql(f"INSERT INTO `{schema.parent_name}` (id, code) VALUES (1, 'a')")
    with pytest.raises(Exception, match="Duplicate entry"):
        await schema.sql(f"INSERT INTO `{schema.parent_name}` (id, code) VALUES (2, 'a')")
    assert await schema.repair() == []  # idempotent


async def test_a_missing_table_is_recreated_empty_with_its_indexes(schema):
    await schema.sql(f"DROP TABLE `{schema.child_name}`")
    assert await schema.repair() == [f"create table {schema.child_name}"]
    assert (await schema.drift()).empty
    assert f"idx_{schema.prefix}child" in await _index_names(schema, schema.child_name)
    assert await schema.sql(f"SELECT COUNT(*) FROM `{schema.child_name}`") == [(0,)]


async def test_missing_nullable_and_defaulted_columns_are_added_with_their_indexes(schema):
    await schema.sql(f"ALTER TABLE `{schema.parent_name}` DROP COLUMN `note`")  # drops idx note with it
    await schema.sql(f"ALTER TABLE `{schema.child_name}` DROP COLUMN `extra`, DROP INDEX `idx_{schema.prefix}child`, DROP COLUMN `kind`")
    await schema.sql(f"INSERT INTO `{schema.parent_name}` (id, code) VALUES (1, 'kept')")
    await schema.sql(f"INSERT INTO `{schema.child_name}` (id, parent_id) VALUES (1, 1)")
    changes = await schema.repair()
    assert f"add column {schema.parent_name}.note" in changes
    assert f"add column {schema.child_name}.kind" in changes
    assert f"add column {schema.child_name}.extra" in changes
    assert (await schema.drift()).empty
    assert await schema.sql(f"SELECT kind FROM `{schema.child_name}`") == [("plain",)]  # server default fills old rows
    assert await schema.sql(f"SELECT code FROM `{schema.parent_name}`") == [("kept",)]


async def test_a_column_that_cannot_be_added_safely_fails_before_any_ddl(schema):
    await schema.sql(f"DROP TABLE `{schema.child_name}`")
    await schema.sql(f"ALTER TABLE `{schema.parent_name}` DROP COLUMN `code`")  # NOT NULL, no server default
    with pytest.raises(SchemaRepairError, match=f"missing column {schema.parent_name}.code"):
        await schema.repair()
    async with schema.engine.connect() as connection:
        tables = (await connection.exec_driver_sql("SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE()")).scalars().all()
    assert schema.child_name not in tables  # the recreatable table was not created: all-or-nothing


async def test_duplicate_keys_block_a_unique_index_and_no_row_is_touched(schema):
    await schema.sql(f"ALTER TABLE `{schema.parent_name}` DROP INDEX `uq_{schema.prefix}code`")
    await schema.sql(f"INSERT INTO `{schema.parent_name}` (id, code) VALUES (1, 'dup'), (2, 'dup'), (3, 'solo')")
    with pytest.raises(SchemaRepairError, match=f"duplicate keys block unique index {schema.parent_name}.uq_{schema.prefix}code"):
        await schema.repair()
    assert await schema.sql(f"SELECT COUNT(*) FROM `{schema.parent_name}`") == [(3,)]
    assert f"uq_{schema.prefix}code" not in await _index_names(schema, schema.parent_name)


async def test_null_keys_do_not_count_as_duplicates(schema):
    await schema.sql(f"ALTER TABLE `{schema.parent_name}` DROP INDEX `uq_{schema.prefix}alias`")
    await schema.sql(f"INSERT INTO `{schema.parent_name}` (id, code, alias) VALUES (1, 'x', NULL), (2, 'y', NULL)")
    assert f"add unique index {schema.parent_name}.uq_{schema.prefix}alias (alias)" in await schema.repair()


async def test_protected_tables_are_never_recreated_or_patched(schema):
    protected = frozenset({schema.child_name, schema.parent_name})
    await schema.sql(f"DROP TABLE `{schema.child_name}`")
    with pytest.raises(SchemaRepairError, match=f"missing protected table {schema.child_name}"):
        await schema.repair(protected)
    await schema.sql(f"ALTER TABLE `{schema.parent_name}` DROP COLUMN `note`")
    with pytest.raises(SchemaRepairError, match=f"missing column {schema.parent_name}.note"):
        await schema.repair(protected)
    # Indexes on a protected table are still repaired (they carry no data).
    await schema.sql(f"ALTER TABLE `{schema.parent_name}` ADD COLUMN `note` VARCHAR(20) NULL, DROP INDEX `uq_{schema.prefix}alias`")
    await schema.sql(f"CREATE TABLE `{schema.child_name}` (id INT PRIMARY KEY, parent_id INT NOT NULL, kind VARCHAR(10) NOT NULL DEFAULT 'plain', extra VARCHAR(10) NULL, KEY `idx_{schema.prefix}child` (parent_id, kind))")
    await schema.sql(f"ALTER TABLE `{schema.parent_name}` ADD INDEX `idx_{schema.prefix}note` (note)")
    assert any("uq_" in change for change in await schema.repair(protected))


async def test_extra_database_objects_and_type_spelling_are_ignored(schema):
    await schema.sql(f"ALTER TABLE `{schema.parent_name}` ADD COLUMN `legacy` TEXT NULL, ADD INDEX `idx_{schema.prefix}legacy` (`legacy`(20)), MODIFY `note` VARCHAR(30) NULL")
    assert (await schema.drift()).empty


async def test_runner_dry_run_reports_and_apply_repairs_unless_disabled(schema, monkeypatch):
    monkeypatch.setattr(runner, "backfill_history", AsyncMock())
    monkeypatch.setattr(runner, "count_unready", AsyncMock(return_value=0))
    monkeypatch.setattr(runner, "_verify_active_paths", AsyncMock())
    monkeypatch.setattr(runner, "describe_drift", partial(describe_drift, metadata=schema.metadata, protected=frozenset()))
    monkeypatch.setattr(runner, "repair_schema", partial(repair_schema, metadata=schema.metadata, protected=frozenset()))
    url = os.environ["DATABASE_URL"]
    index = f"idx_{schema.prefix}note"
    await schema.sql(f"ALTER TABLE `{schema.parent_name}` DROP INDEX `{index}`")

    pending, _ = await runner.migrate(url, apply=False)
    assert pending == [f"schema-repair: add index {schema.parent_name}.{index} (note)"]
    assert index not in await _index_names(schema, schema.parent_name)

    await runner.migrate(url, apply=True, repair_schema_drift=False)
    assert index not in await _index_names(schema, schema.parent_name)

    await runner.migrate(url, apply=True)  # also proves the 028 exact-state check accepts the post-028 indexes
    assert index in await _index_names(schema, schema.parent_name)
    assert (await runner.migrate(url, apply=False))[0] == []


async def test_runner_rejects_unrepairable_drift_without_recording_anything(schema, monkeypatch):
    monkeypatch.setattr(runner, "backfill_history", AsyncMock())
    monkeypatch.setattr(runner, "count_unready", AsyncMock(return_value=0))
    monkeypatch.setattr(runner, "_verify_active_paths", AsyncMock())
    monkeypatch.setattr(runner, "describe_drift", partial(describe_drift, metadata=schema.metadata, protected=frozenset()))
    monkeypatch.setattr(runner, "repair_schema", partial(repair_schema, metadata=schema.metadata, protected=frozenset()))
    await schema.sql(f"ALTER TABLE `{schema.parent_name}` DROP COLUMN `code`")
    ledger = await schema.sql("SELECT COUNT(*) FROM schema_migrations")
    for apply in (False, True):
        with pytest.raises(runner.MigrationLedgerError, match="cannot be repaired automatically"):
            await runner.migrate(os.environ["DATABASE_URL"], apply=apply)
    assert await schema.sql("SELECT COUNT(*) FROM schema_migrations") == ledger
