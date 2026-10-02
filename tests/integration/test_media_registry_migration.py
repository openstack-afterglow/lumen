"""Reconcile the historical media migration on isolated real MariaDB tables."""

from __future__ import annotations

import os
import uuid
from types import SimpleNamespace

import pytest
from lumen_plugin_api.database import DatabaseConfig
from sqlalchemy import event, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import create_async_engine

from lumen.plugins.registry import get_plugin
from lumen.scripts import migrate as migration_runner
from lumen.scripts.migrate import MIGRATIONS, MigrationLedgerError, _statements, load_manifest

pytestmark = pytest.mark.integration

LEGACY_ID = "019-media-model-registry"
LEGACY_PATH = "019_media_model_registry.sql"
LEGACY_SHA256 = "51f2d55f7ec84b8273a8b16ae5c92dd0269cc8dac88b8ac4c7b763b127a98d05"


class _MediaDatabase:
    def __init__(self, connection):
        self.connection = connection
        self.prefix = f"media{uuid.uuid4().hex[:10]}_"
        self.migration = next(item for item in load_manifest() if item.logical_id == "020-media-model-registry")

    def namespace(self, statement):
        for table in ("llm_models", "schema_migrations", "chat_conversations"):
            statement = statement.replace(table, f"{self.prefix}{table}")
        return statement

    async def execute(self, statement, parameters=None):
        return await self.connection.execute(text(self.namespace(statement)), parameters or {})

    async def apply_media(self):
        for statement in _statements(MIGRATIONS / self.migration.relative_path):
            await self.execute(statement)

    async def record_legacy(self, *, path=LEGACY_PATH, checksum=LEGACY_SHA256):
        await self.execute(
            "INSERT INTO schema_migrations VALUES (:id, :path, :checksum, '2026-09-29 12:34:56.123456')",
            {"id": LEGACY_ID, "path": path, "checksum": checksum},
        )

    async def ledger(self):
        return (await self.execute("SELECT * FROM schema_migrations ORDER BY logical_id")).all()

    async def models(self):
        return (await self.execute("SELECT * FROM llm_models ORDER BY id")).all()

    async def run(self, monkeypatch, *, apply=True):
        await self.connection.commit()
        handle = get_plugin("database").open(DatabaseConfig(url=os.environ["DATABASE_URL"]))

        def namespace_sql(connection, cursor, statement, parameters, context, executemany):
            return self.namespace(statement), parameters

        event.listen(handle.engine.sync_engine, "before_cursor_execute", namespace_sql, retval=True)
        with monkeypatch.context() as patch:
            patch.setattr(migration_runner, "load_manifest", lambda: [self.migration])
            patch.setattr(migration_runner, "get_plugin", lambda name: SimpleNamespace(open=lambda config: handle))
            return await migration_runner.migrate(os.environ["DATABASE_URL"], apply=apply)


@pytest.fixture
async def media_database():
    engine = create_async_engine(os.environ["DATABASE_URL"], pool_size=1, max_overflow=0)
    try:
        async with engine.connect() as connection:
            database = _MediaDatabase(connection)
            try:
                await database.execute("""
                    CREATE TABLE llm_models (
                        id BIGINT PRIMARY KEY, model_name VARCHAR(190) NOT NULL,
                        opaque_secret MEDIUMTEXT NOT NULL
                    ) ENGINE=InnoDB
                """)
                await database.execute("""
                    CREATE TABLE schema_migrations (
                        logical_id VARCHAR(100) PRIMARY KEY, relative_path VARCHAR(255) NOT NULL,
                        sha256 CHAR(64) NOT NULL, applied_at DATETIME(6) NOT NULL
                    ) ENGINE=InnoDB
                """)
                await database.execute("CREATE TABLE chat_conversations (id BIGINT PRIMARY KEY) ENGINE=InnoDB")
                await database.execute("INSERT INTO llm_models VALUES (1, 'existing-text', 'encrypted-sentinel')")
                await connection.commit()
                yield database
            finally:
                await connection.rollback()
                for table in ("chat_conversations", "schema_migrations", "llm_models"):
                    await database.execute(f"DROP TABLE IF EXISTS {table}")
                await connection.commit()
    finally:
        await engine.dispose()


async def test_exact_historical_media_upgrade_preserves_rows_and_ledger_on_repeated_apply(media_database, monkeypatch):
    database = media_database
    await database.apply_media()
    await database.execute("""
        INSERT INTO llm_models VALUES
            (2, 'existing-image', 'encrypted-media-sentinel', 'image', '{"image_per_unit":"0.04"}')
    """)
    await database.record_legacy()
    models = await database.models()
    historical = (await database.ledger())[0]

    assert await database.run(monkeypatch) == (["020-media-model-registry"], 0)
    assert await database.models() == models
    ledger = await database.ledger()
    assert ledger[0] == historical
    assert ledger[1][:3] == (database.migration.logical_id, database.migration.relative_path, database.migration.sha256)
    assert await database.run(monkeypatch) == ([], 0)
    assert await database.ledger() == ledger
    assert await database.models() == models


async def test_historical_media_dry_run_does_not_record_canonical_identity(media_database, monkeypatch):
    database = media_database
    await database.apply_media()
    await database.record_legacy()
    before = await database.ledger()
    assert await database.run(monkeypatch, apply=False) == (["020-media-model-registry"], 0)
    assert await database.ledger() == before


async def test_fresh_media_migration_still_adds_columns_and_text_default(media_database, monkeypatch):
    database = media_database
    assert await database.run(monkeypatch) == (["020-media-model-registry"], 0)
    assert await database.models() == [(1, "existing-text", "encrypted-sentinel", "text", None)]
    assert [(row[0], row[1], row[2]) for row in await database.ledger()] == [
        (database.migration.logical_id, database.migration.relative_path, database.migration.sha256)
    ]


@pytest.mark.parametrize(
    ("path", "checksum"),
    [("unrelated.sql", LEGACY_SHA256), (LEGACY_PATH, "0" * 64)],
)
async def test_historical_identity_drift_is_rejected_without_ledger_changes(
    media_database, monkeypatch, path, checksum
):
    database = media_database
    await database.apply_media()
    await database.record_legacy(path=path, checksum=checksum)
    before = await database.ledger()
    with pytest.raises(MigrationLedgerError, match="historical media migration identity drift"):
        await database.run(monkeypatch)
    assert await database.ledger() == before


@pytest.mark.parametrize(
    "corruption",
    [
        "ALTER TABLE llm_models DROP COLUMN media_pricing",
        "ALTER TABLE llm_models MODIFY COLUMN model_kind VARCHAR(16) NULL DEFAULT 'image'",
        "ALTER TABLE llm_models DROP COLUMN media_pricing, ADD COLUMN media_pricing LONGTEXT NULL",
    ],
)
async def test_historical_schema_drift_is_rejected_without_ledger_changes(media_database, monkeypatch, corruption):
    database = media_database
    await database.apply_media()
    await database.record_legacy()
    await database.execute(corruption)
    before = await database.ledger()
    with pytest.raises(MigrationLedgerError, match="historical media migration schema drift"):
        await database.run(monkeypatch)
    assert await database.ledger() == before


async def test_unledgered_duplicate_column_is_not_silently_accepted(media_database, monkeypatch):
    database = media_database
    await database.apply_media()
    with pytest.raises(OperationalError) as exc:
        await database.run(monkeypatch)
    assert exc.value.orig.args[0] == 1060
    assert await database.ledger() == []
