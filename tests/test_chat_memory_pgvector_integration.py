"""Real PostgreSQL/pgvector contract for semantic-memory indexing.

Run with:
AFTERGLOW_TEST_MEMORY_PGVECTOR_URL=postgresql://afterglow:dev@127.0.0.1:5432/afterglow_memory_test \
  pytest tests/test_chat_memory_pgvector_integration.py -v
"""

from __future__ import annotations

import os
import uuid

import pytest

from lumen.services.memory_index import MemoryVector, PgVectorMemoryIndex

pytestmark = pytest.mark.asyncio
_ENV = "AFTERGLOW_TEST_MEMORY_PGVECTOR_URL"


@pytest.fixture
def pgvector_index() -> PgVectorMemoryIndex:
    dsn = os.environ.get(_ENV, "").strip()
    if not dsn:
        pytest.skip(f"{_ENV} is not configured — pgvector integration test skipped")
    return PgVectorMemoryIndex(dsn, dimensions=3)


async def test_pgvector_index_is_content_free_and_namespace_scoped(pgvector_index: PgVectorMemoryIndex):
    await pgvector_index.setup()
    suffix = uuid.uuid4().hex[:12]
    user_id = f"memory-test-{suffix}"
    project_id = f"project-{suffix}"
    other_project_id = f"other-project-{suffix}"

    first_id, second_id, other_id = 10_001, 10_002, 10_003
    for memory_id, project, embedding in (
        (first_id, project_id, [1.0, 0.0, 0.0]),
        (second_id, project_id, [0.8, 0.2, 0.0]),
        (other_id, other_project_id, [0.99, 0.01, 0.0]),
    ):
        await pgvector_index.upsert(
            MemoryVector(
                memory_id=memory_id,
                generation=1,
                user_id=user_id,
                project_id=project,
                workspace_id=None,
                embedding=embedding,
                embedding_model="test-embedding",
                content_hash="a" * 64,
            )
        )

    assert await pgvector_index.required_generations() == [1]
    assert await pgvector_index.search_ids(
        user_id=user_id,
        project_id=project_id,
        workspace_id=None,
        embedding=[1.0, 0.0, 0.0],
        limit=10,
    ) == [first_id, second_id]
    assert await pgvector_index.search_ids(
        user_id=user_id,
        project_id=other_project_id,
        workspace_id=None,
        embedding=[1.0, 0.0, 0.0],
        limit=10,
    ) == [other_id]

    await pgvector_index.delete_namespace(
        generation=1,
        user_id=user_id,
        project_id=project_id,
        workspace_id=None,
    )
    assert (
        await pgvector_index.search_ids(
            user_id=user_id,
            project_id=project_id,
            workspace_id=None,
            embedding=[1.0, 0.0, 0.0],
            limit=10,
        )
        == []
    )
    await pgvector_index.delete(generation=1, memory_id=other_id)
