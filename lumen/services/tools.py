"""Authenticated legacy tool context and compatibility dispatch into installed tools.

The builtin schemas and handlers belong to the versioned default-tools wheel;
core retains only the tenant/run context and admission/runtime bridge.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from lumen_plugin_api.tools import validate_tool_arguments


@dataclass(frozen=True)
class ToolContext:
    """Host-authenticated tool run state; never constructed from model arguments."""

    project_id: str
    user_id: str
    tools_enabled: bool = True
    selected_tool_ids: tuple[int, ...] | None = None
    selected_mcp_ids: tuple[int, ...] | None = None
    expected_mcp_credential_versions: tuple[tuple[int, int], ...] | None = None
    expected_extension_fingerprints: tuple[tuple[str, int, str], ...] | None = None
    run_id: str | None = None
    tool_call_id: str | None = None
    lumen_snapshot: dict[str, object] | None = None
    lumen_snapshot_frozen: bool = False
    execution_hooks: object | None = None
    managed_search: dict[str, Any] | None = None
    managed_fetch: dict[str, Any] | None = None
    managed_advisor: dict[str, Any] | None = None
    advisor_visible_messages: tuple[dict[str, Any], ...] = ()
    binding_session: object | None = None
    plugin_tool_snapshots: tuple[dict[str, Any], ...] = ()


async def tool_schemas() -> list[dict]:
    """Project installed builtin definitions through their selected provider bindings."""
    from lumen.plugins.registry import get_plugin
    from lumen.plugins.tools_host import bind_default_tool
    from lumen.services.tool_runtime.contracts import execution_context

    provider = get_plugin("tools", "default-tools")
    context = execution_context(ToolContext(project_id="", user_id=""))
    result = []
    for export in provider.catalog():
        if export.key not in {"list_my_conversations", "get_conversation_detail"}:
            continue
        definition = (await bind_default_tool(export.key, context)).definition
        result.append({
            "type": "function",
            "function": {
                "name": definition.name,
                "description": definition.description,
                "parameters": definition.input_schema,
            },
        })
    return result


async def execute_tool(name: str, args: dict, ctx: ToolContext) -> str:
    """Legacy string boundary; still terminates in the installed plugin's binding."""
    from lumen.plugins.tools_host import bind_default_tool
    from lumen.services.tool_runtime.contracts import execution_context

    if name not in {"list_my_conversations", "get_conversation_detail"}:
        return f"알 수 없는 툴입니다: {name}"
    try:
        binding = await bind_default_tool(name, execution_context(ctx))
        allowed = binding.definition.input_schema.get("properties", {})
        safe_args = {key: value for key, value in (args if isinstance(args, dict) else {}).items() if key in allowed}
        validate_tool_arguments(binding.definition.input_schema, safe_args)
        result = await binding.execute(safe_args, execution_context(ctx))
        return result.model_content
    except ValueError:
        return "conversation_id(문자열)가 필요합니다." if name == "get_conversation_detail" else "툴 인자가 올바르지 않습니다."
    except Exception:
        return "툴 실행 중 오류가 발생했습니다."
