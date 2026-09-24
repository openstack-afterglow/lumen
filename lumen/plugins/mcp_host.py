"""Core-owned capabilities for independently packaged MCP authority plugins.

Only this host crosses into settings, SQL, encrypted secrets, and DNS-pinned IO.
A plugin receives no database session or unrestricted HTTP client.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
from lumen_plugin_api.contracts import Namespace
from lumen_plugin_api.mcp import McpAuthorityAccess, McpConnectionStore

from lumen.services import extensions_store, ssrf


@dataclass(frozen=True)
class McpPublicHttp:
    def client(self, *, timeout_seconds: float = 15, max_response_bytes: int = 65536) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=ssrf.SafeAsyncTransport(max_response_bytes=max_response_bytes),
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=False,
            trust_env=False,
            headers={"Accept-Encoding": "identity", "Accept": "application/json"},
        )

    def validate_url(self, url: str) -> None:
        ssrf.validate_url(url)

    async def request(self, method: str, url: str, *, headers: dict[str, str] | None = None, json: dict[str, Any] | None = None, data: dict[str, str] | None = None) -> tuple[int, dict[str, str], bytes]:
        if not url.startswith("https://"):
            raise ssrf.SsrfBlocked("MCP requests require HTTPS")
        async with self.client() as client:
            response = await client.request(method, url, headers=headers, json=json, data=data)
            return response.status_code, dict(response.headers), response.content


@dataclass(frozen=True)
class McpExtensions:
    """Resolve only visible, active MCP rows; execution secrets never reach API routes."""
    async def list(self, kind: str, namespace: Namespace) -> list[dict[str, Any]]:
        if kind != "mcp" or not namespace.project_id:
            raise PermissionError("MCP namespace is invalid")
        return await extensions_store.list_for_user("mcp", user_id=namespace.user_id, project_id=namespace.project_id, active_only=True, reveal_secrets=True)

    async def resolve(self, kind: str, identifier: int | str, namespace: Namespace) -> dict[str, Any]:
        if kind != "mcp" or type(identifier) is not int:
            raise PermissionError("MCP server identifier is invalid")
        row = next((row for row in await self.list(kind, namespace) if row["id"] == identifier), None)
        if row is None:
            raise PermissionError("MCP server is unavailable")
        return row

    async def revalidate(self, kind: str, identifier: int | str, fingerprint: str, namespace: Namespace) -> dict[str, Any]:
        row = await self.resolve(kind, identifier, namespace)
        if row.get("config_fingerprint") != fingerprint:
            raise PermissionError("MCP configuration changed")
        return row


def connection_store() -> McpConnectionStore:
    """SQL-backed encrypted OAuth connection store for the remote-mcp plugin."""
    from lumen.services.mcp_oauth import connection_store as sql_connection_store
    return sql_connection_store()


def authority_access() -> McpAuthorityAccess:
    """Transport-only view of the configured Afterglow bridge for the afterglow-mcp plugin."""
    from lumen.services.mcp_adapter import authority_access as bridge_authority_access
    return bridge_authority_access()


def oauth_configuration() -> dict[str, Any]:
    """Host-supplied, allowlisted callback and browser-return configuration."""
    from lumen.services.mcp_oauth import oauth_configuration as sql_oauth_configuration
    return sql_oauth_configuration()
