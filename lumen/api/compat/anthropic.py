"""Anthropic Messages-compatible stateless endpoints using LiteLLM native transport."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from lumen.auth import require_api_key_scopes
from lumen.services import completion_api as core

from .streaming import events_with_ping

router = APIRouter()


class AnthropicMessagesRequest(BaseModel):
    model: str = Field(..., max_length=190)
    provider: str | None = Field(default=None, min_length=1, max_length=40, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    messages: list[dict[str, Any]] = Field(..., min_length=1)
    system: Any = None
    max_tokens: int = Field(..., gt=0)
    stream: bool = False
    temperature: float | None = None
    metadata: dict[str, Any] | None = None
    stop_sequences: list[str] | None = None
    thinking: dict[str, Any] | None = None
    context_management: dict[str, Any] | None = None
    tool_choice: dict[str, Any] | None = None
    tools: list[dict[str, Any]] | None = None
    top_k: int | None = None
    top_p: float | None = None
    container: dict[str, Any] | None = None
    output_config: dict[str, Any] | None = None

    model_config = {"extra": "forbid"}


class AnthropicCountTokensRequest(BaseModel):
    model: str = Field(..., max_length=190)
    provider: str | None = Field(default=None, min_length=1, max_length=40, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    messages: list[dict[str, Any]] = Field(..., min_length=1)
    system: Any = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: dict[str, Any] | None = None
    thinking: dict[str, Any] | None = None

    model_config = {"extra": "forbid"}


class AnthropicUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int | None = None
    cache_read_input_tokens: int | None = None

    model_config = {"extra": "allow"}


class AnthropicMessagesResponse(BaseModel):
    id: str
    type: str = "message"
    role: str = "assistant"
    model: str
    content: list[dict[str, Any]]
    stop_reason: str | None = None
    stop_sequence: str | None = None
    usage: AnthropicUsage

    model_config = {"extra": "allow"}


class AnthropicCountTokensResponse(BaseModel):
    input_tokens: int

    model_config = {"extra": "allow"}


def anthropic_error(status_code: int, message: str) -> dict:
    if status_code == 401:
        kind = "authentication_error"
    elif status_code == 403:
        kind = "permission_error"
    elif status_code == 404:
        kind = "not_found_error"
    elif status_code == 429:
        kind = "rate_limit_error"
    elif status_code < 500:
        kind = "invalid_request_error"
    else:
        kind = "api_error"
    return {"type": "error", "error": {"type": kind, "message": message}}


def anthropic_error_response(status_code: int, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=anthropic_error(status_code, message),
        headers={"Cache-Control": "no-store"},
    )


def _selected_provider(model: str, body_provider: str | None, header_provider: str | None) -> str | None:
    return core.select_api_provider(model, body_provider, header_provider)


def anthropic_protocol_headers(request: Request) -> dict[str, str]:
    """Forward only Anthropic protocol headers, never caller credentials."""
    headers: dict[str, str] = {}
    total_size = 0
    for name, value in request.headers.items():
        normalized = name.lower()
        if not normalized.startswith("anthropic-"):
            continue
        total_size += len(normalized) + len(value)
        if total_size > 16_384:
            raise core.CompletionError(400, "Anthropic protocol headers are too large")
        headers[normalized] = value
    return headers


def _event(payload: dict) -> str:
    event_type = str(payload.get("type") or "message")
    return f"event: {event_type}\ndata: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n"


@router.post(
    "/messages",
    response_model=AnthropicMessagesResponse,
    openapi_extra={"security": [{"APIKeyBearer": []}, {"XApiKey": []}]},
)
async def messages(
    body: AnthropicMessagesRequest,
    request: Request,
    x_lumen_provider: str | None = Header(default=None, alias="X-Lumen-Provider"),
    token_info: dict = Depends(require_api_key_scopes("compat:completions:write")),
):
    try:
        provider = _selected_provider(body.model, body.provider, x_lumen_provider)
        resolved = await core.resolve_api(body.model, provider=provider)
        await core.precheck(token_info["user_id"], token_info["project_id"], api_key_id=token_info.get("api_key_id"))
        options = body.model_dump(
            exclude={"model", "provider", "messages", "max_tokens", "stream"},
            exclude_none=True,
        )
        protocol_headers = anthropic_protocol_headers(request)
        if protocol_headers:
            options["anthropic_headers"] = protocol_headers
        result = await core.complete_anthropic(
            resolved=resolved,
            messages=body.messages,
            max_tokens=body.max_tokens,
            stream=body.stream,
            user_id=token_info["user_id"],
            project_id=token_info["project_id"],
            api_key_id=token_info.get("api_key_id"),
            options=options,
        )
    except core.CompletionError as exc:
        return anthropic_error_response(exc.status_code, exc.message)

    if not body.stream:
        return JSONResponse(content=result)

    async def generate() -> AsyncIterator[str]:
        try:
            async for payload in events_with_ping(result):
                if payload is None:
                    yield 'event: ping\ndata: {"type":"ping"}\n\n'
                else:
                    yield _event(payload)
        except Exception:
            yield _event(anthropic_error(502, "upstream model error"))

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


@router.post(
    "/messages/count_tokens",
    response_model=AnthropicCountTokensResponse,
    openapi_extra={"security": [{"APIKeyBearer": []}, {"XApiKey": []}]},
)
async def count_tokens(
    body: AnthropicCountTokensRequest,
    request: Request,
    x_lumen_provider: str | None = Header(default=None, alias="X-Lumen-Provider"),
    token_info: dict = Depends(require_api_key_scopes("compat:completions:write")),
):
    del token_info
    try:
        provider = _selected_provider(body.model, body.provider, x_lumen_provider)
        resolved = await core.resolve_api(body.model, provider=provider)
        payload = body.model_dump(exclude={"provider"}, exclude_none=True)
        return await core.count_anthropic_tokens(
            resolved=resolved,
            payload=payload,
            anthropic_headers=anthropic_protocol_headers(request),
        )
    except core.CompletionError as exc:
        return anthropic_error_response(exc.status_code, exc.message)
