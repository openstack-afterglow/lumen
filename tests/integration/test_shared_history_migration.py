"""Exercise migration 019 against real MariaDB, without changing the shared schema."""

from __future__ import annotations

import os
import uuid
from types import SimpleNamespace

import pytest
from lumen_plugin_api.database import DatabaseConfig
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from lumen.plugins.registry import get_plugin
from lumen.scripts import migrate as migration_runner
from lumen.scripts.migrate import MIGRATIONS, MigrationLedgerError, _statements, _verify_active_paths, load_manifest

pytestmark = pytest.mark.integration


class _MigrationDatabase:
    """Namespace real tables and constraints so parallel integration runs stay isolated."""

    def __init__(self, connection):
        self.connection = connection
        self.prefix = f"m{uuid.uuid4().hex[:10]}_"

    async def exec_driver_sql(self, statement, parameters=()):
        return await self.connection.exec_driver_sql(self.namespace(statement), parameters)

    def namespace(self, statement):
        return statement.replace("chat_", f"{self.prefix}chat_").replace(
            "schema_migrations", f"{self.prefix}schema_migrations"
        )

    async def run(self, monkeypatch):
        """Run the production orchestrator against this real isolated legacy schema."""
        migration = next(item for item in load_manifest() if item.logical_id == "019-shared-message-membership")
        await self.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                logical_id VARCHAR(100) PRIMARY KEY,
                relative_path VARCHAR(255) NOT NULL,
                sha256 CHAR(64) NOT NULL,
                applied_at DATETIME(6) NOT NULL
            ) ENGINE=InnoDB
        """)
        await self.exec_driver_sql("""
            INSERT INTO schema_migrations
            SELECT '012-chat-history-path', '012_chat_history_path.sql', REPEAT('0', 64), NOW(6)
            WHERE NOT EXISTS (SELECT 1 FROM schema_migrations WHERE logical_id = '012-chat-history-path')
        """)
        await self.connection.commit()
        handle = get_plugin("database").open(DatabaseConfig(url=os.environ["DATABASE_URL"]))

        def namespace_sql(connection, cursor, statement, parameters, context, executemany):
            return self.namespace(statement), parameters

        event.listen(handle.engine.sync_engine, "before_cursor_execute", namespace_sql, retval=True)
        with monkeypatch.context() as patch:
            patch.setattr(migration_runner, "load_manifest", lambda: [migration])
            patch.setattr(migration_runner, "get_plugin", lambda name: SimpleNamespace(open=lambda config: handle))
            return await migration_runner.migrate(os.environ["DATABASE_URL"], apply=True)

    async def apply(self, *, stop_after=None):
        migration = next(item for item in load_manifest() if item.logical_id == "019-shared-message-membership")
        interrupted = False
        for statement in _statements(MIGRATIONS / migration.relative_path):
            await self.exec_driver_sql(statement)
            if stop_after is not None and stop_after in statement:
                interrupted = True
            if interrupted and (statement.startswith("DEALLOCATE") or statement.startswith("INSERT INTO")):
                break
        await self.connection.commit()

    async def snapshot(self):
        return {
            table: (await self.exec_driver_sql(f"SELECT {columns} FROM {table} ORDER BY {order}")).all()
            for table, columns, order in (
                (
                    "chat_conversations",
                    "id, user_id, project_id, active_leaf_id, history_revision, history_index_ready",
                    "id",
                ),
                (
                    "chat_messages",
                    "id, conversation_id, parent_id, content, tool_calls, citations, reasoning, attachments, parts, "
                    "role, created_at",
                    "id",
                ),
                ("chat_conversation_active_path", "conversation_id, position, message_id", "conversation_id, position"),
            )
        }


@pytest.fixture
async def migration_engine():
    engine = create_async_engine(os.environ["DATABASE_URL"], pool_size=1, max_overflow=0)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def legacy_history(migration_engine):
    async with migration_engine.connect() as connection:
        database = _MigrationDatabase(connection)
        try:
            await database.exec_driver_sql("""
                CREATE TABLE chat_conversations (
                    id CHAR(36) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin PRIMARY KEY,
                    user_id VARCHAR(64) NOT NULL,
                    project_id VARCHAR(64) NOT NULL,
                    active_leaf_id BIGINT NULL,
                    history_revision BIGINT NOT NULL,
                    history_index_ready BOOLEAN NOT NULL
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """)
            # An unnamed legacy FK deliberately exercises metadata discovery rather
            # than assuming either the baseline or SQLAlchemy-generated FK name.
            await database.exec_driver_sql("""
                CREATE TABLE chat_messages (
                    id BIGINT PRIMARY KEY,
                    conversation_id CHAR(36) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
                    parent_id BIGINT NULL,
                    content MEDIUMTEXT,
                    tool_calls MEDIUMTEXT,
                    citations MEDIUMTEXT,
                    reasoning MEDIUMTEXT,
                    attachments MEDIUMTEXT,
                    parts MEDIUMTEXT,
                    role VARCHAR(20) NOT NULL DEFAULT 'user',
                    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
                    FOREIGN KEY (conversation_id) REFERENCES chat_conversations(id) ON DELETE CASCADE
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """)
            await database.exec_driver_sql("""
                CREATE TABLE chat_conversation_active_path (
                    conversation_id CHAR(36) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
                    position BIGINT NOT NULL,
                    message_id BIGINT NOT NULL,
                    PRIMARY KEY (conversation_id, position),
                    UNIQUE (conversation_id, message_id),
                    FOREIGN KEY (conversation_id) REFERENCES chat_conversations(id) ON DELETE CASCADE,
                    FOREIGN KEY (message_id) REFERENCES chat_messages(id) ON DELETE CASCADE
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """)
            await database.exec_driver_sql("""
                CREATE TABLE chat_message_assets (
                    message_id BIGINT NOT NULL,
                    asset_id BIGINT NOT NULL,
                    PRIMARY KEY (message_id, asset_id),
                    FOREIGN KEY (message_id) REFERENCES chat_messages(id) ON DELETE CASCADE
                ) ENGINE=InnoDB
            """)
            await database.exec_driver_sql("""
                INSERT INTO chat_conversations VALUES
                    ('source', 'owner', 'project', 102, 73, TRUE),
                    ('empty', 'other-owner', 'other-project', NULL, 9, TRUE)
            """)
            await database.exec_driver_sql("""
                INSERT INTO chat_messages
                    (id, conversation_id, parent_id, content, tool_calls, citations, reasoning, attachments, parts)
                VALUES
                    (101, 'source', NULL, 'cipher-root', 'cipher-tools', 'cipher-citations',
                     'cipher-reasoning', 'cipher-attachments', 'cipher-parts'),
                    (102, 'source', 101, 'cipher-answer', NULL, NULL, NULL, NULL, NULL),
                    (103, 'source', 101, 'cipher-inactive-sibling', NULL, NULL, NULL, NULL, NULL)
            """)
            await database.exec_driver_sql("INSERT INTO chat_message_assets VALUES (101, 501), (102, 502)")
            await database.exec_driver_sql("""
                INSERT INTO chat_conversation_active_path VALUES ('source', 0, 101), ('source', 1, 102)
            """)
            await connection.commit()
            yield database
        finally:
            await connection.rollback()
            for table in (
                "chat_conversation_active_path",
                "chat_conversation_messages",
                "chat_message_assets",
                "chat_messages",
                "chat_conversations",
                "chat_message_graphs",
                "schema_migrations",
            ):
                await database.exec_driver_sql(f"DROP TABLE IF EXISTS {table}")
            await connection.commit()


@pytest.mark.parametrize(
    "stop_after",
    [
        None,
        "CREATE TABLE IF NOT EXISTS chat_message_graphs",
        "INSERT INTO chat_message_graphs",
        "MODIFY COLUMN graph_id",
        "fk_chat_messages_graph",
        "DROP FOREIGN KEY",
        "MODIFY COLUMN conversation_id",
        "CREATE TABLE IF NOT EXISTS chat_conversation_messages",
        "INSERT INTO chat_conversation_messages",
    ],
)
async def test_membership_migration_resumes_without_rewriting_history(legacy_history, stop_after):
    database = legacy_history
    before = await database.snapshot()
    if stop_after is not None:
        await database.apply(stop_after=stop_after)
    await database.apply()
    await database.apply()

    assert await database.snapshot() == before
    memberships = await database.exec_driver_sql(
        "SELECT conversation_id, message_id FROM chat_conversation_messages ORDER BY conversation_id, message_id"
    )
    assert memberships.all() == [("source", 101), ("source", 102), ("source", 103)]
    collation = await database.exec_driver_sql("""
        SELECT COLLATION_NAME FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'chat_conversation_messages'
          AND COLUMN_NAME = 'conversation_id'
    """)
    assert collation.scalar_one() == "utf8mb4_bin"
    graphs = await database.exec_driver_sql("SELECT id, user_id, project_id FROM chat_message_graphs ORDER BY id")
    assert graphs.all() == [("empty", "other-owner", "other-project"), ("source", "owner", "project")]
    mapped = await database.exec_driver_sql("SELECT id, graph_id FROM chat_conversations ORDER BY id")
    assert mapped.all() == [("empty", "empty"), ("source", "source")]
    messages = await database.exec_driver_sql("SELECT id, graph_id FROM chat_messages ORDER BY id")
    assert messages.all() == [(101, "source"), (102, "source"), (103, "source")]
    await _verify_active_paths(database)


async def test_origin_deletion_preserves_shared_history_until_last_graph_mapping(legacy_history):
    database = legacy_history
    await database.apply()
    await database.exec_driver_sql("""
        INSERT INTO chat_conversations
            (id, user_id, project_id, active_leaf_id, history_revision, history_index_ready, graph_id)
        VALUES ('fork', 'owner', 'project', 102, 24, TRUE, 'source')
    """)
    await database.exec_driver_sql("INSERT INTO chat_conversation_messages VALUES ('fork', 101), ('fork', 102)")
    await database.exec_driver_sql("INSERT INTO chat_conversation_active_path VALUES ('fork', 0, 101), ('fork', 1, 102)")
    before = await database.snapshot()
    await database.apply()
    assert await database.snapshot() == before
    inherited = await database.exec_driver_sql(
        "SELECT message_id FROM chat_conversation_messages WHERE conversation_id = 'fork' ORDER BY message_id"
    )
    assert inherited.scalars().all() == [101, 102]
    # Cross-origin reachability is valid before source deletion as well as after it.
    await _verify_active_paths(database)
    await database.exec_driver_sql("DELETE FROM chat_conversations WHERE id = 'source'")
    await _verify_active_paths(database)
    await database.connection.commit()

    rows = await database.exec_driver_sql("SELECT id, conversation_id, parent_id, content FROM chat_messages ORDER BY id")
    assert rows.all() == [
        (101, None, None, "cipher-root"),
        (102, None, 101, "cipher-answer"),
        (103, None, 101, "cipher-inactive-sibling"),
    ]
    path = await database.exec_driver_sql("SELECT position, message_id FROM chat_conversation_active_path ORDER BY position")
    assert path.all() == [(0, 101), (1, 102)]
    revision = await database.exec_driver_sql("SELECT history_revision FROM chat_conversations WHERE id = 'fork'")
    assert revision.scalar_one() == 24
    with pytest.raises(IntegrityError):
        await database.exec_driver_sql("DELETE FROM chat_message_graphs WHERE id = 'source'")
    await database.connection.rollback()
    retained = await database.exec_driver_sql(
        "SELECT message_id FROM chat_conversation_messages WHERE conversation_id = 'fork' ORDER BY message_id"
    )
    assert retained.scalars().all() == [101, 102]
    assets = await database.exec_driver_sql("SELECT message_id, asset_id FROM chat_message_assets ORDER BY message_id")
    assert assets.all() == [(101, 501), (102, 502)]
    await database.exec_driver_sql("DELETE FROM chat_conversations WHERE id = 'fork'")
    await database.exec_driver_sql("DELETE FROM chat_message_graphs WHERE id = 'source'")
    assert (await database.exec_driver_sql("SELECT id FROM chat_messages")).all() == []
    assert (await database.exec_driver_sql("SELECT message_id FROM chat_message_assets")).all() == []
    assert (await database.exec_driver_sql("SELECT message_id FROM chat_conversation_messages")).all() == []
    assert (await database.exec_driver_sql("SELECT id FROM chat_message_graphs")).scalars().all() == ["empty"]


@pytest.mark.parametrize(
    ("corruption", "reason"),
    [
        ("DELETE FROM chat_conversation_messages WHERE message_id = 102", "no reachable membership"),
        ("UPDATE chat_messages SET parent_id = NULL WHERE id = 102", "parent chain is invalid"),
        ("DELETE FROM chat_conversation_active_path", "active leaf membership is invalid"),
        ("UPDATE chat_conversations SET user_id = 'intruder' WHERE id = 'source'", "graph ownership is invalid"),
        ("INSERT INTO chat_conversation_messages VALUES ('empty', 102)", "crosses graph ownership"),
    ],
)
async def test_integrity_rejects_origin_only_access_and_broken_ready_paths(legacy_history, corruption, reason):
    database = legacy_history
    await database.apply()
    await database.exec_driver_sql(corruption)
    with pytest.raises(MigrationLedgerError, match=reason):
        await _verify_active_paths(database)


async def test_legacy_copied_forks_keep_separate_owner_graphs(legacy_history):
    database = legacy_history
    await database.exec_driver_sql("""
        INSERT INTO chat_conversations VALUES ('legacy-copy', 'owner', 'project', 201, 41, TRUE)
    """)
    await database.exec_driver_sql("""
        INSERT INTO chat_messages
            (id, conversation_id, parent_id, content, tool_calls, citations, reasoning, attachments, parts)
        SELECT 201, 'legacy-copy', parent_id, content, tool_calls, citations, reasoning, attachments, parts
        FROM chat_messages WHERE id = 101
    """)
    await database.exec_driver_sql("INSERT INTO chat_conversation_active_path VALUES ('legacy-copy', 0, 201)")
    before = await database.snapshot()
    await database.apply()
    assert await database.snapshot() == before
    copies = await database.exec_driver_sql("""
        SELECT id, graph_id, content FROM chat_messages WHERE id IN (101, 201) ORDER BY id
    """)
    assert copies.all() == [(101, "source", "cipher-root"), (201, "legacy-copy", "cipher-root")]
    await _verify_active_paths(database)


@pytest.mark.parametrize(
    ("corruption", "reason"),
    [
        ("UPDATE chat_messages SET graph_id = '0' WHERE id = 102", "message graph ownership is invalid"),
        ("UPDATE chat_conversations SET graph_id = '0' WHERE id = 'source'", "conversation graph ownership is invalid"),
        ("UPDATE chat_message_graphs SET user_id = 'intruder' WHERE id = 'source'", "graph ownership is invalid"),
        (
            "INSERT INTO chat_message_graphs VALUES ('orphan', 'owner', 'project')",
            "graph has no conversation mapping",
        ),
    ],
)
async def test_runner_rejects_bad_graph_backfill_before_constraints_and_ledger(
    legacy_history, monkeypatch, corruption, reason
):
    database = legacy_history
    await database.apply(stop_after="INSERT INTO chat_message_graphs")
    await database.exec_driver_sql(corruption)
    before = await database.snapshot()
    with pytest.raises(MigrationLedgerError, match=reason):
        await database.run(monkeypatch)
    assert await database.snapshot() == before
    columns = await database.exec_driver_sql("""
        SELECT TABLE_NAME, IS_NULLABLE FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME IN ('chat_conversations', 'chat_messages')
          AND COLUMN_NAME = 'graph_id'
        ORDER BY TABLE_NAME
    """)
    assert columns.all() == [
        (f"{database.prefix}chat_conversations", "YES"),
        (f"{database.prefix}chat_messages", "YES"),
    ]
    ledger = await database.exec_driver_sql("""
        SELECT logical_id FROM schema_migrations WHERE logical_id = '019-shared-message-membership'
    """)
    assert ledger.all() == []


async def test_runner_records_success_only_after_ready_path_validation_and_resumes(legacy_history, monkeypatch):
    database = legacy_history
    before = await database.snapshot()
    await database.exec_driver_sql("DELETE FROM chat_conversation_active_path WHERE message_id = 102")
    with pytest.raises(MigrationLedgerError, match="active leaf membership is invalid"):
        await database.run(monkeypatch)
    ledger = await database.exec_driver_sql("""
        SELECT logical_id FROM schema_migrations WHERE logical_id = '019-shared-message-membership'
    """)
    assert ledger.all() == []
    path = await database.exec_driver_sql("SELECT message_id FROM chat_conversation_active_path")
    assert path.scalars().all() == [101]

    await database.exec_driver_sql("INSERT INTO chat_conversation_active_path VALUES ('source', 1, 102)")
    assert await database.run(monkeypatch) == (["019-shared-message-membership"], 0)
    assert await database.snapshot() == before
    assert await database.run(monkeypatch) == ([], 0)


async def test_runner_rejects_cross_graph_membership_before_ledger(legacy_history, monkeypatch):
    database = legacy_history
    await database.apply()
    await database.exec_driver_sql("INSERT INTO chat_conversation_messages VALUES ('empty', 102)")
    with pytest.raises(MigrationLedgerError, match="crosses graph ownership"):
        await database.run(monkeypatch)
    ledger = await database.exec_driver_sql("""
        SELECT logical_id FROM schema_migrations WHERE logical_id = '019-shared-message-membership'
    """)
    assert ledger.all() == []
