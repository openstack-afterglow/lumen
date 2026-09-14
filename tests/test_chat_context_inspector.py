import pytest

from lumen.models.chat_contracts import ContextState
from lumen.services import context_inspector as inspector
from lumen.services import context_manager

_MODEL = "gpt-4o-mini"


def _plan(**overrides):
    base = {
        "instructions": [
            inspector.instruction_plan_entry("memory", slots=1, count=3, items=[]),
            inspector.instruction_plan_entry("workspace", slots=1, count=1, items=["workspace:7"]),
            inspector.instruction_plan_entry("skills", slots=2, count=2, items=["Docs", "SQL"]),
            inspector.instruction_plan_entry("agent", slots=1, count=1, items=["Researcher"]),
        ],
        "summary_checkpoint_id": None,
        "message_count": 3,
        "attachments": [],
        "deferred_tools": [],
        "undiscovered_mcp": [],
    }
    base.update(overrides)
    return inspector.build_plan(**base)


def _messages(*, summary: str | None = None):
    preamble = [
        {"role": "system", "content": "remembered facts about the user"},
        {"role": "system", "content": "workspace instructions"},
        {"role": "system", "content": "first skill instructions"},
        {"role": "system", "content": "second skill instructions"},
        {"role": "system", "content": "agent instructions"},
    ]
    history = [
        {"role": "user", "content": "first question from the user"},
        {"role": "assistant", "content": "an answer"},
        {"role": "user", "content": "draft the model has not answered yet"},
    ]
    if summary is not None:
        history.insert(0, {"role": "user", "content": f"{inspector.SUMMARY_SENTINEL}\n{summary}"})
    return [*preamble, *history]


def _tool(name: str):
    return {
        "type": "function",
        "function": {"name": name, "description": f"{name} description", "parameters": {"type": "object"}},
    }


def _state(messages, tools, *, plan, scope="preview"):
    return context_manager.context_state(
        messages,
        tools,
        model_name=_MODEL,
        context_limit=128_000,
        output_reserve=4_096,
        revision="rev-1",
        plan=plan,
        scope=scope,
    )


def _by_id(breakdown):
    return {component.id: component for component in breakdown.components}


def test_included_components_reconcile_exactly_to_input_tokens():
    state = _state(_messages(), [_tool("list_my_conversations")], plan=_plan())
    breakdown = state.breakdown

    assert breakdown.scope == "preview"
    assert breakdown.complete is True
    assert breakdown.uncounted == []
    assert sum(component.tokens for component in breakdown.components if component.included) == state.input_tokens

    components = _by_id(breakdown)
    assert components["memory"].count == 3
    assert components["skills"].items == ["Docs", "SQL"]
    assert components["agent"].items == ["Researcher"]
    assert components["messages"].count == 3
    assert components["tools"].items == ["list_my_conversations"]
    assert components["overhead"].tokens >= 0
    assert all(component.tokens > 0 for component in breakdown.components if component.id != "overhead")


def test_summary_and_conversation_instructions_are_attributed_separately():
    messages = _messages(summary="what was decided earlier")
    messages.insert(5, {"role": "system", "content": "conversation owned system turn"})
    plan = _plan(summary_checkpoint_id="ckpt-9")

    breakdown = _state(messages, [], plan=plan).breakdown
    components = _by_id(breakdown)

    assert components["summary"].items == ["ckpt-9"]
    assert components["summary"].count == 1
    assert components["system_prompt"].count == 1
    # The summary turn is not double counted as ordinary conversation material.
    assert components["messages"].count == 3
    assert breakdown.complete is True


def test_instruction_attribution_survives_in_run_compaction():
    """Compaction keeps instruction order, so preamble slots stay meaningful."""
    messages = _messages()
    retained, _older = context_manager._required_messages(messages)
    compacted = context_manager._candidate_messages(retained, "summary of the removed turns")

    breakdown = _state(compacted, [], plan=_plan(summary_checkpoint_id="ckpt-3"), scope="request").breakdown
    components = _by_id(breakdown)

    assert breakdown.scope == "request"
    assert components["memory"].count == 3
    assert components["agent"].items == ["Researcher"]
    assert components["summary"].items == ["ckpt-3"]
    assert "system_prompt" not in components


def test_preview_reports_undiscovered_mcp_as_unknown_rather_than_zero():
    plan = _plan(undiscovered_mcp=["mcp:GitHub", "mcp:Jira"])
    breakdown = _state(_messages(), [_tool("list_my_conversations")], plan=plan).breakdown
    components = _by_id(breakdown)

    assert components["mcp_tools"].included is False
    assert components["mcp_tools"].tokens is None
    # Remote schemas are never discovered in preview, so the tool count is unknown too.
    assert components["mcp_tools"].count is None
    assert components["mcp_tools"].items == ["mcp:GitHub", "mcp:Jira"]
    assert "mcp_tools" in breakdown.uncounted
    assert breakdown.complete is False
    assert sum(component.tokens for component in breakdown.components if component.included) == (
        _state(_messages(), [_tool("list_my_conversations")], plan=plan).input_tokens
    )


def test_deferred_tools_never_count_as_included_and_keep_the_breakdown_complete():
    plan = _plan(
        deferred_tools=[
            inspector.tool_plan_entry("legacy_http_tool", "custom__2__legacy_http_tool_abc123abc123"),
            inspector.tool_plan_entry("managed_web_search", "managed_web_search"),
        ]
    )
    breakdown = _state(_messages(), [_tool("list_my_conversations")], plan=plan).breakdown
    components = _by_id(breakdown)

    assert components["deferred_tools"].included is False
    assert components["deferred_tools"].tokens is None
    assert components["deferred_tools"].count == 2
    assert components["deferred_tools"].items == ["legacy_http_tool", "managed_web_search"]
    assert "deferred_tools" not in breakdown.uncounted
    assert breakdown.complete is True


def test_a_catalog_loaded_tool_moves_from_deferred_to_counted():
    """Once the model loads an on-demand tool, its schema is real and no longer deferred."""
    plan = _plan(
        deferred_tools=[
            inspector.tool_plan_entry("legacy_http_tool", "custom__2__legacy_http_tool_abc123abc123"),
            inspector.tool_plan_entry("other_tool", "custom__3__other_tool_def456def456"),
            inspector.tool_plan_entry("mcp:GitHub", "mcp__5__"),
        ]
    )
    loaded = [
        _tool("list_my_conversations"),
        _tool("custom__2__legacy_http_tool_abc123abc123"),
        _tool("mcp__5__search_issues"),
        _tool("mcp__5__create_issue"),
    ]

    breakdown = _state(_messages(), loaded, plan=plan, scope="request").breakdown
    components = _by_id(breakdown)

    assert components["deferred_tools"].items == ["other_tool"]
    assert components["deferred_tools"].count == 1
    assert components["tools"].count == 2
    assert components["mcp_tools"].count == 2
    assert sum(c.tokens for c in breakdown.components if c.included) == (
        _state(_messages(), loaded, plan=plan, scope="request").input_tokens
    )


def test_bounded_item_lists_still_report_the_true_totals():
    many = [inspector.tool_plan_entry(f"tool_{index}", f"custom__{index}__tool_{index}") for index in range(200)]
    plan = _plan(deferred_tools=many, attachments=[f"image:file-{index}.png" for index in range(200)])
    components = _by_id(_state(_messages(), [], plan=plan).breakdown)

    assert components["deferred_tools"].count == 200
    assert len(components["deferred_tools"].items) == 128
    assert components["attachments"].count == 200
    assert len(components["attachments"].items) == 128

    request = _by_id(
        _state(_messages(), [_tool(entry["match"]) for entry in many], plan=plan, scope="request").breakdown
    )
    assert "deferred_tools" not in request
    assert request["tools"].count == 200
    assert len(request["tools"].items) == 128


def test_attachments_that_the_text_projection_drops_block_exact_free_space():
    plan = _plan(attachments=["image:diagram.png"])
    breakdown = _state(_messages(), [], plan=plan).breakdown
    components = _by_id(breakdown)

    assert components["attachments"].included is False
    assert components["attachments"].count == 1
    assert breakdown.uncounted == ["attachments"]
    assert breakdown.complete is False


def test_request_scope_counts_discovered_mcp_schemas():
    tools = [_tool("list_my_conversations"), _tool("mcp__4__search_issues")]
    state = _state(_messages(), tools, plan=_plan(), scope="request")
    components = _by_id(state.breakdown)

    assert components["mcp_tools"].included is True
    assert components["mcp_tools"].count == 1
    assert components["mcp_tools"].items == ["mcp__4__search_issues"]
    assert components["tools"].items == ["list_my_conversations"]
    # A split marginal cost is attributed, never presented as an exact measurement.
    assert components["mcp_tools"].measurement == "estimated"
    assert sum(component.tokens for component in state.breakdown.components if component.included) == (
        state.input_tokens
    )


def test_breakdown_exposes_no_message_or_instruction_text():
    plan = _plan(summary_checkpoint_id="ckpt-9", attachments=["image:diagram.png"])
    breakdown = _state(_messages(summary="secret decisions"), [_tool("t")], plan=plan).breakdown

    emitted = " ".join(item for component in breakdown.components for item in component.items)
    for secret in ("remembered facts", "agent instructions", "secret decisions", "draft the model"):
        assert secret not in emitted


def test_unknown_window_still_exposes_the_full_measured_composition():
    """GLM-style routes have no catalog window but their sources are still countable."""
    unknown_window = context_manager.context_state(
        _messages(),
        [_tool("list_my_conversations")],
        model_name="perplexity/perplexity/glm-5.3",
        context_limit=None,
        output_reserve=4_096,
        revision="rev-1",
        plan=_plan(),
    )

    assert unknown_window.reason_code == "context_window_unknown"
    assert unknown_window.context_limit is None
    breakdown = unknown_window.breakdown
    components = _by_id(breakdown)
    assert components["skills"].items == ["Docs", "SQL"]
    assert components["tools"].count == 1
    assert breakdown.complete is True
    assert sum(c.tokens for c in breakdown.components if c.included) == unknown_window.input_tokens


def test_uncountable_input_keeps_names_and_counts_without_inventing_tokens(monkeypatch):
    from lumen.services.litellm_client import ContextTokenCount

    monkeypatch.setattr(
        context_manager,
        "count_context_tokens",
        lambda *_args: ContextTokenCount(tokens=None, measurement="unknown"),
    )
    state = _state(_messages(), [_tool("list_my_conversations")], plan=_plan(deferred_tools=["legacy_http_tool"]))

    assert state.reason_code == "token_count_unavailable"
    breakdown = state.breakdown
    components = _by_id(breakdown)
    assert breakdown.complete is False
    assert components["skills"].items == ["Docs", "SQL"]
    assert components["skills"].count == 2
    assert components["messages"].count == 3
    assert components["tools"].items == ["list_my_conversations"]
    assert all(component.tokens is None for component in breakdown.components)
    assert {"messages", "skills", "tools", "overhead"} <= set(breakdown.uncounted)
    # Deferred material is still distinguished from unmeasured included material.
    assert components["deferred_tools"].included is False
    assert "deferred_tools" not in breakdown.uncounted


def test_missing_plan_yields_no_breakdown():
    assert _state(_messages(), [], plan=None).breakdown is None


def test_future_plan_versions_are_ignored_instead_of_guessed():
    plan = {**_plan(), "version": inspector.PLAN_VERSION + 1}
    assert _state(_messages(), [], plan=plan).breakdown is None


def test_persisted_context_state_without_breakdown_still_deserializes():
    legacy = {
        "model_name": "gpt-4o-mini",
        "context_limit": 16_000,
        "output_reserve": 4_096,
        "safety_reserve": 2_048,
        "input_budget": 9_856,
        "input_tokens": 100,
        "utilization": 0.01,
        "measurement": "estimated",
        "recommendation": "none",
        "can_compact": False,
        "reason_code": None,
        "revision": "rev-1",
        "checkpoint_id": None,
        "active_compaction_run_id": None,
    }

    assert ContextState(**legacy).breakdown is None
    assert ContextState(**{**legacy, "breakdown": None}).breakdown is None

    restored = ContextState(**{**legacy, "breakdown": _state(_messages(), [], plan=_plan()).breakdown.model_dump()})
    assert restored.breakdown.scope == "preview"


def test_breakdown_rejects_duplicate_components_and_unsafe_uncounted_entries():
    from pydantic import ValidationError

    from lumen.models.chat_contracts import ContextBreakdown

    component = {"id": "messages", "tokens": 1, "measurement": "estimated", "count": 1, "included": True, "items": []}
    with pytest.raises(ValidationError):
        ContextBreakdown(scope="preview", complete=True, components=[component, component])
    with pytest.raises(ValidationError):
        ContextBreakdown(scope="preview", complete=True, components=[component], uncounted=["raw_prompt"])
    with pytest.raises(ValidationError):
        ContextBreakdown(scope="preview", complete=True, components=[{**component, "tokens": -1}])
