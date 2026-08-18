"""OpenAI 호환 엔드포인트 — POST /v1/chat/completions, GET /v1/models.

litellm 청크가 이미 OpenAI 포맷이므로 변환은 얇다. 도구는 pass-through(모델 tool_calls 릴레이 →
클라이언트가 실행 후 재요청). 인증은 API 키(get_api_key_info). stateless — 대화 저장 안 함.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from lumen.auth import require_api_key_scopes
from lumen.services import completion_api as core
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


# ── 순수 변환 함수 (단위 테스트 대상) ──────────────────────────────────────
def openai_error(status_code: int, message: str) -> dict:
    return {"error": {"message": message, "type": "invalid_request_error" if status_code < 500 else "api_error"}}


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


def models_list(models: list[dict]) -> dict:
    return {
        "object": "list",
        "data": [
            {"id": m["model_name"], "object": "model", "created": 0, "owned_by": m.get("provider_name") or "afterglow"}
            for m in models
        ],
    }


# ── 엔드포인트 ──────────────────────────────────────────────────────────────
def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


@router.post("/chat/completions", openapi_extra={"security": [{"APIKeyBearer": []}, {"XApiKey": []}]})
async def chat_completions(
    body: OpenAIChatRequest, token_info: dict = Depends(require_api_key_scopes("compat:completions:write"))
):
    user_id, project_id = token_info["user_id"], token_info["project_id"]
    api_key_id = token_info.get("api_key_id")
    try:
        resolved = await core.resolve(body.model)
        await core.precheck(user_id, project_id)
    except core.CompletionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=openai_error(exc.status_code, exc.message)) from exc

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
            )
        except core.CompletionError as exc:
            raise HTTPException(status_code=exc.status_code, detail=openai_error(exc.status_code, exc.message)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=openai_error(502, "업스트림 모델 오류")) from exc
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
        ):
            if ev["type"] == "delta":
                yield _sse(chunk_dict(ev, cmpl_id=cmpl_id, created=created, model=resolved["model_name"]))
            elif ev["type"] == "done":
                if include_usage:
                    yield _sse(usage_chunk(ev, cmpl_id=cmpl_id, created=created, model=resolved["model_name"]))
            elif ev["type"] == "error":
                yield _sse(openai_error(502, ev.get("message", "오류")))
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.get("/models", openapi_extra={"security": [{"APIKeyBearer": []}, {"XApiKey": []}]})
async def list_models(token_info: dict = Depends(require_api_key_scopes("models:read"))):
    try:
        models = await repository.list_models(active_only=True)
    except errors.ChatStorageUnavailable:
        models = []
    return models_list(models)
