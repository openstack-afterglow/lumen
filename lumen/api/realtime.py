"""Native durable realtime session admission and one-time-ticket WebSocket gateway."""

from __future__ import annotations

from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field

from lumen.auth import Principal, require_scopes
from lumen.config import get_settings
from lumen.services import credit
from lumen.services.durable_runs import errors as durable_errors
from lumen.services.durable_runs.realtime import admit_realtime_session, run_realtime_session
from lumen.services.providers.errors import ProviderValidationError

router = APIRouter()


class RealtimeSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_id: str = Field(min_length=1, max_length=190)
    provider_id: str | int | None = None
    voice: str | None = Field(default=None, max_length=100)
    instructions: str | None = Field(default=None, max_length=4096)
    max_duration_seconds: int = Field(default=300, ge=10, le=900)


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
    try:
        return await admit_realtime_session(payload.model_dump(), project_id=principal["project_id"],
            user_id=principal["user_id"], client_request_id=str(idempotency_key),
            source=principal.get("source", "web"), api_key_id=principal.get("api_key_id"))
    except (durable_errors.DurableRunError, credit.QuotaExceeded, ProviderValidationError) as exc:
        raise _error(exc) from exc


@router.websocket("/chat/realtime/sessions/{session_id}/ws")
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
    except WebSocketDisconnect:
        pass
    except Exception:
        await websocket.close(code=1011)
    else:
        await websocket.close(code=1000)
