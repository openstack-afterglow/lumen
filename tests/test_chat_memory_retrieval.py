import pytest

from lumen.services import memory_retrieval


@pytest.mark.asyncio
async def test_retrieval_uses_pinned_route_and_single_namespace(monkeypatch):
    captured = {}

    class Index:
        async def search_ids(self, **kwargs):
            captured.update(kwargs)
            return [4, 2]

    async def embed_with_route(**kwargs):
        assert kwargs["route"]["model_name"] == "run-pinned-embed"
        return [0.1, 0.2]

    monkeypatch.setattr(memory_retrieval, "semantic_memory_available", lambda: True)
    monkeypatch.setattr(memory_retrieval, "configured_memory_index", lambda: Index())
    monkeypatch.setattr(memory_retrieval, "embed_with_route", embed_with_route)

    assert await memory_retrieval.candidate_ids(
        query="owned query",
        embedding_route={"model_name": "run-pinned-embed"},
        dimensions=2,
        user_id="user",
        project_id="project",
        workspace_id=7,
        limit=20,
    ) == [4, 2]
    assert captured["user_id"] == "user"
    assert captured["project_id"] == "project"
    assert captured["workspace_id"] == 7
