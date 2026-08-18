"""Pinned embedding boundary for semantic memory."""

from __future__ import annotations

import litellm

from lumen.config import get_settings
from lumen.services.providers import routing as provider_store


class EmbeddingUnavailable(RuntimeError):
    pass


async def embed_with_route(*, route: dict, text: str, dimensions: int) -> list[float]:
    """Embed with a caller-owned immutable route; callers own pricing and snapshot policy."""
    if not text.strip() or dimensions <= 0:
        raise EmbeddingUnavailable("semantic memory embedding route is unavailable")
    try:
        response = await litellm.aembedding(
            model=route["model_name"],
            input=[text],
            api_key=route["api_key"],
            api_base=route["api_base"],
            custom_llm_provider=route["provider_type"],
        )
        data = response.data[0]
        vector = data["embedding"] if isinstance(data, dict) else data.embedding
        values = [float(value) for value in vector]
    except Exception as exc:
        raise EmbeddingUnavailable("semantic memory embedding failed") from exc
    if len(values) != dimensions:
        raise EmbeddingUnavailable("semantic memory embedding dimension mismatch")
    return values


async def embed_maintenance(text: str) -> list[float]:
    """Use the current configured embedding route only for non-user-billed maintenance."""
    settings = get_settings()
    if not settings.chat_memory_embedding_model:
        raise EmbeddingUnavailable("semantic memory embedding route is unavailable")
    route = await provider_store.resolve_model(settings.chat_memory_embedding_model)
    if route is None:
        raise EmbeddingUnavailable("semantic memory embedding route is unavailable")
    return await embed_with_route(
        route=route,
        text=text,
        dimensions=settings.chat_memory_embedding_dimensions,
    )
