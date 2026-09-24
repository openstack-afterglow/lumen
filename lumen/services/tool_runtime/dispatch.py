"""Custom HTTP/MCP dispatch and legacy execution entry points."""

from __future__ import annotations

import logging

from lumen_plugin_api.contracts import PluginError
from lumen_plugin_api.tools import validate_tool_arguments

from lumen.services import mcp_client, tools
from lumen.services.tools import ToolContext

from . import bindings, contracts, managed, selection

logger = logging.getLogger(__name__)


async def _execute_mcp_tool(name: str, args: dict, ctx: ToolContext) -> str:
    """mcp__{server_id}__{tool} 을 파싱해 해당 MCP 서버에서 실행. 항상 안전한 문자열."""
    rest = name[len(selection._MCP_PREFIX) :]
    server_part, _, tool_name = rest.partition("__")
    if not tool_name:
        return f"알 수 없는 MCP 툴입니다: {name}"
    try:
        server_id = int(server_part)
    except ValueError:
        return f"알 수 없는 MCP 툴입니다: {name}"
    servers = {s.get("id"): s for s in await selection._load_mcp(ctx)}
    server = servers.get(server_id)
    if server is None:
        return "선택되지 않았거나 접근 불가한 MCP 서버입니다."
    result = await mcp_client.call_tool(server, tool_name, args if isinstance(args, dict) else {})
    return result.text if isinstance(result, mcp_client.McpToolOutput) else result


async def context_execute_result(name: str, args: dict, ctx: ToolContext) -> contracts.ToolExecutionResult:
    """Execute a tool and preserve whether its result may become a user-visible part."""
    if not ctx.tools_enabled:
        return contracts.ToolExecutionResult("Tool execution is disabled by this run's policy.")
    if isinstance(ctx.binding_session, contracts.ToolBindingSession):
        binding = (await bindings._legacy_dynamic_bindings(ctx)).get(name)
        if binding is not None:
            try:
                safe_args = validate_tool_arguments(binding.definition.input_schema, args)
            except ValueError:
                return contracts.ToolExecutionResult(
                    "Tool arguments do not match the required schema.",
                    warning_code="invalid_tool_arguments",
                    status="failed",
                )
            try:
                result = await binding.execute(safe_args, ctx)
            except Exception:
                logger.warning("catalog-loaded tool execution failed name=%s", name, exc_info=True)
                return contracts.ToolExecutionResult(
                    "Tool execution failed.", warning_code="tool_execution_failed", status="failed"
                )
            components = result.usage_components.get("components", [])
            return contracts.ToolExecutionResult(
                result.model_content,
                visible=bool(result.display or result.artifacts),
                usage=tuple(components) if isinstance(components, list) else (),
                warning_code=result.error_code,
                status=result.status,
            )
    if name == managed._MANAGED_SEARCH_TOOL:
        return contracts._visible_result(await managed._execute_managed_search(args, ctx))
    if name == managed._MANAGED_FETCH_TOOL:
        return contracts._visible_result(await managed._execute_managed_fetch(args, ctx))
    if name == managed._MANAGED_ADVISOR_TOOL:
        return await managed._execute_managed_advisor(args, ctx)
    if name.startswith(selection._MCP_PREFIX):
        return contracts._visible_result(await _execute_mcp_tool(name, args, ctx))
    if name in {"list_my_conversations", "get_conversation_detail"}:
        from lumen.plugins.tools_host import bind_default_tool

        binding = await bind_default_tool(name, contracts.execution_context(ctx))
        try:
            safe_args = validate_tool_arguments(binding.definition.input_schema, args)
        except ValueError:
            return contracts.ToolExecutionResult(
                "Tool arguments do not match the required schema.",
                warning_code="invalid_tool_arguments",
                status="failed",
            )
        return contracts._visible_result(await tools.execute_tool(name, safe_args, ctx))
    from lumen.plugins.tools_host import bind_default_tool

    for tool_def in await selection._load_custom(ctx):
        try:
            binding = await bind_default_tool("custom_http", contracts.execution_context(ctx), tool_def)
        except (PluginError, TypeError, ValueError):
            continue
        if binding.definition.name == name:
            try:
                safe_args = validate_tool_arguments(binding.definition.input_schema, args)
            except ValueError:
                return contracts.ToolExecutionResult("Tool arguments do not match the required schema.")
            try:
                result = await binding.execute(safe_args, contracts.execution_context(ctx))
            except PluginError:
                return contracts.ToolExecutionResult(
                    "The selected custom tool is no longer available.",
                    warning_code="extension_unavailable", status="failed",
                )
            return contracts._visible_result(result.model_content)
    return contracts.ToolExecutionResult(f"알 수 없는 툴입니다: {name}")


async def context_execute(name: str, args: dict, ctx: ToolContext) -> str:
    """Compatibility string boundary for non-graph callers."""
    return (await context_execute_result(name, args, ctx)).content
