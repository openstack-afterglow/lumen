"""Honest composition inspection for a prepared context.

The planner records how ``chat_admission`` assembled a request, and the resolver
turns that plan plus the exact message and tool-schema lists into a
:class:`ContextBreakdown`.  Three rules keep the projection trustworthy:

* Every component marked ``included`` is measured with the same counter that
  produces ``ContextState.input_tokens``, and the included components sum to
  exactly that total.  The residual belongs to ``messages`` because that is the
  only open-ended component; the tokenizer's own framing cost is reported
  separately as ``overhead``.
* Material that is not measured is never presented as zero.  Remote MCP schemas
  are listed with ``tokens``/``count`` of ``None`` and repeated in ``uncounted``,
  and when the tokenizer itself is unavailable the composition is still reported
  with null tokens instead of being withheld entirely.
* ``deferred_tools`` names material the provider request does not carry.  At
  request scope, anything the model has since loaded through the tool catalog is
  removed from that list, because the same name is then a counted schema.

Nothing here performs provider IO; preview stays strictly read-only.
"""

from __future__ import annotations

from typing import Any

from lumen.models.chat_contracts import (
    MAX_CONTEXT_COMPONENT_ITEMS,
    ContextBreakdown,
    ContextComponent,
)
from lumen.services.litellm_client import ContextTokenCount, count_context_tokens

# The compaction projection marks its summary turn with this exact sentinel in
# both ``context_store`` (durable checkpoint projection) and ``context_manager``
# (in-run compaction), so the inspector can locate it without guessing.
SUMMARY_SENTINEL = "[context summary-data]"
PLAN_VERSION = 2
_MCP_TOOL_PREFIX = "mcp__"
_MAX_ITEM_CHARS = 190
_INSTRUCTION_ROLES = {"system", "developer"}


def _safe_item(value: object) -> str:
    """Project one bounded, single-line name; never message or prompt content."""
    text = " ".join(str(value if value is not None else "").split())
    return text[:_MAX_ITEM_CHARS] or "(unnamed)"


def _safe_items(values: list[object] | tuple[object, ...]) -> list[str]:
    return [_safe_item(value) for value in list(values)[:MAX_CONTEXT_COMPONENT_ITEMS]]


def instruction_plan_entry(component_id: str, *, slots: int, count: int, items: list[object]) -> dict[str, Any]:
    return {
        "id": component_id,
        "slots": max(0, int(slots)),
        "count": max(0, int(count)),
        "items": _safe_items(items),
    }


def tool_plan_entry(name: object, match: object) -> dict[str, str]:
    """Name one tool-shaped entry plus the provider identity that would include it.

    ``match`` is the exact provider tool name, or a ``prefix__`` when one entry
    (an MCP server) can expand into several provider tools.
    """
    return {"name": _safe_item(name), "match": _safe_item(match)}


def _entry_group(values: list[Any]) -> dict[str, Any]:
    """Keep every identity for later loading; only public item lists are bounded."""
    entries = [value if isinstance(value, dict) else tool_plan_entry(value, value) for value in values]
    return {"total": len(values), "entries": entries}


def build_plan(
    *,
    instructions: list[dict[str, Any]],
    summary_checkpoint_id: str | None,
    message_count: int,
    attachments: list[object],
    deferred_tools: list[Any],
    undiscovered_mcp: list[Any],
) -> dict[str, Any]:
    """Freeze one JSON-safe composition plan for preview and durable replay.

    ``instructions`` are ordered exactly like the injected system preamble, each
    entry consuming ``slots`` instruction messages.  Every later instruction
    message belongs to the conversation itself and is reported as
    ``system_prompt``.
    """
    return {
        "version": PLAN_VERSION,
        "instructions": [dict(entry) for entry in instructions if int(entry.get("slots", 0)) > 0],
        "summary_checkpoint_id": str(summary_checkpoint_id) if summary_checkpoint_id else None,
        "message_count": max(0, int(message_count)),
        "attachments": {"total": len(attachments), "items": _safe_items(attachments)},
        "deferred_tools": _entry_group(list(deferred_tools)),
        "undiscovered_mcp": _entry_group(list(undiscovered_mcp)),
    }


def _partition_messages(
    messages: list[dict[str, Any]], plan: dict[str, Any]
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    """Split messages into planned components plus the open-ended remainder.

    Instruction order survives compaction (``_candidate_messages`` keeps system
    and developer turns ahead of the body), so instruction slots stay valid for
    both the preview and the in-run request projection.
    """
    entries = [entry for entry in plan.get("instructions", []) if isinstance(entry, dict)]
    slot_owner: list[str] = []
    for entry in entries:
        slot_owner.extend([str(entry.get("id"))] * max(0, int(entry.get("slots", 0))))
    checkpoint_id = plan.get("summary_checkpoint_id")
    grouped: dict[str, list[dict[str, Any]]] = {}
    remainder: list[dict[str, Any]] = []
    instruction_index = 0
    summary_taken = not bool(checkpoint_id)
    for message in messages:
        role = message.get("role")
        if role in _INSTRUCTION_ROLES:
            component = slot_owner[instruction_index] if instruction_index < len(slot_owner) else "system_prompt"
            instruction_index += 1
            grouped.setdefault(component, []).append(message)
            continue
        content = message.get("content")
        if not summary_taken and role == "user" and isinstance(content, str) and content.startswith(SUMMARY_SENTINEL):
            summary_taken = True
            grouped.setdefault("summary", []).append(message)
            continue
        remainder.append(message)
    return grouped, remainder


def _split_tool_schemas(tool_schemas: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    functions: list[dict[str, Any]] = []
    mcp: list[dict[str, Any]] = []
    for schema in tool_schemas:
        function = schema.get("function") if isinstance(schema, dict) else None
        name = str((function or {}).get("name") or "")
        (mcp if name.startswith(_MCP_TOOL_PREFIX) else functions).append(schema)
    return functions, mcp


def _schema_names(tool_schemas: list[dict[str, Any]]) -> list[str]:
    names = []
    for schema in tool_schemas[:MAX_CONTEXT_COMPONENT_ITEMS]:
        function = schema.get("function") if isinstance(schema, dict) else None
        names.append(_safe_item((function or {}).get("name")))
    return names


def _all_schema_names(tool_schemas: list[dict[str, Any]]) -> list[str]:
    names = []
    for schema in tool_schemas:
        function = schema.get("function") if isinstance(schema, dict) else None
        name = (function or {}).get("name")
        if isinstance(name, str):
            names.append(name)
    return names


def _remaining_entries(group: object, included_names: list[str]) -> dict[str, Any]:
    """Drop planned entries the request already carries as counted schemas."""
    if not isinstance(group, dict):
        return {"total": 0, "entries": []}
    entries = [entry for entry in group.get("entries") or [] if isinstance(entry, dict)]
    dropped = 0
    remaining = []
    for entry in entries:
        match = str(entry.get("match") or "")
        loaded = any(name == match or (match.endswith("__") and name.startswith(match)) for name in included_names)
        if loaded and match:
            dropped += 1
            continue
        remaining.append(entry)
    total = max(0, int(group.get("total") or 0) - dropped)
    return {"total": total, "entries": remaining}


def _count(model_name: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ContextTokenCount:
    return count_context_tokens(model_name, messages, tools)


def _unmeasured_component(
    component_id: str, count: int | None, items: list[str], *, included: bool
) -> ContextComponent:
    return ContextComponent(
        id=component_id,
        tokens=None,
        measurement="unknown",
        count=count,
        included=included,
        items=_safe_items(items),
    )


def _uncounted_breakdown(
    messages: list[dict[str, Any]],
    tool_schemas: list[dict[str, Any]],
    *,
    plan: dict[str, Any],
    scope: str,
) -> ContextBreakdown:
    """Report the composition without token values when counting is unavailable.

    An unknown token count or an unknown context window must not hide which
    sources the model is being sent; only the numbers are withheld.
    """
    grouped, remainder = _partition_messages(messages, plan)
    entry_by_id = {str(entry.get("id")): entry for entry in plan.get("instructions", []) if isinstance(entry, dict)}
    functions, mcp_schemas = _split_tool_schemas(tool_schemas)
    components: list[ContextComponent] = []
    uncounted: list[str] = []
    for component_id in ("memory", "workspace", "skills", "agent", "system_prompt", "summary"):
        group = grouped.get(component_id) or []
        if not group:
            continue
        entry = entry_by_id.get(component_id, {})
        declared = entry.get("count")
        components.append(
            _unmeasured_component(
                component_id,
                len(group)
                if component_id in {"summary", "system_prompt"} or not isinstance(declared, int)
                else declared,
                [_safe_item(plan.get("summary_checkpoint_id"))]
                if component_id == "summary"
                else _safe_items(list(entry.get("items") or [])),
                included=True,
            )
        )
        uncounted.append(component_id)
    for component_id, schemas in (("tools", functions), ("mcp_tools", mcp_schemas)):
        if not schemas:
            continue
        components.append(_unmeasured_component(component_id, len(schemas), _schema_names(schemas), included=True))
        uncounted.append(component_id)
    components.append(_unmeasured_component("messages", len(remainder), [], included=True))
    uncounted.append("messages")
    components.append(_unmeasured_component("overhead", None, [], included=True))
    uncounted.append("overhead")
    for component in _not_included_components(plan, tool_schemas, scope=scope):
        components.append(component)
        if component.id in {"attachments", "mcp_tools"} and component.id not in uncounted:
            uncounted.append(component.id)
    return ContextBreakdown(
        scope="request" if scope == "request" else "preview",
        complete=False,
        components=components,
        uncounted=sorted(set(uncounted)),
    )


def _not_included_components(
    plan: dict[str, Any], tool_schemas: list[dict[str, Any]], *, scope: str
) -> list[ContextComponent]:
    """Build the components whose material is not part of ``input_tokens``."""
    components: list[ContextComponent] = []
    attachments = plan.get("attachments")
    attachments = attachments if isinstance(attachments, dict) else {"total": 0, "items": []}
    if attachments.get("total"):
        components.append(
            _unmeasured_component(
                "attachments",
                int(attachments["total"]),
                _safe_items(list(attachments.get("items") or [])),
                included=False,
            )
        )
    included_names = _all_schema_names(tool_schemas)
    _functions, mcp_schemas = _split_tool_schemas(tool_schemas)
    if scope != "request" and not mcp_schemas:
        # Preview never performs MCP discovery, so the tool count itself is unknown.
        undiscovered = _remaining_entries(plan.get("undiscovered_mcp"), included_names)
        if undiscovered["total"]:
            components.append(
                _unmeasured_component(
                    "mcp_tools",
                    None,
                    [_safe_item(entry.get("name")) for entry in undiscovered["entries"]],
                    included=False,
                )
            )
    deferred = _remaining_entries(plan.get("deferred_tools"), included_names)
    if deferred["total"]:
        components.append(
            _unmeasured_component(
                "deferred_tools",
                deferred["total"],
                [_safe_item(entry.get("name")) for entry in deferred["entries"]],
                included=False,
            )
        )
    return components


def breakdown(
    messages: list[dict[str, Any]],
    tool_schemas: list[dict[str, Any]],
    *,
    model_name: str,
    plan: dict[str, Any] | None,
    scope: str,
    input_tokens: int | None,
    measurement: str,
) -> ContextBreakdown | None:
    """Resolve one reconciling breakdown, or an incomplete one when counting fails."""
    if plan is None:
        return None
    plan = dict(plan)
    if int(plan.get("version", 0)) != PLAN_VERSION:
        return None

    base = _count(model_name, [], [])
    if input_tokens is None or measurement == "unknown" or base.tokens is None:
        return _uncounted_breakdown(messages, tool_schemas, plan=plan, scope=scope)
    message_total = _count(model_name, messages, [])
    if message_total.tokens is None:
        return _uncounted_breakdown(messages, tool_schemas, plan=plan, scope=scope)

    grouped, remainder = _partition_messages(messages, plan)
    entry_by_id = {str(entry.get("id")): entry for entry in plan.get("instructions", []) if isinstance(entry, dict)}

    components: list[ContextComponent] = []
    uncounted: list[str] = []
    measured_messages = 0
    degraded = False

    def measured(group: list[dict[str, Any]]) -> tuple[int | None, str]:
        """Return one group's marginal cost, excluding the shared request framing."""
        result = _count(model_name, group, [])
        if result.tokens is None:
            return None, "unknown"
        return max(0, result.tokens - base.tokens), result.measurement

    for component_id in ("memory", "workspace", "skills", "agent", "system_prompt", "summary"):
        group = grouped.get(component_id) or []
        if not group:
            continue
        tokens, group_measurement = measured(group)
        entry = entry_by_id.get(component_id, {})
        declared_count = entry.get("count")
        count = (
            len(group)
            if component_id in {"summary", "system_prompt"} or not isinstance(declared_count, int)
            else declared_count
        )
        if tokens is None:
            degraded = True
            uncounted.append(component_id)
        else:
            measured_messages += tokens
        components.append(
            ContextComponent(
                id=component_id,
                tokens=tokens,
                measurement=group_measurement,
                count=count,
                included=True,
                items=[_safe_item(plan.get("summary_checkpoint_id"))]
                if component_id == "summary"
                else _safe_items(list(entry.get("items") or [])),
            )
        )

    # Tool schemas cost exactly the difference between the full request and the
    # same messages without tools; that marginal total is split across function
    # and MCP schemas, which is why a mixed split is reported as estimated.
    functions, mcp_schemas = _split_tool_schemas(tool_schemas)
    tool_marginal = input_tokens - message_total.tokens
    tool_groups = [(name, schemas) for name, schemas in (("tools", functions), ("mcp_tools", mcp_schemas)) if schemas]
    if tool_marginal < 0:
        # The counter disagrees with itself about tool framing; report the gap
        # instead of inventing a zero-cost schema set.
        shares: list[int | None] = [None] * len(tool_groups)
        tool_measurement = "unknown"
        degraded = degraded or bool(tool_groups)
        uncounted.extend(component_id for component_id, _schemas in tool_groups)
        tool_marginal = 0
    elif len(tool_groups) <= 1:
        shares = [tool_marginal] if tool_groups else []
        tool_measurement = measurement
    else:
        isolated = [
            max(0, (_count(model_name, [], schemas).tokens or base.tokens) - base.tokens) for _, schemas in tool_groups
        ]
        weight = sum(isolated)
        first = round(tool_marginal * isolated[0] / weight) if weight else tool_marginal // max(1, len(tool_groups))
        shares = [first, tool_marginal - first]
        tool_measurement = "estimated"
    for (component_id, schemas), share in zip(tool_groups, shares, strict=True):
        components.append(
            ContextComponent(
                id=component_id,
                tokens=share,
                measurement=tool_measurement if share is not None else "unknown",
                count=len(schemas),
                included=True,
                items=_schema_names(schemas),
            )
        )

    # ``messages`` is the only open-ended component, so it carries the residual
    # and keeps the included components summing to the canonical total.
    residual = message_total.tokens - base.tokens - measured_messages
    message_tokens: int | None = residual
    message_measurement = message_total.measurement
    if residual < 0 or degraded:
        message_tokens = None
        message_measurement = "unknown"
        if "messages" not in uncounted:
            uncounted.append("messages")
    components.append(
        ContextComponent(
            id="messages",
            tokens=message_tokens,
            measurement=message_measurement,
            count=len(remainder),
            included=True,
            items=[],
        )
    )
    components.append(
        ContextComponent(
            id="overhead",
            tokens=base.tokens,
            measurement=base.measurement,
            count=None,
            included=True,
            items=[],
        )
    )

    for component in _not_included_components(plan, tool_schemas, scope=scope):
        components.append(component)
        if component.id in {"attachments", "mcp_tools"}:
            uncounted.append(component.id)

    return ContextBreakdown(
        scope="request" if scope == "request" else "preview",
        complete=not uncounted,
        components=components,
        uncounted=sorted(set(uncounted)),
    )
