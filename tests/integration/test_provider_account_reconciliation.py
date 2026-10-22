"""Real MariaDB upgrades from both immutable027 histories and the legacy repair.

All credentials are synthetic. Each test owns a table namespace, never a live schema.
"""
from __future__ import annotations

import os
import re
from types import SimpleNamespace
from uuid import uuid4

import pytest
from lumen_plugin_api.database import DatabaseConfig
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from lumen.plugins.registry import get_plugin
from lumen.scripts import migrate as runner

pytestmark = pytest.mark.integration

ACCOUNT = "027-provider-account-credentials"
KUBERNETES = "027-kubernetes-sandboxes"
RECONCILIATION = "028-provider-account-reconciliation"


class _UpgradeDatabase:
    def __init__(self, engine):
        self.engine = engine
        self.prefix = "upgrade_" + uuid4().hex[:10] + "_"
        self.migrations = runner.load_manifest()
        self.additions = [item for item in self.migrations if item.logical_id in {ACCOUNT, KUBERNETES, RECONCILIATION}]

    def namespace(self, statement):
        statement = re.sub(
            r"\b(chat_|llm_|user_wallets\b|schema_migrations\b)",
            lambda match: self.prefix + match.group(0), statement,
        )
        return re.sub(r"\b(fk_|ck_|chk_|uq_)", lambda match: self.prefix + match.group(0), statement)

    def namespace_sql(self, connection, cursor, statement, parameters, context, executemany):
        return self.namespace(statement), parameters

    async def execute(self, statement, parameters=None):
        async with self.engine.begin() as connection:
            return await connection.execute(text(statement), parameters or {})

    async def apply(self, migration):
        async with self.engine.begin() as connection:
            for statement in runner._statements(runner.MIGRATIONS / migration.relative_path):
                await connection.exec_driver_sql(statement)
            await connection.execute(text("""
                INSERT INTO schema_migrations VALUES (:id, :path, :checksum, '2026-10-22 08:08:22.123456')
            """), {"id": migration.logical_id, "path": migration.relative_path, "checksum": migration.sha256})

    async def ledger(self):
        return (await self.execute("SELECT * FROM schema_migrations ORDER BY logical_id")).all()

    async def run(self, monkeypatch, *, apply=True):
        handle = get_plugin("database").open(DatabaseConfig(url=os.environ["DATABASE_URL"]))
        event.listen(handle.engine.sync_engine, "before_cursor_execute", self.namespace_sql, retval=True)
        with monkeypatch.context() as patch:
            patch.setattr(runner, "get_plugin", lambda name: SimpleNamespace(open=lambda config: handle))
            return await runner.migrate(os.environ["DATABASE_URL"], apply=apply)

    async def repair(self):
        # Exact schema/material changes in the bounded legacy compatibility repair:
        # preserve credential uniqueness, restore provider PK/nullable credential ID,
        # and copy default ciphertext back without touching accounts or ledger rows.
        await self.execute("ALTER TABLE llm_provider_auth_attempts ADD UNIQUE KEY legacy_credential (credential_id)")
        await self.execute("""
            ALTER TABLE llm_provider_auth_attempts DROP PRIMARY KEY, ADD PRIMARY KEY (provider_id),
                MODIFY COLUMN credential_id BIGINT NULL DEFAULT NULL
        """)
        await self.execute("""
            UPDATE llm_providers p
            JOIN llm_provider_accounts a ON a.provider_id=p.id AND BINARY a.name=BINARY 'default'
            JOIN llm_provider_credentials c ON c.account_id=a.id AND BINARY c.name=BINARY 'default'
            SET p.auth_mode=c.auth_mode, p.encrypted_api_key=c.encrypted_api_key, p.api_key_env=c.api_key_env,
                p.encrypted_subscription_tokens=c.encrypted_subscription_tokens,
                p.subscription_status=c.subscription_status, p.subscription_expires_at=c.subscription_expires_at,
                p.subscription_generation=c.generation
        """)

    async def snapshot(self):
        return {
            table: (await self.execute(f"SELECT * FROM {table} ORDER BY id")).all()
            for table in ("llm_providers", "llm_provider_accounts", "llm_provider_credentials")
        }


@pytest.fixture
async def upgrade_database():
    engine = create_async_engine(os.environ["DATABASE_URL"], pool_size=1, max_overflow=0)
    database = _UpgradeDatabase(engine)
    event.listen(engine.sync_engine, "before_cursor_execute", database.namespace_sql, retval=True)
    try:
        await database.execute("""
            CREATE TABLE schema_migrations (
                logical_id VARCHAR(100) PRIMARY KEY, relative_path VARCHAR(255) NOT NULL,
                sha256 CHAR(64) NOT NULL, applied_at DATETIME(6) NOT NULL
            ) ENGINE=InnoDB
        """)
        # Real immutable history, not today's ORM create_all or a translated SQL dialect.
        for migration in database.migrations:
            if migration in database.additions:
                break
            await database.apply(migration)
        await database.execute("""
            INSERT INTO llm_providers
                (id, name, provider_type, api_provider, encrypted_api_key, encrypted_billing_admin_key,
                 auth_mode, subscription_generation, is_active, margin_multiplier, created_at, updated_at)
            VALUES (71, 'synthetic-legacy-provider', 'openai', 'openai', 'synthetic-ciphertext-A',
                    'synthetic-billing-ciphertext', 'api_key', 4, TRUE, 1, NOW(), NOW())
        """)
        yield database
    finally:
        try:
            async with engine.begin() as connection:
                tables = (await connection.execute(text("""
                    SELECT TABLE_NAME FROM information_schema.TABLES
                    WHERE TABLE_SCHEMA=DATABASE() AND LEFT(TABLE_NAME, LENGTH(:prefix))=:prefix
                """), {"prefix": database.prefix})).scalars().all()
                await connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS=0")
                try:
                    for table in tables:
                        await connection.exec_driver_sql(f"DROP TABLE `{table}`")
                finally:
                    await connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS=1")
        finally:
            await engine.dispose()


@pytest.mark.parametrize("history", ["pre027", "kubernetes027", "account027", "repaired027"])
async def test_ordered_union_upgrades_both_immutable_histories_without_replaying_applied027(
    upgrade_database, monkeypatch, history,
):
    database = upgrade_database
    assert [item.logical_id for item in database.additions] == [ACCOUNT, KUBERNETES, RECONCILIATION]
    if history != "pre027":
        applied_id = KUBERNETES if history == "kubernetes027" else ACCOUNT
        await database.apply(next(item for item in database.additions if item.logical_id == applied_id))
    if history == "repaired027":
        await database.repair()
        # A real legacy writer may create an attempt after the repair without a credential ID.
        await database.execute("""
            INSERT INTO llm_provider_auth_attempts
                (provider_id, id, initiated_by_user_id, initiated_by_project_id, provider_generation,
                 encrypted_payload, status, expires_at, next_poll_at, created_at)
            VALUES (71, :id, 'admin', 'project', 4, 'synthetic-device-payload', 'pending',
                    NOW() + INTERVAL 10 MINUTE, NOW(), NOW())
        """, {"id": str(uuid4())})
    before_ledger = await database.ledger()
    before = await database.snapshot() if history in {"account027", "repaired027"} else None
    pending = [item.logical_id for item in database.additions if item.logical_id not in {row[0] for row in before_ledger}]
    assert await database.run(monkeypatch, apply=False) == (pending, 0)
    assert await database.ledger() == before_ledger
    assert await database.run(monkeypatch) == (pending, 0)
    ledger = await database.ledger()
    assert all(row in ledger for row in before_ledger)  # Includes original path, checksum and applied_at.
    assert {row[:3] for row in ledger} == {
        (item.logical_id, item.relative_path, item.sha256) for item in database.migrations
    }
    material = await database.snapshot()
    if before is not None:
        assert material == before
    credential_id = (await database.execute("SELECT id FROM llm_provider_credentials")).scalar_one()
    assert (await database.execute("""
        SELECT encrypted_api_key, generation FROM llm_provider_credentials WHERE id=:id
    """, {"id": credential_id})).one() == ("synthetic-ciphertext-A", 4)
    assert (await database.execute("""
        SELECT COLUMN_NAME FROM information_schema.KEY_COLUMN_USAGE
        WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='llm_provider_auth_attempts' AND CONSTRAINT_NAME='PRIMARY'
    """)).scalars().all() == ["credential_id"]
    assert (await database.execute("""
        SELECT IS_NULLABLE FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='llm_provider_auth_attempts' AND COLUMN_NAME='credential_id'
    """)).scalar_one() == "NO"
    assert (await database.execute("""
        SELECT IS_NULLABLE FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='chat_runtime_pools' AND COLUMN_NAME='cloud_profile_id'
    """)).scalar_one() == "YES"
    if history == "repaired027":
        assert (await database.execute("""
            SELECT credential_id, provider_generation, encrypted_payload FROM llm_provider_auth_attempts
        """)).one() == (credential_id, 4, "synthetic-device-payload")
    assert await database.run(monkeypatch) == ([], 0)
    assert await database.ledger() == ledger
    assert await database.snapshot() == material

    # Account runtime must be able to keep independent attempts for two credentials
    # of the same provider: the repaired provider-level primary key forbade this.
    await database.execute("DELETE FROM llm_provider_auth_attempts")
    account_id = (await database.execute("SELECT id FROM llm_provider_accounts")).scalar_one()
    await database.execute("""
        INSERT INTO llm_provider_credentials (account_id, name, auth_mode)
        VALUES (:account, 'second', 'chatgpt_device')
    """, {"account": account_id})
    for identity in (await database.execute("SELECT id FROM llm_provider_credentials")).scalars().all():
        await database.execute("""
            INSERT INTO llm_provider_auth_attempts
                (provider_id, credential_id, id, initiated_by_user_id, initiated_by_project_id,
                 provider_generation, encrypted_payload, status, expires_at, next_poll_at, created_at)
            VALUES (71, :credential, :id, 'admin', 'project', 4, 'synthetic-payload', 'pending',
                    NOW() + INTERVAL 10 MINUTE, NOW(), NOW())
        """, {"credential": identity, "id": str(uuid4())})
    assert (await database.execute("SELECT COUNT(*) FROM llm_provider_auth_attempts")).scalar_one() == 2
    with pytest.raises(DBAPIError):
        await database.execute("""
            INSERT INTO llm_provider_auth_attempts
                (provider_id, credential_id, id, initiated_by_user_id, initiated_by_project_id,
                 provider_generation, encrypted_payload, status, expires_at, next_poll_at, created_at)
            VALUES (71, :credential, :id, 'admin', 'project', 4, 'synthetic-payload', 'pending',
                    NOW() + INTERVAL 10 MINUTE, NOW(), NOW())
        """, {"credential": credential_id, "id": str(uuid4())})


@pytest.mark.parametrize("drift", ["rotated_key", "environment", "subscription", "missing_default"])
async def test_repaired_history_fails_closed_before_any_pending_ddl_on_credential_drift(
    upgrade_database, monkeypatch, drift,
):
    database = upgrade_database
    await database.apply(next(item for item in database.additions if item.logical_id == ACCOUNT))
    await database.repair()
    changes = {
        "rotated_key": "UPDATE llm_providers SET encrypted_api_key='synthetic-new-key', subscription_generation=5",
        "environment": "UPDATE llm_providers SET api_key_env='SYNTHETIC_ROTATED_KEY'",
        "subscription": "UPDATE llm_providers SET encrypted_subscription_tokens='synthetic-new-token'",
        "missing_default": "UPDATE llm_provider_accounts SET name='renamed-by-operator'",
    }
    await database.execute(changes[drift])
    before, ledger = await database.snapshot(), await database.ledger()
    for apply in (False, True):
        with pytest.raises(runner.MigrationLedgerError, match="provider_id=71; stop all legacy/account writers") as exc:
            await database.run(monkeypatch, apply=apply)
        assert "synthetic-" not in str(exc.value) and "SYNTHETIC_ROTATED_KEY" not in str(exc.value)
        assert await database.snapshot() == before
        assert await database.ledger() == ledger
        assert (await database.execute("""
            SELECT COUNT(*) FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='chat_runtime_pools' AND COLUMN_NAME='cluster_profile_id'
        """)).scalar_one() == 0  # Preflight runs before Kubernetes027 as well as028.


async def test_repair_after_recorded_reconciliation_is_schema_drift_not_implicit_replay(upgrade_database, monkeypatch):
    database = upgrade_database
    await database.run(monkeypatch)
    await database.repair()
    before, ledger = await database.snapshot(), await database.ledger()
    with pytest.raises(runner.MigrationLedgerError, match="auth-attempt schema drift"):
        await database.run(monkeypatch)
    assert await database.snapshot() == before
    assert await database.ledger() == ledger


async def test_unresolved_attempt_fails_before_alter_even_without_strict_sql_mode(upgrade_database):
    database = upgrade_database
    await database.apply(next(item for item in database.additions if item.logical_id == ACCOUNT))
    await database.repair()
    await database.execute("""
        INSERT INTO llm_provider_auth_attempts
            (provider_id, id, initiated_by_user_id, initiated_by_project_id, provider_generation,
             encrypted_payload, status, expires_at, next_poll_at, created_at)
        VALUES (71, :id, 'admin', 'project', 4, 'synthetic-unresolved-payload', 'pending',
                NOW() + INTERVAL 10 MINUTE, NOW(), NOW())
    """, {"id": str(uuid4())})
    await database.execute("UPDATE llm_provider_accounts SET name='renamed-by-operator'")
    attempts = (await database.execute("SELECT * FROM llm_provider_auth_attempts")).all()
    ledger = await database.ledger()
    migration = next(item for item in database.additions if item.logical_id == RECONCILIATION)
    # Exercise the SQL guard independently of the runner's earlier promotion check.
    # A schema-repair/operator replay must not rely on connection strict-mode defaults.
    async with database.engine.connect() as connection:
        mode = (await connection.exec_driver_sql("SELECT @@SESSION.sql_mode")).scalar_one()
        await connection.exec_driver_sql("SET SESSION sql_mode=''")
        try:
            with pytest.raises(DBAPIError, match="Unresolved provider auth attempt"):
                for statement in runner._statements(runner.MIGRATIONS / migration.relative_path):
                    await connection.exec_driver_sql(statement)
        finally:
            await connection.rollback()
            await connection.execute(text("SET SESSION sql_mode=:mode"), {"mode": mode})
    assert (await database.execute("SELECT * FROM llm_provider_auth_attempts")).all() == attempts
    assert await database.ledger() == ledger
    assert (await database.execute("""
        SELECT COLUMN_NAME FROM information_schema.KEY_COLUMN_USAGE
        WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='llm_provider_auth_attempts' AND CONSTRAINT_NAME='PRIMARY'
    """)).scalars().all() == ["provider_id"]
    assert (await database.execute("""
        SELECT IS_NULLABLE FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='llm_provider_auth_attempts' AND COLUMN_NAME='credential_id'
    """)).scalar_one() == "YES"
