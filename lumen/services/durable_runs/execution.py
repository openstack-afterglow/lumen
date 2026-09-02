"""Durable worker execution and journal transitions."""

import asyncio
import json
import logging
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from time import monotonic
from typing import Any

from sqlalchemy import select

from lumen.crypto import decrypt_chat_content, encrypt_chat_content
from lumen.models.chat_db import ChatMessage
from lumen.models.chat_runs import ChatRun, ChatRunSegment, ChatRunTurn, ChatTempThread
from lumen.services import assets, credit, engine, extensions_store
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
) -> None:
    factory = _factory()
    async with factory() as session, session.begin():
        run = (
            await session.execute(select(ChatRun).where(ChatRun.id == run_id).with_for_update())
        ).scalar_one_or_none()
        if run is None:
            return
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
        await enqueue_completed_run_in_transaction(session, run, memory_enabled=memory_enabled)
        event_type = {"completed": "run.completed", "failed": "run.failed", "canceled": "run.canceled"}[status]
        payload: dict[str, Any] = {"status": status, "message_id": message_id}
        if status != "completed":
            payload.update(error_code=error_code or "run_failed", safe_message=safe_message or "chat run failed")
        await append_event(session, run, _event(run, event_type, payload))


class _DurableExecutionHooks:
    """Persist and replay each external graph boundary for one leased run."""

    journals_tool_events = True

    def __init__(self, *, run_id: str, owner: str) -> None:
        self.run_id = run_id
        self.owner = owner
        self.active_turn_ordinal: int | None = None
        self.active_message_id: str | None = None

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

    async def _start(
        self,
        *,
        segment_id: str,
        ordinal: int,
        endpoint: str,
        turn_ordinal: int,
        call_id: str | None = None,
        journal_started: tuple[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any] | None:
        factory = _factory()
        async with factory() as session, session.begin():
            run = (
                await session.execute(select(ChatRun).where(ChatRun.id == self.run_id).with_for_update())
            ).scalar_one()
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
                return {**payload, "_durable_replay": True}
            if segment.status == "provider_started":
                fail_unresolved_segment(segment, error_code="provider_result_unknown")
                return {"_boundary_abort": "provider_result_unknown"}
            if segment.status == "failed":
                return {"_boundary_abort": "provider_result_unknown"}
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
    ) -> None:
        factory = _factory()
        async with factory() as session, session.begin():
            run = (
                await session.execute(select(ChatRun).where(ChatRun.id == self.run_id).with_for_update())
            ).scalar_one()
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
            run = (
                await session.execute(select(ChatRun).where(ChatRun.id == self.run_id).with_for_update())
            ).scalar_one()
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
            return {"_boundary_abort": "provider_result_unknown"}

    async def provider_started(self, *, round_index: int, attempt: int) -> dict[str, Any] | None:
        return await self._start(
            segment_id=f"provider:{round_index}:{attempt}",
            ordinal=round_index * 1_000 + attempt,
            endpoint="chat_completions",
            turn_ordinal=round_index,
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


_MANAGED_USAGE_KEYS = {
    ("web_search_requests", "web_search_request_per_unit", "request", "search"),
    ("web_search_context", "web_search_context_low_per_unit", "context", "search"),
    ("web_search_context", "web_search_context_medium_per_unit", "context", "search"),
    ("web_search_context", "web_search_context_high_per_unit", "context", "search"),
    ("web_fetch_requests", "web_fetch_request_per_unit", "request", "fetch"),
    ("web_fetch_context", "web_fetch_context_per_unit", "context", "fetch"),
    ("advisor_input_tokens", "advisor_input_price_per_token", "token", "advisor"),
    ("advisor_output_tokens", "advisor_output_price_per_token", "token", "advisor"),
}


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
        try:
            quantity = Decimal(str(record.get("quantity")))
            unit_price = Decimal(str(prices[price_key]))
        except (InvalidOperation, KeyError, TypeError, ValueError) as exc:
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
                "metadata": {},
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
    if isinstance(search_options, dict) and search_options.get("enabled") is True:
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


async def execute_queued_run(run_id: str, *, owner: str) -> bool:
    """Claim one queued run and stream its normalized engine output into the journal."""
    factory = _factory()
    async with factory() as session, session.begin():
        run = await claim_queued_run(session, run_id, owner=owner)
        if run is None:
            return False
        payload = _payload(run)
        model_name = run.model_name
        project_id = run.project_id
        user_id = run.user_id
        conversation_id = run.conversation_id
        parent_id = run.user_message_id
        capability_snapshot = run.capability_snapshot
        pricing_snapshot = run.pricing_snapshot
        existing_message_id = run.assistant_message_id

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
    usage: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0}
    managed_tool_usage: list[dict[str, Any]] = []

    text: list[str] = []
    reasoning: list[str] = []
    pending_deltas: dict[str, list[str]] = {"text": [], "reasoning": []}
    last_delta_flush_at = monotonic()

    async def flush_deltas(*, check_cancel: bool = True) -> bool:
        """Persist bounded delta batches without stalling upstream chunk consumption."""
        if lease_lost.is_set():
            raise DurableRunLeaseLost(f"lease lost for {run_id}")
        nonlocal last_delta_flush_at
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
        last_delta_flush_at = monotonic()
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
        execution_hooks = _DurableExecutionHooks(run_id=run_id, owner=owner)
        feature_options = payload.get("features")
        tool_policy = feature_options.get("tool_policy") if isinstance(feature_options, dict) else None
        approval_mode = tool_policy.get("approval_mode") if isinstance(tool_policy, dict) else None
        workspace_write_mode = (
            tool_policy.get("workspace_write_mode")
            if isinstance(tool_policy, dict) and tool_policy.get("workspace_write_mode") in {"ask", "auto_edit"}
            else "ask"
        )
        resume = await _v2_approval_resume(run_id=run_id, owner=owner) if run.execution_protocol_version == 2 else None
        hydrated_turn_ordinal: int | None = None

        async def hydrate_replayed_turn() -> None:
            nonlocal hydrated_turn_ordinal, text, reasoning, pending_deltas, last_delta_flush_at
            if execution_hooks.active_turn_ordinal == hydrated_turn_ordinal:
                return
            projection = await execution_hooks.replay_turn_projection()
            text = [projection["text"]] if projection["text"] else []
            reasoning = [projection["reasoning"]] if projection["reasoning"] else []
            pending_deltas = {"text": [], "reasoning": []}
            last_delta_flush_at = monotonic()
            hydrated_turn_ordinal = execution_hooks.active_turn_ordinal

        async for event in engine.stream(
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
            run_id=run_id,
            execution_hooks=execution_hooks,
            execution_protocol_version=run.execution_protocol_version,
            lumen_snapshot=payload.get("lumen_snapshot") if isinstance(payload.get("lumen_snapshot"), dict) else None,
            lumen_snapshot_frozen=run.execution_protocol_version == 2,
            approval_mode=approval_mode if isinstance(approval_mode, str) else None,
            workspace_write_mode=workspace_write_mode,
            resume=resume,
        ):
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
                    pending_deltas["reasoning"].append(reasoning_delta)
            else:
                if await flush_deltas():
                    return True
                if etype == "assistant_tool_calls":
                    calls = event.get("tool_calls")
                    pending_tool_results = len(calls) if isinstance(calls, list) else None
                elif etype == "input.interrupted":
                    payload = event.get("payload")
                    if not isinstance(payload, dict) or run.execution_protocol_version != 2:
                        raise DurableRunError("unexpected durable input interrupt")
                    await _persist_v2_approval_interrupt(run_id=run_id, owner=owner, calls=payload.get("calls"))
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
                        usage["prompt_tokens"] = max(0, int(value.get("prompt_tokens") or 0))
                        usage["completion_tokens"] = max(0, int(value.get("completion_tokens") or 0))
                    emitted_tool_usage = event.get("tool_usage")
                    if isinstance(emitted_tool_usage, list):
                        managed_tool_usage = [item for item in emitted_tool_usage if isinstance(item, dict)]
                elif etype == "error":
                    if event.get("code") == "provider_result_unknown":
                        raise DurableRunProviderResultUnknown("provider result is indeterminate")
                    raise DurableRunError(str(event.get("message") or "provider error"))

            if (
                sum(len(chunk) for chunks in pending_deltas.values() for chunk in chunks) >= 128
                or monotonic() - last_delta_flush_at >= 0.1
            ) and await flush_deltas():
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
        )
        managed_cost, managed_components = _managed_usage_components(
            managed_tool_usage,
            pricing_snapshot=pricing_snapshot,
            model_name=resolved["model_name"],
        )
        if managed_cost:
            usage_cost = UsageCost(
                raw_cost=(usage_cost.raw_cost + managed_cost).quantize(
                    Decimal("0.0000000001"), rounding=ROUND_HALF_EVEN
                ),
                input_cost=usage_cost.input_cost,
                output_cost=usage_cost.output_cost,
                pricing_status=usage_cost.pricing_status,
                pricing_snapshot=usage_cost.pricing_snapshot,
            )
        usage_components = []
        for kind, quantity, cost in (
            ("input_tokens", usage["prompt_tokens"], usage_cost.input_cost),
            ("output_tokens", usage["completion_tokens"], usage_cost.output_cost),
        ):
            unit_price = cost / quantity if quantity else 0
            usage_components.append(
                {
                    "segment_id": "executor:aggregate",
                    "kind": kind,
                    "quantity": str(quantity),
                    "unit": "token",
                    "unit_price_usd": format(unit_price, "f"),
                    "cost_usd": format(cost, "f"),
                    "source": "executor",
                    "model_name": resolved["model_name"],
                    "metadata": {},
                }
            )
        usage_components.extend(managed_components)
        usage_record = {
            "usage_cost": usage_cost,
            "prompt_tokens": usage["prompt_tokens"],
            "completion_tokens": usage["completion_tokens"],
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
