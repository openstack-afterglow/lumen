import pytest

from lumen.services import semantic_memory


@pytest.fixture(autouse=True)
def clear_ready_flag():
    semantic_memory._ready = False
    yield
    semantic_memory._ready = False


def test_semantic_memory_is_unavailable_when_the_plugin_index_is_not_configured(monkeypatch):
    monkeypatch.setattr(semantic_memory.memory_host, "configured_index", lambda: None)

    with pytest.raises(semantic_memory.SemanticMemoryUnavailable):
        semantic_memory.configured_memory_index()
    assert semantic_memory.semantic_memory_available() is False


def test_semantic_memory_uses_the_selected_plugins_index(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(semantic_memory.memory_host, "configured_index", lambda: sentinel)

    assert semantic_memory.configured_memory_index() is sentinel


@pytest.mark.asyncio
async def test_semantic_memory_becomes_available_only_after_setup(monkeypatch):
    class Index:
        async def setup(self):
            return None

    monkeypatch.setattr(semantic_memory, "configured_memory_index", lambda: Index())
    assert semantic_memory.semantic_memory_available() is False

    await semantic_memory.setup_semantic_memory()

    assert semantic_memory.semantic_memory_available() is True
