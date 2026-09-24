import pytest
from lumen_memory_default.index import PgVectorMemoryIndex
from lumen_plugin_api.memory import MemoryVector


def test_index_rejects_invalid_dimension_and_search_limit():
    with pytest.raises(ValueError, match="dimensions"):
        PgVectorMemoryIndex("postgresql://unused", dimensions=0)


@pytest.mark.asyncio
async def test_upsert_rejects_mismatched_embedding_without_connecting():
    index = PgVectorMemoryIndex("postgresql://unused", dimensions=3)
    vector = MemoryVector(
        memory_id=1,
        generation=1,
        user_id="user",
        project_id=None,
        workspace_id=None,
        embedding=[0.1, 0.2],
        embedding_model="embed",
        content_hash="a" * 64,
    )

    with pytest.raises(ValueError, match="dimensions"):
        await index.upsert(vector)


@pytest.mark.asyncio
async def test_search_ids_rejects_mismatched_query_embedding_without_connecting():
    index = PgVectorMemoryIndex("postgresql://unused", dimensions=3)

    with pytest.raises(ValueError, match="dimensions"):
        await index.search_ids(
            user_id="user", project_id=None, workspace_id=None, embedding=[0.1, 0.2], limit=10
        )


@pytest.mark.asyncio
async def test_search_ids_rejects_out_of_range_limit_without_connecting():
    index = PgVectorMemoryIndex("postgresql://unused", dimensions=3)
    embedding = [0.1, 0.2, 0.3]

    for limit in (0, 101):
        with pytest.raises(ValueError, match="limit"):
            await index.search_ids(
                user_id="user", project_id=None, workspace_id=None, embedding=embedding, limit=limit
            )

