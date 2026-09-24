"""Core-owned, policy-scoped capabilities for independently installed tool plugins.

Only these methods cross into the plugin. Conversation/extension storage, budgeted
model-provider calls, and DNS-pinned public HTTP remain on the trusted host side.
"""
from __future__ import annotations

import json
from contextlib import AbstractAsyncContextManager
from typing import Any, Literal

import httpx
from lumen_plugin_api.contracts import ExecutionContext, Namespace, PluginError, PluginHost
from lumen_plugin_api.tools import ToolExecutionResult, ToolTextPart

from lumen.services import advisor as advisor_service
from lumen.services import conversation_store as conversations
from lumen.services import extensions_store as extensions
from lumen.services import ssrf, web_search
from lumen.services.usage_breakdown import UsageBreakdown

_MAX_MANAGED_RESULT_BYTES = 48 * 1024


class ToolsConversationAccess:
    async def list(self, context: ExecutionContext, *, limit: int = 20) -> list[dict[str, Any]]:
        return await conversations.list_conversations(
            user_id=context.user_id, project_id=context.project_id, limit=min(max(limit, 1), 20)
        )

    async def read(self, conversation_id: str, context: ExecutionContext) -> dict[str, Any]:
        item = await conversations.get_conversation(
            conversation_id, user_id=context.user_id, project_id=context.project_id
        )
        messages = await conversations.list_messages(
            conversation_id, user_id=context.user_id, project_id=context.project_id
        )
        return {
            "id": item["id"],
            "title": item.get("title"),
            "model_name": item.get("model_name"),
            "message_count": len(messages),
        }


class ToolsExtensionAccess:
    """Return only active, caller-visible custom HTTP rows; never generic SQL."""

    @staticmethod
    def _require_tool(kind: str) -> None:
        if kind != "tool":
            raise PluginError("plugin_unavailable", "tools host serves only custom HTTP tools")

    @staticmethod
    def _project(namespace: Namespace) -> str:
        if not namespace.project_id:
            raise PluginError("plugin_unavailable", "tool resolution requires a project scope")
        return namespace.project_id

    async def list(self, kind: Literal["tool", "skill", "mcp"], namespace: Namespace) -> list[dict[str, Any]]:
        self._require_tool(kind)
        try:
            return await extensions.list_for_user(
                "tool", user_id=namespace.user_id, project_id=self._project(namespace), active_only=True
            )
        except extensions.ChatStorageUnavailable as exc:
            raise PluginError("plugin_unavailable", "extension storage is unavailable") from exc

    async def resolve(
        self, kind: Literal["tool", "skill", "mcp"], identifier: int | str, namespace: Namespace
    ) -> dict[str, Any]:
        self._require_tool(kind)
        if not isinstance(identifier, int) or isinstance(identifier, bool) or identifier < 1:
            raise PluginError("plugin_unavailable", "invalid custom HTTP tool reference")
        item = next((item for item in await self.list("tool", namespace) if item.get("id") == identifier), None)
        if item is None:
            raise PluginError("plugin_authority_revoked", "custom HTTP tool is unavailable")
        return item

    async def revalidate(
        self, kind: Literal["tool", "skill", "mcp"], identifier: int | str, fingerprint: str, namespace: Namespace
    ) -> dict[str, Any]:
        item = await self.resolve(kind, identifier, namespace)
        if extensions.selection_fingerprint(item) != fingerprint:
            raise PluginError("plugin_configuration_changed", "custom HTTP tool configuration changed")
        return item


class ToolsPublicHttpAccess:
    def client(
        self, *, timeout_seconds: float = 15, max_response_bytes: int = 65536
    ) -> AbstractAsyncContextManager[httpx.AsyncClient]:
        if not 0 < timeout_seconds <= 60 or not 0 < max_response_bytes <= 5 * 1024 * 1024:
            raise ValueError("public HTTP resource limit is invalid")
        return httpx.AsyncClient(
            transport=ssrf.SafeAsyncTransport(max_response_bytes=max_response_bytes),
            headers={"Accept-Encoding": "identity"},
            timeout=timeout_seconds,
            follow_redirects=False,
            trust_env=False,
        )

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
        data: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        if method not in {"GET", "POST"}:
            raise ValueError("public HTTP method is not allowed")
        async with self.client(timeout_seconds=15, max_response_bytes=5 * 1024 * 1024) as client, client.stream(
            method,
            url,
            headers={**(headers or {}), "Accept-Encoding": "identity"},
            json=json if method == "POST" else None,
            params=data if method == "GET" else None,
        ) as response:
            content_encoding = response.headers.get("content-encoding", "identity").strip().lower()
            if content_encoding not in ("", "identity"):
                raise ValueError("compressed public HTTP response is not allowed")
            body = bytearray()
            async for chunk in response.aiter_bytes():
                if len(body) + len(chunk) > 5 * 1024 * 1024:
                    raise ValueError("public HTTP response exceeds byte limit")
                body.extend(chunk)
            return response.status_code, dict(response.headers), bytes(body)

    def validate_url(self, url: str) -> None:
        ssrf._parse_and_validate_url(url)


class ToolsAdvisorAccess:
    """Only the selected budgeted search/advisor operations; no arbitrary LLM call."""

    async def invoke(self, arguments: dict[str, Any], context: ExecutionContext) -> ToolExecutionResult:
        kind = arguments.get("kind")
        if kind == "search":
            return await self._search(arguments)
        if kind == "advisor":
            return await self._advisor(arguments)
        raise PluginError("plugin_unavailable", "unsupported provider operation")

    async def _search(self, arguments: dict[str, Any]) -> ToolExecutionResult:
        route = arguments.get("route")
        query = arguments.get("query")
        if not isinstance(route, dict) or not isinstance(query, str):
            raise PluginError("plugin_unavailable", "managed search route is unavailable")
        try:
            citations = await web_search.search_with_route(
                query,
                route=route,
                context_size=str(arguments.get("context_size") or ""),
                allowed_domains=tuple(arguments.get("allowed_domains") or ()),
                blocked_domains=tuple(arguments.get("blocked_domains") or ()),
                country=arguments.get("country") if isinstance(arguments.get("country"), str) else None,
            )
        except web_search.ManagedSearchError:
            return ToolExecutionResult(status="failed", model_content="웹 검색 공급자 호출에 실패했습니다.", error_code="managed_search_failed")
        sources: list[dict[str, str | None]] = []
        for citation in citations:
            candidate = {
                "url": _truncate_utf8(citation.url, 2_048),
                "title": _truncate_utf8(citation.title, 512),
                "snippet": _truncate_utf8(citation.snippet, 2_048),
            }
            encoded = json.dumps({"sources": [*sources, candidate]}, ensure_ascii=False, separators=(",", ":")).encode()
            if len(encoded) > _MAX_MANAGED_RESULT_BYTES:
                break
            sources.append(candidate)
        payload = json.dumps({"sources": sources}, ensure_ascii=False, separators=(",", ":"))
        context_size = str(arguments.get("context_size") or "")
        return ToolExecutionResult(
            status="completed",
            model_content=payload[:8_192],
            display=[ToolTextPart(text=payload[:8_192])],
            usage_components={"components": [
                _usage("web_search_requests", "web_search_request_per_unit", "request", "search"),
                _usage("web_search_context", f"web_search_context_{context_size}_per_unit", "context", "search"),
            ]},
        )

    async def _advisor(self, arguments: dict[str, Any]) -> ToolExecutionResult:
        route = arguments.get("route")
        goal = arguments.get("goal")
        if not isinstance(route, dict) or not isinstance(goal, str):
            raise PluginError("plugin_unavailable", "advisor route is unavailable")
        try:
            result = await advisor_service.ask_with_route(
                route=route, goal=goal, visible_messages=list(arguments.get("visible_messages") or ())
            )
        except advisor_service.AdvisorError:
            return ToolExecutionResult(status="failed", model_content="Advisor request failed.", error_code="advisor_call_failed")
        breakdown = getattr(result, "breakdown", None)
        if not isinstance(breakdown, UsageBreakdown):
            breakdown = UsageBreakdown.from_totals(result.prompt_tokens, result.completion_tokens)
        model_name = str(route.get("model_name") or "")
        tokens = (
            ("advisor_input_tokens", "advisor_input_price_per_token", breakdown.uncached_input_tokens),
            ("advisor_output_tokens", "advisor_output_price_per_token", breakdown.output_tokens),
            ("advisor_cache_read_tokens", "advisor_cache_read_price_per_token", breakdown.cache_read_input_tokens),
            ("advisor_cache_creation_5m_tokens", "advisor_cache_write_price_per_token", breakdown.cache_creation_5m_input_tokens),
            ("advisor_cache_creation_1h_tokens", "advisor_cache_write_1h_price_per_token", breakdown.cache_creation_1h_input_tokens),
        )
        return ToolExecutionResult(
            status="completed",
            model_content=result.advice[:8_192],
            display=[],  # advisor output stays private
            usage_components={"components": [
                _usage(kind, price_key, "token", "advisor", model_name=model_name) | {
                    "quantity": str(quantity),
                    **({"prompt_tokens": breakdown.input_tokens} if kind.startswith("advisor_cache_") else {}),
                }
                for kind, price_key, quantity in tokens
                if quantity > 0 or not kind.startswith("advisor_cache_")
            ]},
        )


def _truncate_utf8(value: object, maximum_bytes: int) -> str | None:
    if not isinstance(value, str):
        return None
    encoded = value.encode("utf-8")
    return value if len(encoded) <= maximum_bytes else encoded[:maximum_bytes].decode("utf-8", errors="ignore")


def _usage(kind: str, price_key: str, unit: str, source: str, *, model_name: str | None = None) -> dict[str, str]:
    item = {"kind": kind, "price_key": price_key, "quantity": "1", "unit": unit, "source": source}
    if model_name is not None:
        item["model_name"] = model_name
    return item


def make_host() -> PluginHost:
    """Return the four capabilities Main merges into the process-wide PluginHost."""
    return PluginHost(
        configuration={},
        conversations=ToolsConversationAccess(),
        extensions=ToolsExtensionAccess(),
        advisor=ToolsAdvisorAccess(),
        public_http=ToolsPublicHttpAccess(),
    )

async def bind_default_tool(export_key: str, context: ExecutionContext, configuration: dict[str, Any] | None = None):
    """Bind an installed, selected default export through the scoped registry host."""
    from lumen_plugin_api.contracts import PluginIdentity
    from lumen_plugin_api.tools import ToolSpec

    from .registry import fingerprint, get_plugin, get_registry

    provider = get_plugin("tools", "default-tools")
    identity = PluginIdentity(
        plugin_id=provider.manifest.id,
        version=provider.manifest.version,
        config_fingerprint=fingerprint({"export": export_key, "configuration": configuration or {}}),
    )
    return await provider.bind(
        ToolSpec(binding_id=f"default:{export_key}", export_key=export_key, identity=identity, configuration=configuration or {}),
        context,
        get_registry().host("default-tools"),
    )
