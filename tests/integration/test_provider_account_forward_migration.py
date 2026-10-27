"""Real MariaDB forward migration; every credential and row is synthetic.

The namespace adapter changes table/constraint names only. It returns actual
MariaDB rows and SHOW CREATE, and never substitutes schema or data guard results.
"""
from __future__ import annotations

import hashlib
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine

from lumen import crypto
from lumen.scripts import migrate as runner
from lumen.scripts import provider_account_migration as account

pytestmark = pytest.mark.integration


class _NamespacedResult:
    def __init__(self, result, prefix):
        self.result = result
        self.prefix = prefix

    def _logical(self, value):
        return value.replace(self.prefix, "") if isinstance(value, str) else value

    def all(self):
        return [tuple(self._logical(value) for value in row) for row in self.result.all()]

    def one(self):
        return tuple(self._logical(value) for value in self.result.one())

    def scalars(self):
        return SimpleNamespace(all=lambda: [self._logical(value) for value in self.result.scalars().all()])

    def scalar_one(self):
        return self.result.scalar_one()


class _Connection:
    def __init__(self, connection, database):
        self.connection = connection
        self.database = database

    async def exec_driver_sql(self, statement, parameters=()):
        if statement.lstrip().upper().startswith("SELECT") and any(name in statement for name in (
            "information_schema.TABLES", "information_schema.COLUMNS", "information_schema.KEY_COLUMN_USAGE",
        )):
            statement += " AND TABLE_NAME LIKE %s"
            parameters = (*parameters, self.database.prefix + "%")
        result = await self.connection.exec_driver_sql(statement, parameters)
        if "information_schema" in statement or statement.startswith("SHOW CREATE"):
            return _NamespacedResult(result, self.database.prefix)
        return result

    async def execute(self, statement, parameters=None):
        return await self.connection.execute(statement, parameters or {})


class _Database:
    def __init__(self, engine):
        self.raw_engine = engine
        self.prefix = "af" + uuid4().hex[:10] + "_"
        self.statements = []
        self.migrations = runner.load_manifest()

    def namespace(self, connection, cursor, statement, parameters, context, executemany):
        statement = re.sub(r"\b(chat_|llm_|user_wallets\b|schema_migrations\b)", lambda m: self.prefix + m[0], statement)
        statement = re.sub(r"\b(fk_|ck_|chk_|uq_)", lambda m: self.prefix + m[0], statement)
        self.statements.append(statement)
        return statement, parameters

    @asynccontextmanager
    async def connect(self):
        async with self.raw_engine.connect() as connection:
            yield _Connection(connection, self)

    @asynccontextmanager
    async def begin(self):
        async with self.raw_engine.begin() as connection:
            yield _Connection(connection, self)

    async def sql(self, statement, parameters=()):
        async with self.raw_engine.begin() as connection:
            return await connection.exec_driver_sql(statement, parameters)

    async def record(self, logical_id, path, checksum):
        await self.sql("INSERT INTO schema_migrations VALUES (%s,%s,%s,'2026-10-22 07:55:08.135432')", (logical_id, path, checksum))

    async def historical(self):
        await self.record(account.HISTORICAL_ID, account.HISTORICAL_PATH, account.HISTORICAL_SHA256)

    async def prepare(self, *, kubernetes=True):
        await self.sql("CREATE TABLE schema_migrations (logical_id VARCHAR(100) PRIMARY KEY, relative_path VARCHAR(255) NOT NULL, sha256 CHAR(64) NOT NULL, applied_at DATETIME(6) NOT NULL) ENGINE=InnoDB")
        for migration in self.migrations:
            if migration.logical_id == account.ACCOUNT_ID or (not kubernetes and migration.logical_id.startswith("027-")):
                break
            for statement in runner._statements(runner.MIGRATIONS / migration.relative_path):
                await self.sql(statement)
            await self.record(migration.logical_id, migration.relative_path, migration.sha256)
        self.key = crypto.encrypt_llm_provider_key("synthetic-legacy-key")
        self.billing = crypto.encrypt_llm_provider_billing_admin_key("synthetic-billing-key")
        self.bundle_plaintext = '{"access_token":"synthetic-subscription-token","expires_at":null}'
        self.bundle = crypto.encrypt_llm_provider_key(self.bundle_plaintext)
        self.attempt = crypto.encrypt_llm_provider_key('{"device_auth_id":"synthetic-device","user_code":"SYNTHETIC"}')
        await self.sql("""
            INSERT INTO llm_providers (id,name,provider_type,api_provider,auth_mode,encrypted_api_key,
                api_key_env,encrypted_subscription_tokens,subscription_status,subscription_generation,
                encrypted_billing_admin_key,is_active,margin_multiplier,created_at,updated_at)
            VALUES (71,'legacy-api','openai','openai','api_key',%s,NULL,NULL,'disconnected',4,%s,1,1,NOW(),NOW()),
                   (72,'legacy-env','openai','openai','api_key',NULL,'SYNTHETIC_KEY_ENV',NULL,'disconnected',2,NULL,1,1,NOW(),NOW()),
                   (73,'legacy-sub','anthropic','anthropic','anthropic_subscription',NULL,NULL,%s,'configured',9,NULL,1,1,NOW(),NOW()),
                   (74,'legacy-device','chatgpt','chatgpt','chatgpt_device',NULL,NULL,NULL,'disconnected',6,NULL,1,1,NOW(),NOW())
        """, (self.key, self.billing, self.bundle))
        await self.sql("""
            INSERT INTO llm_models (id,provider_id,model_name,is_active,is_title_model,is_memory_model,
                input_price,output_price,created_at,updated_at)
            VALUES (811,71,'legacy-model',1,0,0,0.000001,0.000002,NOW(),NOW())
        """)
        self.run_id = str(uuid4())
        await self.sql("""
            INSERT INTO chat_runs (id,run_scope,run_kind,project_id,user_id,model_name,
                capability_snapshot,pricing_snapshot,client_request_id,request_fingerprint,
                fingerprint_version,execution_protocol_version,execution_mode,depth,status,last_seq,
                current_ordinal,reserved_credits,descendant_credits_reserved,sandbox_seconds_reserved,
                source,created_at,updated_at)
            VALUES (%s,'temporary','completion','project','user','legacy-model','{}','{}',%s,%s,
                1,1,'chat',0,'completed',7,1,0,0,0,'api',NOW(),NOW())
        """, (self.run_id, str(uuid4()), "a" * 64))
        await self.sql("""
            INSERT INTO chat_run_providers (run_id,purpose,provider_id,model_id,provider_label,
                model_label,config_version_hash,created_at)
            VALUES (%s,'completion',71,811,'legacy-api','legacy-model','historical-config-hash',NOW())
        """, (self.run_id,))
        await self.sql("""
            INSERT INTO chat_usage_logs (id,project_id,user_id,model_name,prompt_tokens,completion_tokens,
                raw_cost,credited_cost,source,event_id,pricing_status,created_at)
            VALUES (912,'project','user','legacy-model',11,13,0.01,0.02,'api','synthetic-event','settled',NOW())
        """)

    async def apply_unreceipted_account_schema(self):
        fixture = Path(__file__).resolve().parents[1] / "fixtures" / account.HISTORICAL_PATH
        assert hashlib.sha256(fixture.read_bytes()).hexdigest() == account.HISTORICAL_SHA256
        for statement in runner._statements(fixture):
            await self.sql(statement)

    async def repair(self):
        await self.apply_unreceipted_account_schema()
        await self.historical()
        await self.sql("ALTER TABLE llm_provider_auth_attempts ADD UNIQUE KEY uq_legacy_compat_auth_credential (credential_id), DROP PRIMARY KEY, ADD PRIMARY KEY (provider_id), MODIFY COLUMN credential_id BIGINT NULL DEFAULT NULL")
        await self.sql("""
            UPDATE llm_providers p JOIN llm_provider_accounts a ON a.provider_id=p.id
            JOIN llm_provider_credentials c ON c.account_id=a.id
            SET p.auth_mode=c.auth_mode,p.encrypted_api_key=c.encrypted_api_key,p.api_key_env=c.api_key_env,
                p.encrypted_subscription_tokens=c.encrypted_subscription_tokens,p.subscription_status=c.subscription_status,
                p.subscription_expires_at=c.subscription_expires_at,p.subscription_generation=c.generation
        """)

    async def snapshot(self):
        names = (await self.sql("SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME LIKE %s ORDER BY TABLE_NAME", (self.prefix + "%",))).scalars().all()
        snapshot = {}
        for native_name in names:
            name = native_name.removeprefix(self.prefix)
            ddl = (await self.sql("SHOW CREATE TABLE `" + name + "`")).one()[1]
            rows = (await self.sql("SELECT * FROM `" + name + "`")).all()
            snapshot[name] = (ddl, sorted((tuple(row) for row in rows), key=repr))
        return snapshot

    async def run(self, monkeypatch, *, apply=True):
        async def close():
            pass  # The fixture owns its actual engine and namespace lifetime.

        handle = SimpleNamespace(engine=self, close=close)
        monkeypatch.setattr(runner, "get_plugin", lambda name: SimpleNamespace(open=lambda config: handle))
        # These unrelated graph projections are tested by their own real-DB suite.
        monkeypatch.setattr(runner, "backfill_history", AsyncMock())
        monkeypatch.setattr(runner, "count_unready", AsyncMock(return_value=0))
        monkeypatch.setattr(runner, "_verify_active_paths", AsyncMock())
        self.statements.clear()
        return await runner.migrate(os.environ["DATABASE_URL"], apply=apply)


@pytest.fixture
async def database():
    engine = create_async_engine(os.environ["DATABASE_URL"], pool_size=1, max_overflow=0)
    database = _Database(engine)
    event.listen(engine.sync_engine, "before_cursor_execute", database.namespace, retval=True)
    try:
        yield database
    finally:
        async with engine.begin() as connection:
            await connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS=0")
            names = (await connection.exec_driver_sql("SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME LIKE %s", (database.prefix + "%",))).scalars().all()
            for name in names:
                await connection.exec_driver_sql("DROP TABLE `" + name + "`")
            await connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS=1")
        await engine.dispose()


@pytest.mark.parametrize("kubernetes", [False, True])
async def test_pristine_published_upgrade_preserves_ciphertext_generations_attempts_and_usage(database, monkeypatch, kubernetes):
    await database.prepare(kubernetes=kubernetes)
    await database.sql("""
        INSERT INTO llm_provider_auth_attempts (provider_id,id,initiated_by_user_id,initiated_by_project_id,
            provider_generation,encrypted_payload,status,expires_at,next_poll_at,created_at)
        VALUES (74,%s,'admin','project',6,%s,'pending',NOW()+INTERVAL 10 MINUTE,NOW(),NOW())
    """, (str(uuid4()), database.attempt))
    before = await database.snapshot()
    pending, unready = await database.run(monkeypatch)
    assert account.ACCOUNT_ID in pending and unready == 0
    rows = (await database.sql("SELECT a.provider_id,c.encrypted_api_key,c.api_key_env,c.encrypted_subscription_tokens,c.generation FROM llm_provider_accounts a JOIN llm_provider_credentials c ON c.account_id=a.id ORDER BY a.provider_id")).all()
    assert rows == [(71, database.key, None, None, 4), (72, None, 'SYNTHETIC_KEY_ENV', None, 2), (73, None, None, database.bundle, 9), (74, None, None, None, 6)]
    assert crypto.decrypt_llm_provider_key(rows[0][1]) == "synthetic-legacy-key"
    assert crypto.decrypt_llm_provider_key(rows[2][3]) == database.bundle_plaintext
    assert (await database.sql("SELECT encrypted_billing_admin_key FROM llm_providers WHERE id=71")).scalar_one() == database.billing
    assert (await database.sql("SELECT provider_id,provider_generation,encrypted_payload FROM llm_provider_auth_attempts")).one() == (74, 6, database.attempt)
    attempt_credential = (await database.sql("SELECT credential_id FROM llm_provider_auth_attempts")).scalar_one()
    assert (await database.sql("SELECT a.provider_id FROM llm_provider_credentials c JOIN llm_provider_accounts a ON a.id=c.account_id WHERE c.id=%s", (attempt_credential,))).scalar_one() == 74
    after = await database.snapshot()
    assert after['llm_models'] == before['llm_models']
    assert after['chat_usage_logs'][1][0][:len(before['chat_usage_logs'][1][0])] == before['chat_usage_logs'][1][0]
    assert after['chat_runs'] == before['chat_runs']
    assert after['chat_run_providers'][1][0][:len(before['chat_run_providers'][1][0])] == before['chat_run_providers'][1][0]
    for receipt in before['schema_migrations'][1]:
        assert receipt in after['schema_migrations'][1]
    assert await database.run(monkeypatch) == ([], 0)
    assert await database.snapshot() == after


@pytest.mark.parametrize("state", ["accounted", "repaired"])
async def test_exact_historical_receipt_is_preserved_without_replaying027(database, monkeypatch, state):
    await database.prepare()
    if state == "repaired":
        await database.repair()
    else:
        await database.apply_unreceipted_account_schema()
        await database.historical()
        await database.sql("UPDATE llm_provider_accounts SET name='operator-account',weight=17 WHERE provider_id=71")
        await database.sql("UPDATE llm_provider_credentials SET encrypted_api_key='synthetic-rotated-envelope',generation=23 WHERE account_id=(SELECT id FROM llm_provider_accounts WHERE provider_id=71)")
        await database.sql("INSERT INTO chat_provider_usage (event_id,provider_account_id,provider_credential_id,provider_id,provider_credential_generation,raw_cost) VALUES ('historical-deleted-identity',9991,9992,9993,7,0.123)")
    before = await database.snapshot()
    assert await database.run(monkeypatch) == ([account.ACCOUNT_ID], 0)
    migration_statements = tuple(database.statements)
    after = await database.snapshot()
    assert (await database.sql("SELECT COUNT(*) FROM llm_providers WHERE encrypted_api_key IS NOT NULL OR api_key_env IS NOT NULL OR encrypted_subscription_tokens IS NOT NULL OR subscription_status <> 'disconnected' OR subscription_expires_at IS NOT NULL OR subscription_generation <> 0")).scalar_one() == 0
    assert (await database.sql("SELECT encrypted_billing_admin_key FROM llm_providers WHERE id=71")).scalar_one() == database.billing
    async with database.connect() as connection:
        actual_auth = (await connection.exec_driver_sql("SHOW CREATE TABLE llm_provider_auth_attempts")).one()[1]
        installation_default = tuple((await connection.exec_driver_sql("SELECT DEFAULT_CHARACTER_SET_NAME, DEFAULT_COLLATION_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME=DATABASE()")).one())
    assert account.canonical_schema(actual_auth, installation_default) == account.canonical_schema(account.schema_for("accounted")["llm_provider_auth_attempts"], installation_default)
    for table in ('llm_provider_accounts','llm_provider_credentials','chat_provider_usage','chat_run_providers','chat_usage_logs','llm_models'):
        assert after[table] == before[table]
    for receipt in before['schema_migrations'][1]:
        assert receipt in after['schema_migrations'][1]
    assert not any('INSERT INTO' in sql and 'llm_provider_accounts' in sql for sql in migration_statements)
    assert await database.run(monkeypatch) == ([], 0)
    assert await database.snapshot() == after
    rotated = crypto.encrypt_llm_provider_key("synthetic-post028-operator-key")
    await database.sql("UPDATE llm_provider_credentials SET encrypted_api_key=%s,generation=generation+1 WHERE account_id=(SELECT id FROM llm_provider_accounts WHERE provider_id=71)", (rotated,))
    await database.sql("UPDATE llm_provider_accounts SET name='post028-operator-account',weight=29 WHERE provider_id=71")
    edited = await database.snapshot()
    assert await database.run(monkeypatch) == ([], 0)
    assert await database.snapshot() == edited
    assert (await database.sql("SELECT encrypted_api_key FROM llm_provider_credentials WHERE account_id=(SELECT id FROM llm_provider_accounts WHERE provider_id=71)")).scalar_one() == rotated


@pytest.mark.parametrize("apply", [False, True])
async def test_receipted028_missing_published027_rejects_without_ddl(database, monkeypatch, apply):
    await database.prepare()
    assert await database.run(monkeypatch) == ([account.ACCOUNT_ID], 0)
    await database.sql("DELETE FROM schema_migrations WHERE logical_id='027-kubernetes-sandboxes'")
    before = await database.snapshot()
    with pytest.raises(runner.MigrationLedgerError, match="incomplete_published027_prefix"):
        await database.run(monkeypatch, apply=apply)
    assert all(sql.lstrip().startswith(("SELECT", "SHOW")) for sql in database.statements)
    assert await database.snapshot() == before


@pytest.mark.parametrize("corruption", [
    "ALTER TABLE llm_providers ADD COLUMN selection_policy VARCHAR(16) NOT NULL DEFAULT 'priority'",
    "CREATE TABLE llm_provider_accounts (id BIGINT PRIMARY KEY)",
    "UPDATE schema_migrations SET sha256=REPEAT('0',64) WHERE logical_id='027-kubernetes-sandboxes'",
    "INSERT INTO schema_migrations VALUES ('unknown','unknown.sql',REPEAT('0',64),NOW(6))",
])
async def test_partial_or_ambiguous_legacy_states_reject_before_any_mutation(database, monkeypatch, corruption):
    await database.prepare()
    await database.sql(corruption)
    before = await database.snapshot()
    with pytest.raises(runner.MigrationLedgerError, match='provider account preflight'):
        await database.run(monkeypatch)
    assert all(sql.lstrip().startswith(('SELECT','SHOW')) for sql in database.statements)
    assert await database.snapshot() == before


@pytest.mark.parametrize("corruption", [
    "UPDATE llm_providers SET encrypted_api_key='synthetic-new-legacy-key' WHERE id=71",
    "UPDATE llm_provider_credentials SET generation=generation+1 WHERE account_id=(SELECT id FROM llm_provider_accounts WHERE provider_id=71)",
    "UPDATE llm_provider_accounts SET weight=17 WHERE provider_id=71",
    "UPDATE schema_migrations SET relative_path='wrong.sql' WHERE logical_id='027-provider-account-credentials'",
    "ALTER TABLE llm_provider_auth_attempts MODIFY COLUMN credential_id BIGINT NOT NULL",
    "UPDATE chat_runs SET status='queued'",
    "UPDATE chat_usage_logs SET provider_account_id=9991 WHERE id=912",
    "UPDATE chat_usage_logs SET pricing_snapshot='{\"nested\":{\"provider_credential_id\":9992}}' WHERE id=912",
])
async def test_repaired_drift_never_selects_credential_authority_or_writes(database, monkeypatch, corruption):
    await database.prepare()
    await database.repair()
    await database.sql(corruption)
    before = await database.snapshot()
    with pytest.raises(runner.MigrationLedgerError, match='provider account preflight'):
        await database.run(monkeypatch)
    assert all(sql.lstrip().startswith(('SELECT','SHOW')) for sql in database.statements)
    assert await database.snapshot() == before


async def test_repaired_dry_run_is_read_only(database, monkeypatch):
    await database.prepare()
    await database.repair()
    before = await database.snapshot()
    assert await database.run(monkeypatch, apply=False) == ([account.ACCOUNT_ID], 0)
    assert all(sql.lstrip().startswith(('SELECT','SHOW')) for sql in database.statements)
    assert await database.snapshot() == before


async def test_empty_database_bootstrap_and_rerun_use_actual_server_default(database, monkeypatch):
    assert await database.run(monkeypatch, apply=False) == ([m.logical_id for m in database.migrations], 0)
    assert all(sql.lstrip().startswith(('SELECT','SHOW')) for sql in database.statements)
    assert await database.run(monkeypatch) == ([m.logical_id for m in database.migrations], 0)
    before = await database.snapshot()
    assert await database.run(monkeypatch) == ([], 0)
    assert await database.snapshot() == before
