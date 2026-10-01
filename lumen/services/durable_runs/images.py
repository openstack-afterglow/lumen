"""Owned image runs with a persistent, single-use provider boundary and credit hold."""

from __future__ import annotations

import asyncio
import base64
import json
import uuid
from datetime import UTC
from decimal import Decimal

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from lumen.config import get_settings
from lumen.crypto import encrypt_chat_content
from lumen.models.chat_assets import ChatAsset, ChatRunAsset
from lumen.models.chat_runs import ChatModelCallReservation, ChatRun, ChatRunProvider, ChatRunSegment
from lumen.services import assets, credit
from lumen.services.litellm_client import UsageCost
from lumen.services.providers import image_transport, routing
from lumen.services.providers.errors import AmbiguousModelRouteError
from lumen.services.providers.pricing import frozen_token_pricing
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

_SEGMENT = "image:1"
_MAX_SOURCE_BYTES = 5 * 1024 * 1024


def _intent(request: dict) -> dict:
    return {"kind": "image", "model_id": request.get("model_id"), "provider_id": request.get("provider_id"),
            "prompt": request.get("prompt"), "size": request.get("size", "auto"), "quality": request.get("quality", "auto"),
            "n": request.get("n", 1), "source_asset_id": request.get("source_asset_id")}


async def admit_image_run(request: dict, *, project_id: str, user_id: str, client_request_id: str,
                          source: str = "web", api_key_id: int | None = None):
    """Return a prior identical run before credential checks or any provider-side work."""
    from .admission import existing_run_for_intent

    intent = _intent(request)
    previous = await existing_run_for_intent(project_id=project_id, user_id=user_id,
                                              client_request_id=client_request_id, intent=intent, conversation_id=None)
    if previous is not None:
        return previous
    model_id = request.get("model_id")
    provider_id = request.get("provider_id")
    if (not isinstance(model_id, str) or not model_id or not isinstance(request.get("prompt"), str)
            or not request["prompt"].strip() or len(request["prompt"]) > 32000):
        raise DurableRunInputError("image model and bounded prompt are required")
    if provider_id is not None and not isinstance(provider_id, (str, int)):
        raise DurableRunInputError("invalid image provider")
    provider_number = int(provider_id) if provider_id is not None and str(provider_id).isdecimal() else None
    provider_type = str(provider_id) if provider_id is not None and provider_number is None else None
    try:
        route = (await routing.resolve_model_by_id(int(model_id), model_kind="image") if model_id.isdecimal()
                 else await routing.resolve_api_model(model_id, provider=provider_type,
                                                       provider_id=provider_number, model_kind="image"))
    except AmbiguousModelRouteError as exc:
        raise DurableRunInputError("image provider selection is ambiguous") from exc
    if (route is None or (provider_number is not None and provider_number != route["provider_id"])
            or (provider_type is not None and provider_type != route["provider_type"])):
        raise DurableRunInputError("image provider or model is unavailable")
    source_asset_id = request.get("source_asset_id")
    if source_asset_id is not None:
        if not isinstance(source_asset_id, str):
            raise DurableRunInputError("invalid source asset")
        asset = await assets.get_asset(asset_id=source_asset_id, user_id=user_id, project_id=project_id)
        if asset["status"] != "clean" or asset["mime_type"] not in {"image/png", "image/jpeg", "image/webp"} or asset["size_bytes"] > _MAX_SOURCE_BYTES:
            raise DurableRunInputError("source image must be a bounded scanned owned asset")
    size = request.get("size", "auto")
    quality = request.get("quality", "auto")
    n = request.get("n", 1)
    unit_price = image_transport.validate_image_request(route, size=size, quality=quality, n=n, edit=source_asset_id is not None)
    basis = image_transport.image_billing_basis(route)
    if not assets.asset_pipeline_available():
        raise DurableRunInputError("image asset storage or scanner is unavailable")
    await credit.precheck(user_id, project_id, api_key_id)
    per_usd = Decimal(str(get_settings().chat_credit_per_usd))
    margin = Decimal(str(route["margin_multiplier"]))
    cost = unit_price if basis == "tokens" else unit_price * n
    bound = credit.credits_for_cost(cost, margin, per_usd)
    if bound <= 0:
        raise DurableRunInputError("image pricing must be a positive exact variant")
    capability = {key: route[key] for key in ("provider_id", "model_id", "provider_name", "model_name", "model_kind", "config_version_hash")}
    capability["effective_features"] = {}
    pricing = {"billing_basis": basis, "size": size, "quality": quality,
               "count": n, "margin_multiplier": format(margin, "f"), "credit_per_usd": format(per_usd, "f")}
    if basis == "tokens":
        pricing.update(frozen_token_pricing(route))
        pricing["reservation_usd"] = format(unit_price, "f")
        pricing["media_pricing"] = json.loads(json.dumps(route["media_pricing"]))
        pricing["required_token_modalities"] = ["image_output", *(["image_input"] if source_asset_id is not None else [])]
    else:
        pricing["image_per_unit"] = format(unit_price, "f")
    factory = _factory()
    try:
        async with factory() as session, session.begin():
            existing = (await session.execute(select(ChatRun).where(ChatRun.project_id == project_id,
                        ChatRun.user_id == user_id, ChatRun.client_request_id == client_request_id).with_for_update())).scalar_one_or_none()
            if existing is not None:
                if existing.request_fingerprint != _fingerprint(intent):
                    raise DurableRunConflict("idempotency_key_reused_with_different_intent")
                return descriptor(existing)
            from .admission import _lock_run_configurations
            await _lock_run_configurations(session, capability, model_name=route["model_name"])
            run = ChatRun(id=str(uuid.uuid4()), run_scope="image", run_kind="image", project_id=project_id,
                          user_id=user_id, model_name=route["model_name"], source=source, api_key_id=api_key_id,
                          client_request_id=client_request_id, request_fingerprint=_fingerprint(intent), fingerprint_version=1,
                          execution_protocol_version=1, capability_snapshot=capability, pricing_snapshot=pricing,
                          request_payload=encrypt_chat_content(json.dumps({**intent, "execution_protocol_version": 1})),
                          status="queued", last_seq=0, current_ordinal=0)
            session.add(run)
            if source_asset_id is not None:
                row = (await session.execute(select(ChatAsset).where(ChatAsset.id == source_asset_id,
                       ChatAsset.user_id == user_id, ChatAsset.project_id == project_id,
                       ChatAsset.status == "clean").with_for_update())).scalar_one_or_none()
                if row is None or row.mime_type not in {"image/png", "image/jpeg", "image/webp"} or row.size_bytes > _MAX_SOURCE_BYTES:
                    raise DurableRunInputError("source image is unavailable")
                session.add(ChatRunAsset(run_id=run.id, asset_id=row.id, purpose="input"))
            session.add(ChatRunProvider(run_id=run.id, purpose="executor", provider_id=route["provider_id"],
                       model_id=route["model_id"], provider_label=route["provider_name"],
                       model_label=route["model_name"], config_version_hash=route["config_version_hash"]))
            await append_event(session, run, _event(run, "run.started", {"conversation_id": None,
                  "temp_thread_id": None, "model_name": run.model_name, "effective_features": {}, "run_kind": "image"}))
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


async def _image_segment_start(run_id: str, *, owner: str, bound: Decimal) -> str:
    """Commit the hold and irrevocable I/O intent before entering provider transport."""
    factory = _factory()
    async def transaction():
        async with factory() as session, session.begin():
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
            run = (await session.execute(select(ChatRun).where(ChatRun.id == run_id)
                   .with_for_update().execution_options(populate_existing=True))).scalar_one()
            _require_owned_running_lease(run, owner)
            segment = await prepare_segment(session, run, segment_id=_SEGMENT, ordinal=1, endpoint="image_generation")
            await session.flush()
            if segment.status == "completed":
                return "completed"
            if segment.status != "prepared":
                return "unknown"
            if run.cancel_requested_at is not None:
                return "canceled"
            if bound <= 0 or bound >= Decimal("10000000000") or bound != bound.quantize(Decimal("0.00000001")):
                raise DurableRunInputError("image credit reservation is invalid")
            await credit.reserve_media_credit_in_transaction(session, user_id=run.user_id,
                project_id=run.project_id, api_key_id=run.api_key_id, bound=bound)
            reservation = ChatModelCallReservation(run_id=run.id, segment_id=_SEGMENT,
                                                  bound_credits=bound, status="reserved")
            session.add(reservation)
            run.reserved_credits = bound
            begin_segment_io(segment)
            run.provider_started_at = _now()
            await append_event(session, run, _event(run, "run.stage.changed", {"stage": "model_request"}))
            return "started"
    return await budgets.retry_deadlocks(transaction)


async def _image_checkpoint(run_id: str, *, owner: str, asset_ids: list[str], usage: UsageBreakdown | None) -> None:
    factory = _factory()
    async with factory() as session, session.begin():
        run = (await session.execute(select(ChatRun).where(ChatRun.id == run_id).with_for_update())).scalar_one()
        _require_owned_running_lease(run, owner)
        segment = (await session.execute(select(ChatRunSegment).where(ChatRunSegment.run_id == run_id,
                          ChatRunSegment.segment_id == _SEGMENT).with_for_update())).scalar_one()
        if segment.status != "provider_started":
            raise DurableRunProviderResultUnknown("image provider boundary is no longer owned")
        complete_segment_io(segment, result_payload={"asset_ids": asset_ids},
                            usage_payload=_image_usage_payload(len(asset_ids), usage))
        await append_event(session, run, _event(run, "run.stage.changed", {"stage": "model_response"}))


def _image_usage_payload(count: int, usage: UsageBreakdown | None) -> dict:
    return {"count": count, "token_usage": usage.as_usage_dict() if usage is not None else None}


async def _record_image_usage(run_id: str, *, owner: str, count: int, usage: UsageBreakdown) -> None:
    """Persist paid provider usage before asset I/O so an ingestion failure keeps the evidence."""
    async with _factory()() as session, session.begin():
        run = (await session.execute(select(ChatRun).where(ChatRun.id == run_id).with_for_update())).scalar_one()
        _require_owned_running_lease(run, owner)
        segment = (await session.execute(select(ChatRunSegment).where(ChatRunSegment.run_id == run_id,
                          ChatRunSegment.segment_id == _SEGMENT).with_for_update())).scalar_one()
        if segment.status != "provider_started":
            raise DurableRunProviderResultUnknown("image provider boundary is no longer owned")
        record_segment_usage(segment, _image_usage_payload(count, usage))


async def _settle_image(run_id: str, *, owner: str) -> None:
    """Exactly-once observed usage and run-local hold reconciliation in one transaction."""
    factory = _factory()
    async def transaction():
        async with factory() as session, session.begin():
            run = (await session.execute(select(ChatRun).where(ChatRun.id == run_id)
                   .with_for_update().execution_options(populate_existing=True))).scalar_one()
            _require_owned_running_lease(run, owner)
            segment = (await session.execute(select(ChatRunSegment).where(ChatRunSegment.run_id == run_id,
                               ChatRunSegment.segment_id == _SEGMENT).with_for_update())).scalar_one()
            result = load_segment_payload(segment.result_payload)
            if segment.status != "completed" or not isinstance(result, dict) or not isinstance(result.get("asset_ids"), list):
                raise DurableRunProviderResultUnknown("image result checkpoint is unavailable")
            asset_ids = result["asset_ids"]
            pricing = run.pricing_snapshot
            count = pricing["count"]
            if len(asset_ids) != count or len(set(asset_ids)) != count:
                raise DurableRunProviderResultUnknown("image result count differs from priced output")
            owned = (await session.execute(select(ChatRunAsset.asset_id).join(ChatAsset, ChatAsset.id == ChatRunAsset.asset_id)
                     .where(ChatRunAsset.run_id == run_id, ChatRunAsset.purpose == "output",
                            ChatRunAsset.asset_id.in_(asset_ids), ChatAsset.status == "clean",
                            ChatAsset.user_id == run.user_id, ChatAsset.project_id == run.project_id))).scalars().all()
            if set(owned) != set(asset_ids):
                raise DurableRunProviderResultUnknown("image result assets are unavailable")
            reservation = (await session.execute(select(ChatModelCallReservation).where(
                ChatModelCallReservation.run_id == run_id, ChatModelCallReservation.segment_id == _SEGMENT)
                .with_for_update().execution_options(populate_existing=True))).scalar_one()
            if reservation.status == "settled":
                return
            if reservation.status != "reserved":
                raise DurableRunProviderResultUnknown("image credit reservation is not payable")
            token_mode = pricing.get("billing_basis", "unit") == "tokens"
            breakdown = None
            if token_mode:
                usage = load_segment_payload(segment.usage_payload)
                try:
                    breakdown = UsageBreakdown.from_canonical(usage.get("token_usage") if isinstance(usage, dict) else None)
                    if not breakdown.reported("image", "output"):
                        raise ValueError("image output token usage is missing")
                    usage_cost = credit.usage_cost_from_pricing_snapshot(pricing,
                        prompt_tokens=breakdown.input_tokens, completion_tokens=breakdown.output_tokens,
                        breakdown=breakdown)
                    if usage_cost.pricing_status != "priced" or usage_cost.raw_cost > Decimal(pricing["reservation_usd"]):
                        raise ValueError("image token usage is unpriced or exceeds reservation")
                except (ValueError, TypeError, KeyError) as exc:
                    raise DurableRunProviderResultUnknown("image token usage cannot safely be settled") from exc
                components = credit.token_usage_components(usage_cost, segment_id=_SEGMENT,
                    source="media", model_name=run.model_name, metadata={"size": pricing["size"], "quality": pricing["quality"]})
            else:
                price = Decimal(pricing["image_per_unit"])
                total = price * count
                components = [{"segment_id": _SEGMENT, "kind": "image_units", "quantity": str(count), "unit": "image",
                               "unit_price_usd": str(price), "cost_usd": str(total), "source": "media",
                               "model_name": run.model_name, "metadata": {"size": pricing["size"], "quality": pricing["quality"]}}]
                usage_cost = UsageCost(raw_cost=total, input_cost=Decimal(0), output_cost=total,
                                       pricing_status="priced", pricing_snapshot=pricing)
            prompt_tokens = breakdown.input_tokens if breakdown is not None else 0
            completion_tokens = breakdown.output_tokens if breakdown is not None else 0
            expected_credits = credit.credits_for_cost(usage_cost.raw_cost,
                Decimal(pricing["margin_multiplier"]), Decimal(pricing["credit_per_usd"]))
            if expected_credits > Decimal(str(reservation.bound_credits)):
                raise DurableRunProviderResultUnknown("image token usage exceeds reserved credits")
            credited = await credit.apply_usage_in_transaction(session, event_id=f"run:{run.id}",
                        user_id=run.user_id, project_id=run.project_id, model_name=run.model_name,
                        provider=run.capability_snapshot["provider_name"], prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                        usage_cost=usage_cost, breakdown=breakdown,
                        margin_multiplier=Decimal(pricing["margin_multiplier"]),
                        credit_per_usd=Decimal(pricing["credit_per_usd"]), source=run.source,
                        api_key_id=run.api_key_id, run_id=run.id, usage_components=components)
            if not token_mode and credited != Decimal(str(reservation.bound_credits)):
                raise DurableRunError("settled image usage differs from reserved exact price")
            reservation.actual_credits = credited
            reservation.status = "settled"
            reservation.settled_at = _now()
            run.usage_reconciled_at = _now()
            await append_event(session, run, _event(run, "usage.updated", {"components": components,
                "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                "raw_cost": str(usage_cost.raw_cost), "credited_cost": str(credited)}))
    await budgets.retry_deadlocks(transaction)


async def _read_source(*, asset_id: str, user_id: str, project_id: str) -> tuple[bytes, str]:
    opened = await assets.open_download(asset_id=asset_id, user_id=user_id, project_id=project_id)
    if opened.mime_type not in {"image/png", "image/jpeg", "image/webp"} or opened.size_bytes > _MAX_SOURCE_BYTES:
        await asyncio.to_thread(opened.body.close)
        raise DurableRunInputError("source image is not a bounded scanned image")
    data = bytearray()
    async for chunk in opened.chunks():
        if len(data) + len(chunk) > _MAX_SOURCE_BYTES:
            raise DurableRunInputError("source image exceeds maximum size")
        data.extend(chunk)
    if len(data) != opened.size_bytes:
        raise DurableRunInputError("source image size changed")
    return bytes(data), opened.mime_type


async def execute_image_run(run_id: str, *, owner: str, payload: dict, capability_snapshot: dict, pricing_snapshot: dict) -> bool:
    """Keep the fenced claim live during slow provider and asset I/O."""
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
        return await _execute_image_run_inner(run_id, owner=owner, payload=payload,
                         capability_snapshot=capability_snapshot, pricing_snapshot=pricing_snapshot)
    finally:
        stop.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

async def _execute_image_run_inner(run_id: str, *, owner: str, payload: dict, capability_snapshot: dict, pricing_snapshot: dict) -> bool:
    """Execute a claimed run; recovering a started segment never invokes provider again."""
    from .execution import _finish
    factory = _factory()
    async with factory() as session:
        persisted = (await session.execute(select(ChatRunSegment.status).where(
            ChatRunSegment.run_id == run_id, ChatRunSegment.segment_id == _SEGMENT))).scalar_one_or_none()
    if persisted in {"provider_started", "failed"}:
        await _finish(run_id, status="failed", message_id=None, owner=owner,
                      error_code="provider_result_unknown", safe_message="image provider result cannot safely be retried")
        return True
    if persisted == "completed":
        try:
            await _settle_image(run_id, owner=owner)
        except DurableRunProviderResultUnknown:
            await _finish(run_id, status="failed", message_id=None, owner=owner,
                          error_code="provider_result_unknown", safe_message="image result cannot safely be settled")
            return True
        canceled = await _cancel_requested(run_id)
        await _finish(run_id, status="canceled" if canceled else "completed", message_id=None, owner=owner,
                      error_code="canceled" if canceled else None, safe_message="image run canceled" if canceled else None)
        return True
    try:
        if await _cancel_requested(run_id):
            await _finish(run_id, status="canceled", message_id=None, owner=owner, error_code="canceled", safe_message="image run canceled")
            return True
        route = await routing.resolve_model_snapshot(capability_snapshot)
        if route is None:
            raise DurableRunInputError("image provider configuration changed")
        source_id = payload.get("source_asset_id")
        source_data = source_mime = None
        if source_id:
            source_data, source_mime = await _read_source(asset_id=source_id, user_id=payload["user_id"], project_id=payload["project_id"])
        token_mode = pricing_snapshot.get("billing_basis", "unit") == "tokens"
        if token_mode:
            # Preflight against admission-frozen prices; the resolver fence above still rejects config changes.
            route = {**route, "media_pricing": pricing_snapshot["media_pricing"],
                     **{key: pricing_snapshot.get(key) for key in (
                         "input_price_per_token", "output_price_per_token", "cache_read_price_per_token")}}
        price = image_transport.validate_image_request(route, size=pricing_snapshot["size"],
                    quality=pricing_snapshot["quality"], n=pricing_snapshot["count"], edit=bool(source_id))
        expected_price = Decimal(pricing_snapshot["reservation_usd"] if token_mode else pricing_snapshot["image_per_unit"])
        if price != expected_price:
            raise DurableRunInputError("image price changed after admission")
        bound = credit.credits_for_cost(price if token_mode else price * pricing_snapshot["count"],
                       Decimal(pricing_snapshot["margin_multiplier"]), Decimal(pricing_snapshot["credit_per_usd"]))
        state = await _image_segment_start(run_id, owner=owner, bound=bound)
        if state == "canceled":
            await _finish(run_id, status="canceled", message_id=None, owner=owner, error_code="canceled", safe_message="image run canceled")
            return True
        if state == "unknown":
            raise DurableRunProviderResultUnknown("image provider result cannot be replayed")
        if state == "started":
            generated = await image_transport.generate_images(route, prompt=payload["prompt"], size=pricing_snapshot["size"],
                         quality=pricing_snapshot["quality"], n=pricing_snapshot["count"],
                         source_image=source_data, source_mime=source_mime)
            outputs = generated.images
            if len(outputs) != pricing_snapshot["count"]:
                raise DurableRunProviderResultUnknown("provider returned a different image count")
            if generated.usage is not None:
                await _record_image_usage(run_id, owner=owner, count=len(outputs), usage=generated.usage)
            stored_ids = []
            for index, (image_bytes, mime_type) in enumerate(outputs):
                extension = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}.get(mime_type)
                if extension is None:
                    raise DurableRunProviderResultUnknown("provider returned unsupported image MIME")
                stored = await assets.create_generated_asset_bytes(data=image_bytes, original_name=f"image-{index + 1}.{extension}",
                              media_type=mime_type, user_id=payload["user_id"], project_id=payload["project_id"], run_id=run_id)
                stored_ids.append(stored["id"])
            await _image_checkpoint(run_id, owner=owner, asset_ids=stored_ids, usage=generated.usage)
        await _settle_image(run_id, owner=owner)
        canceled = await _cancel_requested(run_id)
        await _finish(run_id, status="canceled" if canceled else "completed", message_id=None, owner=owner,
                      error_code="canceled" if canceled else None, safe_message="image run canceled" if canceled else None)
    except Exception:
        from .execution import logger
        logger.exception("durable image execution failed run_id=%s", run_id)
        try:
            await _finish(run_id, status="failed", message_id=None, owner=owner,
                          error_code="provider_result_unknown", safe_message="image provider result cannot safely be retried")
        except Exception:
            logger.exception("image run finalization deferred run_id=%s", run_id)
    return True


async def image_result(*, run_id: str, project_id: str, user_id: str) -> dict:
    """Materialize real, owned scanned bytes for compatibility SDK responses."""
    factory = _factory()
    async with factory() as session:
        run = (await session.execute(select(ChatRun).where(ChatRun.id == run_id,
                   ChatRun.user_id == user_id, ChatRun.project_id == project_id, ChatRun.run_kind == "image"))).scalar_one_or_none()
        if run is None:
            raise DurableRunNotFound("image run not found")
        if run.status != "completed":
            raise DurableRunConflict("image run is not completed")
        segment = (await session.execute(select(ChatRunSegment).where(ChatRunSegment.run_id == run_id,
                    ChatRunSegment.segment_id == _SEGMENT))).scalar_one_or_none()
        result = load_segment_payload(segment.result_payload) if segment is not None and segment.status == "completed" else None
        if result is None or not isinstance(result.get("asset_ids"), list):
            raise DurableRunError("image result checkpoint is unavailable")
        asset_ids = result["asset_ids"]
        created = int(run.created_at.replace(tzinfo=run.created_at.tzinfo or UTC).timestamp())
    output = []
    for asset_id in asset_ids:
        data, _mime = await _read_source(asset_id=asset_id, user_id=user_id, project_id=project_id)
        output.append({"b64_json": base64.b64encode(data).decode("ascii"), "asset_id": asset_id})
    return {"created": created, "data": output}
