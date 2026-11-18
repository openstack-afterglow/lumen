"""Admission and metering for stateless Responses tools, independent of transport."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit

from lumen.services.provider_errors import OpenAIUsageInvalid

DEFAULT_MAX_TOOL_CALLS = 10
MAX_TOOL_CALLS = 20
WEB_SEARCH_USD = Decimal("0.01")
CONTAINER_BLOCK_USD = {
    "1g": Decimal("0.03"), "4g": Decimal("0.12"), "16g": Decimal("0.48"), "64g": Decimal("1.92"),
}
METERED_MODELS = frozenset({"gpt-6.2-sol", "gpt-6.2-astra"})
_CLIENT_TOOLS = frozenset({"function", "custom", "computer", "computer_use", "computer_use_preview", "apply_patch", "local_shell"})
_WEB_SEARCH = frozenset({"web_search", "web_search_preview"})
_BLOCKED_OUTPUT = frozenset({"file_search_call", "image_generation_call", "skill_call"})


class UnsupportedHostedTool(ValueError):
    """Request uses an unmetered fee, model, or provider-side state."""


class UnsupportedProviderReference(ValueError):
    """Caller attempted to reference objects in the operator's account."""


def _token_only(tool: Any) -> bool:
    if not isinstance(tool, dict):
        return False
    kind = tool.get("type")
    if not isinstance(kind, str):
        return False
    if kind in _CLIENT_TOOLS or kind == "tool_search":
        return True
    if kind == "mcp" and "connector_id" not in tool and isinstance(tool.get("server_url"), str):
        try:
            url = urlsplit(tool["server_url"])
            return url.scheme == "https" and bool(url.hostname) and url.username is None
        except ValueError:
            return False
    if kind == "namespace" and isinstance(tool.get("tools"), list):
        return all(isinstance(item, dict) and item.get("type") in ("function", "custom") for item in tool["tools"])
    return kind == "shell" and tool.get("environment") == {"type": "local"}


def validate_input(source: str | list[dict], *, tool_search: bool = False) -> None:
    """Reject provider-owned IDs and undeclared hosted tool definitions at any depth."""
    pending = [source]
    while pending:
        item = pending.pop()
        if isinstance(item, list):
            pending.extend(item)
            continue
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind in ("item_reference", "container_reference", "code_interpreter_call") or any(
            name in item for name in (
                "container_id", "file_id", "file_ids", "vector_store_id", "vector_store_ids",
                "response_id", "previous_response_id", "comparison_response_id", "conversation",
            )
        ):
            raise UnsupportedProviderReference()
        if kind == "additional_tools":
            raise UnsupportedHostedTool()
        if kind == "tool_search_output":
            tools = item.get("tools")
            if not tool_search or not isinstance(tools, list) or not all(_token_only(tool) for tool in tools):
                raise UnsupportedHostedTool()
        pending.extend(item.values())


@dataclass(frozen=True)
class ToolMeter:
    max_calls: int = 0
    web_search: bool = False
    container_tier: str | None = None
    interpreter: bool = False
    hosted_shell: bool = False
    local_shell: bool = False

    @property
    def bound_usd(self) -> Decimal:
        return self.max_calls * (
            (WEB_SEARCH_USD if self.web_search else Decimal(0)) + CONTAINER_BLOCK_USD.get(self.container_tier, Decimal(0))
        )

    def charges(self, output: Any) -> dict[str, dict[str, str | int]]:
        if not isinstance(output, list):
            raise OpenAIUsageInvalid()
        web_calls = container_calls = 0
        containers: set[str] = set()
        for item in output:
            if not isinstance(item, dict) or not isinstance(kind := item.get("type"), str):
                raise OpenAIUsageInvalid()
            if kind in _BLOCKED_OUTPUT:
                raise OpenAIUsageInvalid()
            if kind == "web_search_call":
                if not self.web_search:
                    raise OpenAIUsageInvalid()
                web_calls += 1
            if kind == "code_interpreter_call":
                if not self.interpreter:
                    raise OpenAIUsageInvalid()
                container_id = item.get("container_id")
            elif kind == "shell_call":
                env = item.get("environment")
                if self.local_shell and (env is None or isinstance(env, dict) and env.get("type") == "local"):
                    continue
                if not self.hosted_shell:
                    raise OpenAIUsageInvalid()
                container_id = env.get("container_id") if isinstance(env, dict) else None
                if not container_id:
                    container_id = item.get("container_id")
            else:
                continue
            if not isinstance(container_id, str) or not container_id.strip():
                raise OpenAIUsageInvalid()
            containers.add(container_id)
            container_calls += 1
        if web_calls + container_calls > self.max_calls:
            raise OpenAIUsageInvalid()
        charges: dict[str, dict[str, str | int]] = {}
        if web_calls:
            charges["web_search"] = {"calls": web_calls, "cost": str(web_calls * WEB_SEARCH_USD)}
        if containers:
            charges["container"] = {
                "blocks": len(containers), "memory_limit": self.container_tier,
                "cost": str(len(containers) * CONTAINER_BLOCK_USD[self.container_tier]),
            }
        return charges


def admit(source: str | list[dict], options: dict, *, metered: bool) -> ToolMeter:
    """Admit only token-only tools or exact, bounded hosted fee schedules."""
    tools = options.get("tools") or ()
    validate_input(source, tool_search=any(tool.get("type") == "tool_search" for tool in tools))
    web = interpreter = shell = local_shell = False
    tiers = set()
    for tool in tools:
        if _token_only(tool):
            local_shell |= tool["type"] == "shell"
            continue
        if not metered:
            raise UnsupportedHostedTool()
        kind = tool.get("type")
        if not isinstance(kind, str):
            raise UnsupportedHostedTool()
        if kind in _WEB_SEARCH:
            web = True
            continue
        if kind == "code_interpreter" and set(tool) <= {"type", "container"}:
            config, expected = tool.get("container"), "auto"
            interpreter = True
        elif kind == "shell" and set(tool) <= {"type", "environment"}:
            config, expected = tool.get("environment"), "container_auto"
            shell = True
        else:
            raise UnsupportedHostedTool()
        if not isinstance(config, dict) or config.get("type") != expected or set(config) - {"type", "memory_limit"}:
            raise UnsupportedHostedTool()
        tier = config.get("memory_limit", "1g")
        if not isinstance(tier, str) or tier not in CONTAINER_BLOCK_USD:
            raise UnsupportedHostedTool()
        tiers.add(tier)
    # One tier per request keeps a container's memory attribution unambiguous.
    # Mixing local and hosted shell makes an omitted environment ambiguous.
    if len(tiers) > 1 or local_shell and shell:
        raise UnsupportedHostedTool()
    if web or tiers:
        options.setdefault("max_tool_calls", DEFAULT_MAX_TOOL_CALLS)
    max_calls = options.get("max_tool_calls", 0)
    if "max_tool_calls" in options and (type(max_calls) is not int or not 1 <= max_calls <= MAX_TOOL_CALLS):
        raise UnsupportedHostedTool()
    return ToolMeter(max_calls, web, next(iter(tiers), None), interpreter, shell, local_shell)
