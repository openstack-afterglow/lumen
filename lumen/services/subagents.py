"""Pure bounded child-agent policy for the protocol-v2 executor."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from lumen.services.agent_protocol import AgentExecutionPolicy


class DelegationDenied(ValueError):
    pass


@dataclass(frozen=True)
class ChildRequest:
    agent_id: int
    task: str
    access: Literal["read", "write"]
    call_id: str


def merge_policy(*policies: AgentExecutionPolicy) -> AgentExecutionPolicy:
    if not policies:
        raise ValueError("at least one policy is required")
    return AgentExecutionPolicy(
        allowed_modes=set.intersection(*(set(policy.allowed_modes) for policy in policies)),
        can_delegate_read=all(policy.can_delegate_read for policy in policies),
        can_delegate_write=all(policy.can_delegate_write for policy in policies),
        max_model_turns=min(policy.max_model_turns for policy in policies),
        max_tool_calls=min(policy.max_tool_calls for policy in policies),
        max_children=min(policy.max_children for policy in policies),
        max_parallel_children=min(policy.max_parallel_children for policy in policies),
        max_child_depth=min(policy.max_child_depth for policy in policies),
    )


def validate_child_request(
    request: ChildRequest,
    *,
    policy: AgentExecutionPolicy,
    delegable_agent_ids: set[int],
    current_children: int,
    active_children: int,
    parent_depth: int,
) -> None:
    if request.agent_id not in delegable_agent_ids:
        raise DelegationDenied("delegate agent is not authorized")
    if not request.task.strip() or len(request.task) > 20_000 or not request.call_id.strip():
        raise DelegationDenied("delegate task is invalid")
    if parent_depth + 1 > policy.max_child_depth:
        raise DelegationDenied("child depth limit exceeded")
    if current_children >= policy.max_children:
        raise DelegationDenied("child count limit exceeded")
    if active_children >= policy.max_parallel_children:
        raise DelegationDenied("parallel child limit exceeded")
    if request.access == "read" and not policy.can_delegate_read:
        raise DelegationDenied("read delegation is not allowed")
    if request.access == "write" and not policy.can_delegate_write:
        raise DelegationDenied("write delegation is not allowed")
