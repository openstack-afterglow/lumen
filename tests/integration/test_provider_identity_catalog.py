"""Real MariaDB backfill and repository reload coverage in isolated legacy tables."""

from __future__ import annotations

import os
import re
import uuid

import pytest
from sqlalchemy import event, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from lumen.crypto import encrypt_llm_provider_key
from lumen.models.chat_db import LlmModel, LlmProvider
from lumen.scripts.migrate import MIGRATIONS, _statements, load_manifest
from lumen.services.providers import repository, routing
from lumen.services.providers.errors import AmbiguousModelRouteError

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("model_kind", ["text", "image"])
async def test_catalog_backfill_rename_rank_and_encrypted_transport_survive_reload(monkeypatch, model_kind):
    engine = create_async_engine(os.environ["DATABASE_URL"], pool_size=1, max_overflow=0)
    prefix = f"catalog_{uuid.uuid4().hex[:10]}_"

    def namespace_sql(connection, cursor, statement, parameters, context, executemany):
        return statement.replace("llm_", f"{prefix}llm_").replace("chat_run", f"{prefix}chat_run"), parameters

    event.listen(engine.sync_engine, "before_cursor_execute", namespace_sql, retval=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(repository, "_require_db", lambda: factory)
    monkeypatch.setattr(routing, "_require_db", lambda: factory)
    migrations = load_manifest()
    catalog_migration = next(item for item in migrations if item.logical_id == "021-provider-identity-catalog-order")
    encrypted = encrypt_llm_provider_key("isolated-provider-key")
    try:
        async with engine.begin() as connection:
            # Build the actual pre-021 catalog from immutable historical SQL, not
            # today's ORM defaults. No shared integration schema is altered.
            for migration in migrations:
                if migration == catalog_migration:
                    break
                for statement in _statements(MIGRATIONS / migration.relative_path):
                    if (
                        re.match(r"(?:CREATE TABLE (?:IF NOT EXISTS )?|ALTER TABLE )llm_", statement)
                        or " ON llm_" in statement
                    ):
                        await connection.exec_driver_sql(statement)
            await connection.exec_driver_sql("CREATE TABLE chat_runs (id CHAR(36) PRIMARY KEY, status VARCHAR(20))")
            await connection.exec_driver_sql(
                "CREATE TABLE chat_run_providers (run_id CHAR(36), provider_id BIGINT, model_id BIGINT)"
            )
            # A fixed past clock makes any timestamp bump by selector/rank edits change the route hash.
            await connection.exec_driver_sql(
                """
                INSERT INTO llm_providers
                    (id, name, provider_type, api_base, encrypted_api_key, is_active, margin_multiplier,
                     created_at, updated_at)
                VALUES (1, 'OpenAI', 'openai', NULL, %s, TRUE, 1, '2026-01-01 00:00:00', '2026-01-01 00:00:00'),
                       (2, 'NVIDIA NIM', 'openai', 'https://provider.example/v1', %s, TRUE, 1,
                        '2026-01-01 00:00:00', '2026-01-01 00:00:00')
            """,
                (encrypted, encrypted),
            )
            await connection.exec_driver_sql("""
                INSERT INTO llm_models
                    (id, provider_id, model_name, is_active, is_title_model, is_memory_model,
                     input_price, output_price, price_source, created_at, updated_at)
                VALUES (11, 1, 'shared-id', TRUE, FALSE, FALSE, 0.000001, 0.000002, 'manual',
                        '2026-01-01 00:00:00', '2026-01-01 00:00:00'),
                       (12, 2, 'shared-id', TRUE, FALSE, FALSE, 0.000001, 0.000002, 'manual',
                        '2026-01-01 00:00:00', '2026-01-01 00:00:00'),
                       (13, 2, 'other-id', TRUE, FALSE, FALSE, 0.000001, 0.000002, 'manual',
                        '2026-01-01 00:00:00', '2026-01-01 00:00:00')
            """)
            if model_kind == "image":
                await connection.exec_driver_sql("UPDATE llm_providers SET api_base = NULL")
                await connection.exec_driver_sql("""
                    UPDATE llm_models SET model_kind = 'image',
                        media_pricing = '{"image_per_unit": "0.01"}', input_price = NULL, output_price = NULL
                """)
            for statement in _statements(MIGRATIONS / catalog_migration.relative_path):
                await connection.exec_driver_sql(statement)

        backfilled = await repository.list_providers()
        assert [(p["id"], p["api_provider"], p["sort_order"]) for p in backfilled] == [
            (1, "openai", 0),
            (2, "openai", 0),
        ]
        with pytest.raises(AmbiguousModelRouteError):
            await routing.resolve_api_model("shared-id", provider="openai", model_kind=model_kind)
        original = await routing.resolve_api_model("shared-id", provider_id=2, model_kind=model_kind)
        assert original["api_key"] == "isolated-provider-key"
        assert original["provider_type"] == "openai"

        # Preserve a frozen route through selector/order-only edits.
        await repository.update_provider(2, {"api_provider": "nvidia", "sort_order": 0})
        await repository.update_provider(1, {"sort_order": 10})
        await repository.update_model(13, {"sort_order": 0})
        await repository.update_model(12, {"sort_order": 5})
        restored = await routing.resolve_model_snapshot(original)
        assert restored["config_version_hash"] == original["config_version_hash"]
        await repository.update_provider(2, {"name": "NVIDIA"})

        # Reapplying additive SQL must not overwrite an administrator's rename/ranks.
        async with engine.begin() as connection:
            for statement in _statements(MIGRATIONS / catalog_migration.relative_path):
                await connection.exec_driver_sql(statement)
        providers = await repository.list_providers()
        assert [(p["id"], p["name"], p["api_provider"], p["sort_order"]) for p in providers] == [
            (2, "NVIDIA", "nvidia", 0),
            (1, "OpenAI", "openai", 10),
        ]
        assert [row["id"] for row in await repository.list_models()] == [13, 12, 11]
        assert [row["provider_id"] for row in await routing.list_api_models(model_kind=model_kind)] == [2, 2, 1]
        selected = await routing.resolve_api_model("shared-id", provider="nvidia", model_kind=model_kind)
        assert selected["provider_id"] == 2 and selected["model_id"] == 12
        assert selected["api_provider"] == "nvidia" and selected["provider_type"] == "openai"
        assert selected["api_base"] == original["api_base"]
        assert selected["api_key"] == original["api_key"]
        assert (await routing.resolve_api_model("shared-id", provider="openai", model_kind=model_kind))[
            "model_id"
        ] == 11
        with pytest.raises(AmbiguousModelRouteError):
            await routing.resolve_api_model("shared-id", model_kind=model_kind)
        async with factory() as session:
            stored = (await session.execute(select(LlmProvider).where(LlmProvider.id == 2))).scalar_one()
            assert stored.encrypted_api_key == encrypted
            assert stored.encrypted_api_key != "isolated-provider-key"
            assert stored.auth_mode == "api_key"
        for entity in (LlmProvider, LlmModel):
            with pytest.raises(DBAPIError):
                async with factory() as session, session.begin():
                    await session.execute(update(entity).values(sort_order=-1))
        assert [row["id"] for row in await repository.list_models()] == [13, 12, 11]
    finally:
        async with engine.begin() as connection:
            for table in (
                "chat_run_providers",
                "chat_runs",
                "llm_provider_auth_attempts",
                "llm_models",
                "llm_providers",
            ):
                await connection.exec_driver_sql(f"DROP TABLE IF EXISTS {table}")
        await engine.dispose()
