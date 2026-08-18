"""Tool binding construction and deferred catalog loading."""

from __future__ import annotations

import json
import logging
import re

from lumen.models.chat_contracts import TextPart
from lumen.services import mcp_adapter as mcp_ledger
from lumen.services import mcp_adapter as mcp_lumen
from lumen.services import mcp_adapter as mcp_registry
from lumen.services import mcp_client
from lumen.services.agent_protocol import ToolBinding, ToolDefinition
from lumen.services.agent_protocol import ToolExecutionResult as V2ToolExecutionResult
from lumen.services.tools import ToolContext

from .contracts import (
    ToolBindingSession,
    _lumen_idempotency_key,
    _v2_config_fingerprint,
    _v2_destination_origin,
    _v2_effect,
    _v2_provider_name,
    _v2_result,
    v2_builtin_tool_bindings,
)
from .selection import _load_custom, _load_mcp

logger = logging.getLogger(__name__)


async def _lumen_registry_bindings(ctx: ToolContext) -> tuple[ToolBinding, ...]:
    """Expose only the selected grant's explicit consumer-cloud registry entries."""
    try:
        if ctx.lumen_snapshot_frozen:
            snapshot = mcp_lumen.frozen_snapshot(ctx.lumen_snapshot)
        else:
            snapshot = await mcp_lumen.selected_lumen_snapshot(user_id=ctx.user_id, project_id=ctx.project_id)
        if snapshot is None:
            return ()
        if snapshot.user_id != ctx.user_id or snapshot.project_id != ctx.project_id:
            return ()
        principal = await mcp_lumen.resolve_lumen_principal(snapshot)
    except mcp_lumen.McpLumenAuthorityError:
        return ()

    bindings: list[ToolBinding] = []
    for entry in mcp_registry.enabled_entries(principal):
        definition = ToolDefinition(
            name=entry.name,
            description=entry.description,
            input_schema={**entry.input_schema(), "additionalProperties": False},
            effect=entry.effect,
            parallel_safe=entry.effect == "read",
            source="managed",
            activity_category="Lumen",
        )
        config_fingerprint = _v2_config_fingerprint(
            {
                "grant_id": snapshot.grant_id,
                "credential_epoch": snapshot.credential_epoch,
                "selection_generation": snapshot.selection_generation,
                "registry_version": mcp_registry.REGISTRY_VERSION,
                "service_fingerprint": mcp_registry.enabled_service_fingerprint(principal),
                "tool_name": entry.name,
            }
        )

        async def execute(
            arguments: dict[str, object],
            context: object,
            *,
            registry_entry=entry,
            frozen_snapshot=snapshot,
        ) -> V2ToolExecutionResult:
            if (
                not isinstance(context, ToolContext)
                or context.user_id != frozen_snapshot.user_id
                or context.project_id != frozen_snapshot.project_id
            ):
                return V2ToolExecutionResult(
                    status="failed",
                    model_content="Cloud tool execution context is invalid.",
                    error_code="invalid_tool_context",
                )
            try:
                current_principal = await mcp_lumen.resolve_lumen_principal(frozen_snapshot)
                parsed = mcp_registry.parse_entry_arguments(registry_entry, arguments)
                normalized = parsed.model_dump(mode="json")
                cloud_context = mcp_registry.ConsumerCloudContext(principal=current_principal)
                if registry_entry.effect == "read":
                    try:
                        result = await mcp_registry.dispatch(cloud_context, entry=registry_entry, arguments=parsed)
                        payload = mcp_registry.output_payload(registry_entry, result)
                    except Exception as exc:
                        await mcp_ledger.record_read_invocation(
                            current_principal,
                            entry=registry_entry,
                            arguments=normalized,
                            status="failed",
                            error=str(exc),
                            source="lumen",
                        )
                        raise
                    await mcp_ledger.record_read_invocation(
                        current_principal,
                        entry=registry_entry,
                        arguments=normalized,
                        status="succeeded",
                        source="lumen",
                    )
                else:
                    if not context.run_id or not context.tool_call_id:
                        raise mcp_ledger.McpInvocationError("Lumen mutation requires a durable tool call")
                    claim = await mcp_ledger.claim_mutation(
                        current_principal,
                        entry=registry_entry,
                        arguments=normalized,
                        idempotency_key=_lumen_idempotency_key(
                            run_id=context.run_id,
                            tool_call_id=context.tool_call_id,
                        ),
                        source="lumen",
                    )
                    if claim.state == "replay":
                        assert claim.result is not None
                        payload = claim.result
                    elif claim.state in {"in_progress", "unknown", "failed"}:
                        raise mcp_ledger.McpInvocationError(claim.error or "Cloud mutation is unavailable")
                    else:
                        try:
                            await mcp_registry.build_mutation_preview(
                                cloud_context, entry=registry_entry, arguments=parsed
                            )
                        except Exception:
                            await mcp_ledger.fail_pre_dispatch(
                                current_principal,
                                invocation_id=claim.invocation_id,
                                error="Lumen pre-dispatch validation failed",
                                source="lumen",
                            )
                            raise
                        try:
                            current_principal = await mcp_lumen.resolve_lumen_principal(frozen_snapshot)
                        except Exception:
                            await mcp_ledger.fail_pre_dispatch(
                                current_principal,
                                invocation_id=claim.invocation_id,
                                error="Lumen delegated MCP selection changed before dispatch",
                                source="lumen",
                            )
                            raise
                        cloud_context = mcp_registry.ConsumerCloudContext(principal=current_principal)
                        await mcp_ledger.authorize_mutation_dispatch(
                            current_principal, invocation_id=claim.invocation_id, source="lumen"
                        )
                        try:
                            result = await mcp_registry.dispatch(cloud_context, entry=registry_entry, arguments=parsed)
                            payload = mcp_registry.output_payload(registry_entry, result)
                            await mcp_ledger.complete_mutation(
                                current_principal,
                                invocation_id=claim.invocation_id,
                                result=payload,
                                source="lumen",
                            )
                        except Exception:
                            try:
                                await mcp_ledger.complete_mutation(
                                    current_principal,
                                    invocation_id=claim.invocation_id,
                                    error="Lumen mutation outcome is unknown after dispatch authorization",
                                    source="lumen",
                                )
                            except mcp_ledger.McpInvocationError:
                                pass
                            raise
                content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
                return V2ToolExecutionResult(
                    status="completed",
                    model_content=content,
                    display=[TextPart(type="text", text=content)],
                )
            except Exception:
                logger.warning("Lumen cloud registry call failed tool=%s", registry_entry.name, exc_info=True)
                return V2ToolExecutionResult(
                    status="failed",
                    model_content="Cloud control-plane call was denied or is unavailable.",
                    error_code="cloud_control_unavailable",
                )

        async def preview(
            arguments: dict[str, object],
            context: object,
            *,
            registry_entry=entry,
            frozen_snapshot=snapshot,
        ) -> dict[str, object]:
            if registry_entry.effect != "external_mutation":
                return {}
            if (
                not isinstance(context, ToolContext)
                or context.user_id != frozen_snapshot.user_id
                or context.project_id != frozen_snapshot.project_id
            ):
                raise ValueError("Cloud tool execution context is invalid")
            current_principal = await mcp_lumen.resolve_lumen_principal(frozen_snapshot)
            parsed = mcp_registry.parse_entry_arguments(registry_entry, arguments)
            plan = await mcp_registry.build_mutation_preview(
                mcp_registry.ConsumerCloudContext(principal=current_principal),
                entry=registry_entry,
                arguments=parsed,
            )
            detail = f"{plan.intended_transition}: {plan.resource_identity}" + (
                f" (current state: {plan.current_state})" if plan.current_state else ""
            )
            return {
                "preview": [TextPart(type="text", text=detail).model_dump(mode="json")],
                "expected_state_revision": None,
                "writer_fence": None,
            }

        bindings.append(
            ToolBinding(
                definition=definition,
                execute=execute,
                preview=preview if entry.effect == "external_mutation" else None,
                config_fingerprint=config_fingerprint,
            )
        )
    return tuple(bindings)


def _binding_schema(binding: ToolBinding) -> dict:
    definition = binding.definition
    return {
        "type": "function",
        "function": {
            "name": definition.name,
            "description": definition.description,
            "parameters": definition.input_schema,
        },
    }


def _closed_mcp_input_schema(raw_schema: object) -> dict:
    """Close explicitly enumerated MCP object schemas without permitting dynamic maps."""
    source = raw_schema if isinstance(raw_schema, dict) else {"type": "object", "properties": {}}

    def normalize(value: object) -> object:
        if isinstance(value, dict):
            normalized = {key: normalize(child) for key, child in value.items()}
            if normalized.get("type") == "object" and isinstance(normalized.get("properties"), dict):
                normalized["additionalProperties"] = False
            return normalized
        if isinstance(value, list):
            return [normalize(child) for child in value]
        return value

    normalized = normalize(source)
    return normalized if isinstance(normalized, dict) else {"type": "object", "properties": {}}


_CATALOG_TERM_RE = re.compile(r"[a-z0-9_]{2,}|[가-힣]{2,}", re.IGNORECASE)


def _catalog_matches(binding: ToolBinding, query: str) -> bool:
    searchable = " ".join(
        (
            binding.definition.name,
            binding.definition.description,
            binding.definition.source,
            binding.definition.activity_category,
        )
    ).casefold()
    query = query.casefold()
    if query in searchable:
        return True
    query_terms = set(_CATALOG_TERM_RE.findall(query))
    searchable_terms = set(_CATALOG_TERM_RE.findall(searchable))
    return bool(query_terms & searchable_terms)


async def _search_and_load_bindings(
    arguments: dict[str, object],
    context: ToolContext,
    target_bindings: dict[str, ToolBinding],
    *,
    include_managed: bool,
) -> V2ToolExecutionResult:
    session = context.binding_session
    if not isinstance(session, ToolBindingSession):
        return V2ToolExecutionResult(
            status="failed",
            model_content="Tool execution context is invalid.",
            error_code="invalid_tool_context",
        )
    query = arguments.get("query")
    category = arguments.get("category")
    maximum = arguments.get("max_results", 5)
    if not isinstance(query, str) or not (query := query.strip()) or len(query) > 160:
        return V2ToolExecutionResult(
            status="failed",
            model_content="Provide a concise tool capability query.",
            error_code="invalid_tool_catalog_query",
        )
    if category is not None and (not isinstance(category, str) or len(category.strip()) > 100):
        return V2ToolExecutionResult(
            status="failed",
            model_content="category must be a concise category filter.",
            error_code="invalid_tool_catalog_category",
        )
    if not isinstance(maximum, int) or not 1 <= maximum <= 8:
        return V2ToolExecutionResult(
            status="failed",
            model_content="max_results must be between 1 and 8.",
            error_code="invalid_tool_catalog_limit",
        )
    deferred = await v2_tool_bindings(
        context,
        extension_load_policy="on_demand",
        include_platform=False,
        include_managed=include_managed,
    )
    session.deferred_bindings = deferred
    category_needle = category.strip().casefold() if isinstance(category, str) and category.strip() else None
    selected = [
        binding
        for binding in deferred.values()
        if _catalog_matches(binding, query)
        and (
            category_needle is None
            or category_needle in binding.definition.activity_category.casefold()
            or category_needle in binding.definition.source.casefold()
        )
    ][:maximum]
    for binding in selected:
        target_bindings[binding.definition.name] = binding
        session.loaded_names.add(binding.definition.name)
    catalog = [
        {
            "name": binding.definition.name,
            "description": binding.definition.description[:280],
            "source": binding.definition.source,
            "category": binding.definition.activity_category,
        }
        for binding in selected
    ]
    return V2ToolExecutionResult(
        status="completed",
        model_content=json.dumps(
            {"loaded_tools": [item["name"] for item in catalog], "tools": catalog},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        display=[TextPart(type="text", text=f"도구 후보 {len(catalog)}개를 확인하고 기술을 로드했습니다.")],
    )


def _catalog_binding(target_bindings: dict[str, ToolBinding], *, include_managed: bool) -> ToolBinding:
    async def list_available_tools(arguments: dict[str, object], context: object) -> V2ToolExecutionResult:
        if not isinstance(context, ToolContext):
            return V2ToolExecutionResult(
                status="failed",
                model_content="Tool execution context is invalid.",
                error_code="invalid_tool_context",
            )
        return await _search_and_load_bindings(
            arguments,
            context,
            target_bindings,
            include_managed=include_managed,
        )

    return ToolBinding(
        definition=ToolDefinition(
            name="list_available_tools",
            description="Search eligible on-demand tools by capability and load at most eight schemas for this run.",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "minLength": 1, "maxLength": 160},
                    "category": {"type": "string", "minLength": 1, "maxLength": 100},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 8},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            effect="read",
            parallel_safe=False,
            source="builtin",
            activity_category="도구 준비",
        ),
        execute=list_available_tools,
    )


async def v2_tool_bindings(
    ctx: ToolContext,
    *,
    extension_load_policy: str | None = None,
    include_platform: bool = True,
    include_managed: bool = False,
) -> dict[str, ToolBinding]:
    """Resolve tenant-scoped bindings, optionally restricted to one extension load policy."""
    if not ctx.tools_enabled:
        return {}
    bindings = v2_builtin_tool_bindings() if include_platform else {}

    def add(binding: ToolBinding) -> None:
        if binding.definition.name in bindings:
            raise ValueError(f"duplicate v2 tool name: {binding.definition.name}")
        bindings[binding.definition.name] = binding

    if include_managed:
        for binding in await _lumen_registry_bindings(ctx):
            add(binding)

    for custom in await _load_custom(ctx):
        if extension_load_policy is not None and custom.get("load_policy") != extension_load_policy:
            continue
        identifier = custom.get("id")
        if not isinstance(identifier, int):
            continue
        destination_origin = _v2_destination_origin(custom.get("url"))
        if destination_origin is None:
            logger.warning("custom tool without a canonical destination excluded from v2 bindings id=%s", identifier)
            continue
        try:
            definition = ToolDefinition(
                name=_v2_provider_name("custom", identifier, custom.get("name")),
                description=str(custom.get("description") or custom.get("name") or "Custom HTTP tool"),
                input_schema={
                    **(custom.get("params_schema") or {"type": "object", "properties": {}}),
                    "additionalProperties": False,
                },
                effect=_v2_effect(custom.get("effect")),
                source="custom_http",
                activity_category="커스텀 도구",
            )
        except (TypeError, ValueError):
            logger.warning("invalid custom tool excluded from v2 bindings id=%s", identifier)
            continue

        async def execute(
            arguments: dict[str, object], context: object, *, tool_id=identifier
        ) -> V2ToolExecutionResult:
            if not isinstance(context, ToolContext):
                return V2ToolExecutionResult(
                    status="failed",
                    model_content="Tool execution context is invalid.",
                    error_code="invalid_tool_context",
                )
            current = next((item for item in await _load_custom(context) if item.get("id") == tool_id), None)
            if current is None:
                return V2ToolExecutionResult(
                    status="failed",
                    model_content="The selected custom tool is no longer available.",
                    error_code="extension_unavailable",
                )
            from .dispatch import _execute_custom_http_tool

            return _v2_result(await _execute_custom_http_tool(current, arguments, context))

        add(
            ToolBinding(
                definition=definition,
                execute=execute,
                config_fingerprint=_v2_config_fingerprint(
                    {
                        "id": identifier,
                        "name": custom.get("name"),
                        "url": custom.get("url"),
                        "method": custom.get("method"),
                        "params_schema": custom.get("params_schema"),
                        "effect": custom.get("effect"),
                        "config_version": custom.get("config_version"),
                    }
                ),
                destination_origin=destination_origin,
                load_policy=custom.get("load_policy")
                if custom.get("load_policy") in {"preloaded", "on_demand"}
                else None,
            )
        )

    for server in await _load_mcp(ctx):
        if extension_load_policy is not None and server.get("load_policy") != extension_load_policy:
            continue
        identifier = server.get("id")
        if not isinstance(identifier, int):
            continue
        destination_origin = _v2_destination_origin(server.get("url"))
        if destination_origin is None:
            logger.warning("MCP server without a canonical destination excluded from v2 bindings server=%s", identifier)
            continue
        try:
            discovered = await mcp_client.list_tools(server)
        except Exception:
            logger.warning("MCP discovery failed for v2 bindings server=%s", identifier, exc_info=True)
            continue
        for method in discovered:
            if not isinstance(method, dict) or not isinstance(method.get("name"), str):
                continue
            try:
                definition = ToolDefinition(
                    name=_v2_provider_name("mcp", identifier, method["name"]),
                    description=str(method.get("description") or method["name"]),
                    input_schema=_closed_mcp_input_schema(method.get("input_schema")),
                    effect=_v2_effect((server.get("effect_overrides") or {}).get(method["name"])),
                    source="mcp",
                    activity_category=f"MCP · {str(server.get('name') or '서버')[:80]}",
                )
            except (TypeError, ValueError) as exc:
                logger.warning(
                    "invalid MCP tool excluded from v2 bindings server=%s name=%r reason=%s",
                    identifier,
                    method.get("name"),
                    exc,
                )
                continue

            async def execute(
                arguments: dict[str, object],
                context: object,
                *,
                server_id=identifier,
                method_name=method["name"],
            ) -> V2ToolExecutionResult:
                if not isinstance(context, ToolContext):
                    return V2ToolExecutionResult(
                        status="failed",
                        model_content="Tool execution context is invalid.",
                        error_code="invalid_tool_context",
                    )
                current = next((item for item in await _load_mcp(context) if item.get("id") == server_id), None)
                if current is None:
                    return V2ToolExecutionResult(
                        status="failed",
                        model_content="The selected MCP server is no longer available.",
                        error_code="extension_unavailable",
                    )
                return _v2_result(await mcp_client.call_tool(current, method_name, arguments))

            add(
                ToolBinding(
                    definition=definition,
                    execute=execute,
                    config_fingerprint=_v2_config_fingerprint(
                        {
                            "id": identifier,
                            "url": server.get("url"),
                            "effect_overrides": server.get("effect_overrides"),
                            "config_version": server.get("config_version"),
                        }
                    ),
                    destination_origin=destination_origin,
                    load_policy=server.get("load_policy")
                    if server.get("load_policy") in {"preloaded", "on_demand"}
                    else None,
                )
            )

    if (
        include_platform
        and extension_load_policy in {None, "preloaded"}
        and isinstance(ctx.binding_session, ToolBindingSession)
    ):
        add(_catalog_binding(bindings, include_managed=True))
    return bindings


async def _legacy_dynamic_bindings(ctx: ToolContext) -> dict[str, ToolBinding]:
    session = ctx.binding_session
    if not isinstance(session, ToolBindingSession):
        return {}
    if not session.legacy_preloaded:
        session.legacy_bindings.update(
            await v2_tool_bindings(
                ctx,
                extension_load_policy="preloaded",
                include_platform=False,
            )
        )
        session.legacy_preloaded = True
    bindings = dict(session.legacy_bindings)
    bindings["list_available_tools"] = _catalog_binding(session.legacy_bindings, include_managed=False)
    return bindings


async def restore_v2_deferred_bindings(
    ctx: ToolContext,
    bindings: dict[str, ToolBinding],
    names: list[str],
    *,
    include_managed: bool,
) -> None:
    """Rebuild previously loaded bindings after durable graph replay."""
    if not isinstance(ctx.binding_session, ToolBindingSession) or not names:
        return
    deferred = await v2_tool_bindings(
        ctx,
        extension_load_policy="on_demand",
        include_platform=False,
        include_managed=include_managed,
    )
    ctx.binding_session.deferred_bindings = deferred
    unavailable = [name for name in names if name not in deferred]
    if unavailable:
        raise RuntimeError("a previously loaded tool is no longer eligible for this run")
    for name in names:
        bindings[name] = deferred[name]
        ctx.binding_session.loaded_names.add(name)
