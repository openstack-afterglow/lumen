"""Checksum-verified Lumen schema migration runner."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from lumen.config import get_settings
from lumen.scripts.backfill_history import backfill_history, count_unready

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
MANIFEST = MIGRATIONS / "manifest.txt"


class MigrationLedgerError(RuntimeError):
    pass


@dataclass(frozen=True)
class Migration:
    logical_id: str
    relative_path: str
    sha256: str


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_manifest(path: Path = MANIFEST) -> list[Migration]:
    if not path.is_file():
        raise MigrationLedgerError(f"migration manifest is missing: {path}")
    migrations: list[Migration] = []
    seen_ids: set[str] = set()
    seen_paths: set[str] = set()
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        fields = [field.strip() for field in line.split("|")]
        if len(fields) != 3:
            raise MigrationLedgerError(f"manifest line {line_number} must contain logical_id|path|sha256")
        logical_id, relative_path, checksum = fields
        if not logical_id or not relative_path or len(checksum) != 64:
            raise MigrationLedgerError(f"manifest line {line_number} is malformed")
        if logical_id in seen_ids or relative_path in seen_paths:
            raise MigrationLedgerError(f"manifest line {line_number} duplicates an immutable identity")
        migration_path = MIGRATIONS / relative_path
        if migration_path.parent != MIGRATIONS or not migration_path.is_file():
            raise MigrationLedgerError(f"manifest line {line_number} references an invalid migration path")
        actual = _sha256(migration_path)
        if actual != checksum:
            raise MigrationLedgerError(f"checksum drift for {logical_id}: manifest={checksum} actual={actual}")
        seen_ids.add(logical_id)
        seen_paths.add(relative_path)
        migrations.append(Migration(logical_id, relative_path, checksum))
    unlisted = {path.name for path in MIGRATIONS.glob("*.sql")} - seen_paths
    if unlisted:
        raise MigrationLedgerError(f"migration files absent from manifest: {', '.join(sorted(unlisted))}")
    if not migrations:
        raise MigrationLedgerError("migration manifest is empty")
    return migrations


def _statements(path: Path) -> list[str]:
    sql = "\n".join(
        line for line in path.read_text(encoding="utf-8").splitlines() if not line.lstrip().startswith("--")
    )
    return [statement.strip() for statement in sql.split(";") if statement.strip()]


async def _verify_active_paths(connection) -> None:
    """Fail closed when the additive active-path projection does not match immutable ancestry."""
    checks = (
        (
            """
            SELECT conversation.id
            FROM chat_conversations AS conversation
            LEFT JOIN chat_conversation_active_path AS path
              ON path.conversation_id = conversation.id
            GROUP BY conversation.id, conversation.active_leaf_id
            HAVING (conversation.active_leaf_id IS NULL AND COUNT(path.message_id) <> 0)
                OR (conversation.active_leaf_id IS NOT NULL
                    AND SUM(path.message_id = conversation.active_leaf_id) <> 1)
            LIMIT 1
            """,
            "active leaf membership is invalid",
        ),
        (
            """
            SELECT path.conversation_id
            FROM chat_conversation_active_path AS path
            JOIN chat_messages AS message ON message.id = path.message_id
            WHERE message.conversation_id <> path.conversation_id
            LIMIT 1
            """,
            "projected message belongs to another conversation",
        ),
        (
            """
            SELECT path.conversation_id
            FROM chat_conversation_active_path AS path
            GROUP BY path.conversation_id
            HAVING MIN(path.position) <> 0 OR MAX(path.position) <> COUNT(*) - 1
            LIMIT 1
            """,
            "active path positions are not contiguous",
        ),
        (
            """
            SELECT current_path.conversation_id
            FROM chat_conversation_active_path AS current_path
            JOIN chat_messages AS current_message ON current_message.id = current_path.message_id
            LEFT JOIN chat_conversation_active_path AS previous_path
              ON previous_path.conversation_id = current_path.conversation_id
             AND previous_path.position = current_path.position - 1
            WHERE (current_path.position = 0 AND current_message.parent_id IS NOT NULL)
               OR (current_path.position > 0
                   AND (previous_path.message_id IS NULL
                        OR current_message.parent_id <> previous_path.message_id))
            LIMIT 1
            """,
            "active path parent chain is invalid",
        ),
        (
            """
            SELECT conversation.id
            FROM chat_conversations AS conversation
            JOIN (
                SELECT conversation_id, MAX(position) AS leaf_position
                FROM chat_conversation_active_path
                GROUP BY conversation_id
            ) AS terminal ON terminal.conversation_id = conversation.id
            JOIN chat_conversation_active_path AS leaf
              ON leaf.conversation_id = terminal.conversation_id
             AND leaf.position = terminal.leaf_position
            WHERE conversation.active_leaf_id <> leaf.message_id
            LIMIT 1
            """,
            "projected terminal message differs from active leaf",
        ),
    )
    for statement, reason in checks:
        row = (await connection.exec_driver_sql(statement)).first()
        if row is not None:
            raise MigrationLedgerError(f"{reason}: conversation_id={row[0]}")


async def migrate(database_url: str, *, apply: bool) -> tuple[list[str], int]:
    if not database_url:
        raise MigrationLedgerError("database URL is required")
    migrations = load_manifest()
    engine = create_async_engine(database_url, pool_pre_ping=True)
    pending: list[str] = []
    try:
        async with engine.begin() as connection:
            await connection.exec_driver_sql(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                  logical_id VARCHAR(100) NOT NULL PRIMARY KEY,
                  relative_path VARCHAR(255) NOT NULL,
                  sha256 CHAR(64) NOT NULL,
                  applied_at DATETIME(6) NOT NULL
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
        for migration in migrations:
            async with engine.begin() as connection:
                row = (
                    await connection.execute(
                        text("SELECT relative_path, sha256 FROM schema_migrations WHERE logical_id = :logical_id"),
                        {"logical_id": migration.logical_id},
                    )
                ).first()
                if row is not None:
                    if row.relative_path != migration.relative_path or row.sha256 != migration.sha256:
                        raise MigrationLedgerError(f"applied migration identity drift: {migration.logical_id}")
                    continue
                pending.append(migration.logical_id)
                if not apply:
                    continue
                for statement in _statements(MIGRATIONS / migration.relative_path):
                    await connection.exec_driver_sql(statement)
                await connection.execute(
                    text(
                        "INSERT INTO schema_migrations (logical_id, relative_path, sha256, applied_at) "
                        "VALUES (:logical_id, :relative_path, :sha256, NOW(6))"
                    ),
                    {
                        "logical_id": migration.logical_id,
                        "relative_path": migration.relative_path,
                        "sha256": migration.sha256,
                    },
                )
        async with engine.begin() as connection:
            active_path_applied = (
                await connection.execute(
                    text("SELECT 1 FROM schema_migrations WHERE logical_id = '012-chat-history-path'")
                )
            ).scalar_one_or_none()
        if active_path_applied is not None:
            if apply:
                await backfill_history(engine)
            unready = await count_unready(engine)
            if unready == 0:
                async with engine.begin() as connection:
                    await _verify_active_paths(connection)
        else:
            async with engine.connect() as connection:
                unready = int((await connection.execute(text("SELECT COUNT(*) FROM chat_conversations"))).scalar_one())
        return pending, unready
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default="")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    database_url = args.database_url or get_settings().database_url
    pending, unready = asyncio.run(migrate(database_url, apply=args.apply))
    if pending and not args.apply:
        raise SystemExit(f"pending Lumen migrations: {', '.join(pending)}; unready conversation histories: {unready}")
    if unready:
        raise SystemExit(f"unready conversation histories: {unready}")


if __name__ == "__main__":
    main()
