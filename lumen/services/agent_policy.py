"""Immutable, narrowing-only execution policy resolution for v2 chat runs."""

from __future__ import annotations

from typing import Any

from lumen.services.agent_protocol import AgentExecutionPolicy
from lumen.services.subagents import merge_policy

_SERVER_POLICY = AgentExecutionPolicy(
    allowed_modes={"chat", "plan", "code"},
    can_delegate_read=True,
    can_delegate_write=True,
    max_model_turns=16,
    max_tool_calls=64,
    max_children=4,
    max_parallel_children=4,
    max_child_depth=1,
)
_DEFAULT_AGENT_POLICY = AgentExecutionPolicy(
    allowed_modes={"chat", "plan", "code"},
    can_delegate_read=False,
    can_delegate_write=False,
    max_model_turns=8,
    max_tool_calls=24,
    max_children=4,
    max_parallel_children=4,
    max_child_depth=1,
)
_ROLE_POLICIES = {
    "general": _SERVER_POLICY,
    "coordinator": _SERVER_POLICY,
    "researcher": AgentExecutionPolicy(
        allowed_modes={"chat", "plan"},
        can_delegate_read=False,
        can_delegate_write=False,
        max_model_turns=16,
        max_tool_calls=64,
        max_children=0,
        max_parallel_children=0,
        max_child_depth=0,
    ),
    "planner": AgentExecutionPolicy(
        allowed_modes={"chat", "plan"},
        can_delegate_read=False,
        can_delegate_write=False,
        max_model_turns=16,
        max_tool_calls=64,
        max_children=0,
        max_parallel_children=0,
        max_child_depth=0,
    ),
    "reviewer": AgentExecutionPolicy(
        allowed_modes={"chat", "plan"},
        can_delegate_read=False,
        can_delegate_write=False,
        max_model_turns=16,
        max_tool_calls=64,
        max_children=0,
        max_parallel_children=0,
        max_child_depth=0,
    ),
    "executor": AgentExecutionPolicy(
        allowed_modes={"plan", "code"},
        can_delegate_read=False,
        can_delegate_write=False,
        max_model_turns=16,
        max_tool_calls=64,
        max_children=0,
        max_parallel_children=0,
        max_child_depth=0,
    ),
}

_ALL_EFFECTS = frozenset({"read", "workspace_write", "process", "external_mutation"})
_ROLE_EFFECTS = {
    "general": _ALL_EFFECTS,
    "coordinator": frozenset({"read"}),
    "researcher": frozenset({"read"}),
    "planner": frozenset({"read"}),
    "reviewer": frozenset({"read"}),
    "executor": _ALL_EFFECTS,
}
_MODE_EFFECTS = {
    "chat": _ALL_EFFECTS,
    "plan": frozenset({"read"}),
    "code": _ALL_EFFECTS,
}


def resolve_direct_effects(agent: dict[str, Any] | None, *, execution_mode: str) -> frozenset[str]:
    """Freeze the role/mode ceiling used before preview, approval, or dispatch."""
    if execution_mode not in _MODE_EFFECTS:
        raise ValueError("execution mode is invalid")
    role = str((agent or {}).get("role") or "general")
    role_effects = _ROLE_EFFECTS.get(role)
    if role_effects is None:
        raise ValueError("agent role is invalid")
    return role_effects & _MODE_EFFECTS[execution_mode]


def default_execution_policy() -> AgentExecutionPolicy:
    """Return the default used when a v2 run has no agent policy."""
    return _DEFAULT_AGENT_POLICY


def resolve_execution_policy(agent: dict[str, Any] | None, *, execution_mode: str) -> AgentExecutionPolicy:
    """Compose server, role, agent, and selected-mode ceilings without widening."""
    if execution_mode not in {"chat", "plan", "code"}:
        raise ValueError("execution mode is invalid")

    role = str((agent or {}).get("role") or "general")
    role_policy = _ROLE_POLICIES.get(role)
    if role_policy is None:
        raise ValueError("agent role is invalid")

    raw_agent_policy = (agent or {}).get("execution_policy")
    try:
        agent_policy = (
            AgentExecutionPolicy.model_validate(raw_agent_policy) if raw_agent_policy else _DEFAULT_AGENT_POLICY
        )
    except Exception as exc:
        raise ValueError("agent execution policy is invalid") from exc

    effective = merge_policy(
        _SERVER_POLICY,
        role_policy,
        agent_policy,
        AgentExecutionPolicy(
            allowed_modes={execution_mode},
            can_delegate_read=True,
            can_delegate_write=True,
            max_model_turns=16,
            max_tool_calls=64,
            max_children=4,
            max_parallel_children=4,
            max_child_depth=1,
        ),
    )
    if execution_mode not in effective.allowed_modes:
        raise ValueError("agent policy does not allow this execution mode")
    return effective
