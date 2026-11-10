"""Detect and repair additive drift between the migrated schema snapshot and the live database.

``lumen-migrate --apply`` is the bootstrap step that runs before every API, worker and controller start. After the
checksum-verified ledger is applied it compares the live schema with ``data/schema_snapshot.sql`` and restores
what is purely additive. That snapshot is the ``SHOW CREATE TABLE`` output of a database produced by the ledger
migrations alone (``tests/integration/test_schema_index_drift.py`` fails when it stops matching), so a restored object
has exactly the ledger's types, defaults, precision, key and foreign-key names. It is never derived from the ORM, whose
``create_all`` loses SQL-only defaults and ``datetime(6)`` precision.

* a missing table is created from its snapshot DDL (empty: its rows are already gone);
* a missing column is added from its snapshot definition when it is nullable or has a default, and is not a key,
  foreign-key or check-constraint column;
* a missing index or unique key is created from its snapshot definition. Unique additions are pre-checked for
  duplicate keys, so nothing is deleted and a violating database is rejected before any DDL.

Everything else fails closed before the first DDL statement with every reason listed. Tables that hold secret or money
state (see ``sensitive_tables``) are never recreated or patched with columns: an empty replacement would silently reset
balances or drop credentials. Presence is compared by table, column and key name only. Types, existing columns'
nullability, foreign keys and database-only objects are deliberately not compared, because spelling differs between
servers and an automatic change could lose data.

Regenerate the snapshot after a migration changes a table:
``LUMEN_UPDATE_SCHEMA_SNAPSHOT=1 uv run lumen-test integration -k schema_snapshot``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

SNAPSHOT_PATH = Path(__file__).resolve().parents[1] / "data" / "schema_snapshot.sql"
LEDGER_TABLE = "schema_migrations"
SNAPSHOT_HEADER = (
    "-- Ledger-shaped schema: SHOW CREATE TABLE of a database produced by the migrations alone, normalized.\n"
    "-- Used by lumen/scripts/schema_repair.py to restore missing tables, columns and keys at bootstrap. Do not edit:\n"
    "-- tests/integration/test_schema_index_drift.py compares it with the ledger result. Regenerate with\n"
    "-- LUMEN_UPDATE_SCHEMA_SNAPSHOT=1 uv run lumen-test integration -k schema_snapshot\n"
)

# Column names that mark a table as holding secret or money state; table names for credit holds and cloud resources.
_SENSITIVE_COLUMN = re.compile(r"encrypt|ciphertext|secret|credential|credit|balance|raw_cost|key_hash", re.I)
_SENSITIVE_TABLE = re.compile(r"credential|secret|wallet|api_key|reservation|resource", re.I)
# Conversation history. The runner's own integrity checks (``_verify_active_paths``) reject conversations whose
# message membership or active-path rows are missing, and ``backfill_history`` only rebuilds conversations whose
# index is not yet ready, so recreating any of these empty would only move the failure to after the DDL.
_HISTORY_TABLES = frozenset(
    {
        "chat_conversations",
        "chat_messages",
        "chat_message_graphs",
        "chat_conversation_messages",
        "chat_conversation_active_path",
        "chat_message_assets",
    }
)


class SchemaRepairError(RuntimeError):
    """The schema differs from the snapshot in a way that must not be repaired automatically."""


@dataclass(frozen=True)
class TableShape:
    name: str
    ddl: str
    columns: dict[str, str]
    keys: dict[str, tuple[bool, str, tuple[str, ...]]]
    guarded: frozenset[str]
    body: tuple[str, ...]

    def key_ddl(self, name: str) -> str:
        unique, definition, _columns = self.keys[name]
        return f"ALTER TABLE `{self.name}` ADD " + definition.replace(
            "UNIQUE KEY" if unique else "KEY", ("UNIQUE KEY" if unique else "KEY") + " IF NOT EXISTS", 1
        )

    def duplicate_probe(self, name: str) -> str:
        _unique, definition, columns = self.keys[name]
        parts = re.findall(r"`([^`]+)`(?:\((\d+)\))?", definition[definition.index("(") :])
        expressions = [f"LEFT(`{column}`, {length})" if length else f"`{column}`" for column, length in parts]
        not_null = " AND ".join(f"`{column}` IS NOT NULL" for column in columns)
        return (
            f"SELECT 1 FROM `{self.name}` WHERE {not_null} GROUP BY {', '.join(expressions)} "
            "HAVING COUNT(*) > 1 LIMIT 1"
        )


def normalize_ddl(ddl: str) -> str:
    """Strip what varies per installation: counters, display widths, table default charset and collation."""
    lines = ddl.strip().splitlines()
    lines[-1] = re.sub(r"\s+AUTO_INCREMENT=\d+", "", lines[-1])
    lines[-1] = re.sub(r"\s+DEFAULT CHARSET=\w+", "", lines[-1])
    lines[-1] = re.sub(r"\s+COLLATE=\w+", "", lines[-1])
    return "\n".join(re.sub(r"\b(bigint|int|tinyint|smallint)\(\d+\)", r"\1", line) for line in lines).rstrip(";")


def shape_from_ddl(ddl: str) -> TableShape:
    ddl = normalize_ddl(ddl)
    lines = ddl.splitlines()
    name = re.match(r"CREATE TABLE `([^`]+)` \(", lines[0]).group(1)
    columns: dict[str, str] = {}
    keys: dict[str, tuple[bool, str, tuple[str, ...]]] = {}
    guarded: set[str] = set()
    body: list[str] = []
    for raw in lines[1:]:
        if raw.startswith(")"):
            break
        line = raw.strip().rstrip(",")
        body.append(line)
        if line.startswith("`"):
            columns[re.match(r"`([^`]+)`", line).group(1)] = line
        elif re.match(r"(?:UNIQUE )?KEY `", line):
            unique = line.startswith("UNIQUE")
            key_name = re.search(r"KEY `([^`]+)`", line).group(1)
            inner = line[line.index("(") :]
            keys[key_name] = (unique, line, tuple(re.findall(r"`([^`]+)`", inner)))
        elif line.startswith(("PRIMARY KEY", "CONSTRAINT")):
            guarded.update(re.findall(r"`([^`]+)`", line.split(" REFERENCES ")[0]))
    return TableShape(name, ddl, columns, keys, frozenset(guarded), tuple(sorted(body)))


def render_snapshot(ddls: dict[str, str]) -> str:
    parts = [SNAPSHOT_HEADER]
    for name in sorted(ddls):
        if name != LEDGER_TABLE:
            parts.append(normalize_ddl(ddls[name]) + ";\n")
    return "\n".join(parts)


def parse_snapshot(text: str) -> dict[str, TableShape]:
    shapes: dict[str, TableShape] = {}
    current: list[str] = []
    for line in text.splitlines():
        if not current and not line.startswith("CREATE TABLE"):
            continue
        current.append(line)
        if line.startswith(")"):
            shape = shape_from_ddl("\n".join(current))
            shapes[shape.name] = shape
            current = []
    return shapes


def load_snapshot(path: Path = SNAPSHOT_PATH) -> dict[str, TableShape]:
    return parse_snapshot(path.read_text(encoding="utf-8"))


def sensitive_tables(shapes: dict[str, TableShape]) -> frozenset[str]:
    """Tables that must never be recreated empty or patched with columns."""
    from lumen.scripts.provider_account_migration import REPAIRED_SCHEMA

    protected = set(REPAIRED_SCHEMA) | (_HISTORY_TABLES & set(shapes))
    for shape in shapes.values():
        if _SENSITIVE_TABLE.search(shape.name) or any(_SENSITIVE_COLUMN.search(column) for column in shape.columns):
            protected.add(shape.name)
    return frozenset(protected)


@dataclass
class Drift:
    tables: list[TableShape] = field(default_factory=list)
    columns: list[tuple[TableShape, str]] = field(default_factory=list)
    keys: list[tuple[TableShape, str]] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.tables or self.columns or self.keys or self.problems)

    def describe(self) -> list[str]:
        items = [f"create table {table.name}" for table in self.tables]
        items += [f"add column {table.name}.{column}" for table, column in self.columns]
        for table, name in self.keys:
            unique, _definition, columns = table.keys[name]
            items.append(f"add {'unique ' if unique else ''}index {table.name}.{name} ({', '.join(columns)})")
        return items


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
    keys: dict[str, set[str]] = {}
    for table, name in (await connection.exec_driver_sql(
        "SELECT DISTINCT TABLE_NAME, INDEX_NAME FROM information_schema.STATISTICS WHERE TABLE_SCHEMA = DATABASE()"
    )).all():
        keys.setdefault(table, set()).add(name)
    return tables, columns, keys


def _column_problem(shape: TableShape, column: str) -> str | None:
    definition = shape.columns[column]
    if column in shape.guarded:
        return f"missing column {shape.name}.{column} (key, foreign-key or check column; restore from backup)"
    if "NOT NULL" in definition and "DEFAULT" not in definition and "AUTO_INCREMENT" not in definition:
        return f"missing column {shape.name}.{column} (NOT NULL without a default)"
    return None


async def inspect_drift(
    connection, shapes: dict[str, TableShape] | None = None, protected: frozenset[str] | None = None
) -> Drift:
    """Read only. Compare table, column-name and key-name presence with the ledger-shaped snapshot."""
    shapes = load_snapshot() if shapes is None else shapes
    protected = sensitive_tables(shapes) if protected is None else protected
    live_tables, live_columns, live_keys = await _live_schema(connection)
    drift = Drift()
    for shape in shapes.values():
        if shape.name not in live_tables:
            if shape.name in protected:
                drift.problems.append(
                    f"missing protected table {shape.name} (secret, money or history state, never recreated empty)"
                )
            else:
                drift.tables.append(shape)
            continue
        present_columns = live_columns.get(shape.name, set())
        for column in shape.columns:
            if column in present_columns:
                continue
            problem = _column_problem(shape, column)
            if problem is None and shape.name in protected:
                problem = f"missing column {shape.name}.{column} (protected table)"
            if problem is not None:
                drift.problems.append(problem)
            else:
                drift.columns.append((shape, column))
        usable = present_columns | {column for owner, column in drift.columns if owner is shape}
        for name, (_unique, _definition, columns) in shape.keys.items():
            # A key on a column that is missing and not addable cannot be created; its problem is already reported.
            if name not in live_keys.get(shape.name, set()) and set(columns) <= usable:
                drift.keys.append((shape, name))
    return drift


async def plan_repair(
    engine, shapes: dict[str, TableShape] | None = None, protected: frozenset[str] | None = None
) -> Drift:
    """Inspect and pre-check unique additions. Raises ``SchemaRepairError`` listing every blocking reason."""
    async with engine.connect() as connection:
        drift = await inspect_drift(connection, shapes, protected)
        added = {(owner.name, column) for owner, column in drift.columns}
        for shape, name in drift.keys:
            unique, _definition, columns = shape.keys[name]
            has_added_column = any((shape.name, column) in added for column in columns)
            if unique and not has_added_column and (await connection.exec_driver_sql(shape.duplicate_probe(name))).first():
                drift.problems.append(f"duplicate keys block unique index {shape.name}.{name}")
    if drift.problems:
        raise SchemaRepairError("; ".join(drift.problems))
    return drift


async def describe_drift(
    engine, shapes: dict[str, TableShape] | None = None, protected: frozenset[str] | None = None
) -> list[str]:
    """Dry run: what ``repair_schema`` would change. Raises when the drift is not repairable."""
    return (await plan_repair(engine, shapes, protected)).describe()


def _literal(statement: str) -> str:
    return statement.replace("%", "%%")  # the driver applies %-formatting to every statement


async def repair_schema(
    engine, shapes: dict[str, TableShape] | None = None, protected: frozenset[str] | None = None
) -> list[str]:
    """Apply the additive repairs found by ``plan_repair``. Returns what was changed (empty when nothing drifted)."""
    drift = await plan_repair(engine, shapes, protected)
    if drift.empty:
        return []
    async with engine.connect() as connection:
        if drift.tables:
            await connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS=0")  # creation order between new tables is free
            try:
                for shape in drift.tables:
                    await connection.exec_driver_sql(_literal(shape.ddl))
            finally:
                await connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS=1")
        for shape, column in drift.columns:
            await connection.exec_driver_sql(
                _literal(f"ALTER TABLE `{shape.name}` ADD COLUMN IF NOT EXISTS {shape.columns[column]}")
            )
        for shape, name in drift.keys:
            await connection.exec_driver_sql(_literal(shape.key_ddl(name)))
        await connection.commit()
    changes = drift.describe()
    for change in changes:
        logger.warning("schema repair: %s", change)
    return changes
