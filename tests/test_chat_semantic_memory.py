from types import SimpleNamespace

import pytest

from lumen.services import semantic_memory


@pytest.fixture(autouse=True)
def clear_index_cache():
    getattr(semantic_memory.configured_memory_index, "cache_clear", lambda: None)()
    semantic_memory._ready = False
    yield
    getattr(semantic_memory.configured_memory_index, "cache_clear", lambda: None)()
    semantic_memory._ready = False


def test_semantic_memory_is_unavailable_when_disabled(monkeypatch):
    monkeypatch.setattr(
        semantic_memory,
        "get_settings",
        lambda: SimpleNamespace(chat_semantic_memory_enabled=False),
    )

    assert semantic_memory.semantic_memory_available() is False


def test_semantic_memory_uses_dedicated_pgvector_dsn(monkeypatch):
    monkeypatch.setattr(
        semantic_memory,
        "get_settings",
        lambda: SimpleNamespace(
            chat_semantic_memory_enabled=True,
            chat_memory_pgvector_url="postgresql://memory.example/afterglow_memory",
            chat_memory_embedding_dimensions=1536,
        ),
    )

    index = semantic_memory.configured_memory_index()
    assert index._dsn == "postgresql://memory.example/afterglow_memory"
    assert index._dimensions == 1536


@pytest.mark.asyncio
async def test_semantic_memory_becomes_available_only_after_setup(monkeypatch):
    class Index:
        async def setup(self):
            return None

    monkeypatch.setattr(semantic_memory, "configured_memory_index", lambda: Index())
    assert semantic_memory.semantic_memory_available() is False

    await semantic_memory.setup_semantic_memory()

    assert semantic_memory.semantic_memory_available() is True
