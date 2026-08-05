import pytest

from lumen.services.agent_policy import resolve_direct_effects, resolve_execution_policy
from lumen.services.agent_protocol import AgentExecutionPolicy
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
