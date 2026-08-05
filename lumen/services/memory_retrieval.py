"""Run-owned semantic candidate lookup; hydration remains in MySQL."""

from __future__ import annotations

from lumen.services.memory_embeddings import embed_with_route
from lumen.services.semantic_memory import (
    SemanticMemoryUnavailable,
    configured_memory_index,
    semantic_memory_available,
)


async def candidate_ids(
    *,
    query: str,
    embedding_route: dict,
    dimensions: int,
    user_id: str,
    project_id: str | None,
    workspace_id: int | None,
    limit: int,
) -> list[int]:
    """Return only vector IDs for one exact namespace; plaintext stays in MySQL."""
    if not semantic_memory_available():
        raise SemanticMemoryUnavailable("semantic memory is unavailable")
    embedding = await embed_with_route(route=embedding_route, text=query, dimensions=dimensions)
    return await configured_memory_index().search_ids(
        user_id=user_id,
        project_id=project_id,
        workspace_id=workspace_id,
        embedding=embedding,
        limit=limit,
    )
