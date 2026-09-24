"""Default independently installable tool provider for Lumen.

The wheel does not import Lumen's application, ORM, settings, or service modules.
Its only authority is the narrow host capabilities supplied by the runtime.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from lumen_plugin_api.contracts import (
    ExecutionContext,
    Namespace,
    PluginError,
    PluginExport,
    PluginHost,
    PluginManifest,
)
from lumen_plugin_api.tools import ToolBinding, ToolDefinition, ToolSpec

from .builtin import BUILTIN_TOOLS, builtin_tool, execute_builtin
from .custom_http import CUSTOM_HTTP_EXPORT_KEY, CustomHttpTool, build_definition
from .custom_http import execute as execute_custom_http
from .managed import (
    MANAGED_ADVISOR_KEY,
    MANAGED_TOOLS,
    MANAGED_WEB_FETCH_KEY,
    MANAGED_WEB_SEARCH_KEY,
    execute_managed_advisor,
    execute_managed_fetch,
    execute_managed_search,
)


class DefaultTools:
    """Static exports plus a parameterized binding for existing custom HTTP rows."""

    manifest = PluginManifest(
        id="default-tools",
        kind="tools",
        version="0.1.0",
        required_capabilities=("conversations", "extensions", "advisor", "public_http"),
        exports=(
            *(tool.export for tool in BUILTIN_TOOLS),
            *(PluginExport(key=item.key, kind="tool", name=item.key) for item in MANAGED_TOOLS),
        ),
    )

    async def start(self, host: PluginHost) -> None:
        self._host = host

    async def close(self) -> None:
        self._host = None

    def catalog(self) -> tuple[PluginExport, ...]:
        return self.manifest.exports

    async def bind(self, spec: ToolSpec, context: ExecutionContext, host: PluginHost) -> ToolBinding:
        if spec.identity.plugin_id != self.manifest.id or spec.identity.version != self.manifest.version:
            raise PluginError("plugin_incompatible", "tool plugin identity mismatch")
        builtin = builtin_tool(spec.export_key)
        if builtin is not None:
            if host.conversations is None:
                raise PluginError("plugin_unavailable", "conversation host capability is unavailable")
            definition = ToolDefinition(
                name=builtin.export.key,
                description={
                    "list_my_conversations": "현재 사용자의 채팅 대화 목록(제목·모델)을 반환한다.",
                    "get_conversation_detail": "특정 대화의 요약(메시지 수·모델)을 반환한다.",
                }[builtin.export.key],
                input_schema=builtin.input_schema,
                effect="read",
                parallel_safe=True,
                source="builtin",
                activity_category="기본 도구",
            )

            async def execute(arguments: dict[str, Any], supplied: ExecutionContext):
                _require_context(context, supplied)
                return await execute_builtin(builtin, arguments, supplied, host.conversations)

            return ToolBinding(definition=definition, execute=execute)

        if spec.export_key == CUSTOM_HTTP_EXPORT_KEY:
            return await self._bind_custom_http(spec, context, host)

        if spec.export_key in {MANAGED_WEB_SEARCH_KEY, MANAGED_WEB_FETCH_KEY, MANAGED_ADVISOR_KEY}:
            return self._bind_managed(spec, context, host)

        raise PluginError("plugin_unavailable", "unknown tool export")

    async def _bind_custom_http(self, spec: ToolSpec, context: ExecutionContext, host: PluginHost) -> ToolBinding:
        if host.extensions is None or host.public_http is None:
            raise PluginError("plugin_unavailable", "custom HTTP tool host capability is unavailable")
        configuration = deepcopy(spec.configuration)
        identifier = configuration.get("id")
        if not isinstance(identifier, int) or isinstance(identifier, bool):
            raise PluginError("plugin_incompatible", "custom HTTP tool ID is invalid")
        namespace = Namespace(user_id=context.user_id, project_id=context.project_id)
        if context.identity is not None and context.identity != spec.identity:
            raise PluginError("plugin_authority_revoked", "tool execution identity changed")
        current = await host.extensions.resolve("tool", identifier, namespace)
        if not _matching_custom_config(current, configuration):
            raise PluginError("plugin_configuration_changed", "custom HTTP tool changed")
        tool = CustomHttpTool(
            identifier=identifier,
            name=str(current.get("name") or ""),
            description=str(current.get("description") or ""),
            url=str(current.get("url") or ""),
            method=str(current.get("method") or "GET").upper(),
            params_schema=deepcopy(current.get("params_schema")) if isinstance(current.get("params_schema"), dict) else None,
            timeout_seconds=int(current.get("timeout_seconds") or 10),
            effect=str(current.get("effect") or "external_mutation"),
        )
        definition = build_definition(tool)

        async def execute(arguments: dict[str, Any], supplied: ExecutionContext):
            _require_context(context, supplied)
            if supplied.identity is not None and supplied.identity != spec.identity:
                raise PluginError("plugin_authority_revoked", "tool execution identity changed")
            latest = await host.extensions.resolve("tool", identifier, namespace)
            if not _matching_custom_config(latest, configuration):
                raise PluginError("plugin_configuration_changed", "custom HTTP tool changed before dispatch")
            return await execute_custom_http(tool, arguments, supplied, host.public_http)

        return ToolBinding(definition=definition, execute=execute)

    def _bind_managed(self, spec: ToolSpec, context: ExecutionContext, host: PluginHost) -> ToolBinding:
        key = spec.export_key
        meta = next(item for item in MANAGED_TOOLS if item.key == key)
        schema = {
            "type": "object",
            "properties": {meta.property_name: {"type": "string"}},
            "required": [meta.property_name],
            "additionalProperties": False,
        }
        definition = ToolDefinition(
            name=key,
            description=meta.description,
            input_schema=schema,
            effect="read",
            parallel_safe=False,
            source="managed",
            activity_category="관리형 도구",
        )
        configuration = spec.configuration
        if key == MANAGED_WEB_FETCH_KEY:
            if host.public_http is None:
                raise PluginError("plugin_unavailable", "public HTTP host capability is unavailable")

            async def execute(arguments: dict[str, Any], supplied: ExecutionContext):
                _require_context(context, supplied)
                return await execute_managed_fetch(arguments, configuration, supplied, host.public_http)

        else:
            if host.advisor is None:
                raise PluginError("plugin_unavailable", "advisor host capability is unavailable")
            execute_managed = execute_managed_search if key == MANAGED_WEB_SEARCH_KEY else execute_managed_advisor

            async def execute(arguments: dict[str, Any], supplied: ExecutionContext):
                _require_context(context, supplied)
                return await execute_managed(arguments, configuration, supplied, host.advisor)

        return ToolBinding(definition=definition, execute=execute)


def _require_context(bound: ExecutionContext, supplied: ExecutionContext) -> None:
    if (bound.user_id, bound.project_id, bound.run_id) != (supplied.user_id, supplied.project_id, supplied.run_id):
        raise PluginError("plugin_authority_revoked", "tool execution context changed")


def _matching_custom_config(current: dict[str, Any], frozen: dict[str, Any]) -> bool:
    fields = ("id", "name", "description", "url", "method", "params_schema", "timeout_seconds", "effect", "config_version", "load_policy")
    return bool(current.get("is_active")) and all(current.get(key) == frozen.get(key) for key in fields)


def create_plugin() -> DefaultTools:
    return DefaultTools()
