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
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from lumen.auth import ensure_scopes, require_api_key_scopes
from lumen.models.api_requests import OpenAIChatRequest, OpenAIChatResponse
from lumen.services import completion_api as core
from lumen.services import openai_compat
from lumen.services.completion_format import nonstream_response, prompt_token_details
from lumen.services.inference_authority import completion_scopes, principal_may_use
from lumen.services.infrastructure.api_load import admit_sse, register_sse_resource
from lumen.services.providers import errors, routing

from .anthropic import anthropic_error_response

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


class AnthropicModelItem(BaseModel):
    id: str
    type: Literal["model"] = "model"
    display_name: str
    created_at: datetime = datetime.fromtimestamp(0, UTC)


class AnthropicModelListResponse(BaseModel):
    data: list[AnthropicModelItem]
    first_id: str | None
    last_id: str | None
    has_more: bool


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
            "prompt_tokens_details": prompt_token_details(done),
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


def _anthropic_models_page(
    models: list[dict], *, limit: int, after_id: str | None, before_id: str | None
) -> AnthropicModelListResponse:
    if not 1 <= limit <= 1000 or (after_id is not None and before_id is not None):
        raise ValueError("Use a limit from 1 to 1000 and at most one pagination cursor.")
    model_ids = list(dict.fromkeys(
        model["api_model_name"] for model in models
        if model["api_model_name"].strip().lower() != openai_compat.VIRTUAL_MODEL_ID
    ))
    cursor = before_id if before_id is not None else after_id
    if cursor is not None and cursor not in model_ids:
        raise ValueError("Unknown model pagination cursor.")
    position = model_ids.index(cursor) if cursor is not None else -1
    if before_id is not None:
        start = max(0, position - limit)
        page_ids = model_ids[start:position]
        has_more = start > 0
    else:
        start = position + 1
        page_ids = model_ids[start:start + limit]
        has_more = start + limit < len(model_ids)
    return AnthropicModelListResponse(
        data=[AnthropicModelItem(id=model_id, display_name=model_id) for model_id in page_ids],
        first_id=page_ids[0] if page_ids else None,
        last_id=page_ids[-1] if page_ids else None,
        has_more=has_more,
    )


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
    if body.service_tier not in (None, "default", "auto"):
        return openai_error_response(422, "Only Standard processing is supported.", code="service_tier_unsupported")
    if body.max_completion_tokens is None and body.max_tokens is not None and body.max_tokens <= 0:
        return openai_error_response(400, "max_tokens must be positive", code="invalid_max_tokens")
    user_id, project_id = token_info["user_id"], token_info["project_id"]
    api_key_id = token_info.get("api_key_id")
    source = str(token_info.get("source") or "api")
    try:
        selected_provider = core.select_api_provider(body.provider, x_lumen_provider)
    except core.CompletionError as exc:
        return openai_error_response(exc.status_code, exc.message)

    if openai_compat.is_lumen_virtual_model(body.model):
        if token_info.get("credential_class") in {"personal_key", "service_key"}:
            return openai_error_response(403, "Virtual model is unavailable to model API keys.",
                                         code="model_not_allowed_for_key")
        if body.provider_options():
            return openai_error_response(422, "Provider options are not supported for model=lumen",
                                         code="provider_options_not_supported_for_lumen")
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
                payer=token_info.get("payer"),
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
        await core.precheck(user_id, project_id, api_key_id=api_key_id, payer=token_info.get("payer"))
    except core.CompletionError as exc:
        return openai_error_response(exc.status_code, exc.message, code=exc.code)

    cmpl_id, created = _completion_id(), int(time.time())
    options = body.provider_options()
    max_output = body.max_tokens
    if core._official_openai(resolved) and body.max_completion_tokens is not None:
        max_output = body.max_completion_tokens
    if max_output is not None and max_output <= 0:
        return openai_error_response(400, "max_tokens must be positive", code="invalid_max_tokens")
    include_usage = bool((body.stream_options or {}).get("include_usage"))

    if not body.stream:
        try:
            result = await core.complete_once(
                resolved=resolved,
                messages=body.messages,
                user_id=user_id,
                project_id=project_id,
                api_key_id=api_key_id,
                payer=token_info.get("payer"),
                max_tokens=max_output,
                temperature=body.temperature,
                tools=body.tools,
                tool_choice=body.tool_choice,
                options=options,
            )
        except core.CompletionError as exc:
            return openai_error_response(exc.status_code, exc.message, code=exc.code)
        except Exception:
            return openai_error_response(502, "업스트림 모델 오류")
        return nonstream_response(result, cmpl_id=cmpl_id, created=created)

    try:
        stream = await core.complete_stream(
            resolved=resolved,
            messages=body.messages,
            user_id=user_id,
            project_id=project_id,
            api_key_id=api_key_id,
            payer=token_info.get("payer"),
            max_tokens=max_output,
            temperature=body.temperature,
            tools=body.tools,
            tool_choice=body.tool_choice,
            options=options,
        )
    except core.CompletionError as exc:
        return openai_error_response(exc.status_code, exc.message, code=exc.code)
    except Exception:
        return openai_error_response(502, "업스트림 모델 오류")
    register_sse_resource(request, stream)

    async def gen() -> AsyncIterator[str]:
        async with aclosing(stream):
            async for ev in stream:
                if ev["type"] == "delta":
                    yield _sse(chunk_dict(ev, cmpl_id=cmpl_id, created=created, model=resolved["api_model_name"]))
                elif ev["type"] == "done":
                    if include_usage:
                        yield _sse(usage_chunk(ev, cmpl_id=cmpl_id, created=created, model=resolved["api_model_name"]))
                elif ev["type"] == "error":
                    yield _sse(openai_error_dict(ev.get("message", "오류"), type_="api_error", code=ev.get("code")))
                    return
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.get(
    "/models",
    response_model=OpenAIModelListResponse | AnthropicModelListResponse,
    openapi_extra={"security": [{"APIKeyBearer": []}, {"XApiKey": []}]},
)
async def list_models(
    response: Response,
    anthropic_version: str | None = Header(default=None, alias="Anthropic-Version"),
    limit: str = Query(default="20"),
    after_id: str | None = Query(default=None),
    before_id: str | None = Query(default=None),
    x_lumen_provider: str | None = Header(
        default=None,
        alias="X-Lumen-Provider",
        min_length=1,
        max_length=40,
        pattern=r"^[a-z0-9][a-z0-9_-]*$",
    ),
    token_info: dict = Depends(require_api_key_scopes("models:read")),
):
    vary = "Anthropic-Version, X-Lumen-Provider"
    response.headers.append("Vary", vary)
    try:
        if anthropic_version is None:
            models = await routing.list_api_models()
            models.extend(await routing.list_api_models(model_kind="embedding"))
        else:
            # Messages SDKs list only rows /v1/messages can execute (model-level
            # transport rule: bounded official OpenAI rows lack Messages accounting).
            models = [{"api_model_name": row["api_model_name"], "api_provider": row["provider"],
                       "required_scopes": row["required_scopes"]}
                      for row in await routing.list_messages_api_models()]
    except errors.ChatStorageUnavailable:
        models = []
    # Discovery mirrors dispatch: hide routes whose intrinsic scopes (Sonar built-in
    # search needs native:tools:execute) this caller cannot hold.
    models = [model for model in models if principal_may_use(token_info, model["required_scopes"])]
    if x_lumen_provider is not None:
        models = [model for model in models if model.get("api_provider") == x_lumen_provider]
    if anthropic_version is not None:
        try:
            page_limit = int(limit)
        except ValueError:
            page_limit = 0
        try:
            return _anthropic_models_page(models, limit=page_limit, after_id=after_id, before_id=before_id)
        except ValueError as exc:
            error = anthropic_error_response(400, str(exc))
            error.headers.append("Vary", vary)
            return error
    include_lumen = (
        x_lumen_provider is None
        and token_info.get("credential_class") not in {"personal_key", "service_key"}
        and await openai_compat.is_virtual_model_active(models)
    )
    return models_list(models, include_lumen=include_lumen)
