"""Core-owned ``MemoryHost`` view backing the selected memory plugin.

Encrypted rows, authorization, expiry filtering and the atomic mutation+outbox
transaction remain exclusively in ``memory_store``. The default memory plugin
never touches storage directly: it only calls the operations below (thin
passthroughs to ``memory_store``) and ranks what ``list``/``embed_query``
return. Candidate IDs a provider selects are never treated as authorization
by themselves — ``recall_for_run`` always re-hydrates through
``memory_store`` before disclosing any plaintext content. Domain exceptions
(``ChatStorageUnavailable``/``MemoryNotFound``/``MemoryForbidden``/
``MemoryValidationError``) propagate unchanged so existing API-layer status
mapping (404/403/400/503) keeps working exactly as before this cutover; the
plugin methods calling these never inspect the exception type, so no
Lumen-internal type actually needs to cross the published ``MemoryHost``
contract for a real third-party implementation to remain compliant.
"""
from __future__ import annotations

from typing import Any, Literal

from lumen_plugin_api.contracts import Namespace, PluginError
from lumen_plugin_api.memory import MemoryAccess, MemoryIndex, MemoryMutation, MemoryQuery

from lumen.config import get_settings
from lumen.plugins.registry import get_plugin
from lumen.services import memory_store as ms
from lumen.services.memory_embeddings import EmbeddingUnavailable, embed_maintenance


class MemoryHostImpl:
    """Narrows existing ``memory_store`` calls to the ``Namespace``/``MemoryMutation`` shapes plugins use."""

    async def list(self, namespace: Namespace) -> list[dict[str, Any]]:
        return await ms.list_memories(
            user_id=namespace.user_id,
            project_id=namespace.project_id,
            workspace_id=namespace.workspace_id,
            include_account=namespace.include_account,
        )

    async def get(self, memory_id: int, namespace: Namespace) -> dict[str, Any]:
        return await ms.get_memory(
            memory_id,
            user_id=namespace.user_id,
            project_id=namespace.project_id,
            include_account=namespace.include_account,
        )

    async def hydrate(self, ids: tuple[int, ...], namespace: Namespace) -> list[dict[str, Any]]:
        return await ms.hydrate_namespace_ids(
            ids=list(ids),
            user_id=namespace.user_id,
            project_id=namespace.project_id,
            workspace_id=namespace.workspace_id,
            include_account=namespace.include_account,
        )

    async def create(self, mutation: MemoryMutation, namespace: Namespace) -> dict[str, Any]:
        if mutation.scope == "account" and not namespace.include_account:
            raise PluginError("plugin_authority_revoked", "account memory is not authorized for this caller")
        return await ms.create_memory(
            user_id=namespace.user_id,
            content=mutation.content or "",
            scope=mutation.scope,
            project_id=namespace.project_id,
            workspace_id=namespace.workspace_id,
            category=mutation.category or "general",
        )

    async def update(self, memory_id: int, mutation: MemoryMutation, namespace: Namespace) -> dict[str, Any]:
        patch: dict[str, Any] = {}
        if mutation.content is not None:
            patch["content"] = mutation.content
        if mutation.category is not None:
            patch["category"] = mutation.category
        if mutation.is_active is not None:
            patch["is_active"] = mutation.is_active
        return await ms.update_memory(
            memory_id,
            user_id=namespace.user_id,
            project_id=namespace.project_id,
            patch=patch,
            include_account=namespace.include_account,
        )

    async def delete(self, memory_id: int, namespace: Namespace) -> None:
        await ms.delete_memory(
            memory_id,
            user_id=namespace.user_id,
            project_id=namespace.project_id,
            include_account=namespace.include_account,
        )

    async def embed_query(self, query: MemoryQuery) -> list[float]:
        try:
            return await embed_maintenance(query.query)
        except EmbeddingUnavailable as exc:
            raise PluginError("plugin_unavailable", str(exc)) from exc


def make_host() -> MemoryHostImpl:
    return MemoryHostImpl()


def configured_index() -> MemoryIndex | None:
    """Build the selected memory plugin's index when semantic memory is fully configured.

    Returns ``None`` (never raises) when semantic memory is disabled or its DSN/dimensions
    are incomplete, so callers can translate absence into ``plugin_unavailable`` themselves.
    """
    settings = get_settings()
    if not settings.chat_semantic_memory_enabled:
        return None
    if not settings.chat_memory_pgvector_url or settings.chat_memory_embedding_dimensions <= 0:
        return None
    provider = get_plugin("memory")
    return provider.build_index(settings.chat_memory_pgvector_url, dimensions=settings.chat_memory_embedding_dimensions)


def access_for(namespace: Namespace) -> MemoryAccess:
    """Build the ``MemoryAccess`` every provider call (recall or CRUD) is authorized against."""
    return MemoryAccess(namespace=namespace, host=make_host(), index=configured_index())


async def recall_candidate_ids(
    *,
    namespace: Namespace,
    strategy: Literal["recency", "semantic"],
    query: str = "",
    limit: int = 30,
    token_budget: int = 0,
) -> list[int]:
    """Ask the selected memory provider to rank/select candidates; IDs only, never authorization."""
    provider = get_plugin("memory")
    selection = await provider.recall(
        MemoryQuery(namespace=namespace, query=query, strategy=strategy, limit=limit, token_budget=token_budget),
        access_for(namespace),
    )
    return list(selection.candidate_ids)


async def recall_for_run(
    *,
    user_id: str,
    project_id: str,
    workspace_id: int | None,
    include_account: bool,
    strategy: Literal["recency", "semantic"],
    query: str,
    limit: int,
    token_budget: int,
) -> list[str]:
    """Recall via the selected provider, then re-authorize and hydrate plaintext content in core."""
    namespace = Namespace(user_id=user_id, project_id=project_id, workspace_id=workspace_id, include_account=include_account)
    ids = await recall_candidate_ids(namespace=namespace, strategy=strategy, query=query, limit=limit, token_budget=token_budget)
    hydrated = await ms.hydrate_namespace_ids(
        ids=ids, user_id=user_id, project_id=project_id, workspace_id=workspace_id, include_account=include_account
    )
    return [item["content"] for item in hydrated if isinstance(item.get("content"), str) and item["content"].strip()]
