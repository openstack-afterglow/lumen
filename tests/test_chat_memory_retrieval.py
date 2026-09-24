import pytest

from lumen.services import memory_retrieval


@pytest.mark.asyncio
async def test_candidate_ids_delegates_one_exact_namespace_semantic_recall(monkeypatch):
    captured = {}

    async def fake_recall_candidate_ids(*, namespace, strategy, query, limit, **_kwargs):
        captured["namespace"] = namespace
        captured["strategy"] = strategy
        captured["query"] = query
        captured["limit"] = limit
        return [4, 2]

    monkeypatch.setattr(memory_retrieval.memory_host, "recall_candidate_ids", fake_recall_candidate_ids)

    assert (
        await memory_retrieval.candidate_ids(
            query="owned query",
            user_id="user",
            project_id="project",
            workspace_id=7,
            limit=20,
        )
        == [4, 2]
    )
    assert captured["strategy"] == "semantic"
    assert captured["query"] == "owned query"
    assert captured["limit"] == 20
    assert captured["namespace"].user_id == "user"
    assert captured["namespace"].project_id == "project"
    assert captured["namespace"].workspace_id == 7
    assert captured["namespace"].include_account is False
