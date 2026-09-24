"""Durable worker execution and journal transitions."""

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import replace
from decimal import ROUND_CEILING, ROUND_HALF_EVEN, Decimal, InvalidOperation
from time import monotonic
from typing import Any

from lumen_plugin_api.contracts import Namespace, PluginError
from sqlalchemy import select

from lumen.config import get_settings
from lumen.crypto import decrypt_chat_content, encrypt_chat_content
from lumen.models.chat_db import ChatConversation, ChatMessage, ChatUsageLog
from lumen.models.chat_runs import ChatRun, ChatRunSegment, ChatRunTurn, ChatTempThread
from lumen.plugins import bindings as plugin_bindings
from lumen.plugins import skills_host
from lumen.services import (
    advisor,
    assets,
    context_manager,
    context_store,
    credit,
    engine,
    extensions_store,
    litellm_client,
    native_compaction,
)
from lumen.services import conversation_store as cs
from lumen.services.litellm_client import UsageCost
from lumen.services.memory_jobs import enqueue_completed_run_in_transaction
from lumen.services.providers import routing as ps
from lumen.services.run_store import (
    NONTERMINAL,
    RunStoreError,
    append_event,
    begin_segment_io,
    claim_queued_run,
    complete_segment_io,
    fail_unresolved_segment,
    load_segment_payload,
    prepare_segment,
    replay_events,
)
from lumen.services.structured_output import StructuredOutputError, parse_structured_output
from lumen.services.usage_breakdown import CACHE_USAGE_KEYS, UsageBreakdown

from . import budgets, children
from .common import (
    _V2_DEFAULT_MAX_MODEL_TURNS,
    _V2_DEFAULT_MAX_TOOL_CALLS,
    _event,
    _factory,
    _message_timestamps_for_run,
    _now,
    _payload,
    _provider_response_format,
    _selected_mcp_credential_versions,
    _selected_tool_ids,
    _tool_display_parts,
    _tool_result_error_code,
    _tool_result_status,
    _tools_enabled,
    _validate_run_protocol_payload,
)
from .errors import (
    DurableRunError,
    DurableRunInputError,
    DurableRunLeaseLost,
    DurableRunProtocolMismatch,
    DurableRunProviderResultUnknown,
)
from .interactions import _persist_v2_approval_interrupt, _v2_approval_resume
from .lifecycle import _cancel_requested, _renew_lease, _require_owned_running_lease


async def _v2_resume(*, run_id: str, owner: str) -> dict[str, Any] | None:
    """Answer exactly the interrupt the checkpoint is waiting on with a discriminated payload."""
    from lumen.services import graph

    pending = await graph.pending_interrupt(run_id)
    if pending is None:
        # No checkpointed interrupt: a fresh start, or a run that resumes past an already
        # consumed approval boundary. Legacy approval rows alone never fabricate a payload.
        return None
    if pending["kind"] == "tool_approval":
        decisions = await _v2_approval_resume(run_id=run_id, owner=owner)
        if decisions is None:
            raise DurableRunError("queued v2 run is waiting on approvals that were never recorded")
        return {"kind": "tool_approval", "decisions": decisions}
    wait_group_id = pending.get("wait_group_id")
    if not isinstance(wait_group_id, str):
        raise DurableRunError("children interrupt is missing its wait group")
    return await children.delegation_results(parent_run_id=run_id, owner=owner, wait_group_id=wait_group_id)


def _delegation_enabled(run: ChatRun, payload: dict[str, Any]) -> bool:
    """Advertise ``delegate_agent`` only when policy, budgets and a sandbox runtime all permit it."""
    if run.execution_protocol_version != 2 or run.credit_ceiling is None or run.sandbox_seconds_ceiling is None:
        return False
    try:
        policy = children._policy_from_snapshot(run.capability_snapshot or {})
    except DurableRunError:
        return False
    if not (policy.can_delegate_read or policy.can_delegate_write) or policy.max_children < 1:
        return False
    if int(run.depth or 0) >= policy.max_child_depth:
        return False
    runtime = get_settings().runtime_config
    return runtime.enabled and any(pool.enabled and pool.role == "sandbox" for pool in runtime.pools)

logger = logging.getLogger(__name__)


async def _append_temp_history(
    run_id: str, payload: dict[str, Any], parts: list[dict[str, Any]], *, owner: str
) -> None:
    factory = _factory()
    async with factory() as session, session.begin():
        run = (await session.execute(select(ChatRun).where(ChatRun.id == run_id).with_for_update())).scalar_one()
        _require_owned_running_lease(run, owner)
        if run.temp_thread_id is None:
            return
        thread = (
            await session.execute(
                select(ChatTempThread).where(ChatTempThread.id == run.temp_thread_id).with_for_update()
            )
        ).scalar_one_or_none()
        if thread is None:
            return
        try:
            history = json.loads(decrypt_chat_content(thread.history))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise DurableRunError("temporary chat history is invalid") from exc
        if not isinstance(history, list):
            raise DurableRunError("temporary chat history is invalid")
        input_parts = payload.get("input_parts")
        if not isinstance(input_parts, list):
            input_parts = [{"type": "text", "text": str(payload["input_messages"][-1]["content"])}]
        history.extend(
            [
                {"role": "user", "parts": input_parts},
                {"role": "assistant", "parts": parts},
            ]
        )
        thread.history = encrypt_chat_content(json.dumps(history, ensure_ascii=False, separators=(",", ":")))
        thread.active_run_id = None


async def _cancel_streaming_assistant_message(session, run: ChatRun) -> str | None:
    if run.assistant_message_id is None:
        return None
    message = (
        await session.execute(select(ChatMessage).where(ChatMessage.id == run.assistant_message_id).with_for_update())
    ).scalar_one_or_none()
    if message is None:
        return None
    if message.status == "streaming":
        deltas: dict[str, list[str]] = {"text": [], "reasoning": []}
        for event in await replay_events(session, run, after_seq=0):
            if event.type != "part.delta":
                continue
            payload = event.payload.model_dump()
            if payload.get("message_id") != str(message.id):
                continue
            part_type = payload.get("part_type")
            delta = payload.get("delta")
            if part_type in deltas and isinstance(delta, str):
                deltas[part_type].append(delta)
        if deltas["text"]:
            message.content = encrypt_chat_content("".join(deltas["text"]))
        if deltas["reasoning"]:
            message.reasoning = encrypt_chat_content("".join(deltas["reasoning"]))
        message.status = "canceled"
    return str(message.id)


async def _append(run_id: str, event_type: str, payload: dict[str, Any], *, owner: str) -> None:
    factory = _factory()
    async with factory() as session, session.begin():
        run = (
            await session.execute(select(ChatRun).where(ChatRun.id == run_id).with_for_update())
        ).scalar_one_or_none()
        if run is None:
            return
        _require_owned_running_lease(run, owner)
        await append_event(session, run, _event(run, event_type, payload))


async def _set_stage(
    run_id: str,
    stage: str,
    *,
    owner: str,
    tool_name: str | None = None,
) -> None:
    payload: dict[str, Any] = {"stage": stage}
    if tool_name is not None:
        payload["tool_name"] = tool_name
    await _append(run_id, "run.stage.changed", payload, owner=owner)


async def _finish(
    run_id: str,
    *,
    status: str,
    message_id: str | None,
    owner: str,
    error_code: str | None = None,
    safe_message: str | None = None,
    usage_record: dict[str, Any] | None = None,
    message_finalization: dict[str, Any] | None = None,
    completed_parts: list[tuple[int, dict[str, Any]]] | None = None,
    child_result: dict[str, Any] | None = None,
) -> None:
    factory = _factory()
    order = budgets.LockOrder()
    wake_parent: str | None = None
    async with factory() as session, session.begin():
        # Global lock order: project quota for code roots or lineages -> conversation/temp ->
        # root -> ancestors -> children -> ledgers -> resources.
        unlocked_run = (await session.execute(select(ChatRun).where(ChatRun.id == run_id))).scalar_one_or_none()
        if unlocked_run is None:
            return
        quota = None
        root = None
        has_children = (
            await session.execute(select(ChatRun.id).where(ChatRun.parent_run_id == run_id).limit(1))
        ).scalar_one_or_none() is not None
        if (unlocked_run.parent_run_id is not None or has_children or unlocked_run.execution_mode == "code"
                or unlocked_run.credit_ceiling is not None):
            quota = await budgets.lock_project_quota(session, unlocked_run.project_id, order=order)
        order.take("conversation")
        if unlocked_run.conversation_id is not None:
            await session.execute(
                select(ChatConversation.id).where(ChatConversation.id == unlocked_run.conversation_id).with_for_update()
            )
        elif unlocked_run.temp_thread_id is not None:
            await session.execute(
                select(ChatTempThread.id).where(ChatTempThread.id == unlocked_run.temp_thread_id).with_for_update()
            )
        if unlocked_run.parent_run_id is not None:
            _quota, root, _ancestors = await children._lock_lineage(session, unlocked_run, order, quota=quota)
            run = await budgets.lock_run(session, run_id, order=order, lock_class="child")
        else:
            run = await budgets.lock_run(session, run_id, order=order, lock_class="root")
            root = run
        _require_owned_running_lease(run, owner)
        if run.status not in NONTERMINAL:
            return
        await append_event(session, run, _event(run, "run.stage.changed", {"stage": "finalizing"}))
        run.status = "finalizing"
        if usage_record is not None:
            credited = await credit.apply_usage_in_transaction(
                session,
                event_id=f"run:{run.id}",
                user_id=run.user_id,
                project_id=run.project_id,
                model_name=usage_record["model_name"],
                provider=usage_record["provider_name"],
                prompt_tokens=usage_record["prompt_tokens"],
                completion_tokens=usage_record["completion_tokens"],
                usage_cost=usage_record["usage_cost"],
                margin_multiplier=usage_record["margin_multiplier"],
                credit_per_usd=usage_record["credit_per_usd"],
                usage_components=usage_record["usage_components"],
                conversation_id=run.conversation_id,
                source=run.source,
                api_key_id=run.api_key_id,
                run_id=run.id,
                breakdown=usage_record.get("breakdown"),
            )
            run.usage_reconciled_at = _now()
            await append_event(
                session,
                run,
                _event(
                    run,
                    "usage.updated",
                    {
                        "components": usage_record["usage_components"],
                        "prompt_tokens": usage_record["prompt_tokens"],
                        "completion_tokens": usage_record["completion_tokens"],
                        "raw_cost": format(usage_record["usage_cost"].raw_cost, "f"),
                        "credited_cost": format(credited, "f"),
                    },
                ),
            )
        if completed_parts is not None and message_id is not None and run.conversation_id is not None:
            terminal_turn = (
                await session.execute(
                    select(ChatRunTurn)
                    .where(
                        ChatRunTurn.run_id == run.id,
                        ChatRunTurn.assistant_message_id == int(message_id),
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if terminal_turn is None:
                raise DurableRunError("terminal assistant message has no durable turn")
            if terminal_turn.completion_event_seq is None:
                completion_event_seq = 0
                for part_index, part in completed_parts:
                    event = await append_event(
                        session,
                        run,
                        _event(
                            run, "part.completed", {"message_id": message_id, "part_index": part_index, "part": part}
                        ),
                    )
                    completion_event_seq = event.seq
                terminal_turn.completion_event_seq = completion_event_seq
        if message_finalization is not None:
            await cs.complete_message_in_transaction(
                session,
                message_finalization["conversation_id"],
                message_id=message_finalization["message_id"],
                content=message_finalization["content"],
                reasoning=message_finalization["reasoning"],
                model_name=message_finalization["model_name"],
                canonical_parts=message_finalization.get("canonical_parts"),
                status=message_finalization.get("message_status", "complete"),
                token_prompt=message_finalization["token_prompt"],
                token_completion=message_finalization["token_completion"],
            )
        elif status != "completed" and run.conversation_id is not None:
            turn_message_ids = (
                (
                    await session.execute(
                        select(ChatRunTurn.assistant_message_id).where(
                            ChatRunTurn.run_id == run.id,
                            ChatRunTurn.assistant_message_id.is_not(None),
                        )
                    )
                )
                .scalars()
                .all()
            )
            if turn_message_ids:
                deltas: dict[str, dict[str, list[str]]] = {
                    str(message_id): {"text": [], "reasoning": []} for message_id in turn_message_ids
                }
                for event in await replay_events(session, run, after_seq=0):
                    if event.type != "part.delta":
                        continue
                    payload = event.payload.model_dump()
                    event_message_id = payload.get("message_id")
                    part_type = payload.get("part_type")
                    delta = payload.get("delta")
                    if (
                        isinstance(event_message_id, str)
                        and part_type in ("text", "reasoning")
                        and isinstance(delta, str)
                        and event_message_id in deltas
                    ):
                        deltas[event_message_id][part_type].append(delta)
                messages = (
                    (await session.execute(select(ChatMessage).where(ChatMessage.id.in_(turn_message_ids))))
                    .scalars()
                    .all()
                )
                for message in messages:
                    if message.status != "streaming":
                        continue
                    recovered = deltas[str(message.id)]
                    if recovered["text"]:
                        message.content = encrypt_chat_content("".join(recovered["text"]))
                    if recovered["reasoning"]:
                        message.reasoning = encrypt_chat_content("".join(recovered["reasoning"]))
                    message.status = status
        run.status = status
        memory_enabled = False
        if status == "completed":
            try:
                request = _payload(run)
                features = request.get("features")
                memory_enabled = isinstance(features, dict) and features.get("memory") is True
            except DurableRunError:
                logger.warning("completed chat run has unreadable memory feature setting run_id=%s", run.id)
        if getattr(run, "run_kind", "completion") == "completion":
            await enqueue_completed_run_in_transaction(session, run, memory_enabled=memory_enabled)
            if (
                status == "completed"
                and run.run_scope == "persistent"
                and run.parent_run_id is None
                and run.conversation_id is not None
            ):
                from lumen.services import title_jobs

                await title_jobs.enqueue_completed_run_in_transaction(session, run)
        event_type = {"completed": "run.completed", "failed": "run.failed", "canceled": "run.canceled"}[status]
        payload: dict[str, Any] = {"status": status, "message_id": message_id}
        if status != "completed":
            payload.update(error_code=error_code or "run_failed", safe_message=safe_message or "chat run failed")
        await append_event(session, run, _event(run, event_type, payload))
        if run.parent_run_id is not None:
            finalized = []
            if has_children and status != "completed":
                finalized = await children.cancel_descendants(
                    session, run, quota=quota, order=order, user_id=run.user_id,
                    release_resources=False, budget_root=root,
                )
            result = child_result or children.child_result_payload(
                status=status, error_code=error_code, summary="", artifacts=[]
            )
            wake_parent = await children.record_child_terminal(
                session, run, quota=quota, root=root, order=order, status=status,
                error_code=error_code, result=result, release_resource=False,
            )
            from lumen.services.infrastructure import store as infra_store

            for child_id in finalized:
                await infra_store.release_sandbox_intent(session, run_id=child_id, order=order)
            await infra_store.release_sandbox_intent(session, run_id=run.id, order=order)
        else:
            finalized = []
            if has_children and status != "completed":
                finalized = await children.cancel_descendants(
                    session, run, quota=quota, order=order, user_id=run.user_id, release_resources=False
                )
            if run.execution_mode == "code":
                await budgets.settle_root_sandbox(session, quota=quota, root=run, order=order)
            if quota is not None:
                await budgets.close_root_budget(session, quota=quota, root=run, order=order)
            from lumen.services.infrastructure import store as infra_store

            for child_id in finalized:
                await infra_store.release_sandbox_intent(session, run_id=child_id, order=order)
            if run.execution_mode == "code":
                await infra_store.release_sandbox_intent(session, run_id=run.id, order=order)
    if wake_parent is not None:
        from .common import wake_run

        await wake_run(wake_parent)



def _bounded_credits(pricing: dict[str, Any], input_tokens: int, output_tokens: int, *,
                     input_keys: tuple[str, ...], output_key: str) -> Decimal:
    """Round a worst-case frozen price upward, never a live catalog price."""
    try:
        rates = [Decimal(str(pricing[key])) for key in input_keys if pricing.get(key) is not None]
        output_rate = Decimal(str(pricing[output_key]))
        if not rates or any(not rate.is_finite() or rate < 0 for rate in [*rates, output_rate]):
            raise ValueError("missing or invalid frozen price")
        multiplier = Decimal(str(pricing["margin_multiplier"])) * Decimal(str(pricing["chat_credit_per_usd"]))
        if not multiplier.is_finite() or multiplier < 0:
            raise ValueError("invalid credit conversion")
        cost = max(rates) * input_tokens + output_rate * output_tokens
        # Each of the at most five billed token categories is rounded to 1e-10 USD.
        # Cushion only nonzero estimates so their rounded sum cannot exceed the hold.
        return ((cost + (Decimal("0.0000000005") if cost else 0)) * multiplier).quantize(
            Decimal("0.00000001"), rounding=ROUND_CEILING
        )
    except (KeyError, InvalidOperation, TypeError, ValueError) as exc:
        raise DurableRunError("bounded credit pricing is unavailable") from exc


def _provider_credit_bound(run: ChatRun, *, messages: list[dict], tool_schemas: list[dict],
                           max_tokens: int | None, summary: bool = False) -> Decimal:
    snapshot = run.pricing_snapshot
    pricing = (snapshot.get("summary_route") or snapshot) if summary and isinstance(snapshot, dict) else snapshot
    if not isinstance(pricing, dict):
        raise DurableRunError("frozen provider pricing is unavailable")
    pricing = {**pricing, "margin_multiplier": snapshot["margin_multiplier"],
               "chat_credit_per_usd": snapshot["chat_credit_per_usd"]}
    capabilities = run.capability_snapshot if isinstance(run.capability_snapshot, dict) else {}
    if not summary:
        features = capabilities.get("effective_features")
        search = features.get("web_search") if isinstance(features, dict) else None
        if isinstance(search, dict) and search.get("enabled") is True and search.get("mode") == "native":
            # The provider may perform an unbounded number of searches in one turn.
            # Only explicitly frozen zero request/context rates need no hold.
            prices = snapshot.get("component_prices") or {}
            keys = ("web_search_request_per_unit", f"web_search_context_{search.get('context_size')}_per_unit")
            if any(key not in prices or Decimal(str(prices[key])) != 0 for key in keys):
                raise DurableRunError("native web search has no provider-enforced request bound")
    route = (capabilities.get("summary_route") or capabilities.get("capabilities")) if summary else capabilities.get("capabilities")
    route = route if isinstance(route, dict) else capabilities
    limit = route.get("context_limit")
    count = litellm_client.count_context_tokens(
        str(route.get("model_name") or run.model_name), messages, tool_schemas
    )
    input_keys = ("input_price_per_token", "cache_read_price_per_token",
                  "cache_write_price_per_token", "cache_write_1h_price_per_token",
                  "cache_read_price_per_token_above_200k", "cache_write_price_per_token_above_200k",
                  "cache_write_1h_price_per_token_above_200k")
    priced_input = any(Decimal(str(pricing.get(key) or 0)) > 0 for key in input_keys)
    priced_output = Decimal(str(pricing.get("output_price_per_token") or 0)) > 0
    if priced_input and (type(limit) is not int or limit <= 0):
        raise DurableRunError("bounded provider input requires a frozen context limit")
    if priced_output and (type(max_tokens) is not int or max_tokens <= 0):
        raise DurableRunError("bounded provider output requires max_tokens")
    if count.tokens is not None and type(limit) is int and count.tokens > limit:
        raise DurableRunError("provider context exceeds frozen input bound")
    return _bounded_credits(pricing, limit if priced_input else 0, max_tokens if priced_output else 0,
                            input_keys=input_keys, output_key="output_price_per_token")


def _tool_credit_bound(run: ChatRun, name: str) -> Decimal:
    snapshot = run.pricing_snapshot
    prices = snapshot.get("component_prices") if isinstance(snapshot, dict) else None
    if not isinstance(prices, dict):
        raise DurableRunError("frozen tool pricing is unavailable")
    if name == "managed_web_search":
        features = run.capability_snapshot.get("effective_features") or {}
        options = features.get("web_search") or {}
        size = options.get("context_size")
        if size not in {"low", "medium", "high"}:
            raise DurableRunError("managed search context bound is unavailable")
        keys = ("web_search_request_per_unit", f"web_search_context_{size}_per_unit")
        usd = sum(Decimal(str(prices[key])) for key in keys)
    elif name == "managed_web_fetch":
        usd = sum(Decimal(str(prices[key])) for key in ("web_fetch_request_per_unit", "web_fetch_context_per_unit"))
    elif name == "managed_advisor":
        route = (run.capability_snapshot.get("feature_routes") or {}).get("advisor") or {}
        limit = route.get("context_limit")
        keys = ("advisor_input_price_per_token", "advisor_cache_read_price_per_token",
                "advisor_cache_write_price_per_token", "advisor_cache_write_1h_price_per_token",
                "advisor_cache_read_price_per_token_above_200k",
                "advisor_cache_write_price_per_token_above_200k",
                "advisor_cache_write_1h_price_per_token_above_200k")
        rates = [Decimal(str(prices[key])) for key in keys if prices.get(key) is not None]
        if not rates or (max(rates) > 0 and (type(limit) is not int or limit <= 0)):
            raise DurableRunError("bounded advisor input requires frozen context limit")
        usd = max(rates) * (limit if type(limit) is int else 0) + Decimal(str(prices["advisor_output_price_per_token"])) * advisor._MAX_ADVISOR_TOKENS
    else:
        return Decimal("0")
    multiplier = Decimal(str(snapshot["margin_multiplier"])) * Decimal(str(snapshot["chat_credit_per_usd"]))
    if not usd.is_finite() or usd < 0 or not multiplier.is_finite() or multiplier < 0:
        raise DurableRunError("frozen tool pricing is invalid")
    return ((usd + (Decimal("0.0000000002") if usd else 0)) * multiplier).quantize(
        Decimal("0.00000001"), rounding=ROUND_CEILING
    )

class _DurableExecutionHooks:
    """Persist and replay each external graph boundary for one leased run."""

    journals_tool_events = True

    def __init__(self, *, run_id: str, owner: str, credit_bounded: bool = False,
                 skill_snapshots: list[dict] | None = None, user_id: str = "", project_id: str = "") -> None:
        self.run_id = run_id
        self.owner = owner
        self.requires_credit_reservation = credit_bounded
        self.skill_snapshots = skill_snapshots or []
        self.user_id = user_id
        self.project_id = project_id
        self.active_turn_ordinal: int | None = None
        self.force_compaction = False
        self.active_message_id: str | None = None
        self._conversation_id: str | None = None
        self._temp_thread_id: str | None = None

    async def revalidate_skills(self) -> None:
        if self.skill_snapshots:
            await skills_host.revalidate_skills(self.skill_snapshots, user_id=self.user_id, project_id=self.project_id)

    async def loaded_tool_names(self) -> list[str]:
        """Restore completed deferred bindings after restart or approval resume."""
        factory = _factory()
        async with factory() as session:
            segments = (
                await session.execute(
                    select(ChatRunSegment)
                    .where(
                        ChatRunSegment.run_id == self.run_id,
                        ChatRunSegment.endpoint == "tool",
                        ChatRunSegment.status == "completed",
                    )
                    .order_by(ChatRunSegment.ordinal.asc())
                )
            ).scalars()
            loaded: list[str] = []
            for segment in segments:
                try:
                    payload = load_segment_payload(segment.result_payload)
                    if not isinstance(payload, dict) or payload.get("tool_name") != "list_available_tools":
                        continue
                    content = json.loads(str(payload.get("content") or ""))
                    names = content.get("loaded_tools") if isinstance(content, dict) else None
                except (RunStoreError, TypeError, ValueError, json.JSONDecodeError):
                    continue
                if not isinstance(names, list):
                    continue
                for name in names:
                    if isinstance(name, str) and name not in loaded:
                        loaded.append(name)
            return loaded

    async def _record_summary_usage(
        self,
        *,
        segment_id: str,
        usage_payload: dict[str, Any],
        route: dict[str, Any],
    ) -> None:
        """Record one observed summary call with a stable, replay-safe ledger key."""
        try:
            breakdown = _usage_payload_breakdown(usage_payload)
        except (TypeError, ValueError) as exc:
            raise DurableRunError("context compaction usage is invalid") from exc
        prompt_tokens = breakdown.input_tokens
        completion_tokens = breakdown.output_tokens
        factory = _factory()
        async with factory() as session, session.begin():
            run = (
                await session.execute(select(ChatRun).where(ChatRun.id == self.run_id).with_for_update())
            ).scalar_one()
            _require_owned_running_lease(run, self.owner)
            event_id = f"context:{run.id}:{segment_id}"
            existing = (
                await session.execute(select(ChatUsageLog.id).where(ChatUsageLog.event_id == event_id).limit(1))
            ).scalar_one_or_none()
            if existing is not None:
                return
            root_pricing = run.pricing_snapshot if isinstance(run.pricing_snapshot, dict) else {}
            summary_pricing = root_pricing.get("summary_route")
            summary_pricing = summary_pricing if isinstance(summary_pricing, dict) else {}
            pricing = {
                "input_price_per_token": summary_pricing.get(
                    "input_price_per_token", route.get("input_price_per_token")
                ),
                "output_price_per_token": summary_pricing.get(
                    "output_price_per_token", route.get("output_price_per_token")
                ),
                "price_source": summary_pricing.get("price_source"),
                "price_version": summary_pricing.get("price_version"),
                "cache_price_sources": summary_pricing.get("cache_price_sources") or route.get("cache_price_sources") or {},
                # Snapshots frozen before cache rates existed carry no cache keys.
                # The route fallback mirrors the input/output keys above: a live
                # route only resolves under the admission config hash, which
                # pins the same (then null) cache rates, and a replayed route
                # snapshot carries no rates, so those categories bill 0 (partial).
                **{
                    key: summary_pricing.get(key, route.get(key))
                    for key in (
                        "cache_read_price_per_token",
                        "cache_write_price_per_token",
                        "cache_write_1h_price_per_token",
                        "cache_read_price_per_token_above_200k",
                        "cache_write_price_per_token_above_200k",
                        "cache_write_1h_price_per_token_above_200k",
                    )
                },
            }
            if pricing["input_price_per_token"] is None or pricing["output_price_per_token"] is None:
                raise DurableRunError("context compaction pricing is unavailable")
            usage_cost = credit.usage_cost_from_pricing_snapshot(
                pricing,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                breakdown=breakdown,
            )
            components = _token_usage_components(
                breakdown,
                usage_cost,
                segment_id=segment_id,
                source="system",
                model_name=route.get("model_name") or run.model_name,
                metadata={"operation": "context_compaction"},
            )
            await credit.apply_usage_in_transaction(
                session,
                event_id=event_id,
                user_id=run.user_id,
                project_id=run.project_id,
                model_name=str(route.get("model_name") or run.model_name),
                provider=route.get("provider_name"),
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                usage_cost=usage_cost,
                margin_multiplier=root_pricing.get("margin_multiplier", "1"),
                credit_per_usd=root_pricing.get("chat_credit_per_usd"),
                conversation_id=run.conversation_id,
                source="system",
                api_key_id=getattr(run, "api_key_id", None),
                run_id=str(run.id),
                charge_wallet=False,
                usage_components=components,
                breakdown=breakdown,
            )

    async def _summary_compactor(
        self,
        chunks: list[str],
        *,
        context: dict[str, Any],
        route: dict[str, Any],
        run_context: dict[str, Any],
    ) -> dict[str, Any]:
        """Execute one fenced summary call, replaying only completed results."""
        round_index = int(context.get("round", 0))
        chunk_index = int(context.get("chunk_index", 0))
        reduce_round = round_index if context.get("phase") == "reduce" else 0
        ordinal = 100 + round_index * 64 + chunk_index * 4 + reduce_round
        phase = "reduce" if context.get("phase") == "reduce" else "map"
        segment_id = f"context:{round_index}:{chunk_index}:{phase}"
        if await _cancel_requested(self.run_id):
            raise DurableRunError("context compaction canceled")

        factory = _factory()
        async with factory() as session:
            run = (await session.execute(select(ChatRun).where(ChatRun.id == self.run_id))).scalar_one()
            await credit.precheck(run.user_id, run.project_id, api_key_id=getattr(run, "api_key_id", None))
        summary_run_context = context.get("run_context")
        summary_run_context = summary_run_context if isinstance(summary_run_context, dict) else {}
        system_prompt = str(summary_run_context.get("summary_system_prompt") or context_manager._SUMMARY_SYSTEM)
        messages = context.get("summary_messages")
        if not isinstance(messages, list) or not all(isinstance(item, dict) for item in messages):
            messages = context_manager._summary_messages(chunks, system_prompt=system_prompt)
        output_bound = min(4096, int(run_context.get("summary_output_reserve") or 512))
        started = await self._start(
            segment_id=segment_id,
            ordinal=ordinal,
            endpoint="context_compaction",
            turn_ordinal=round_index,
            credit_bound=(lambda run: _provider_credit_bound(
                run, messages=messages, tool_schemas=[], max_tokens=output_bound, summary=True,
            )) if self.requires_credit_reservation else None,
        )
        usage_payload: dict[str, Any] = {}
        if isinstance(started, dict):
            payload = started
            if payload.get("_boundary_abort"):
                raise DurableRunProviderResultUnknown("context compaction result is indeterminate")
            usage = payload.pop("_durable_usage", None)
            if isinstance(usage, dict):
                usage_payload = usage
            if isinstance(payload.get("summary"), str) and isinstance(payload.get("title"), str):
                await self._record_summary_usage(
                    segment_id=segment_id,
                    usage_payload=usage_payload,
                    route=route,
                )
                payload.pop("_durable_replay", None)
                return payload
            raise DurableRunError("completed context segment has invalid result")
        if await _cancel_requested(self.run_id):
            raise DurableRunError("context compaction canceled")

        # provider credentials immediately after the write-ahead fence and
        # never place the resolved secret in a durable payload.
        resolved_route = await ps.resolve_model_snapshot(route)
        if resolved_route is None:
            await self._fail(
                segment_id=segment_id,
                ordinal=ordinal,
                endpoint="context_compaction",
                turn_ordinal=round_index,
            )
            raise DurableRunError("context compaction route is unavailable")
        segment_completed = False
        try:
            response = await litellm_client.acompletion(
                model=resolved_route["model_name"],
                messages=messages,
                api_base=resolved_route.get("api_base"),
                api_key=resolved_route.get("api_key"),
                custom_llm_provider=resolved_route.get("provider_type"),
                provider_auth=resolved_route.get("provider_auth"),
                max_tokens=output_bound,
                temperature=0,
                extra={"response_format": {"type": "json_object"}},
            )
            choices = response.get("choices") if isinstance(response, dict) else getattr(response, "choices", None)
            first = choices[0] if choices else None
            message = first.get("message") if isinstance(first, dict) else getattr(first, "message", None)
            content = message.get("content") if isinstance(message, dict) else getattr(message, "content", None)
            parsed = json.loads(content) if isinstance(content, str) else None
            if (
                not isinstance(parsed, dict)
                or set(parsed) != {"summary", "title"}
                or not isinstance(parsed["summary"], str)
                or not isinstance(parsed["title"], str)
                or not parsed["summary"]
                or len(parsed["summary"]) > 16_000
                or len(parsed["title"]) > 80
            ):
                raise DurableRunError("context compaction result is invalid")
            usage = response.get("usage") if isinstance(response, dict) else getattr(response, "usage", None)
            observed_usage = UsageBreakdown.from_runtime(usage)
            usage_payload = (
                observed_usage
                or litellm_client.extract_usage_breakdown(resolved_route["model_name"], messages, content, None)
            ).as_usage_dict()
            await self._complete(
                segment_id=segment_id,
                ordinal=ordinal,
                endpoint="context_compaction",
                turn_ordinal=round_index,
                result_payload=parsed,
                usage_payload={**usage_payload, **({"_durable_estimated": True} if observed_usage is None else {})},
            )
            segment_completed = True
            await self._record_summary_usage(
                segment_id=segment_id,
                usage_payload=usage_payload,
                route=resolved_route,
            )
            return {**parsed, "usage": usage_payload}
        except BaseException:
            if not segment_completed:
                await self._fail(
                    segment_id=segment_id,
                    ordinal=ordinal,
                    endpoint="context_compaction",
                    turn_ordinal=round_index,
                )
            raise

    async def _persist_context_result(
        self,
        *,
        prepared: context_manager.PreparedContext,
        source: dict[str, Any],
        tool_schemas: list[dict[str, Any]],
        model_name: str,
        context_limit: int,
        output_reserve: int,
        revision: str,
        cause: str,
        plan: dict[str, Any] | None = None,
    ) -> None:
        """Atomically apply checkpoint, title, and the public context event."""
        if not prepared.compacted or not prepared.summary:
            return
        prefix_n = len(prepared.source_hashes)
        raw_ids = source.get("message_ids")
        if isinstance(raw_ids, list):
            source_ids = tuple(str(item) for item in raw_ids[:prefix_n])
        else:
            source_ids = tuple(prepared.source_message_ids)
        source_hashes = tuple(prepared.source_hashes)
        if not source_ids or len(source_ids) != len(source_hashes):
            raise DurableRunError("context compaction provenance is unavailable")
        metadata = {
            "version": 1,
            "projection_version": 1,
            "scope": "conversation",
            "source_revision": revision,
            "model_name": model_name,
            "tokenizer": prepared.measurement,
            "tool_schema_hash": source.get("tool_schema_hash"),
            "input_budget": prepared.input_budget,
            "before_tokens": prepared.before_tokens,
            "after_tokens": prepared.after_tokens,
            "cause": cause,
            "active_leaf_id": source.get("active_leaf_id"),
        }
        factory = _factory()
        async with factory() as session, session.begin():
            # Parent scope is locked before the run, matching admission/_finish.
            parent = None
            if self._conversation_id is not None:
                parent = (
                    await session.execute(
                        select(ChatConversation).where(ChatConversation.id == self._conversation_id).with_for_update()
                    )
                ).scalar_one_or_none()
            elif self._temp_thread_id is not None:
                parent = (
                    await session.execute(
                        select(ChatTempThread).where(ChatTempThread.id == self._temp_thread_id).with_for_update()
                    )
                ).scalar_one_or_none()
            run = (
                await session.execute(select(ChatRun).where(ChatRun.id == self.run_id).with_for_update())
            ).scalar_one()
            _require_owned_running_lease(run, self.owner)
            if parent is None:
                raise DurableRunError("context compaction parent is unavailable")
            if run.conversation_id is not None:
                metadata["scope"] = "conversation"
            else:
                metadata["scope"] = "temp"
            checkpoint = await context_store.persist_context_checkpoint(
                session,
                run=run,
                source=source,
                summary=prepared.summary,
                source_message_ids=source_ids,
                source_hashes=source_hashes,
                token_estimate=prepared.after_tokens,
                context_limit=context_limit,
                context_metadata=metadata,
            )
            if run.conversation_id is not None and prepared.title and parent.title_source != "explicit":
                parent.title = encrypt_chat_content(prepared.title)
                parent.title_source = "auto"
                parent.title_status = "ready"
                parent.title_revision = int(parent.title_revision or 0) + 1
            state = context_manager.context_state(
                prepared.messages,
                tool_schemas,
                model_name=model_name,
                context_limit=context_limit,
                output_reserve=output_reserve,
                revision=revision,
                checkpoint_id=str(checkpoint.id),
                plan={**plan, "summary_checkpoint_id": str(checkpoint.id)} if plan else None,
                scope="request",
            )
            await append_event(
                session,
                run,
                _event(
                    run,
                    "context.updated",
                    {
                        "state": state.model_dump(mode="json"),
                        "phase": "compacted",
                        "cause": cause,
                        "before_tokens": prepared.before_tokens,
                        "after_tokens": prepared.after_tokens,
                    },
                ),
            )

    async def prepare_context(
        self,
        *,
        messages: list[dict],
        tool_schemas: list[dict],
        round_index: int,
    ) -> list[dict]:
        """Apply the same budget fence before every provider call."""
        factory = _factory()
        async with factory() as session:
            run = (await session.execute(select(ChatRun).where(ChatRun.id == self.run_id))).scalar_one()
            self._conversation_id = run.conversation_id
            self._temp_thread_id = run.temp_thread_id
            payload = _payload(run)
            capabilities = run.capability_snapshot if isinstance(run.capability_snapshot, dict) else {}
            route_capabilities = capabilities.get("capabilities")
            route_capabilities = route_capabilities if isinstance(route_capabilities, dict) else capabilities
            context_limit = route_capabilities.get("context_limit")
            try:
                context_limit = int(context_limit) if context_limit is not None else None
            except (TypeError, ValueError):
                context_limit = None
            output_reserve = int(payload.get("max_tokens") or 4096)
            source = payload.get("context_source") if isinstance(payload.get("context_source"), dict) else {}
            revision = str(source.get("revision") or f"run:{self.run_id}")
            plan = payload.get("context_plan") if isinstance(payload.get("context_plan"), dict) else None
            state = context_manager.context_state(
                messages,
                tool_schemas,
                model_name=run.model_name,
                context_limit=context_limit,
                output_reserve=output_reserve,
                revision=revision,
                plan=plan,
                scope="request",
            )
        if state.measurement == "unknown" or state.input_budget is None:
            await _append(
                self.run_id,
                "run.warning",
                {
                    "code": "context_budget_unavailable",
                    "safe_message": "Context budget is unavailable; automatic compaction was skipped.",
                },
                owner=self.owner,
            )
            return messages
        if state.recommendation != "required" and not self.force_compaction:
            return messages
        if self._conversation_id is None and self._temp_thread_id is None:
            if state.input_tokens is None or state.input_tokens > state.input_budget:
                raise context_manager.ContextLimitExceeded("context_limit_exceeded")
            return messages
        route = capabilities.get("summary_route")
        if not isinstance(route, dict):
            route = capabilities
        summary_context_limit: int | None = None
        if isinstance(route, dict) and route.get("model_name"):
            try:
                candidate_limit = route.get("context_limit")
                summary_context_limit = int(candidate_limit) if candidate_limit is not None else None
            except (TypeError, ValueError):
                summary_context_limit = None
        if summary_context_limit is None or summary_context_limit <= 0:
            if self.force_compaction:
                raise context_manager.ContextLimitExceeded(
                    "context_budget_unavailable", code="context_budget_unavailable"
                )
            await _append(
                self.run_id,
                "run.warning",
                {
                    "code": "context_budget_unavailable",
                    "safe_message": "Summary context budget is unavailable; automatic compaction was skipped.",
                },
                owner=self.owner,
            )
            return messages
        summary_model_name = str(route["model_name"])
        await _append(
            self.run_id,
            "context.updated",
            {
                "state": state.model_copy(update={"active_compaction_run_id": self.run_id}).model_dump(mode="json"),
                "phase": "compacting",
                "cause": "manual" if self.force_compaction else "automatic",
                "before_tokens": state.input_tokens,
                "after_tokens": None,
            },
            owner=self.owner,
        )
        checkpoint = source.get("checkpoint") if isinstance(source.get("checkpoint"), dict) else {}
        previous_summary = checkpoint.get("summary") if isinstance(checkpoint.get("summary"), str) else ""
        checkpoint_raw_prefix_count = (
            len(checkpoint.get("source_message_ids"))
            if isinstance(checkpoint.get("source_message_ids"), list)
            else None
        )

        async def compact(chunks: list[str], context: dict[str, Any]) -> dict[str, Any]:
            return await self._summary_compactor(
                chunks,
                context=context,
                route=route,
                run_context={
                    "summary_context_limit": summary_context_limit,
                    "summary_output_reserve": 512,
                    "summary_token_counter": lambda current, tools: litellm_client.count_context_tokens(
                        summary_model_name, current, tools
                    ),
                    "previous_summary": previous_summary,
                    "stored_messages": source.get("messages"),
                    "source_message_ids": source.get("message_ids"),
                    "source_hashes": source.get("source_hashes"),
                    "checkpoint_raw_prefix_count": checkpoint_raw_prefix_count,
                    "summary_projection_count": 1,
                },
            )

        try:
            prepared = await context_manager.prepare_model_messages(
                messages,
                tool_schemas,
                context_limit,
                output_reserve,
                {
                    "summary_context_limit": summary_context_limit,
                    "summary_output_reserve": 512,
                    "summary_token_counter": lambda current, tools: litellm_client.count_context_tokens(
                        summary_model_name, current, tools
                    ),
                    "force_compaction": self.force_compaction,
                    "previous_summary": previous_summary,
                    "stored_messages": source.get("messages"),
                    "source_message_ids": source.get("message_ids"),
                    "source_hashes": source.get("source_hashes"),
                    "checkpoint_raw_prefix_count": checkpoint_raw_prefix_count,
                    "summary_projection_count": 1,
                },
                token_counter=lambda current, tools: (
                    context_manager._count_result(
                        current, tools, lambda m, t: litellm_client.count_context_tokens(run.model_name, m, t)
                    )[0]
                    or 0
                ),
                compactor=compact,
            )
        except context_manager.ContextLimitExceeded:
            # Automatic compaction may continue once with the unchanged input
            # only when it is still within budget; known overflow fails closed.
            if (
                not self.force_compaction
                and state.input_budget is not None
                and state.input_tokens is not None
                and state.input_tokens <= state.input_budget
            ):
                await _append(
                    self.run_id,
                    "context.updated",
                    {
                        "state": state.model_dump(mode="json"),
                        "phase": "failed",
                        "cause": "automatic",
                        "before_tokens": state.input_tokens,
                        "after_tokens": state.input_tokens,
                    },
                    owner=self.owner,
                )
                return messages
            try:
                await _append(
                    self.run_id,
                    "context.updated",
                    {
                        "state": state.model_dump(mode="json"),
                        "phase": "failed",
                        "cause": "manual" if self.force_compaction else "automatic",
                        "before_tokens": state.input_tokens,
                        "after_tokens": None,
                    },
                    owner=self.owner,
                )
            except Exception:
                logger.exception("failed to persist context failure event run_id=%s", self.run_id)
            raise
        except Exception as exc:
            if (
                not self.force_compaction
                and state.input_budget is not None
                and state.input_tokens is not None
                and state.input_tokens <= state.input_budget
            ):
                try:
                    await _append(
                        self.run_id,
                        "context.updated",
                        {
                            "state": state.model_dump(mode="json"),
                            "phase": "failed",
                            "cause": "automatic",
                            "before_tokens": state.input_tokens,
                            "after_tokens": state.input_tokens,
                        },
                        owner=self.owner,
                    )
                except Exception:
                    logger.exception("failed to persist context failure event run_id=%s", self.run_id)
                return messages
            try:
                await _append(
                    self.run_id,
                    "context.updated",
                    {
                        "state": state.model_dump(mode="json"),
                        "phase": "failed",
                        "cause": "manual" if self.force_compaction else "automatic",
                        "before_tokens": state.input_tokens,
                        "after_tokens": None,
                    },
                    owner=self.owner,
                )
            except Exception:
                logger.exception("failed to persist context failure event run_id=%s", self.run_id)
            if (
                not self.force_compaction
                and state.input_budget is not None
                and state.input_tokens is not None
                and state.input_tokens > state.input_budget
            ):
                raise context_manager.ContextLimitExceeded("context_limit_exceeded") from exc
            raise
        await self._persist_context_result(
            prepared=prepared,
            source=source,
            tool_schemas=tool_schemas,
            model_name=run.model_name,
            context_limit=context_limit,
            output_reserve=output_reserve,
            revision=revision,
            cause="manual" if self.force_compaction else "automatic",
            plan=plan,
        )
        return prepared.messages

    async def _ensure_provider_turn(self, session, run: ChatRun, turn_ordinal: int) -> None:
        turn = (
            await session.execute(
                select(ChatRunTurn)
                .where(ChatRunTurn.run_id == run.id, ChatRunTurn.ordinal == turn_ordinal)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if turn is None and run.conversation_id is None:
            turn = ChatRunTurn(run_id=run.id, ordinal=turn_ordinal)
            session.add(turn)
            message_id = f"temp:{run.id}:assistant:{turn_ordinal}"
            event = await append_event(
                session,
                run,
                _event(
                    run,
                    "message.created",
                    {"message_id": message_id, "role": "assistant", "parent_id": None},
                ),
            )
            turn.message_event_seq = event.seq
        elif turn is None:
            previous_message_id = (
                await session.execute(
                    select(ChatRunTurn.assistant_message_id)
                    .where(ChatRunTurn.run_id == run.id, ChatRunTurn.ordinal < turn_ordinal)
                    .order_by(ChatRunTurn.ordinal.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            created_at, created_at_local, created_timezone = _message_timestamps_for_run(run)
            placeholder = ChatMessage(
                conversation_id=run.conversation_id,
                role="assistant",
                parent_id=previous_message_id or run.user_message_id,
                content=encrypt_chat_content(""),
                status="streaming",
                model_name=run.model_name,
                created_at=created_at,
                created_at_local=created_at_local,
                created_timezone=created_timezone,
            )
            session.add(placeholder)
            await session.flush()
            turn = ChatRunTurn(
                run_id=run.id,
                ordinal=turn_ordinal,
                assistant_message_id=placeholder.id,
            )
            session.add(turn)
            event = await append_event(
                session,
                run,
                _event(
                    run,
                    "message.created",
                    {
                        "message_id": str(placeholder.id),
                        "role": "assistant",
                        "parent_id": str(placeholder.parent_id) if placeholder.parent_id is not None else None,
                    },
                ),
            )
            turn.message_event_seq = event.seq
        if turn is None:
            raise DurableRunError("failed to create a durable run turn")
        run.current_ordinal = turn_ordinal
        self.active_turn_ordinal = turn_ordinal
        if run.conversation_id is None:
            self.active_message_id = f"temp:{run.id}:assistant:{turn_ordinal}"
        else:
            if turn.assistant_message_id is None:
                raise DurableRunError("persistent run turn is missing its assistant message")
            run.assistant_message_id = turn.assistant_message_id
            self.active_message_id = str(turn.assistant_message_id)

    async def _credit_lineage(self, session):
        """Acquire quota and lineage before the segment ledger, including on completion."""
        identity = (await session.execute(select(ChatRun).where(ChatRun.id == self.run_id))).scalar_one()
        order = budgets.LockOrder()
        quota, root, _ancestors = await children._lock_lineage(session, identity, order)
        run = root if root.id == self.run_id else await budgets.lock_run(
            session, self.run_id, order=order, lock_class="child"
        )
        return quota, root, run

    def _actual_credit(self, run: ChatRun, endpoint: str, usage: dict[str, Any] | None,
                       result: dict[str, Any] | None) -> Decimal | None:
        if endpoint == "tool":
            records = result.get("usage", []) if isinstance(result, dict) else []
            if not isinstance(records, list):
                raise DurableRunError("tool usage payload is invalid")
            if not records and isinstance(result, dict) and (
                result.get("status") == "failed"
                or result.get("tool_name") in {"managed_web_search", "managed_web_fetch", "managed_advisor"}
            ):
                return None
            cost, _ = _managed_usage_components(records, pricing_snapshot=run.pricing_snapshot, model_name=run.model_name)
        else:
            if not isinstance(usage, dict) or usage.get("_durable_estimated") is True:
                return None
            breakdown = _usage_payload_breakdown(usage)
            pricing = run.pricing_snapshot
            if endpoint == "context_compaction":
                summary = pricing.get("summary_route") if isinstance(pricing, dict) else None
                if isinstance(summary, dict):
                    pricing = summary
            cost = credit.usage_cost_from_pricing_snapshot(
                pricing, prompt_tokens=breakdown.input_tokens, completion_tokens=breakdown.output_tokens,
                breakdown=breakdown,
            ).raw_cost
        snapshot = run.pricing_snapshot
        return credit.credits_for_cost(cost, snapshot["margin_multiplier"], snapshot["chat_credit_per_usd"])

    async def _start(
        self,
        *,
        segment_id: str,
        ordinal: int,
        endpoint: str,
        turn_ordinal: int,
        call_id: str | None = None,
        journal_started: tuple[str, dict[str, Any]] | None = None,
        credit_bound: Callable[[ChatRun], Decimal] | None = None,
    ) -> dict[str, Any] | None:
        factory = _factory()
        async with factory() as session, session.begin():
            if self.requires_credit_reservation:
                quota, root, run = await self._credit_lineage(session)
            else:
                run = (await session.execute(select(ChatRun).where(ChatRun.id == self.run_id).with_for_update())).scalar_one()
            _require_owned_running_lease(run, self.owner)
            if endpoint == "chat_completions":
                await self._ensure_provider_turn(session, run, turn_ordinal)
            segment = await prepare_segment(
                session,
                run,
                segment_id=segment_id,
                ordinal=ordinal,
                endpoint=endpoint,
                turn_ordinal=turn_ordinal,
                call_id=call_id,
            )
            if segment.status == "completed":
                payload = load_segment_payload(segment.result_payload)
                if payload is None:
                    raise DurableRunError("completed segment is missing its replay payload")
                usage = load_segment_payload(segment.usage_payload)
                return {
                    **payload,
                    "_durable_replay": True,
                    "_durable_usage": usage if isinstance(usage, dict) else {},
                }
            if segment.status == "provider_started":
                fail_unresolved_segment(segment, error_code="provider_result_unknown")
                if self.requires_credit_reservation:
                    await budgets.settle_call_credit(session, run=run, root=root, quota=quota, segment=segment, actual=None)
                return {"_boundary_abort": "provider_result_unknown"}
            if segment.status == "failed":
                return {"_boundary_abort": "provider_result_unknown"}
            if self.requires_credit_reservation:
                if credit_bound is None:
                    raise DurableRunError("v2 call is missing its bounded credit estimate")
                await budgets.reserve_call_credit(
                    session, run=run, root=root, quota=quota, segment=segment, amount=credit_bound(run)
                )
            begin_segment_io(segment)
            if journal_started is not None:
                event_type, payload = journal_started
                event = await append_event(session, run, _event(run, event_type, payload))
                segment.started_event_seq = event.seq
            return None

    async def _complete(
        self,
        *,
        segment_id: str,
        ordinal: int,
        endpoint: str,
        turn_ordinal: int,
        result_payload: dict[str, Any] | None,
        usage_payload: dict[str, Any] | None = None,
        call_id: str | None = None,
        journal_completed: tuple[str, dict[str, Any]] | None = None,
        output_asset_ids: list[str] | None = None,
    ) -> None:
        factory = _factory()
        async with factory() as session, session.begin():
            if self.requires_credit_reservation:
                quota, root, run = await self._credit_lineage(session)
            else:
                run = (await session.execute(select(ChatRun).where(ChatRun.id == self.run_id).with_for_update())).scalar_one()
            _require_owned_running_lease(run, self.owner)
            segment = await prepare_segment(
                session,
                run,
                segment_id=segment_id,
                ordinal=ordinal,
                endpoint=endpoint,
                turn_ordinal=turn_ordinal,
                call_id=call_id,
            )
            if segment.status == "completed":
                return
            if output_asset_ids:
                await assets.link_output_assets_in_transaction(
                    session,
                    run=run,
                    asset_ids=output_asset_ids,
                )
            if self.requires_credit_reservation:
                await budgets.settle_call_credit(
                    session, run=run, root=root, quota=quota, segment=segment,
                    actual=self._actual_credit(run, endpoint, usage_payload, result_payload),
                )
            complete_segment_io(segment, result_payload=result_payload, usage_payload=usage_payload)
            if journal_completed is not None:
                event_type, payload = journal_completed
                event = await append_event(session, run, _event(run, event_type, payload))
                segment.completed_event_seq = event.seq

    async def _fail(
        self,
        *,
        segment_id: str,
        ordinal: int,
        endpoint: str,
        turn_ordinal: int,
        call_id: str | None = None,
    ) -> dict[str, str] | None:
        factory = _factory()
        async with factory() as session, session.begin():
            if self.requires_credit_reservation:
                quota, root, run = await self._credit_lineage(session)
            else:
                run = (await session.execute(select(ChatRun).where(ChatRun.id == self.run_id).with_for_update())).scalar_one()
            _require_owned_running_lease(run, self.owner)
            segment = await prepare_segment(
                session,
                run,
                segment_id=segment_id,
                ordinal=ordinal,
                endpoint=endpoint,
                turn_ordinal=turn_ordinal,
                call_id=call_id,
            )
            if segment.status == "provider_started":
                fail_unresolved_segment(segment, error_code="provider_result_unknown")
                if self.requires_credit_reservation:
                    await budgets.settle_call_credit(session, run=run, root=root, quota=quota, segment=segment, actual=None)
            return {"_boundary_abort": "provider_result_unknown"}

    async def provider_started(self, *, round_index: int, attempt: int,
                               messages: list[dict] | None = None, tool_schemas: list[dict] | None = None,
                               max_tokens: int | None = None) -> dict[str, Any] | None:
        return await self._start(
            segment_id=f"provider:{round_index}:{attempt}",
            ordinal=round_index * 1_000 + attempt,
            endpoint="chat_completions",
            turn_ordinal=round_index,
            credit_bound=(lambda run: _provider_credit_bound(
                run, messages=messages or [], tool_schemas=tool_schemas or [], max_tokens=max_tokens,
            )) if self.requires_credit_reservation else None,
        )

    async def provider_completed(
        self,
        *,
        round_index: int,
        attempt: int,
        usage: dict[str, Any],
        result_payload: dict[str, Any],
    ) -> None:
        await self._complete(
            segment_id=f"provider:{round_index}:{attempt}",
            ordinal=round_index * 1_000 + attempt,
            endpoint="chat_completions",
            turn_ordinal=round_index,
            result_payload=result_payload,
            usage_payload=usage,
        )

    async def provider_failed(self, *, round_index: int, attempt: int) -> dict[str, str]:
        return await self._fail(
            segment_id=f"provider:{round_index}:{attempt}",
            ordinal=round_index * 1_000 + attempt,
            endpoint="chat_completions",
            turn_ordinal=round_index,
        )

    async def tool_started(
        self,
        *,
        round_index: int,
        tool_index: int,
        tool_call_id: str,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        source: str = "builtin",
        category: str = "기본 도구",
    ) -> dict[str, Any] | None:
        return await self._start(
            segment_id=f"tool:{round_index}:{tool_index}",
            ordinal=round_index * 1_000 + 500 + tool_index,
            endpoint="tool",
            turn_ordinal=round_index,
            call_id=tool_call_id,
            credit_bound=(lambda run: _tool_credit_bound(run, tool_name)) if self.requires_credit_reservation else None,
            journal_started=(
                "tool.call.started",
                {
                    "call_id": tool_call_id,
                    "name": tool_name,
                    "arguments": arguments if isinstance(arguments, dict) else {},
                    "source": source,
                    "category": category,
                },
            ),
        )

    async def managed_tool_allowed(self, *, tool_name: str, maximum: int) -> bool:
        """Reserve a managed-use slot from journaled starts, including crashed calls."""
        if maximum < 1:
            return False
        factory = _factory()
        async with factory() as session:
            run = (await session.execute(select(ChatRun).where(ChatRun.id == self.run_id))).scalar_one()
            used = 0
            for event in await replay_events(session, run, after_seq=0):
                if event.type != "tool.call.started":
                    continue
                payload = event.payload.model_dump()
                if payload.get("name") == tool_name:
                    used += 1
            # The current call has already committed its started boundary.
            return used <= maximum

    async def tool_completed(
        self,
        *,
        round_index: int,
        tool_index: int,
        tool_call_id: str,
        tool_name: str,
        result_payload: dict[str, Any],
        source: str = "builtin",
        category: str = "기본 도구",
    ) -> None:
        content = result_payload.get("content")
        if not isinstance(content, str):
            raise DurableRunError("tool result payload is missing text content")
        visible = result_payload.get("visible") is not False
        status = _tool_result_status(result_payload.get("status"))
        error_code = _tool_result_error_code(result_payload.get("error_code"))
        display_parts = _tool_display_parts(result_payload, content=content, visible=visible)
        raw_artifacts = result_payload.get("artifacts")
        output_asset_ids = (
            [
                artifact["asset_id"]
                for artifact in raw_artifacts
                if isinstance(artifact, dict) and isinstance(artifact.get("asset_id"), str)
            ]
            if isinstance(raw_artifacts, list)
            else []
        )
        await self._complete(
            segment_id=f"tool:{round_index}:{tool_index}",
            ordinal=round_index * 1_000 + 500 + tool_index,
            endpoint="tool",
            turn_ordinal=round_index,
            call_id=tool_call_id,
            result_payload=result_payload,
            journal_completed=(
                "tool.call.completed",
                {
                    "call_id": tool_call_id,
                    "name": tool_name,
                    "content": display_parts,
                    "status": status,
                    "source": source,
                    "category": category,
                    "error_code": error_code,
                },
            ),
            output_asset_ids=output_asset_ids,
        )

    async def delegations_prepared(self, *, segment_id: str, calls: list[Any]) -> dict[str, Any]:
        """Create or recover the wait group for one model response's delegation calls."""
        return await children.prepare_delegations(
            parent_run_id=self.run_id, owner=self.owner, model_segment_id=segment_id, calls=list(calls)
        )

    async def tool_failed(
        self, *, round_index: int, tool_index: int, tool_call_id: str, tool_name: str
    ) -> dict[str, str]:
        return await self._fail(
            segment_id=f"tool:{round_index}:{tool_index}",
            ordinal=round_index * 1_000 + 500 + tool_index,
            endpoint="tool",
            turn_ordinal=round_index,
            call_id=tool_call_id,
        )

    async def replay_turn_projection(self) -> dict[str, Any]:
        """Read the committed display projection for the active durable turn."""
        if self.active_turn_ordinal is None:
            raise DurableRunError("active durable turn is not set")
        factory = _factory()
        async with factory() as session:
            run = (await session.execute(select(ChatRun).where(ChatRun.id == self.run_id))).scalar_one()
            turn = (
                await session.execute(
                    select(ChatRunTurn).where(
                        ChatRunTurn.run_id == self.run_id,
                        ChatRunTurn.ordinal == self.active_turn_ordinal,
                    )
                )
            ).scalar_one_or_none()
            if turn is None:
                raise DurableRunError("active durable turn is missing")
            message_id = (
                str(turn.assistant_message_id)
                if turn.assistant_message_id is not None
                else f"temp:{run.id}:assistant:{turn.ordinal}"
            )
            deltas: dict[str, list[str]] = {"text": [], "reasoning": []}
            for event in await replay_events(session, run, after_seq=turn.message_event_seq or 0):
                if event.type != "part.delta":
                    continue
                payload = event.payload.model_dump()
                if payload.get("message_id") != message_id:
                    continue
                part_type = payload.get("part_type")
                delta = payload.get("delta")
                if part_type in deltas and isinstance(delta, str):
                    deltas[part_type].append(delta)
            return {
                "text": "".join(deltas["text"]),
                "reasoning": "".join(deltas["reasoning"]),
                "completed": turn.completion_event_seq is not None,
            }


def _usage_payload_breakdown(value: dict[str, Any]) -> UsageBreakdown:
    """Read a journaled usage dict; entries written before cache accounting carry no split."""
    return UsageBreakdown.from_totals(
        max(0, int(value.get("prompt_tokens") or 0)),
        max(0, int(value.get("completion_tokens") or 0)),
        **{key: value.get(key) or 0 for key in CACHE_USAGE_KEYS},
    )


# (usage kind, breakdown token field, UsageCost cost field). ``input_tokens`` is
# the UNCACHED quantity, matching Anthropic where input_tokens excludes cache.
_TOKEN_USAGE_KINDS = (
    ("input_tokens", "uncached_input_tokens", "input_cost"),
    ("output_tokens", "output_tokens", "output_cost"),
    ("cache_read_input_tokens", "cache_read_input_tokens", "cache_read_cost"),
    ("cache_creation_5m_input_tokens", "cache_creation_5m_input_tokens", "cache_creation_5m_cost"),
    ("cache_creation_1h_input_tokens", "cache_creation_1h_input_tokens", "cache_creation_1h_cost"),
)


def _token_usage_components(
    breakdown: UsageBreakdown,
    usage_cost: UsageCost,
    *,
    segment_id: str,
    source: str,
    model_name: str,
    metadata: dict[str, Any],
) -> list[dict[str, Any]]:
    """Per-category token components; cache categories appear only with tokens."""
    components: list[dict[str, Any]] = []
    for kind, token_field, cost_field in _TOKEN_USAGE_KINDS:
        quantity = getattr(breakdown, token_field)
        if kind.startswith("cache_") and quantity == 0:
            continue
        cost = getattr(usage_cost, cost_field)
        unit_price = cost / quantity if quantity else Decimal("0")
        components.append(
            {
                "segment_id": segment_id,
                "kind": kind,
                "quantity": str(quantity),
                "unit": "token",
                "unit_price_usd": format(unit_price, "f"),
                "cost_usd": format(cost, "f"),
                "source": source,
                "model_name": model_name,
                "metadata": dict(metadata),
            }
        )
    return components


_MANAGED_USAGE_KEYS = {
    ("web_search_requests", "web_search_request_per_unit", "request", "search"),
    ("web_search_context", "web_search_context_low_per_unit", "context", "search"),
    ("web_search_context", "web_search_context_medium_per_unit", "context", "search"),
    ("web_search_context", "web_search_context_high_per_unit", "context", "search"),
    ("web_fetch_requests", "web_fetch_request_per_unit", "request", "fetch"),
    ("web_fetch_context", "web_fetch_context_per_unit", "context", "fetch"),
    ("advisor_input_tokens", "advisor_input_price_per_token", "token", "advisor"),
    ("advisor_output_tokens", "advisor_output_price_per_token", "token", "advisor"),
    ("advisor_cache_read_tokens", "advisor_cache_read_price_per_token", "token", "advisor"),
    ("advisor_cache_creation_5m_tokens", "advisor_cache_write_price_per_token", "token", "advisor"),
    ("advisor_cache_creation_1h_tokens", "advisor_cache_write_1h_price_per_token", "token", "advisor"),
}
# Advisor cache rates come from the frozen resolved route (manual or exact
# direct-provider catalog); a missing category stays unpriced.
_OPTIONAL_MANAGED_PRICE_KEYS = frozenset(
    {
        "advisor_cache_read_price_per_token",
        "advisor_cache_write_price_per_token",
        "advisor_cache_write_1h_price_per_token",
    }
)


def _managed_usage_components(
    records: list[dict[str, Any]],
    *,
    pricing_snapshot: dict[str, Any],
    model_name: str,
) -> tuple[Decimal, list[dict[str, Any]]]:
    """Price worker-authored managed tool records from the immutable run snapshot."""
    prices = pricing_snapshot.get("component_prices")
    if not isinstance(prices, dict):
        raise DurableRunError("managed tool pricing snapshot is missing")
    total = Decimal("0")
    components: list[dict[str, Any]] = []
    for index, record in enumerate(records):
        kind = record.get("kind")
        price_key = record.get("price_key")
        unit = record.get("unit")
        source = record.get("source")
        if (kind, price_key, unit, source) not in _MANAGED_USAGE_KEYS:
            raise DurableRunError("managed tool usage record is invalid")
        unpriced = price_key in _OPTIONAL_MANAGED_PRICE_KEYS and prices.get(price_key) is None
        try:
            quantity = Decimal(str(record.get("quantity")))
            unit_price = Decimal("0") if unpriced else Decimal(str(prices[price_key]))
        except (InvalidOperation, KeyError, TypeError, ValueError) as exc:
            raise DurableRunError("managed tool pricing snapshot is invalid") from exc
        above_price_key = f"{price_key}_above_200k"
        if price_key in _OPTIONAL_MANAGED_PRICE_KEYS and prices.get(above_price_key) is not None:
            prompt_tokens = record.get("prompt_tokens")
            if type(prompt_tokens) is not int or prompt_tokens < 0 or prompt_tokens > 10_000_000:
                raise DurableRunError("managed advisor cache prompt size is invalid")
            if prompt_tokens > 200_000 and not unpriced:
                try:
                    unit_price = Decimal(str(prices[above_price_key]))
                except (InvalidOperation, TypeError, ValueError) as exc:
                    raise DurableRunError("managed tool pricing snapshot is invalid") from exc
        is_advisor = source == "advisor"
        if (
            not quantity.is_finite()
            or quantity < 0
            or (is_advisor and quantity != quantity.to_integral_value())
            or (not is_advisor and quantity != 1)
            or (is_advisor and quantity > 1_000_000)
            or not unit_price.is_finite()
            or unit_price < 0
        ):
            raise DurableRunError("managed tool usage record is invalid")
        cost = (quantity * unit_price).quantize(Decimal("0.0000000001"), rounding=ROUND_HALF_EVEN)
        total += cost
        components.append(
            {
                "segment_id": f"{source}:managed:{index}",
                "kind": kind,
                "quantity": format(quantity, "f"),
                "unit": unit,
                "unit_price_usd": format(unit_price, "f"),
                "cost_usd": format(cost, "f"),
                "source": source,
                "model_name": record.get("model_name") if isinstance(record.get("model_name"), str) else model_name,
                "metadata": {"unpriced": True} if unpriced and quantity > 0 else {},
            }
        )
    return total.quantize(Decimal("0.0000000001"), rounding=ROUND_HALF_EVEN), components


async def _managed_tool_configs(
    payload: dict[str, Any], capability_snapshot: dict[str, Any]
) -> tuple[dict | None, dict | None, dict | None]:
    """Resolve non-secret snapshots into worker-only managed tool configs."""
    features = payload.get("features")
    if not isinstance(features, dict):
        return None, None, None
    search_config: dict | None = None
    fetch_config: dict | None = None
    advisor_config: dict | None = None
    routes = capability_snapshot.get("feature_routes")
    routes = routes if isinstance(routes, dict) else {}
    search_options = features.get("web_search")
    if (
        isinstance(search_options, dict)
        and search_options.get("enabled") is True
        and search_options.get("mode", "managed") == "managed"
    ):
        route_snapshot = routes.get("search")
        if not isinstance(route_snapshot, dict):
            raise DurableRunError("managed search route snapshot is missing")
        route = await ps.resolve_provider_snapshot(route_snapshot)
        if route is None:
            raise DurableRunError("managed search provider configuration is unavailable")
        search_config = {"route": route, "options": search_options}
    fetch_options = features.get("web_fetch")
    if isinstance(fetch_options, dict) and fetch_options.get("enabled") is True:
        fetch_config = fetch_options
    advisor_options = features.get("advisor")
    if isinstance(advisor_options, dict) and advisor_options.get("enabled") is True:
        route_snapshot = routes.get("advisor")
        if not isinstance(route_snapshot, dict):
            raise DurableRunError("managed advisor route snapshot is missing")
        route = await ps.resolve_model_snapshot(route_snapshot)
        if route is None:
            raise DurableRunError("managed advisor model configuration is unavailable")
        advisor_config = {"route": route, "options": advisor_options}
    return search_config, fetch_config, advisor_config


def _native_web_search_options(capability_snapshot: dict[str, Any]) -> dict[str, Any] | None:
    """Read only frozen native-search options; managed routes are never inferred here."""
    features = capability_snapshot.get("effective_features")
    options = features.get("web_search") if isinstance(features, dict) else None
    if not isinstance(options, dict) or options.get("enabled") is not True or options.get("mode") != "native":
        return None
    if options.get("provider_id") is not None:
        raise DurableRunError("native web search must not contain a provider route")
    context_size = options.get("context_size")
    if context_size not in {"low", "medium", "high"}:
        raise DurableRunError("native web search context size is invalid")
    native_options: dict[str, Any] = {"search_context_size": context_size}
    location = options.get("approximate_location")
    if location is not None:
        if not isinstance(location, dict) or set(location) != {"city", "country", "region", "timezone"}:
            raise DurableRunError("native web search location is invalid")
        native_options["user_location"] = {"type": "approximate", "approximate": dict(location)}
    return native_options


def _route_context_limit(capability_snapshot: dict[str, Any]) -> object:
    """Read the frozen route window using the same accessor as the context fence."""
    route_capabilities = capability_snapshot.get("capabilities")
    if not isinstance(route_capabilities, dict):
        route_capabilities = capability_snapshot
    return route_capabilities.get("context_limit")


def _native_compaction_options(capability_snapshot: dict[str, Any], resolved: dict[str, Any]) -> dict[str, Any] | None:
    """Resolve provider-native compaction for this route, or None when unavailable.

    This layers under, not over, the Lumen context fence in ``prepare_context``.
    That fence measures against ``input_budget`` (window minus output and safety
    reserves) and so trips at a lower absolute token count than a native trigger
    resolved against the raw window, which keeps Lumen's durable summary the
    primary mechanism.  The native edit only covers what the fence structurally
    cannot: growth inside a single provider request, and rounds where the
    summary route is unavailable and the fence skips itself.
    """
    if not get_settings().chat_native_compaction_enabled:
        return None
    return native_compaction.compaction_options(
        provider_type=resolved.get("provider_type"),
        context_limit=_route_context_limit(capability_snapshot),
        ratio=context_manager.COMPACTION_REQUIRED,
    )


async def execute_queued_run(run_id: str, *, owner: str, registration_id: str | None = None) -> bool:
    """Claim one queued run and stream its normalized engine output into the journal."""
    factory = _factory()
    async with factory() as session, session.begin():
        run = await claim_queued_run(session, run_id, owner=owner, registration_id=registration_id)
        if run is None:
            return False
        # From here on every journal write proves the claim fence, not just the worker identity.
        owner = run.lease_owner
        payload = _payload(run)
        model_name = run.model_name
        project_id = run.project_id
        user_id = run.user_id
        conversation_id = run.conversation_id
        parent_id = run.user_message_id
        capability_snapshot = run.capability_snapshot
        pricing_snapshot = run.pricing_snapshot
        existing_message_id = run.assistant_message_id

    if getattr(run, "run_kind", "completion") == "compaction":
        input_messages = [dict(m) for m in (payload.get("input_messages") or [])]
        raw_tool_schemas = payload.get("tool_schemas")
        if not isinstance(raw_tool_schemas, list) or not all(isinstance(schema, dict) for schema in raw_tool_schemas):
            raise DurableRunError("context compaction tool schema snapshot is unavailable")
        tool_schemas = [dict(schema) for schema in raw_tool_schemas]
        # Compaction is a real durable run: keep renewing its lease and honor
        # cancellation while map/reduce provider calls are in flight.
        heartbeat_stop = asyncio.Event()
        lease_lost = asyncio.Event()

        async def heartbeat() -> None:
            while True:
                try:
                    await asyncio.wait_for(heartbeat_stop.wait(), timeout=10)
                    return
                except TimeoutError:
                    try:
                        renewed = await _renew_lease(run_id, owner)
                    except Exception:
                        logger.exception("durable compaction lease renewal failed run_id=%s owner=%s", run_id, owner)
                        renewed = False
                    if not renewed:
                        lease_lost.set()
                        return

        heartbeat_task = asyncio.create_task(heartbeat())
        hooks = _DurableExecutionHooks(run_id=run_id, owner=owner)
        hooks.force_compaction = True
        try:
            await _set_stage(run_id, "model_request", owner=owner)
            await hooks.prepare_context(messages=input_messages, tool_schemas=tool_schemas, round_index=0)
            if lease_lost.is_set():
                raise DurableRunLeaseLost(f"lease lost for {run_id}")
            if await _cancel_requested(run_id):
                await _finish(
                    run_id,
                    status="canceled",
                    message_id=None,
                    owner=owner,
                    error_code="canceled",
                    safe_message="chat run canceled",
                )
            else:
                await _finish(run_id, status="completed", message_id=None, owner=owner)
        except DurableRunLeaseLost:
            logger.warning("stopped stale durable compaction run_id=%s owner=%s", run_id, owner)
        except asyncio.CancelledError:
            raise
        except context_manager.ContextLimitExceeded as exc:
            logger.warning(
                "durable compaction rejected run_id=%s code=%s",
                run_id,
                getattr(exc, "code", "compaction_failed"),
            )
            if await _cancel_requested(run_id):
                await _finish(
                    run_id,
                    status="canceled",
                    message_id=None,
                    owner=owner,
                    error_code="canceled",
                    safe_message="chat run canceled",
                )
            else:
                await _finish(
                    run_id,
                    status="failed",
                    message_id=None,
                    owner=owner,
                    error_code=getattr(exc, "code", "compaction_failed"),
                    safe_message="컨텍스트 압축을 완료하지 못했습니다",
                )
        except Exception:
            logger.exception("manual compaction failed run_id=%s", run_id)
            if await _cancel_requested(run_id):
                await _finish(
                    run_id,
                    status="canceled",
                    message_id=None,
                    owner=owner,
                    error_code="canceled",
                    safe_message="chat run canceled",
                )
            else:
                await _finish(
                    run_id,
                    status="failed",
                    message_id=None,
                    owner=owner,
                    error_code="compaction_failed",
                    safe_message="컨텍스트 압축을 완료하지 못했습니다",
                )
        finally:
            heartbeat_stop.set()
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
        return True

    if conversation_id is not None and existing_message_id is not None:
        # The provider boundary selects the replay turn; never route it through
        # the run's latest assistant message before that boundary runs.
        message_id: str | None = None
    elif conversation_id is not None:
        async with factory() as session, session.begin():
            run = (await session.execute(select(ChatRun).where(ChatRun.id == run_id).with_for_update())).scalar_one()
            _require_owned_running_lease(run, owner)
            created_at, created_at_local, created_timezone = _message_timestamps_for_run(run)
            placeholder = ChatMessage(
                conversation_id=conversation_id,
                role="assistant",
                parent_id=parent_id,
                content=encrypt_chat_content(""),
                status="streaming",
                model_name=model_name,
                created_at=created_at,
                created_at_local=created_at_local,
                created_timezone=created_timezone,
            )
            session.add(placeholder)
            await session.flush()
            message_id = str(placeholder.id)
            run.assistant_message_id = placeholder.id
            run.current_ordinal = 0
            turn = ChatRunTurn(run_id=run.id, ordinal=0, assistant_message_id=placeholder.id)
            session.add(turn)
            event = await append_event(
                session,
                run,
                _event(
                    run,
                    "message.created",
                    {
                        "message_id": message_id,
                        "role": "assistant",
                        "parent_id": str(parent_id) if parent_id is not None else None,
                    },
                ),
            )
            turn.message_event_seq = event.seq
    else:
        # Temporary threads have no ChatMessage row, but their replay stream still
        # needs a stable message identity for typed part events.
        message_id = f"temp:{run_id}"
        await _append(
            run_id,
            "message.created",
            {"message_id": message_id, "role": "assistant", "parent_id": None},
            owner=owner,
        )

    citations: list[dict[str, Any]] = []
    tool_invocations: dict[str, dict[str, Any]] = {}
    tool_results: dict[str, dict[str, Any]] = {}
    usage_breakdown = UsageBreakdown(0, 0)
    usage: dict[str, int] = usage_breakdown.as_usage_dict()
    managed_tool_usage: list[dict[str, Any]] = []

    text: list[str] = []
    reasoning: list[str] = []
    pending_deltas: dict[str, list[str]] = {"text": [], "reasoning": []}
    pending_first_at: float | None = None

    async def flush_deltas(*, check_cancel: bool = True) -> bool:
        """Persist bounded delta batches without stalling upstream chunk consumption."""
        if lease_lost.is_set():
            raise DurableRunLeaseLost(f"lease lost for {run_id}")
        nonlocal pending_first_at
        if check_cancel and await _cancel_requested(run_id):
            await _finish(
                run_id,
                status="canceled",
                message_id=message_id,
                owner=owner,
                error_code="canceled",
                safe_message="chat run canceled",
            )
            return True
        if not pending_deltas["text"] and not pending_deltas["reasoning"]:
            return False
        for part_index, part_type, destination in (
            (0, "text", text),
            (1, "reasoning", reasoning),
        ):
            chunks = pending_deltas[part_type]
            if not chunks:
                continue
            delta = "".join(chunks)
            chunks.clear()
            destination.append(delta)
            await _append(
                run_id,
                "part.delta",
                {"message_id": message_id, "part_index": part_index, "part_type": part_type, "delta": delta},
                owner=owner,
            )
        pending_first_at = None
        return False

    async def finalize_current_turn() -> None:
        if conversation_id is None or message_id is None:
            return
        canonical_parts: list[dict[str, Any]] = []
        content = "".join(text)
        visible_reasoning = "".join(reasoning)
        if content:
            canonical_parts.append({"type": "text", "text": content})
        if visible_reasoning:
            canonical_parts.append({"type": "reasoning", "text": visible_reasoning, "visibility": "user"})
        canonical_parts.extend(tool_invocations.values())
        canonical_parts.extend(tool_results.values())
        parts = list(enumerate(canonical_parts))
        async with factory() as session, session.begin():
            run = (await session.execute(select(ChatRun).where(ChatRun.id == run_id).with_for_update())).scalar_one()
            _require_owned_running_lease(run, owner)
            if execution_hooks.active_turn_ordinal is None:
                raise DurableRunError("finalized durable turn is not set")
            turn = (
                await session.execute(
                    select(ChatRunTurn)
                    .where(
                        ChatRunTurn.run_id == run.id,
                        ChatRunTurn.ordinal == execution_hooks.active_turn_ordinal,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if turn is None or turn.assistant_message_id != int(message_id):
                raise DurableRunError("finalized durable turn does not match its assistant message")
            if turn.completion_event_seq is not None:
                return
            completion_event_seq = 0
            for part_index, part in parts:
                event = await append_event(
                    session,
                    run,
                    _event(run, "part.completed", {"message_id": message_id, "part_index": part_index, "part": part}),
                )
                completion_event_seq = event.seq
            await cs.complete_message_in_transaction(
                session,
                conversation_id,
                message_id=int(message_id),
                content=content,
                reasoning=visible_reasoning or None,
                model_name=model_name,
                canonical_parts=canonical_parts,
            )
            turn.completion_event_seq = completion_event_seq

    heartbeat_stop = asyncio.Event()
    lease_lost = asyncio.Event()

    async def heartbeat() -> None:
        while True:
            try:
                await asyncio.wait_for(heartbeat_stop.wait(), timeout=10)
                return
            except TimeoutError:
                try:
                    renewed = await _renew_lease(run_id, owner)
                except Exception:
                    logger.exception("durable chat lease renewal failed run_id=%s owner=%s", run_id, owner)
                    renewed = False
                if not renewed:
                    lease_lost.set()
                    logger.warning("durable chat run lease was lost run_id=%s owner=%s", run_id, owner)
                    return

    heartbeat_task = asyncio.create_task(heartbeat())

    def ensure_lease() -> None:
        if lease_lost.is_set():
            raise DurableRunLeaseLost(f"lease lost for {run_id}")

    response_writing = False
    engine_iterator = None
    next_event_task = None
    lease_lost_wait_task = None
    deadline_task = None

    async def mark_response_writing() -> None:
        nonlocal response_writing
        if not response_writing:
            await _set_stage(run_id, "response_writing", owner=owner)
            response_writing = True

    try:
        if not _validate_run_protocol_payload(run, payload):
            raise DurableRunProtocolMismatch("run execution protocol does not match this worker")
        await _set_stage(run_id, "model_request", owner=owner)
        resolved = await ps.resolve_model_snapshot(capability_snapshot)
        if resolved is None:
            raise DurableRunError("requested model configuration is unavailable")
        managed_search, managed_fetch, managed_advisor = await _managed_tool_configs(payload, capability_snapshot)
        native_web_search = _native_web_search_options(capability_snapshot)
        native_compaction_options = _native_compaction_options(capability_snapshot, resolved)
        extension_snapshot = payload.get("extension_snapshot")
        if extension_snapshot is None:
            selected_tool_ids = _selected_tool_ids(payload.get("features"))
            selected_mcp_ids = None
            expected_mcp_credential_versions = None
            expected_extension_fingerprints = None
        else:
            try:
                selected_tool_ids, selected_mcp_ids, warnings = await extensions_store.validated_frozen_selection(
                    extension_snapshot, user_id=user_id, project_id=project_id
                )
            except extensions_store.ChatStorageUnavailable as exc:
                raise DurableRunError("frozen extension configuration could not be verified") from exc
            for warning in warnings:
                await _append(
                    run_id, "run.warning", {"code": "extension_config_changed", "safe_message": warning}, owner=owner
                )
            expected_mcp_credential_versions = _selected_mcp_credential_versions(extension_snapshot, selected_mcp_ids)
            expected_extension_fingerprints = extensions_store.frozen_selection_fingerprints(
                extension_snapshot,
                selected_tool_ids,
                selected_mcp_ids,
            )
        plugin_tool_snapshots = payload.get("plugin_tool_snapshots") or []
        skill_snapshot = payload.get("skill_snapshot") or []
        if not isinstance(plugin_tool_snapshots, list) or not isinstance(skill_snapshot, list):
            raise DurableRunInputError("frozen plugin selection is invalid")
        # Graph checks frozen skill authority before each provider model turn;
        # installed tool bindings are also checked before constructing bindings.
        for snapshot in plugin_tool_snapshots:
            if not isinstance(snapshot, dict):
                raise DurableRunInputError("frozen plugin selection is invalid")
            await plugin_bindings.revalidate(snapshot, namespace=Namespace(user_id=user_id, project_id=project_id))
        input_messages = [dict(message) for message in payload["input_messages"]]
        input_parts = payload.get("input_parts")
        if isinstance(input_parts, list) and any(
            part.get("type") != "text" for part in input_parts if isinstance(part, dict)
        ):
            document_gate = ((resolved.get("capabilities") or {}).get("feature_gates") or {}).get("document_input")
            try:
                materialized = await assets.provider_content_for_input_parts(
                    input_parts,
                    user_id=user_id,
                    project_id=project_id,
                    allow_document=bool(isinstance(document_gate, dict) and document_gate.get("available")),
                )
            except assets.AssetError as exc:
                raise DurableRunInputError(str(exc)) from exc
            for message in reversed(input_messages):
                if message.get("role") == "user":
                    message["content"] = materialized
                    break
        awaiting_model_response = True
        pending_tool_results: int | None = None
        execution_hooks = _DurableExecutionHooks(
            run_id=run_id, owner=owner, credit_bounded=run.execution_protocol_version == 2,
            skill_snapshots=skill_snapshot, user_id=user_id, project_id=project_id,
        )
        feature_options = payload.get("features")
        tool_policy = feature_options.get("tool_policy") if isinstance(feature_options, dict) else None
        approval_mode = tool_policy.get("approval_mode") if isinstance(tool_policy, dict) else None
        workspace_write_mode = (
            tool_policy.get("workspace_write_mode")
            if isinstance(tool_policy, dict) and tool_policy.get("workspace_write_mode") in {"ask", "auto_edit"}
            else "ask"
        )
        resume = await _v2_resume(run_id=run_id, owner=owner) if run.execution_protocol_version == 2 else None
        delegation_enabled = _delegation_enabled(run, payload)
        sandbox_lease = None
        if run.execution_protocol_version == 2 and run.assigned_resource_id is not None:
            try:
                sandbox_lease = (owner, int(str(owner).rsplit("#", 1)[1]))
            except (IndexError, ValueError):
                raise DurableRunError("lease owner token is not fenced") from None
        hydrated_turn_ordinal: int | None = None

        async def hydrate_replayed_turn() -> None:
            nonlocal hydrated_turn_ordinal, text, reasoning, pending_deltas, pending_first_at
            if execution_hooks.active_turn_ordinal == hydrated_turn_ordinal:
                return
            projection = await execution_hooks.replay_turn_projection()
            text = [projection["text"]] if projection["text"] else []
            reasoning = [projection["reasoning"]] if projection["reasoning"] else []
            pending_deltas = {"text": [], "reasoning": []}
            pending_first_at = None
            hydrated_turn_ordinal = execution_hooks.active_turn_ordinal

        await credit.precheck(user_id, project_id, api_key_id=getattr(run, "api_key_id", None))
        engine_iterator = engine.stream(
            model=resolved["model_name"],
            max_tool_calls=int(payload.get("v2_max_tool_calls", _V2_DEFAULT_MAX_TOOL_CALLS)),
            max_model_turns=int(payload.get("v2_max_model_turns", _V2_DEFAULT_MAX_MODEL_TURNS)),
            allowed_direct_effects=tuple(payload.get("allowed_direct_effects") or ()),
            messages=input_messages,
            project_id=project_id,
            user_id=user_id,
            custom_llm_provider=resolved.get("provider_type"),
            api_base=resolved.get("api_base"),
            api_key=resolved.get("api_key"),
            provider_auth=resolved.get("provider_auth"),
            max_tokens=payload.get("max_tokens"),
            temperature=payload.get("temperature"),
            reasoning_effort=payload.get("reasoning_effort"),
            response_format=_provider_response_format(payload.get("features")),
            selected_tool_ids=selected_tool_ids,
            selected_mcp_ids=selected_mcp_ids,
            expected_mcp_credential_versions=expected_mcp_credential_versions,
            expected_extension_fingerprints=expected_extension_fingerprints,
            tools_enabled=_tools_enabled(payload.get("features")),
            managed_search=managed_search,
            managed_fetch=managed_fetch,
            managed_advisor=managed_advisor,
            native_web_search=native_web_search,
            native_compaction_options=native_compaction_options,
            run_id=run_id,
            execution_hooks=execution_hooks,
            execution_protocol_version=run.execution_protocol_version,
            lumen_snapshot=payload.get("lumen_snapshot") if isinstance(payload.get("lumen_snapshot"), dict) else None,
            lumen_snapshot_frozen=run.execution_protocol_version == 2,
            approval_mode=approval_mode if isinstance(approval_mode, str) else None,
            workspace_write_mode=workspace_write_mode,
            resume=resume,
            plugin_tool_snapshots=tuple(plugin_tool_snapshots),
            delegation_enabled=delegation_enabled,
            sandbox_lease=sandbox_lease,
        ).__aiter__()
        next_event_task = asyncio.create_task(anext(engine_iterator))
        lease_lost_wait_task = asyncio.create_task(lease_lost.wait())
        deadline_task = None
        while True:
            ensure_lease()
            wait_tasks = {next_event_task, lease_lost_wait_task}
            if pending_first_at is not None:
                # Use an explicit timer task instead of asyncio.wait's timeout.
                # This keeps the flush deadline observable even when the
                # iterator is paused inside provider I/O.
                remaining = max(0.0, pending_first_at + 0.05 - monotonic())
                deadline_task = asyncio.create_task(asyncio.sleep(remaining))
                wait_tasks.add(deadline_task)
            done, _ = await asyncio.wait(wait_tasks, return_when=asyncio.FIRST_COMPLETED)
            deadline_fired = deadline_task is not None and deadline_task in done
            if deadline_task is not None and deadline_task not in done:
                deadline_task.cancel()
                await asyncio.gather(deadline_task, return_exceptions=True)
            deadline_task = None
            if lease_lost_wait_task in done:
                raise DurableRunLeaseLost(f"lease lost for {run_id}")
            if deadline_fired and next_event_task not in done:
                if await flush_deltas():
                    return True
                continue
            try:
                event = next_event_task.result()
            except StopAsyncIteration:
                if await flush_deltas():
                    return True
                break
            next_event_task = asyncio.create_task(anext(engine_iterator))
            ensure_lease()
            active_message_id = execution_hooks.active_message_id
            if active_message_id is not None and active_message_id != message_id:
                if message_id is not None:
                    if await flush_deltas():
                        return True
                    await finalize_current_turn()
                message_id = active_message_id
                text = []
                reasoning = []
                pending_deltas = {"text": [], "reasoning": []}
                pending_first_at = None
                hydrated_turn_ordinal = None
                response_writing = False
            is_durable_replay = event.get("_durable_replay") is True
            if is_durable_replay:
                await hydrate_replayed_turn()
            etype = event.get("type")
            if awaiting_model_response:
                await _set_stage(run_id, "model_response", owner=owner)
                awaiting_model_response = False
            if etype == "token" and event.get("text"):
                await mark_response_writing()
                token = str(event["text"])
                if is_durable_replay:
                    existing = "".join(text)
                    if not token.startswith(existing):
                        raise DurableRunError("provider replay does not extend the committed text projection")
                    token = token[len(existing) :]
                if token:
                    if pending_first_at is None:
                        pending_first_at = monotonic()
                    pending_deltas["text"].append(token)
            elif etype == "reasoning" and event.get("text"):
                await mark_response_writing()
                reasoning_delta = str(event["text"])
                if is_durable_replay:
                    existing = "".join(reasoning)
                    if not reasoning_delta.startswith(existing):
                        raise DurableRunError("provider replay does not extend the committed reasoning projection")
                    reasoning_delta = reasoning_delta[len(existing) :]
                if reasoning_delta:
                    if pending_first_at is None:
                        pending_first_at = monotonic()
                    pending_deltas["reasoning"].append(reasoning_delta)
            else:
                if await flush_deltas():
                    return True
                if etype == "assistant_tool_calls":
                    calls = event.get("tool_calls")
                    pending_tool_results = len(calls) if isinstance(calls, list) else None
                elif etype == "input.interrupted":
                    payload_value = event.get("payload")
                    if not isinstance(payload_value, dict) or run.execution_protocol_version != 2:
                        raise DurableRunError("unexpected durable input interrupt")
                    if payload_value.get("kind") == "children":
                        from lumen.services import graph as graph_module

                        checkpointed = await graph_module.pending_interrupt(run_id)
                        wait_group_id = payload_value.get("wait_group_id")
                        if (
                            checkpointed is None
                            or checkpointed.get("kind") != "children"
                            or checkpointed.get("wait_group_id") != wait_group_id
                            or not isinstance(wait_group_id, str)
                        ):
                            raise DurableRunError("children interrupt has no resumable checkpoint")
                        await children.mark_waiting_children(
                            parent_run_id=run_id,
                            owner=owner,
                            wait_group_id=wait_group_id,
                            checkpoint_id=checkpointed.get("checkpoint_id"),
                        )
                        return True
                    await _persist_v2_approval_interrupt(run_id=run_id, owner=owner, calls=payload_value.get("calls"))
                    return True
                elif etype == "tool_call":
                    try:
                        arguments = json.loads(event.get("args") or "{}")
                    except (TypeError, json.JSONDecodeError):
                        arguments = {}
                    tool_name = str(event.get("name") or "tool")
                    await _set_stage(run_id, "tool_execution", owner=owner, tool_name=tool_name)
                    call_id = str(event.get("tool_call_id") or event.get("name"))
                    tool_invocations[call_id] = {
                        "type": "tool_call",
                        "call_id": call_id,
                        "name": tool_name,
                        "arguments": arguments if isinstance(arguments, dict) else {},
                        "status": "running",
                    }
                    if event.get("_durable_journaled") is not True:
                        await _append(
                            run_id,
                            "tool.call.started",
                            {
                                "call_id": str(event.get("tool_call_id") or event.get("name")),
                                "name": tool_name,
                                "arguments": arguments if isinstance(arguments, dict) else {},
                            },
                            owner=owner,
                        )
                elif etype == "tool_result":
                    visible = event.get("hidden") is not True
                    status = _tool_result_status(event.get("status"))
                    error_code = _tool_result_error_code(event.get("error_code"))
                    display_parts = _tool_display_parts(
                        event,
                        content=str(event.get("content") or ""),
                        visible=visible,
                    )
                    if event.get("_durable_journaled") is not True:
                        await _append(
                            run_id,
                            "tool.call.completed",
                            {
                                "call_id": str(event.get("tool_call_id") or event.get("name")),
                                "name": str(event.get("name") or "tool"),
                                "content": display_parts,
                                "status": status,
                                "error_code": error_code,
                            },
                            owner=owner,
                        )
                    call_id = str(event.get("tool_call_id") or event.get("name"))
                    tool_name = str(event.get("name") or "tool")
                    invocation = tool_invocations.setdefault(
                        call_id,
                        {
                            "type": "tool_call",
                            "call_id": call_id,
                            "name": tool_name,
                            "arguments": {},
                            "status": "running",
                        },
                    )
                    invocation["status"] = status
                    if visible:
                        tool_results[call_id] = {
                            "type": "tool_result",
                            "call_id": call_id,
                            "name": tool_name,
                            "content": display_parts,
                            "is_error": status == "failed",
                        }
                    if pending_tool_results is not None:
                        pending_tool_results -= 1
                        if pending_tool_results > 0:
                            continue
                        pending_tool_results = None
                    if message_id is not None and conversation_id is not None:
                        if await flush_deltas():
                            return True
                        await finalize_current_turn()
                        text = []
                        tool_invocations.clear()
                        tool_results.clear()
                        reasoning = []
                        pending_deltas = {"text": [], "reasoning": []}
                        pending_first_at = None
                        message_id = None
                    await _set_stage(run_id, "model_request", owner=owner)
                    awaiting_model_response = True
                    response_writing = False
                elif etype == "warning":
                    if event.get("code") == "advisor_call_failed":
                        await _append(
                            run_id,
                            "run.warning",
                            {"code": "advisor_call_failed", "safe_message": "Advisor request failed."},
                            owner=owner,
                        )
                elif etype == "citations":
                    for item in event.get("items") or []:
                        if not isinstance(item, dict):
                            continue
                        source_kind = item.get("source_kind", "web")
                        url = item.get("url")
                        document_index = item.get("document_index")
                        if source_kind == "web" and not isinstance(url, str):
                            continue
                        if source_kind == "document" and (not isinstance(document_index, int) or document_index < 0):
                            continue
                        citations.append(
                            {
                                "type": "citation",
                                "citation_id": str(
                                    item.get("citation_id")
                                    or (
                                        f"document-{document_index}-{len(citations) + 1}"
                                        if source_kind == "document"
                                        else f"citation-{len(citations) + 1}"
                                    )
                                ),
                                "source_kind": source_kind,
                                **({"url": url} if isinstance(url, str) else {}),
                                **({"document_index": document_index} if isinstance(document_index, int) else {}),
                                **({"title": item["title"]} if isinstance(item.get("title"), str) else {}),
                                **({"snippet": item["snippet"]} if isinstance(item.get("snippet"), str) else {}),
                                **(
                                    {"start_index": item["start_index"]}
                                    if isinstance(item.get("start_index"), int)
                                    else {}
                                ),
                                **({"end_index": item["end_index"]} if isinstance(item.get("end_index"), int) else {}),
                            }
                        )
                elif etype == "usage":
                    value = event.get("usage")
                    if isinstance(value, dict):
                        usage_breakdown = _usage_payload_breakdown(value)
                        usage = usage_breakdown.as_usage_dict()
                    emitted_tool_usage = event.get("tool_usage")
                    if isinstance(emitted_tool_usage, list):
                        managed_tool_usage = [item for item in emitted_tool_usage if isinstance(item, dict)]
                elif etype == "error":
                    if event.get("code") == "provider_result_unknown":
                        raise DurableRunProviderResultUnknown("provider result is indeterminate")
                    raise DurableRunError(str(event.get("message") or "provider error"))

            if (
                sum(len(chunk) for chunks in pending_deltas.values() for chunk in chunks) >= 128
                and await flush_deltas()
            ):
                return True

        if await flush_deltas():
            return True

        raw_text = "".join(text)
        parts: list[dict[str, Any]] = []
        terminal_status = "completed"
        terminal_error_code: str | None = None
        terminal_safe_message: str | None = None
        feature_options = payload.get("features")
        response_format = (
            feature_options.get("response_format", {"kind": "text"})
            if isinstance(feature_options, dict)
            else {"kind": "text"}
        )
        if response_format["kind"] == "text":
            if raw_text:
                parts.append({"type": "text", "text": raw_text})
        else:
            try:
                parts.append(parse_structured_output(raw_text, response_format))
            except StructuredOutputError as exc:
                terminal_status = "failed"
                terminal_error_code = "structured_output_invalid"
                terminal_safe_message = "모델 응답이 요청한 구조화 형식과 일치하지 않습니다"
                if raw_text:
                    parts.append({"type": "text", "text": raw_text})
                parts.append(
                    {
                        "type": "structured",
                        "schema_name": response_format.get("name") or "json_object",
                        "schema_version": response_format.get("version") or "1",
                        "value": None,
                        "valid": False,
                        "validation_error": str(exc),
                    }
                )
        if reasoning:
            parts.append({"type": "reasoning", "text": "".join(reasoning), "visibility": "user"})
        parts.extend(citations)
        completed_parts = list(enumerate(parts, start=1 if parts and parts[0]["type"] == "reasoning" else 0))
        ensure_lease()
        usage_cost = credit.usage_cost_from_pricing_snapshot(
            pricing_snapshot,
            prompt_tokens=usage["prompt_tokens"],
            completion_tokens=usage["completion_tokens"],
            breakdown=usage_breakdown,
        )
        managed_cost, managed_components = _managed_usage_components(
            managed_tool_usage,
            pricing_snapshot=pricing_snapshot,
            model_name=resolved["model_name"],
        )
        if managed_cost:
            usage_cost = replace(
                usage_cost,
                raw_cost=(usage_cost.raw_cost + managed_cost).quantize(
                    Decimal("0.0000000001"), rounding=ROUND_HALF_EVEN
                ),
            )
        if (
            any(component["metadata"].get("unpriced") for component in managed_components)
            and usage_cost.pricing_status == "priced"
        ):
            usage_cost = replace(usage_cost, pricing_status="partial")
        usage_components = _token_usage_components(
            usage_breakdown,
            usage_cost,
            segment_id="executor:aggregate",
            source="executor",
            model_name=resolved["model_name"],
            metadata={},
        )
        usage_components.extend(managed_components)
        usage_record = {
            "usage_cost": usage_cost,
            "prompt_tokens": usage["prompt_tokens"],
            "completion_tokens": usage["completion_tokens"],
            "breakdown": usage_breakdown,
            "usage_components": usage_components,
            "model_name": resolved["model_name"],
            "provider_name": resolved["provider_name"],
            "margin_multiplier": pricing_snapshot["margin_multiplier"],
            "credit_per_usd": pricing_snapshot["chat_credit_per_usd"],
        }
        message_finalization = (
            {
                "conversation_id": conversation_id,
                "message_id": int(message_id),
                "content": raw_text,
                "reasoning": "".join(reasoning) or None,
                "model_name": resolved["model_name"],
                "canonical_parts": parts,
                "token_prompt": usage["prompt_tokens"],
                "token_completion": usage["completion_tokens"],
                "message_status": "complete" if terminal_status == "completed" else "failed",
            }
            if conversation_id is not None and message_id is not None
            else None
        )
        if conversation_id is None:
            await _append_temp_history(run_id, payload, parts, owner=owner)
        child_result = None
        if run.parent_run_id is not None:
            child_result = children.child_result_payload(
                status=terminal_status,
                error_code=terminal_error_code,
                summary=raw_text,
                artifacts=[part for part in parts if isinstance(part, dict) and part.get("type") == "file"],
            )
        await _finish(
            run_id,
            status=terminal_status,
            message_id=message_id,
            owner=owner,
            error_code=terminal_error_code,
            safe_message=terminal_safe_message,
            usage_record=usage_record,
            message_finalization=message_finalization,
            completed_parts=completed_parts,
            child_result=child_result,
        )
    except DurableRunLeaseLost:
        logger.warning("stopped stale durable chat worker run_id=%s owner=%s", run_id, owner)
    except asyncio.CancelledError:
        logger.info("durable chat worker interrupted; deferring run recovery run_id=%s owner=%s", run_id, owner)
        raise
    except DurableRunProviderResultUnknown:
        await _finish(
            run_id,
            status="failed",
            message_id=message_id,
            owner=owner,
            error_code="provider_result_unknown",
            safe_message="이전 모델 호출 결과를 안전하게 확인할 수 없습니다",
        )
    except DurableRunProtocolMismatch:
        await _finish(
            run_id,
            status="failed",
            message_id=message_id,
            owner=owner,
            error_code="execution_protocol_mismatch",
            safe_message="chat run execution protocol is unavailable",
        )
    except PluginError as exc:
        await _finish(
            run_id,
            status="failed",
            message_id=message_id,
            owner=owner,
            error_code=exc.code,
            safe_message="selected plugin is unavailable or changed since admission",
        )
    except context_manager.ContextLimitExceeded as exc:
        await _finish(
            run_id,
            status="failed",
            message_id=message_id,
            owner=owner,
            error_code=getattr(exc, "code", "context_limit_exceeded"),
            safe_message="context input exceeds the configured limit",
        )
    except Exception:
        try:
            await flush_deltas(check_cancel=False)
        except Exception:
            logger.exception("failed to persist buffered chat deltas run_id=%s", run_id)
        logger.exception("durable chat provider execution failed run_id=%s", run_id)
        await _finish(
            run_id,
            status="failed",
            message_id=message_id,
            owner=owner,
            error_code="provider_failed",
            safe_message="chat provider failed",
        )
    finally:
        if deadline_task is not None:
            deadline_task.cancel()
            await asyncio.gather(deadline_task, return_exceptions=True)
        if next_event_task is not None:
            if not next_event_task.done():
                next_event_task.cancel()
            await asyncio.gather(next_event_task, return_exceptions=True)
        if engine_iterator is not None:
            close_iterator = getattr(engine_iterator, "aclose", None)
            if close_iterator is not None:
                await close_iterator()
        if lease_lost_wait_task is not None:
            lease_lost_wait_task.cancel()
            await asyncio.gather(lease_lost_wait_task, return_exceptions=True)
        heartbeat_stop.set()
        heartbeat_task.cancel()
        await asyncio.gather(heartbeat_task, return_exceptions=True)
    return True


async def queued_run_ids(*, limit: int = 4) -> list[str]:
    factory = _factory()
    async with factory() as session:
        return list(
            (
                await session.execute(
                    select(ChatRun.id).where(ChatRun.status == "queued").order_by(ChatRun.created_at).limit(limit)
                )
            ).scalars()
        )
