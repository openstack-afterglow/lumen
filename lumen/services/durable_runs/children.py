"""Durable child-run creation, wait groups, joins, settlement and cascading cancellation.

Every state change here is a MariaDB transaction under the global lock order
(project quota -> conversation -> root -> ancestors -> children -> ledger -> resources).
LangGraph checkpoints are committed separately; the durable group/call rows decide
whether a resumed parent replays a prepared delegation or joins recorded results.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from lumen_plugin_api.tools import AgentExecutionPolicy
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen.config import get_settings
from lumen.crypto import decrypt_chat_content, encrypt_chat_content
from lumen.models.chat_db import ChatAgent
from lumen.models.chat_infrastructure import ChatDelegationCall, ChatDelegationGroup, ChatRuntimeResource
from lumen.models.chat_runs import ChatRun
from lumen.plugins.registry import get_registry
from lumen.services.run_protocol_v2 import transition_allowed
from lumen.services.run_store import NONTERMINAL, TERMINAL, append_event
from lumen.services.subagents import ChildRequest, DelegationDenied, validate_child_request

from . import budgets
from .common import _event, _factory, _now, _payload, wake_run
from .errors import DurableRunConflict, DurableRunError, DurableRunInputError, DurableRunLeaseLost

logger = logging.getLogger(__name__)

DELEGATE_TOOL_NAME = "delegate_agent"
MAX_TASK_CHARS = 20_000
MAX_SUMMARY_CHARS = 8_192
_CHILD_INSTRUCTION = (
    "You are a delegated child agent. Complete exactly the task below using only the tools you were "
    "given, then answer with a concise result the parent agent can use."
)


class ChildFailure(DurableRunInputError):
    """Typed delegation failure exposed to the parent model as a tool result."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class DelegationCall:
    call_id: str
    agent_id: int
    task: str
    access: Literal["read", "write"]
    credit_budget: Decimal
    sandbox_seconds: int

    def fingerprint(self) -> str:
        material = {
            "agent_id": self.agent_id,
            "task": self.task,
            "access": self.access,
            "credit_budget": format(self.credit_budget, "f"),
            "sandbox_seconds": self.sandbox_seconds,
        }
        return hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def parse_delegation_call(call_id: str, arguments: dict[str, Any]) -> DelegationCall:
    """Validate model-supplied delegation arguments; identity/budget authority stays server-side."""
    try:
        agent_id = arguments["agent_id"]
        task = arguments["task"]
        access = arguments.get("access", "read")
        credit_budget = Decimal(str(arguments["credit_budget"]))
        sandbox_seconds = arguments["sandbox_seconds"]
    except (KeyError, TypeError, InvalidOperation) as exc:
        raise ChildFailure("invalid_tool_arguments", "delegation arguments are invalid") from exc
    if (
        type(agent_id) is not int
        or agent_id < 1
        or not isinstance(task, str)
        or not task.strip()
        or len(task) > MAX_TASK_CHARS
        or access not in {"read", "write"}
        or not credit_budget.is_finite()
        or credit_budget <= 0
        or type(sandbox_seconds) is not int
        or sandbox_seconds < 1
    ):
        raise ChildFailure("invalid_tool_arguments", "delegation arguments are invalid")
    return DelegationCall(
        call_id=call_id,
        agent_id=agent_id,
        task=task,
        access=access,
        credit_budget=credit_budget,
        sandbox_seconds=sandbox_seconds,
    )


def delegate_tool_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "agent_id": {"type": "integer", "minimum": 1, "description": "An agent the parent is allowed to delegate to."},
            "task": {"type": "string", "minLength": 1, "maxLength": MAX_TASK_CHARS},
            "access": {"type": "string", "enum": ["read", "write"]},
            "credit_budget": {"type": "string", "pattern": "^\\d+(\\.\\d{1,8})?$", "description": "Credits reserved for the child."},
            "sandbox_seconds": {"type": "integer", "minimum": 1, "maximum": 86400},
        },
        "required": ["agent_id", "task", "credit_budget", "sandbox_seconds"],
        "additionalProperties": False,
    }


def _policy_from_snapshot(snapshot: dict[str, Any]) -> AgentExecutionPolicy:
    try:
        return AgentExecutionPolicy.model_validate(snapshot.get("execution_policy") or {})
    except Exception as exc:
        raise DurableRunError("parent execution policy is unavailable") from exc


def _aware(value: datetime | None) -> datetime | None:
    return value.replace(tzinfo=UTC) if value is not None and value.tzinfo is None else value


async def _lock_lineage(
    session: AsyncSession, run: ChatRun, order: budgets.LockOrder, *, quota=None
) -> tuple[Any, ChatRun, list[ChatRun]]:
    """Lock quota, root and ancestors (by depth) for ``run``; return (quota, root, ancestors)."""
    if quota is None:
        quota = await budgets.lock_project_quota(session, run.project_id, order=order)
    root_id = run.root_run_id or run.id
    root = await budgets.lock_run(session, root_id, order=order, lock_class="root")
    ancestors: list[ChatRun] = []
    chain: list[str] = []
    cursor = run.parent_run_id
    while cursor is not None and cursor != root_id:
        chain.append(cursor)
        parent = (await session.execute(select(ChatRun.parent_run_id).where(ChatRun.id == cursor))).scalar_one_or_none()
        cursor = parent
    for ancestor_id in reversed(chain):
        ancestors.append(await budgets.lock_run(session, ancestor_id, order=order, lock_class="ancestor"))
    return quota, root, ancestors


async def _locked_children(session: AsyncSession, parent_run_id: str, order: budgets.LockOrder) -> list[ChatRun]:
    order.take("child")
    return list(
        (
            await session.execute(
                select(ChatRun)
                .where(ChatRun.parent_run_id == parent_run_id)
                .order_by(ChatRun.created_at, ChatRun.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalars()
    )


def _child_payload(parent_payload: dict[str, Any], agent: dict[str, Any], call: DelegationCall, *, prepared: dict[str, Any]) -> dict[str, Any]:
    features = dict(parent_payload.get("features") or {})
    # Children never inherit the caller's account memory or the parent prompt wholesale.
    features["memory"] = False
    instructions = [_CHILD_INSTRUCTION]
    if agent.get("instructions"):
        instructions.append(str(agent["instructions"]))
    instructions.extend(prepared["skill_instructions"])
    return {
        "input_messages": [
            *({"role": "system", "content": text} for text in instructions),
            {"role": "user", "content": call.task},
        ],
        "input_parts": [{"type": "text", "text": call.task}],
        "features": features,
        "skill_snapshot": prepared["skill_snapshot"],
        "plugin_tool_snapshots": prepared["plugin_tool_snapshots"],
        "extension_snapshot": prepared["extension_selection"],
        "max_tokens": prepared["max_tokens"],
        "temperature": prepared["temperature"],
        "reasoning_effort": "auto",
        "client_timezone": parent_payload.get("client_timezone"),
        "execution_protocol_version": 2,
        "execution_mode": "code" if call.access == "write" else "plan",
        "execution_policy": prepared["policy"].model_dump(mode="json"),
        "allowed_direct_effects": sorted(prepared["direct_effects"]),
        "v2_max_model_turns": prepared["policy"].max_model_turns,
        "v2_max_tool_calls": prepared["policy"].max_tool_calls,
        "lumen_snapshot": None,
        "delegation": {"call_id": call.call_id, "access": call.access, "fingerprint": call.fingerprint()},
    }


def _require_child_authority(
    parent_payload: dict[str, Any], extensions: dict[str, list[dict[str, Any]]],
    plugin_tools: list[dict[str, Any]], skills: list[dict[str, Any]],
) -> None:
    """A child may narrow the parent's selected, immutable authority, never add it."""
    parent_extensions = parent_payload.get("extension_snapshot") or {}
    for kind in ("tools", "mcp"):
        allowed = {
            (item.get("id"), item.get("config_fingerprint"), item.get("credential_version"))
            for item in parent_extensions.get(kind, [])
        }
        if any(
            (item.get("id"), item.get("config_fingerprint"), item.get("credential_version")) not in allowed
            for item in extensions.get(kind, [])
        ):
            raise ChildFailure("child_policy_denied", "child extension exceeds frozen parent authority")
    allowed_tools = {
        (item["binding"]["id"], item["identity"]["config_fingerprint"])
        for item in parent_payload.get("plugin_tool_snapshots", [])
    }
    if any(
        (item["binding"]["id"], item["identity"]["config_fingerprint"]) not in allowed_tools
        for item in plugin_tools
    ):
        raise ChildFailure("child_policy_denied", "child plugin tool exceeds frozen parent authority")
    allowed_skills = {
        json.dumps(item["snapshot"], sort_keys=True, separators=(",", ":"))
        for item in parent_payload.get("skill_snapshot", [])
    }
    if any(
        json.dumps(item["snapshot"], sort_keys=True, separators=(",", ":")) not in allowed_skills
        for item in skills
    ):
        raise ChildFailure("child_policy_denied", "child skill exceeds frozen parent authority")

async def _prepare_child_inputs(
    parent: ChatRun, parent_payload: dict[str, Any], call: DelegationCall, agent: dict[str, Any]
) -> dict[str, Any]:
    """Resolve the child's frozen route, policy, skills and tool bindings before any lock is held."""
    from lumen.models.chat_contracts import ChatFeatureOptions
    from lumen.services import agent_policy, chat_admission
    from lumen.services.providers import routing as ps

    parent_policy = _policy_from_snapshot(parent.capability_snapshot or {})
    mode = "code" if call.access == "write" else "plan"
    try:
        child_policy = agent_policy.resolve_execution_policy(agent, execution_mode=mode)
        direct_effects = agent_policy.resolve_direct_effects(agent, execution_mode=mode)
    except ValueError as exc:
        raise ChildFailure("child_policy_denied", str(exc)) from exc
    if call.access == "read":
        direct_effects = direct_effects & {"read"}
    # A child never widens the frozen parent authority: depth and delegation shrink by one level.
    from lumen.services.subagents import merge_policy

    child_policy = merge_policy(parent_policy, child_policy)
    child_policy = child_policy.model_copy(
        update={
            "max_child_depth": max(0, parent_policy.max_child_depth - 1),
            "can_delegate_read": False,
            "can_delegate_write": False,
        }
    )
    features = ChatFeatureOptions.model_validate({**(parent_payload.get("features") or {}), "memory": False})
    route = None
    if agent.get("model_name"):
        route = await ps.resolve_model(str(agent["model_name"]))
        if route is None:
            raise ChildFailure("child_route_unavailable", "child agent model is not available")
    else:
        route = await ps.resolve_model_snapshot(parent.capability_snapshot or {})
        if route is None:
            raise ChildFailure("child_route_unavailable", "parent route is not available for the child")
    try:
        extension_selection = await chat_admission._resolve_extension_selection(
            agent, features, user_id=parent.user_id, project_id=parent.project_id
        )
        plugin_tool_snapshots = await chat_admission._freeze_plugin_bindings(
            [item for item in agent.get("plugin_tool_ids", []) if isinstance(item, str)],
            kind="tool",
            user_id=parent.user_id,
            project_id=parent.project_id,
        )
        plugin_skill_snapshots = await chat_admission._freeze_plugin_bindings(
            [item for item in agent.get("plugin_skill_ids", []) if isinstance(item, str)],
            kind="skill",
            user_id=parent.user_id,
            project_id=parent.project_id,
        )
        skill_instructions, skill_snapshot = await chat_admission._load_skill_snapshot(
            agent, [], plugin_skill_snapshots, parent.user_id, parent.project_id
        )
    except Exception as exc:  # HTTP-shaped admission failures become typed child failures.
        code = getattr(exc, "detail", None)
        raise ChildFailure("child_admission_denied", str(code or exc)) from exc
    _require_child_authority(parent_payload, extension_selection, plugin_tool_snapshots, skill_snapshot)
    capability_snapshot, pricing_snapshot = chat_admission._run_snapshots(
        route, features.model_dump(mode="json", by_alias=True), feature_routes={}, summary_route=None
    )
    capability_snapshot["execution_policy"] = child_policy.model_dump(mode="json")
    capability_snapshot["direct_effects"] = sorted(direct_effects)
    capability_snapshot["execution_protocol_version"] = 2
    capability_snapshot["extensions"] = chat_admission._capability_extension_snapshot(extension_selection)
    if plugin_tool_snapshots or plugin_skill_snapshots:
        capability_snapshot["required_plugin_digest"] = get_registry().digest
    params = agent.get("params") or {}
    return {
        "policy": child_policy,
        "direct_effects": direct_effects,
        "route": route,
        "capability_snapshot": capability_snapshot,
        "pricing_snapshot": pricing_snapshot,
        "extension_selection": extension_selection,
        "plugin_tool_snapshots": plugin_tool_snapshots,
        "skill_instructions": skill_instructions,
        "skill_snapshot": skill_snapshot,
        "max_tokens": min(int(params.get("max_tokens") or 4096), 4096),
        "temperature": params.get("temperature"),
    }


async def prepare_delegations(
    *,
    parent_run_id: str,
    owner: str,
    model_segment_id: str,
    calls: list[DelegationCall],
) -> dict[str, Any]:
    """Create (or recover) one wait group with an isolated child run per delegation call.

    Route/policy/skill/tool resolution runs before the lock; the transaction then re-checks
    cancellation, deadline, lease fence, fingerprints, counts, policy and quotas atomically.
    """
    from lumen.services import agent_store as ags
    from lumen.services.infrastructure import store as infra_store

    if not calls:
        raise DurableRunInputError("delegation group needs at least one call")
    if len({call.call_id for call in calls}) != len(calls):
        raise DurableRunInputError("delegation call IDs must be unique")
    factory = _factory()
    async with factory() as session:
        parent_identity = (await session.execute(select(ChatRun).where(ChatRun.id == parent_run_id))).scalar_one()
    parent_payload = _payload(parent_identity)
    agent_rows: dict[int, dict[str, Any]] = {}
    prepared_inputs: dict[str, dict[str, Any]] = {}
    for call in calls:
        agent = await ags.get_agent_for_run(call.agent_id, user_id=parent_identity.user_id, project_id=parent_identity.project_id)
        if agent is None:
            raise ChildFailure("child_agent_unavailable", "delegate agent is not available")
        agent_rows[call.agent_id] = agent
        prepared_inputs[call.call_id] = await _prepare_child_inputs(parent_identity, parent_payload, call, agent)
    settings = get_settings()

    async def transaction() -> dict[str, Any]:
        order = budgets.LockOrder()
        async with factory() as session, session.begin():
            quota, root, ancestors = await _lock_lineage(session, parent_identity, order)
            parent = root if parent_identity.id == root.id else ancestors[-1] if ancestors and ancestors[-1].id == parent_identity.id else None
            if parent is None:
                parent = await budgets.lock_run(session, parent_identity.id, order=order, lock_class="ancestor")
            if parent.lease_owner != owner or parent.status != "running":
                raise DurableRunLeaseLost(f"lease lost for {parent.id}")
            if parent.cancel_requested_at is not None or root.cancel_requested_at is not None:
                raise ChildFailure("child_canceled", "root run cancellation is in progress")
            deadline = _aware(root.deadline_at)
            if deadline is not None and deadline <= _now():
                raise ChildFailure("resource_deadline_exceeded", "root run deadline has passed")
            policy = _policy_from_snapshot(parent.capability_snapshot or {})
            existing_children = await _locked_children(session, parent.id, order)
            order.take("ledger")
            group = (
                await session.execute(
                    select(ChatDelegationGroup)
                    .where(
                        ChatDelegationGroup.parent_run_id == parent.id,
                        ChatDelegationGroup.model_segment_id == model_segment_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if group is not None:
                rows = list(
                    (
                        await session.execute(
                            select(ChatDelegationCall)
                            .where(ChatDelegationCall.group_id == group.id)
                            .order_by(ChatDelegationCall.ordinal)
                            .with_for_update()
                        )
                    ).scalars()
                )
                by_call = {row.call_id: row for row in rows}
                if {call.call_id for call in calls} != set(by_call):
                    raise DurableRunConflict("replayed delegation group has different calls")
                for call in calls:
                    if by_call[call.call_id].fingerprint != call.fingerprint():
                        raise DurableRunConflict("replayed delegation call has a different fingerprint")
                return _group_view(group, rows)

            parent_agent = await session.get(ChatAgent, parent.agent_id) if parent.agent_id is not None else None
            delegable = {
                int(item) for item in (getattr(parent_agent, "delegable_agent_ids", None) or []) if isinstance(item, int)
            }
            current_children = len(existing_children)
            active_children = sum(child.status in NONTERMINAL for child in existing_children)
            group = ChatDelegationGroup(
                id=str(uuid.uuid4()), parent_run_id=parent.id, model_segment_id=model_segment_id, state="prepared"
            )
            session.add(group)
            rows: list[ChatDelegationCall] = []
            pending_resources: list[tuple[ChatRun, int]] = []
            for ordinal, call in enumerate(calls):
                agent_row = await session.get(ChatAgent, call.agent_id)
                if (
                    agent_row is None
                    or not agent_row.is_active
                    or agent_row.owner_user_id != parent.user_id
                    or agent_row.project_id != parent.project_id
                ):
                    raise ChildFailure("child_agent_unavailable", "delegate agent is not available")
                try:
                    validate_child_request(
                        ChildRequest(agent_id=call.agent_id, task=call.task, access=call.access, call_id=call.call_id),
                        policy=policy,
                        delegable_agent_ids=delegable,
                        current_children=current_children,
                        active_children=active_children,
                        parent_depth=int(parent.depth or 0),
                    )
                except DelegationDenied as exc:
                    raise ChildFailure("child_policy_denied", str(exc)) from exc
                prepared = prepared_inputs[call.call_id]
                child_id = str(uuid.uuid4())
                child = ChatRun(
                    id=child_id,
                    run_scope="child",
                    run_kind="completion",
                    project_id=parent.project_id,
                    user_id=parent.user_id,
                    model_name=prepared["route"]["model_name"],
                    source=parent.source,
                    api_key_id=parent.api_key_id,
                    agent_id=call.agent_id,
                    execution_protocol_version=2,
                    execution_mode="code" if call.access == "write" else "plan",
                    parent_run_id=parent.id,
                    root_run_id=root.id,
                    delegation_call_id=call.call_id,
                    depth=int(parent.depth or 0) + 1,
                    capability_snapshot=prepared["capability_snapshot"],
                    required_plugin_digest=prepared["capability_snapshot"].get("required_plugin_digest"),
                    pricing_snapshot=prepared["pricing_snapshot"],
                    policy_snapshot={"policy": prepared["policy"].model_dump(mode="json"), "access": call.access},
                    request_payload=encrypt_chat_content(
                        json.dumps(
                            _child_payload(parent_payload, agent_rows[call.agent_id], call, prepared=prepared),
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                    ),
                    client_request_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"lumen:delegation:{parent.id}:{call.call_id}")),
                    request_fingerprint=call.fingerprint(),
                    fingerprint_version=1,
                    credit_ceiling=call.credit_budget,
                    sandbox_seconds_ceiling=call.sandbox_seconds,
                    wall_time_seconds=root.wall_time_seconds,
                    deadline_at=root.deadline_at,
                    status="waiting_resource",
                )
                session.add(child)
                await session.flush()
                # Reserve before the child becomes runnable; failures roll back the whole group.
                try:
                    await budgets.reserve(session, quota=quota, root=root, run_id=child_id, kind="child_slot", amount=1, order=order)
                    await budgets.reserve(session, quota=quota, root=root, run_id=child_id, kind="sandbox_slot", amount=1, order=order)
                    await budgets.reserve(
                        session, quota=quota, root=root, run_id=child_id, kind="credit", amount=call.credit_budget, order=order
                    )
                    await budgets.reserve(
                        session, quota=quota, root=root, run_id=child_id, kind="sandbox_seconds", amount=call.sandbox_seconds, order=order
                    )
                except budgets.BudgetExceeded as exc:
                    raise ChildFailure(exc.code, str(exc)) from exc
                row = ChatDelegationCall(
                    id=str(uuid.uuid4()),
                    group_id=group.id,
                    parent_run_id=parent.id,
                    call_id=call.call_id,
                    fingerprint=call.fingerprint(),
                    ordinal=ordinal,
                    child_run_id=child_id,
                    state="prepared",
                )
                session.add(row)
                rows.append(row)
                await append_event(
                    session,
                    child,
                    _event(
                        child,
                        "run.started",
                        {
                            "conversation_id": None,
                            "temp_thread_id": None,
                            "model_name": child.model_name,
                            "effective_features": (prepared["capability_snapshot"].get("effective_features") or {}),
                            "run_kind": "completion",
                        },
                    ),
                )
                await append_event(session, child, _event(child, "run.stage.changed", {"stage": "waiting_resource"}))
                await append_event(
                    session,
                    parent,
                    _event(
                        parent,
                        "child.created",
                        {
                            "child_run_id": child_id,
                            "call_id": call.call_id,
                            "wait_group_id": group.id,
                            "agent_id": call.agent_id,
                            "access": call.access,
                            "ordinal": ordinal,
                            "status": "waiting_resource",
                        },
                    ),
                )
                current_children += 1
                active_children += 1
                pending_resources.append((child, call.sandbox_seconds))
            for child, seconds in pending_resources:
                allocation_deadline = _now() + timedelta(seconds=seconds)
                if deadline is not None:
                    allocation_deadline = min(allocation_deadline, deadline)
                try:
                    await infra_store.assign_sandbox(
                        session,
                        run=child,
                        config=settings.runtime_config,
                        deadline_at=allocation_deadline,
                        order=order,
                    )
                except infra_store.ResourceUnavailable as exc:
                    raise ChildFailure(exc.code, str(exc)) from exc
            return _group_view(group, rows)

    return await budgets.retry_deadlocks(transaction)


def _group_view(group: ChatDelegationGroup, rows: list[ChatDelegationCall]) -> dict[str, Any]:
    return {
        "wait_group_id": group.id,
        "state": group.state,
        "children": [
            {"call_id": row.call_id, "child_run_id": row.child_run_id, "ordinal": row.ordinal, "state": row.state}
            for row in sorted(rows, key=lambda item: item.ordinal)
        ],
    }


async def mark_waiting_children(*, parent_run_id: str, owner: str, wait_group_id: str, checkpoint_id: str | None) -> str:
    """After the interrupt checkpoint is durable, park the parent or requeue it if children already finished."""

    async def transaction() -> str:
        order = budgets.LockOrder()
        async with _factory()() as session, session.begin():
            identity = (await session.execute(select(ChatRun).where(ChatRun.id == parent_run_id))).scalar_one()
            _quota, root, ancestors = await _lock_lineage(session, identity, order)
            parent = root if identity.id == root.id else (ancestors[-1] if ancestors and ancestors[-1].id == identity.id else None)
            if parent is None:
                parent = await budgets.lock_run(session, identity.id, order=order, lock_class="ancestor")
            if parent.lease_owner != owner or parent.status != "running":
                raise DurableRunLeaseLost(f"lease lost for {parent.id}")
            order.take("ledger")
            group = (
                await session.execute(
                    select(ChatDelegationGroup).where(ChatDelegationGroup.id == wait_group_id).with_for_update()
                )
            ).scalar_one_or_none()
            if group is None or group.parent_run_id != parent.id:
                raise DurableRunError("delegation group is unavailable")
            rows = list(
                (
                    await session.execute(
                        select(ChatDelegationCall).where(ChatDelegationCall.group_id == group.id).with_for_update()
                    )
                ).scalars()
            )
            if checkpoint_id is None and group.checkpoint_id is None:
                raise DurableRunError("waiting state requires a resumable checkpoint")
            group.checkpoint_id = checkpoint_id or group.checkpoint_id
            all_terminal = all(row.state in {"join_ready", "joined", "canceled"} for row in rows)
            if all_terminal:
                group.state = "join_ready"
                parent.status = "queued"
                parent.lease_owner = None
                parent.lease_expires_at = None
                await append_event(session, parent, _event(parent, "run.stage.changed", {"stage": "queued"}))
                return "queued"
            if not transition_allowed(parent.status, "waiting_children"):
                raise DurableRunConflict("parent cannot wait for children from its current state")
            group.state = "waiting"
            parent.status = "waiting_children"
            parent.lease_owner = None
            parent.lease_expires_at = None
            await append_event(session, parent, _event(parent, "run.stage.changed", {"stage": "waiting_children"}))
            return "waiting_children"

    outcome = await budgets.retry_deadlocks(transaction)
    if outcome == "queued":
        await wake_run(parent_run_id)
    return outcome


def child_result_payload(
    *, status: str, error_code: str | None, summary: str, artifacts: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "status": status,
        "error_code": error_code,
        "summary": summary[:MAX_SUMMARY_CHARS],
        "artifacts": [
            {
                "type": "file",
                "asset_id": item["asset_id"],
                "mime_type": item.get("mime_type") or item.get("media_type"),
                "name": item["name"],
                "size_bytes": item["size_bytes"],
            }
            for item in artifacts
            if isinstance(item, dict) and item.get("asset_id")
        ][:20],
    }


async def record_child_terminal(
    session: AsyncSession,
    child: ChatRun,
    *,
    quota,
    root: ChatRun,
    order: budgets.LockOrder,
    status: str,
    error_code: str | None,
    result: dict[str, Any],
    release_resource: bool = True,
) -> str | None:
    """Settle the child's reservations once, record its typed result and return a parent to wake."""
    from lumen.services.infrastructure import store as infra_store

    if status not in TERMINAL:
        raise DurableRunError("child terminal status is invalid")
    order.take("ledger")
    call_row = (
        await session.execute(
            select(ChatDelegationCall).where(ChatDelegationCall.child_run_id == child.id).with_for_update()
        )
    ).scalar_one_or_none()
    if call_row is None:
        raise DurableRunError("child run has no delegation call")
    if call_row.state in {"join_ready", "joined", "canceled"}:
        return None
    settled_credits = Decimal(str(child.reserved_credits or 0))
    await budgets.settle(session, quota=quota, root=root, run_id=child.id, kind="credit", actual=settled_credits, order=order)
    await budgets.release(session, quota=quota, root=root, run_id=child.id, kind="child_slot", order=order)
    await budgets.release(session, quota=quota, root=root, run_id=child.id, kind="sandbox_slot", order=order)
    ready_at = None
    if child.assigned_resource_id is not None:
        ready_at = (await session.execute(select(ChatRuntimeResource.ready_at).where(
            ChatRuntimeResource.id == child.assigned_resource_id))).scalar_one_or_none()
    sandbox_used = max(0, int((_now() - _aware(ready_at)).total_seconds())) if ready_at else 0
    await budgets.settle(
        session, quota=quota, root=root, run_id=child.id, kind="sandbox_seconds", actual=sandbox_used, order=order
    )
    call_row.state = "canceled" if status == "canceled" else "join_ready"
    call_row.result_payload = encrypt_chat_content(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    group = (
        await session.execute(select(ChatDelegationGroup).where(ChatDelegationGroup.id == call_row.group_id).with_for_update())
    ).scalar_one()
    siblings = list(
        (await session.execute(select(ChatDelegationCall.state).where(ChatDelegationCall.group_id == group.id))).scalars()
    )
    parent = (await session.execute(select(ChatRun).where(ChatRun.id == group.parent_run_id))).scalar_one()
    await append_event(
        session,
        parent,
        _event(
            parent,
            "child.completed",
            {
                "child_run_id": child.id,
                "call_id": call_row.call_id,
                "wait_group_id": group.id,
                "status": status,
                "error_code": error_code,
                "summary": result.get("summary", ""),
                "artifacts": result.get("artifacts", []),
            },
        ),
    )
    wake: str | None = None
    if all(state in {"join_ready", "joined", "canceled"} for state in siblings):
        if group.state in {"prepared", "waiting"}:
            group.state = "join_ready"
        if parent.status == "waiting_children" and parent.cancel_requested_at is None:
            parent.status = "queued"
            parent.lease_owner = None
            parent.lease_expires_at = None
            await append_event(session, parent, _event(parent, "run.stage.changed", {"stage": "queued"}))
            wake = parent.id
    if release_resource:
        # Resource rows are the last lock class; callers finalizing several children pass
        # ``release_resource=False`` and release every sandbox intent after their ledger writes.
        await infra_store.release_sandbox_intent(session, run_id=child.id, order=order)
    return wake


async def delegation_results(*, parent_run_id: str, owner: str, wait_group_id: str) -> dict[str, Any]:
    """Join exactly once: record the unique join segment, then return ordered typed results."""

    async def transaction() -> dict[str, Any]:
        order = budgets.LockOrder()
        async with _factory()() as session, session.begin():
            identity = (await session.execute(select(ChatRun).where(ChatRun.id == parent_run_id))).scalar_one()
            _quota, root, ancestors = await _lock_lineage(session, identity, order)
            parent = root if identity.id == root.id else (ancestors[-1] if ancestors and ancestors[-1].id == identity.id else None)
            if parent is None:
                parent = await budgets.lock_run(session, identity.id, order=order, lock_class="ancestor")
            if parent.lease_owner != owner or parent.status != "running":
                raise DurableRunLeaseLost(f"lease lost for {parent.id}")
            order.take("ledger")
            group = (
                await session.execute(
                    select(ChatDelegationGroup).where(ChatDelegationGroup.id == wait_group_id).with_for_update()
                )
            ).scalar_one_or_none()
            if group is None or group.parent_run_id != parent.id:
                raise DurableRunError("delegation group is unavailable")
            rows = list(
                (
                    await session.execute(
                        select(ChatDelegationCall)
                        .where(ChatDelegationCall.group_id == group.id)
                        .order_by(ChatDelegationCall.ordinal)
                        .with_for_update()
                    )
                ).scalars()
            )
            if any(row.state not in {"join_ready", "joined", "canceled"} for row in rows):
                raise DurableRunConflict("delegation group is not ready to join")
            results = []
            for row in rows:
                payload = json.loads(decrypt_chat_content(row.result_payload)) if row.result_payload else {
                    "status": "canceled",
                    "error_code": "child_canceled",
                    "summary": "",
                    "artifacts": [],
                }
                results.append({"call_id": row.call_id, "child_run_id": row.child_run_id, **payload})
                if row.state == "join_ready":
                    row.state = "joined"
            if group.join_segment_id is None:
                group.join_segment_id = f"join:{group.id}"
            group.state = "joined"
            return {"kind": "children", "wait_group_id": group.id, "results": results}

    return await budgets.retry_deadlocks(transaction)


async def pending_wait_group(run_id: str) -> str | None:
    """Return the wait group a parked or requeued parent must join, if any."""
    async with _factory()() as session:
        group_id = (
            await session.execute(
                select(ChatDelegationGroup.id)
                .where(
                    ChatDelegationGroup.parent_run_id == run_id,
                    ChatDelegationGroup.state.in_(("waiting", "join_ready", "joined")),
                    ChatDelegationGroup.join_segment_id.is_(None),
                )
                .order_by(ChatDelegationGroup.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        return group_id


async def cancel_descendants(
    session: AsyncSession, root: ChatRun, *, quota, order: budgets.LockOrder, user_id: str,
    release_resources: bool = True, budget_root: ChatRun | None = None,
) -> list[str]:
    """Cancel every descendant under the root lock: park states finalize now, running ones get intent.

    Phase 1 locks all descendant rows (children class, depth then creation order); phase 2 then
    performs ledger/resource writes so the lock order stays monotone across the whole cascade.
    """
    lineage_root_id = root.root_run_id or root.id
    candidates = list(
        (await session.execute(
            select(ChatRun.id, ChatRun.parent_run_id)
            .where(ChatRun.root_run_id == lineage_root_id, ChatRun.id != root.id)
            .order_by(ChatRun.depth, ChatRun.created_at, ChatRun.id)
        )).all()
    )
    subtree = {root.id}
    descendants = []
    for run_id, parent_id in candidates:
        if parent_id in subtree:
            subtree.add(run_id)
            descendants.append(run_id)
    locked_runs = [await budgets.lock_run(session, run_id, order=order, lock_class="child") for run_id in descendants]
    now = _now()
    finalized: list[ChatRun] = []
    for locked in locked_runs:
        if locked.status not in NONTERMINAL:
            continue
        if locked.cancel_requested_at is None:
            locked.cancel_requested_at = now
        if locked.status in {"queued", "waiting_resource", "waiting_children", "awaiting_input"}:
            locked.status = "finalizing"
            await append_event(session, locked, _event(locked, "run.stage.changed", {"stage": "finalizing"}))
            locked.status = "canceled"
            await append_event(
                session,
                locked,
                _event(
                    locked,
                    "run.canceled",
                    {
                        "status": "canceled",
                        "message_id": None,
                        "error_code": "child_canceled",
                        "safe_message": "root run canceled",
                    },
                ),
            )
            finalized.append(locked)
    for locked in finalized:
        await record_child_terminal(
            session,
            locked,
            quota=quota,
            root=budget_root or root,
            order=order,
            status="canceled",
            error_code="child_canceled",
            result=child_result_payload(status="canceled", error_code="child_canceled", summary="", artifacts=[]),
            release_resource=False,
        )
    if release_resources:
        from lumen.services.infrastructure import store as infra_store

        for locked in finalized:
            await infra_store.release_sandbox_intent(session, run_id=locked.id, order=order)
    return [run.id for run in finalized]
