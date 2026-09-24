"""Core-only durable admission and usage bridge for managed plugin tools.

Schemas and actual execution live in the installed default-tools wheel; only the
run-scoped durable use limit and preselected provider route live in core.
"""
from __future__ import annotations

from lumen_plugin_api.tools import ToolExecutionResult as PublicToolResult

from lumen.plugins.tools_host import bind_default_tool
from lumen.services.tools import ToolContext

from .contracts import ToolExecutionResult, execution_context

_MANAGED_SEARCH_TOOL = "managed_web_search"
_MANAGED_FETCH_TOOL = "managed_web_fetch"
_MANAGED_ADVISOR_TOOL = "managed_advisor"


async def _managed_schema(name: str) -> dict:
    """Project the selected managed export from its executable provider binding."""
    definition = (await bind_default_tool(name, execution_context(ToolContext(project_id="", user_id="")))).definition
    return {
        "type": "function",
        "function": {
            "name": definition.name,
            "description": definition.description,
            "parameters": definition.input_schema,
        },
    }


async def _managed_use_allowed(ctx: ToolContext, name: str, maximum: object) -> bool:
    if not isinstance(maximum, int) or maximum < 1:
        return False
    check = getattr(ctx.execution_hooks, "managed_tool_allowed", None)
    if check is None:
        return False
    return bool(await check(tool_name=name, maximum=maximum))


def _legacy_result(value: PublicToolResult) -> ToolExecutionResult:
    components = value.usage_components.get("components", [])
    return ToolExecutionResult(
        value.model_content,
        visible=bool(value.display or value.artifacts),
        usage=tuple(components) if isinstance(components, list) else (),
        warning_code=value.error_code,
        status=value.status,
    )


async def _execute_managed_search(args: dict, ctx: ToolContext) -> ToolExecutionResult:
    config = ctx.managed_search
    query = args.get("query") if isinstance(args, dict) else None
    if not isinstance(config, dict) or not isinstance(query, str) or not (query := query.strip()) or len(query) > 10_000:
        return ToolExecutionResult("검색어가 올바르지 않습니다.")
    options = config.get("options")
    route = config.get("route")
    if not isinstance(options, dict) or not isinstance(route, dict):
        return ToolExecutionResult("관리형 웹 검색 설정이 올바르지 않습니다.")
    if not await _managed_use_allowed(ctx, _MANAGED_SEARCH_TOOL, options.get("max_uses")):
        return ToolExecutionResult("이 실행의 웹 검색 사용 한도에 도달했습니다.")
    location = options.get("approximate_location")
    country = location.get("country") if isinstance(location, dict) and isinstance(location.get("country"), str) else None
    binding = await bind_default_tool(_MANAGED_SEARCH_TOOL, execution_context(ctx), {
        "route": route,
        "max_uses": 1,
        "current_use_count": 0,
        "context_size": options.get("context_size"),
        "allowed_domains": options.get("allowed_domains"),
        "blocked_domains": options.get("blocked_domains"),
        "country": country,
    })
    return _legacy_result(await binding.execute({"query": query}, execution_context(ctx)))


async def _execute_managed_fetch(args: dict, ctx: ToolContext) -> ToolExecutionResult:
    options = ctx.managed_fetch
    url = args.get("url") if isinstance(args, dict) else None
    if not isinstance(options, dict) or not isinstance(url, str) or not (url := url.strip()) or len(url) > 2_048:
        return ToolExecutionResult("가져올 URL이 올바르지 않습니다.")
    if not await _managed_use_allowed(ctx, _MANAGED_FETCH_TOOL, options.get("max_uses")):
        return ToolExecutionResult("이 실행의 웹 가져오기 사용 한도에 도달했습니다.")
    binding = await bind_default_tool(_MANAGED_FETCH_TOOL, execution_context(ctx), {
        "max_uses": 1,
        "current_use_count": 0,
        "allowed_domains": options.get("allowed_domains"),
        "blocked_domains": options.get("blocked_domains"),
    })
    return _legacy_result(await binding.execute({"url": url}, execution_context(ctx)))


async def _execute_managed_advisor(args: dict, ctx: ToolContext) -> ToolExecutionResult:
    config = ctx.managed_advisor
    goal = args.get("goal") if isinstance(args, dict) else None
    if not isinstance(config, dict) or not isinstance(goal, str) or not (goal := goal.strip()) or len(goal) > 10_000:
        return ToolExecutionResult("Advisor goal is invalid.", visible=False)
    route = config.get("route")
    options = config.get("options")
    if not isinstance(route, dict) or not isinstance(options, dict):
        return ToolExecutionResult("Advisor configuration is invalid.", visible=False)
    if not await _managed_use_allowed(ctx, _MANAGED_ADVISOR_TOOL, options.get("max_uses")):
        return ToolExecutionResult("Advisor use limit reached.", visible=False)
    binding = await bind_default_tool(_MANAGED_ADVISOR_TOOL, execution_context(ctx), {
        "route": route,
        "max_uses": 1,
        "current_use_count": 0,
        "visible_messages": ctx.advisor_visible_messages,
    })
    return _legacy_result(await binding.execute({"goal": goal}, execution_context(ctx)))
