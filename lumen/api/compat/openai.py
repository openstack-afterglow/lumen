"""OpenAI 호환 엔드포인트 — POST /v1/chat/completions, GET /v1/models.

기존 모델은 LiteLLM/completion_api를 통한 직접 완료를 수행하며, model="lumen"은
Lumen native durable worker 실행 경계(create_temp_run, 저널 이벤트 투영)로 분기한다.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from lumen.auth import require_api_key_scopes
from lumen.services import completion_api as core
from lumen.services import openai_compat
from lumen.services.providers import errors, repository

router = APIRouter()


class OpenAIChatRequest(BaseModel):
    model: str = Field(..., max_length=190)
    messages: list[dict] = Field(..., min_length=1)
    stream: bool = False
    temperature: float | None = None
    max_tokens: int | None = None
    tools: list[dict] | None = None
    tool_choice: Any = None
    stream_options: dict | None = None

    model_config = {"extra": "allow"}  # 미지원 OpenAI 파라미터는 무시(호환성)


class OpenAIChatChoiceMessage(BaseModel):
    role: str = "assistant"
    content: str | None = None
    tool_calls: list[dict] | None = None


class OpenAIChatChoice(BaseModel):
    index: int = 0
    message: OpenAIChatChoiceMessage
    finish_reason: str = "stop"


class OpenAIUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class OpenAIChatResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: list[OpenAIChatChoice]
    usage: OpenAIUsage


class OpenAIModelItem(BaseModel):
    id: str
    object: str = "model"
    created: int = 0
    owned_by: str = "lumen"


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


def nonstream_response(result: dict, *, cmpl_id: str, created: int) -> dict:
    msg: dict = {"role": "assistant", "content": result["content"] or None}
    if result.get("tool_calls"):
        msg["tool_calls"] = [
            {
                "id": tc.get("id") or f"call_{i}",
                "type": "function",
                "function": {"name": tc["function"].get("name"), "arguments": tc["function"].get("arguments") or "{}"},
            }
            for i, tc in enumerate(result["tool_calls"])
        ]
    return {
        "id": cmpl_id,
        "object": "chat.completion",
        "created": created,
        "model": result["model"],
        "choices": [{"index": 0, "message": msg, "finish_reason": result["finish_reason"]}],
        "usage": {
            "prompt_tokens": result["prompt_tokens"],
            "completion_tokens": result["completion_tokens"],
            "total_tokens": result["prompt_tokens"] + result["completion_tokens"],
        },
    }


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
            }
        )
    for m in models:
        model_name = m.get("model_name", "")
        # Filter reserved virtual model ID from provider list to prevent duplicates
        if model_name.strip().lower() == openai_compat.VIRTUAL_MODEL_ID:
            continue
        data.append(
            {
                "id": model_name,
                "object": "model",
                "created": 0,
                "owned_by": m.get("provider_name") or "lumen",
            }
        )
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
async def chat_completions(
    request: Request,
    body: OpenAIChatRequest,
    token_info: dict = Depends(require_api_key_scopes("compat:completions:write")),
):
    user_id, project_id = token_info["user_id"], token_info["project_id"]
    api_key_id = token_info.get("api_key_id")
    source = token_info.get("source", "api")

    if openai_compat.is_lumen_virtual_model(body.model):
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
            async for ev in openai_compat.execute_lumen_stream(
                run_id=run_id,
                user_id=user_id,
                project_id=project_id,
                cmpl_id=cmpl_id,
                created=created,
                include_usage=include_usage,
                is_disconnected=request.is_disconnected,
            ):
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
        resolved = await core.resolve(body.model)
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

    async def gen() -> AsyncIterator[str]:
        async for ev in core.complete_stream(
            resolved=resolved,
            messages=body.messages,
            user_id=user_id,
            project_id=project_id,
            api_key_id=api_key_id,
            max_tokens=body.max_tokens,
            temperature=body.temperature,
            tools=body.tools,
            tool_choice=body.tool_choice,
        ):
            if ev["type"] == "delta":
                yield _sse(chunk_dict(ev, cmpl_id=cmpl_id, created=created, model=resolved["model_name"]))
            elif ev["type"] == "done":
                if include_usage:
                    yield _sse(usage_chunk(ev, cmpl_id=cmpl_id, created=created, model=resolved["model_name"]))
            elif ev["type"] == "error":
                yield _sse(openai_error_dict(ev.get("message", "오류"), type_="api_error"))
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.get(
    "/models",
    response_model=OpenAIModelListResponse,
    openapi_extra={"security": [{"APIKeyBearer": []}, {"XApiKey": []}]},
)
async def list_models(token_info: dict = Depends(require_api_key_scopes("models:read"))):
    try:
        models = await repository.list_models(active_only=True)
    except errors.ChatStorageUnavailable:
        models = []
    include_lumen = await openai_compat.is_virtual_model_active(models)
    return models_list(models, include_lumen=include_lumen)
