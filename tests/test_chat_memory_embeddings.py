import pytest

from lumen.services import memory_embeddings


@pytest.mark.asyncio
async def test_embedding_uses_caller_supplied_route(monkeypatch):
    captured = {}

    class Response:
        data = [{"embedding": [0.1, 0.2]}]

    async def fake_embedding(**kwargs):
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr(memory_embeddings.litellm, "aembedding", fake_embedding)
    route = {
        "model_name": "embed",
        "api_key": "secret",
        "api_base": "https://provider.example",
        "provider_type": "openai",
    }

    assert await memory_embeddings.embed_with_route(route=route, text="query", dimensions=2) == [0.1, 0.2]
    assert captured["model"] == "embed"
    assert captured["api_key"] == "secret"


@pytest.mark.asyncio
async def test_embedding_rejects_dimension_mismatch_without_fallback(monkeypatch):
    class Response:
        data = [{"embedding": [0.1]}]

    async def fake_embedding(**_kwargs):
        return Response()

    monkeypatch.setattr(memory_embeddings.litellm, "aembedding", fake_embedding)
    with pytest.raises(memory_embeddings.EmbeddingUnavailable, match="dimension"):
        await memory_embeddings.embed_with_route(
            route={"model_name": "embed", "api_key": "secret", "api_base": None, "provider_type": "openai"},
            text="query",
            dimensions=2,
        )
