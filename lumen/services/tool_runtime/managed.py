"""Managed search, fetch, and advisor tools."""

from __future__ import annotations

import json
import logging

from lumen.services import advisor, web_fetch, web_search
from lumen.services.tools import ToolContext

from .contracts import ToolExecutionResult

logger = logging.getLogger(__name__)

_MANAGED_SEARCH_TOOL = "managed_web_search"
_MANAGED_FETCH_TOOL = "managed_web_fetch"
_MANAGED_ADVISOR_TOOL = "managed_advisor"
_MAX_MANAGED_RESULT_BYTES = 48 * 1024


def _truncate_utf8(value: object, maximum_bytes: int) -> str | None:
    if not isinstance(value, str):
        return None
    encoded = value.encode("utf-8")
    if len(encoded) <= maximum_bytes:
        return value
    return encoded[:maximum_bytes].decode("utf-8", errors="ignore")


def _bounded_search_result(citations: list[web_search.SearchCitation]) -> str:
    sources: list[dict[str, str | None]] = []
    for citation in citations:
        candidate = {
            "url": _truncate_utf8(citation.url, 2_048),
            "title": _truncate_utf8(citation.title, 512),
            "snippet": _truncate_utf8(citation.snippet, 2_048),
        }
        encoded = json.dumps({"sources": [*sources, candidate]}, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        if len(encoded) > _MAX_MANAGED_RESULT_BYTES:
            break
        sources.append(candidate)
    return json.dumps({"sources": sources}, ensure_ascii=False, separators=(",", ":"))


def _managed_schema(name: str, description: str, property_name: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": {property_name: {"type": "string"}},
                "required": [property_name],
                "additionalProperties": False,
            },
        },
    }


async def _managed_use_allowed(ctx: ToolContext, name: str, maximum: object) -> bool:
    if not isinstance(maximum, int) or maximum < 1:
        return False
    check = getattr(ctx.execution_hooks, "managed_tool_allowed", None)
    if check is None:
        return False
    return bool(await check(tool_name=name, maximum=maximum))


def _managed_usage(
    *,
    kind: str,
    price_key: str,
    unit: str,
    source: str,
    model_name: str | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "kind": kind,
        "price_key": price_key,
        "quantity": "1",
        "unit": unit,
        "source": source,
    }
    if model_name is not None:
        result["model_name"] = model_name
    return result


async def _execute_managed_search(args: dict, ctx: ToolContext) -> ToolExecutionResult:
    config = ctx.managed_search
    query = args.get("query") if isinstance(args, dict) else None
    if (
        not isinstance(config, dict)
        or not isinstance(query, str)
        or not (query := query.strip())
        or len(query) > 10_000
    ):
        return ToolExecutionResult("검색어가 올바르지 않습니다.")
    options = config.get("options")
    route = config.get("route")
    if not isinstance(options, dict) or not isinstance(route, dict):
        return ToolExecutionResult("관리형 웹 검색 설정이 올바르지 않습니다.")
    if not await _managed_use_allowed(ctx, _MANAGED_SEARCH_TOOL, options.get("max_uses")):
        return ToolExecutionResult("이 실행의 웹 검색 사용 한도에 도달했습니다.")
    location = options.get("approximate_location")
    country = (
        location.get("country") if isinstance(location, dict) and isinstance(location.get("country"), str) else None
    )
    try:
        citations = await web_search.search_with_route(
            query,
            route=route,
            context_size=str(options.get("context_size") or ""),
            allowed_domains=tuple(options.get("allowed_domains") or ()),
            blocked_domains=tuple(options.get("blocked_domains") or ()),
            country=country,
        )
    except web_search.ManagedSearchError:
        logger.warning("managed web search failed", exc_info=True)
        return ToolExecutionResult("웹 검색 공급자 호출에 실패했습니다.")
    context_size = str(options.get("context_size") or "")
    return ToolExecutionResult(
        _bounded_search_result(citations),
        usage=(
            _managed_usage(
                kind="web_search_requests",
                price_key="web_search_request_per_unit",
                unit="request",
                source="search",
            ),
            _managed_usage(
                kind="web_search_context",
                price_key=f"web_search_context_{context_size}_per_unit",
                unit="context",
                source="search",
            ),
        ),
    )


async def _execute_managed_fetch(args: dict, ctx: ToolContext) -> ToolExecutionResult:
    options = ctx.managed_fetch
    url = args.get("url") if isinstance(args, dict) else None
    if not isinstance(options, dict) or not isinstance(url, str) or not (url := url.strip()) or len(url) > 2_048:
        return ToolExecutionResult("가져올 URL이 올바르지 않습니다.")
    if not await _managed_use_allowed(ctx, _MANAGED_FETCH_TOOL, options.get("max_uses")):
        return ToolExecutionResult("이 실행의 웹 가져오기 사용 한도에 도달했습니다.")
    try:
        document = await web_fetch.fetch_document(
            url,
            allowed_domains=tuple(options.get("allowed_domains") or ()),
            blocked_domains=tuple(options.get("blocked_domains") or ()),
        )
    except web_fetch.ManagedFetchError:
        logger.warning("managed web fetch failed", exc_info=True)
        return ToolExecutionResult("웹 페이지를 안전하게 가져오지 못했습니다.")
    return ToolExecutionResult(
        json.dumps(
            {
                "url": _truncate_utf8(document.url, 2_048),
                "title": _truncate_utf8(document.title, 512),
                "content_type": document.content_type,
                "text": _truncate_utf8(document.text, _MAX_MANAGED_RESULT_BYTES - 4_096),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        usage=(
            _managed_usage(
                kind="web_fetch_requests",
                price_key="web_fetch_request_per_unit",
                unit="request",
                source="fetch",
            ),
            _managed_usage(
                kind="web_fetch_context",
                price_key="web_fetch_context_per_unit",
                unit="context",
                source="fetch",
            ),
        ),
    )


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
    try:
        result = await advisor.ask_with_route(
            route=route,
            goal=goal,
            visible_messages=list(ctx.advisor_visible_messages),
        )
    except advisor.AdvisorError:
        logger.warning("managed advisor failed", exc_info=True)
        return ToolExecutionResult("Advisor request failed.", visible=False, warning_code="advisor_call_failed")
    return ToolExecutionResult(
        result.advice,
        visible=False,
        usage=(
            _managed_usage(
                kind="advisor_input_tokens",
                price_key="advisor_input_price_per_token",
                unit="token",
                source="advisor",
                model_name=str(route.get("model_name") or ""),
            )
            | {"quantity": str(result.prompt_tokens)},
            _managed_usage(
                kind="advisor_output_tokens",
                price_key="advisor_output_price_per_token",
                unit="token",
                source="advisor",
                model_name=str(route.get("model_name") or ""),
            )
            | {"quantity": str(result.completion_tokens)},
        ),
    )
