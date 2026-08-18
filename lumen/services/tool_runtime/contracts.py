"""Shared tool binding and execution contracts."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from uuid import NAMESPACE_URL, uuid5

from lumen.models.chat_contracts import TextPart
from lumen.services import ssrf, tools
from lumen.services.agent_protocol import ToolBinding, ToolDefinition
from lumen.services.agent_protocol import ToolExecutionResult as V2ToolExecutionResult
from lumen.services.tools import ToolContext


@dataclass
class ToolBindingSession:
    """Mutable per-run schemas loaded by the model from the bounded catalog."""

    deferred_bindings: dict[str, ToolBinding] = field(default_factory=dict)
    loaded_names: set[str] = field(default_factory=set)
    legacy_bindings: dict[str, ToolBinding] = field(default_factory=dict)
    legacy_preloaded: bool = False


class ToolExecutionResult:
    """Internal tool result; `visible=False` keeps content out of message parts and SSE."""

    __slots__ = ("content", "usage", "visible", "warning_code")

    def __init__(
        self,
        content: str,
        *,
        visible: bool = True,
        usage: tuple[dict[str, object], ...] = (),
        warning_code: str | None = None,
    ) -> None:
        self.content = content
        self.visible = visible
        self.usage = usage
        self.warning_code = warning_code


_MANAGED_ADVISOR_TOOL = "managed_advisor"


def v2_builtin_tool_bindings() -> dict[str, ToolBinding]:
    """Return immutable v2 bindings for the read-only built-in tool registry.

    The v2 graph dispatches these bindings through ``agent_runtime_v2`` so JSON
    schema validation runs before a handler receives model-supplied arguments.
    """

    bindings: dict[str, ToolBinding] = {}
    for tool in tools.TOOLS:

        async def execute(arguments: dict[str, object], context: object, *, builtin=tool) -> V2ToolExecutionResult:
            if not isinstance(context, ToolContext):
                return V2ToolExecutionResult(
                    status="failed",
                    model_content="Tool execution context is invalid.",
                    error_code="invalid_tool_context",
                )
            result = await builtin.handler(arguments, context)
            model_content = str(result)[:8_192]
            return V2ToolExecutionResult(
                status="completed",
                model_content=model_content,
                display=[TextPart(type="text", text=model_content)] if model_content else [],
            )

        definition = ToolDefinition(
            name=tool.name,
            description=tool.description,
            input_schema={**tool.parameters, "additionalProperties": False},
            effect="read",
            parallel_safe=True,
            source="builtin",
            activity_category="기본 도구",
        )
        bindings[definition.name] = ToolBinding(definition=definition, execute=execute)
    return bindings


def _v2_provider_name(prefix: str, identifier: int, name: object) -> str:
    raw_name = str(name)
    normalized = re.sub(r"[^A-Za-z0-9_]+", "_", raw_name).strip("_") or "tool"
    digest = hashlib.sha256(raw_name.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}__{identifier}__{normalized[:96]}_{digest}"[:128]


def _v2_effect(value: object) -> str:
    return value if value in {"read", "workspace_write", "process", "external_mutation"} else "external_mutation"


def _v2_config_fingerprint(value: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def _v2_destination_origin(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        hostname, _port, scheme = ssrf._parse_and_validate_url(value)
    except ssrf.SsrfBlocked:
        return None
    return f"{scheme}://{hostname}"


def _v2_result(value: str | ToolExecutionResult) -> V2ToolExecutionResult:
    legacy = _visible_result(value)
    model_content = legacy.content[:8_192]
    return V2ToolExecutionResult(
        status="completed",
        model_content=model_content,
        display=[TextPart(type="text", text=model_content)] if legacy.visible and model_content else [],
        usage_components={"components": list(legacy.usage)},
        error_code=legacy.warning_code,
    )


def _lumen_idempotency_key(*, run_id: str, tool_call_id: str) -> str:
    """Use the durable run and call identity so retries replay only that call."""
    return f"lumen-{uuid5(NAMESPACE_URL, f'afterglow:lumen:{run_id}:{tool_call_id}')}"


def _visible_result(value: str | ToolExecutionResult) -> ToolExecutionResult:
    return value if isinstance(value, ToolExecutionResult) else ToolExecutionResult(value)
