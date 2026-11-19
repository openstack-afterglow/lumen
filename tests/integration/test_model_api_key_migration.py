"""Migration 038 preserves every legacy key field and enforces machine ownership."""

import os
from uuid import uuid4

import pytest
from sqlalchemy import event
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from lumen.scripts.migrate import MIGRATIONS, _statements, load_manifest

pytestmark = pytest.mark.integration


async def test_key_kind_migration_preserves_active_inactive_expired_and_gateway_rows():
    engine = create_async_engine(os.environ["DATABASE_URL"], pool_size=1, max_overflow=0)
    namespace = f"k_{uuid4().hex[:8]}_"

    @event.listens_for(engine.sync_engine, "before_cursor_execute", retval=True)
    def isolate_table(connection, cursor, statement, parameters, context, executemany):
        return statement.replace("chat_api_keys", f"{namespace}chat_api_keys"), parameters

    migrations = load_manifest()
    target = next(item for item in migrations if item.logical_id == "038-model-api-key-ownership")
    legacy_columns = (
        "id, owner_user_id, owner_project_id, name, key_prefix, key_hash, scopes, "
        "owner_monthly_credit_limit, owner_weekly_credit_limit, admin_monthly_credit_limit, is_active, "
        "last_used_at, revoked_at, expires_at, credential_kind, created_at, updated_at"
    )
    try:
        async with engine.begin() as conn:
            for migration in migrations[:migrations.index(target)]:
                for statement in _statements(MIGRATIONS / migration.relative_path):
                    if statement.startswith(("CREATE TABLE IF NOT EXISTS chat_api_keys", "ALTER TABLE chat_api_keys",
                                             "UPDATE chat_api_keys")):
                        await conn.exec_driver_sql(statement)
            for key_id, active, revoked, expires, credential in (
                (1, True, None, None, "api_key"),
                (2, False, "2026-01-02 00:00:00", None, "api_key"),
                (3, True, None, "2026-01-02 00:00:00", "api_key"),
                (4, True, None, "2099-01-01 00:00:00", "claude_gateway"),
            ):
                await conn.exec_driver_sql(f"""
                    INSERT INTO chat_api_keys ({legacy_columns}) VALUES (
                        %s, 'user-a', 'initial-project', 'legacy', 'sk-afgl-AbCd', %s,
                        '["models:read", "native:tools:execute"]', 4.25, 2.5, 10, %s,
                        '2026-01-01 00:00:00', %s, %s, %s, '2026-01-01 00:00:00', '2026-01-01 01:00:00'
                    )
                """, (key_id, str(key_id) * 64, active, revoked, expires, credential))
            projection = f"SELECT {legacy_columns} FROM chat_api_keys ORDER BY id"
            before = (await conn.exec_driver_sql(projection)).all()
            for statement in _statements(MIGRATIONS / target.relative_path):
                await conn.exec_driver_sql(statement)
            assert (await conn.exec_driver_sql(projection)).all() == before
            assert (await conn.exec_driver_sql(
                "SELECT id, key_kind, owner_domain_id, created_by_user_id FROM chat_api_keys ORDER BY id"
            )).all() == [(key_id, "personal", None, None) for key_id in range(1, 5)]
            with pytest.raises(DBAPIError):
                await conn.exec_driver_sql("UPDATE chat_api_keys SET owner_user_id = NULL WHERE id = 1")
            with pytest.raises(DBAPIError):
                await conn.exec_driver_sql("UPDATE chat_api_keys SET owner_domain_id = 'domain-a' WHERE id = 1")
            with pytest.raises(DBAPIError):
                await conn.exec_driver_sql("UPDATE chat_api_keys SET key_kind = 'service' WHERE id = 1")
            with pytest.raises(DBAPIError):
                await conn.exec_driver_sql("UPDATE chat_api_keys SET key_kind = 'alien' WHERE id = 1")
            await conn.exec_driver_sql("""
                UPDATE chat_api_keys SET key_kind = 'service', owner_user_id = NULL, owner_project_id = NULL,
                    owner_domain_id = 'domain-a', created_by_user_id = 'user-a' WHERE id = 1
            """)
            for statement in _statements(MIGRATIONS / target.relative_path):
                await conn.exec_driver_sql(statement)
            assert (await conn.exec_driver_sql("SELECT key_kind FROM chat_api_keys WHERE id = 1")).scalar_one() == "service"
    finally:
        async with engine.begin() as conn:
            await conn.exec_driver_sql("DROP TABLE IF EXISTS chat_api_keys")
        await engine.dispose()
