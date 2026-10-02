"""Finite speech runs: owned assets, frozen duration prices, and one-way provider I/O."""

from __future__ import annotations

import asyncio
import json
import uuid
from decimal import Decimal

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from lumen.config import get_settings
from lumen.crypto import encrypt_chat_content
from lumen.models.chat_assets import ChatAsset, ChatRunAsset
from lumen.models.chat_runs import ChatModelCallReservation, ChatRun, ChatRunProvider, ChatRunSegment
from lumen.services import assets, credit
from lumen.services.litellm_client import UsageCost
from lumen.services.providers import audio_transport, routing
from lumen.services.providers.errors import AmbiguousModelRouteError
from lumen.services.run_store import (
    append_event,
    begin_segment_io,
    complete_segment_io,
    load_segment_payload,
    prepare_segment,
)

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
        return {"kind": kind, "model_id": request.get("model_id"), "provider_id": request.get("provider_id"),
                "input_asset_id": request.get("input_asset_id"), "language": request.get("language"),
                "prompt": request.get("prompt")}
    raise DurableRunInputError("unsupported audio operation")


async def admit_audio_run(request: dict, *, project_id: str, user_id: str, client_request_id: str,
                          source: str = "web", api_key_id: int | None = None):
    from .admission import _lock_run_configurations, existing_run_for_intent

    intent = _intent(request)
    previous = await existing_run_for_intent(project_id=project_id, user_id=user_id,
        client_request_id=client_request_id, intent=intent, conversation_id=None)
    if previous is not None:
        return previous
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
    rate = audio_transport.validate_audio_request(route, kind=kind,
        format=intent["response_format"] if kind == "tts" else None)
    if not assets.asset_pipeline_available():
        raise DurableRunInputError("audio asset storage or scanner is unavailable")
    duration_ms = _MAX_DURATION_MS
    if kind == "stt":
        asset = await assets.get_asset(asset_id=intent["input_asset_id"], user_id=user_id, project_id=project_id)
        duration_ms = _duration(asset)
        source_limit = (audio_transport._MAX_GEMINI_INLINE_BYTES if route["provider_type"] == "gemini"
                        else audio_transport._MAX_INPUT_BYTES)
        if asset["size_bytes"] > source_limit or asset["mime_type"] not in audio_transport._INPUT_FORMATS:
            raise DurableRunInputError("source audio exceeds provider input limits")
    await credit.precheck(user_id, project_id, api_key_id)
    per_usd = Decimal(str(get_settings().chat_credit_per_usd))
    margin = Decimal(str(route["margin_multiplier"]))
    bound = credit.credits_for_cost(rate * Decimal(duration_ms) / 1000, margin, per_usd)
    if bound <= 0 or bound >= Decimal("10000000000"):
        raise DurableRunInputError("audio credit reservation is invalid")
    capability = {key: route[key] for key in ("provider_id", "model_id", "provider_name", "model_name", "model_kind", "config_version_hash")}
    capability["effective_features"] = {}
    pricing = {"usd_per_second": format(rate, "f"), "max_duration_ms": duration_ms,
               "margin_multiplier": format(margin, "f"), "credit_per_usd": format(per_usd, "f")}
    factory = _factory()
    try:
        async with factory() as session, session.begin():
            existing = (await session.execute(select(ChatRun).where(ChatRun.project_id == project_id,
                ChatRun.user_id == user_id, ChatRun.client_request_id == client_request_id).with_for_update())).scalar_one_or_none()
            if existing is not None:
                if existing.request_fingerprint != _fingerprint(intent):
                    raise DurableRunConflict("idempotency_key_reused_with_different_intent")
                return descriptor(existing)
            await _lock_run_configurations(session, capability, model_name=route["model_name"])
            run = ChatRun(id=str(uuid.uuid4()), run_scope="audio", run_kind=kind, project_id=project_id,
                user_id=user_id, model_name=route["model_name"], source=source, api_key_id=api_key_id,
                client_request_id=client_request_id, request_fingerprint=_fingerprint(intent), fingerprint_version=1,
                execution_protocol_version=1, capability_snapshot=capability, pricing_snapshot=pricing,
                request_payload=encrypt_chat_content(json.dumps({**intent, "execution_protocol_version": 1})),
                status="queued", last_seq=0, current_ordinal=0)
            session.add(run)
            if kind == "stt":
                asset = (await session.execute(select(ChatAsset).where(ChatAsset.id == intent["input_asset_id"],
                    ChatAsset.user_id == user_id, ChatAsset.project_id == project_id,
                    ChatAsset.status == "clean").with_for_update())).scalar_one_or_none()
                if asset is None or _duration({"mime_type": asset.mime_type, "size_bytes": asset.size_bytes,
                                               "media_metadata": asset.media_metadata}) != duration_ms:
                    raise DurableRunInputError("source audio changed before admission")
                session.add(ChatRunAsset(run_id=run.id, asset_id=asset.id, purpose="input"))
            session.add(ChatRunProvider(run_id=run.id, purpose="executor", provider_id=route["provider_id"],
                model_id=route["model_id"], provider_label=route["provider_name"],
                model_label=route["model_name"], config_version_hash=route["config_version_hash"]))
            await append_event(session, run, _event(run, "run.started", {"conversation_id": None,
                "temp_thread_id": None, "model_name": run.model_name, "effective_features": {}, "run_kind": kind}))
            await append_event(session, run, _event(run, "run.stage.changed", {"stage": "queued"}))
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


async def _read_source(asset_id: str, *, user_id: str, project_id: str, expected_ms: int) -> tuple[bytes, str]:
    row = await assets.get_asset(asset_id=asset_id, user_id=user_id, project_id=project_id)
    if row["status"] != "clean" or _duration(row) != expected_ms:
        raise DurableRunInputError("source audio changed")
    opened = await assets.open_download(asset_id=asset_id, user_id=user_id, project_id=project_id)
    if opened.size_bytes != row["size_bytes"] or opened.mime_type != row["mime_type"]:
        await asyncio.to_thread(opened.body.close)
        raise DurableRunInputError("source audio changed")
    data = bytearray()
    async for chunk in opened.chunks():
        if len(data) + len(chunk) > _MAX_AUDIO_BYTES:
            raise DurableRunInputError("source audio exceeds size limit")
        data.extend(chunk)
    if len(data) != opened.size_bytes:
        raise DurableRunInputError("source audio size changed")
    return bytes(data), opened.mime_type


async def _start(run_id: str, *, owner: str, bound: Decimal, kind: str) -> str:
    async def transaction():
        async with _factory()() as session, session.begin():
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
            run = (await session.execute(select(ChatRun).where(ChatRun.id == run_id)
                .with_for_update().execution_options(populate_existing=True))).scalar_one()
            _require_owned_running_lease(run, owner)
            segment = await prepare_segment(session, run, segment_id=f"{kind}:1", ordinal=1, endpoint=f"audio_{kind}")
            await session.flush()
            if segment.status == "completed":
                return "completed"
            if segment.status != "prepared":
                return "unknown"
            if run.cancel_requested_at is not None:
                return "canceled"
            await credit.reserve_media_credit_in_transaction(session, user_id=run.user_id,
                project_id=run.project_id, api_key_id=run.api_key_id, bound=bound)
            session.add(ChatModelCallReservation(run_id=run.id, segment_id=f"{kind}:1", bound_credits=bound, status="reserved"))
            run.reserved_credits = bound
            begin_segment_io(segment)
            run.provider_started_at = _now()
            await append_event(session, run, _event(run, "run.stage.changed", {"stage": "model_request"}))
            return "started"
    return await budgets.retry_deadlocks(transaction)


async def _checkpoint(run_id: str, *, owner: str, kind: str, result: dict, duration_ms: int) -> None:
    async with _factory()() as session, session.begin():
        run = (await session.execute(select(ChatRun).where(ChatRun.id == run_id).with_for_update())).scalar_one()
        _require_owned_running_lease(run, owner)
        segment = (await session.execute(select(ChatRunSegment).where(ChatRunSegment.run_id == run_id,
            ChatRunSegment.segment_id == f"{kind}:1").with_for_update())).scalar_one()
        if segment.status != "provider_started":
            raise DurableRunProviderResultUnknown("audio provider boundary is no longer owned")
        complete_segment_io(segment, result_payload=result, usage_payload={"duration_ms": duration_ms})
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
            elif not isinstance(result.get("text"), str) or len(result["text"]) > _MAX_TRANSCRIPT:
                raise DurableRunProviderResultUnknown("transcript checkpoint is unavailable")
            reservation = (await session.execute(select(ChatModelCallReservation).where(
                ChatModelCallReservation.run_id == run_id, ChatModelCallReservation.segment_id == f"{kind}:1")
                .with_for_update().execution_options(populate_existing=True))).scalar_one()
            if reservation.status == "settled":
                return
            if reservation.status != "reserved":
                raise DurableRunProviderResultUnknown("audio credit reservation is not payable")
            price = Decimal(run.pricing_snapshot["usd_per_second"])
            seconds = Decimal(duration_ms) / 1000
            total = price * seconds
            component = {"segment_id": f"{kind}:1", "kind": "audio_output_seconds" if kind == "tts" else "audio_input_seconds",
                         "quantity": str(seconds), "unit": "second", "unit_price_usd": str(price),
                         "cost_usd": str(total), "source": "media", "model_name": run.model_name, "metadata": {}}
            credited = await credit.apply_usage_in_transaction(session, event_id=f"run:{run.id}",
                user_id=run.user_id, project_id=run.project_id, model_name=run.model_name,
                provider=run.capability_snapshot["provider_name"], prompt_tokens=0, completion_tokens=0,
                usage_cost=UsageCost(raw_cost=total, input_cost=total if kind == "stt" else Decimal(0),
                                    output_cost=total if kind == "tts" else Decimal(0),
                                    pricing_status="priced", pricing_snapshot=run.pricing_snapshot),
                margin_multiplier=Decimal(run.pricing_snapshot["margin_multiplier"]),
                credit_per_usd=Decimal(run.pricing_snapshot["credit_per_usd"]), source=run.source,
                api_key_id=run.api_key_id, run_id=run.id, usage_components=[component])
            if credited > Decimal(str(reservation.bound_credits)):
                raise DurableRunError("audio usage exceeds reserved price")
            reservation.actual_credits = credited
            reservation.status = "settled"
            reservation.settled_at = _now()
            run.usage_reconciled_at = _now()
            await append_event(session, run, _event(run, "usage.updated", {"components": [component],
                "prompt_tokens": 0, "completion_tokens": 0, "raw_cost": str(total), "credited_cost": str(credited)}))
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
            if await _cancel_requested(run_id):
                await _finish(run_id, status="canceled", message_id=None, owner=owner,
                    error_code="canceled", safe_message="audio run canceled")
                return True
            route = await routing.resolve_model_snapshot(capability_snapshot)
            if route is None:
                raise DurableRunInputError("audio provider configuration changed")
            rate = audio_transport.validate_audio_request(route, kind=kind,
                format=payload["response_format"] if kind == "tts" else None)
            if rate != Decimal(pricing_snapshot["usd_per_second"]):
                raise DurableRunInputError("audio price changed after admission")
            source = None
            if kind == "stt":
                source = await _read_source(payload["input_asset_id"], user_id=payload["user_id"],
                    project_id=payload["project_id"], expected_ms=pricing_snapshot["max_duration_ms"])
            bound = credit.credits_for_cost(rate * Decimal(pricing_snapshot["max_duration_ms"]) / 1000,
                Decimal(pricing_snapshot["margin_multiplier"]), Decimal(pricing_snapshot["credit_per_usd"]))
            state = await _start(run_id, owner=owner, bound=bound, kind=kind)
            if state == "canceled":
                await _finish(run_id, status="canceled", message_id=None, owner=owner,
                    error_code="canceled", safe_message="audio run canceled")
                return True
            if state == "unknown":
                raise DurableRunProviderResultUnknown("audio provider result cannot be replayed")
            if state == "started":
                if kind == "tts":
                    data, mime = await audio_transport.generate_speech(route, text=payload["input"],
                        voice=payload["voice"], format=payload["response_format"])
                    stored = await assets.create_generated_asset_bytes(data=data,
                        original_name="speech." + payload["response_format"], media_type=mime,
                        user_id=payload["user_id"], project_id=payload["project_id"], run_id=run_id)
                    duration_ms = _duration({**stored, "media_metadata": stored.get("media_metadata")})
                    result = {"asset_id": stored["id"]}
                else:
                    data, mime = source
                    text = await audio_transport.transcribe_audio(route, data=data, mime_type=mime,
                        language=payload.get("language"), prompt=payload.get("prompt"))
                    if not isinstance(text, str) or len(text) > _MAX_TRANSCRIPT:
                        raise DurableRunProviderResultUnknown("provider transcript exceeds size limit")
                    duration_ms = pricing_snapshot["max_duration_ms"]
                    result = {"text": text}
                await _checkpoint(run_id, owner=owner, kind=kind, result=result, duration_ms=duration_ms)
        await _settle(run_id, owner=owner, kind=kind)
        canceled = await _cancel_requested(run_id)
        await _finish(run_id, status="canceled" if canceled else "completed", message_id=None, owner=owner,
            error_code="canceled" if canceled else None, safe_message="audio run canceled" if canceled else None)
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
            return {"kind": "stt", "text": result["text"]}
        row = (await session.execute(select(ChatAsset).join(ChatRunAsset, ChatRunAsset.asset_id == ChatAsset.id)
            .where(ChatRunAsset.run_id == run_id, ChatRunAsset.purpose == "output",
                   ChatAsset.id == result.get("asset_id"), ChatAsset.project_id == project_id,
                   ChatAsset.user_id == user_id, ChatAsset.status == "clean"))).scalar_one_or_none()
        if row is None:
            raise DurableRunError("speech output asset is unavailable")
        return {"kind": "tts", "asset_id": row.id, "mime_type": row.mime_type, "size_bytes": row.size_bytes}
