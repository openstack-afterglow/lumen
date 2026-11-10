"""Every table, column and index the ORM declares must exist after the ledger migrations alone.

The baseline migration was generated from ``CREATE TABLE``, so ORM ``Index(...)`` objects on baseline tables never
reached a migration until 029 and 030 caught them up. ``lumen-migrate --apply`` also repairs such drift at bootstrap
(``schema_repair``), which would hide a migration that forgot a new ORM index; the integration layer therefore
bootstraps with ``--no-schema-repair`` and this test fails when the ledger migrations leave any drift behind.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from lumen.scripts.schema_repair import inspect_drift

pytestmark = pytest.mark.integration


async def test_ledger_migrations_leave_no_orm_drift():
    engine = create_async_engine(os.environ["DATABASE_URL"])
    try:
        async with engine.connect() as connection:
            drift = await inspect_drift(connection)
    finally:
        await engine.dispose()
    assert drift.problems == []
    assert drift.describe() == [], "add a migration for the ORM change (the bootstrap repair is only a safety net)"
