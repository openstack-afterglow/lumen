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

from asyncio import gather
from itertools import islice, zip_longest
from typing import Any, Literal

from lumen_plugin_api.contracts import Namespace, PluginError
from lumen_plugin_api.memory import MemoryAccess, MemoryIndex, MemoryMutation, MemoryQuery

from lumen.config import get_settings
from lumen.plugins.registry import get_plugin
from lumen.services import memory_store as ms
from lumen.services.memory_embeddings import EmbeddingUnavailable, embed_maintenance


class MemoryHostImpl:
    """Narrows existing ``memory_store`` calls to the ``Namespace``/``MemoryMutation`` shapes plugins use."""
    def __init__(self, namespace: Namespace | None = None, domain_id: str | None = None):
        self._namespace = namespace
        self._domain_id = domain_id

    def _scope(self, namespace: Namespace) -> dict:
        if self._namespace is None or namespace != self._namespace:
            raise PluginError("plugin_authority_revoked", "memory namespace is not authorized")
        return dict(user_id=namespace.user_id, project_id=namespace.project_id, domain_id=self._domain_id)

    async def list(self, namespace: Namespace) -> list[dict[str, Any]]:
        return await ms.list_memories(**self._scope(namespace), workspace_id=namespace.workspace_id,
                                      include_account=namespace.include_account)

    async def get(self, memory_id: int, namespace: Namespace) -> dict[str, Any]:
        return await ms.get_memory(memory_id, **self._scope(namespace), include_account=namespace.include_account)

    async def hydrate(self, ids: tuple[int, ...], namespace: Namespace) -> list[dict[str, Any]]:
        return await ms.hydrate_namespace_ids(ids=list(ids), **self._scope(namespace), workspace_id=namespace.workspace_id,
                                              include_account=namespace.include_account)

    async def create(self, mutation: MemoryMutation, namespace: Namespace) -> dict[str, Any]:
        scope = self._scope(namespace)
        if mutation.scope == "account" and not namespace.include_account:
            raise PluginError("plugin_authority_revoked", "account memory is not authorized for this caller")
        return await ms.create_memory(**scope, workspace_id=namespace.workspace_id,
                                      content=mutation.content or "", scope=mutation.scope,
                                      category=mutation.category or "general")

    async def update(self, memory_id: int, mutation: MemoryMutation, namespace: Namespace) -> dict[str, Any]:
        patch = {name: value for name in ("content", "category", "is_active")
                 if (value := getattr(mutation, name)) is not None}
        return await ms.update_memory(memory_id, **self._scope(namespace), patch=patch,
                                      include_account=namespace.include_account, expected_hash=mutation.expected_state)

    async def delete(self, memory_id: int, namespace: Namespace) -> None:
        await ms.delete_memory(memory_id, **self._scope(namespace), include_account=namespace.include_account)

    async def embed_query(self, query: MemoryQuery) -> list[float]:
        self._scope(query.namespace)
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


class _ScopedVectorSearch:
    """The plugin only receives authorized candidate IDs, never a vector mutation capability."""

    def __init__(self, index: MemoryIndex, namespace: Namespace, targets: tuple[tuple[str | None, int | None], ...]):
        self._index = index
        self._namespace = namespace
        self._targets = targets

    async def search_ids(self, *, user_id, project_id, workspace_id, embedding, limit):
        ns = self._namespace
        if (user_id, project_id, workspace_id) != (ns.user_id, ns.project_id, ns.workspace_id):
            raise PluginError("plugin_authority_revoked", "vector namespace is not authorized")
        results = await gather(*(self._index.search_ids(
            user_id=user_id, project_id=target_project, workspace_id=target_workspace,
            embedding=embedding, limit=limit,
        ) for target_project, target_workspace in self._targets))
        ranked = (candidate for rank in zip_longest(*results) for candidate in rank if candidate is not None)
        return list(islice(dict.fromkeys(ranked), limit))


def access_for(namespace: Namespace, *, domain_id: str | None = None, index=None) -> MemoryAccess:
    """Bind a host to the authorized account/project before calling a plugin."""
    return MemoryAccess(namespace=namespace, host=MemoryHostImpl(namespace, domain_id), index=index)


async def recall_candidate_ids(
    *,
    namespace: Namespace,
    strategy: Literal["recency", "semantic"],
    query: str = "",
    limit: int = 30,
    token_budget: int = 0,
    domain_id: str | None = None,
    vector_scopes: tuple[tuple[str | None, int | None], ...] = (),
) -> list[int]:
    """Ask the selected memory provider to rank/select candidates; IDs only, never authorization."""
    provider = get_plugin("memory")
    index = configured_index() if strategy == "semantic" else None
    vector_search = _ScopedVectorSearch(index, namespace, vector_scopes) if index is not None else None
    selection = await provider.recall(
        MemoryQuery(namespace=namespace, query=query, strategy=strategy, limit=limit, token_budget=token_budget),
        access_for(namespace, domain_id=domain_id, index=vector_search),
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
    domain_id: str | None = None,
) -> list[str]:
    """Recall via the selected provider, then re-authorize and hydrate plaintext content in core."""
    namespace = Namespace(user_id=user_id, project_id=project_id, workspace_id=workspace_id, include_account=include_account)
    if not domain_id:
        return []
    targets = [(domain_id, None)]
    if include_account:
        targets.append((None, None))
    if workspace_id is not None:
        targets.append((domain_id, workspace_id))
    ids = await recall_candidate_ids(
        namespace=namespace, strategy=strategy, query=query, limit=limit, token_budget=token_budget,
        domain_id=domain_id, vector_scopes=tuple(targets),
    )
    hydrated = await ms.hydrate_namespace_ids(
        ids=ids, user_id=user_id, project_id=project_id, workspace_id=workspace_id,
        include_account=include_account, domain_id=domain_id,
    )
    selected = []
    spent = 0
    for row in hydrated:
        content = row["content"]
        if not content or not content.strip():
            continue
        spent += max(1, len(content) // 4)
        if token_budget and spent > token_budget:
            break
        selected.append(content)
    return selected
