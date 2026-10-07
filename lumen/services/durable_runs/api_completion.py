"""Durable Batch chat/Responses runs: one fenced provider call on the shared run ledger.

``prepare_api_completion_run`` validates and freezes one stateless text request with
no provider I/O. ``persist_api_completion_run_in_transaction`` binds it inside the
Batch coordinator's transaction (project queue -> batch -> item -> new run) without
committing, holding credit or waking a worker. The worker executes nonstreaming
requests exactly once: the wallet/API-key hold and the ``provider_started`` intent
commit atomically before network I/O; the terminal response envelope is
checkpointed before settlement; usage settles once under event ``run:<run_id>``.
A call whose outcome cannot be proven is never re-sent and keeps its hold.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

from litellm import exceptions as litellm_errors
from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from lumen.config import get_settings
from lumen.crypto import encrypt_chat_content
from lumen.models.api_requests import OpenAIChatRequest, ResponsesRequest
from lumen.models.chat_runs import ChatModelCallReservation, ChatRun, ChatRunProvider, ChatRunSegment
from lumen.services import completion_api, credit, litellm_client, worker_routing
from lumen.services.api_key_store import ApiKeyAuthorityUnavailable, ApiKeyForbidden, authorize_api_key_in_transaction
from lumen.services.completion_format import nonstream_response
from lumen.services.inference_authority import completion_scopes
from lumen.services.providers import routing
from lumen.services.providers.errors import ProviderSubscriptionError
from lumen.services.providers.pricing import frozen_token_pricing
from lumen.services.run_store import (
    append_event,
    begin_segment_io,
    complete_segment_io,
    load_segment_payload,
    prepare_segment,
)
from lumen.services.usage_breakdown import UsageBreakdown

from . import budgets
from .common import _event, _factory, _fingerprint, _now
from .errors import DurableRunConflict, DurableRunError, DurableRunInputError, DurableRunProviderResultUnknown
from .lifecycle import _require_owned_running_lease

logger = logging.getLogger(__name__)

Operation = Literal["chat.completions", "responses"]

_SEGMENT = "api_completion:1"
_ENDPOINTS: dict[str, str] = {"chat.completions": "chat_completions", "responses": "responses"}
_DEFAULT_MAX_OUTPUT_TOKENS = 4096
_MAX_BOUND = Decimal("10000000000")
_DEFAULT_SCOPES = ("compat:completions:write",)
# Confirmed provider refusals: the request was answered without inference, so the
# hold is released at zero. Every other failure after I/O intent stays unknown.
_REJECTION_STATUSES = frozenset({400, 401, 403, 404, 413, 415, 422, 429})
_REJECTION_ERRORS = (
    litellm_errors.BadRequestError,
    litellm_errors.AuthenticationError,
    litellm_errors.PermissionDeniedError,
    litellm_errors.NotFoundError,
    litellm_errors.UnprocessableEntityError,
    litellm_errors.RateLimitError,
)
_SUBSCRIPTION_REJECTIONS = {
    "subscription_auth_required": 401,
    "subscription_rate_limited": 429,
    "subscription_protocol_unsupported": 400,
    "subscription_native_web_search_unsupported": 422,
}
_REJECTION_MESSAGES = {
    400: "upstream model rejected request",
    401: "upstream authentication failed",
    403: "upstream authentication failed",
    404: "upstream model not found",
    413: "upstream request too large",
    415: "upstream rejected request media type",
    422: "upstream model rejected request",
    429: "upstream rate limited",
}


@dataclass(frozen=True)
class PreparedApiCompletion:
    """Frozen finite text request; ``payload`` is exactly what the worker sends."""

    payload: dict
    capability_snapshot: dict
    pricing_snapshot: dict
    required_scopes: tuple[str, ...]
    project_id: str
    user_id: str


def _input_error(exc: completion_api.CompletionError) -> DurableRunError:
    """Request faults are item validation errors; storage outages stay retryable."""
    if exc.status_code >= 500:
        return DurableRunError("route_resolution_unavailable")
    return DurableRunInputError(exc.message)


def _function_tools_only(tools: list[dict] | None) -> None:
    # Provider built-in tools carry no provider-enforced cost bound; only
    # client-executed function tools are relayed.
    for tool in tools or []:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            raise DurableRunInputError("provider_builtin_tool_unsupported")


def _positive_limit(route: dict) -> int:
    capabilities = route.get("capabilities") if isinstance(route.get("capabilities"), dict) else {}
    limit = capabilities.get("context_limit")
    if type(limit) is not int or limit <= 0:
        raise DurableRunInputError("context_window_unknown")
    return limit


async def prepare_api_completion_run(
    request: dict,
    *,
    operation: Operation,
    project_id: str,
    user_id: str,
    source: str = "api",
    api_key_id: int | None = None,
    required_scopes: tuple[str, ...] | None = None,
) -> PreparedApiCompletion:
    """Validate and freeze one stateless chat/Responses request; never invokes a provider.

    The route is resolved exactly like the online compatibility API. The frozen
    bound covers the full context window at the highest frozen input/cache/media
    rate plus the explicit (default 4096) output budget at the highest output rate.
    """
    del source, api_key_id
    if operation not in _ENDPOINTS:
        raise DurableRunInputError("unsupported_operation")
    if not isinstance(request, dict):
        raise DurableRunInputError("invalid_request_body")
    from lumen.services.openai_compat import is_lumen_virtual_model

    try:
        body: OpenAIChatRequest | ResponsesRequest = (
            OpenAIChatRequest.model_validate(request) if operation == "chat.completions"
            else ResponsesRequest.model_validate(request)
        )
    except ValidationError as exc:
        raise DurableRunInputError("invalid_request_body") from exc
    if body.stream:
        raise DurableRunInputError("stream_unsupported")
    if is_lumen_virtual_model(body.model):
        raise DurableRunInputError("virtual_model_unsupported")
    try:
        provider = completion_api.select_api_provider(body.provider, None)
        route = await completion_api.resolve_api(body.model, provider=provider)
    except completion_api.CompletionError as exc:
        raise _input_error(exc) from exc
    if not litellm_client.provider_once_supported(route.get("provider_type")):
        raise DurableRunInputError("provider_once_unsupported")
    limit = _positive_limit(route)

    if isinstance(body, OpenAIChatRequest):
        _function_tools_only(body.tools)
        try:
            max_output = completion_api.clamp_max_tokens(body.max_tokens)
            route = completion_api.billing_route(route, body.messages, protocol="chat", options={"tools": body.tools})
        except completion_api.CompletionError as exc:
            raise _input_error(exc) from exc
        counted_messages, counted_tools = body.messages, body.tools
        payload: dict[str, Any] = {
            "kind": "api_completion", "operation": operation, "messages": body.messages,
            "max_tokens": max_output, "temperature": body.temperature, "tools": body.tools,
            "tool_choice": body.tool_choice,
        }
    else:
        if body.store is True or body.previous_response_id is not None:
            raise DurableRunInputError("stateful_responses_not_supported")
        if body.background is True:
            raise DurableRunInputError("background_not_supported")
        if body.context_management is not None:
            raise DurableRunInputError("provider_compaction_unsupported")
        if body.service_tier not in (None, "default"):
            raise DurableRunInputError("service_tier_unsupported")
        _function_tools_only(body.tools)
        options = body.model_dump(
            exclude={"model", "provider", "input", "stream", "store", "previous_response_id", "background",
                     "client_metadata"},
            exclude_none=True,
        )
        max_output = body.max_output_tokens or _DEFAULT_MAX_OUTPUT_TOKENS
        options["max_output_tokens"] = max_output
        counted_messages = completion_api.responses_input_messages(body.input)
        if body.instructions:
            counted_messages = [{"role": "system", "content": body.instructions}, *counted_messages]
        try:
            route = completion_api.billing_route(route, counted_messages, protocol="responses", options=options)
        except completion_api.CompletionError as exc:
            raise _input_error(exc) from exc
        counted_tools = body.tools
        payload = {
            "kind": "api_completion", "operation": operation, "input": body.input,
            "options": completion_api.responses_provider_options(options, resolved=route),
        }

    count = litellm_client.count_context_tokens(route["model_name"], counted_messages, counted_tools)
    if count.tokens is not None and count.tokens > limit:
        raise DurableRunInputError("context_length_exceeded")
    try:
        token_pricing = frozen_token_pricing(route)
    except Exception as exc:
        raise DurableRunInputError("model token pricing is invalid") from exc
    if token_pricing["input_price_per_token"] is None or token_pricing["output_price_per_token"] is None:
        raise DurableRunInputError("pricing_unavailable")
    pricing = {
        **token_pricing,
        "required_token_modalities": list(route["required_token_modalities"]),
        "margin_multiplier": format(Decimal(str(route.get("margin_multiplier", "1"))), "f"),
        "chat_credit_per_usd": format(Decimal(str(get_settings().chat_credit_per_usd)), "f"),
        "provider_name": route["provider_name"],
        "model_name": route["model_name"],
        "rounding_version": "half_even_v1",
        "context_limit": limit,
        "max_output_tokens": max_output,
    }
    try:
        bound = credit.call_credit_bound(pricing, input_tokens=limit, output_tokens=max_output)
    except ValueError as exc:
        raise DurableRunInputError("pricing_unavailable") from exc
    if bound >= _MAX_BOUND:
        raise DurableRunInputError("credit_bound_too_large")
    pricing["bound_credits"] = format(bound, "f")
    capability = {key: route[key] for key in (
        "provider_id", "model_id", "provider_name", "provider_type", "model_name", "model_kind",
        "api_model_name", "config_version_hash",
    )}
    capability.update({"operation": operation, "context_limit": limit, "effective_features": {}})
    return PreparedApiCompletion(
        payload=payload,
        capability_snapshot=capability,
        pricing_snapshot=pricing,
        required_scopes=completion_scopes(request, base=tuple(required_scopes or _DEFAULT_SCOPES), resolved=route),
        project_id=project_id,
        user_id=user_id,
    )


async def persist_api_completion_run_in_transaction(
    session: AsyncSession,
    prepared: PreparedApiCompletion,
    *,
    project_id: str,
    user_id: str,
    client_request_id: str,
    source: str = "api",
    api_key_id: int | None = None,
    workload_class: str | None = "batch",
    batch_id: str | None = None,
) -> ChatRun:
    """Bind a prepared request in the caller's transaction; never commit, hold credit or wake.

    The caller already holds project queue -> batch -> item locks. ``api_completion``
    runs are Batch-only: routing rejects a missing ``batch_id``. A replayed
    ``client_request_id`` (deterministic per item) returns the existing run.
    """
    from .admission import _lock_run_configurations

    if (prepared.project_id, prepared.user_id) != (project_id, user_id):
        raise DurableRunInputError("prepared request owner changed")
    try:
        uuid.UUID(client_request_id)
    except ValueError as exc:
        raise DurableRunInputError("client_request_id must be a UUID") from exc
    capability, pricing = prepared.capability_snapshot, prepared.pricing_snapshot
    fingerprint = _fingerprint(prepared.payload)
    existing = (await session.execute(select(ChatRun).where(
        ChatRun.project_id == project_id, ChatRun.user_id == user_id, ChatRun.client_request_id == client_request_id,
    ).with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
    if existing is not None:
        if existing.request_fingerprint != fingerprint or existing.batch_id != batch_id:
            raise DurableRunConflict("idempotency_key_reused_with_different_intent")
        return existing
    route = await worker_routing.resolve_worker_route(
        session, run_kind="api_completion", workload_class=workload_class, batch_id=batch_id
    )
    await _lock_run_configurations(session, capability, model_name=capability["model_name"])
    try:
        await authorize_api_key_in_transaction(session, api_key_id=api_key_id, user_id=user_id,
                                               project_id=project_id, required_scopes=prepared.required_scopes)
    except ApiKeyForbidden as exc:
        raise DurableRunInputError("api_key_unauthorized") from exc
    except ApiKeyAuthorityUnavailable as exc:
        raise DurableRunError("inference_authority_unavailable") from exc
    request_payload = {**prepared.payload, "execution_protocol_version": 1,
                       "required_scopes": list(prepared.required_scopes)}
    run = ChatRun(
        id=str(uuid.uuid4()), run_scope="api", run_kind="api_completion", project_id=project_id, user_id=user_id,
        model_name=capability["model_name"], source=source, api_key_id=api_key_id,
        workload_class=route.workload_class, worker_pool_id=route.worker_pool_id, batch_id=batch_id,
        client_request_id=client_request_id, request_fingerprint=fingerprint, fingerprint_version=1,
        execution_protocol_version=1, capability_snapshot=capability, pricing_snapshot=pricing,
        request_payload=encrypt_chat_content(json.dumps(request_payload, ensure_ascii=False, separators=(",", ":"))),
        status="queued", last_seq=0, current_ordinal=0,
    )
    session.add(run)
    session.add(ChatRunProvider(
        run_id=run.id, purpose="executor", provider_id=capability["provider_id"], model_id=capability["model_id"],
        provider_label=capability["provider_name"], model_label=capability["model_name"],
        config_version_hash=capability["config_version_hash"],
    ))
    await append_event(session, run, _event(run, "run.started", {
        "conversation_id": None, "temp_thread_id": None, "model_name": run.model_name,
        "effective_features": {}, "run_kind": "api_completion",
    }))
    await append_event(session, run, _event(run, "run.stage.changed", {"stage": "queued"}))
    return run


async def load_api_completion_envelope(session: AsyncSession, run_id: str) -> dict | None:
    """Checkpointed terminal ``{status_code, request_id, body}`` of a run, or None if absent.

    Reads in the caller's transaction; a checkpoint is immutable once completed.
    """
    segment = (await session.execute(select(ChatRunSegment).where(
        ChatRunSegment.run_id == run_id, ChatRunSegment.segment_id == _SEGMENT,
    ))).scalar_one_or_none()
    if segment is None or segment.status != "completed":
        return None
    result = load_segment_payload(segment.result_payload)
    envelope = result.get("envelope") if isinstance(result, dict) else None
    return envelope if isinstance(envelope, dict) else None


def _frozen_bound(pricing: dict) -> Decimal:
    """Recompute the admission bound from the frozen snapshot; any drift fails closed."""
    try:
        bound = credit.call_credit_bound(pricing, input_tokens=int(pricing["context_limit"]),
                                         output_tokens=int(pricing["max_output_tokens"]))
        frozen = Decimal(str(pricing["bound_credits"]))
    except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
        raise DurableRunInputError("frozen credit bound is unavailable") from exc
    if bound != frozen or bound >= _MAX_BOUND:
        raise DurableRunInputError("frozen credit bound is invalid")
    return bound


async def _segment_start(run_id: str, *, owner: str, operation: str, required_scopes: tuple[str, ...],
                         bound: Decimal) -> str:
    """Commit the hold and irrevocable I/O intent, or report why no provider I/O may begin.

    Lock order: batch (shared) -> run -> segment -> provider config -> wallet.
    """
    from .admission import _lock_run_configurations

    async def transaction() -> str:
        async with _factory()() as session, session.begin():
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
            batch_id = await session.scalar(select(ChatRun.batch_id).where(ChatRun.id == run_id))
            batch_open = batch_id is not None and await worker_routing.lock_open_batch(session, batch_id)
            run = (await session.execute(select(ChatRun).where(ChatRun.id == run_id)
                   .with_for_update().execution_options(populate_existing=True))).scalar_one()
            _require_owned_running_lease(run, owner)
            if run.batch_id != batch_id:
                raise DurableRunInputError("batch identity changed")
            segment = await prepare_segment(session, run, segment_id=_SEGMENT, ordinal=1,
                                            endpoint=_ENDPOINTS[operation])
            await session.flush()
            if segment.status == "completed":
                return "completed"
            if segment.status != "prepared":
                return "unknown"
            if run.cancel_requested_at is not None:
                return "canceled"
            if not batch_open:
                return "batch_closed"
            await _lock_run_configurations(session, run.capability_snapshot, model_name=run.model_name)
            await authorize_api_key_in_transaction(session, api_key_id=run.api_key_id, user_id=run.user_id,
                                                   project_id=run.project_id, required_scopes=required_scopes)
            await credit.reserve_call_credit_in_transaction(session, user_id=run.user_id, project_id=run.project_id,
                                                            api_key_id=run.api_key_id, bound=bound)
            session.add(ChatModelCallReservation(run_id=run.id, segment_id=_SEGMENT, bound_credits=bound,
                                                 status="reserved"))
            run.reserved_credits = bound
            begin_segment_io(segment)
            run.provider_started_at = _now()
            await append_event(session, run, _event(run, "run.stage.changed", {"stage": "model_request"}))
            return "started"

    return await budgets.retry_deadlocks(transaction)


def _rejection_status(exc: BaseException) -> int | None:
    """HTTP status of a provider refusal proven to precede inference, else None (unknown)."""
    if isinstance(exc, ProviderSubscriptionError):
        return _SUBSCRIPTION_REJECTIONS.get(exc.code)
    if isinstance(exc, ValueError) and str(exc) == "provider_once_unsupported":
        return 400
    status = getattr(exc, "status_code", None)
    if isinstance(exc, _REJECTION_ERRORS) and type(status) is int and status in _REJECTION_STATUSES:
        return status
    return None


def _rejection_envelope(run_id: str, status: int) -> dict:
    return {"status_code": status, "request_id": run_id, "body": {"error": {
        "message": _REJECTION_MESSAGES[status],
        "type": "invalid_request_error" if status != 429 else "rate_limit_error",
        "code": "provider_rejected",
    }}}


async def _invoke(route: dict, payload: dict, *, run_id: str, pricing: dict) -> tuple[dict, dict]:
    """Send exactly one provider request; return the checkpoint ``(result, usage)`` payloads.

    Usage is validated against the frozen modality requirements before the
    envelope is built. A response without provably priceable usage is still
    checkpointed (never re-sent), marked so settlement keeps its hold unknown.
    """
    operation = payload["operation"]
    token_rates = pricing.get("token_rates")
    required = pricing.get("required_token_modalities") or []
    if operation == "chat.completions":
        messages = payload["messages"]
        invocation = await completion_api.invoke_chat_once(
            resolved=route, messages=messages, max_tokens=payload["max_tokens"],
            temperature=payload.get("temperature"), tools=payload.get("tools"),
            tool_choice=payload.get("tool_choice"), provider_once=True,
        )
        output_text, usage = invocation.content, invocation.usage
    else:
        messages = completion_api.responses_input_messages(payload["input"])
        body = await completion_api.invoke_responses_once(
            resolved=route, input=payload["input"], options=payload["options"], provider_once=True,
        )
        output_text, usage = completion_api.responses_usage(body, strict=True)
    try:
        if usage is None:
            raise completion_api.CompletionError(502, "usage_unavailable")
        breakdown = completion_api.usage_breakdown_for_billing(
            route["model_name"], token_rates=token_rates, required=required, messages=messages,
            text=output_text, final_usage=usage,
        )
    except completion_api.CompletionError as exc:
        logger.warning("durable api completion usage is not priceable run_id=%s code=%s", run_id, exc.message)
        raw = {"content": invocation.content, "tool_calls": invocation.tool_calls,
               "finish_reason": invocation.finish_reason} if operation == "chat.completions" else body
        return {"operation": operation, "envelope": None, "unpriced_result": raw}, {"usage_error": exc.message}
    if operation == "chat.completions":
        body = nonstream_response({
            "model": invocation.model, "content": invocation.content, "tool_calls": invocation.tool_calls,
            "finish_reason": invocation.finish_reason, "prompt_tokens": breakdown.input_tokens,
            "completion_tokens": breakdown.output_tokens,
            "cache_read_input_tokens": breakdown.cache_read_input_tokens,
        }, cmpl_id=f"chatcmpl-{uuid.UUID(run_id).hex}", created=int(time.time()))
    envelope = {"status_code": 200, "request_id": run_id, "body": body}
    return {"operation": operation, "envelope": envelope}, {"token_usage": breakdown.as_usage_dict()}


async def _checkpoint(run_id: str, *, owner: str, result: dict, usage: dict | None) -> None:
    """Durably record the terminal provider outcome before any settlement."""
    async def transaction() -> None:
        async with _factory()() as session, session.begin():
            run = (await session.execute(select(ChatRun).where(ChatRun.id == run_id)
                   .with_for_update().execution_options(populate_existing=True))).scalar_one()
            _require_owned_running_lease(run, owner)
            segment = (await session.execute(select(ChatRunSegment).where(
                ChatRunSegment.run_id == run_id, ChatRunSegment.segment_id == _SEGMENT,
            ).with_for_update().execution_options(populate_existing=True))).scalar_one()
            if segment.status != "provider_started":
                raise DurableRunProviderResultUnknown("api completion boundary is no longer owned")
            complete_segment_io(segment, result_payload=result, usage_payload=usage)
            await append_event(session, run, _event(run, "run.stage.changed", {"stage": "model_response"}))

    await budgets.retry_deadlocks(transaction)


async def _settle(run_id: str, *, owner: str) -> Literal["completed", "rejected"]:
    """Settle the checkpoint exactly once; unpriceable usage keeps the hold for reconciliation."""
    async def transaction() -> Literal["completed", "rejected"]:
        async with _factory()() as session, session.begin():
            run = (await session.execute(select(ChatRun).where(ChatRun.id == run_id)
                   .with_for_update().execution_options(populate_existing=True))).scalar_one()
            _require_owned_running_lease(run, owner)
            segment = (await session.execute(select(ChatRunSegment).where(
                ChatRunSegment.run_id == run_id, ChatRunSegment.segment_id == _SEGMENT,
            ).with_for_update().execution_options(populate_existing=True))).scalar_one()
            result = load_segment_payload(segment.result_payload)
            if segment.status != "completed" or not isinstance(result, dict):
                raise DurableRunProviderResultUnknown("api completion checkpoint is unavailable")
            envelope = result.get("envelope")
            rejected = isinstance(envelope, dict) and envelope.get("status_code") != 200
            reservation = (await session.execute(select(ChatModelCallReservation).where(
                ChatModelCallReservation.run_id == run_id, ChatModelCallReservation.segment_id == _SEGMENT,
            ).with_for_update().execution_options(populate_existing=True))).scalar_one()
            if reservation.status == "settled":
                return "rejected" if rejected else "completed"
            if reservation.status != "reserved" or not isinstance(envelope, dict):
                raise DurableRunProviderResultUnknown("api completion result cannot safely be settled")
            if rejected:
                # The provider refused before inference: release the hold at zero, no ledger row.
                reservation.actual_credits = Decimal("0")
                reservation.status = "settled"
                reservation.settled_at = _now()
                run.usage_reconciled_at = _now()
                return "rejected"
            pricing = run.pricing_snapshot
            usage = load_segment_payload(segment.usage_payload)
            try:
                breakdown = UsageBreakdown.from_canonical(usage.get("token_usage") if isinstance(usage, dict) else None)
                usage_cost = credit.usage_cost_from_pricing_snapshot(
                    pricing, prompt_tokens=breakdown.input_tokens, completion_tokens=breakdown.output_tokens,
                    breakdown=breakdown, required_modalities=pricing.get("required_token_modalities") or (),
                )
                components = credit.token_usage_components(
                    usage_cost, segment_id=_SEGMENT, source="executor", model_name=run.model_name,
                    metadata={"operation": result.get("operation")},
                )
            except (ValueError, TypeError, KeyError) as exc:
                raise DurableRunProviderResultUnknown("api completion usage cannot safely be settled") from exc
            margin = Decimal(pricing["margin_multiplier"])
            per_usd = Decimal(pricing["chat_credit_per_usd"])
            credited = await credit.apply_usage_in_transaction(
                session, event_id=f"run:{run.id}", user_id=run.user_id, project_id=run.project_id,
                model_name=run.model_name, provider=run.capability_snapshot["provider_name"],
                prompt_tokens=breakdown.input_tokens, completion_tokens=breakdown.output_tokens,
                usage_cost=usage_cost, breakdown=breakdown, margin_multiplier=margin, credit_per_usd=per_usd,
                source=run.source, api_key_id=run.api_key_id, run_id=run.id, usage_components=components,
            )
            if credited > Decimal(str(reservation.bound_credits)):
                logger.warning("durable api completion usage exceeded its frozen bound run_id=%s", run.id)
            reservation.actual_credits = credited
            reservation.status = "settled"
            reservation.settled_at = _now()
            run.usage_reconciled_at = _now()
            await append_event(session, run, _event(run, "usage.updated", {
                "components": components, "prompt_tokens": breakdown.input_tokens,
                "completion_tokens": breakdown.output_tokens, "raw_cost": format(usage_cost.raw_cost, "f"),
                "credited_cost": format(credited, "f"),
            }))
            return "completed"

    return await budgets.retry_deadlocks(transaction)


async def execute_api_completion_run(run_id: str, *, owner: str, payload: dict, capability_snapshot: dict,
                                     pricing_snapshot: dict) -> bool:
    """Keep the fenced claim live while the single provider call is in flight."""
    from .lifecycle import _renew_lease

    stop = asyncio.Event()

    async def heartbeat() -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=10)
            except TimeoutError:
                if not await _renew_lease(run_id, owner):
                    return

    task = asyncio.create_task(heartbeat())
    try:
        return await _execute(run_id, owner=owner, payload=payload, capability_snapshot=capability_snapshot,
                              pricing_snapshot=pricing_snapshot)
    finally:
        stop.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def _finish_settled(run_id: str, *, owner: str) -> None:
    """Settle a durable checkpoint without provider I/O and publish the terminal status."""
    from .execution import _finish

    try:
        outcome = await _settle(run_id, owner=owner)
    except DurableRunProviderResultUnknown:
        await _finish(run_id, status="failed", message_id=None, owner=owner, error_code="provider_result_unknown",
                      safe_message="api completion usage cannot safely be settled")
        return
    if outcome == "rejected":
        await _finish(run_id, status="failed", message_id=None, owner=owner, error_code="provider_rejected",
                      safe_message="upstream provider rejected the request")
        return
    # A collected, billed result is published even if cancellation was requested meanwhile.
    await _finish(run_id, status="completed", message_id=None, owner=owner)


async def _execute(run_id: str, *, owner: str, payload: dict, capability_snapshot: dict,
                   pricing_snapshot: dict) -> bool:
    """Execute a claimed run; a started or checkpointed boundary never invokes the provider again."""
    from .execution import _finish

    async with _factory()() as session:
        persisted = (await session.execute(select(ChatRunSegment.status).where(
            ChatRunSegment.run_id == run_id, ChatRunSegment.segment_id == _SEGMENT,
        ))).scalar_one_or_none()
    try:
        if persisted == "completed":
            await _finish_settled(run_id, owner=owner)
            return True
        if persisted in {"provider_started", "failed"}:
            await _finish(run_id, status="failed", message_id=None, owner=owner,
                          error_code="provider_result_unknown",
                          safe_message="api completion provider result cannot safely be retried")
            return True
    except Exception:
        logger.exception("durable api completion finalization deferred run_id=%s", run_id)
        return True

    operation = payload.get("operation")
    scopes = payload.get("required_scopes")
    pre_io_error: tuple[str, str] | None = None
    state = "unstarted"
    try:
        if operation not in _ENDPOINTS or payload.get("kind") != "api_completion":
            raise DurableRunInputError("api completion payload is invalid")
        if not isinstance(scopes, list) or not scopes or not all(isinstance(scope, str) for scope in scopes):
            raise DurableRunInputError("api completion scopes are unavailable")
        bound = _frozen_bound(pricing_snapshot)
        route = await routing.resolve_model_snapshot(capability_snapshot)
        if route is None or route.get("provider_type") != capability_snapshot.get("provider_type"):
            raise DurableRunConflict("provider_configuration_changed")
        state = await _segment_start(run_id, owner=owner, operation=operation, required_scopes=tuple(scopes),
                                     bound=bound)
    except ApiKeyForbidden:
        pre_io_error = ("api_key_unauthorized", "API key is no longer authorized for this request")
    except ApiKeyAuthorityUnavailable:
        pre_io_error = ("inference_authority_unavailable", "Current inference authority is unavailable")
    except credit.QuotaExceeded:
        pre_io_error = ("quota_exceeded", "credit quota is exhausted")
    except DurableRunConflict:
        pre_io_error = ("provider_configuration_changed", "provider configuration changed after admission")
    except DurableRunInputError:
        pre_io_error = ("invalid_request", "api completion request cannot be executed")
    except Exception:
        logger.exception("durable api completion could not start run_id=%s", run_id)
        pre_io_error = ("execution_unavailable", "api completion could not start")

    try:
        if pre_io_error is not None:
            # No intent was committed: nothing was sent and no hold exists.
            await _finish(run_id, status="failed", message_id=None, owner=owner, error_code=pre_io_error[0],
                          safe_message=pre_io_error[1])
            return True
        if state == "canceled":
            await _finish(run_id, status="canceled", message_id=None, owner=owner, error_code="canceled",
                          safe_message="api completion canceled")
            return True
        if state == "batch_closed":
            await _finish(run_id, status="canceled", message_id=None, owner=owner, error_code="batch_closed",
                          safe_message="batch no longer accepts provider calls")
            return True
        if state == "unknown":
            await _finish(run_id, status="failed", message_id=None, owner=owner,
                          error_code="provider_result_unknown",
                          safe_message="api completion provider result cannot safely be retried")
            return True
        if state == "completed":
            await _finish_settled(run_id, owner=owner)
            return True
    except Exception:
        logger.exception("durable api completion finalization deferred run_id=%s", run_id)
        return True

    # Intent and hold are committed: exactly one request is sent; failures never re-send.
    frozen_route = {**route, "token_rates": pricing_snapshot.get("token_rates"),
                    "required_token_modalities": pricing_snapshot.get("required_token_modalities") or []}
    try:
        try:
            result, usage = await _invoke(frozen_route, payload, run_id=run_id, pricing=pricing_snapshot)
        except Exception as exc:
            status = _rejection_status(exc)
            if status is None:
                raise DurableRunProviderResultUnknown("api completion provider outcome is unknown") from exc
            logger.info("durable api completion provider rejected run_id=%s status=%s", run_id, status)
            result, usage = {"operation": operation, "envelope": _rejection_envelope(run_id, status)}, None
        await _checkpoint(run_id, owner=owner, result=result, usage=usage)
    except Exception:
        logger.exception("durable api completion execution failed run_id=%s", run_id)
        try:
            # _finish keeps a still-reserved hold as unknown and marks the started segment failed.
            await _finish(run_id, status="failed", message_id=None, owner=owner,
                          error_code="provider_result_unknown",
                          safe_message="api completion provider result cannot safely be retried")
        except Exception:
            logger.exception("api completion run finalization deferred run_id=%s", run_id)
        return True
    try:
        await _finish_settled(run_id, owner=owner)
    except Exception:
        # The checkpoint is durable; recovery settles it without provider I/O.
        logger.exception("durable api completion settlement deferred run_id=%s", run_id)
    return True


__all__ = [
    "PreparedApiCompletion",
    "execute_api_completion_run",
    "load_api_completion_envelope",
    "persist_api_completion_run_in_transaction",
    "prepare_api_completion_run",
]
