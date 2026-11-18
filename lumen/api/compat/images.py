"""OpenAI image JSON generation and multipart edit over durable image runs."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from lumen.api.assets import _map_error as asset_error
from lumen.api.assets import _spool_upload
from lumen.api.compat.openai import openai_error_response
from lumen.api.images import admit, image_error
from lumen.auth import Principal, require_api_key_scopes
from lumen.db import get_session_factory
from lumen.models.api_requests import ImageEditRequest, ImageGenerationRequest, OpenAIImageGenerationRequest
from lumen.models.chat_assets import ChatAsset, ChatRunAsset
from lumen.models.chat_runs import ChatRun
from lumen.services import assets, openai_compat
from lumen.services.conversation_store import ChatStorageUnavailable
from lumen.services.durable_runs import errors as durable_errors
from lumen.services.durable_runs import queries
from lumen.services.durable_runs.common import _payload
from lumen.services.durable_runs.images import image_result
from lumen.services.durable_runs.media import image_input_ids, rejection_http_response
from lumen.services.providers.errors import ProviderValidationError
from lumen.services.providers.image_transport import (
    _MAX_SOURCE_BYTES,
    IMAGE_OPTION_KEYS,
    MAX_OPENAI_IMAGE_INPUT_COUNT,
    reject_streaming,
)

router = APIRouter()
_POLL_INTERVAL_SECONDS = 0.25
_EDIT_FIELDS = frozenset({
    "image", "image[]", "mask", "markup", "prompt", "model", "n", "size", "quality",
    "response_format", "provider", "provider_id",
    "stream", "partial_images", *IMAGE_OPTION_KEYS,
})


def _compat_error(exc: HTTPException) -> JSONResponse:
    code = {402: 429, 422: 400}.get(exc.status_code, exc.status_code)
    return openai_error_response(code, str(exc.detail))


def _failure(exc: Exception) -> JSONResponse:
    return _compat_error(image_error(exc))


async def _replayed_sources(
    paths: list[Path], key: uuid.UUID, principal: Principal, *, overlay: str | None,
) -> list[str] | None:
    """Compare each role and digest against the owned run's original upload order."""
    factory = get_session_factory()
    if factory is None:
        raise HTTPException(status_code=503, detail="image storage unavailable")
    try:
        async with factory() as session:
            run = await session.scalar(select(ChatRun).where(
                ChatRun.project_id == principal["project_id"], ChatRun.user_id == principal["user_id"],
                ChatRun.client_request_id == str(key),
            ))
            if run is None:
                return None
            payload = _payload(run) if run.run_kind == "image" else {}
            source_ids = image_input_ids(payload)
            expected_overlay = next((name for name in ("mask", "markup") if f"{name}_asset_id" in payload), None)
            if len(source_ids) != len(paths) or expected_overlay != overlay:
                raise HTTPException(status_code=409, detail="idempotency_key_reused_with_different_image")
            rows = (await session.scalars(select(ChatAsset)
                .join(ChatRunAsset, ChatRunAsset.asset_id == ChatAsset.id)
                .where(ChatRunAsset.run_id == run.id, ChatRunAsset.purpose == "input",
                       ChatAsset.id.in_(source_ids), ChatAsset.user_id == principal["user_id"],
                       ChatAsset.project_id == principal["project_id"]))).all()
            stored = {asset.id: (asset.size_bytes, asset.sha256) for asset in rows}
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail="image storage unavailable") from exc

    def digest(path: Path) -> tuple[int, str]:
        with path.open("rb") as source:
            return path.stat().st_size, hashlib.file_digest(source, "sha256").hexdigest()

    for asset_id, path in zip(source_ids, paths, strict=True):
        if stored.get(asset_id) != await asyncio.to_thread(digest, path):
            raise HTTPException(status_code=409, detail="idempotency_key_reused_with_different_image")
    return list(source_ids)


async def _completed_image(run_id: str, *, principal: Principal, request: Request) -> dict | JSONResponse:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + openai_compat.get_compat_timeout_seconds()
    while True:
        if await request.is_disconnected():
            return openai_error_response(499, "Client disconnected")
        if loop.time() >= deadline:
            return openai_error_response(504, "Request timed out waiting for image generation", type_="timeout_error")
        try:
            run = await queries.owned_run_response(
                run_id=run_id, project_id=principal["project_id"], user_id=principal["user_id"]
            )
            if run.status == "completed":
                result = await image_result(
                    run_id=run_id, project_id=principal["project_id"], user_id=principal["user_id"]
                )
                return {"created": result["created"], "data": [{"b64_json": item["b64_json"]} for item in result["data"]]}
            if run.status in {"failed", "canceled"}:
                events, _ = await queries.owned_events(
                    run_id=run_id, project_id=principal["project_id"], user_id=principal["user_id"],
                    after_seq=max(0, run.last_seq - 8),
                )
                terminal = next((event for event in reversed(events) if event.type in {"run.failed", "run.canceled"}), None)
                if terminal is not None and getattr(terminal.payload, "error_code", None) == "provider_rejected":
                    # The hold was released at zero: the refusal is reported with the upstream status class.
                    status, error_type, refusal = rejection_http_response(terminal.payload.safe_message)
                    return openai_error_response(status, refusal, type_=error_type)
                message = terminal.payload.safe_message if terminal is not None else "Image generation failed"
                return openai_error_response(500, message or "Image generation failed", type_="api_error")
        except (durable_errors.DurableRunError, assets.AssetError, assets.AssetUnavailable, ChatStorageUnavailable) as exc:
            return _failure(exc)
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)


@router.post("/images/generations", openapi_extra={"security": [{"APIKeyBearer": []}, {"XApiKey": []}]})
async def generate_image(
    request: Request,
    body: OpenAIImageGenerationRequest,
    idempotency_key: uuid.UUID | None = Header(default=None, alias="Idempotency-Key"),
    principal: Principal = Depends(require_api_key_scopes("compat:images:write")),
):
    if body.response_format != "b64_json":
        return openai_error_response(400, "Only response_format=b64_json is supported")
    if body.provider is not None and body.provider_id is not None:
        return openai_error_response(400, "Specify only one of provider and provider_id")
    try:
        admitted = await admit(
            ImageGenerationRequest(model_id=body.model, **body.model_dump(exclude={"model", "provider", "response_format"})),
            principal=principal,
            idempotency_key=idempotency_key or uuid.uuid4(),
            api_provider=body.provider,
            required_scopes=("compat:images:write",),
        )
    except HTTPException as exc:
        return _compat_error(exc)
    return await _completed_image(admitted.run_id, principal=principal, request=request)


@router.post("/images/edits", openapi_extra={"security": [{"APIKeyBearer": []}, {"XApiKey": []}]})
async def edit_image(
    request: Request,
    image: list[UploadFile] | None = File(None),
    image_array: list[UploadFile] | None = File(None, alias="image[]"),
    mask: UploadFile | None = File(None),
    markup: UploadFile | None = File(None),
    prompt: str = Form(...),
    model: str = Form(...),
    provider: str | None = Form(None),
    provider_id: int | None = Form(None),
    n: int = Form(1),
    size: str = Form("auto"),
    quality: str = Form("auto"),
    complexity: str | None = Form(None),
    background: str | None = Form(None),
    output_format: str | None = Form(None),
    output_compression: int | None = Form(None),
    frame_count: int | None = Form(None),
    frame_duration_ms: int | None = Form(None),
    input_fidelity: str | None = Form(None),
    moderation: str | None = Form(None),
    stream: bool | None = Form(None),
    partial_images: int | None = Form(None),
    response_format: str = Form("b64_json"),
    idempotency_key: uuid.UUID | None = Header(default=None, alias="Idempotency-Key"),
    principal: Principal = Depends(require_api_key_scopes("compat:images:write")),
):
    unknown = set((await request.form()).keys()) - _EDIT_FIELDS
    if unknown:
        return openai_error_response(400, f"Unsupported image edit fields: {', '.join(sorted(unknown))}")
    if image and image_array or not (image or image_array) or len(image or image_array) > MAX_OPENAI_IMAGE_INPUT_COUNT:
        return openai_error_response(400, "Supply one to sixteen files using either image or image[]")
    if (mask is not None and markup is not None) or (mask is not None and output_format == "gif"):
        return openai_error_response(400, "Mask and markup are mutually exclusive; GIF does not support masks")
    overlay = "mask" if mask is not None else "markup" if markup is not None else None
    uploads = [*(image or image_array), *([mask or markup] if overlay else [])]
    if response_format != "b64_json":
        return openai_error_response(400, "Only response_format=b64_json is supported")
    if not prompt.strip() or not model.strip() or n < 1:
        return openai_error_response(400, "model, prompt and a positive n are required")
    if provider is not None and provider_id is not None:
        return openai_error_response(400, "Specify only one of provider and provider_id")
    try:
        reject_streaming(stream=stream, partial_images=partial_images)
        options = ImageGenerationRequest(
            model_id=model, provider_id=provider_id, prompt=prompt, n=n, size=size, quality=quality,
            complexity=complexity, background=background, output_format=output_format,
            output_compression=output_compression, frame_count=frame_count, frame_duration_ms=frame_duration_ms,
            moderation=moderation,
        )
        if input_fidelity not in (None, "low", "high"):
            return openai_error_response(400, "Unsupported image input fidelity")
    except ProviderValidationError as exc:
        return openai_error_response(422, str(exc))
    except ValidationError as exc:
        return openai_error_response(400, str(exc), code="invalid_image_request")
    paths = []
    try:
        for upload in uploads:
            paths.append(await _spool_upload(upload, max_bytes=_MAX_SOURCE_BYTES))
        asset_ids = await _replayed_sources(paths, idempotency_key, principal, overlay=overlay) if idempotency_key else None
        if asset_ids is None:
            asset_ids = []
            for path, upload in zip(paths, uploads, strict=True):
                source = await assets.create_uploaded_asset(
                    path=path, original_name=upload.filename or "image.png", user_id=principal["user_id"],
                    project_id=principal["project_id"],
                )
                asset_ids.append(source["id"])
        asset_ids = [uuid.UUID(asset_id) for asset_id in asset_ids]
        overlay_id = asset_ids.pop() if overlay else None
        primary = asset_ids[0] if len(asset_ids) == 1 else None
        payload = ImageEditRequest(**options.model_dump(), input_fidelity=input_fidelity,
            input_asset_id=primary, input_asset_ids=asset_ids if primary is None else None,
            **({f"{overlay}_asset_id": overlay_id} if overlay else {}),
        )
    except (assets.AssetError, assets.AssetUnavailable, ChatStorageUnavailable) as exc:
        mapped = asset_error(exc)
        return openai_error_response(400 if mapped.status_code == 422 else mapped.status_code, str(mapped.detail))
    except HTTPException as exc:
        return openai_error_response(exc.status_code, str(exc.detail))
    finally:
        for path in paths:
            path.unlink(missing_ok=True)
        for upload in uploads:
            await upload.close()
    try:
        admitted = await admit(
            payload,
            principal=principal,
            idempotency_key=idempotency_key or uuid.uuid4(),
            input_asset_id=primary,
            api_provider=provider,
            required_scopes=("compat:images:write",),
        )
    except HTTPException as exc:
        return _compat_error(exc)
    return await _completed_image(admitted.run_id, principal=principal, request=request)
