"""Detect and repair additive drift between the ORM schema and the migrated database.

``lumen-migrate --apply`` is the bootstrap step that runs before every API, worker and controller start. After the
checksum-verified ledger is applied it compares the ORM metadata with the live schema and repairs what is purely
additive and safe to recreate:

* a missing table is created from the ORM definition (empty: its rows are already gone);
* a missing nullable or server-defaulted column without a foreign key is added;
* a missing index or unique constraint is created. Unique additions are pre-checked for duplicate keys first, so
  nothing is deleted and a database that violates the constraint is rejected before any DDL.

Everything else fails closed before the first DDL statement with every reason listed: a missing column that cannot be
added safely (NOT NULL without a server default, primary key, foreign key), and any missing table or column of the
credential-bearing provider-account tables, whose exact states are owned by ``provider_account_migration``. Column
types, nullability of existing columns and extra database objects are deliberately not compared: spelling differs
between servers and an automatic type change could lose data.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import Column, MetaData, Table, UniqueConstraint
from sqlalchemy.schema import CreateColumn

logger = logging.getLogger(__name__)


class SchemaRepairError(RuntimeError):
    """The schema differs from the ORM in a way that must not be repaired automatically."""


@dataclass(frozen=True)
class MissingIndex:
    table: str
    name: str
    unique: bool
    columns: tuple[str, ...]

    def ddl(self) -> str:
        columns = ", ".join(f"`{column}`" for column in self.columns)
        kind = "UNIQUE INDEX" if self.unique else "INDEX"
        return f"ALTER TABLE `{self.table}` ADD {kind} IF NOT EXISTS `{self.name}` ({columns})"

    def duplicate_probe(self) -> str:
        columns = ", ".join(f"`{column}`" for column in self.columns)
        not_null = " AND ".join(f"`{column}` IS NOT NULL" for column in self.columns)
        return f"SELECT 1 FROM `{self.table}` WHERE {not_null} GROUP BY {columns} HAVING COUNT(*) > 1 LIMIT 1"


@dataclass
class Drift:
    tables: list[Table] = field(default_factory=list)
    columns: list[tuple[Table, Column]] = field(default_factory=list)
    indexes: list[MissingIndex] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.tables or self.columns or self.indexes or self.problems)

    def describe(self) -> list[str]:
        items = [f"create table {table.name}" for table in self.tables]
        items += [f"add column {table.name}.{column.name}" for table, column in self.columns]
        items += [f"add {'unique ' if i.unique else ''}index {i.table}.{i.name} ({', '.join(i.columns)})" for i in self.indexes]
        return items


def _protected_tables() -> frozenset[str]:
    from lumen.scripts.provider_account_migration import REPAIRED_SCHEMA

    return frozenset(REPAIRED_SCHEMA)


def _orm_metadata() -> MetaData:
    import lumen.models  # noqa: F401  (registers every table on the shared metadata)
    from lumen.models.chat_db import Base

    return Base.metadata


def _wanted_indexes(table: Table) -> list[MissingIndex]:
    wanted: list[MissingIndex] = []
    for index in table.indexes:
        if len(index.expressions) != len(index.columns):
            raise SchemaRepairError(f"unsupported functional index {table.name}.{index.name}")
        wanted.append(MissingIndex(table.name, index.name, bool(index.unique), tuple(c.name for c in index.columns)))
    for constraint in table.constraints:
        if isinstance(constraint, UniqueConstraint):
            columns = tuple(column.name for column in constraint.columns)
            name = constraint.name or "uq_" + table.name + "_" + "_".join(columns)
            wanted.append(MissingIndex(table.name, name, True, columns))
    return wanted


async def _live_schema(connection):
    tables = set(
        (await connection.exec_driver_sql(
            "SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE()"
        )).scalars().all()
    )
    columns: dict[str, set[str]] = {}
    for table, column in (await connection.exec_driver_sql(
        "SELECT TABLE_NAME, COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE()"
    )).all():
        columns.setdefault(table, set()).add(column)
    grouped: dict[tuple[str, str], tuple[bool, list[str]]] = {}
    for table, name, non_unique, column in (await connection.exec_driver_sql(
        "SELECT TABLE_NAME, INDEX_NAME, NON_UNIQUE, COLUMN_NAME FROM information_schema.STATISTICS "
        "WHERE TABLE_SCHEMA = DATABASE() ORDER BY TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX"
    )).all():
        grouped.setdefault((table, name), (int(non_unique) == 0, []))[1].append(column)
    indexes: dict[str, list[tuple[bool, list[str]]]] = {}
    for (table, _name), entry in grouped.items():
        indexes.setdefault(table, []).append(entry)
    return tables, columns, indexes


def _column_problem(table: Table, column: Column) -> str | None:
    if column.primary_key or column.foreign_keys or column.autoincrement is True:
        return f"missing column {table.name}.{column.name} (key column; restore from backup)"
    if not column.nullable and column.server_default is None:
        return f"missing column {table.name}.{column.name} (NOT NULL without a server default)"
    return None


async def inspect_drift(connection, metadata: MetaData | None = None, protected: frozenset[str] | None = None) -> Drift:
    """Read only. Compare table, column-name and index presence with the ORM metadata."""
    metadata = metadata if metadata is not None else _orm_metadata()
    protected = _protected_tables() if protected is None else protected
    live_tables, live_columns, live_indexes = await _live_schema(connection)
    drift = Drift()
    for table in metadata.sorted_tables:
        if table.name not in live_tables:
            if table.name in protected:
                drift.problems.append(f"missing protected table {table.name} (credential schema, never recreated empty)")
            else:
                drift.tables.append(table)
            continue
        for column in table.columns:
            if column.name in live_columns.get(table.name, set()):
                continue
            problem = _column_problem(table, column)
            if problem is None and table.name in protected:
                problem = f"missing column {table.name}.{column.name} (protected credential table)"
            if problem is not None:
                drift.problems.append(problem)
            else:
                drift.columns.append((table, column))
        present = live_indexes.get(table.name, [])
        # A column that is missing and not addable cannot be indexed; its problem is already reported.
        usable = live_columns.get(table.name, set()) | {column.name for owner, column in drift.columns if owner is table}
        for wanted in _wanted_indexes(table):
            columns = list(wanted.columns)
            if wanted.unique:
                satisfied = any(unique and cols == columns for unique, cols in present)
            else:  # Any index that starts with the declared columns serves the lookup.
                satisfied = any(cols[: len(columns)] == columns for _unique, cols in present)
            if not satisfied and set(columns) <= usable:
                drift.indexes.append(wanted)
    return drift


async def plan_repair(engine, metadata: MetaData | None = None, protected: frozenset[str] | None = None) -> Drift:
    """Inspect and pre-check unique additions. Raises ``SchemaRepairError`` listing every blocking reason."""
    async with engine.connect() as connection:
        drift = await inspect_drift(connection, metadata, protected)
        added = {(owner.name, column.name) for owner, column in drift.columns}
        for index in drift.indexes:
            # Columns that are about to be added are NULL everywhere, so only existing data can collide.
            has_added_column = any((index.table, column) in added for column in index.columns)
            if index.unique and not has_added_column and (await connection.exec_driver_sql(index.duplicate_probe())).first():
                drift.problems.append(f"duplicate keys block unique index {index.table}.{index.name}")
    if drift.problems:
        raise SchemaRepairError("; ".join(drift.problems))
    return drift


async def describe_drift(engine, metadata: MetaData | None = None, protected: frozenset[str] | None = None) -> list[str]:
    """Dry run: what ``repair_schema`` would change. Raises when the drift is not repairable."""
    return (await plan_repair(engine, metadata, protected)).describe()


async def repair_schema(engine, metadata: MetaData | None = None, protected: frozenset[str] | None = None) -> list[str]:
    """Apply the additive repairs found by ``plan_repair``. Returns what was changed (empty when nothing drifted)."""
    metadata = metadata if metadata is not None else _orm_metadata()
    drift = await plan_repair(engine, metadata, protected)
    if drift.empty:
        return []
    async with engine.connect() as connection:
        if drift.tables:
            await connection.run_sync(lambda sync: metadata.create_all(sync, tables=drift.tables))
        for table, column in drift.columns:
            definition = str(CreateColumn(column).compile(dialect=connection.dialect))
            await connection.exec_driver_sql(f"ALTER TABLE `{table.name}` ADD COLUMN IF NOT EXISTS {definition}")
        for index in drift.indexes:
            await connection.exec_driver_sql(index.ddl())
        await connection.commit()
    changes = drift.describe()
    for change in changes:
        logger.warning("schema repair: %s", change)
    return changes
