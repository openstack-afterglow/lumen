"""Semantic-memory availability and pgvector-index construction."""

from __future__ import annotations

from functools import lru_cache

from lumen.config import get_settings
from lumen.services.memory_index import PgVectorMemoryIndex

_ready = False


class SemanticMemoryUnavailable(RuntimeError):
    pass


@lru_cache(maxsize=1)
def configured_memory_index() -> PgVectorMemoryIndex:
    settings = get_settings()
    if not settings.chat_semantic_memory_enabled:
        raise SemanticMemoryUnavailable("semantic memory is disabled")
    if not settings.chat_memory_pgvector_url or settings.chat_memory_embedding_dimensions <= 0:
        raise SemanticMemoryUnavailable("semantic memory configuration is incomplete")
    return PgVectorMemoryIndex(
        settings.chat_memory_pgvector_url,
        dimensions=settings.chat_memory_embedding_dimensions,
    )


async def setup_semantic_memory() -> None:
    """Mark retrieval available only after the dedicated index schema is ready."""
    global _ready
    index = configured_memory_index()
    await index.setup()
    _ready = True


def semantic_memory_available() -> bool:
    return _ready
