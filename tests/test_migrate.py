from __future__ import annotations

from lumen.scripts import migrate


def test_baseline_migration_splits_every_statement_before_execution():
    statements = migrate._statements(migrate.MIGRATIONS / "001_baseline.sql")
    chat_jobs = next(statement for statement in statements if "CREATE TABLE IF NOT EXISTS chat_jobs" in statement)

    assert "ALTER TABLE chat_mcp_servers" not in chat_jobs
    assert any(statement.startswith("ALTER TABLE chat_mcp_servers") for statement in statements)
