"""Finite speech runs: owned assets, frozen explicit billing and one-way provider I/O."""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from lumen.config import get_settings
from lumen.crypto import encrypt_chat_content
from lumen.models.chat_assets import ChatAsset, ChatRunAsset
from lumen.models.chat_runs import ChatModelCallReservation, ChatRun, ChatRunProvider, ChatRunSegment
from lumen.services import assets, credit
from lumen.services.litellm_client import UsageCost
from lumen.services.providers import audio_transport, routing
from lumen.services.providers.errors import AmbiguousModelRouteError, ProviderValidationError
from lumen.services.providers.pricing import (
    duration_cost_usd,
    exact_duration_price,
    exact_media_price,
    frozen_token_pricing,
    media_billing_basis,
    media_reservation_usd,
)
from lumen.services.run_store import (
    append_event,
    begin_segment_io,
    complete_segment_io,
    load_segment_payload,
    prepare_segment,
    record_segment_usage,
)
from lumen.services.usage_breakdown import UsageBreakdown

from . import budgets
from .common import _event, _factory, _fingerprint, _now, descriptor, wake_run
from .errors import (
    DurableRunConflict,
    DurableRunError,
    DurableRunInputError,
    DurableRunNotFound,
    DurableRunProviderResultUnknown,
)
from .lifecycle import _cancel_requested, _require_owned_running_lease
from .media import (
    IO_BLOCKED,
    MediaAuthorizationRevoked,
    authorize_media_in_transaction,
    confirmed_media_rejection,
    lock_media_run_for_io,
    lock_media_source_in_transaction,
    media_io_allowed,
    validate_batch_media_io_in_transaction,
)

_MAX_AUDIO_BYTES = 25 * 1024 * 1024
_MAX_DURATION_MS = 30 * 60 * 1000
_MAX_TEXT = 4096
_MAX_TRANSCRIPT = 65536


def _intent(request: dict) -> dict:
    kind = request.get("kind")
    if kind == "tts":
        return {"kind": kind, "model_id": request.get("model_id"), "provider_id": request.get("provider_id"),
                "input": request.get("input"), "voice": request.get("voice"),
                "response_format": request.get("response_format", "mp3")}
    if kind == "stt":
        intent = {"kind": kind, "model_id": request.get("model_id"), "provider_id": request.get("provider_id"),
                  "input_asset_id": request.get("input_asset_id"), "language": request.get("language"),
                  "prompt": request.get("prompt")}
        try:
            granularities = audio_transport.normalize_timestamp_granularities(request.get("timestamp_granularities"))
        except ProviderValidationError as exc:
            raise DurableRunInputError(str(exc)) from exc
        # Untimed requests keep the legacy intent bytes and fingerprint; explicit
        # timing is part of the immutable intent so a reused key cannot switch it.
        if granularities:
            intent["timestamp_granularities"] = list(granularities)
        return intent
    raise DurableRunInputError("unsupported audio operation")


def _timed_segments(segments: object, text: str) -> list[dict] | None:
    """Revalidate provider timing at checkpoint, settlement and projection; None if invalid."""
    try:
        parsed = audio_transport._transcript_segments(segments, text)
    except audio_transport.AudioTransportError:
        return None
    return [{"start": item.start, "end": item.end, "text": item.text} for item in parsed]


def _freeze_pricing(route: dict, *, kind: str, duration_ms: int, input_text: str | None,
                    margin: Decimal, per_usd: Decimal) -> dict:
    media = route.get("media_pricing")
    basis = media_billing_basis(kind, media)
    pricing = {"billing_basis": basis, "media_pricing": media, "max_duration_ms": duration_ms,
               "margin_multiplier": str(margin), "credit_per_usd": str(per_usd)}
    if basis == "tokens":
        pricing.update(frozen_token_pricing(route))
        pricing["reservation_usd"] = str(media_reservation_usd(media))
    elif basis == "characters":
        pricing["usd_per_character"] = str(exact_media_price(media, "audio_per_character"))
        pricing["input_characters"] = len(input_text)
    else:
        family = "audio_output" if kind == "tts" else "audio_input"
        amount, unit_seconds = exact_duration_price(media, family)
        pricing.update({"duration_family": family, "duration_price_usd": str(amount), "duration_unit_seconds": unit_seconds})
    return pricing


def _duration_cost(pricing: dict, duration_ms: int) -> Decimal:
    # The shared helper multiplies before dividing and rounds once. Snapshots
    # frozen before explicit duration units keep their persisted per-second rate.
    if "duration_family" in pricing:
        cost = duration_cost_usd(pricing["media_pricing"], pricing["duration_family"], Decimal(duration_ms) / 1000)
        if cost is None:
            raise DurableRunProviderResultUnknown("frozen audio duration price is unavailable")
        return cost
    return Decimal(pricing["usd_per_second"]) * duration_ms / 1000


def _reservation_usd(pricing: dict) -> Decimal:
    basis = pricing.get("billing_basis", "duration")
    if basis == "tokens":
        amount = Decimal(pricing["reservation_usd"])
        if not amount.is_finite() or amount <= 0:
            raise DurableRunInputError("audio token reservation is invalid")
        return amount
    if basis == "characters":
        return Decimal(pricing["usd_per_character"]) * pricing["input_characters"]
    return _duration_cost(pricing, pricing["max_duration_ms"])


def _bill(pricing: dict, *, kind: str, usage: dict, model_name: str) -> tuple[UsageCost, UsageBreakdown | None, list[dict]]:
    basis = pricing.get("billing_basis", "duration")
    if basis == "tokens":
        try:
            breakdown = UsageBreakdown.from_canonical(usage.get("tokens"))
            direction = "output" if kind == "tts" else "input"
            if not breakdown.reported("audio", direction):
                raise ValueError("provider did not report audio tokens")
            cost = credit.usage_cost_from_pricing_snapshot(pricing, prompt_tokens=breakdown.input_tokens,
                completion_tokens=breakdown.output_tokens, breakdown=breakdown,
                required_modalities=(f"audio_{direction}",))
        except ValueError as exc:
            raise DurableRunProviderResultUnknown("audio token usage is unavailable or invalid") from exc
        if cost.pricing_status != "priced" or cost.raw_cost > _reservation_usd(pricing):
            raise DurableRunProviderResultUnknown("audio token usage exceeds frozen pricing envelope")
        components = credit.token_usage_components(cost, segment_id=f"{kind}:1", source="media",
            model_name=model_name, metadata={})
        return cost, breakdown, components
    if basis == "characters":
        quantity = pricing["input_characters"]
        if type(quantity) is not int or quantity <= 0 or usage.get("input_characters") != quantity:
            raise DurableRunProviderResultUnknown("audio character usage is unavailable")
        price = Decimal(pricing["usd_per_character"])
        total = price * quantity
        unit, component_kind = "character", "audio_input_characters"
    else:
        quantity = Decimal(usage["duration_ms"]) / 1000
        total = _duration_cost(pricing, usage["duration_ms"])
        price = (Decimal(pricing["duration_price_usd"]) / pricing["duration_unit_seconds"]
                 if "duration_family" in pricing else Decimal(pricing["usd_per_second"]))
        unit, component_kind = "second", "audio_output_seconds" if kind == "tts" else "audio_input_seconds"
    component = {"segment_id": f"{kind}:1", "kind": component_kind, "quantity": str(quantity), "unit": unit,
                 "unit_price_usd": str(price), "cost_usd": str(total), "source": "media", "model_name": model_name,
                 "metadata": {"duration_price_usd": pricing.get("duration_price_usd"),
                              "duration_unit_seconds": pricing.get("duration_unit_seconds")} if basis == "duration" else {}}
    cost = UsageCost(raw_cost=total, input_cost=total if kind == "stt" else Decimal(0),
        output_cost=total if kind == "tts" else Decimal(0), pricing_status="priced", pricing_snapshot=pricing)
    return cost, None, [component]


@dataclass(frozen=True)
class PreparedAudioRun:
    payload: dict
    capability_snapshot: dict
    pricing_snapshot: dict
    required_scopes: tuple[str, ...]
    source_asset_id: str | None
    project_id: str
    user_id: str


async def prepare_audio_run(request: dict, *, project_id: str, user_id: str,
                            source: str = "web", api_key_id: int | None = None,
                            operation: str | None = None,
                            required_scopes: tuple[str, ...] | None = None) -> PreparedAudioRun:
    """Freeze speech/transcription without inference or downloading source bytes."""
    if operation is not None:
        if operation not in {"audio.speech", "audio.transcriptions"}:
            raise DurableRunInputError("unsupported audio operation")
        request = {**request, "kind": "tts" if operation == "audio.speech" else "stt"}
    intent = _intent(request)
    kind = intent["kind"]
    model_id, provider_id = intent["model_id"], intent["provider_id"]
    if not isinstance(model_id, str) or not model_id or (provider_id is not None and not isinstance(provider_id, (str, int))):
        raise DurableRunInputError("audio model is required")
    provider_number = int(provider_id) if provider_id is not None and str(provider_id).isdecimal() else None
    api_provider = str(provider_id) if provider_id is not None and provider_number is None else None
    try:
        route = (await routing.resolve_model_by_id(int(model_id), model_kind=kind) if model_id.isdecimal()
                 else await routing.resolve_api_model(model_id, provider=api_provider,
                                                       provider_id=provider_number, model_kind=kind))
    except AmbiguousModelRouteError as exc:
        raise DurableRunInputError("audio provider selection is ambiguous") from exc
    if route is None or (provider_number is not None and route["provider_id"] != provider_number) or (
            api_provider is not None and route["api_provider"] != api_provider):
        raise DurableRunInputError("audio provider or model is unavailable")
    if kind == "tts":
        if not isinstance(intent["input"], str) or not intent["input"].strip() or len(intent["input"]) > audio_transport._MAX_TEXT_CHARS:
            raise DurableRunInputError("speech input is empty or too long")
        if not isinstance(intent["voice"], str) or not 0 < len(intent["voice"]) <= 190:
            raise DurableRunInputError("speech voice is required")
        voices = audio_transport._OPENAI_VOICES if route["provider_type"] == "openai" else audio_transport._GEMINI_VOICES
        if intent["voice"] not in voices:
            raise DurableRunInputError("unsupported speech voice")
    else:
        if not isinstance(intent["input_asset_id"], str):
            raise DurableRunInputError("input_asset_id is required")
        if intent["language"] is not None and (not isinstance(intent["language"], str)
            or not intent["language"].isascii() or not intent["language"].replace("-", "").isalpha()
            or len(intent["language"]) > 16):
            raise DurableRunInputError("invalid transcription language")
        if intent["prompt"] is not None and (not isinstance(intent["prompt"], str) or len(intent["prompt"]) > 1024):
            raise DurableRunInputError("invalid transcription prompt")
    audio_transport.validate_audio_request(route, kind=kind,
        format=intent["response_format"] if kind == "tts" else None,
        timestamp_granularities=intent.get("timestamp_granularities"))
    if not assets.asset_pipeline_available():
        raise DurableRunInputError("audio asset storage or scanner is unavailable")
    duration_ms = _MAX_DURATION_MS
    if kind == "stt":
        asset = await assets.get_asset(asset_id=intent["input_asset_id"], user_id=user_id, project_id=project_id)
        duration_ms = _duration(asset)
        source_limit = (audio_transport._MAX_GEMINI_INLINE_BYTES if route["provider_type"] == "gemini"
                        else audio_transport._MAX_INPUT_BYTES)
        if asset["status"] != "clean" or asset["size_bytes"] > source_limit or asset["mime_type"] not in audio_transport._INPUT_FORMATS:
            raise DurableRunInputError("source audio exceeds provider input limits")
    await credit.precheck(user_id, project_id, api_key_id)
    per_usd = Decimal(str(get_settings().chat_credit_per_usd))
    margin = Decimal(str(route["margin_multiplier"]))
    pricing = _freeze_pricing(route, kind=kind, duration_ms=duration_ms, input_text=intent.get("input"), margin=margin, per_usd=per_usd)
    if kind == "stt":
        pricing["max_source_bytes"] = source_limit
    bound = credit.credits_for_cost(_reservation_usd(pricing), margin, per_usd)
    if bound <= 0 or bound >= Decimal("10000000000"):
        raise DurableRunInputError("audio credit reservation is invalid")
    pricing["bound_credits"] = format(bound, "f")
    capability = {key: route[key] for key in ("provider_id", "model_id", "provider_name", "model_name", "model_kind", "config_version_hash")}
    capability["effective_features"] = {}
    scopes = required_scopes or ("native:audio:write", *(("native:assets:read",) if kind == "stt" else ()))
    return PreparedAudioRun(intent, capability, json.loads(json.dumps(pricing)), scopes,
                            intent.get("input_asset_id"), project_id, user_id)


async def persist_audio_run_in_transaction(
    session: AsyncSession, prepared: PreparedAudioRun, *, project_id: str, user_id: str,
    client_request_id: str, source: str = "web", api_key_id: int | None = None,
    workload_class: str | None = None, batch_id: str | None = None,
) -> ChatRun:
    """Atomically bind a finite request in the caller's session; never commit, hold credit or wake.

    Default class is ``online_media``; a Batch caller passes ``workload_class="batch"``
    with its locked ``batch_id``. The pool is resolved from persisted configuration.
    """
    from lumen.services.worker_routing import resolve_worker_route

    from .admission import _lock_run_configurations

    if (prepared.project_id, prepared.user_id) != (project_id, user_id):
        raise DurableRunInputError("prepared audio owner changed")
    intent, capability, pricing = prepared.payload, prepared.capability_snapshot, prepared.pricing_snapshot
    kind = intent["kind"]
    existing = (await session.execute(select(ChatRun).where(ChatRun.project_id == project_id,
        ChatRun.user_id == user_id, ChatRun.client_request_id == client_request_id)
        .with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
    if existing is not None:
        if existing.request_fingerprint != _fingerprint(intent):
            raise DurableRunConflict("idempotency_key_reused_with_different_intent")
        return existing
    route = await resolve_worker_route(session, run_kind=kind, workload_class=workload_class, batch_id=batch_id)
    await _lock_run_configurations(session, capability, model_name=capability["model_name"])
    await authorize_media_in_transaction(session, user_id=user_id, project_id=project_id,
        api_key_id=api_key_id, required_scopes=prepared.required_scopes)
    run = ChatRun(id=str(uuid.uuid4()), run_scope="audio", run_kind=kind, project_id=project_id,
        user_id=user_id, model_name=capability["model_name"], source=source, api_key_id=api_key_id,
        workload_class=route.workload_class, worker_pool_id=route.worker_pool_id, batch_id=batch_id,
        client_request_id=client_request_id, request_fingerprint=_fingerprint(intent), fingerprint_version=1,
        execution_protocol_version=1, capability_snapshot=capability, pricing_snapshot=pricing,
        request_payload=encrypt_chat_content(json.dumps({**intent, "execution_protocol_version": 1,
                                                       "required_scopes": list(prepared.required_scopes)})),
        status="queued", last_seq=0, current_ordinal=0)
    session.add(run)
    if prepared.source_asset_id is not None:
        asset = await lock_media_source_in_transaction(session, asset_id=prepared.source_asset_id, user_id=user_id,
            project_id=project_id, run_kind=kind, pricing=pricing, batch_id=batch_id)
        session.add(ChatRunAsset(run_id=run.id, asset_id=asset.id, purpose="input"))
    session.add(ChatRunProvider(run_id=run.id, purpose="executor", provider_id=capability["provider_id"],
        model_id=capability["model_id"], provider_label=capability["provider_name"],
        model_label=capability["model_name"], config_version_hash=capability["config_version_hash"]))
    await append_event(session, run, _event(run, "run.started", {"conversation_id": None,
        "temp_thread_id": None, "model_name": run.model_name, "effective_features": {}, "run_kind": kind}))
    await append_event(session, run, _event(run, "run.stage.changed", {"stage": "queued"}))
    return run


async def admit_audio_run(request: dict, *, project_id: str, user_id: str, client_request_id: str,
                          source: str = "web", api_key_id: int | None = None,
                          required_scopes: tuple[str, ...] | None = None):
    from .admission import existing_run_for_intent

    intent = _intent(request)
    previous = await existing_run_for_intent(project_id=project_id, user_id=user_id,
        client_request_id=client_request_id, intent=intent, conversation_id=None)
    if previous is not None:
        return previous
    prepared = await prepare_audio_run(request, project_id=project_id, user_id=user_id, source=source,
                                       api_key_id=api_key_id, required_scopes=required_scopes)
    try:
        async with _factory()() as session, session.begin():
            run = await persist_audio_run_in_transaction(session, prepared, project_id=project_id, user_id=user_id,
                client_request_id=client_request_id, source=source, api_key_id=api_key_id)
            created = descriptor(run)
    except IntegrityError:
        previous = await existing_run_for_intent(project_id=project_id, user_id=user_id,
            client_request_id=client_request_id, intent=intent, conversation_id=None)
        if previous is None:
            raise
        return previous
    await wake_run(created.run_id)
    return created


def _duration(asset: dict) -> int:
    metadata = asset.get("media_metadata") or {}
    ms = metadata.get("duration_ms") if isinstance(metadata, dict) else None
    if (asset.get("mime_type") not in assets._AUDIO_MIMES or type(ms) is not int or ms <= 0
            or ms > _MAX_DURATION_MS or type(asset.get("size_bytes")) is not int
            or not 0 < asset["size_bytes"] <= _MAX_AUDIO_BYTES):
        raise DurableRunInputError("source audio must be a bounded scanned owned asset with duration")
    return ms


async def _read_source(asset_id: str, *, user_id: str, project_id: str, expected_ms: int,
                       max_bytes: int = _MAX_AUDIO_BYTES, run_id: str | None = None) -> tuple[bytes, str]:
    row = await assets.get_asset(asset_id=asset_id, user_id=user_id, project_id=project_id)
    # Only a Batch run's accepted input pin survives a later delete; online runs need clean.
    if row["status"] not in ({"clean", "deleting"} if run_id is not None else {"clean"}) or (
            _duration(row) != expected_ms or row["size_bytes"] > max_bytes):
        raise DurableRunInputError("source audio changed")
    opened = (await assets.open_run_input_download(asset_id=asset_id, run_id=run_id, user_id=user_id, project_id=project_id)
              if run_id is not None else await assets.open_download(asset_id=asset_id, user_id=user_id, project_id=project_id))
    if opened.size_bytes != row["size_bytes"] or opened.mime_type != row["mime_type"]:
        await asyncio.to_thread(opened.body.close)
        raise DurableRunInputError("source audio changed")
    data = bytearray()
    async for chunk in opened.chunks():
        if len(data) + len(chunk) > max_bytes:
            raise DurableRunInputError("source audio exceeds size limit")
        data.extend(chunk)
    if len(data) != opened.size_bytes:
        raise DurableRunInputError("source audio size changed")
    return bytes(data), opened.mime_type


async def _start(run_id: str, *, owner: str, bound: Decimal, kind: str) -> str:
    async def transaction():
        async with _factory()() as session, session.begin():
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
            run, blocked = await lock_media_run_for_io(session, run_id, owner=owner)
            segment = await prepare_segment(session, run, segment_id=f"{kind}:1", ordinal=1, endpoint=f"audio_{kind}")
            await session.flush()
            if segment.status == "completed":
                return "completed"
            if segment.status != "prepared":
                return "unknown"
            if blocked is not None:
                return blocked
            await validate_batch_media_io_in_transaction(session, run)
            if "bound_credits" in run.pricing_snapshot and bound != Decimal(run.pricing_snapshot["bound_credits"]):
                raise DurableRunInputError("audio credit reservation changed")
            await credit.reserve_call_credit_in_transaction(session, user_id=run.user_id,
                project_id=run.project_id, api_key_id=run.api_key_id, bound=bound)
            session.add(ChatModelCallReservation(run_id=run.id, segment_id=f"{kind}:1", bound_credits=bound, status="reserved"))
            run.reserved_credits = bound
            begin_segment_io(segment)
            run.provider_started_at = _now()
            await append_event(session, run, _event(run, "run.stage.changed", {"stage": "model_request"}))
            return "started"
    return await budgets.retry_deadlocks(transaction)


async def _checkpoint(run_id: str, *, owner: str, kind: str, result: dict | None, usage: dict) -> None:
    async with _factory()() as session, session.begin():
        run = (await session.execute(select(ChatRun).where(ChatRun.id == run_id).with_for_update())).scalar_one()
        _require_owned_running_lease(run, owner)
        segment = (await session.execute(select(ChatRunSegment).where(ChatRunSegment.run_id == run_id,
            ChatRunSegment.segment_id == f"{kind}:1").with_for_update())).scalar_one()
        if segment.status != "provider_started":
            raise DurableRunProviderResultUnknown("audio provider boundary is no longer owned")
        if result is None:
            record_segment_usage(segment, usage)
        else:
            complete_segment_io(segment, result_payload=result, usage_payload=usage)
            await append_event(session, run, _event(run, "run.stage.changed", {"stage": "model_response"}))


async def _settle(run_id: str, *, owner: str, kind: str) -> None:
    async def transaction():
        async with _factory()() as session, session.begin():
            run = (await session.execute(select(ChatRun).where(ChatRun.id == run_id)
                .with_for_update().execution_options(populate_existing=True))).scalar_one()
            _require_owned_running_lease(run, owner)
            segment = (await session.execute(select(ChatRunSegment).where(ChatRunSegment.run_id == run_id,
                ChatRunSegment.segment_id == f"{kind}:1").with_for_update())).scalar_one()
            result = load_segment_payload(segment.result_payload)
            usage = load_segment_payload(segment.usage_payload)
            if segment.status != "completed" or not isinstance(result, dict) or not isinstance(usage, dict):
                raise DurableRunProviderResultUnknown("audio result checkpoint is unavailable")
            duration_ms = usage.get("duration_ms")
            if type(duration_ms) is not int or not 0 < duration_ms <= run.pricing_snapshot["max_duration_ms"]:
                raise DurableRunProviderResultUnknown("audio duration exceeds frozen reservation")
            if kind == "tts":
                asset_id = result.get("asset_id")
                asset = (await session.execute(select(ChatAsset).join(ChatRunAsset, ChatRunAsset.asset_id == ChatAsset.id)
                    .where(ChatRunAsset.run_id == run_id, ChatRunAsset.purpose == "output", ChatAsset.id == asset_id,
                           ChatAsset.project_id == run.project_id, ChatAsset.user_id == run.user_id,
                           ChatAsset.status == "clean"))).scalar_one_or_none()
                if asset is None or _duration({"mime_type": asset.mime_type, "size_bytes": asset.size_bytes,
                                                "media_metadata": asset.media_metadata}) != duration_ms:
                    raise DurableRunProviderResultUnknown("output audio asset is unavailable")
            elif (not isinstance(result.get("text"), str) or len(result["text"]) > _MAX_TRANSCRIPT
                    or ("segments" in result and _timed_segments(result["segments"], result["text"]) is None)):
                raise DurableRunProviderResultUnknown("transcript checkpoint is unavailable")
            reservation = (await session.execute(select(ChatModelCallReservation).where(
                ChatModelCallReservation.run_id == run_id, ChatModelCallReservation.segment_id == f"{kind}:1")
                .with_for_update().execution_options(populate_existing=True))).scalar_one()
            if reservation.status == "settled":
                return
            if reservation.status != "reserved":
                raise DurableRunProviderResultUnknown("audio credit reservation is not payable")
            usage_cost, breakdown, components = _bill(run.pricing_snapshot, kind=kind, usage=usage, model_name=run.model_name)
            total = usage_cost.raw_cost
            prompt_tokens = breakdown.input_tokens if breakdown is not None else 0
            completion_tokens = breakdown.output_tokens if breakdown is not None else 0
            expected_credits = credit.credits_for_cost(total, Decimal(run.pricing_snapshot["margin_multiplier"]),
                Decimal(run.pricing_snapshot["credit_per_usd"]))
            if expected_credits > Decimal(str(reservation.bound_credits)):
                raise DurableRunProviderResultUnknown("audio usage exceeds reserved price")
            credited = await credit.apply_usage_in_transaction(session, event_id=f"run:{run.id}",
                user_id=run.user_id, project_id=run.project_id, model_name=run.model_name,
                provider=run.capability_snapshot["provider_name"], prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                usage_cost=usage_cost, breakdown=breakdown,
                margin_multiplier=Decimal(run.pricing_snapshot["margin_multiplier"]),
                credit_per_usd=Decimal(run.pricing_snapshot["credit_per_usd"]), source=run.source,
                api_key_id=run.api_key_id, run_id=run.id, usage_components=components)
            reservation.actual_credits = credited
            reservation.status = "settled"
            reservation.settled_at = _now()
            run.usage_reconciled_at = _now()
            await append_event(session, run, _event(run, "usage.updated", {"components": components,
                "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens, "raw_cost": str(total), "credited_cost": str(credited)}))
    await budgets.retry_deadlocks(transaction)


async def execute_audio_run(run_id: str, *, owner: str, payload: dict, capability_snapshot: dict, pricing_snapshot: dict) -> bool:
    from .lifecycle import _renew_lease
    stop = asyncio.Event()
    async def heartbeat():
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=10)
            except TimeoutError:
                if not await _renew_lease(run_id, owner):
                    return
    task = asyncio.create_task(heartbeat())
    try:
        return await _execute(run_id, owner=owner, payload=payload,
            capability_snapshot=capability_snapshot, pricing_snapshot=pricing_snapshot)
    finally:
        stop.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def _execute(run_id: str, *, owner: str, payload: dict, capability_snapshot: dict, pricing_snapshot: dict) -> bool:
    from .execution import _finish, logger
    kind = payload["kind"]
    async with _factory()() as session:
        persisted = (await session.execute(select(ChatRunSegment.status).where(ChatRunSegment.run_id == run_id,
            ChatRunSegment.segment_id == f"{kind}:1"))).scalar_one_or_none()
    if persisted in {"provider_started", "failed"}:
        await _finish(run_id, status="failed", message_id=None, owner=owner,
            error_code="provider_result_unknown", safe_message="audio provider result cannot safely be retried")
        return True
    try:
        if persisted != "completed":
            blocked, is_batch = await media_io_allowed(run_id, owner=owner)
            if blocked is not None:
                await _finish(run_id, status="canceled", message_id=None, owner=owner,
                    error_code=blocked, safe_message="audio run canceled")
                return True
            route = await routing.resolve_model_snapshot(capability_snapshot)
            if route is None:
                raise DurableRunInputError("audio provider configuration changed")
            # The resolver still enforces current configuration/route fences.
            # Prices used by validation and I/O come only from admission.
            route = {**route, "media_pricing": pricing_snapshot.get("media_pricing", route.get("media_pricing"))}
            for key in ("input_price_per_token", "output_price_per_token"):
                if key in pricing_snapshot:
                    route[key] = pricing_snapshot[key]
            audio_transport.validate_audio_request(route, kind=kind,
                format=payload["response_format"] if kind == "tts" else None,
                timestamp_granularities=payload.get("timestamp_granularities"))
            source = None
            if kind == "stt":
                source = await _read_source(payload["input_asset_id"], user_id=payload["user_id"],
                    project_id=payload["project_id"], expected_ms=pricing_snapshot["max_duration_ms"],
                    max_bytes=(audio_transport._MAX_GEMINI_INLINE_BYTES if route["provider_type"] == "gemini"
                               else audio_transport._MAX_INPUT_BYTES), run_id=run_id if is_batch else None)
            bound = credit.credits_for_cost(_reservation_usd(pricing_snapshot),
                Decimal(pricing_snapshot["margin_multiplier"]), Decimal(pricing_snapshot["credit_per_usd"]))
            state = await _start(run_id, owner=owner, bound=bound, kind=kind)
            if state in IO_BLOCKED:
                await _finish(run_id, status="canceled", message_id=None, owner=owner,
                    error_code=state, safe_message="audio run canceled")
                return True
            if state == "unknown":
                raise DurableRunProviderResultUnknown("audio provider result cannot be replayed")
            if state == "started":
                try:
                    if kind == "tts":
                        speech = await audio_transport.generate_speech(route, text=payload["input"],
                            voice=payload["voice"], format=payload["response_format"])
                    else:
                        data, mime = source
                        granularities = payload.get("timestamp_granularities")
                        # Untimed runs keep the legacy transport call exactly.
                        timing = {"timestamp_granularities": granularities} if granularities else {}
                        transcription = await audio_transport.transcribe_audio(route, data=data, mime_type=mime,
                            language=payload.get("language"), prompt=payload.get("prompt"), **timing)
                except audio_transport.AudioTransportError as exc:
                    # Batch only: a confirmed refusal settles at zero; anything else stays unknown.
                    if not is_batch or confirmed_media_rejection(exc) is None:
                        raise
                    await _finish(run_id, status="failed", message_id=None, owner=owner,
                        error_code="provider_rejected", safe_message="upstream provider rejected the request")
                    return True
                if kind == "tts":
                    data, mime, tokens = speech.data, speech.mime_type, speech.usage
                    usage = {"tokens": tokens.as_usage_dict() if tokens is not None else None}
                    if pricing_snapshot.get("billing_basis") == "characters":
                        usage["input_characters"] = len(payload["input"])
                    await _checkpoint(run_id, owner=owner, kind=kind, result=None, usage=usage)
                    stored = await assets.create_generated_asset_bytes(data=data,
                        original_name="speech." + payload["response_format"], media_type=mime,
                        user_id=payload["user_id"], project_id=payload["project_id"], run_id=run_id)
                    duration_ms = _duration({**stored, "media_metadata": stored.get("media_metadata")})
                    result = {"asset_id": stored["id"]}
                else:
                    text, tokens = transcription.text, transcription.usage
                    usage = {"duration_ms": pricing_snapshot["max_duration_ms"],
                             "tokens": tokens.as_usage_dict() if tokens is not None else None}
                    await _checkpoint(run_id, owner=owner, kind=kind, result=None, usage=usage)
                    if not isinstance(text, str) or len(text) > _MAX_TRANSCRIPT:
                        raise DurableRunProviderResultUnknown("provider transcript exceeds size limit")
                    duration_ms = pricing_snapshot["max_duration_ms"]
                    result = {"text": text}
                    if granularities:
                        # Requested timing is never downgraded to a plain transcript.
                        segments = transcription.segments
                        result["segments"] = _timed_segments([
                            {"start": item.start, "end": item.end, "text": item.text} for item in segments
                        ], text) if isinstance(segments, tuple) else None
                        if result["segments"] is None:
                            raise DurableRunProviderResultUnknown("provider transcript timing is invalid")
                usage["duration_ms"] = duration_ms
                await _checkpoint(run_id, owner=owner, kind=kind, result=result, usage=usage)
        await _settle(run_id, owner=owner, kind=kind)
        canceled = await _cancel_requested(run_id)
        await _finish(run_id, status="canceled" if canceled else "completed", message_id=None, owner=owner,
            error_code="canceled" if canceled else None, safe_message="audio run canceled" if canceled else None)
    except MediaAuthorizationRevoked:
        # Raised before any reservation or provider I/O committed: definite, not unknown.
        try:
            await _finish(run_id, status="failed", message_id=None, owner=owner,
                error_code="api_key_unauthorized", safe_message="audio request is no longer authorized")
        except Exception:
            logger.exception("audio run finalization deferred run_id=%s", run_id)
    except Exception:
        logger.exception("durable audio execution failed run_id=%s", run_id)
        try:
            await _finish(run_id, status="failed", message_id=None, owner=owner,
                error_code="provider_result_unknown", safe_message="audio provider result cannot safely be retried")
        except Exception:
            logger.exception("audio run finalization deferred run_id=%s", run_id)
    return True


async def audio_result(run_id: str, *, user_id: str, project_id: str) -> dict:
    async with _factory()() as session:
        run = (await session.execute(select(ChatRun).where(ChatRun.id == run_id, ChatRun.user_id == user_id,
            ChatRun.project_id == project_id, ChatRun.run_kind.in_(("tts", "stt"))))).scalar_one_or_none()
        if run is None:
            raise DurableRunNotFound("audio run not found")
        if run.status != "completed":
            raise DurableRunConflict("audio run is not completed")
        segment = (await session.execute(select(ChatRunSegment).where(ChatRunSegment.run_id == run_id,
            ChatRunSegment.segment_id == f"{run.run_kind}:1"))).scalar_one_or_none()
        result = load_segment_payload(segment.result_payload) if segment is not None and segment.status == "completed" else None
        if not isinstance(result, dict) or run.usage_reconciled_at is None:
            raise DurableRunError("audio result checkpoint is unavailable")
        if run.run_kind == "stt":
            if not isinstance(result.get("text"), str):
                raise DurableRunError("transcript is unavailable")
            if "segments" not in result:
                return {"kind": "stt", "text": result["text"]}
            segments = _timed_segments(result["segments"], result["text"])
            if segments is None:
                raise DurableRunError("transcript timing is unavailable")
            return {"kind": "stt", "text": result["text"], "segments": segments}
        row = (await session.execute(select(ChatAsset).join(ChatRunAsset, ChatRunAsset.asset_id == ChatAsset.id)
            .where(ChatRunAsset.run_id == run_id, ChatRunAsset.purpose == "output",
                   ChatAsset.id == result.get("asset_id"), ChatAsset.project_id == project_id,
                   ChatAsset.user_id == user_id, ChatAsset.status == "clean"))).scalar_one_or_none()
        if row is None:
            raise DurableRunError("speech output asset is unavailable")
        return {"kind": "tts", "asset_id": row.id, "mime_type": row.mime_type, "size_bytes": row.size_bytes}
