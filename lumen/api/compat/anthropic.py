"""Anthropic(Claude) 호환 엔드포인트 — POST /v1/messages, GET /v1/models(공유).

내부는 OpenAI 포맷 messages 로 변환해 litellm 호출 후, Anthropic 포맷으로 재변환한다.
도구는 pass-through(tool_use 릴레이). 인증은 API 키. stateless — 대화 저장 안 함.
스트리밍은 Anthropic 이벤트 프로토콜(message_start/content_block_delta/…)을 따른다.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from lumen.auth import require_api_key_scopes
from lumen.services import completion_api as core

router = APIRouter()


class AnthropicMessagesRequest(BaseModel):
    model: str = Field(..., max_length=190)
    messages: list[dict] = Field(..., min_length=1)
    system: Any = None  # str 또는 [{type:text,text}]
    max_tokens: int | None = None
    stream: bool = False
    temperature: float | None = None
    tools: list[dict] | None = None
    tool_choice: Any = None

    model_config = {"extra": "allow"}


# ── 순수 변환 함수 (단위 테스트 대상) ──────────────────────────────────────
def anthropic_error(status_code: int, message: str) -> dict:
    kind = "invalid_request_error" if status_code < 500 else "api_error"
    return {"type": "error", "error": {"type": kind, "message": message}}


def _flatten_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(b.get("text", "") for b in value if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _anthropic_tools_to_openai(tools: list[dict] | None) -> list[dict] | None:
    """Anthropic tools([{name,description,input_schema}]) → OpenAI function tools."""
    if not tools:
        return None
    out = []
    for t in tools:
        if not isinstance(t, dict) or not t.get("name"):
            continue
        out.append(
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description") or "",
                    "parameters": t.get("input_schema") or {"type": "object", "properties": {}},
                },
            }
        )
    return out or None


def to_internal_messages(body: AnthropicMessagesRequest) -> list[dict]:
    """Anthropic 요청 → 내부 OpenAI 포맷 messages. system·tool_use·tool_result 변환."""
    msgs: list[dict] = []
    if body.system:
        sys_text = _flatten_text(body.system)
        if sys_text:
            msgs.append({"role": "system", "content": sys_text})
    for m in body.messages:
        role = m.get("role", "user")
        content = m.get("content")
        if isinstance(content, str):
            msgs.append({"role": role, "content": content})
            continue
        texts: list[str] = []
        tool_calls: list[dict] = []
        for block in content or []:
            if not isinstance(block, dict):
                continue
            bt = block.get("type")
            if bt == "text":
                texts.append(block.get("text", ""))
            elif bt == "tool_use":  # 이전 assistant 의 도구 호출
                tool_calls.append(
                    {
                        "id": block.get("id"),
                        "type": "function",
                        "function": {
                            "name": block.get("name"),
                            "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False),
                        },
                    }
                )
            elif bt == "tool_result":  # user 가 돌려준 도구 결과
                msgs.append(
                    {
                        "role": "tool",
                        "tool_call_id": block.get("tool_use_id"),
                        "content": _flatten_text(block.get("content")),
                    }
                )
        if tool_calls:
            msgs.append({"role": "assistant", "content": ("\n".join(texts) or None), "tool_calls": tool_calls})
        elif texts:
            msgs.append({"role": role, "content": "\n".join(texts)})
    return msgs


def _message_id() -> str:
    return "msg_" + uuid.uuid4().hex


def _parse_args(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw) if raw else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def nonstream_response(result: dict, *, msg_id: str) -> dict:
    content: list[dict] = []
    if result.get("content"):
        content.append({"type": "text", "text": result["content"]})
    stop_reason = "end_turn"
    if result.get("tool_calls"):
        for i, tc in enumerate(result["tool_calls"]):
            content.append(
                {
                    "type": "tool_use",
                    "id": tc.get("id") or f"toolu_{i}",
                    "name": tc["function"].get("name"),
                    "input": _parse_args(tc["function"].get("arguments")),
                }
            )
        stop_reason = "tool_use"
    return {
        "id": msg_id,
        "type": "message",
        "role": "assistant",
        "model": result["model"],
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": result["prompt_tokens"], "output_tokens": result["completion_tokens"]},
    }


def _accumulate_tool_calls(acc: dict[int, dict], tcs: list[dict] | None) -> None:
    """스트림 tool_call 조각(index별)을 누적한다."""
    for tc in tcs or []:
        idx = tc.get("index") or 0
        slot = acc.setdefault(idx, {"id": None, "name": None, "arguments": ""})
        if tc.get("id"):
            slot["id"] = tc["id"]
        fn = tc.get("function") or {}
        if fn.get("name"):
            slot["name"] = fn["name"]
        if fn.get("arguments"):
            slot["arguments"] += fn["arguments"]


# ── 엔드포인트 ──────────────────────────────────────────────────────────────
def _event(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.post("/messages", openapi_extra={"security": [{"APIKeyBearer": []}, {"XApiKey": []}]})
async def messages(
    body: AnthropicMessagesRequest, token_info: dict = Depends(require_api_key_scopes("compat:completions:write"))
):
    user_id, project_id = token_info["user_id"], token_info["project_id"]
    api_key_id = token_info.get("api_key_id")
    internal = to_internal_messages(body)
    tools = _anthropic_tools_to_openai(body.tools)
    try:
        resolved = await core.resolve(body.model)
        await core.precheck(user_id, project_id)
    except core.CompletionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=anthropic_error(exc.status_code, exc.message)) from exc

    msg_id = _message_id()

    if not body.stream:
        try:
            result = await core.complete_once(
                resolved=resolved,
                messages=internal,
                user_id=user_id,
                project_id=project_id,
                api_key_id=api_key_id,
                max_tokens=body.max_tokens,
                temperature=body.temperature,
                tools=tools,
            )
        except core.CompletionError as exc:
            raise HTTPException(
                status_code=exc.status_code, detail=anthropic_error(exc.status_code, exc.message)
            ) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=anthropic_error(502, "업스트림 모델 오류")) from exc
        return nonstream_response(result, msg_id=msg_id)

    async def gen() -> AsyncIterator[str]:
        model = resolved["model_name"]
        yield _event(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": msg_id,
                    "type": "message",
                    "role": "assistant",
                    "model": model,
                    "content": [],
                    "stop_reason": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            },
        )
        yield _event(
            "content_block_start",
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        )
        tool_acc: dict[int, dict] = {}
        out_tokens = 0
        async for ev in core.complete_stream(
            resolved=resolved,
            messages=internal,
            user_id=user_id,
            project_id=project_id,
            api_key_id=api_key_id,
            max_tokens=body.max_tokens,
            temperature=body.temperature,
            tools=tools,
        ):
            if ev["type"] == "delta":
                if ev.get("content"):
                    yield _event(
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": 0,
                            "delta": {"type": "text_delta", "text": ev["content"]},
                        },
                    )
                _accumulate_tool_calls(tool_acc, ev.get("tool_calls"))
            elif ev["type"] == "done":
                out_tokens = ev["completion_tokens"]
            elif ev["type"] == "error":
                yield _event("error", anthropic_error(502, ev.get("message", "오류")))
        yield _event("content_block_stop", {"type": "content_block_stop", "index": 0})
        # 누적된 tool_use 블록(있으면) 방출
        stop_reason = "end_turn"
        for i, (_, slot) in enumerate(sorted(tool_acc.items()), start=1):
            stop_reason = "tool_use"
            yield _event(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": i,
                    "content_block": {
                        "type": "tool_use",
                        "id": slot["id"] or f"toolu_{i}",
                        "name": slot["name"],
                        "input": {},
                    },
                },
            )
            yield _event(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": i,
                    "delta": {"type": "input_json_delta", "partial_json": slot["arguments"] or "{}"},
                },
            )
            yield _event("content_block_stop", {"type": "content_block_stop", "index": i})
        yield _event(
            "message_delta",
            {"type": "message_delta", "delta": {"stop_reason": stop_reason}, "usage": {"output_tokens": out_tokens}},
        )
        yield _event("message_stop", {"type": "message_stop"})

    return StreamingResponse(gen(), media_type="text/event-stream")
