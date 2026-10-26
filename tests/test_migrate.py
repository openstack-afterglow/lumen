from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from lumen.scripts import migrate
from lumen.scripts import provider_account_migration as account


def test_baseline_migration_splits_every_statement_before_execution():
    statements = migrate._statements(migrate.MIGRATIONS / "001_baseline.sql")
    chat_jobs = next(statement for statement in statements if "CREATE TABLE IF NOT EXISTS chat_jobs" in statement)

    assert "ALTER TABLE chat_mcp_servers" not in chat_jobs
    assert any(statement.startswith("ALTER TABLE chat_mcp_servers") for statement in statements)


class _ReadResult:
    def __init__(self, rows=(), scalar=None):
        self.rows = list(rows)
        self.scalar = scalar

    def all(self):
        return self.rows

    def one(self):
        assert len(self.rows) == 1
        return self.rows[0]

    def scalars(self):
        return SimpleNamespace(all=lambda: [row[0] for row in self.rows])

    def scalar_one(self):
        return self.scalar


class _ReadOnlyAccountConnection:
    def __init__(self, *, mode="pristine", ledger=None, corrupt_table=None):
        self.mode = mode
        self.ledger = ledger
        self.corrupt_table = corrupt_table
        self.statements = []

    async def exec_driver_sql(self, sql):
        self.statements.append(sql)
        assert sql.lstrip().startswith(("SELECT", "SHOW")), sql
        if "information_schema.TABLES" in sql:
            return _ReadResult([(name,) for name in (*account.schema_for(self.mode), "schema_migrations", "chat_runs")])
        if "SELECT logical_id" in sql:
            return _ReadResult(self.ledger if self.ledger is not None else [
                (m.logical_id, m.relative_path, m.sha256) for m in migrate.load_manifest()
                if m.logical_id != account.ACCOUNT_ID
            ])
        if "SELECT VERSION()" in sql:
            return _ReadResult(scalar="11.8.3-MariaDB")
        if "SELECT @@SESSION.sql_mode" in sql:
            return _ReadResult(scalar="STRICT_TRANS_TABLES,NO_ENGINE_SUBSTITUTION")
        if "information_schema.SCHEMATA" in sql:
            return _ReadResult([("utf8mb4", "utf8mb4_uca1400_ai_ci")])
        if sql.startswith("SHOW CREATE"):
            table = sql.split()[-1].strip("`")
            ddl = account.schema_for(self.mode)[table]
            if table == self.corrupt_table:
                ddl = ddl.replace("`id` bigint(20)", "`id` int(11)")
            return _ReadResult([(table, ddl)])
        return _ReadResult(scalar=0)


async def test_account_preflight_validates_pristine_source_schema_using_only_reads():
    connection = _ReadOnlyAccountConnection()
    assert await account.inspect_account_state(connection, migrate.load_manifest()) == "pristine"
    assert all(sql.lstrip().startswith(("SELECT", "SHOW")) for sql in connection.statements)


@pytest.mark.parametrize("mode", ["accounted", "repaired"])
async def test_exact_historical_account_receipt_classifies_without_writes(mode):
    ledger = [(m.logical_id, m.relative_path, m.sha256) for m in migrate.load_manifest() if m.logical_id != account.ACCOUNT_ID]
    ledger.append((account.HISTORICAL_ID, account.HISTORICAL_PATH, account.HISTORICAL_SHA256))
    connection = _ReadOnlyAccountConnection(mode=mode, ledger=ledger)
    assert await account.inspect_account_state(connection, migrate.load_manifest()) == mode
    assert connection.ledger == ledger


async def test_applied028_receipt_does_not_hide_partial_schema_on_rerun():
    ledger = [(m.logical_id, m.relative_path, m.sha256) for m in migrate.load_manifest()]
    connection = _ReadOnlyAccountConnection(mode="accounted", ledger=ledger, corrupt_table="llm_provider_credentials")
    with pytest.raises(account.AccountMigrationError, match="schema_drift_llm_provider_credentials"):
        await account.inspect_account_state(connection, migrate.load_manifest())


async def test_missing_published_prefix_is_not_repaired_by_replaying_history():
    ledger = [(m.logical_id, m.relative_path, m.sha256) for m in migrate.load_manifest() if not m.logical_id.startswith(("026-", "028-"))]
    connection = _ReadOnlyAccountConnection(ledger=ledger)
    with pytest.raises(account.AccountMigrationError, match="incomplete_published026_prefix"):
        await account.inspect_account_state(connection, migrate.load_manifest())


@pytest.mark.parametrize("logical_id,path,checksum", [
    (account.HISTORICAL_ID, "wrong.sql", account.HISTORICAL_SHA256),
    (account.HISTORICAL_ID, account.HISTORICAL_PATH, "0" * 64),
    ("unknown-account-migration", account.HISTORICAL_PATH, account.HISTORICAL_SHA256),
])
async def test_account_preflight_rejects_unknown_or_drifted_receipts_before_schema_reads(logical_id, path, checksum):
    connection = _ReadOnlyAccountConnection(ledger=[(logical_id, path, checksum)])
    with pytest.raises(account.AccountMigrationError, match="unknown_or_drifted_ledger"):
        await account.inspect_account_state(connection, migrate.load_manifest())
    assert not any(sql.startswith("SHOW CREATE") for sql in connection.statements)


async def test_account_preflight_rejects_schema_drift_read_only():
    connection = _ReadOnlyAccountConnection(corrupt_table="llm_providers")
    with pytest.raises(account.AccountMigrationError, match="schema_drift_llm_providers"):
        await account.inspect_account_state(connection, migrate.load_manifest())


def test_account_schema_normalizes_only_database_default_not_json_or_unknown_structure():
    ddl = account.schema_for("pristine")["llm_providers"]
    default = ("utf8mb4", "utf8mb4_uca1400_ai_ci")
    actual = ddl.replace("CHARSET=utf8mb3 COLLATE=utf8mb3_general_ci", "CHARSET=utf8mb4 COLLATE=utf8mb4_uca1400_ai_ci")
    assert account.canonical_schema(actual, default) == account.canonical_schema(ddl, default)
    assert account.canonical_schema(actual) != account.canonical_schema(ddl)
    account_ddl = account.schema_for("accounted")["llm_provider_accounts"]
    assert account.canonical_schema(account_ddl.replace("utf8mb4_bin", "utf8mb4_unicode_ci"), default) != account.canonical_schema(account_ddl, default)


async def test_account_preflight_failure_precedes_every_runner_ddl_and_closes_handle(monkeypatch):
    connection = _ReadOnlyAccountConnection()
    began = False

    @asynccontextmanager
    async def connect():
        yield connection

    @asynccontextmanager
    async def begin():
        nonlocal began
        began = True
        yield connection

    close = AsyncMock()
    handle = SimpleNamespace(engine=SimpleNamespace(connect=connect, begin=begin), close=close)
    monkeypatch.setattr(migrate, "get_plugin", lambda name: SimpleNamespace(open=lambda config: handle))
    monkeypatch.setattr(migrate, "inspect_account_state", AsyncMock(side_effect=account.AccountMigrationError("synthetic-preflight-denial")))
    with pytest.raises(migrate.MigrationLedgerError, match="synthetic-preflight-denial"):
        await migrate.migrate("mysql+aiomysql://synthetic/fixture", apply=True)
    assert not began
    assert connection.statements == []
    close.assert_awaited_once()


async def test_empty_database_dry_run_never_creates_ledger(monkeypatch):
    @asynccontextmanager
    async def connect():
        yield SimpleNamespace()

    close = AsyncMock()
    handle = SimpleNamespace(engine=SimpleNamespace(connect=connect), close=close)
    monkeypatch.setattr(migrate, "get_plugin", lambda name: SimpleNamespace(open=lambda config: handle))
    monkeypatch.setattr(migrate, "inspect_account_state", AsyncMock(return_value="bootstrap"))
    assert await migrate.migrate("mysql+aiomysql://synthetic/fixture", apply=False) == ([m.logical_id for m in migrate.load_manifest()], 0)
    close.assert_awaited_once()


def test_published027_is_unchanged_and_accounts_have_distinct_forward_identity():
    manifests = {migration.logical_id: migration for migration in migrate.load_manifest()}
    assert manifests["027-kubernetes-sandboxes"].relative_path == "027_kubernetes_sandboxes.sql"
    assert manifests["027-kubernetes-sandboxes"].sha256 == "d0a1706c7c1c3d625af085ff3a9f34b33198dae1e2af11c1aa33d71f3fe764f8"
    assert account.HISTORICAL_ID not in manifests
    assert manifests[account.ACCOUNT_ID].relative_path == "028_provider_account_credentials.sql"
    statements = migrate._statements(migrate.MIGRATIONS / manifests[account.ACCOUNT_ID].relative_path)
    assert not any("IF NOT EXISTS" in statement for statement in statements)
    assert not any("schema_migrations" in statement for statement in statements)
