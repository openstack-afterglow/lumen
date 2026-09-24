"""Shared tool binding and execution contracts."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from uuid import NAMESPACE_URL, uuid5

from lumen_plugin_api.contracts import ExecutionContext
from lumen_plugin_api.tools import ToolBinding, ToolFilePart, ToolTextPart
from lumen_plugin_api.tools import ToolExecutionResult as V2ToolExecutionResult

from lumen.models.chat_contracts import FilePart, TextPart
from lumen.services import ssrf
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

    __slots__ = ("content", "usage", "visible", "warning_code", "status")

    def __init__(
        self,
        content: str,
        *,
        visible: bool = True,
        usage: tuple[dict[str, object], ...] = (),
        warning_code: str | None = None,
        status: str = "completed",
    ) -> None:
        if status not in {"completed", "failed", "denied", "canceled"}:
            raise ValueError("tool execution status is invalid")
        self.content = content
        self.visible = visible
        self.usage = usage
        self.warning_code = warning_code
        self.status = status


def execution_context(ctx: ToolContext) -> ExecutionContext:
    """Bridge authenticated run state, never model arguments, to the public plugin API."""
    return ExecutionContext(
        user_id=ctx.user_id,
        project_id=ctx.project_id,
        run_id=ctx.run_id,
        call_id=ctx.tool_call_id,
    )


def native_tool_part(part: ToolTextPart | ToolFilePart) -> TextPart | FilePart:
    """Explicit DTO conversion at the core wire boundary."""
    if isinstance(part, ToolTextPart):
        return TextPart(type="text", text=part.text)
    if isinstance(part, ToolFilePart):
        return FilePart(type="file", asset_id=part.asset_id, mime_type=part.mime_type, name=part.name, size_bytes=part.size_bytes)
    raise ValueError("unsupported tool display part")


async def v2_builtin_tool_bindings(ctx: ToolContext) -> dict[str, ToolBinding]:
    """Bind selected installed builtin exports, with no core handler registry."""
    from lumen.plugins.registry import get_plugin
    from lumen.plugins.tools_host import bind_default_tool

    provider = get_plugin("tools", "default-tools")
    public_context = execution_context(ctx)
    bindings: dict[str, ToolBinding] = {}
    for export in provider.catalog():
        if export.key not in {"list_my_conversations", "get_conversation_detail"}:
            continue
        bound = await bind_default_tool(export.key, public_context)

        async def execute(arguments: dict[str, object], context: object, *, binding=bound) -> V2ToolExecutionResult:
            if not isinstance(context, ToolContext):
                return V2ToolExecutionResult(status="failed", model_content="Tool execution context is invalid.", error_code="invalid_tool_context")
            return await binding.execute(arguments, execution_context(context))

        bindings[bound.definition.name] = ToolBinding(definition=bound.definition, execute=execute)
    return bindings


def _v2_provider_name(prefix: str, identifier: int, name: object) -> str:
    raw_name = str(name)
    normalized = re.sub(r"[^A-Za-z0-9_]+", "_", raw_name).strip("_") or "tool"
    digest = hashlib.sha256(raw_name.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}__{identifier}__{normalized[:96]}_{digest}"[:128]


async def custom_tool_function_schema(tool_def: dict[str, object], ctx: ToolContext) -> dict[str, object]:
    """Bind the scoped selected row; admission keeps only a secret-free projection."""
    from lumen_plugin_api.contracts import Namespace, PluginError

    from lumen.plugins.registry import get_registry
    from lumen.plugins.tools_host import bind_default_tool
    from lumen.services.extensions_store import selection_fingerprint

    identifier = tool_def.get("id")
    if not isinstance(identifier, int) or isinstance(identifier, bool) or identifier < 1:
        raise ValueError("invalid custom tool identifier")
    host = get_registry().host("default-tools")
    if host.extensions is None:
        raise PluginError("plugin_unavailable", "tool extension access is unavailable")
    row = await host.extensions.resolve(
        "tool", identifier, Namespace(user_id=ctx.user_id, project_id=ctx.project_id)
    )
    expected = tool_def.get("config_fingerprint")
    if expected is not None and expected != selection_fingerprint(row):
        raise PluginError("plugin_configuration_changed", "custom HTTP tool changed before preview")
    definition = (await bind_default_tool("custom_http", execution_context(ctx), row)).definition
    return {
        "name": definition.name,
        "description": definition.description,
        "parameters": definition.input_schema,
    }


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
        status=legacy.status,
        model_content=model_content,
        display=[ToolTextPart(text=model_content)] if legacy.visible and model_content else [],
        usage_components={"components": list(legacy.usage)},
        error_code=legacy.warning_code,
    )


def _lumen_idempotency_key(*, run_id: str, tool_call_id: str) -> str:
    """Use the durable run and call identity so retries replay only that call."""
    return f"lumen-{uuid5(NAMESPACE_URL, f'afterglow:lumen:{run_id}:{tool_call_id}')}"


def _visible_result(value: str | ToolExecutionResult) -> ToolExecutionResult:
    return value if isinstance(value, ToolExecutionResult) else ToolExecutionResult(value)
