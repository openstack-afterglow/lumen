import pytest

from lumen.services.memory_index import MemoryVector, PgVectorMemoryIndex


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
