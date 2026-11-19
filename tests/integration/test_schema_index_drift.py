"""The ledger migrations alone must produce the schema the ORM and the repair snapshot describe.

``lumen-migrate --apply`` also repairs additive drift at bootstrap (``schema_repair``), which would hide a migration
that forgot an object; the integration layer therefore bootstraps with ``--no-schema-repair`` and these tests fail when
the ledger result differs from the ORM declarations or from ``data/schema_snapshot.sql``.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import UniqueConstraint
from sqlalchemy.ext.asyncio import create_async_engine

import lumen.models  # noqa: F401  (registers every table on the shared metadata)
from lumen.models.chat_db import Base
from lumen.scripts import schema_repair

pytestmark = pytest.mark.integration


async def _live_ddls(connection) -> dict[str, str]:
    names = (await connection.exec_driver_sql(
        "SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() ORDER BY TABLE_NAME"
    )).scalars().all()
    return {name: (await connection.exec_driver_sql(f"SHOW CREATE TABLE `{name}`")).one()[1] for name in names}


async def test_orm_declarations_exist_in_the_ledger_migrated_schema():
    engine = create_async_engine(os.environ["DATABASE_URL"])
    try:
        async with engine.connect() as connection:
            tables = set((await connection.exec_driver_sql(
                "SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE()")).scalars().all())
            columns: dict[str, set[str]] = {}
            for table, column in (await connection.exec_driver_sql(
                "SELECT TABLE_NAME, COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE()")).all():
                columns.setdefault(table, set()).add(column)
            grouped: dict[tuple[str, str], tuple[bool, list[str]]] = {}
            for table, name, non_unique, column in (await connection.exec_driver_sql(
                "SELECT TABLE_NAME, INDEX_NAME, NON_UNIQUE, COLUMN_NAME FROM information_schema.STATISTICS "
                "WHERE TABLE_SCHEMA = DATABASE() ORDER BY TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX")).all():
                grouped.setdefault((table, name), (int(non_unique) == 0, []))[1].append(column)
    finally:
        await engine.dispose()
    missing: list[str] = []
    for table in Base.metadata.sorted_tables:
        if table.name not in tables:
            missing.append(f"table {table.name}")
            continue
        missing += [f"column {table.name}.{c.name}" for c in table.columns if c.name not in columns[table.name]]
        present = [entry for (owner, _name), entry in grouped.items() if owner == table.name]
        wanted = [(i.name, bool(i.unique), [c.name for c in i.columns]) for i in table.indexes]
        wanted += [(c.name, True, [col.name for col in c.columns]) for c in table.constraints if isinstance(c, UniqueConstraint)]
        for name, unique, cols in wanted:
            if unique:
                ok = any(u and existing == cols for u, existing in present)
            else:  # any index that starts with the declared columns serves the lookup
                ok = any(existing[: len(cols)] == cols for _u, existing in present)
            if not ok:
                missing.append(f"index {table.name}.{name}")
    assert missing == [], "add a migration for the ORM change (the bootstrap repair is only a safety net)"


async def test_schema_snapshot_matches_the_ledger_result():
    engine = create_async_engine(os.environ["DATABASE_URL"])
    try:
        async with engine.connect() as connection:
            live = await _live_ddls(connection)
    finally:
        await engine.dispose()
    if os.environ.get("LUMEN_UPDATE_SCHEMA_SNAPSHOT") == "1":
        schema_repair.SNAPSHOT_PATH.write_text(schema_repair.render_snapshot(live), encoding="utf-8")
    snapshot = schema_repair.load_snapshot()
    expected = {name: schema_repair.shape_from_ddl(ddl) for name, ddl in live.items() if name != schema_repair.LEDGER_TABLE}
    assert sorted(snapshot) == sorted(expected), "regenerate: LUMEN_UPDATE_SCHEMA_SNAPSHOT=1 lumen-test integration -k schema_snapshot"
    stale = [name for name in expected if snapshot[name].body != expected[name].body]
    assert stale == [], "snapshot differs from the ledger result for " + ", ".join(stale) + " (regenerate it)"
