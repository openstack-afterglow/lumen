"""Built-in remote MCP connector templates (Notion, GitHub).

Neither Notion nor GitHub is an Anthropic-executed server tool, so a third-party product reaches a model only
through a remote MCP server. A bundle is a template: installing it creates a ``scope="global"`` connection in
``chat_mcp_servers``, and installing it again with another label creates a second connection (for example a
second Notion workspace with narrower permissions). Each connection authorizes separately.

Credentials are absent from these definitions. Each bundle declares ``auth_mode="oauth"``, so every user
authorizes individually through :mod:`lumen.services.mcp_oauth`, and tokens live only in
``chat_mcp_oauth_connections.encrypted_tokens``. A shared static token would make one person's access everyone's.

Installation never updates an existing row, because an update bumps ``config_version`` and revokes every user's
OAuth connection to it. Rows created before templates existed (no ``template_key``) are recognised by URL.
"""

from __future__ import annotations

from typing import Any

from lumen.services import extensions_store as es
from lumen.services import mcp_connections
from lumen.services.mcp_registry_model import normalized_url


class McpBundleError(ValueError):
    """A bundle slug is unknown or its definition cannot be materialized."""


# ``load_policy="on_demand"`` matters: both servers expose large tool sets, and
# preloading them would spend the per-request schema budget on tools the model
# usually does not need. Under execution protocol v2 they stay behind the tool
# catalog until the model asks for them.
BUNDLES: dict[str, dict[str, Any]] = {
    "notion": {
        "slug": "notion",
        "name": "Notion",
        "url": "https://mcp.notion.com/mcp",
        "transport": "http",
        "auth_mode": "oauth",
        "load_policy": "on_demand",
        "description": "Notion 워크스페이스의 페이지·데이터베이스를 검색하고 편집합니다.",
        # Notion's hosted MCP server accepts the interactive OAuth flow only; a
        # pasted integration secret does not authenticate against it.
        "docs_url": "https://developers.notion.com/guides/mcp/get-started-with-mcp",
    },
    "github": {
        "slug": "github",
        "name": "GitHub",
        "url": "https://api.githubcopilot.com/mcp/",
        "transport": "http",
        "auth_mode": "oauth",
        "load_policy": "on_demand",
        "description": "GitHub 저장소·이슈·풀 리퀘스트를 조회하고 다룹니다.",
        "docs_url": "https://github.com/github/github-mcp-server",
    },
}


def definition(slug: str) -> dict[str, Any]:
    bundle = BUNDLES.get(slug)
    if bundle is None:
        raise McpBundleError(f"알 수 없는 MCP 번들입니다: {slug}")
    return dict(bundle)


def _linked(bundle: dict[str, Any], rows: list[dict]) -> list[dict]:
    """Connections made from this template, plus pre-template rows for the same destination."""
    target = normalized_url(bundle["url"])
    return [
        row
        for row in rows
        if row.get("template_key") == bundle["slug"]
        or (row.get("template_key") is None and normalized_url(row.get("url")) == target)
    ]


async def catalog() -> list[dict[str, Any]]:
    """Every template with its connections. ``installed`` means at least one connection exists."""
    rows = await es.list_global("mcp")
    entries: list[dict[str, Any]] = []
    for slug in sorted(BUNDLES):
        bundle = BUNDLES[slug]
        linked = _linked(bundle, rows)
        entries.append(
            {
                "slug": slug,
                "name": bundle["name"],
                "url": bundle["url"],
                "transport": bundle["transport"],
                "auth_mode": bundle["auth_mode"],
                "load_policy": bundle["load_policy"],
                "description": bundle["description"],
                "docs_url": bundle["docs_url"],
                "installed": bool(linked),
                "connections": [row["id"] for row in linked],
                "server_id": linked[0]["id"] if linked else None,
                "is_active": linked[0].get("is_active") if linked else None,
            }
        )
    return entries


async def install(slug: str, *, label: str | None = None) -> dict[str, Any]:
    """Create one connection of a template, or return the matching existing one unchanged.

    Without ``label`` the connection is the template's default one. Pass a label to add a second connection with
    different credentials or permissions.
    """
    bundle = definition(slug)
    result = await mcp_connections.create_connection(
        name=bundle["name"],
        url=bundle["url"],
        transport=bundle["transport"],
        auth_mode=bundle["auth_mode"],
        template_key=slug,
        default_label=bundle["name"],
        label=label,
        implicit_selection=True,
        load_policy=bundle["load_policy"],
    )
    return {"slug": slug, "created": result["created"], "server": result["server"]}
