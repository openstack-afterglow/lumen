"""Built-in remote MCP connector bundles (Notion, GitHub).

Neither Notion nor GitHub is an Anthropic-executed server tool — the provider's
server-tool set is web search, web fetch, code execution, advisor, tool search
and the MCP toolset, and nothing else.  Third-party products reach a model only
through a remote MCP server, so a "bundle" here is a declarative preset for a
``scope="global"`` row in ``chat_mcp_servers``: the destination, its transport,
and how a user authorizes it.

Credentials are deliberately absent from these definitions and from any package
manifest (``extension_packages._FORBIDDEN_KEYS`` rejects them outright).  Each
bundle declares ``auth_mode="oauth"`` so every user authorizes individually
through :mod:`lumen.services.mcp_oauth`, and the resulting tokens live only in
``chat_mcp_oauth_connections.encrypted_tokens``.  A shared static token would
make one person's access everyone's.

Installation is additive and idempotent.  It never calls
``extensions_store.update`` on an existing row, because any update bumps
``config_version`` and revokes every user's OAuth connection to that server.
"""

from __future__ import annotations

from typing import Any

from lumen.services import extensions_store as es


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


def _normalized_url(value: object) -> str:
    """Compare destinations the way a duplicate install would collide.

    Only case and a trailing slash are folded. Anything more would let two
    genuinely different endpoints look identical.
    """
    if not isinstance(value, str):
        return ""
    return value.strip().rstrip("/").lower()


def definition(slug: str) -> dict[str, Any]:
    bundle = BUNDLES.get(slug)
    if bundle is None:
        raise McpBundleError(f"알 수 없는 MCP 번들입니다: {slug}")
    return dict(bundle)


def _install_fields(bundle: dict[str, Any]) -> dict[str, Any]:
    """Project a bundle onto the exact whitelist ``extensions_store`` accepts."""
    return {
        "name": bundle["name"],
        "transport": bundle["transport"],
        "url": bundle["url"],
        "auth_mode": bundle["auth_mode"],
        "load_policy": bundle["load_policy"],
        "is_active": True,
    }


def _match(bundle: dict[str, Any], rows: list[dict]) -> dict | None:
    target = _normalized_url(bundle["url"])
    for row in rows:
        if _normalized_url(row.get("url")) == target:
            return row
    return None


async def catalog() -> list[dict[str, Any]]:
    """Return every bundle with its current installation state.

    The installed row is reported by id so a caller can drive the existing
    OAuth connect/status endpoints without a second lookup.
    """
    rows = await es.list_global("mcp")
    entries: list[dict[str, Any]] = []
    for slug in sorted(BUNDLES):
        bundle = BUNDLES[slug]
        installed = _match(bundle, rows)
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
                "installed": installed is not None,
                "server_id": installed.get("id") if installed else None,
                "is_active": installed.get("is_active") if installed else None,
            }
        )
    return entries


async def install(slug: str) -> dict[str, Any]:
    """Install one bundle as a global MCP source, or return the existing row.

    Re-installing is a no-op rather than an update: every user's OAuth
    connection is pinned to the row's ``config_version``, so rewriting the row
    would disconnect all of them.
    """
    bundle = definition(slug)
    rows = await es.list_global("mcp")
    existing = _match(bundle, rows)
    if existing is not None:
        return {"slug": slug, "created": False, "server": existing}
    created = await es.create("mcp", _install_fields(bundle), scope="global")
    return {"slug": slug, "created": True, "server": created}
