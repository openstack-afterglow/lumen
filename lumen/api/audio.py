"""Synchronous native audio endpoints backed by owned, durable runs."""

from __future__ import annotations

import asyncio
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from lumen.auth import Principal, require_scopes
from lumen.services import assets, credit, openai_compat
from lumen.services.conversation_store import ChatStorageUnavailable
from lumen.services.durable_runs import errors as durable_errors
from lumen.services.durable_runs import queries
from lumen.services.durable_runs.audio import admit_audio_run, audio_result
from lumen.services.providers.errors import ProviderValidationError

router = APIRouter()
_POLL_INTERVAL_SECONDS = 0.25


class SpeechRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_id: str = Field(min_length=1, max_length=190)
    provider_id: int | str | None = None

    input: str = Field(min_length=1, max_length=4096)
    voice: str = Field(min_length=1, max_length=190)
    response_format: str = "mp3"

    @field_validator("model_id", "input", "voice")
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("audio fields must not be blank")
        return value


class TranscriptionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_id: str = Field(min_length=1, max_length=190)
    provider_id: int | str | None = None
    input_asset_id: UUID
    language: str | None = Field(default=None, max_length=16, pattern=r"^[A-Za-z-]+$")
    prompt: str | None = Field(default=None, max_length=1024)

    @field_validator("model_id")
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("audio model must not be blank")
        return value


def audio_error(exc: Exception) -> HTTPException:
    if isinstance(exc, durable_errors.DurableRunConflict):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, durable_errors.DurableRunNotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, (durable_errors.DurableRunInputError, ProviderValidationError)):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, credit.QuotaExceeded):
        return HTTPException(status_code=402, detail=str(exc))
    if isinstance(exc, assets.AssetUnavailable):
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, assets.AssetError):
        code = 403 if str(exc) == "asset forbidden" else 404 if str(exc) == "asset not found" else 422
        return HTTPException(status_code=code, detail=str(exc))
    if isinstance(exc, (durable_errors.DurableRunError, ChatStorageUnavailable)):
        return HTTPException(status_code=503, detail=str(exc))
    return HTTPException(status_code=503, detail="audio service unavailable")


async def admit(payload: SpeechRequest | TranscriptionRequest, *, kind: str, principal: Principal,
                idempotency_key: UUID, provider_type: str | None = None) -> str:
    body = payload.model_dump(mode="json")
    body["kind"] = kind
    if provider_type is not None:
        body["provider_id"] = provider_type
    try:
        descriptor = await admit_audio_run(
            body, project_id=principal["project_id"], user_id=principal["user_id"],
            client_request_id=str(idempotency_key), source=principal.get("source", "web"),
            api_key_id=principal.get("api_key_id"),
        )
    except (durable_errors.DurableRunError, assets.AssetError, assets.AssetUnavailable,
            ChatStorageUnavailable, credit.QuotaExceeded, ProviderValidationError) as exc:
        raise audio_error(exc) from exc
    return descriptor.run_id


async def completed_audio(run_id: str, *, principal: Principal, request: Request) -> dict:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + openai_compat.get_compat_timeout_seconds()
    while True:
        if await request.is_disconnected():
            raise HTTPException(status_code=499, detail="Client disconnected")
        if loop.time() >= deadline:
            raise HTTPException(status_code=504, detail="Request timed out waiting for audio")
        try:
            run = await queries.owned_run_response(
                run_id=run_id, project_id=principal["project_id"], user_id=principal["user_id"]
            )
            if run.status == "completed":
                return await audio_result(run_id=run_id, project_id=principal["project_id"], user_id=principal["user_id"])
            if run.status in {"failed", "canceled"}:
                events, _ = await queries.owned_events(
                    run_id=run_id, project_id=principal["project_id"], user_id=principal["user_id"],
                    after_seq=max(0, run.last_seq - 8),
                )
                terminal = next((event for event in reversed(events) if event.type in {"run.failed", "run.canceled"}), None)
                message = terminal.payload.safe_message if terminal is not None else "Audio run failed"
                raise HTTPException(status_code=500, detail=message or "Audio run failed")
        except (durable_errors.DurableRunError, assets.AssetError, assets.AssetUnavailable,
                ChatStorageUnavailable) as exc:
            raise audio_error(exc) from exc
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)


async def speech_stream(result: dict, *, principal: Principal) -> StreamingResponse:
    if result.get("kind") != "tts":
        raise HTTPException(status_code=503, detail="speech output is unavailable")
    try:
        download = await assets.open_download(
            asset_id=result["asset_id"], user_id=principal["user_id"], project_id=principal["project_id"]
        )
    except (assets.AssetError, assets.AssetUnavailable, ChatStorageUnavailable) as exc:
        raise audio_error(exc) from exc
    if download.mime_type != result["mime_type"] or download.size_bytes != result["size_bytes"]:
        await asyncio.to_thread(download.body.close)
        raise HTTPException(status_code=503, detail="speech output changed")
    return StreamingResponse(
        download.chunks(), media_type=download.mime_type,
        headers={"Content-Length": str(download.size_bytes), "Cache-Control": "private, no-store",
                 "X-Content-Type-Options": "nosniff"},
    )


@router.post("/chat/audio/speech", responses={200: {"content": {
    "audio/mpeg": {"schema": {"type": "string", "format": "binary"}},
    "audio/wav": {"schema": {"type": "string", "format": "binary"}},
}}})
async def speech(
    request: Request, payload: SpeechRequest, idempotency_key: UUID = Header(alias="Idempotency-Key"),
    principal: Principal = Depends(require_scopes("native:audio:write")),
):
    if payload.response_format not in {"mp3", "wav"}:
        raise HTTPException(status_code=422, detail="unsupported speech output format")
    run_id = await admit(payload, kind="tts", principal=principal, idempotency_key=idempotency_key)
    return await speech_stream(await completed_audio(run_id, principal=principal, request=request), principal=principal)


@router.post("/chat/audio/transcriptions")
async def transcriptions(
    request: Request, payload: TranscriptionRequest, idempotency_key: UUID = Header(alias="Idempotency-Key"),
    principal: Principal = Depends(require_scopes("native:audio:write", "native:assets:read")),
):
    run_id = await admit(payload, kind="stt", principal=principal, idempotency_key=idempotency_key)
    result = await completed_audio(run_id, principal=principal, request=request)
    if result.get("kind") != "stt":
        raise HTTPException(status_code=503, detail="transcript is unavailable")
    return {"text": result["text"]}
