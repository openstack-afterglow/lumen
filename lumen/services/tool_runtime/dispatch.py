"""Custom HTTP/MCP dispatch and legacy execution entry points."""

from __future__ import annotations

import logging

from lumen.services import mcp_client, ssrf, tools
from lumen.services.agent_protocol import validate_tool_arguments
from lumen.services.tools import ToolContext

from . import bindings, contracts, managed, selection

logger = logging.getLogger(__name__)
_MAX_RESPONSE_BYTES = 64 * 1024
_MAX_RESPONSE_CHARS = 4000


async def _read_bounded_response(response) -> str:
    content_encoding = response.headers.get("content-encoding", "identity").strip().lower()
    if content_encoding not in ("", "identity"):
        return "압축된 툴 응답은 허용되지 않습니다."
    content = bytearray()
    async for chunk in response.aiter_bytes():
        if len(content) + len(chunk) > _MAX_RESPONSE_BYTES:
            return "툴 응답이 허용 크기를 초과했습니다."
        content.extend(chunk)
    return content.decode(response.encoding or "utf-8", errors="replace")[:_MAX_RESPONSE_CHARS]


async def _execute_custom_http_tool(tool_def: dict, args: dict, ctx: ToolContext) -> str:
    """커스텀 HTTP 툴을 SSRF 가드 후 대리 호출. 항상 안전한 문자열 반환."""
    url = tool_def.get("url") or ""
    method = (tool_def.get("method") or "GET").upper()
    timeout = int(tool_def.get("timeout_seconds") or 10)

    safe_args = args if isinstance(args, dict) else {}
    try:
        import httpx

        # 인증 헤더 미부착(사용자 토큰 유출 방지), redirect 미추적(내부 우회 차단).
        async with httpx.AsyncClient(
            transport=ssrf.SafeAsyncTransport(),
            headers={"Accept-Encoding": "identity"},
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
        ) as cx:
            request_kwargs = {"json": safe_args} if method == "POST" else {"params": safe_args}
            request_method = "POST" if method == "POST" else "GET"
            async with cx.stream(request_method, url, **request_kwargs) as response:
                body = await _read_bounded_response(response)
                return f"[{response.status_code}] {body}"
    except ssrf.SsrfBlocked:
        return "허용되지 않은 URL 입니다(내부/사설 주소 차단)."
    except Exception:
        logger.warning("커스텀 툴 HTTP 호출 실패 name=%s", tool_def.get("name"), exc_info=True)
        return "툴 호출 중 오류가 발생했습니다."


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
    return await mcp_client.call_tool(server, tool_name, args if isinstance(args, dict) else {})


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
                )
            try:
                result = await binding.execute(safe_args, ctx)
            except Exception:
                logger.warning("catalog-loaded tool execution failed name=%s", name, exc_info=True)
                return contracts.ToolExecutionResult("Tool execution failed.", warning_code="tool_execution_failed")
            components = result.usage_components.get("components", [])
            return contracts.ToolExecutionResult(
                result.model_content,
                visible=bool(result.display or result.artifacts),
                usage=tuple(components) if isinstance(components, list) else (),
                warning_code=result.error_code,
            )
    if name == managed._MANAGED_SEARCH_TOOL:
        return contracts._visible_result(await managed._execute_managed_search(args, ctx))
    if name == managed._MANAGED_FETCH_TOOL:
        return contracts._visible_result(await managed._execute_managed_fetch(args, ctx))
    if name == managed._MANAGED_ADVISOR_TOOL:
        return await managed._execute_managed_advisor(args, ctx)
    if name.startswith(selection._MCP_PREFIX):
        return contracts._visible_result(await _execute_mcp_tool(name, args, ctx))
    if name in tools._TOOL_BY_NAME:
        return contracts._visible_result(await tools.execute_tool(name, args, ctx))
    customs = {tool["name"]: tool for tool in await selection._load_custom(ctx)}
    if name in customs:
        return contracts._visible_result(await _execute_custom_http_tool(customs[name], args, ctx))
    return contracts.ToolExecutionResult(f"알 수 없는 툴입니다: {name}")


async def context_execute(name: str, args: dict, ctx: ToolContext) -> str:
    """Compatibility string boundary for non-graph callers."""
    return (await context_execute_result(name, args, ctx)).content
