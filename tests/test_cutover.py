from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from lumen.scripts import cutover

_BASELINE_MIGRATION = Path(__file__).parents[1] / "lumen" / "migrations" / "001_baseline.sql"
_AFTERGLOW_DROP_MIGRATION = Path(__file__).parents[3] / "backend" / "migrations" / "075_drop_lumen_tables.sql"


def _spec() -> cutover.TableSpec:
    return cutover.TableSpec(name="chat_agents", columns=("id", "metadata"), primary_key=("id",))


def test_cutover_and_afterglow_cleanup_cover_exactly_every_lumen_baseline_table():
    migration_tables = set(re.findall(r"CREATE TABLE IF NOT EXISTS ([a-z_]+)", _BASELINE_MIGRATION.read_text()))
    dropped_tables = set(re.findall(r"DROP TABLE IF EXISTS ([a-z_]+)", _AFTERGLOW_DROP_MIGRATION.read_text()))

    assert set(cutover._TABLE_NAMES) == migration_tables == dropped_tables
    assert "user_wallets" in cutover._TABLE_NAMES


def test_copy_plan_is_restart_safe_and_normalizes_json_values():
    spec = _spec()
    source = [{"id": 1, "metadata": '{"tools": ["search"]}'}]
    destination = [{"id": 1, "metadata": {"tools": ["search"]}}]

    assert cutover._copy_plan(spec, source, destination) == ()
    assert cutover._copy_plan(spec, source, []) == tuple(source)


@pytest.mark.parametrize(
    ("source", "destination", "message"),
    [
        (
            [{"id": 1, "metadata": "{}"}],
            [{"id": 2, "metadata": "{}"}],
            "destination chat_agents has rows absent from Afterglow",
        ),
        (
            [{"id": 1, "metadata": "{}"}],
            [{"id": 1, "metadata": "[]"}],
            "conflicting chat_agents row during cutover",
        ),
    ],
)
def test_copy_plan_rejects_destination_divergence(
    source: list[dict[str, Any]], destination: list[dict[str, Any]], message: str
):
    with pytest.raises(cutover.CutoverError, match=message):
        cutover._copy_plan(_spec(), source, destination)


def test_database_url_normalization_rejects_unsupported_backends():
    assert cutover._mysql_async_url("mysql://user:secret@db/source", label="source").drivername == "mysql+aiomysql"
    assert (
        cutover._mysql_async_url("mysql+asyncmy://user:secret@db/source", label="source").drivername == "mysql+aiomysql"
    )

    with pytest.raises(cutover.CutoverError, match="MySQL/MariaDB"):
        cutover._mysql_async_url("postgresql://user:secret@db/source", label="source")
