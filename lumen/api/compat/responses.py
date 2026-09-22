"""OpenAI Responses-compatible stateless endpoint backed by LiteLLM native transport."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from lumen.auth import require_api_key_scopes
from lumen.services import completion_api as core

from .streaming import events_with_ping

router = APIRouter()


class ResponsesRequest(BaseModel):
    model: str = Field(..., max_length=190)
    provider: str | None = Field(default=None, min_length=1, max_length=40, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    input: str | list[dict[str, Any]]
    stream: bool = False
    store: bool | None = None
    previous_response_id: str | None = None
    background: bool | None = None
    include: list[str] | None = None
    prompt_cache_key: str | None = None
    client_metadata: dict[str, Any] | None = None
    instructions: str | None = None
    max_output_tokens: int | None = Field(default=None, gt=0)
    metadata: dict[str, Any] | None = None
    parallel_tool_calls: bool | None = None
    reasoning: dict[str, Any] | None = None
    temperature: float | None = None
    text: dict[str, Any] | None = None
    tool_choice: Any = None
    tools: list[dict[str, Any]] | None = None
    top_p: float | None = None
    truncation: Literal["auto", "disabled"] | None = None
    user: str | None = None
    service_tier: str | None = None
    safety_identifier: str | None = None
    # Accepting this lets a caller keep its own context strategy. Lumen only
    # supplies a default when the field is absent; it never overrides one.
    context_management: list[dict[str, Any]] | None = None

    model_config = {"extra": "forbid"}


def responses_error(status_code: int, message: str, *, code: str | None = None) -> JSONResponse:
    error_type = "invalid_request_error" if status_code < 500 else "api_error"
    return JSONResponse(
        status_code=status_code,
        content={"error": {"message": message, "type": error_type, "code": code}},
        headers={"Cache-Control": "no-store"},
    )


def _selected_provider(model: str, body_provider: str | None, header_provider: str | None) -> str | None:
    return core.select_api_provider(model, body_provider, header_provider)


def _sse(event: dict) -> str:
    event_type = str(event.get("type") or "message")
    return f"event: {event_type}\ndata: {json.dumps(event, ensure_ascii=False, separators=(',', ':'))}\n\n"


@router.post(
    "/responses",
    openapi_extra={"security": [{"APIKeyBearer": []}, {"XApiKey": []}]},
)
async def responses(
    body: ResponsesRequest,
    x_lumen_provider: str | None = Header(default=None, alias="X-Lumen-Provider"),
    token_info: dict = Depends(require_api_key_scopes("compat:completions:write")),
):
    if body.store is True:
        return responses_error(400, "store=true is not supported", code="stateful_responses_not_supported")
    if body.previous_response_id is not None:
        return responses_error(400, "previous_response_id is not supported", code="stateful_responses_not_supported")
    if body.background is True:
        return responses_error(400, "background responses are not supported", code="background_not_supported")

    try:
        provider = _selected_provider(body.model, body.provider, x_lumen_provider)
        resolved = await core.resolve_api(body.model, provider=provider)
        await core.precheck(token_info["user_id"], token_info["project_id"], api_key_id=token_info.get("api_key_id"))
        options = body.model_dump(
            exclude={
                "model",
                "provider",
                "input",
                "stream",
                "store",
                "previous_response_id",
                "background",
                "client_metadata",
            },
            exclude_none=True,
        )
        result = await core.complete_responses(
            resolved=resolved,
            input=body.input,
            stream=body.stream,
            user_id=token_info["user_id"],
            project_id=token_info["project_id"],
            api_key_id=token_info.get("api_key_id"),
            options=options,
        )
    except core.CompletionError as exc:
        return responses_error(exc.status_code, exc.message)

    if not body.stream:
        return JSONResponse(content=result)

    async def generate() -> AsyncIterator[str]:
        try:
            async for event in events_with_ping(result):
                if event is None:
                    yield ": ping\n\n"
                else:
                    yield _sse(event)
        except Exception:
            yield _sse(
                {
                    "type": "error",
                    "error": {"type": "api_error", "message": "upstream model error", "code": None},
                }
            )

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )
