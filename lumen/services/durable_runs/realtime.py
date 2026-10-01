"""Durable realtime session admission, usage checkpoint and exactly-once settlement.

Only encrypted admission options, the frozen billing plan, aggregate PCM byte counts,
provider-connected session time and canonical provider token usage persist. Provider
audio and transcripts stay inside the WebSocket relay and are never journaled.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import uuid
from decimal import Decimal

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from lumen.cache import _get_redis
from lumen.config import get_settings
from lumen.crypto import encrypt_chat_content
from lumen.models.chat_runs import ChatModelCallReservation, ChatRun, ChatRunProvider, ChatRunSegment
from lumen.services import credit
from lumen.services.litellm_client import UsageCost
from lumen.services.providers import pricing as provider_pricing
from lumen.services.providers import realtime_transport, routing
from lumen.services.providers.errors import AmbiguousModelRouteError, ProviderValidationError
from lumen.services.run_store import (
    append_event,
    begin_segment_io,
    claim_queued_run,
    complete_segment_io,
    load_segment_payload,
    prepare_segment,
    record_segment_usage,
)
from lumen.services.usage_breakdown import UsageBreakdown

from . import budgets
from .common import _event, _factory, _fingerprint, _now, _payload
from .errors import (
    DurableRunConflict,
    DurableRunError,
    DurableRunInputError,
    DurableRunNotFound,
    DurableRunProviderResultUnknown,
)
from .lifecycle import _renew_lease

_TICKET_TTL = 60
_SEGMENT = "realtime:1"
_COMPARE_CONSUME = """
local raw = redis.call('GET', KEYS[1])
if not raw then return nil end
local ok, value = pcall(cjson.decode, raw)
if not ok or value['digest'] ~= ARGV[1] then return nil end
redis.call('DEL', KEYS[1])
return raw
"""

# Provider-connected session time; ledger schema kind owned by the usage contract.
_SESSION_KIND = "realtime_session_seconds"


def _duration_seconds_cost(plan: dict, family: str, seconds: Decimal) -> Decimal:
    try:
        cost = provider_pricing.duration_cost_usd(plan["media_pricing"], family, seconds)
    except (KeyError, TypeError, ValueError, ProviderValidationError) as exc:
        raise DurableRunProviderResultUnknown("frozen realtime duration price is invalid") from exc
    if cost is None or cost < 0:
        raise DurableRunProviderResultUnknown("frozen realtime duration price is invalid")
    return cost


def _pricing_snapshot(snapshot: dict) -> dict:
    """Read admission snapshots predating billing_basis as their frozen legacy PCM plan."""
    if "billing_basis" in snapshot:
        return snapshot
    try:
        rates = {family: {"rate": snapshot[key], "unit_seconds": 60} for family, key in (
            ("realtime_input", "input_per_minute"), ("realtime_output", "output_per_minute"))}
    except KeyError as exc:
        raise DurableRunProviderResultUnknown("frozen realtime duration price is unavailable") from exc
    return {**snapshot, "billing_basis": "duration", "duration_rates": rates,
            "media_pricing": {f"{family}_per_minute": item["rate"] for family, item in rates.items()}}


def _plan_matches(snapshot: dict, current: dict | None) -> bool:
    if current is None:
        return False
    if "billing_basis" in snapshot:
        return all(snapshot.get(key) == value for key, value in current.items())
    if current.get("billing_basis") != "duration":
        return False
    frozen = _pricing_snapshot(snapshot)["duration_rates"]
    return all(Decimal(item["rate"]) * current["duration_rates"][family]["unit_seconds"]
               == Decimal(current["duration_rates"][family]["rate"]) * item["unit_seconds"]
               for family, item in frozen.items())


def _envelope_usd(plan: dict, max_duration_seconds: int) -> Decimal:
    """Whole-session USD funding bound reserved before any provider I/O."""
    basis = plan["billing_basis"]
    if basis == "tokens":
        return Decimal(plan["reservation_usd"])
    seconds = Decimal(max_duration_seconds)
    try:
        return sum((_duration_seconds_cost(plan, family, seconds) for family in plan["duration_rates"]), Decimal(0))
    except DurableRunProviderResultUnknown as exc:
        raise DurableRunInputError("exact configured realtime price is unavailable") from exc


def _checkpoint(meter, basis: str) -> dict:
    """Canonical observed usage persisted before any settlement decision."""
    usage: dict = {"billing_basis": basis, "input_bytes": meter.input_bytes, "output_bytes": meter.output_bytes,
                   "input_sample_rate_hz": meter.input_sample_rate_hz,
                   "output_sample_rate_hz": meter.output_sample_rate_hz}
    if basis == "session":
        seconds = meter.connected_seconds
        usage["connected_seconds"] = format(seconds, "f") if isinstance(seconds, Decimal) else None
    if basis == "tokens":
        usage["usage_state"] = meter.usage_state
        usage["usage"] = meter.usage.as_usage_dict() if meter.usage is not None else None
    return usage


def _duration_bill(run: ChatRun, snapshot: dict, usage: dict):
    duration = snapshot["max_duration_seconds"]
    components = []
    costs = []
    summary = {}
    for family, kind, summary_key, size, rate in (
        ("realtime_input", "audio_input_seconds", "input_audio_seconds",
         usage.get("input_bytes"), usage.get("input_sample_rate_hz")),
        ("realtime_output", "audio_output_seconds", "output_audio_seconds",
         usage.get("output_bytes"), usage.get("output_sample_rate_hz")),
    ):
        if type(size) is not int or type(rate) is not int or rate not in {16000, 24000} or size < 0 or size > 2 * rate * duration:
            raise DurableRunProviderResultUnknown("realtime duration exceeds frozen reservation")
        seconds = Decimal(size) / Decimal(2 * rate)
        cost = _duration_seconds_cost(snapshot, family, seconds)
        frozen = snapshot["duration_rates"][family]
        costs.append(cost)
        summary[summary_key] = format(seconds, "f")
        components.append({"segment_id": _SEGMENT, "kind": kind, "quantity": format(seconds, "f"), "unit": "second",
            "unit_price_usd": format(Decimal(frozen["rate"]) / frozen["unit_seconds"], "f"),
            "cost_usd": format(cost, "f"), "source": "media", "model_name": run.model_name,
            "metadata": {"billing_basis": "duration", "rate_unit_seconds": frozen["unit_seconds"]}})
    usage_cost = UsageCost(raw_cost=costs[0] + costs[1], input_cost=costs[0], output_cost=costs[1],
                           pricing_status="priced", pricing_snapshot=snapshot)
    return usage_cost, components, None, summary


def _session_bill(run: ChatRun, snapshot: dict, usage: dict):
    raw = usage.get("connected_seconds")
    try:
        seconds = Decimal(raw) if isinstance(raw, str) else None
    except ArithmeticError:
        seconds = None
    limit = Decimal(snapshot["max_duration_seconds"])
    if seconds is None or not seconds.is_finite() or seconds < 0 or seconds > limit:
        raise DurableRunProviderResultUnknown("realtime session time exceeds frozen reservation")
    cost = _duration_seconds_cost(snapshot, "realtime_session", seconds)
    frozen = snapshot["duration_rates"]["realtime_session"]
    components = [{"segment_id": _SEGMENT, "kind": _SESSION_KIND, "quantity": format(seconds, "f"), "unit": "second",
        "unit_price_usd": format(Decimal(frozen["rate"]) / frozen["unit_seconds"], "f"),
        "cost_usd": format(cost, "f"), "source": "media", "model_name": run.model_name,
        "metadata": {"billing_basis": "session", "rate_unit_seconds": frozen["unit_seconds"]}}]
    usage_cost = UsageCost(raw_cost=cost, input_cost=cost, output_cost=Decimal(0),
                           pricing_status="priced", pricing_snapshot=snapshot)
    return usage_cost, components, None, {"session_seconds": format(seconds, "f")}


def _token_bill(run: ChatRun, snapshot: dict, usage: dict):
    """Price provider-reported tokens with admission-frozen rates; anything uncertain stays held."""
    if usage.get("usage_state") != "complete":
        raise DurableRunProviderResultUnknown("realtime provider token usage is incomplete")
    observed = usage.get("usage")
    required: tuple[str, ...] = ()
    if observed is None:
        # A truly idle session can be free; sent input without completion usage is uncertain.
        if usage.get("input_bytes") != 0 or usage.get("output_bytes") != 0:
            raise DurableRunProviderResultUnknown("realtime provider token usage is missing")
        breakdown = UsageBreakdown.from_totals(0, 0)
    else:
        try:
            breakdown = UsageBreakdown.from_canonical(observed)
        except ValueError as exc:
            raise DurableRunProviderResultUnknown("realtime provider token usage is invalid") from exc
        # Observed PCM in a direction demands the provider's audio share for it.
        required = tuple(name for name, size in (("audio_input", usage.get("input_bytes")),
                                                 ("audio_output", usage.get("output_bytes"))) if size)
    try:
        usage_cost = credit.usage_cost_from_pricing_snapshot(snapshot["token_pricing"],
            prompt_tokens=breakdown.input_tokens, completion_tokens=breakdown.output_tokens,
            breakdown=breakdown, required_modalities=required)
        components = credit.token_usage_components(usage_cost, segment_id=_SEGMENT, source="media",
            model_name=run.model_name, metadata={"billing_basis": "tokens"})
    except ValueError as exc:
        raise DurableRunProviderResultUnknown("realtime provider token usage cannot be priced") from exc
    if usage_cost.pricing_status != "priced" or usage_cost.raw_cost > Decimal(snapshot["reservation_usd"]):
        raise DurableRunProviderResultUnknown("realtime token usage exceeds the frozen reservation")
    return usage_cost, components, breakdown, {"input_tokens": str(breakdown.input_tokens),
                                               "output_tokens": str(breakdown.output_tokens)}


_BILLS = {"duration": _duration_bill, "session": _session_bill, "tokens": _token_bill}


def _intent(request: dict) -> dict:
    return {"model_id": request.get("model_id"), "provider_id": request.get("provider_id"),
            "voice": request.get("voice"), "instructions": request.get("instructions"),
            "max_duration_seconds": request.get("max_duration_seconds", 300)}


def _ticket_key(run_id: str) -> str:
    return f"lumen:realtime:ticket:{run_id}"


async def _issue_ticket(run_id: str, *, user_id: str, project_id: str) -> str:
    token = secrets.token_urlsafe(32)
    value = json.dumps({"digest": hashlib.sha256(token.encode()).hexdigest(), "user_id": user_id,
                        "project_id": project_id})
    try:
        redis = await _get_redis()
        await redis.set(_ticket_key(run_id), value, ex=_TICKET_TTL)
    except Exception as exc:
        raise DurableRunError("realtime ticket storage is unavailable") from exc
    return token


async def consume_ticket(run_id: str, token: str) -> tuple[str, str]:
    """Atomic single-use ticket consumption precedes any provider connection."""
    if not isinstance(token, str) or len(token) > 128 or len(token) < 32:
        raise DurableRunNotFound("realtime ticket is unavailable")
    try:
        redis = await _get_redis()
        stored = await redis.eval(_COMPARE_CONSUME, 1, _ticket_key(run_id), hashlib.sha256(token.encode()).hexdigest())
    except Exception as exc:
        raise DurableRunError("realtime ticket storage is unavailable") from exc
    if not stored:
        raise DurableRunNotFound("realtime ticket is unavailable")
    try:
        value = json.loads(stored)
    except (TypeError, ValueError) as exc:
        raise DurableRunNotFound("realtime ticket is unavailable") from exc
    if not isinstance(value.get("user_id"), str) or not isinstance(value.get("project_id"), str):
        raise DurableRunNotFound("realtime ticket is unavailable")
    return value["user_id"], value["project_id"]


async def admit_realtime_session(
    request: dict, *, project_id: str, user_id: str, client_request_id: str,
    source: str = "web", api_key_id: int | None = None,
) -> dict:
    from .admission import _lock_run_configurations, existing_run_for_intent

    intent = _intent(request)
    previous = await existing_run_for_intent(project_id=project_id, user_id=user_id,
        client_request_id=client_request_id, intent=intent, conversation_id=None)
    if previous is not None:
        if previous.run_kind != "realtime" or previous.status != "queued":
            raise DurableRunConflict("realtime session already connected or completed")
        async with _factory()() as session:
            row = (await session.execute(select(ChatRun).where(ChatRun.id == previous.run_id))).scalar_one()
            return await _response(row, user_id=user_id, project_id=project_id)
    model_id, provider_id = intent["model_id"], intent["provider_id"]
    if not isinstance(model_id, str) or not model_id or (provider_id is not None and not isinstance(provider_id, (str, int))):
        raise DurableRunInputError("realtime model is required")
    provider_number = int(provider_id) if provider_id is not None and str(provider_id).isdecimal() else None
    provider_type = str(provider_id) if provider_id is not None and provider_number is None else None
    try:
        route = (await routing.resolve_model_by_id(int(model_id), model_kind="realtime") if model_id.isdecimal()
                 else await routing.resolve_api_model(model_id, provider=provider_type,
                                                       provider_id=provider_number, model_kind="realtime"))
    except AmbiguousModelRouteError as exc:
        raise DurableRunInputError("realtime provider selection is ambiguous") from exc
    if route is None or (provider_number is not None and route["provider_id"] != provider_number) or (
        provider_type is not None and route["provider_type"] != provider_type
    ):
        raise DurableRunInputError("realtime provider or model is unavailable")
    voices = realtime_transport.available_realtime_options(route)
    voice = intent["voice"] or voices["default_voice"]
    plan = realtime_transport.validate_realtime_request(route, voice=voice,
        instructions=intent["instructions"], max_duration_seconds=intent["max_duration_seconds"])
    payload = {**intent, "voice": voice}
    await credit.precheck(user_id, project_id, api_key_id)
    per_usd = Decimal(str(get_settings().chat_credit_per_usd))
    margin = Decimal(str(route["margin_multiplier"]))
    envelope = _envelope_usd(plan, intent["max_duration_seconds"])
    bound = credit.credits_for_cost(envelope, margin, per_usd)
    if bound <= 0 or bound >= Decimal("10000000000"):
        raise DurableRunInputError("realtime credit reservation is invalid")
    capability = {key: route[key] for key in ("provider_id", "model_id", "provider_name", "provider_type", "model_name", "model_kind", "config_version_hash")}
    capability["effective_features"] = {}
    pricing = {**plan, "max_duration_seconds": intent["max_duration_seconds"],
               "reservation_usd": format(envelope, "f"), "bound_credits": format(bound, "f"),
               "margin_multiplier": format(margin, "f"), "credit_per_usd": format(per_usd, "f")}
    try:
        async with _factory()() as session, session.begin():
            existing = (await session.execute(select(ChatRun).where(ChatRun.project_id == project_id,
                ChatRun.user_id == user_id, ChatRun.client_request_id == client_request_id).with_for_update())).scalar_one_or_none()
            if existing is not None:
                if existing.request_fingerprint != _fingerprint(intent):
                    raise DurableRunConflict("idempotency_key_reused_with_different_intent")
                if existing.run_kind != "realtime" or existing.status != "queued":
                    raise DurableRunConflict("realtime session already connected or completed")
                run = existing
            else:
                await _lock_run_configurations(session, capability, model_name=route["model_name"])
                run = ChatRun(id=str(uuid.uuid4()), run_scope="realtime", run_kind="realtime",
                    project_id=project_id, user_id=user_id, model_name=route["model_name"], source=source,
                    api_key_id=api_key_id, client_request_id=client_request_id, request_fingerprint=_fingerprint(intent),
                    fingerprint_version=1, execution_protocol_version=1, capability_snapshot=capability,
                    pricing_snapshot=pricing, request_payload=encrypt_chat_content(json.dumps(payload)),
                    status="queued", last_seq=0, current_ordinal=0, reserved_credits=bound)
                session.add(run)
                session.add(ChatRunProvider(run_id=run.id, purpose="executor", provider_id=route["provider_id"],
                    model_id=route["model_id"], provider_label=route["provider_name"],
                    model_label=route["model_name"], config_version_hash=route["config_version_hash"]))
                await append_event(session, run, _event(run, "run.started", {"conversation_id": None,
                    "temp_thread_id": None, "model_name": run.model_name, "effective_features": {}, "run_kind": "realtime"}))
                await append_event(session, run, _event(run, "run.stage.changed", {"stage": "queued"}))
    except IntegrityError:
        previous = await existing_run_for_intent(project_id=project_id, user_id=user_id,
            client_request_id=client_request_id, intent=intent, conversation_id=None)
        if previous is None or previous.status != "queued" or previous.run_kind != "realtime":
            raise DurableRunConflict("realtime session already connected or completed")
        async with _factory()() as session:
            run = (await session.execute(select(ChatRun).where(ChatRun.id == previous.run_id))).scalar_one()
            return await _response(run, user_id=user_id, project_id=project_id)
    return await _response(run, user_id=user_id, project_id=project_id)


async def _response(run: ChatRun, *, user_id: str, project_id: str) -> dict:
    token = await _issue_ticket(run.id, user_id=user_id, project_id=project_id)
    return {"session_id": run.id, "status": "ready", "model_name": run.model_name,
            "provider_type": run.capability_snapshot["provider_type"], "expires_in_seconds": _TICKET_TTL,
            "connect_token": token}


async def _start(run_id: str, *, owner: str, user_id: str, project_id: str) -> tuple[dict, dict, dict, str]:
    async def transaction():
        async with _factory()() as session, session.begin():
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
            run = await claim_queued_run(session, run_id, owner=owner, lease_seconds=45)
            if run is None or run.user_id != user_id or run.project_id != project_id:
                raise DurableRunNotFound("realtime session is unavailable")
            owner_fenced = run.lease_owner
            segment = await prepare_segment(session, run, segment_id=_SEGMENT, ordinal=1, endpoint="realtime_voice")
            await session.flush()
            if segment.status != "prepared":
                raise DurableRunProviderResultUnknown("realtime session cannot be resumed")
            await credit.reserve_media_credit_in_transaction(session, user_id=run.user_id,
                project_id=run.project_id, api_key_id=run.api_key_id,
                bound=Decimal(run.pricing_snapshot["bound_credits"]))
            session.add(ChatModelCallReservation(run_id=run.id, segment_id=_SEGMENT,
                bound_credits=Decimal(run.pricing_snapshot["bound_credits"]), status="reserved"))
            begin_segment_io(segment)
            run.provider_started_at = _now()
            await append_event(session, run, _event(run, "run.stage.changed", {"stage": "model_request"}))
            return run.capability_snapshot, run.pricing_snapshot, _payload(run), owner_fenced
    return await budgets.retry_deadlocks(transaction)


async def _lock_settlement_rows(session, run_id: str, owner: str):
    run = (await session.execute(select(ChatRun).where(ChatRun.id == run_id)
        .with_for_update().execution_options(populate_existing=True))).scalar_one()
    if run.status != "running" or run.lease_owner != owner:
        raise DurableRunProviderResultUnknown("realtime lease was lost")
    segment = (await session.execute(select(ChatRunSegment).where(ChatRunSegment.run_id == run_id,
        ChatRunSegment.segment_id == _SEGMENT).with_for_update().execution_options(populate_existing=True))).scalar_one()
    reservation = (await session.execute(select(ChatModelCallReservation).where(
        ChatModelCallReservation.run_id == run_id, ChatModelCallReservation.segment_id == _SEGMENT)
        .with_for_update().execution_options(populate_existing=True))).scalar_one()
    return run, segment, reservation


async def _checkpoint_usage(run_id: str, *, owner: str, meter) -> None:
    """Commit observed usage on the still-started segment before any settlement decision.

    The segment stays ``provider_started``: a crash or an unknown outcome fails it closed
    (keeping this evidence for audited resolution) and the hold stays unknown.
    """
    async def transaction():
        async with _factory()() as session, session.begin():
            run, segment, reservation = await _lock_settlement_rows(session, run_id, owner)
            if reservation.status == "settled":
                return
            if segment.status != "provider_started" or reservation.status != "reserved":
                raise DurableRunProviderResultUnknown("realtime measurement cannot be checkpointed")
            if segment.usage_payload is not None:
                return
            record_segment_usage(segment, _checkpoint(meter, _pricing_snapshot(run.pricing_snapshot)["billing_basis"]))
    await budgets.retry_deadlocks(transaction)


async def _settle(run_id: str, *, owner: str, meter) -> dict[str, str]:
    """Checkpoint observed usage, then price it from the checkpoint and debit the ledger once."""
    await _checkpoint_usage(run_id, owner=owner, meter=meter)

    async def transaction():
        async with _factory()() as session, session.begin():
            run, segment, reservation = await _lock_settlement_rows(session, run_id, owner)
            if reservation.status == "settled":
                return {}
            if segment.status != "provider_started" or reservation.status != "reserved":
                raise DurableRunProviderResultUnknown("realtime measurement cannot be settled")
            usage = load_segment_payload(segment.usage_payload)
            snapshot = _pricing_snapshot(run.pricing_snapshot)
            bill = _BILLS.get(snapshot.get("billing_basis")) if isinstance(usage, dict) else None
            if bill is None or usage.get("billing_basis") != snapshot.get("billing_basis"):
                raise DurableRunProviderResultUnknown("realtime usage checkpoint is unavailable")
            usage_cost, components, breakdown, summary = bill(run, snapshot, usage)
            complete_segment_io(segment, result_payload={"closed": True}, usage_payload=usage)
            prompt_tokens = breakdown.input_tokens if breakdown is not None else 0
            completion_tokens = breakdown.output_tokens if breakdown is not None else 0
            credited = await credit.apply_usage_in_transaction(session, event_id=f"run:{run.id}",
                user_id=run.user_id, project_id=run.project_id, model_name=run.model_name,
                provider=run.capability_snapshot["provider_name"], prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens, usage_cost=usage_cost,
                margin_multiplier=Decimal(snapshot["margin_multiplier"]),
                credit_per_usd=Decimal(snapshot["credit_per_usd"]), source=run.source,
                api_key_id=run.api_key_id, run_id=run.id, usage_components=components, breakdown=breakdown)
            if credited > Decimal(str(reservation.bound_credits)):
                # Rolls back the debit; the committed checkpoint and the unknown hold remain.
                raise DurableRunProviderResultUnknown("realtime usage exceeds the reserved envelope")
            reservation.actual_credits = credited
            reservation.status = "settled"
            reservation.settled_at = _now()
            run.usage_reconciled_at = _now()
            await append_event(session, run, _event(run, "usage.updated", {"components": components,
                "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                "raw_cost": format(usage_cost.raw_cost, "f"), "credited_cost": format(credited, "f")}))
            return summary
    return await budgets.retry_deadlocks(transaction)


async def run_realtime_session(websocket, *, run_id: str, token: str, wire: str = "native") -> None:
    """Consume ticket, own the fenced run, relay, settle, and terminalize on disconnect."""
    from lumen.services.providers.realtime_protocol import relay_audio

    from .execution import _finish, logger

    user_id, project_id = await consume_ticket(run_id, token)
    capability, pricing, payload, owner = await _start(run_id, owner=f"realtime:{uuid.uuid4()}",
        user_id=user_id, project_id=project_id)
    route = await routing.resolve_model_snapshot(capability)
    if route is None:
        await _finish(run_id, status="failed", message_id=None, owner=owner,
                      error_code="provider_result_unknown", safe_message="realtime provider changed before connection")
        raise DurableRunProviderResultUnknown("realtime provider changed before connection")
    try:
        current_plan = realtime_transport.validate_realtime_request(route, voice=payload["voice"],
            instructions=payload["instructions"], max_duration_seconds=pricing["max_duration_seconds"])
    except ProviderValidationError:
        current_plan = None
    if not _plan_matches(pricing, current_plan):
        await _finish(run_id, status="failed", message_id=None, owner=owner,
                      error_code="provider_result_unknown", safe_message="realtime price changed before connection")
        raise DurableRunProviderResultUnknown("realtime price changed before connection")
    stop = asyncio.Event()

    async def heartbeat():
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=10)
            except TimeoutError:
                if not await _renew_lease(run_id, owner):
                    stop.set()

    async def is_canceled() -> bool:
        if stop.is_set():
            return True
        async with _factory()() as session:
            state = (await session.execute(select(ChatRun.status, ChatRun.lease_owner, ChatRun.cancel_requested_at)
                .where(ChatRun.id == run_id))).one_or_none()
        return state is None or state[0] != "running" or state[1] != owner or state[2] is not None

    heartbeat_task = asyncio.create_task(heartbeat())
    try:
        meter = await relay_audio(websocket, route, session_id=run_id, voice=payload["voice"],
            instructions=payload["instructions"], max_duration_seconds=pricing["max_duration_seconds"],
            wire=wire, is_canceled=is_canceled, token_usage=pricing.get("billing_basis", "duration") == "tokens",
            session_time=pricing.get("billing_basis", "duration") == "session")
        summary = await _settle(run_id, owner=owner, meter=meter)
        await _finish(run_id, status="canceled" if await is_canceled() else "completed", message_id=None, owner=owner)
        if wire == "native":
            try:
                await websocket.send_json({"type": "session.closed", **summary})
            except Exception:
                pass
    except Exception as exc:
        # Upstream WebSocket exceptions may contain Gemini's secret-bearing ?key= URL.
        logger.error("realtime relay failed run_id=%s", run_id)
        partial = getattr(exc, "meter", None)
        if partial is not None:
            try:
                await _checkpoint_usage(run_id, owner=owner, meter=partial)
            except Exception:
                logger.error("realtime partial usage checkpoint deferred run_id=%s", run_id)
        try:
            await _finish(run_id, status="failed", message_id=None, owner=owner,
                error_code="provider_result_unknown", safe_message="realtime provider usage cannot safely be confirmed")
        except Exception:
            logger.exception("realtime finalization deferred run_id=%s", run_id)
        raise
    finally:
        stop.set()
        heartbeat_task.cancel()
        await asyncio.gather(heartbeat_task, return_exceptions=True)
