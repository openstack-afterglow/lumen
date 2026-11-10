"""Administrator-managed MCP connections: several per template, each with its own credentials and catalog.

A template (a registry entry or a bundle) is a shared description of one service. A connection is one
authorized view of it: its own credentials, its own permissions, its own label. Two Notion connections for two
workspaces, or two afterglow connections with different scopes, are two rows with the same ``template_key``.
Rows are unique on ``(template_key, connection_label)``.

Tool discovery is metadata. Its results are stored on the row without bumping ``config_version``, so refreshing
the catalog never revokes an OAuth connection. Credential or URL changes do bump it, as before.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from lumen.services import extensions_store as es
from lumen.services import mcp_client, mcp_oauth
from lumen.services.mcp_registry_model import AUTH_MODES, McpRegistryError, normalized_url

TRANSPORTS = ("http", "sse")
LABEL_MAX = 100
_CONTROL = frozenset(chr(code) for code in range(32)) | {chr(127)}


class McpConnectionError(McpRegistryError):
    """The connection request is invalid or cannot be performed right now."""


def _label(value: str | None, default: str) -> str:
    label = (value if value is not None else default).strip()
    if not label or len(label) > LABEL_MAX or any(ch in _CONTROL for ch in label):
        raise McpConnectionError("연결 라벨은 1–100자의 제어문자 없는 텍스트여야 합니다")
    return label


def _schema_digest(schema: Any) -> str:
    return hashlib.sha256(json.dumps(schema, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


def catalog_from_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Stable, bounded tool catalog: sorted by name, with a digest of each input schema for change detection."""
    entries = []
    for tool in tools:
        name = tool.get("name")
        if not isinstance(name, str) or not name:
            continue
        entries.append(
            {
                "name": name[:128],
                "description": str(tool.get("description") or name)[:500],
                "schema_sha256": _schema_digest(tool.get("input_schema")),
            }
        )
    return sorted(entries, key=lambda item: item["name"])


def diff_catalog(previous: list[dict[str, Any]], current: list[dict[str, Any]]) -> dict[str, list[str]]:
    """What changed between two discoveries: added, removed, and tools whose input schema changed."""
    before = {item["name"]: item.get("schema_sha256") for item in previous}
    after = {item["name"]: item.get("schema_sha256") for item in current}
    return {
        "added": sorted(set(after) - set(before)),
        "removed": sorted(set(before) - set(after)),
        "changed": sorted(name for name in set(after) & set(before) if after[name] != before[name]),
    }


def find_existing(
    rows: list[dict], *, template_key: str | None, label: str | None, url: str, allow_legacy: bool
) -> dict | None:
    """Return the connection that already satisfies a request, without touching it.

    A template request matches on ``(template_key, label)``. A request that named no label also accepts an
    unlabeled legacy row for the same URL, so installs made before templates existed are not duplicated.
    """
    if template_key is not None and label is not None:
        for row in rows:
            if row.get("template_key") == template_key and row.get("connection_label") == label:
                return row
    if allow_legacy:
        target = normalized_url(url)
        for row in rows:
            if row.get("template_key") is None and normalized_url(row.get("url")) == target:
                return row
    return None


async def create_connection(
    *,
    name: str,
    url: str,
    transport: str = "http",
    auth_mode: str = "none",
    headers: dict[str, str] | None = None,
    template_key: str | None = None,
    default_label: str | None = None,
    label: str | None = None,
    load_policy: str = "on_demand",
) -> dict[str, Any]:
    """Create one global connection, or return the matching existing one unchanged."""
    if transport not in TRANSPORTS:
        raise McpConnectionError("transport 는 http(streamable HTTP) 또는 sse 만 지원합니다")
    if auth_mode not in AUTH_MODES:
        raise McpConnectionError("auth_mode 는 none|oauth|admin 만 지원합니다")
    if auth_mode == "admin" and not headers:
        raise McpConnectionError("auth_mode=admin 에는 headers 가 필요합니다")
    if auth_mode != "admin" and headers:
        raise McpConnectionError("정적 인증 헤더는 auth_mode=admin 에서만 허용됩니다")
    resolved_label = None
    if template_key is not None:
        resolved_label = _label(label, default_label or name)
    elif label is not None:
        raise McpConnectionError("label 은 template_key 가 있는 연결에만 지정할 수 있습니다")
    rows = await es.list_global("mcp")
    existing = find_existing(rows, template_key=template_key, label=resolved_label, url=url, allow_legacy=label is None)
    if existing is not None:
        return {"created": False, "server": existing}
    fields: dict[str, Any] = {
        "name": name,
        "transport": transport,
        "url": url,
        "auth_mode": auth_mode,
        "load_policy": load_policy,
        "is_active": True,
        "template_key": template_key,
        "connection_label": resolved_label,
    }
    if headers:
        fields["headers"] = dict(headers)
    created = await es.create("mcp", fields, scope="global")
    return {"created": True, "server": created}


async def list_connections() -> list[dict[str, Any]]:
    return await es.list_global("mcp")


async def refresh_catalog(server_id: int, *, user_id: str | None, project_id: str | None) -> dict[str, Any]:
    """Re-discover one connection's tools and record what changed.

    An OAuth connection is discovered with the requesting user's token. ``headers_for_user`` refreshes that
    token first when it is about to expire. A failed discovery records its error and keeps the last good
    catalog, so a transient outage does not erase the tools a run still depends on.
    """
    row = await es.get_global_mcp_revealed(server_id)
    if not row.get("is_active"):
        raise McpConnectionError("비활성 연결은 도구를 검색할 수 없습니다")
    server = dict(row)
    if row.get("auth_mode") == "oauth":
        if not user_id or not project_id:
            raise McpConnectionError("OAuth 연결의 도구 검색에는 연결한 사용자 인증이 필요합니다")
        bearer = (await mcp_oauth.headers_for_user(user_id=user_id, project_id=project_id)).get(server_id)
        if not bearer:
            raise McpConnectionError("요청한 사용자에게 이 연결의 활성 OAuth 연결이 없습니다")
        server["headers"] = {**(server.get("headers") or {}), **bearer}
    try:
        tools = await mcp_client.discover_tools(server)
    except Exception as exc:
        # Only the plugin's consumer-safe text is reported; anything else stays generic (no type names, URLs or headers).
        detail = mcp_client.consumer_message(exc) or "unexpected error"
        error = f"MCP 도구 검색 실패: {detail}"
        await es.record_catalog(server_id, tools=None, error=error)
        return {"status": "failed", "error": error, "diff": None}
    current = catalog_from_tools(tools)
    previous = await es.record_catalog(server_id, tools=current, error=None)
    return {
        "status": "ok",
        "tool_count": len(current),
        "diff": diff_catalog(previous, current),
    }
