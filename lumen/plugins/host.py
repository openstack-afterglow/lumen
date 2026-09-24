"""Compose the single process-wide PluginHost from core-owned capability adapters.

Each adapter enforces owner/project scope itself; this module only routes by domain
and never widens what any single adapter would return.
"""
from __future__ import annotations

from typing import Any, Literal

from lumen_plugin_api.contracts import Namespace, PluginError, PluginHost


class CompositeExtensionAccess:
    """Route ``kind`` to the owning core adapter; unknown kinds fail closed."""

    def __init__(self, by_kind: dict[str, Any]) -> None:
        self._by_kind = by_kind

    def _target(self, kind: str):
        target = self._by_kind.get(kind)
        if target is None:
            raise PluginError("plugin_unavailable", f"extension kind {kind!r} has no host adapter")
        return target

    async def list(self, kind: Literal["tool", "skill", "mcp"], namespace: Namespace) -> list[dict[str, Any]]:
        return await self._target(kind).list(kind, namespace)

    async def resolve(self, kind: Literal["tool", "skill", "mcp"], identifier: int | str, namespace: Namespace) -> dict[str, Any]:
        return await self._target(kind).resolve(kind, identifier, namespace)

    async def revalidate(self, kind: Literal["tool", "skill", "mcp"], identifier: int | str, fingerprint: str, namespace: Namespace) -> dict[str, Any]:
        return await self._target(kind).revalidate(kind, identifier, fingerprint, namespace)


def build_host() -> PluginHost:
    """Assemble every core capability once; the registry hands each plugin only its declared subset."""
    from lumen.plugins import mcp_host, memory_host, skills_host, tools_host

    tools = tools_host.make_host()
    return PluginHost(
        configuration={},
        conversations=tools.conversations,
        extensions=CompositeExtensionAccess(
            {
                "tool": tools.extensions,
                "skill": skills_host.make_host(),
                "mcp": mcp_host.McpExtensions(),
            }
        ),
        advisor=tools.advisor,
        public_http=tools.public_http,
        memory=memory_host.make_host(),
        mcp_connections=mcp_host.connection_store(),
        mcp_authority=mcp_host.authority_access(),
    )
