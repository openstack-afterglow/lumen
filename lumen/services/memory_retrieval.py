"""Run-owned semantic candidate lookup, delegated to the selected memory provider.

Embedding and vector-index access now live behind the ``lumen.memory`` plugin
contract; this module only shapes one exact-namespace query for the API
search route. Hydration/authorization stay in ``memory_store``.
"""

from __future__ import annotations

from lumen_plugin_api.contracts import Namespace

from lumen.plugins import memory_host


async def candidate_ids(
    *,
    query: str,
    user_id: str,
    project_id: str | None,
    workspace_id: int | None,
    limit: int,
) -> list[int]:
    """Return only vector IDs for one exact namespace; plaintext stays in MySQL until hydration."""
    namespace = Namespace(user_id=user_id, project_id=project_id, workspace_id=workspace_id, include_account=False)
    return await memory_host.recall_candidate_ids(namespace=namespace, strategy="semantic", query=query, limit=limit)
