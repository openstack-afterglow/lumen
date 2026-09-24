"""Default memory access provider: ranking and index adapter only.

Encrypted rows, authorization, expiry filtering and the atomic mutation+outbox
transaction remain exclusively with the host supplied on ``MemoryAccess``.
This plugin never touches storage directly: it only ranks host-authorized
rows for ``recency`` or asks the pluggable vector index for candidate IDs for
``semantic``, and never treats a returned vector ID as authorization by
itself — the host re-authorizes every candidate before hydration.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from lumen_plugin_api.contracts import PluginError, PluginHost, PluginManifest
from lumen_plugin_api.memory import MemoryAccess, MemoryIndex, MemoryMutation, MemoryQuery, MemorySelection

from .index import PgVectorMemoryIndex

_PROVENANCE_RECENCY = "recency:host_list"
_PROVENANCE_SEMANTIC = "semantic:pgvector"


def _estimate_tokens(content: str) -> int:
    """Coarse token estimate; ranking only needs a monotone cost, not exactness."""
    return max(1, len(content) // 4)


def _updated_at(row: dict[str, Any]) -> datetime:
    value = row.get("updated_at")
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return datetime.min
    return datetime.min


class DefaultMemory:
    """Recency ranking over host-authorized rows; semantic candidate IDs via the index only."""

    manifest = PluginManifest(
        id="default-memory",
        kind="memory",
        version="0.1.0",
        required_capabilities=(),
    )

    async def start(self, host: PluginHost) -> None:
        return None

    async def close(self) -> None:
        return None

    def build_index(self, dsn: str, *, dimensions: int) -> MemoryIndex:
        return PgVectorMemoryIndex(dsn, dimensions=dimensions)

    async def recall(self, query: MemoryQuery, access: MemoryAccess) -> MemorySelection:
        if query.namespace != access.namespace:
            raise PluginError("plugin_authority_revoked", "memory query namespace does not match authorized access")
        if query.strategy == "semantic":
            return await self._recall_semantic(query, access)
        return await self._recall_recency(query, access)

    async def _recall_semantic(self, query: MemoryQuery, access: MemoryAccess) -> MemorySelection:
        if access.index is None:
            raise PluginError("plugin_unavailable", "semantic memory index is not configured")
        embedding = await access.host.embed_query(query)
        ids = await access.index.search_ids(
            user_id=query.namespace.user_id,
            project_id=query.namespace.project_id,
            workspace_id=query.namespace.workspace_id,
            embedding=embedding,
            limit=query.limit,
        )
        return MemorySelection(candidate_ids=tuple(ids[: query.limit]), provenance=_PROVENANCE_SEMANTIC)

    async def _recall_recency(self, query: MemoryQuery, access: MemoryAccess) -> MemorySelection:
        rows = await access.host.list(query.namespace)
        # "active" is a ranking criterion the provider applies, not something the host pre-filters:
        # ``list`` also serves the plain CRUD list capability, which must return every visible row.
        active = [row for row in rows if row.get("is_active") is True]
        ranked = sorted(active, key=_updated_at, reverse=True)
        selected: list[int] = []
        spent = 0
        for row in ranked:
            if len(selected) >= query.limit:
                break
            content = row.get("content")
            if not isinstance(content, str) or not content.strip():
                continue
            if query.token_budget > 0:
                cost = _estimate_tokens(content)
                # No first-candidate exemption: a single row costing more than the whole budget
                # is never selected, so the result always stays within the caller's cap.
                if spent + cost > query.token_budget:
                    break
                spent += cost
            memory_id = row.get("id")
            if isinstance(memory_id, int):
                selected.append(memory_id)
        return MemorySelection(candidate_ids=tuple(selected), provenance=_PROVENANCE_RECENCY)

    async def list(self, access: MemoryAccess) -> list[dict[str, Any]]:
        return await access.host.list(access.namespace)

    async def get(self, memory_id: int, access: MemoryAccess) -> dict[str, Any]:
        return await access.host.get(memory_id, access.namespace)

    async def create(self, mutation: MemoryMutation, access: MemoryAccess) -> dict[str, Any]:
        return await access.host.create(mutation, access.namespace)

    async def update(self, memory_id: int, mutation: MemoryMutation, access: MemoryAccess) -> dict[str, Any]:
        return await access.host.update(memory_id, mutation, access.namespace)

    async def delete(self, memory_id: int, access: MemoryAccess) -> None:
        await access.host.delete(memory_id, access.namespace)


def create_plugin() -> DefaultMemory:
    return DefaultMemory()
