"""Content-free pgvector index for encrypted MySQL chat memories."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from pgvector.psycopg import register_vector_async
from psycopg import AsyncConnection, sql


@dataclass(frozen=True)
class MemoryVector:
    memory_id: int
    generation: int
    user_id: str
    project_id: str | None
    workspace_id: int | None
    embedding: list[float]
    embedding_model: str
    content_hash: str


class MemoryIndex(Protocol):
    async def setup(self) -> None: ...

    async def search_ids(
        self,
        *,
        user_id: str,
        project_id: str | None,
        workspace_id: int | None,
        embedding: list[float],
        limit: int,
    ) -> list[int]: ...

    async def required_generations(self) -> list[int]: ...
    async def upsert(self, vector: MemoryVector) -> None: ...

    async def delete(self, *, generation: int, memory_id: int) -> None: ...

    async def delete_namespace(
        self, *, generation: int, user_id: str, project_id: str | None, workspace_id: int | None
    ) -> None: ...


class PgVectorMemoryIndex:
    """PostgreSQL index containing IDs, scope fields and vectors—never plaintext."""

    def __init__(self, dsn: str, *, dimensions: int) -> None:
        if dimensions <= 0:
            raise ValueError("memory vector dimensions must be positive")
        self._dsn = dsn
        self._dimensions = dimensions

    async def _connection(self, *, register_vector: bool = True) -> AsyncConnection:
        conn = await AsyncConnection.connect(self._dsn, autocommit=True)
        if register_vector:
            await register_vector_async(conn)
        return conn

    async def setup(self) -> None:
        conn = await self._connection(register_vector=False)
        try:
            async with conn.cursor() as cursor:
                await cursor.execute("SELECT pg_advisory_lock(82171402)")
                try:
                    await cursor.execute("CREATE EXTENSION IF NOT EXISTS vector")
                    await cursor.execute(
                        sql.SQL(
                            "CREATE TABLE IF NOT EXISTS chat_memory_vectors ("
                            "generation INTEGER NOT NULL, memory_id BIGINT NOT NULL, user_id VARCHAR(64) NOT NULL, "
                            "project_id VARCHAR(64) NULL, workspace_id BIGINT NULL, embedding vector({dimensions}) NOT NULL, "
                            "embedding_model VARCHAR(190) NOT NULL, content_hash CHAR(64) NOT NULL, "
                            "updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP, "
                            "PRIMARY KEY (generation, memory_id))"
                        ).format(dimensions=sql.Literal(self._dimensions))
                    )
                    await cursor.execute(
                        "CREATE TABLE IF NOT EXISTS chat_memory_schema ("
                        "singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton), "
                        "active_generation INTEGER NOT NULL DEFAULT 1, target_generation INTEGER NULL)"
                    )
                    await cursor.execute(
                        "ALTER TABLE chat_memory_schema ADD COLUMN IF NOT EXISTS target_generation INTEGER NULL"
                    )
                    await cursor.execute(
                        "INSERT INTO chat_memory_schema (singleton, active_generation, target_generation) VALUES (TRUE, 1, NULL) "
                        "ON CONFLICT (singleton) DO NOTHING"
                    )
                    await cursor.execute(
                        "CREATE INDEX IF NOT EXISTS idx_chat_memory_vectors_scope "
                        "ON chat_memory_vectors (generation, user_id, project_id, workspace_id)"
                    )
                finally:
                    await cursor.execute("SELECT pg_advisory_unlock(82171402)")
        finally:
            await conn.close()

    async def search_ids(
        self,
        *,
        user_id: str,
        project_id: str | None,
        workspace_id: int | None,
        embedding: list[float],
        limit: int,
    ) -> list[int]:
        if not 1 <= limit <= 100:
            raise ValueError("memory search limit must be between 1 and 100")
        conn = await self._connection()
        try:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "SELECT memory_id FROM chat_memory_vectors "
                    "WHERE generation = (SELECT active_generation FROM chat_memory_schema WHERE singleton = TRUE) "
                    "AND user_id = %s AND project_id IS NOT DISTINCT FROM %s "
                    "AND workspace_id IS NOT DISTINCT FROM %s ORDER BY embedding <=> %s::vector LIMIT %s",
                    (user_id, project_id, workspace_id, embedding, limit),
                )
                return [row[0] async for row in cursor]
        finally:
            await conn.close()

    async def required_generations(self) -> list[int]:
        conn = await self._connection()
        try:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "SELECT active_generation, target_generation FROM chat_memory_schema WHERE singleton = TRUE"
                )
                row = await cursor.fetchone()
                if row is None:
                    raise RuntimeError("semantic memory schema is not initialized")
                return [value for value in row if value is not None]
        finally:
            await conn.close()

    async def upsert(self, vector: MemoryVector) -> None:
        if len(vector.embedding) != self._dimensions:
            raise ValueError("memory embedding dimensions do not match index")
        conn = await self._connection()
        try:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "INSERT INTO chat_memory_vectors (generation, memory_id, user_id, project_id, workspace_id, embedding, embedding_model, content_hash) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (generation, memory_id) DO UPDATE SET user_id = EXCLUDED.user_id, project_id = EXCLUDED.project_id, "
                    "workspace_id = EXCLUDED.workspace_id, embedding = EXCLUDED.embedding, embedding_model = EXCLUDED.embedding_model, "
                    "content_hash = EXCLUDED.content_hash, updated_at = CURRENT_TIMESTAMP",
                    (
                        vector.generation,
                        vector.memory_id,
                        vector.user_id,
                        vector.project_id,
                        vector.workspace_id,
                        vector.embedding,
                        vector.embedding_model,
                        vector.content_hash,
                    ),
                )
        finally:
            await conn.close()

    async def delete(self, *, generation: int, memory_id: int) -> None:
        conn = await self._connection()
        try:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "DELETE FROM chat_memory_vectors WHERE generation = %s AND memory_id = %s", (generation, memory_id)
                )
        finally:
            await conn.close()

    async def delete_namespace(
        self, *, generation: int, user_id: str, project_id: str | None, workspace_id: int | None
    ) -> None:
        conn = await self._connection()
        try:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "DELETE FROM chat_memory_vectors WHERE generation = %s AND user_id = %s AND project_id IS NOT DISTINCT FROM %s "
                    "AND workspace_id IS NOT DISTINCT FROM %s",
                    (generation, user_id, project_id, workspace_id),
                )
        finally:
            await conn.close()
