"""Pure version-2 durable-run transition contract.

The v1 worker does not import this module.  A v2 executor must validate every
transition through this table before it persists a status change.
"""

from __future__ import annotations

from typing import Literal

RunStatusV2 = Literal[
    "queued", "running", "awaiting_input", "waiting_children", "finalizing", "completed", "failed", "canceled"
]

_NONTERMINAL = frozenset({"queued", "running", "awaiting_input", "waiting_children", "finalizing"})
_TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset({"running", "finalizing"}),
    "running": frozenset({"awaiting_input", "waiting_children", "finalizing"}),
    "awaiting_input": frozenset({"queued", "finalizing"}),
    "waiting_children": frozenset({"queued", "finalizing"}),
    "finalizing": frozenset({"completed", "failed", "canceled"}),
    "completed": frozenset(),
    "failed": frozenset(),
    "canceled": frozenset(),
}


def transition_allowed(current: str, target: str, *, cancel_or_failure: bool = False) -> bool:
    """Allow only v2 lifecycle edges; cancellation/failure may enter finalization."""
    if cancel_or_failure and current in _NONTERMINAL:
        return target == "finalizing"
    return target in _TRANSITIONS.get(current, frozenset())
