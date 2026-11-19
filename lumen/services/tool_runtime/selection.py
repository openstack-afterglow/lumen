"""Frozen extension selection and legacy schema projection."""

from __future__ import annotations

import logging

from lumen_plugin_api.contracts import Namespace, PluginError

from lumen.services import mcp_client, tools
from lumen.services.tools import ToolContext

from . import contracts, managed

logger = logging.getLogger(__name__)
_MCP_PREFIX = "mcp__"


def _selected(items: list[dict], selected_ids: tuple[int, ...] | None) -> list[dict]:
    """selected_ids 가 None 이면 전체, 아니면 id 가 포함된 것만."""
    if selected_ids is None:
        return items
    allow = set(selected_ids)
    return [it for it in items if it.get("id") in allow]


def _frozen_selection(items: list[dict], ctx: ToolContext, kind: str) -> list[dict]:
    expected = dict(
        ((entry_kind, identifier), fingerprint)
        for entry_kind, identifier, fingerprint in (ctx.expected_extension_fingerprints or ())
    )
    if ctx.expected_extension_fingerprints is None:
        return items
    from lumen.services import extensions_store as es

    return [
        item
        for item in items
        if isinstance(item.get("id"), int) and expected.get((kind, item["id"])) == es.selection_fingerprint(item)
    ]


async def _load_custom(ctx: ToolContext) -> list[dict]:
    """호출자에게 노출되는 활성 커스텀 툴(global + 본인) — 선택 필터 적용. 저장소 장애 시 []."""
    from lumen.services import extensions_store as es

    try:
        items = await es.list_for_user(
            "tool", user_id=ctx.user_id, project_id=ctx.project_id, active_only=True,
            include_domain=ctx.selected_tool_ids or False, selected_ids=ctx.selected_tool_ids, runtime=True,
        )
    except Exception:
        logger.warning("커스텀 툴 로드 실패 — 내장 툴만 사용", exc_info=True)
        return []
    return _frozen_selection(_selected(items, ctx.selected_tool_ids), ctx, "tool")


async def _load_mcp(ctx: ToolContext) -> list[dict]:
    """호출자에게 노출되는 활성 MCP 서버 — 선택 필터 적용. 저장소 장애 시 []."""
    from lumen.services import extensions_store as es
    from lumen.services import mcp_oauth

    expected_credential_versions = dict(ctx.expected_mcp_credential_versions or ())
    try:
        # Read the scope, configuration, activation and OAuth epoch before any remote discovery/refresh.
        items = await es.list_for_user(
            "mcp", user_id=ctx.user_id, project_id=ctx.project_id, active_only=True, reveal_secrets=True,
            include_domain=ctx.selected_mcp_ids or False, selected_ids=ctx.selected_mcp_ids, runtime=True,
        )
        if ctx.selected_mcp_ids is None:
            items = [server for server in items if server.get("implicit_selection", True)]
        items = _frozen_selection(_selected(items, ctx.selected_mcp_ids), ctx, "mcp")
        oauth_fences = {
            server["id"]: mcp_oauth.OAuthFence.from_server(server, expected)
            for server in items if server.get("auth_mode") == "oauth"
            if type(expected := expected_credential_versions.get(server["id"])) is int and expected > 0
            and expected == server.get("credential_version")
        }
        oauth_headers_by_server = {}
        if oauth_fences:
            namespace = Namespace(user_id=ctx.user_id, project_id=ctx.project_id)
            with mcp_oauth.frozen_credentials(namespace, oauth_fences):
                oauth_headers_by_server = await mcp_oauth.headers_for_user(user_id=ctx.user_id, project_id=ctx.project_id)
    except es.ExtensionSecretUnavailable:
        logger.warning("MCP secrets cannot be decrypted; disabling MCP tools", exc_info=True)
        return []
    except Exception:
        logger.warning("MCP server load failed", exc_info=True)
        return []
    usable: list[dict] = []
    for server in items:
        if server.get("transport") not in {"http", "sse"} or not str(server.get("url") or "").lower().startswith(
            "https://"
        ):
            logger.warning("MCP server %s uses an unsupported transport or URL", server.get("id"))
            continue
        server_id = server.get("id")
        if server.get("auth_mode") == "oauth":
            oauth_headers = oauth_headers_by_server.get(server_id)
            if not oauth_headers:
                logger.info("MCP server %s has no active OAuth connection", server_id)
                continue
            server["headers"] = {**(server.get("headers") or {}), **oauth_headers}
        elif expected_credential_versions and expected_credential_versions.get(server_id) != 0:
            continue
        usable.append(server)
    return usable


def _mcp_config_fingerprint(server: dict) -> str:
    """Freeze execution configuration and consent, excluding secrets and run headers."""
    from lumen.services import extensions_store as es

    return contracts._v2_config_fingerprint(
        {
            "selection": es.selection_fingerprint(server),
            "effect_overrides": server.get("effect_overrides"),
            "oauth_scopes": server.get("oauth_scopes"),
        }
    )


def _mcp_schema(server_id: int, tool: dict) -> dict:
    """MCP 툴 → litellm function 스키마. 이름에 server_id 를 접두해 실행 시 라우팅."""
    return {
        "type": "function",
        "function": {
            "name": f"{_MCP_PREFIX}{server_id}__{tool['name']}",
            "description": tool.get("description") or tool["name"],
            "parameters": tool.get("input_schema") or {"type": "object", "properties": {}},
        },
    }


async def context_tool_activity_metadata(name: str, ctx: ToolContext) -> tuple[str, str]:
    """Return server-derived timeline metadata without trusting the model's tool name."""
    if isinstance(ctx.binding_session, contracts.ToolBindingSession):
        binding = ctx.binding_session.legacy_bindings.get(name)
        if binding is not None:
            return binding.definition.source, binding.definition.activity_category
        if name == "list_available_tools":
            return "builtin", "도구 준비"
    if name.startswith(_MCP_PREFIX):
        server_part = name[len(_MCP_PREFIX) :].partition("__")[0]
        try:
            server_id = int(server_part)
        except ValueError:
            return "mcp", "MCP"
        for server in await _load_mcp(ctx):
            if server.get("id") == server_id:
                return "mcp", f"MCP · {str(server.get('name') or '서버')[:80]}"
        return "mcp", "MCP"
    if name in {managed._MANAGED_SEARCH_TOOL, managed._MANAGED_FETCH_TOOL, managed._MANAGED_ADVISOR_TOOL}:
        return "managed", "관리형 도구"
    for tool in await _load_custom(ctx):
        try:
            definition = await contracts.custom_tool_function_schema(tool, ctx)
        except (PluginError, TypeError, ValueError):
            continue
        if definition["name"] == name:
            return "custom_http", "커스텀 도구"
    return "builtin", "기본 도구"


async def context_tool_schemas(ctx: ToolContext) -> list[dict]:
    """Return bounded legacy schemas; catalog-loaded schemas persist for this run only."""
    if not ctx.tools_enabled:
        return []
    if isinstance(ctx.binding_session, contracts.ToolBindingSession):
        from . import bindings

        dynamic_bindings = await bindings._legacy_dynamic_bindings(ctx)
        return [
            *await tools.tool_schemas(),
            *(
                bindings._binding_schema(binding)
                for binding in dynamic_bindings.values()
                if binding.definition.source != "mcp" or binding.definition.effect == "read"
            ),
        ]
    schemas = await tools.tool_schemas()
    for name, enabled in (
        (managed._MANAGED_SEARCH_TOOL, ctx.managed_search),
        (managed._MANAGED_FETCH_TOOL, ctx.managed_fetch),
        (managed._MANAGED_ADVISOR_TOOL, ctx.managed_advisor),
    ):
        if enabled is not None:
            schemas.append(await managed._managed_schema(name))
    for tool_def in await _load_custom(ctx):
        if tool_def.get("scope") == "domain" and tool_def.get("effect") != "read":
            continue
        try:
            function = await contracts.custom_tool_function_schema(tool_def, ctx)
        except (PluginError, TypeError, ValueError):
            logger.warning("invalid custom tool excluded from legacy schemas id=%s", tool_def.get("id"))
            continue
        schemas.append({"type": "function", "function": function})
    for server in await _load_mcp(ctx):
        server_id = server.get("id")
        for tool_def in await mcp_client.list_tools(server, context=contracts.execution_context(ctx)):
            if contracts._v2_effect((server.get("effect_overrides") or {}).get(tool_def["name"])) == "read":
                schemas.append(_mcp_schema(server_id, tool_def))
    return schemas
