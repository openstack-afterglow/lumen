"""OpenAI 호환 엔드포인트 — POST /v1/chat/completions, GET /v1/models.

기존 모델은 LiteLLM/completion_api를 통한 직접 완료를 수행하며, model="lumen"은
Lumen native durable worker 실행 경계(create_temp_run, 저널 이벤트 투영)로 분기한다.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import aclosing

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from lumen.auth import ensure_scopes, require_api_key_scopes
from lumen.models.api_requests import OpenAIChatRequest, OpenAIChatResponse
from lumen.services import completion_api as core
from lumen.services import openai_compat
from lumen.services.completion_format import nonstream_response
from lumen.services.inference_authority import completion_scopes
from lumen.services.infrastructure.api_load import admit_sse
from lumen.services.providers import errors, routing

router = APIRouter()


class OpenAIModelItem(BaseModel):
    id: str
    object: str = "model"
    created: int = 0
    owned_by: str = "lumen"
    providers: list[str] = Field(default_factory=list)


class OpenAIModelListResponse(BaseModel):
    object: str = "list"
    data: list[OpenAIModelItem]


# ── 순수 변환 함수 (단위 테스트 대상) ──────────────────────────────────────
def openai_error_dict(message: str, type_: str = "invalid_request_error", code: str | None = None) -> dict:
    return {"error": {"message": message, "type": type_, "code": code}}


def openai_error_response(
    status_code: int, message: str, type_: str | None = None, code: str | None = None
) -> JSONResponse:
    if type_ is None:
        type_ = "invalid_request_error" if status_code < 500 else "api_error"
    return JSONResponse(status_code=status_code, content=openai_error_dict(message, type_=type_, code=code))


def _completion_id() -> str:
    return "chatcmpl-" + uuid.uuid4().hex


def chunk_dict(delta: dict, *, cmpl_id: str, created: int, model: str) -> dict:
    d: dict = {}
    if delta.get("content"):
        d["content"] = delta["content"]
    if delta.get("tool_calls"):
        d["tool_calls"] = delta["tool_calls"]
    return {
        "id": cmpl_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": d, "finish_reason": delta.get("finish_reason")}],
    }


def usage_chunk(done: dict, *, cmpl_id: str, created: int, model: str) -> dict:
    return {
        "id": cmpl_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [],
        "usage": {
            "prompt_tokens": done["prompt_tokens"],
            "completion_tokens": done["completion_tokens"],
            "total_tokens": done["prompt_tokens"] + done["completion_tokens"],
            "prompt_tokens_details": {"cached_tokens": done.get("cache_read_input_tokens", 0)},
        },
    }


def models_list(models: list[dict], include_lumen: bool = False) -> dict:
    data = []
    if include_lumen:
        data.append(
            {
                "id": openai_compat.VIRTUAL_MODEL_ID,
                "object": "model",
                "created": 0,
                "owned_by": "lumen",
                "providers": [],
            }
        )
    by_public_id: dict[str, dict] = {}
    for model in models:
        public_name = model["api_model_name"]
        if public_name.strip().lower() == openai_compat.VIRTUAL_MODEL_ID:
            continue
        item = by_public_id.get(public_name)
        if item is None:
            item = {
                "id": public_name,
                "object": "model",
                "created": 0,
                "owned_by": "lumen",
                "providers": [],
            }
            by_public_id[public_name] = item
            data.append(item)
        provider = model["api_provider"]
        if provider not in item["providers"]:
            item["providers"].append(provider)
    for item in data:
        item["providers"].sort()
    return {
        "object": "list",
        "data": data,
    }


# ── 엔드포인트 ──────────────────────────────────────────────────────────────
def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


@router.post(
    "/chat/completions",
    response_model=OpenAIChatResponse,
    openapi_extra={"security": [{"APIKeyBearer": []}, {"XApiKey": []}]},
)
@admit_sse
async def chat_completions(
    request: Request,
    body: OpenAIChatRequest,
    x_lumen_provider: str | None = Header(
        default=None,
        alias="X-Lumen-Provider",
        min_length=1,
        max_length=40,
        pattern=r"^[a-z0-9][a-z0-9_-]*$",
    ),
    token_info: dict = Depends(require_api_key_scopes("compat:completions:write")),
):
    ensure_scopes(token_info, *completion_scopes(body.model_dump(exclude_none=True)))
    if body.max_tokens is not None and body.max_tokens <= 0:
        return openai_error_response(400, "max_tokens must be positive", code="invalid_max_tokens")
    user_id, project_id = token_info["user_id"], token_info["project_id"]
    api_key_id = token_info.get("api_key_id")
    source = str(token_info.get("source") or "api")
    try:
        selected_provider = core.select_api_provider(body.provider, x_lumen_provider)
    except core.CompletionError as exc:
        return openai_error_response(exc.status_code, exc.message)

    if openai_compat.is_lumen_virtual_model(body.model):
        if selected_provider is not None:
            return openai_error_response(
                400,
                "provider is not supported for model=lumen",
                code="provider_not_supported_for_lumen",
            )
        try:
            normalized_messages, last_user_text, capped_max_tokens, validated_temp = (
                openai_compat.validate_and_normalize_transcript(
                    body.messages,
                    tools=body.tools,
                    tool_choice=body.tool_choice,
                    max_tokens=body.max_tokens,
                    temperature=body.temperature,
                )
            )
            run_id, _resolved_model = await openai_compat.create_lumen_temp_run(
                normalized_messages=normalized_messages,
                last_user_text=last_user_text,
                user_id=user_id,
                project_id=project_id,
                api_key_id=api_key_id,
                source=source,
                max_tokens=capped_max_tokens,
                temperature=validated_temp,
            )
        except openai_compat.OpenAICompatError as exc:
            return openai_error_response(exc.status_code, exc.message, type_=exc.type_, code=exc.code)

        cmpl_id, created = f"chatcmpl-lumen-{run_id}", int(time.time())

        if not body.stream:
            try:
                result = await openai_compat.execute_lumen_nonstream(
                    run_id=run_id,
                    user_id=user_id,
                    project_id=project_id,
                    is_disconnected=request.is_disconnected,
                )
                return nonstream_response(result, cmpl_id=cmpl_id, created=created)
            except openai_compat.OpenAICompatError as exc:
                return openai_error_response(exc.status_code, exc.message, type_=exc.type_, code=exc.code)

        include_usage = bool((body.stream_options or {}).get("include_usage"))

        async def gen() -> AsyncIterator[str]:
            async with aclosing(openai_compat.execute_lumen_stream(
                run_id=run_id,
                user_id=user_id,
                project_id=project_id,
                cmpl_id=cmpl_id,
                created=created,
                include_usage=include_usage,
                is_disconnected=request.is_disconnected,
            )) as events:
                async for ev in events:
                    if ev["kind"] in {"chunk", "error"}:
                        yield _sse(ev["data"])
                    elif ev["kind"] == "keepalive":
                        yield ": keepalive\n\n"
                    elif ev["kind"] == "done":
                        yield "data: [DONE]\n\n"

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
        )

    try:
        resolved = await core.resolve_api(body.model, provider=selected_provider)
        await core.precheck(user_id, project_id, api_key_id=api_key_id)
    except core.CompletionError as exc:
        return openai_error_response(exc.status_code, exc.message)

    cmpl_id, created = _completion_id(), int(time.time())
    include_usage = bool((body.stream_options or {}).get("include_usage"))

    if not body.stream:
        try:
            result = await core.complete_once(
                resolved=resolved,
                messages=body.messages,
                user_id=user_id,
                project_id=project_id,
                api_key_id=api_key_id,
                max_tokens=body.max_tokens,
                temperature=body.temperature,
                tools=body.tools,
                tool_choice=body.tool_choice,
            )
        except core.CompletionError as exc:
            return openai_error_response(exc.status_code, exc.message)
        except Exception:
            return openai_error_response(502, "업스트림 모델 오류")
        return nonstream_response(result, cmpl_id=cmpl_id, created=created)

    try:
        stream = core.complete_stream(
            resolved=resolved,
            messages=body.messages,
            user_id=user_id,
            project_id=project_id,
            api_key_id=api_key_id,
            max_tokens=body.max_tokens,
            temperature=body.temperature,
            tools=body.tools,
            tool_choice=body.tool_choice,
        )
    except core.CompletionError as exc:
        return openai_error_response(exc.status_code, exc.message)
    except Exception:
        return openai_error_response(502, "업스트림 모델 오류")

    async def gen() -> AsyncIterator[str]:
        async with aclosing(stream):
            async for ev in stream:
                if ev["type"] == "delta":
                    yield _sse(chunk_dict(ev, cmpl_id=cmpl_id, created=created, model=resolved["api_model_name"]))
                elif ev["type"] == "done":
                    if include_usage:
                        yield _sse(usage_chunk(ev, cmpl_id=cmpl_id, created=created, model=resolved["api_model_name"]))
                elif ev["type"] == "error":
                    yield _sse(openai_error_dict(ev.get("message", "오류"), type_="api_error"))
                    return
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.get(
    "/models",
    response_model=OpenAIModelListResponse,
    openapi_extra={"security": [{"APIKeyBearer": []}, {"XApiKey": []}]},
)
async def list_models(
    x_lumen_provider: str | None = Header(
        default=None,
        alias="X-Lumen-Provider",
        min_length=1,
        max_length=40,
        pattern=r"^[a-z0-9][a-z0-9_-]*$",
    ),
    token_info: dict = Depends(require_api_key_scopes("models:read")),
):
    del token_info
    try:
        models = await routing.list_api_models()
    except errors.ChatStorageUnavailable:
        models = []
    if x_lumen_provider is not None:
        models = [model for model in models if model.get("api_provider") == x_lumen_provider]
    include_lumen = x_lumen_provider is None and await openai_compat.is_virtual_model_active(models)
    return models_list(models, include_lumen=include_lumen)
