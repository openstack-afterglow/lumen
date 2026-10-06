"""OpenAI speech and transcription endpoints over the canonical audio asset pipeline."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from lumen.api.assets import _map_error as asset_error
from lumen.api.assets import _spool_upload
from lumen.api.audio import admit, completed_audio, speech_stream
from lumen.api.compat.openai import openai_error_response
from lumen.auth import Principal, require_api_key_scopes
from lumen.db import get_session_factory
from lumen.models.api_requests import SpeechRequest, TranscriptionRequest
from lumen.models.chat_assets import ChatAsset, ChatRunAsset
from lumen.models.chat_runs import ChatRun
from lumen.services import assets
from lumen.services.conversation_store import ChatStorageUnavailable

router = APIRouter()
_TRANSCRIPTION_FIELDS = frozenset({"file", "model", "provider", "provider_id", "language", "prompt", "response_format", "temperature"})


def _compat_error(exc: HTTPException) -> JSONResponse:
    code = {402: 429, 422: 400}.get(exc.status_code, exc.status_code)
    return openai_error_response(code, str(exc.detail), type_="timeout_error" if code == 504 else None)


async def _replayed_source(path: Path, key: uuid.UUID, principal: Principal) -> str | None:
    """A repeated upload must match the owned run's original scanned source."""
    factory = get_session_factory()
    if factory is None:
        raise HTTPException(status_code=503, detail="audio storage unavailable")
    try:
        async with factory() as session:
            asset = (await session.execute(
                select(ChatAsset)
                .join(ChatRunAsset, ChatRunAsset.asset_id == ChatAsset.id)
                .join(ChatRun, ChatRun.id == ChatRunAsset.run_id)
                .where(ChatRun.project_id == principal["project_id"], ChatRun.user_id == principal["user_id"],
                       ChatRun.client_request_id == str(key), ChatRun.run_kind == "stt",
                       ChatRunAsset.purpose == "input")
            )).scalar_one_or_none()
            if asset is None:
                existing = await session.scalar(select(ChatRun.id).where(
                    ChatRun.project_id == principal["project_id"], ChatRun.user_id == principal["user_id"],
                    ChatRun.client_request_id == str(key),
                ))
                if existing is not None:
                    raise HTTPException(status_code=409, detail="idempotency_key_reused_with_different_intent")
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail="audio storage unavailable") from exc
    if asset is None:
        return None

    def digest() -> str:
        sha = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(64 * 1024), b""):
                sha.update(chunk)
        return sha.hexdigest()

    if path.stat().st_size != asset.size_bytes or await asyncio.to_thread(digest) != asset.sha256:
        raise HTTPException(status_code=409, detail="idempotency_key_reused_with_different_audio")
    return asset.id


@router.post("/audio/speech", openapi_extra={"security": [{"APIKeyBearer": []}, {"XApiKey": []}]},
             responses={200: {"content": {
                 "audio/mpeg": {"schema": {"type": "string", "format": "binary"}},
                 "audio/wav": {"schema": {"type": "string", "format": "binary"}},
             }}})
async def speech(
    request: Request, body: dict,
    idempotency_key: uuid.UUID | None = Header(default=None, alias="Idempotency-Key"),
    principal: Principal = Depends(require_api_key_scopes("compat:audio:write")),
):
    fields = {"model", "input", "voice", "response_format", "provider", "provider_id", "speed", "stream_format"}
    unknown = set(body) - fields
    if unknown:
        return openai_error_response(400, f"Unsupported speech fields: {', '.join(sorted(unknown))}")
    if body.get("response_format", "mp3") not in ("mp3", "wav"):
        return openai_error_response(400, "Only response_format=mp3 or wav is supported")
    if (type(body.get("speed", 1)) not in (int, float) or body.get("speed", 1) != 1
            or body.get("stream_format", "audio") != "audio"):
        return openai_error_response(400, "Only speed=1 and stream_format=audio are supported")
    if body.get("provider") is not None and body.get("provider_id") is not None:
        return openai_error_response(400, "Specify only one of provider and provider_id")
    if body.get("provider") is not None and (not isinstance(body["provider"], str) or not body["provider"].strip()):
        return openai_error_response(400, "provider must be a non-empty string")
    try:
        payload = SpeechRequest(model_id=body.get("model"), input=body.get("input"), voice=body.get("voice"),
                                response_format=body.get("response_format", "mp3"), provider_id=body.get("provider_id"))
        run_id = await admit(payload, kind="tts", principal=principal,
                             idempotency_key=idempotency_key or uuid.uuid4(), api_provider=body.get("provider"),
                             required_scopes=("compat:audio:write",))
        result = await completed_audio(run_id, principal=principal, request=request)
        return await speech_stream(result, principal=principal)
    except ValidationError as exc:
        return openai_error_response(400, str(exc), code="invalid_audio_request")
    except HTTPException as exc:
        return _compat_error(exc)


@router.post("/audio/transcriptions", openapi_extra={"security": [{"APIKeyBearer": []}, {"XApiKey": []}]})
async def transcriptions(
    request: Request,
    file: UploadFile = File(...),
    model: str = Form(...),
    provider: str | None = Form(None),
    provider_id: int | None = Form(None),
    language: str | None = Form(None),
    prompt: str | None = Form(None),
    response_format: str = Form("json"),
    temperature: float = Form(0),
    idempotency_key: uuid.UUID | None = Header(default=None, alias="Idempotency-Key"),
    principal: Principal = Depends(require_api_key_scopes("compat:audio:write", "native:assets:write")),
):
    try:
        unknown = set((await request.form()).keys()) - _TRANSCRIPTION_FIELDS
        if unknown:
            return openai_error_response(400, f"Unsupported transcription fields: {', '.join(sorted(unknown))}")
        if response_format not in {"json", "text"}:
            return openai_error_response(400, "Only response_format=json or text is supported")
        if temperature != 0:
            return openai_error_response(400, "Only temperature=0 is supported")
        if provider is not None and provider_id is not None:
            return openai_error_response(400, "Specify only one of provider and provider_id")
        try:
            payload = TranscriptionRequest(model_id=model, provider_id=provider_id,
                                           input_asset_id=uuid.uuid4(), language=language, prompt=prompt)
        except ValidationError as exc:
            return openai_error_response(400, str(exc), code="invalid_audio_request")
        try:
            path = await _spool_upload(file)
        except HTTPException as exc:
            return _compat_error(exc)
        try:
            source_id = await _replayed_source(path, idempotency_key, principal) if idempotency_key else None
            if source_id is None:
                source = await assets.create_uploaded_asset(
                    path=path, original_name=file.filename or "audio", user_id=principal["user_id"],
                    project_id=principal["project_id"],
                )
                source_id = source["id"]
        except (assets.AssetError, assets.AssetUnavailable, ChatStorageUnavailable) as exc:
            return _compat_error(asset_error(exc))
        except HTTPException as exc:
            return _compat_error(exc)
        finally:
            path.unlink(missing_ok=True)
        payload.input_asset_id = uuid.UUID(source_id)
        try:
            run_id = await admit(payload, kind="stt", principal=principal,
                                 idempotency_key=idempotency_key or uuid.uuid4(), api_provider=provider,
                                 required_scopes=("compat:audio:write",))
            result = await completed_audio(run_id, principal=principal, request=request)
            if result.get("kind") != "stt":
                raise HTTPException(status_code=503, detail="transcript is unavailable")
            return PlainTextResponse(result["text"]) if response_format == "text" else {"text": result["text"]}
        except HTTPException as exc:
            return _compat_error(exc)
    finally:
        await file.close()
