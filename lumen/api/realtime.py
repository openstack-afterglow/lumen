"""Native durable realtime session admission and one-time-ticket WebSocket gateway."""

from __future__ import annotations

from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import ConfigDict, Field, model_validator

from lumen.auth import Principal, credential_authority_snapshot, require_scopes
from lumen.config import get_settings
from lumen.models.api_requests import NativeMediaModelSelection
from lumen.services import credit
from lumen.services.durable_runs import errors as durable_errors
from lumen.services.durable_runs.realtime import admit_realtime_session, run_realtime_session
from lumen.services.infrastructure.api_load import admit_websocket
from lumen.services.providers.errors import ProviderValidationError

router = APIRouter()


class RealtimeSessionRequest(NativeMediaModelSelection):
    model_config = ConfigDict(extra="forbid")
    provider_id: str | int | None = None
    kind: Literal["conversation", "transcription", "translation", "live"] = "conversation"
    voice: str | None = Field(default=None, max_length=100)
    instructions: str | None = Field(default=None, max_length=4096)
    # Translation target language, fixed for the session; required for and only for translation.
    output_language: str | None = Field(default=None, min_length=2, max_length=12)
    # Conversation models that advertise available_reasoning_efforts only.
    reasoning_effort: Literal["minimal", "low", "medium", "high", "xhigh"] | None = None
    # Function tools for models advertising realtime_function_calling; validated by the transport.
    tools: list[dict] | None = Field(default=None, max_length=16)
    tool_choice: str | dict | None = None
    max_duration_seconds: int = Field(default=300, ge=10, le=900)

    @model_validator(mode="after")
    def _kind_fields(self) -> RealtimeSessionRequest:
        if (self.kind == "translation") != (self.output_language is not None):
            raise ValueError("output_language is required for, and only for, translation sessions")
        if self.kind != "conversation" and any(
                value is not None for value in (self.reasoning_effort, self.tools, self.tool_choice)):
            raise ValueError("reasoning_effort and tools apply only to conversation sessions")
        return self


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, durable_errors.DurableRunConflict):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, (durable_errors.DurableRunInputError, ProviderValidationError)):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, credit.QuotaExceeded):
        return HTTPException(status_code=402, detail=str(exc))
    return HTTPException(status_code=503, detail="Realtime session unavailable")


def origin_allowed(websocket: WebSocket) -> bool:
    """Non-browser clients may omit Origin; browsers must match an explicit CORS origin."""
    origin = websocket.headers.get("origin")
    if origin is None:
        return True
    try:
        parsed = urlsplit(origin)
        if (parsed.scheme not in {"http", "https"} or parsed.username or parsed.password
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
            return False
    except ValueError:
        return False
    return origin.rstrip("/") in {item.rstrip("/") for item in get_settings().cors_origin_list}


@router.post("/chat/realtime/sessions", status_code=201)
async def create_session(
    payload: RealtimeSessionRequest,
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
    principal: Principal = Depends(require_scopes("native:realtime:write")),
):
    # Optional kind-specific fields are omitted when absent, keeping historical request shapes.
    excluded = {"kind"} | {
        key for key in ("output_language", "reasoning_effort", "tools", "tool_choice", "model_row_id")
        if getattr(payload, key) is None
    }
    try:
        return await admit_realtime_session(payload.model_dump(exclude=excluded), kind=payload.kind,
            project_id=principal["project_id"],
            user_id=principal["user_id"], client_request_id=str(idempotency_key),
            source=principal.get("source", "web"), api_key_id=principal.get("api_key_id"),
            payer=principal.get("payer"),
            credential_authority=credential_authority_snapshot(principal))
    except (durable_errors.DurableRunError, credit.QuotaExceeded, ProviderValidationError) as exc:
        raise _error(exc) from exc


@router.websocket("/chat/realtime/sessions/{session_id}/ws")
@admit_websocket
async def realtime_socket(websocket: WebSocket, session_id: UUID):
    if not origin_allowed(websocket):
        await websocket.close(code=4403)
        return
    token = websocket.headers.get("x-realtime-token") or websocket.query_params.get("token")
    if not token:
        await websocket.close(code=4401)
        return
    try:
        await websocket.accept()
        await run_realtime_session(websocket, run_id=str(session_id), token=token)
    except (durable_errors.DurableRunNotFound, durable_errors.DurableRunConflict):
        await websocket.close(code=4404)
    except credit.DomainCreditInsufficient as exc:
        await websocket.close(code=4429, reason=exc.code)
    except WebSocketDisconnect:
        pass
    except Exception:
        await websocket.close(code=1011)
    else:
        await websocket.close(code=1000)
