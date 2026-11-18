"""Owned image runs with a persistent, single-use provider boundary and credit hold."""

from __future__ import annotations

import asyncio
import base64
import json
import uuid
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC
from decimal import Decimal

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from lumen.config import get_settings
from lumen.crypto import encrypt_chat_content
from lumen.models.billing import Payer
from lumen.models.chat_assets import ChatAsset, ChatRunAsset
from lumen.models.chat_runs import ChatModelCallReservation, ChatRun, ChatRunProvider, ChatRunSegment
from lumen.services import assets, credit
from lumen.services.provider_metadata import UsageCost
from lumen.services.providers import image_transport, routing
from lumen.services.providers.credentials import execution_identity
from lumen.services.providers.errors import AmbiguousModelRouteError
from lumen.services.providers.image_billing import parse_svg_usage, svg_author_aliases, svg_author_pricing
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
from .admission import _intent_with_credential_authority
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
    image_input_ids,
    lock_media_run_for_io,
    lock_media_source_in_transaction,
    media_io_allowed,
    rejection_message,
    validate_batch_media_io_in_transaction,
)

_SEGMENT = "image:1"
_MAX_SOURCE_BYTES = 5 * 1024 * 1024
_SOURCE_MIMES = {"image/png", "image/jpeg", "image/webp"}
_OUTPUT_EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp",
                      "image/gif": "gif", "image/svg+xml": "svg"}


def svg_author_scope(required_scopes: tuple[str, ...] | None) -> str:
    return ("compat:completions:write" if required_scopes and "compat:images:write" in required_scopes
            else "native:runs:write")


def _route_snapshot(route: dict) -> dict:
    return {**{key: route[key] for key in ("provider_id", "model_id", "provider_name", "model_name",
                                         "model_kind", "config_version_hash")},
            **execution_identity(route)}


def _intent(request: dict) -> dict:
    intent = {"kind": "image", "model_id": request.get("model_id"), "provider_id": request.get("provider_id"),
              "prompt": request.get("prompt"), "size": request.get("size", "auto"), "quality": request.get("quality", "auto"),
              "n": request.get("n", 1), "source_asset_id": request.get("source_asset_id", request.get("input_asset_id"))}
    # Added only when requested so every pre-existing request keeps its idempotency fingerprint.
    intent.update(image_transport.image_options(request))
    ordered = request.get("input_asset_ids")
    if ordered is not None:
        if (not isinstance(ordered, list) or not 1 <= len(ordered) <= image_transport.MAX_OPENAI_IMAGE_INPUT_COUNT
                or intent["source_asset_id"] is not None):
            raise DurableRunInputError("specify one to sixteen ordered input assets")
        intent["source_asset_id"] = ordered[0]
        if len(ordered) > 1:
            intent["additional_source_asset_ids"] = ordered[1:]
    for key in ("mask_asset_id", "markup_asset_id"):
        if request.get(key) is not None:
            intent[key] = request[key]
    if "mask_asset_id" in intent and "markup_asset_id" in intent:
        raise DurableRunInputError("mask and markup are mutually exclusive")
    inputs = image_input_ids(intent)
    if any(not isinstance(item, str) or not item for item in inputs) or (inputs and not intent["source_asset_id"]):
        raise DurableRunInputError("image inputs must be owned assets")
    return intent


@dataclass(frozen=True)
class PreparedImageRun:
    payload: dict
    capability_snapshot: dict
    pricing_snapshot: dict
    required_scopes: tuple[str, ...]
    source_asset_id: str | None
    project_id: str
    user_id: str


async def prepare_image_run(request: dict, *, project_id: str, user_id: str,
                            source: str = "web", api_key_id: int | None = None,
                            payer: Payer | None = None,
                            required_scopes: tuple[str, ...] | None = None,
                            credential_authority: dict | None = None) -> PreparedImageRun:
    """Freeze one finite image request without inference or source-byte I/O."""
    image_transport.reject_streaming(stream=request.get("stream"), partial_images=request.get("partial_images"))
    if "model" in request:
        # OpenAI-compatible JSON body (Batch /v1/images/generations): same limits as the sync route.
        if request.get("response_format", "b64_json") != "b64_json":
            raise DurableRunInputError("Only response_format=b64_json is supported")
        if request.get("provider") is not None and request.get("provider_id") is not None:
            raise DurableRunInputError("Specify only one of provider and provider_id")
        required_scopes = required_scopes or ("compat:images:write",)
        request = {**request, "model_id": request["model"],
                   "provider_id": request.get("provider_id") if request.get("provider") is None else request["provider"]}
    intent = _intent_with_credential_authority(_intent(request), credential_authority)
    model_id = request.get("model_id")
    provider_id = request.get("provider_id")
    if (not isinstance(model_id, str) or not model_id or not isinstance(request.get("prompt"), str)
            or not request["prompt"].strip() or len(request["prompt"]) > 32000):
        raise DurableRunInputError("image model and bounded prompt are required")
    if provider_id is not None and not isinstance(provider_id, (str, int)):
        raise DurableRunInputError("invalid image provider")
    provider_number = int(provider_id) if provider_id is not None and str(provider_id).isdecimal() else None
    api_provider = str(provider_id) if provider_id is not None and provider_number is None else None
    try:
        route = (await routing.resolve_model_by_id(int(model_id), model_kind="image") if model_id.isdecimal()
                 else await routing.resolve_api_model(model_id, provider=api_provider,
                                                       provider_id=provider_number, model_kind="image"))
    except AmbiguousModelRouteError as exc:
        raise DurableRunInputError("image provider selection is ambiguous") from exc
    if (route is None or (provider_number is not None and provider_number != route["provider_id"])
            or (api_provider is not None and api_provider != route["api_provider"])):
        raise DurableRunInputError("image provider or model is unavailable")
    source_asset_id = intent["source_asset_id"]
    inputs = image_input_ids(intent)
    overlay = next((name for name in ("mask", "markup") if f"{name}_asset_id" in intent), None)
    dimensions = []
    for asset_id in inputs:
        asset = await assets.get_asset(asset_id=asset_id, user_id=user_id, project_id=project_id)
        if (asset["status"] != "clean" or asset["mime_type"] not in _SOURCE_MIMES
                or not 0 < asset["size_bytes"] <= _MAX_SOURCE_BYTES):
            raise DurableRunInputError("source image must be a bounded scanned owned asset")
        metadata = asset.get("media_metadata") or {}
        dimensions.append([metadata.get("width"), metadata.get("height")])
        if overlay and asset_id == intent[f"{overlay}_asset_id"] and (
                asset["mime_type"] != "image/png" or metadata.get("has_transparency") != 1
                or dimensions[-1] != dimensions[0] or any(type(value) is not int for value in dimensions[0])):
            raise DurableRunInputError("mask or markup must be a transparent PNG matching the first image")
    size = request.get("size", "auto")
    quality = request.get("quality", "auto")
    n = request.get("n", 1)
    options = image_transport.image_options(intent)
    author_prices = svg_author_pricing() if options.get("output_format") == "svg" else None
    author_route = None
    if options.get("svg_model") is not None:
        authors = await routing.resolve_image_svg_authors(route, model_name=options["svg_model"])
        if not authors:
            raise DurableRunInputError("requested SVG author is not available")
        author_route = authors[0]
        effort = options.get("svg_reasoning_effort")
        if effort is not None and effort not in author_route["svg_reasoning_efforts"]:
            raise DurableRunInputError("unsupported reasoning effort for the SVG author")
        author_prices = {options["svg_model"]: frozen_token_pricing(author_route)}
    unit_price = image_transport.validate_image_request(
        route, size=size, quality=quality, n=n, edit=source_asset_id is not None, prompt=intent["prompt"],
        source_dimensions=dimensions[0] if dimensions else None, additional_dimensions=dimensions[1:],
        source_count=len(inputs) - (overlay is not None), overlay=overlay, author_prices=author_prices, **options,
    )
    basis = image_transport.image_billing_basis(route)
    if not assets.asset_pipeline_available():
        raise DurableRunInputError("image asset storage or scanner is unavailable")
    per_usd = Decimal(str(get_settings().chat_credit_per_usd))
    margin = Decimal(str(route["margin_multiplier"]))
    cost = unit_price if basis == "tokens" else unit_price * n
    bound = credit.credits_for_cost(cost, margin, per_usd)
    if bound <= 0 or bound >= Decimal("10000000000"):
        raise DurableRunInputError("image credit reservation is outside the supported range")
    if basis == "tokens" and "reservation_usd" not in route["media_pricing"]:
        await credit.precheck(user_id, project_id, api_key_id, bound=bound, payer=payer)
    else:
        await credit.precheck(user_id, project_id, api_key_id, payer=payer)
    capability = _route_snapshot(route)
    if author_route is not None:
        capability["svg_author_route"] = _route_snapshot(author_route)
    capability["effective_features"] = {}
    if credential_authority is not None:
        capability["credential_authority"] = deepcopy(credential_authority)
    pricing = {"billing_basis": basis, "size": size, "quality": quality,
               "count": n, "margin_multiplier": format(margin, "f"), "credit_per_usd": format(per_usd, "f")}
    if basis == "tokens":
        pricing.update(frozen_token_pricing(route))
        pricing["reservation_usd"] = format(unit_price, "f")
        pricing["media_pricing"] = json.loads(json.dumps(route["media_pricing"]))
        pricing["required_token_modalities"] = ["image_output", *(["image_input"] if source_asset_id is not None else [])]
        if "reservation_usd" not in route["media_pricing"] and dimensions:
            pricing["source_dimensions"] = dimensions[0]
            if len(dimensions) > 1:
                pricing["additional_dimensions"] = dimensions[1:]
        if author_prices is not None:
            pricing["svg_author_pricing"] = deepcopy(author_prices)
            if author_route is not None:
                # A provider-reported snapshot of this same entry uses the
                # selected route's frozen rates, never a different local route.
                pricing["svg_author_aliases"] = list(svg_author_aliases(options["svg_model"]))
    else:
        pricing["image_per_unit"] = format(unit_price, "f")
    pricing["bound_credits"] = format(bound, "f")
    scopes = required_scopes or ("native:images:write", *(("native:assets:read",) if source_asset_id else ()))
    if author_route is not None and (author_scope := svg_author_scope(scopes)) not in scopes:
        scopes = (*scopes, author_scope)
    return PreparedImageRun(intent, capability, pricing, scopes, source_asset_id, project_id, user_id)


async def persist_image_run_in_transaction(
    session: AsyncSession, prepared: PreparedImageRun, *, project_id: str, user_id: str,
    client_request_id: str, source: str = "web", api_key_id: int | None = None,
    payer: Payer | None = None,
    workload_class: str | None = None, batch_id: str | None = None,
    allowed_scopes: frozenset[str] | None = None,
) -> ChatRun:
    """Bind the frozen request in the caller's transaction; never commit, hold credit or wake.

    Default class is ``online_media``; a Batch caller passes ``workload_class="batch"``
    with its locked ``batch_id``. The pool is resolved from persisted configuration.
    """
    from lumen.services.worker_routing import resolve_worker_route

    from .admission import _lock_run_configurations

    if (prepared.project_id, prepared.user_id) != (project_id, user_id):
        raise DurableRunInputError("prepared image owner changed")
    intent, capability, pricing = prepared.payload, prepared.capability_snapshot, prepared.pricing_snapshot
    intent = _intent_with_credential_authority(intent, capability.get("credential_authority"))
    existing = (await session.execute(select(ChatRun).where(ChatRun.project_id == project_id,
        ChatRun.user_id == user_id, ChatRun.client_request_id == client_request_id)
        .with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
    if existing is not None:
        if existing.request_fingerprint != _fingerprint(intent):
            raise DurableRunConflict("idempotency_key_reused_with_different_intent")
        return existing
    route = await resolve_worker_route(session, run_kind="image", workload_class=workload_class, batch_id=batch_id)
    await _lock_run_configurations(session, capability, model_name=capability["model_name"])
    await authorize_media_in_transaction(session, user_id=user_id, project_id=project_id,
        api_key_id=api_key_id, required_scopes=prepared.required_scopes, allowed_scopes=allowed_scopes,
        credential_authority=capability.get("credential_authority"))
    run = ChatRun(id=str(uuid.uuid4()), run_scope="image", run_kind="image", project_id=project_id,
        user_id=user_id, model_name=capability["model_name"], source=source, api_key_id=api_key_id,
        payer_domain_id=payer.domain_id if payer is not None else None,
        workload_class=route.workload_class, worker_pool_id=route.worker_pool_id, batch_id=batch_id,
        client_request_id=client_request_id, request_fingerprint=_fingerprint(intent), fingerprint_version=1,
        execution_protocol_version=1, capability_snapshot=capability, pricing_snapshot=pricing,
        request_payload=encrypt_chat_content(json.dumps({**intent, "execution_protocol_version": 1,
                                                       "required_scopes": list(prepared.required_scopes)})),
        status="queued", last_seq=0, current_ordinal=0)
    session.add(run)
    for source_id in sorted(set(image_input_ids(intent))):
        row = await lock_media_source_in_transaction(session, asset_id=source_id, user_id=user_id,
            project_id=project_id, run_kind="image", pricing=pricing, batch_id=batch_id)
        session.add(ChatRunAsset(run_id=run.id, asset_id=row.id, purpose="input"))
    provider_routes = [("executor", capability)]
    if "svg_author_route" in capability:
        provider_routes.append(("svg_author", capability["svg_author_route"]))
    for purpose, snapshot in provider_routes:
        session.add(ChatRunProvider(run_id=run.id, purpose=purpose, provider_id=snapshot["provider_id"],
            model_id=snapshot["model_id"], provider_label=snapshot["provider_name"],
            model_label=snapshot["model_name"], config_version_hash=snapshot["config_version_hash"],
            **{key: value for key, value in execution_identity(snapshot).items()
               if key not in {"provider_auth", "provider_quota_group"}}))
    await append_event(session, run, _event(run, "run.started", {"conversation_id": None,
        "temp_thread_id": None, "model_name": run.model_name, "effective_features": {}, "run_kind": "image"}))
    await append_event(session, run, _event(run, "run.stage.changed", {"stage": "queued"}))
    return run


async def admit_image_run(request: dict, *, project_id: str, user_id: str, client_request_id: str,
                          source: str = "web", api_key_id: int | None = None,
                          payer: Payer | None = None,
                          required_scopes: tuple[str, ...] | None = None,
                          credential_authority: dict | None = None):
    """Preserve UUID idempotency before checks, then commit before the Redis hint."""
    from .admission import existing_run_for_intent

    image_transport.reject_streaming(stream=request.get("stream"), partial_images=request.get("partial_images"))
    intent = _intent_with_credential_authority(_intent(request), credential_authority)
    previous = await existing_run_for_intent(project_id=project_id, user_id=user_id,
        client_request_id=client_request_id, intent=intent, conversation_id=None)
    if previous is not None:
        return previous
    prepared = await prepare_image_run(request, project_id=project_id, user_id=user_id, source=source,
                                       api_key_id=api_key_id, required_scopes=required_scopes,
                                       payer=payer,
                                       credential_authority=credential_authority)
    try:
        async with _factory()() as session, session.begin():
            # No gap locks: the unique idempotency index (migration 030) decides same-key races, and the
            # IntegrityError replay below returns the winner. Gap locks deadlocked concurrent admissions.
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
            run = await persist_image_run_in_transaction(session, prepared, project_id=project_id, user_id=user_id,
                client_request_id=client_request_id, source=source, api_key_id=api_key_id, payer=payer)
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
            await credit.lock_run_payer(session, run_id)
            run, blocked = await lock_media_run_for_io(session, run_id, owner=owner)
            segment = await prepare_segment(session, run, segment_id=_SEGMENT, ordinal=1, endpoint="image_generation")
            await session.flush()
            if segment.status == "completed":
                return "completed"
            if segment.status != "prepared":
                return "unknown"
            if blocked is not None:
                return blocked
            if bound <= 0 or bound >= Decimal("10000000000") or bound != bound.quantize(Decimal("0.00000001")):
                raise DurableRunInputError("image credit reservation is invalid")
            await validate_batch_media_io_in_transaction(session, run)
            if "bound_credits" in run.pricing_snapshot and bound != Decimal(run.pricing_snapshot["bound_credits"]):
                raise DurableRunInputError("image credit reservation changed")
            payer = credit.run_payer(run)
            await credit.reserve_call_credit_in_transaction(session, user_id=run.user_id,
                project_id=run.project_id, api_key_id=run.api_key_id, bound=bound, payer=payer)
            reservation = ChatModelCallReservation(run_id=run.id, segment_id=_SEGMENT,
                                                  bound_credits=bound, status="reserved", **payer.columns())
            session.add(reservation)
            await credit.refresh_domain_reserved(session, payer)
            run.reserved_credits = bound
            begin_segment_io(segment)
            run.provider_started_at = _now()
            await append_event(session, run, _event(run, "run.stage.changed", {"stage": "model_request"}))
            return "started"
    return await budgets.retry_deadlocks(transaction)


async def _image_checkpoint(run_id: str, *, owner: str, asset_ids: list[str],
                            usage: UsageBreakdown | None, svg_usage: dict | None = None) -> None:
    factory = _factory()
    async with factory() as session, session.begin():
        run = (await session.execute(select(ChatRun).where(ChatRun.id == run_id).with_for_update())).scalar_one()
        _require_owned_running_lease(run, owner)
        segment = (await session.execute(select(ChatRunSegment).where(ChatRunSegment.run_id == run_id,
                          ChatRunSegment.segment_id == _SEGMENT).with_for_update())).scalar_one()
        if segment.status != "provider_started":
            raise DurableRunProviderResultUnknown("image provider boundary is no longer owned")
        complete_segment_io(segment, result_payload={"asset_ids": asset_ids},
                            usage_payload=_image_usage_payload(len(asset_ids), usage, svg_usage))
        await append_event(session, run, _event(run, "run.stage.changed", {"stage": "model_response"}))


def _image_usage_payload(count: int, usage: UsageBreakdown | None, svg_usage: dict | None = None) -> dict:
    payload = {"count": count, "token_usage": usage.as_usage_dict() if usage is not None else None}
    if svg_usage is not None:
        payload["svg_usage"] = svg_usage
    return payload


async def _record_image_usage(run_id: str, *, owner: str, count: int, usage: UsageBreakdown,
                              svg_usage: dict | None = None) -> None:
    """Persist paid provider usage before asset I/O so an ingestion failure keeps the evidence."""
    async with _factory()() as session, session.begin():
        run = (await session.execute(select(ChatRun).where(ChatRun.id == run_id).with_for_update())).scalar_one()
        _require_owned_running_lease(run, owner)
        segment = (await session.execute(select(ChatRunSegment).where(ChatRunSegment.run_id == run_id,
                          ChatRunSegment.segment_id == _SEGMENT).with_for_update())).scalar_one()
        if segment.status != "provider_started":
            raise DurableRunProviderResultUnknown("image provider boundary is no longer owned")
        record_segment_usage(segment, _image_usage_payload(count, usage, svg_usage))


def _price_token_image(pricing: dict, usage: object, *, model_name: str) -> tuple[UsageBreakdown, UsageCost, list[dict]]:
    if not isinstance(usage, dict) or usage.get("count") != pricing["count"]:
        raise ValueError("image usage count is inconsistent")
    breakdown = UsageBreakdown.from_canonical(usage.get("token_usage"))
    if breakdown is None or not breakdown.reported("image", "output"):
        raise ValueError("image output token usage is missing")
    usage_cost = credit.usage_cost_from_pricing_snapshot(
        pricing, prompt_tokens=breakdown.input_tokens, completion_tokens=breakdown.output_tokens, breakdown=breakdown,
    )
    if usage_cost.pricing_status != "priced":
        raise ValueError("image token usage is unpriced")
    metadata = {"size": pricing["size"], "quality": pricing["quality"]}
    components = credit.token_usage_components(
        usage_cost, segment_id=_SEGMENT, source="media", model_name=model_name, metadata=metadata,
    )
    author_prices = pricing.get("svg_author_pricing")
    if author_prices is not None:
        author = parse_svg_usage(usage.get("svg_usage"))
        author_model = author["model"]
        priced_author = author_model
        if len(author_prices) == 1 and author_model in pricing.get("svg_author_aliases", ()):
            priced_author = next(iter(author_prices))
        author_cost = credit.usage_cost_from_pricing_snapshot(
            author_prices[priced_author], prompt_tokens=author["input_tokens"], completion_tokens=author["output_tokens"],
        )
        if author_cost.pricing_status != "priced":
            raise ValueError("SVG authoring usage is unpriced")
        components += credit.token_usage_components(
            author_cost, segment_id=_SEGMENT, source="media", model_name=author_model,
            metadata={**metadata, "stage": "svg_authoring", "reasoning_tokens": author["reasoning_tokens"]},
        )
        breakdown += UsageBreakdown.from_totals(author["input_tokens"], author["output_tokens"])
        usage_cost = replace(usage_cost, raw_cost=usage_cost.raw_cost + author_cost.raw_cost,
                             input_cost=usage_cost.input_cost + author_cost.input_cost,
                             output_cost=usage_cost.output_cost + author_cost.output_cost,
                             pricing_snapshot={**usage_cost.pricing_snapshot, "svg_usage": author})
    elif "svg_usage" in usage:
        raise ValueError("unexpected SVG authoring usage")
    if usage_cost.raw_cost > Decimal(pricing["reservation_usd"]):
        raise ValueError("image token usage exceeds reservation")
    return breakdown, usage_cost, components


async def _settle_image(run_id: str, *, owner: str) -> None:
    """Exactly-once observed usage and run-local hold reconciliation in one transaction."""
    factory = _factory()
    async def transaction():
        async with factory() as session, session.begin():
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
            await credit.lock_run_payer(session, run_id)
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
                    breakdown, usage_cost, components = _price_token_image(pricing, usage, model_name=run.model_name)
                except (ValueError, TypeError, KeyError) as exc:
                    raise DurableRunProviderResultUnknown("image token usage cannot safely be settled") from exc
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
            payer = credit.run_payer(run)
            credited = await credit.apply_usage_in_transaction(session, event_id=f"run:{run.id}",
                        user_id=run.user_id, project_id=run.project_id, model_name=run.model_name,
                        payer=payer,
                        provider=run.capability_snapshot["provider_name"], prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                        usage_cost=usage_cost, breakdown=breakdown,
                        execution_route=run.capability_snapshot,
                        margin_multiplier=Decimal(pricing["margin_multiplier"]),
                        credit_per_usd=Decimal(pricing["credit_per_usd"]), source=run.source,
                        api_key_id=run.api_key_id, run_id=run.id, usage_components=components)
            if not token_mode and credited != Decimal(str(reservation.bound_credits)):
                raise DurableRunError("settled image usage differs from reserved exact price")
            reservation.actual_credits = credited
            reservation.status = "settled"
            reservation.settled_at = _now()
            await credit.refresh_domain_reserved(session, payer)
            run.usage_reconciled_at = _now()
            await append_event(session, run, _event(run, "usage.updated", {"components": components,
                "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                "raw_cost": str(usage_cost.raw_cost), "credited_cost": str(credited)}))
    await budgets.retry_deadlocks(transaction)


async def _read_image_asset(*, asset_id: str, user_id: str, project_id: str,
                            run_id: str | None = None, output: bool = False) -> tuple[bytes, str]:
    # A Batch run reads its accepted input pin even if the asset was later deleted.
    opened = (await assets.open_run_input_download(asset_id=asset_id, run_id=run_id, user_id=user_id, project_id=project_id)
              if run_id is not None else await assets.open_download(asset_id=asset_id, user_id=user_id, project_id=project_id))
    allowed = _OUTPUT_EXTENSIONS if output else _SOURCE_MIMES
    if opened.mime_type not in allowed or opened.size_bytes > _MAX_SOURCE_BYTES:
        await asyncio.to_thread(opened.body.close)
        raise DurableRunInputError("asset is not a bounded scanned image")
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
        blocked, is_batch = await media_io_allowed(run_id, owner=owner)
        if blocked is not None:
            await _finish(run_id, status="canceled", message_id=None, owner=owner, error_code=blocked, safe_message="image run canceled")
            return True
        route = await routing.resolve_model_snapshot(capability_snapshot)
        if route is None:
            raise DurableRunInputError("image provider configuration changed")
        source_id = payload.get("source_asset_id")
        additional_ids = payload.get("additional_source_asset_ids") or ()
        overlay = next((name for name in ("mask", "markup") if f"{name}_asset_id" in payload), None)
        source_data = source_mime = None
        token_mode = pricing_snapshot.get("billing_basis", "unit") == "tokens"
        if token_mode:
            # Preflight against admission-frozen prices; the resolver fence above still rejects config changes.
            route = {**route, "media_pricing": pricing_snapshot["media_pricing"],
                     **{key: pricing_snapshot.get(key) for key in (
                         "input_price_per_token", "output_price_per_token", "cache_read_price_per_token")}}
        extra = image_transport.image_options(payload)
        for key in ("source_dimensions", "additional_dimensions"):
            if key in pricing_snapshot:
                extra[key] = pricing_snapshot[key]
        if "svg_author_pricing" in pricing_snapshot:
            extra["author_prices"] = pricing_snapshot["svg_author_pricing"]
        price = image_transport.validate_image_request(
            route, size=pricing_snapshot["size"], quality=pricing_snapshot["quality"],
            n=pricing_snapshot["count"], edit=bool(source_id), prompt=payload["prompt"],
            source_count=(1 + len(additional_ids)) if source_id else 0, overlay=overlay, **extra,
        )
        expected_price = Decimal(pricing_snapshot["reservation_usd"] if token_mode else pricing_snapshot["image_per_unit"])
        if price != expected_price:
            raise DurableRunInputError("image price changed after admission")
        bound = credit.credits_for_cost(price if token_mode else price * pricing_snapshot["count"],
                       Decimal(pricing_snapshot["margin_multiplier"]), Decimal(pricing_snapshot["credit_per_usd"]))
        if source_id:
            async def read_source(asset_id: str) -> tuple[bytes, str]:
                return await _read_image_asset(asset_id=asset_id, user_id=payload["user_id"],
                    project_id=payload["project_id"], run_id=run_id if is_batch else None)

            source_data, source_mime = await read_source(source_id)
            if additional_ids:
                extra["additional_sources"] = [await read_source(asset_id) for asset_id in additional_ids]
            if overlay:
                extra["overlay_data"] = (overlay, *await read_source(payload[f"{overlay}_asset_id"]))
        state = await _image_segment_start(run_id, owner=owner, bound=bound)
        if state in IO_BLOCKED:
            await _finish(run_id, status="canceled", message_id=None, owner=owner, error_code=state, safe_message="image run canceled")
            return True
        if state == "unknown":
            raise DurableRunProviderResultUnknown("image provider result cannot be replayed")
        if state == "started":
            try:
                generated = await image_transport.generate_images(route, prompt=payload["prompt"],
                             size=pricing_snapshot["size"], quality=pricing_snapshot["quality"],
                             n=pricing_snapshot["count"], source_image=source_data, source_mime=source_mime, **extra)
            except image_transport.ImageTransportError as exc:
                # A refusal proven to precede inference settles at zero; anything else stays unknown.
                rejected = confirmed_media_rejection(exc)
                if rejected is None:
                    raise
                await _finish(run_id, status="failed", message_id=None, owner=owner, error_code="provider_rejected",
                              safe_message=rejection_message(rejected))
                return True
            outputs = generated.images
            if len(outputs) != pricing_snapshot["count"]:
                raise DurableRunProviderResultUnknown("provider returned a different image count")
            if generated.usage is not None:
                await _record_image_usage(run_id, owner=owner, count=len(outputs),
                                          usage=generated.usage, svg_usage=generated.svg_usage)
            stored_ids = []
            gif_pixels = 0
            for index, (image_bytes, mime_type) in enumerate(outputs):
                extension = _OUTPUT_EXTENSIONS.get(mime_type)
                if extension is None:
                    raise DurableRunProviderResultUnknown("provider returned unsupported image MIME")
                stored = await assets.create_generated_asset_bytes(data=image_bytes, original_name=f"image-{index + 1}.{extension}",
                              media_type=mime_type, user_id=payload["user_id"], project_id=payload["project_id"], run_id=run_id)
                if mime_type == "image/gif":
                    metadata = stored["media_metadata"]
                    gif_pixels += metadata["width"] * metadata["height"] * metadata["frame_count"]
                    if gif_pixels > image_transport.GIF_LIMITS["max_total_pixels"]:
                        raise DurableRunProviderResultUnknown("provider GIF result exceeds the frame pixel budget")
                stored_ids.append(stored["id"])
            await _image_checkpoint(run_id, owner=owner, asset_ids=stored_ids,
                                    usage=generated.usage, svg_usage=generated.svg_usage)
        await _settle_image(run_id, owner=owner)
        canceled = await _cancel_requested(run_id)
        await _finish(run_id, status="canceled" if canceled else "completed", message_id=None, owner=owner,
                      error_code="canceled" if canceled else None, safe_message="image run canceled" if canceled else None)
    except credit.QuotaExceeded as exc:
        await _finish(run_id, status="failed", message_id=None, owner=owner,
                      error_code=getattr(exc, "code", "quota_exceeded"), safe_message="credit quota is exhausted")
    except MediaAuthorizationRevoked:
        # Raised before any reservation or provider I/O committed: definite, not unknown.
        from .execution import logger
        try:
            await _finish(run_id, status="failed", message_id=None, owner=owner,
                          error_code="api_key_unauthorized", safe_message="image request is no longer authorized")
        except Exception:
            logger.exception("image run finalization deferred run_id=%s", run_id)
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
        data, _mime = await _read_image_asset(asset_id=asset_id, user_id=user_id, project_id=project_id, output=True)
        output.append({"b64_json": base64.b64encode(data).decode("ascii"), "asset_id": asset_id})
    return {"created": created, "data": output}
