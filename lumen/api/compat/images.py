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
from lumen.models.api_requests import ImageGenerationRequest, OpenAIImageGenerationRequest
from lumen.models.chat_assets import ChatAsset, ChatRunAsset
from lumen.models.chat_runs import ChatRun
from lumen.services import assets, openai_compat
from lumen.services.conversation_store import ChatStorageUnavailable
from lumen.services.durable_runs import errors as durable_errors
from lumen.services.durable_runs import queries
from lumen.services.durable_runs.images import image_result

router = APIRouter()
_POLL_INTERVAL_SECONDS = 0.25
_EDIT_FIELDS = frozenset({"image", "prompt", "model", "n", "size", "quality", "response_format", "provider", "provider_id"})


def _compat_error(exc: HTTPException) -> JSONResponse:
    code = {402: 429, 422: 400}.get(exc.status_code, exc.status_code)
    return openai_error_response(code, str(exc.detail))


def _failure(exc: Exception) -> JSONResponse:
    return _compat_error(image_error(exc))


def _request(
    model: str, prompt: str, n: int, size: str, quality: str, provider_id: int | None = None
) -> ImageGenerationRequest:
    return ImageGenerationRequest(model_id=model, provider_id=provider_id, prompt=prompt, n=n, size=size, quality=quality)


async def _replayed_source(path: Path, key: uuid.UUID, principal: Principal) -> str | None:
    """Match uploaded bytes to an owned prior edit before allocating another asset."""
    factory = get_session_factory()
    if factory is None:
        raise HTTPException(status_code=503, detail="image storage unavailable")
    try:
        async with factory() as session:
            asset = (await session.execute(
                select(ChatAsset)
                .join(ChatRunAsset, ChatRunAsset.asset_id == ChatAsset.id)
                .join(ChatRun, ChatRun.id == ChatRunAsset.run_id)
                .where(ChatRun.project_id == principal["project_id"], ChatRun.user_id == principal["user_id"],
                       ChatRun.client_request_id == str(key), ChatRun.run_kind == "image",
                       ChatRunAsset.purpose == "input")
            )).scalar_one_or_none()
            if asset is None:
                existing = await session.scalar(
                    select(ChatRun.id).where(
                        ChatRun.project_id == principal["project_id"],
                        ChatRun.user_id == principal["user_id"],
                        ChatRun.client_request_id == str(key),
                    )
                )
                if existing is not None:
                    raise HTTPException(status_code=409, detail="idempotency_key_reused_with_different_intent")
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail="image storage unavailable") from exc
    if asset is None:
        return None

    def digest() -> str:
        sha = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(64 * 1024), b""):
                sha.update(chunk)
        return sha.hexdigest()

    if path.stat().st_size != asset.size_bytes or await asyncio.to_thread(digest) != asset.sha256:
        raise HTTPException(status_code=409, detail="idempotency_key_reused_with_different_image")
    return asset.id


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
            _request(body.model, body.prompt, body.n, body.size, body.quality, body.provider_id),
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
    image: UploadFile = File(...),
    prompt: str = Form(...),
    model: str = Form(...),
    provider: str | None = Form(None),
    provider_id: int | None = Form(None),
    n: int = Form(1),
    size: str = Form("auto"),
    quality: str = Form("auto"),
    response_format: str = Form("b64_json"),
    idempotency_key: uuid.UUID | None = Header(default=None, alias="Idempotency-Key"),
    principal: Principal = Depends(require_api_key_scopes("compat:images:write", "native:assets:write")),
):
    unknown = set((await request.form()).keys()) - _EDIT_FIELDS
    if unknown:
        return openai_error_response(400, f"Unsupported image edit fields: {', '.join(sorted(unknown))}")
    if response_format != "b64_json":
        return openai_error_response(400, "Only response_format=b64_json is supported")
    if not prompt.strip() or not model.strip() or n < 1:
        return openai_error_response(400, "model, prompt and a positive n are required")
    if provider is not None and provider_id is not None:
        return openai_error_response(400, "Specify only one of provider and provider_id")
    try:
        payload = _request(model, prompt, n, size, quality, provider_id)
    except ValidationError as exc:
        return openai_error_response(400, str(exc), code="invalid_image_request")
    try:
        path = await _spool_upload(image)
    except HTTPException as exc:
        await image.close()
        return openai_error_response(exc.status_code, str(exc.detail))
    try:
        existing_id = await _replayed_source(path, idempotency_key, principal) if idempotency_key else None
        if existing_id is None:
            source = await assets.create_uploaded_asset(
                path=path,
                original_name=image.filename or "image.png",
                user_id=principal["user_id"],
                project_id=principal["project_id"],
            )
            source_id = source["id"]
        else:
            source_id = existing_id
    except (assets.AssetError, assets.AssetUnavailable, ChatStorageUnavailable) as exc:
        mapped = asset_error(exc)
        return openai_error_response(400 if mapped.status_code == 422 else mapped.status_code, str(mapped.detail))
    except HTTPException as exc:
        return openai_error_response(exc.status_code, str(exc.detail))
    finally:
        path.unlink(missing_ok=True)
        await image.close()
    try:
        admitted = await admit(
            payload,
            principal=principal,
            idempotency_key=idempotency_key or uuid.uuid4(),
            input_asset_id=uuid.UUID(source_id),
            api_provider=provider,
            required_scopes=("compat:images:write",),
        )
    except HTTPException as exc:
        return _compat_error(exc)
    return await _completed_image(admitted.run_id, principal=principal, request=request)
