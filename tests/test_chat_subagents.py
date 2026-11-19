from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from lumen_plugin_api.tools import AgentExecutionPolicy

from lumen.services.agent_policy import resolve_direct_effects, resolve_execution_policy
from lumen.services.subagents import ChildRequest, DelegationDenied, merge_policy, validate_child_request


def _policy(**overrides):
    value = {
        "allowed_modes": {"chat", "plan", "code"},
        "can_delegate_read": True,
        "can_delegate_write": True,
        "max_model_turns": 8,
        "max_tool_calls": 24,
        "max_children": 4,
        "max_parallel_children": 4,
        "max_child_depth": 1,
    }
    value.update(overrides)
    return AgentExecutionPolicy(**value)


def test_child_policy_only_narrows_authority_and_limits():
    merged = merge_policy(_policy(), _policy(allowed_modes={"plan"}, can_delegate_write=False, max_children=2))

    assert merged.allowed_modes == {"plan"}
    assert merged.can_delegate_write is False
    assert merged.max_children == 2


def test_child_request_blocks_unauthorized_write_and_depth():
    request = ChildRequest(agent_id=2, task="Inspect code", access="write", call_id="call-1")
    with pytest.raises(DelegationDenied, match="write delegation"):
        validate_child_request(
            request,
            policy=_policy(can_delegate_write=False),
            delegable_agent_ids={2},
            current_children=0,
            active_children=0,
            parent_depth=0,
        )
    with pytest.raises(DelegationDenied, match="depth"):
        validate_child_request(
            request, policy=_policy(), delegable_agent_ids={2}, current_children=0, active_children=0, parent_depth=1
        )
    with pytest.raises(DelegationDenied, match="authorized"):
        validate_child_request(
            request, policy=_policy(), delegable_agent_ids=set(), current_children=0, active_children=0, parent_depth=0
        )


def test_effective_agent_policy_is_a_narrowing_v2_snapshot():
    policy = resolve_execution_policy(
        {
            "role": "general",
            "execution_policy": _policy(
                allowed_modes={"chat"},
                can_delegate_read=True,
                can_delegate_write=False,
                max_model_turns=3,
                max_tool_calls=7,
                max_children=2,
                max_parallel_children=1,
                max_child_depth=1,
            ).model_dump(mode="json"),
        },
        execution_mode="chat",
    )

    assert policy.allowed_modes == {"chat"}
    assert policy.can_delegate_read is True
    assert policy.can_delegate_write is False
    assert policy.max_model_turns == 3
    assert policy.max_tool_calls == 7
    assert policy.max_children == 2
    assert policy.max_parallel_children == 1


def test_effective_agent_policy_rejects_role_mode_widening():
    with pytest.raises(ValueError, match="does not allow"):
        resolve_execution_policy({"role": "executor"}, execution_mode="chat")


def test_role_direct_effect_ceiling_is_separate_from_delegation_policy():
    assert resolve_direct_effects({"role": "coordinator"}, execution_mode="chat") == {"read"}
    assert resolve_direct_effects({"role": "executor"}, execution_mode="code") == {
        "read",
        "workspace_write",
        "process",
        "external_mutation",
    }


async def test_child_uses_pinned_model_and_rejects_ambiguous_legacy_name(monkeypatch):
    from lumen.services import chat_admission
    from lumen.services.durable_runs import children
    from lumen.services.providers import routing

    parent = SimpleNamespace(user_id="u", project_id="p", payer_domain_id=None, capability_snapshot={
        "model_id": 11, "provider_id": 1, "execution_policy": _policy().model_dump(mode="json"),
    })
    child = children.DelegationCall("call", 4, "Inspect", "read", Decimal("1"), 1)
    selected = {
        "model_name": "same", "provider_name": "second", "model_id": 12, "provider_id": 2,
        "config_version_hash": "a" * 64, "input_price_per_token": "0.1", "output_price_per_token": "0.2",
    }
    exact = AsyncMock(return_value=selected)
    by_name = AsyncMock(side_effect=routing.AmbiguousModelRouteError("duplicate"))
    inherited = AsyncMock(return_value={**selected, "model_id": 11, "provider_id": 1})
    monkeypatch.setattr(routing, "resolve_model_by_id", exact)
    monkeypatch.setattr(routing, "resolve_model", by_name)
    monkeypatch.setattr(routing, "resolve_model_snapshot", inherited)
    monkeypatch.setattr(chat_admission, "_resolve_extension_selection", AsyncMock(return_value={"tools": [], "mcp": []}))
    monkeypatch.setattr(chat_admission, "_load_skill_snapshot", AsyncMock(return_value=([], [])))
    pinned = await children._prepare_child_inputs(parent, {}, child, {"model_name": "same", "model_row_id": 12})
    exact.assert_awaited_once_with(12, model_kind="text")
    by_name.assert_not_called()
    assert (pinned["capability_snapshot"]["model_id"], pinned["capability_snapshot"]["provider_id"]) == (12, 2)
    with pytest.raises(children.ChildFailure) as ambiguous:
        await children._prepare_child_inputs(parent, {}, child, {"model_name": "same"})
    assert ambiguous.value.code == "child_route_ambiguous"
    inherited_route = await children._prepare_child_inputs(parent, {}, child, {})
    assert (inherited_route["capability_snapshot"]["model_id"], inherited_route["capability_snapshot"]["provider_id"]) == (11, 1)


@pytest.mark.parametrize("context_limit", [None, 8000])
async def test_domain_paid_child_rejects_unbounded_agent_model_before_spawn(monkeypatch, context_limit):
    from lumen.services import chat_admission
    from lumen.services.durable_runs import children
    from lumen.services.providers import routing

    parent = SimpleNamespace(user_id="u", project_id="p", payer_domain_id="domain-a", capability_snapshot={
        "model_id": 11, "provider_id": 1, "execution_policy": _policy().model_dump(mode="json"),
    })
    child = children.DelegationCall("call", 4, "Inspect", "read", Decimal("1"), 1)
    selected = {
        "model_name": "agent-model", "provider_name": "second", "model_id": 12, "provider_id": 2,
        "config_version_hash": "a" * 64, "input_price_per_token": "0.1", "output_price_per_token": "0.2",
        "capabilities": {"context_limit": context_limit},
    }
    monkeypatch.setattr(routing, "resolve_model_by_id", AsyncMock(return_value=selected))
    monkeypatch.setattr(chat_admission, "_resolve_extension_selection", AsyncMock(return_value={"tools": [], "mcp": []}))
    monkeypatch.setattr(chat_admission, "_load_skill_snapshot", AsyncMock(return_value=([], [])))
    agent = {"model_name": "agent-model", "model_row_id": 12}
    if context_limit is None:
        with pytest.raises(children.ChildFailure) as failure:
            await children._prepare_child_inputs(parent, {}, child, agent)
        assert failure.value.code == "child_route_unavailable"
        assert "context_limit_unknown" in str(failure.value)
    else:
        prepared = await children._prepare_child_inputs(parent, {}, child, agent)
        assert prepared["capability_snapshot"]["model_id"] == 12
