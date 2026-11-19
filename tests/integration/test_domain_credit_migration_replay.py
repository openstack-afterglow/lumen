"""Migration 037 replays cleanly after a partial failure, in isolated MariaDB table namespaces."""

from __future__ import annotations

import os
import re
import uuid

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine

from lumen.scripts.migrate import MIGRATIONS, _statements, load_manifest

pytestmark = pytest.mark.integration

_TABLES = (
    "domain_credit_pools", "domain_credit_ledger", "chat_usage_logs", "chat_runs", "chat_model_call_reservations",
    "chat_stateless_call_reservations", "chat_agent_reservations", "chat_batches",
)


def _namespace(prefix: str):
    def rewrite(statement: str) -> str:
        statement = re.sub(r"\b(chat_|llm_|domain_credit_|user_wallets\b)", lambda m: prefix + m.group(0), statement)
        # Constraint and foreign-key symbols are database-global.
        return re.sub(r"\b(fk_|ck_|chk_|uq_)", lambda m: prefix + m.group(0), statement)

    return rewrite


async def _build(prefix: str, *, partial_first: int | None, replays: int) -> dict[str, str]:
    """Apply the immutable pre-037 history, optionally a failed 037 prefix, then 037 ``replays`` times."""
    engine = create_async_engine(os.environ["DATABASE_URL"], pool_size=1, max_overflow=0)
    rewrite = _namespace(prefix)
    event.listen(engine.sync_engine, "before_cursor_execute",
                 lambda conn, cursor, statement, params, context, many: (rewrite(statement), params), retval=True)
    migrations = load_manifest()
    target = next(m for m in migrations if m.logical_id == "037-domain-credit-pools")
    statements = _statements(MIGRATIONS / target.relative_path)
    try:
        async with engine.begin() as connection:
            for migration in migrations[:migrations.index(target)]:
                for statement in _statements(MIGRATIONS / migration.relative_path):
                    await connection.exec_driver_sql(statement)
            if partial_first is not None:
                # DDL auto-commits: a crash leaves exactly these statements applied.
                for statement in statements[:partial_first]:
                    await connection.exec_driver_sql(statement)
            for _ in range(replays):
                for statement in statements:
                    await connection.exec_driver_sql(statement)
            return {
                table: (await connection.exec_driver_sql(f"SHOW CREATE TABLE {table}")).one()[1].replace(prefix, "")
                for table in _TABLES
            }
    finally:
        async with engine.begin() as connection:
            await connection.exec_driver_sql("SET SESSION FOREIGN_KEY_CHECKS=0")
            try:
                leftovers = (await connection.exec_driver_sql(
                    "SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME LIKE %s",
                    (prefix.replace("_", r"\_") + "%",),
                )).scalars().all()
                for table in leftovers:
                    assert table.startswith(prefix)
                    await connection.exec_driver_sql(f"DROP TABLE IF EXISTS `{table}`")
            finally:
                await connection.exec_driver_sql("SET SESSION FOREIGN_KEY_CHECKS=1")
        await engine.dispose()


async def test_migration_037_replays_after_partial_failure_and_twice():
    clean = await _build(f"m37a_{uuid.uuid4().hex[:8]}_", partial_first=None, replays=1)
    # Failure right after the first CREATE, then the runner replays the file, then once more.
    recovered = await _build(f"m37b_{uuid.uuid4().hex[:8]}_", partial_first=1, replays=2)
    assert recovered == clean
    assert "`payer_domain_id` varchar(64) DEFAULT NULL" in clean["chat_usage_logs"]
    assert "`project_id` varchar(64) DEFAULT NULL" in clean["chat_usage_logs"]
    assert "KEY `idx_chat_usage_payer_user_created`" in clean["chat_usage_logs"]
