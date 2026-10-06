"""OpenAI Responses-compatible stateless endpoint backed by LiteLLM native transport."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import aclosing

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse

from lumen.auth import require_api_key_scopes
from lumen.models.api_requests import ResponsesRequest
from lumen.services import completion_api as core
from lumen.services.infrastructure.api_load import admit_sse, register_sse_resource

from .streaming import events_with_ping

router = APIRouter()


def responses_error(status_code: int, message: str, *, code: str | None = None) -> JSONResponse:
    error_type = "invalid_request_error" if status_code < 500 else "api_error"
    return JSONResponse(
        status_code=status_code,
        content={"error": {"message": message, "type": error_type, "code": code}},
        headers={"Cache-Control": "no-store"},
    )


def _sse(event: dict) -> str:
    event_type = str(event.get("type") or "message")
    return f"event: {event_type}\ndata: {json.dumps(event, ensure_ascii=False, separators=(',', ':'))}\n\n"


@router.post(
    "/responses",
    openapi_extra={"security": [{"APIKeyBearer": []}, {"XApiKey": []}]},
)
@admit_sse
async def responses(
    request: Request,
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
        provider = core.select_api_provider(body.provider, x_lumen_provider)
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
    # The provider stream is already open; the SSE owner closes it even if the body never starts.
    register_sse_resource(request, result)

    async def generate() -> AsyncIterator[str]:
        async with aclosing(events_with_ping(result)) as events:
            try:
                async for event in events:
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
