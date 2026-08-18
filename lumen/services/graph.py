"""Phase 2 chat_engine — LangGraph + litellm 에이전트 루프(내부 툴, MCP 미포함).

설계 결정(2026-07-18):
- litellm_client 를 그대로 쓰고(ChatLiteLLM/langchain-community 미도입), LangGraph 는 그래프 구조·
  상태·스트리밍만 담당한다. 에이전트 루프(모델↔툴)는 노드 내부에서 돌린다.
- litellm 은 LangChain 모델이 아니므로 stream_mode="messages" 대신 **stream_mode="custom" +
  get_stream_writer()** 로 토큰/툴 이벤트를 emit 한다.
- 툴은 자체 루프로 실행: 스트리밍하며 텍스트 델타와 tool_call 델타를 누적하고, tool_call 이 있으면
  **테넌트 안전(tools.execute_tool, ToolContext)** 하게 실행 후 결과를 붙여 다음 턴으로 넘긴다.
- 멀티스텝(툴 호출로 여러 litellm 콜) usage 를 합산해 마지막에 1회 emit → 엔드포인트가 과금.
- 이벤트 계약: token / tool_call / usage / error → completions 엔드포인트가 SSE 로 중계.
- 대화 전사는 MySQL chat_messages(source of truth)에서 매 요청 로드해 messages 로 주입.
- 외부 MCP 서버 연동은 pydantic/uvicorn/httpx 핀 이동 필요(별도 결정) — 여기선 내부 툴만.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any, TypedDict

from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from lumen.services import agent_runtime_v2, litellm_client
from lumen.services.checkpointer import chat_checkpointer
from lumen.services.tool_runtime import bindings, contracts, dispatch, selection
from lumen.services.tools import ToolContext

logger = logging.getLogger(__name__)

_MAX_TOOL_STEPS = 4  # 툴 실행 라운드 상한(무한 루프 방지) — 초과 시 마지막 턴을 최종 답변으로

# Generic reasoning rejections are retried without the parameter.
_REASONING_UNSUPPORTED: set[str] = set()
# OpenAI Chat Completions models that require the explicit `none` value when tools are sent.
_TOOL_REASONING_EXPLICIT_NONE: set[str] = set()
_MAX_INTERNAL_TOOL_CALL_ID = 190


def _requires_explicit_none_for_tools(
    model: str, custom_llm_provider: str | None, reasoning_effort: str | None
) -> bool:
    """Make OpenAI's implicit automatic reasoning explicit only for tool requests."""
    normalized_effort = reasoning_effort.strip().lower() if isinstance(reasoning_effort, str) else None
    return (
        custom_llm_provider == "openai"
        and model.strip().lower().startswith("gpt-5")
        and normalized_effort in {None, "", "auto"}
    )


class _BoundaryAbort(RuntimeError):
    """A durable hook detected an indeterminate external-call boundary."""


def _abort_code(payload: Any) -> str | None:
    if isinstance(payload, dict) and isinstance(payload.get("_boundary_abort"), str):
        return payload["_boundary_abort"]
    return None


def _is_tool_reasoning_conflict(error: Exception) -> bool:
    """OpenAI Chat Completions가 tool+기본 추론을 거부했는지 판별한다."""
    message = str(error).lower()
    return "function tools" in message and "reasoning_effort" in message


class ChatState(TypedDict, total=False):
    messages: list[dict]
    pending_tool_calls: list[dict]
    classified_tool_calls: list[dict]
    denied_tool_calls: list[dict]
    policy_limited_tool_calls: list[dict]
    effect_denied_tool_calls: list[dict]
    tool_call_count: int
    model_turn_count: int
    tool_call_ids: list[str]
    loop_count: int
    final_text: str
    citations: list[dict]
    usage: dict[str, int]
    tool_usage: list[dict[str, object]]
    model_failed: bool


def _delta(chunk) -> Any:
    choices = getattr(chunk, "choices", None)
    if choices is None and isinstance(chunk, dict):
        choices = chunk.get("choices")
    if not choices:
        return None
    first = choices[0]
    d = getattr(first, "delta", None)
    if d is None and isinstance(first, dict):
        d = first.get("delta")
    return d


def _delta_content(delta) -> str | None:
    content = getattr(delta, "content", None)
    if content is None and isinstance(delta, dict):
        content = delta.get("content")
    return content


def _delta_reasoning(delta) -> str | None:
    """추론 델타(reasoning_content) 추출 — litellm 이 provider 무관하게 정규화한 필드."""
    reasoning = getattr(delta, "reasoning_content", None)
    if reasoning is None and isinstance(delta, dict):
        reasoning = delta.get("reasoning_content")
    return reasoning if isinstance(reasoning, str) and reasoning else None


def _accumulate_tool_calls(delta, acc: dict[int, dict]) -> None:
    """스트리밍 tool_call 델타(index별 부분)를 acc 에 누적."""
    tcs = getattr(delta, "tool_calls", None)
    if tcs is None and isinstance(delta, dict):
        tcs = delta.get("tool_calls")
    if not tcs:
        return
    for tc in tcs:
        idx = getattr(tc, "index", None)
        if idx is None and isinstance(tc, dict):
            idx = tc.get("index")
        if idx is None:
            idx = 0
        entry = acc.setdefault(idx, {"id": None, "name": None, "args": ""})
        tc_id = getattr(tc, "id", None) or (tc.get("id") if isinstance(tc, dict) else None)
        if tc_id:
            entry["id"] = tc_id
        fn = getattr(tc, "function", None)
        if fn is None and isinstance(tc, dict):
            fn = tc.get("function")
        if fn is not None:
            name = getattr(fn, "name", None) or (fn.get("name") if isinstance(fn, dict) else None)
            if name:
                entry["name"] = name
            args = getattr(fn, "arguments", None)
            if args is None and isinstance(fn, dict):
                args = fn.get("arguments")
            if args:
                entry["args"] += args


def _normalize_tool_calls(tool_calls: list[dict], *, require_unique_provider_ids: bool = False) -> list[dict]:
    """Keep provider IDs for continuation while bounding durable internal IDs."""
    normalized: list[dict] = []
    seen_provider_ids: set[str] = set()
    seen_internal_ids: set[str] = set()
    for index, raw_tool_call in enumerate(tool_calls):
        tool_call = dict(raw_tool_call)
        name = tool_call.get("name")
        provider_call_id = tool_call.get("provider_call_id")
        if not isinstance(provider_call_id, str) or not provider_call_id:
            raw_call_id = tool_call.get("id")
            provider_call_id = (
                raw_call_id
                if isinstance(raw_call_id, str) and raw_call_id
                else f"generated:{index}:{name if isinstance(name, str) and name else 'tool'}"
            )
        if require_unique_provider_ids and provider_call_id in seen_provider_ids:
            raise RuntimeError("v2 tool call IDs must be unique")
        seen_provider_ids.add(provider_call_id)
        internal_call_id = provider_call_id
        if len(internal_call_id) > _MAX_INTERNAL_TOOL_CALL_ID or internal_call_id in seen_internal_ids:
            internal_call_id = f"tool_{hashlib.sha256(f'{provider_call_id}:{index}'.encode()).hexdigest()}"
        if internal_call_id in seen_internal_ids:
            raise RuntimeError("tool call IDs must be unique")
        seen_internal_ids.add(internal_call_id)
        tool_call["id"] = internal_call_id
        tool_call["provider_call_id"] = provider_call_id
        normalized.append(tool_call)
    return normalized


def _provider_tool_call_id(tool_call: dict) -> str:
    call_id = tool_call.get("provider_call_id") or tool_call.get("id")
    if not isinstance(call_id, str) or not call_id:
        raise RuntimeError("provider tool call ID is invalid")
    return call_id


def _internal_tool_call_id(tool_call: dict) -> str:
    call_id = tool_call.get("id")
    if not isinstance(call_id, str) or not call_id or len(call_id) > _MAX_INTERNAL_TOOL_CALL_ID:
        raise RuntimeError("internal tool call ID is invalid")
    return call_id


_CITATION_SNIPPET_CAP = 300  # 스니펫 저장/전송 상한(Perplexity 는 표 덤프 등 매우 길 수 있음)


def _get(obj, key):
    val = getattr(obj, key, None)
    if val is None and isinstance(obj, dict):
        val = obj.get(key)
    return val


def _is_http_url(url) -> bool:
    """http/https 만 출처로 허용(javascript:·data: 등 방어). 프론트가 다시 검증하지만 이중 방어."""
    return isinstance(url, str) and (url.startswith("http://") or url.startswith("https://"))


def _extract_citations(chunk, delta, acc: dict[str, dict]) -> None:
    """Normalize provider citations into stable web or document source records."""

    # search_results — rich web sources take precedence over bare URL citations.
    for item in _get(chunk, "search_results") or []:
        url = _get(item, "url")
        if not _is_http_url(url):
            continue
        snippet = _get(item, "snippet")
        acc[url] = {
            "source_kind": "web",
            "url": url,
            "title": _get(item, "title"),
            "snippet": (snippet[:_CITATION_SNIPPET_CAP] if isinstance(snippet, str) else None),
        }

    # Bare URL citations, emitted by several OpenAI-compatible providers.
    for citation in _get(chunk, "citations") or []:
        if _is_http_url(citation):
            acc.setdefault(citation, {"source_kind": "web", "url": citation, "title": None, "snippet": None})

    # LiteLLM's Anthropic-unified protocol attaches document citations to a
    # streaming delta's provider_specific_fields["citation"]. A non-stream
    # content-block shape is also accepted for replayed/provider test fixtures.
    def add_document_citation(citation: Any) -> None:
        index = _get(citation, "document_index")
        if not isinstance(index, int) or index < 0:
            return
        start = _get(citation, "start_char_index")
        end = _get(citation, "end_char_index")
        if not isinstance(start, int) or start < 0:
            start = None
        if not isinstance(end, int) or end < 0:
            end = None
        if (start is None) != (end is None) or (start is not None and end is not None and start > end):
            start = end = None
        snippet = _get(citation, "cited_text")
        key = f"document:{index}:{start if start is not None else ''}:{end if end is not None else ''}:{snippet or ''}"
        acc.setdefault(
            key,
            {
                "source_kind": "document",
                "document_index": index,
                "title": _get(citation, "document_title"),
                "snippet": snippet[:_CITATION_SNIPPET_CAP] if isinstance(snippet, str) else None,
                **({"start_index": start, "end_index": end} if start is not None else {}),
            },
        )

    provider_citation = _get(_get(delta, "provider_specific_fields") or {}, "citation")
    if provider_citation is not None:
        add_document_citation(provider_citation)
    for content_block in _get(chunk, "content") or []:
        for citation in _get(content_block, "citations") or []:
            add_document_citation(citation)

    # annotations(url_citation) — Gemini/OpenAI
    for annotation in _get(delta, "annotations") or []:
        url_citation = _get(annotation, "url_citation") or {}
        url = _get(url_citation, "url")
        if _is_http_url(url):
            acc.setdefault(
                url,
                {"source_kind": "web", "url": url, "title": _get(url_citation, "title"), "snippet": None},
            )


def _managed_tool_citations(tool_name: str, result: str) -> list[dict]:
    """Lift canonical managed search/fetch source metadata out of untrusted tool text."""
    if tool_name not in {"managed_web_search", "managed_web_fetch"}:
        return []
    try:
        payload = json.loads(result)
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(payload, dict):
        return []
    entries = payload.get("sources") if tool_name == "managed_web_search" else [payload]
    citations: list[dict] = []
    if not isinstance(entries, list):
        return citations
    for entry in entries:
        if not isinstance(entry, dict) or not _is_http_url(entry.get("url")):
            continue
        title = entry.get("title")
        snippet = entry.get("snippet")
        if tool_name == "managed_web_fetch":
            snippet = entry.get("text")
        citations.append(
            {
                "source_kind": "web",
                "url": entry["url"],
                "title": title if isinstance(title, str) else None,
                "snippet": snippet[:_CITATION_SNIPPET_CAP] if isinstance(snippet, str) else None,
            }
        )
    return citations


def _usage_pt_ct(usage) -> tuple[int, int] | None:
    if usage is None:
        return None
    pt = getattr(usage, "prompt_tokens", None)
    if pt is None and isinstance(usage, dict):
        pt = usage.get("prompt_tokens")
    ct = getattr(usage, "completion_tokens", None)
    if ct is None and isinstance(usage, dict):
        ct = usage.get("completion_tokens")
    if pt is None or ct is None:
        return None
    try:
        return int(pt), int(ct)
    except (TypeError, ValueError):
        return None


def _assistant_tool_calls_msg(content: str, tool_calls: list[dict]) -> dict:
    return {
        "role": "assistant",
        "content": content or None,
        "tool_calls": [
            {
                "id": _provider_tool_call_id(tool_call),
                "type": "function",
                "function": {"name": tool_call["name"], "arguments": tool_call.get("args") or "{}"},
            }
            for tool_call in tool_calls
        ],
    }


def _v2_tool_schemas(bindings: dict[str, object]) -> list[dict]:
    schemas: list[dict] = []
    for binding in bindings.values():
        definition = getattr(binding, "definition", None)
        if definition is None:
            continue
        schemas.append(
            {
                "type": "function",
                "function": {
                    "name": definition.name,
                    "description": definition.description,
                    "parameters": definition.input_schema,
                },
            }
        )
    return schemas


def _v2_tool_definition_hash(definition: object) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "name": definition.name,
                "description": definition.description,
                "input_schema": definition.input_schema,
                "effect": definition.effect,
                "parallel_safe": definition.parallel_safe,
                "source": definition.source,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _requires_v2_approval(effect: str, approval_mode: object, workspace_write_mode: object = "ask") -> bool:
    if approval_mode == "always":
        return True
    if effect == "read":
        return False
    return not (effect == "workspace_write" and workspace_write_mode == "auto_edit") and approval_mode != "never"


def _v2_effect_for_call(binding: object, raw_arguments: object) -> str:
    """Resolve dynamic effects before the approval boundary; malformed calls fail closed."""
    definition = getattr(binding, "definition", None)
    static_effect = getattr(definition, "effect", None)
    if static_effect not in {"read", "workspace_write", "process", "external_mutation"}:
        return "external_mutation"
    classifier = getattr(binding, "classify_effect", None)
    if classifier is None:
        return static_effect
    try:
        arguments = json.loads(raw_arguments or "{}") if isinstance(raw_arguments, str) else raw_arguments
        if not isinstance(arguments, dict):
            return "external_mutation"
        effective_effect = classifier(arguments)
    except Exception:
        logger.warning("v2 tool effect classification failed", exc_info=True)
        return "external_mutation"
    return (
        effective_effect
        if effective_effect in {"read", "workspace_write", "process", "external_mutation"}
        else "external_mutation"
    )


def _v2_tool_call_id(tool_call: dict) -> str:
    try:
        return _internal_tool_call_id(tool_call)
    except RuntimeError as exc:
        raise RuntimeError("v2 tool call ID is invalid") from exc


def _build_graph(params: dict, ctx: ToolContext):
    """Execute one provider call or tool batch per resumable LangGraph node."""
    hooks = params.get("execution_hooks")

    async def boundary(name: str, **payload: Any) -> Any | None:
        callback = getattr(hooks, name, None) if hooks is not None else None
        if callback is not None:
            return await callback(**payload)
        return None

    async def tool_activity_metadata(tool_name: str) -> tuple[str, str]:
        v2_bindings = params.get("v2_bindings")
        if isinstance(v2_bindings, dict):
            binding = v2_bindings.get(tool_name)
            if binding is not None:
                return binding.definition.source, binding.definition.activity_category
        return await selection.context_tool_activity_metadata(tool_name, ctx)

    async def call_model(state: ChatState) -> dict:
        writer = get_stream_writer()
        model_turn_count = int(state.get("model_turn_count", 0))
        if params.get("execution_protocol_version") == 2:
            max_model_turns = int(params.get("max_model_turns", 8))
            if not 1 <= max_model_turns <= 16 or model_turn_count < 0:
                raise RuntimeError("v2 model-turn limit is invalid")
            if model_turn_count >= max_model_turns:
                writer(
                    {
                        "type": "warning",
                        "code": "model_turn_limit_exceeded",
                        "safe_message": "Run model-turn limit reached.",
                    }
                )
                return {
                    "pending_tool_calls": [],
                    "final_text": state.get("final_text") or "Run model-turn limit reached.",
                    "model_failed": False,
                }

        messages = list(state["messages"])
        v2_bindings = params.get("v2_bindings")
        if isinstance(ctx.binding_session, contracts.ToolBindingSession) and not ctx.binding_session.loaded_names:
            restored = await boundary("loaded_tool_names")
            if isinstance(restored, list) and all(isinstance(name, str) for name in restored):
                if isinstance(v2_bindings, dict):
                    await bindings.restore_v2_deferred_bindings(
                        ctx,
                        v2_bindings,
                        restored,
                        include_managed=True,
                    )
                else:
                    await bindings.restore_v2_deferred_bindings(
                        ctx,
                        ctx.binding_session.legacy_bindings,
                        restored,
                        include_managed=False,
                    )
        schemas = (
            _v2_tool_schemas(v2_bindings) if isinstance(v2_bindings, dict) else await selection.context_tool_schemas(ctx)
        )

        reasoning_effort = None if params["model"] in _REASONING_UNSUPPORTED else params.get("reasoning_effort")
        disable_reasoning_for_tools = bool(schemas) and (
            params["model"] in _TOOL_REASONING_EXPLICIT_NONE
            or _requires_explicit_none_for_tools(params["model"], params.get("custom_llm_provider"), reasoning_effort)
        )
        attempt = 0
        round_index = int(state.get("loop_count", 0))

        async def open_stream(effort, *, disable_reasoning: bool = False):
            nonlocal attempt
            attempt += 1
            replay_payload = await boundary("provider_started", round_index=round_index, attempt=attempt)
            if _abort_code(replay_payload) is not None:
                raise _BoundaryAbort(_abort_code(replay_payload))
            if isinstance(replay_payload, dict):
                return None, replay_payload
            try:
                provider_extra = {"reasoning_effort": "none"} if disable_reasoning else {}
                if params.get("response_format") is not None:
                    provider_extra["response_format"] = params["response_format"]
                return (
                    await litellm_client.acompletion_stream(
                        model=params["model"],
                        messages=messages,
                        tools=schemas,
                        custom_llm_provider=params.get("custom_llm_provider"),
                        api_base=params.get("api_base"),
                        api_key=params.get("api_key"),
                        max_tokens=params.get("max_tokens"),
                        temperature=params.get("temperature"),
                        reasoning_effort=effort,
                        extra=provider_extra or None,
                    ),
                    None,
                )
            except BaseException:
                logger.warning("litellm 스트림 초기화 오류 model=%s", params.get("model"), exc_info=True)
                failure = await boundary("provider_failed", round_index=round_index, attempt=attempt)
                if _abort_code(failure) is not None:
                    raise _BoundaryAbort(_abort_code(failure))
                raise

        try:
            response, replay_payload = await open_stream(
                None if disable_reasoning_for_tools else reasoning_effort,
                disable_reasoning=disable_reasoning_for_tools,
            )
        except _BoundaryAbort as exc:
            writer(
                {
                    "type": "error",
                    "code": str(exc),
                    "message": "이전 모델 호출 결과를 안전하게 확인할 수 없습니다",
                }
            )
            return {"model_failed": True, "pending_tool_calls": []}
        except Exception as exc:
            if schemas and _is_tool_reasoning_conflict(exc):
                logger.warning("tool 요청의 기본 reasoning을 명시적으로 비활성화해 재시도 model=%s", params["model"])
                try:
                    response, replay_payload = await open_stream(None, disable_reasoning=True)
                except Exception:
                    logger.warning("litellm 스트리밍 오류 model=%s", params.get("model"), exc_info=True)
                    writer({"type": "error", "message": "모델 응답 중 오류가 발생했습니다"})
                    return {"model_failed": True, "pending_tool_calls": []}
                _TOOL_REASONING_EXPLICIT_NONE.add(params["model"])
            elif reasoning_effort:
                logger.warning(
                    "reasoning 포함 요청 실패 — reasoning 없이 재시도 model=%s", params.get("model"), exc_info=True
                )
                try:
                    response, replay_payload = await open_stream(None)
                except Exception:
                    logger.warning("litellm 스트리밍 오류 model=%s", params.get("model"), exc_info=True)
                    writer({"type": "error", "message": "모델 응답 중 오류가 발생했습니다"})
                    return {"model_failed": True, "pending_tool_calls": []}
                _REASONING_UNSUPPORTED.add(params["model"])
            else:
                logger.warning("litellm 스트리밍 오류 model=%s", params.get("model"), exc_info=True)
                writer({"type": "error", "message": "모델 응답 중 오류가 발생했습니다"})
                return {"model_failed": True, "pending_tool_calls": []}

        citations_by_url = {item["url"]: item for item in state.get("citations", []) if item.get("url")}
        if replay_payload is not None:
            response_text = str(replay_payload.get("text", ""))
            replay_calls = replay_payload.get("tool_calls", [])
            tool_calls = _normalize_tool_calls(
                [item for item in replay_calls if isinstance(item, dict) and item.get("name")],
                require_unique_provider_ids=isinstance(v2_bindings, dict),
            )
            for citation in replay_payload.get("citations", []):
                if isinstance(citation, dict) and citation.get("url"):
                    citations_by_url[citation["url"]] = citation
            replay_reasoning = str(replay_payload.get("reasoning", ""))
            if replay_reasoning:
                reasoning_event: dict[str, Any] = {"type": "reasoning", "text": replay_reasoning}
                if replay_payload.get("_durable_replay") is True:
                    reasoning_event["_durable_replay"] = True
                writer(reasoning_event)
            if response_text:
                token_event: dict[str, Any] = {"type": "token", "text": response_text}
                if replay_payload.get("_durable_replay") is True:
                    token_event["_durable_replay"] = True
                writer(token_event)
            replay_usage = replay_payload.get("usage", {})
            round_usage = (
                max(0, int(replay_usage.get("prompt_tokens", 0))),
                max(0, int(replay_usage.get("completion_tokens", 0))),
            )
        else:
            text_parts: list[str] = []
            reasoning_parts: list[str] = []
            tool_acc: dict[int, dict] = {}
            final_usage = None
            try:
                async for chunk in response:
                    delta = _delta(chunk)
                    if delta is not None:
                        reasoning = _delta_reasoning(delta)
                        if reasoning:
                            reasoning_parts.append(reasoning)
                            writer({"type": "reasoning", "text": reasoning})
                        content = _delta_content(delta)
                        if content:
                            text_parts.append(content)
                            writer({"type": "token", "text": content})
                        _accumulate_tool_calls(delta, tool_acc)
                    _extract_citations(chunk, delta, citations_by_url)
                    usage = _get(chunk, "usage")
                    if usage is not None:
                        final_usage = usage
            except Exception:
                logger.warning("litellm 스트림 소비 오류 model=%s", params.get("model"), exc_info=True)
                failure = await boundary("provider_failed", round_index=round_index, attempt=attempt)
                if _abort_code(failure) is not None:
                    writer(
                        {
                            "type": "error",
                            "code": _abort_code(failure),
                            "message": "이전 모델 호출 결과를 안전하게 확인할 수 없습니다",
                        }
                    )
                    return {"model_failed": True, "pending_tool_calls": []}
                logger.warning("litellm 스트리밍 오류 model=%s", params.get("model"), exc_info=True)
                writer({"type": "error", "message": "모델 응답 중 오류가 발생했습니다"})
                return {"model_failed": True, "pending_tool_calls": []}
            tool_calls = _normalize_tool_calls(
                [tool_call for tool_call in tool_acc.values() if tool_call.get("name")],
                require_unique_provider_ids=isinstance(v2_bindings, dict),
            )
            round_usage = _usage_pt_ct(final_usage)
            if round_usage is None:
                tool_payload = json.dumps(tool_calls, ensure_ascii=False, sort_keys=True) if tool_calls else ""
                round_usage = litellm_client.extract_usage(
                    params["model"], messages, "".join(text_parts) + tool_payload, None
                )
            response_text = "".join(text_parts)
            await boundary(
                "provider_completed",
                round_index=round_index,
                attempt=attempt,
                usage={"prompt_tokens": round_usage[0], "completion_tokens": round_usage[1]},
                result_payload={
                    "text": response_text,
                    "reasoning": "".join(reasoning_parts),
                    "tool_calls": tool_calls,
                    "citations": list(citations_by_url.values()),
                    "usage": {"prompt_tokens": round_usage[0], "completion_tokens": round_usage[1]},
                },
            )

        previous_usage = state.get("usage", {})
        total_usage = {
            "prompt_tokens": int(previous_usage.get("prompt_tokens", 0)) + round_usage[0],
            "completion_tokens": int(previous_usage.get("completion_tokens", 0)) + round_usage[1],
        }
        next_messages = messages
        if tool_calls:
            next_messages = messages + [_assistant_tool_calls_msg(response_text, tool_calls)]
        return {
            "messages": next_messages,
            "pending_tool_calls": tool_calls,
            "final_text": response_text,
            "citations": list(citations_by_url.values()),
            "usage": total_usage,
            "model_turn_count": model_turn_count + 1,
            "model_failed": False,
        }

    async def classify_tool_calls(state: ChatState) -> dict:
        bindings = params.get("v2_bindings")
        if not isinstance(bindings, dict):
            return {"classified_tool_calls": []}
        classified: list[dict] = []
        max_tool_calls = int(params.get("max_tool_calls", 24))
        if max_tool_calls < 1:
            raise RuntimeError("v2 tool-call limit is invalid")
        emitted_count = int(state.get("tool_call_count", 0))
        previous_call_ids = state.get("tool_call_ids", [])
        if (
            not isinstance(previous_call_ids, list)
            or not all(isinstance(call_id, str) and call_id and len(call_id) <= 190 for call_id in previous_call_ids)
            or len(set(previous_call_ids)) != len(previous_call_ids)
        ):
            raise RuntimeError("v2 tool call IDs are invalid")
        seen_call_ids = set(previous_call_ids)
        for index, tool_call in enumerate(state.get("pending_tool_calls", [])):
            call_id = _v2_tool_call_id(tool_call)
            if call_id in seen_call_ids:
                raise RuntimeError("v2 tool call IDs must be unique")
            seen_call_ids.add(call_id)
            name = tool_call.get("name")
            binding = bindings.get(name) if isinstance(name, str) else None
            definition = getattr(binding, "definition", None)
            if definition is None:
                raise RuntimeError("model requested an unavailable v2 tool")
            effective_effect = _v2_effect_for_call(binding, tool_call.get("args"))
            allowed_direct_effects = params.get("allowed_direct_effects")
            effect_allowed = (
                not isinstance(allowed_direct_effects, frozenset) or effective_effect in allowed_direct_effects
            )
            classified.append(
                {
                    "tool_call": tool_call,
                    "policy_limited": emitted_count + index >= max_tool_calls,
                    "effect_denied": emitted_count + index < max_tool_calls and not effect_allowed,
                    "requires_approval": effect_allowed
                    and _requires_v2_approval(
                        effective_effect,
                        params.get("approval_mode"),
                        params.get("workspace_write_mode"),
                    ),
                    "source": definition.source,
                    "effect": effective_effect,
                    "tool_definition_hash": _v2_tool_definition_hash(definition),
                    "config_fingerprint": getattr(binding, "config_fingerprint", None),
                    "destination_origin": getattr(binding, "destination_origin", None),
                }
            )
        return {
            "classified_tool_calls": classified,
            "tool_call_count": emitted_count + len(classified),
            "tool_call_ids": [*previous_call_ids, *(item["tool_call"]["id"] for item in classified)],
        }

    async def preview_tool_calls(state: ChatState) -> dict:
        bindings = params.get("v2_bindings")
        if not isinstance(bindings, dict):
            return {}
        classified: list[dict] = []
        for item in state.get("classified_tool_calls", []):
            updated = dict(item)
            tool_call = updated.get("tool_call")
            binding = bindings.get(tool_call.get("name")) if isinstance(tool_call, dict) else None
            preview = getattr(binding, "preview", None)
            if updated.get("requires_approval") is True and callable(preview) and isinstance(tool_call, dict):
                try:
                    arguments = json.loads(tool_call.get("args") or "{}")
                    if not isinstance(arguments, dict):
                        raise ValueError("tool arguments must be an object")
                    detail = await preview(arguments, ctx)
                    if not isinstance(detail, dict):
                        raise ValueError("tool preview is invalid")
                    updated["approval_preview"] = detail.get("preview", [])
                    updated["expected_state_revision"] = detail.get("expected_state_revision")
                    updated["writer_fence"] = detail.get("writer_fence")
                except Exception:
                    updated["approval_preview"] = []
                    updated["preview_unavailable"] = True
            classified.append(updated)
        return {"classified_tool_calls": classified}

    async def await_input(state: ChatState) -> dict:
        bindings = params.get("v2_bindings")
        if not isinstance(bindings, dict):
            return {}
        classified = state.get("classified_tool_calls", [])
        limited = [item["tool_call"] for item in classified if item.get("policy_limited") is True]
        gated = [
            item
            for item in classified
            if item.get("policy_limited") is not True and item.get("requires_approval") is True
        ]
        if not gated:
            return {
                "pending_tool_calls": [
                    item["tool_call"]
                    for item in classified
                    if item.get("policy_limited") is not True and item.get("effect_denied") is not True
                ],
                "denied_tool_calls": [],
                "effect_denied_tool_calls": [
                    item["tool_call"] for item in classified if item.get("effect_denied") is True
                ],
                "policy_limited_tool_calls": limited,
                "classified_tool_calls": [],
            }

        def approval_call(item: dict) -> dict:
            tool_call = item["tool_call"]
            payload = {
                "call_id": _v2_tool_call_id(tool_call),
                "name": tool_call["name"],
                "arguments": tool_call.get("args") or "{}",
                "source": item["source"],
                "effect": item["effect"],
                "tool_definition_hash": item["tool_definition_hash"],
                "config_fingerprint": item["config_fingerprint"],
                "destination_origin": item["destination_origin"],
            }
            if "approval_preview" in item:
                payload.update(
                    preview=item["approval_preview"],
                    expected_state_revision=item.get("expected_state_revision"),
                    writer_fence=item.get("writer_fence"),
                )
            return payload

        decisions = interrupt(
            {
                "kind": "tool_approval",
                "calls": [approval_call(item) for item in gated],
            }
        )
        if not isinstance(decisions, list):
            raise RuntimeError("v2 approval resume payload is invalid")
        for item in gated:
            tool_call = item["tool_call"]
            binding = bindings.get(tool_call.get("name"))
            definition = getattr(binding, "definition", None)
            if (
                definition is None
                or _v2_tool_definition_hash(definition) != item["tool_definition_hash"]
                or getattr(binding, "config_fingerprint", None) != item["config_fingerprint"]
                or getattr(binding, "destination_origin", None) != item["destination_origin"]
                or _v2_effect_for_call(binding, tool_call.get("args")) != item["effect"]
            ):
                raise RuntimeError("v2 approval dispatch identity changed")
        decision_by_call = {
            item.get("call_id"): item.get("decision")
            for item in decisions
            if isinstance(item, dict) and isinstance(item.get("call_id"), str)
        }
        approved: list[dict] = []
        denied: list[dict] = []
        for item in classified:
            tool_call = item["tool_call"]
            if item.get("policy_limited") is True or item.get("effect_denied") is True:
                continue
            if item.get("requires_approval") is not True:
                continue
            call_id = _v2_tool_call_id(tool_call)
            if decision_by_call.get(call_id) == "approve":
                approved.append(tool_call)
            else:
                denied.append(tool_call)
        return {
            "pending_tool_calls": approved,
            "denied_tool_calls": denied,
            "effect_denied_tool_calls": [item["tool_call"] for item in classified if item.get("effect_denied") is True],
            "policy_limited_tool_calls": limited,
        }

    async def route_tools(_state: ChatState) -> dict:
        return {}

    def next_step(state: ChatState) -> str:
        if state.get("model_failed"):
            return "finalize"
        if (
            state.get("pending_tool_calls")
            or state.get("denied_tool_calls")
            or state.get("effect_denied_tool_calls")
            or state.get("policy_limited_tool_calls")
        ):
            return "execute_tools"
        return "finalize"

    async def execute_tools(state: ChatState) -> dict:
        writer = get_stream_writer()
        messages = list(state["messages"])
        approved_tool_calls = state.get("pending_tool_calls", [])
        denied_tool_calls = state.get("denied_tool_calls", [])
        effect_denied_tool_calls = state.get("effect_denied_tool_calls", [])
        policy_limited_tool_calls = state.get("policy_limited_tool_calls", [])
        tool_calls = [*denied_tool_calls, *effect_denied_tool_calls, *policy_limited_tool_calls, *approved_tool_calls]
        v2_bindings = params.get("v2_bindings")

        def call_id_for(tool_call: dict) -> str:
            return _internal_tool_call_id(tool_call)

        round_index = int(state.get("loop_count", 0))
        tool_messages: list[dict[str, str]] = []
        citations_by_url = {item["url"]: item for item in state.get("citations", []) if item.get("url")}
        journals_tool_events = bool(getattr(hooks, "journals_tool_events", False))
        tool_usage = list(state.get("tool_usage", []))
        writer(
            {
                "type": "assistant_tool_calls",
                "content": state.get("final_text") or None,
                "tool_calls": [
                    {
                        "id": call_id_for(tool_call),
                        "name": tool_call["name"],
                        "args": tool_call.get("args") or "{}",
                    }
                    for tool_call in tool_calls
                ],
            }
        )
        for tool_call in denied_tool_calls:
            tool_call_id = call_id_for(tool_call)
            result = "Tool call was denied by the user."
            writer(
                {
                    "type": "tool_result",
                    "tool_call_id": tool_call_id,
                    "name": tool_call["name"],
                    "content": result,
                    "hidden": False,
                    "status": "failed",
                    "error_code": "tool_denied",
                }
            )
            tool_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": _provider_tool_call_id(tool_call),
                    "name": tool_call["name"],
                    "content": result,
                }
            )
        for tool_call in effect_denied_tool_calls:
            tool_call_id = call_id_for(tool_call)
            result = "Tool call is not permitted by the selected agent policy."
            writer(
                {
                    "type": "tool_result",
                    "tool_call_id": tool_call_id,
                    "name": tool_call["name"],
                    "content": result,
                    "hidden": False,
                    "status": "failed",
                    "error_code": "policy_effect_denied",
                }
            )
            tool_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": _provider_tool_call_id(tool_call),
                    "name": tool_call["name"],
                    "content": result,
                }
            )
        for tool_call in policy_limited_tool_calls:
            tool_call_id = call_id_for(tool_call)
            result = "Tool call exceeded the run policy limit."
            writer(
                {
                    "type": "tool_result",
                    "tool_call_id": tool_call_id,
                    "name": tool_call["name"],
                    "content": result,
                    "hidden": False,
                    "error_code": "policy_limit_exceeded",
                    "status": "failed",
                }
            )
            tool_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": _provider_tool_call_id(tool_call),
                    "name": tool_call["name"],
                    "content": result,
                }
            )

        async def prepare_tool(tool_index: int, tool_call: dict) -> dict[str, Any]:
            tool_call_id = call_id_for(tool_call)
            try:
                arguments = json.loads(tool_call.get("args") or "{}")
            except (json.JSONDecodeError, TypeError):
                arguments = {}
            source, category = await tool_activity_metadata(tool_call["name"])
            replay_payload = await boundary(
                "tool_started",
                round_index=round_index,
                tool_index=tool_index,
                tool_call_id=tool_call_id,
                tool_name=tool_call["name"],
                arguments=arguments,
                source=source,
                category=category,
            )
            if _abort_code(replay_payload) is not None:
                raise _BoundaryAbort(_abort_code(replay_payload))
            tool_event = {
                "type": "tool_call",
                "tool_call_id": tool_call_id,
                "name": tool_call["name"],
                "args": tool_call.get("args") or "{}",
            }
            if journals_tool_events:
                tool_event["_durable_journaled"] = True
            writer(tool_event)
            return {
                "index": tool_index,
                "tool_call": tool_call,
                "tool_call_id": tool_call_id,
                "arguments": arguments,
                "source": source,
                "category": category,
                "replay_payload": replay_payload,
            }

        async def dispatch_prepared(prepared: dict[str, Any]) -> dict[str, Any]:
            replay_payload = prepared["replay_payload"]
            if isinstance(replay_payload, dict):
                replay_content = replay_payload.get("content")
                if not isinstance(replay_content, str):
                    return {"exception": RuntimeError("completed tool segment has invalid replay payload")}
                replay_usage = replay_payload.get("usage")
                return {
                    "result": replay_content,
                    "visible": replay_payload.get("visible") is not False,
                    "execution_usage": replay_usage if isinstance(replay_usage, list) else [],
                    "display": replay_payload.get("display") if isinstance(replay_payload.get("display"), list) else [],
                    "artifacts": replay_payload.get("artifacts")
                    if isinstance(replay_payload.get("artifacts"), list)
                    else [],
                    "warning_code": None,
                    "result_status": replay_payload.get("status")
                    if isinstance(replay_payload.get("status"), str)
                    else "completed",
                    "error_code": replay_payload.get("error_code")
                    if isinstance(replay_payload.get("error_code"), str)
                    else None,
                    "replayed": True,
                }
            tool_call = prepared["tool_call"]
            try:
                if isinstance(v2_bindings, dict):
                    binding = v2_bindings.get(tool_call["name"])
                    if binding is None:
                        raise RuntimeError("v2 tool binding is unavailable")
                    execution_result = await agent_runtime_v2.dispatch_tool_call(
                        binding,
                        prepared["arguments"],
                        replace(
                            ctx,
                            advisor_visible_messages=tuple(messages),
                            tool_call_id=prepared["tool_call_id"],
                        ),
                    )
                    components = execution_result.usage_components.get("components", [])
                    return {
                        "result": execution_result.model_content,
                        "visible": bool(execution_result.display or execution_result.artifacts),
                        "execution_usage": components if isinstance(components, list) else [],
                        "display": [part.model_dump(mode="json") for part in execution_result.display],
                        "artifacts": [artifact.model_dump(mode="json") for artifact in execution_result.artifacts],
                        "warning_code": execution_result.error_code,
                        "result_status": execution_result.status,
                        "error_code": execution_result.error_code,
                        "replayed": False,
                    }
                execution_result = await dispatch.context_execute_result(
                    tool_call["name"],
                    prepared["arguments"],
                    replace(
                        ctx,
                        advisor_visible_messages=tuple(messages),
                        tool_call_id=prepared["tool_call_id"],
                    ),
                )
                return {
                    "result": execution_result.content,
                    "visible": execution_result.visible,
                    "execution_usage": list(execution_result.usage),
                    "warning_code": execution_result.warning_code,
                    "display": [],
                    "artifacts": [],
                    "result_status": "completed",
                    "error_code": execution_result.warning_code,
                    "replayed": False,
                }
            except Exception as exc:
                return {"exception": exc}

        async def finalize_prepared(prepared: dict[str, Any], outcome: dict[str, Any]) -> None:
            tool_call = prepared["tool_call"]
            exception = outcome.get("exception")
            if isinstance(exception, Exception):
                await boundary(
                    "tool_failed",
                    round_index=round_index,
                    tool_index=prepared["index"],
                    tool_call_id=prepared["tool_call_id"],
                    tool_name=tool_call["name"],
                )
                raise exception
            result = outcome["result"]
            visible = outcome["visible"]
            execution_usage = outcome["execution_usage"]
            warning_code = outcome["warning_code"]
            result_status = outcome["result_status"]
            error_code = outcome["error_code"]
            display = outcome["display"]
            artifacts = outcome["artifacts"]
            if outcome["replayed"] is not True:
                await boundary(
                    "tool_completed",
                    round_index=round_index,
                    tool_index=prepared["index"],
                    tool_call_id=prepared["tool_call_id"],
                    tool_name=tool_call["name"],
                    source=prepared["source"],
                    category=prepared["category"],
                    result_payload={
                        "content": result,
                        "tool_name": tool_call["name"],
                        "usage": execution_usage,
                        "visible": visible,
                        "display": display,
                        "artifacts": artifacts,
                        "warning_code": warning_code,
                        "status": result_status,
                        "error_code": error_code,
                    },
                )
            if warning_code:
                writer({"type": "warning", "code": warning_code, "safe_message": "Advisor request failed."})
            tool_result_event = {
                "type": "tool_result",
                "tool_call_id": prepared["tool_call_id"],
                "name": tool_call["name"],
                "content": result if visible else "",
                "hidden": not visible,
                "status": result_status,
                "display": display,
                "artifacts": artifacts,
                "error_code": error_code,
            }
            if journals_tool_events:
                tool_result_event["_durable_journaled"] = True
            writer(tool_result_event)
            tool_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": _provider_tool_call_id(tool_call),
                    "name": tool_call["name"],
                    "content": result,
                }
            )
            tool_usage.extend(item for item in execution_usage if isinstance(item, dict))
            if visible:
                for citation in _managed_tool_citations(tool_call["name"], result):
                    citations_by_url[citation["url"]] = citation

        async def flush_prepared(prepared_calls: list[dict[str, Any]]) -> None:
            outcomes = await asyncio.gather(*(dispatch_prepared(prepared) for prepared in prepared_calls))
            failure: Exception | None = None
            for prepared, outcome in zip(prepared_calls, outcomes, strict=True):
                try:
                    await finalize_prepared(prepared, outcome)
                except Exception as exc:
                    if failure is None:
                        failure = exc
            if failure is not None:
                raise failure

        def is_parallel_safe_read(prepared: dict[str, Any]) -> bool:
            if not isinstance(v2_bindings, dict) or isinstance(prepared["replay_payload"], dict):
                return False
            binding = v2_bindings.get(prepared["tool_call"]["name"])
            definition = getattr(binding, "definition", None)
            return bool(
                getattr(definition, "parallel_safe", False)
                and _v2_effect_for_call(binding, prepared["tool_call"].get("args")) == "read"
            )

        parallel_batch: list[dict[str, Any]] = []
        for tool_index, tool_call in enumerate(approved_tool_calls):
            prepared = await prepare_tool(tool_index, tool_call)
            if is_parallel_safe_read(prepared):
                parallel_batch.append(prepared)
                if len(parallel_batch) == 4:
                    await flush_prepared(parallel_batch)
                    parallel_batch = []
                continue
            if parallel_batch:
                await flush_prepared(parallel_batch)
                parallel_batch = []
            await flush_prepared([prepared])
        if parallel_batch:
            await flush_prepared(parallel_batch)
        return {
            "messages": messages + tool_messages,
            "pending_tool_calls": [],
            "denied_tool_calls": [],
            "effect_denied_tool_calls": [],
            "policy_limited_tool_calls": [],
            "loop_count": state.get("loop_count", 0) + 1,
            "citations": list(citations_by_url.values()),
            "tool_usage": tool_usage,
        }

    async def finalize(state: ChatState) -> dict:
        if state.get("model_failed"):
            return {"messages": state["messages"]}
        writer = get_stream_writer()
        if state.get("citations"):
            writer({"type": "citations", "items": state["citations"]})
        usage_event = {"type": "usage", "usage": state.get("usage", {"prompt_tokens": 0, "completion_tokens": 0})}
        if state.get("tool_usage"):
            usage_event["tool_usage"] = state["tool_usage"]
        writer(usage_event)
        return {"messages": state["messages"] + [{"role": "assistant", "content": state.get("final_text", "")}]}

    builder = StateGraph(ChatState)
    builder.add_node("call_model", call_model)
    builder.add_node("classify_tool_calls", classify_tool_calls)
    builder.add_node("preview_tool_calls", preview_tool_calls)
    builder.add_node("await_input", await_input)
    builder.add_node("route_tools", route_tools)
    builder.add_node("execute_tools", execute_tools)
    builder.add_node("finalize", finalize)
    builder.add_edge(START, "call_model")
    builder.add_edge("call_model", "classify_tool_calls")
    builder.add_edge("classify_tool_calls", "preview_tool_calls")
    builder.add_edge("preview_tool_calls", "await_input")
    builder.add_edge("await_input", "route_tools")
    builder.add_conditional_edges("route_tools", next_step, {"execute_tools": "execute_tools", "finalize": "finalize"})
    builder.add_edge("execute_tools", "call_model")
    builder.add_edge("finalize", END)
    return builder.compile(checkpointer=chat_checkpointer.saver)


async def stream(
    *,
    model: str,
    messages: list[dict],
    project_id: str,
    user_id: str,
    custom_llm_provider: str | None = None,
    api_base: str | None = None,
    api_key: str | None = None,
    max_tokens: int | None = None,
    temperature: float | None = None,
    reasoning_effort: str | None = None,
    selected_tool_ids: tuple[int, ...] | None = None,
    selected_mcp_ids: tuple[int, ...] | None = None,
    expected_mcp_credential_versions: tuple[tuple[int, int], ...] | None = None,
    expected_extension_fingerprints: tuple[tuple[str, int, str], ...] | None = None,
    tools_enabled: bool = True,
    managed_search: dict[str, Any] | None = None,
    managed_fetch: dict[str, Any] | None = None,
    managed_advisor: dict[str, Any] | None = None,
    response_format: dict[str, Any] | None = None,
    execution_hooks: object | None = None,
    run_id: str | None = None,
    execution_protocol_version: int = 1,
    approval_mode: str | None = None,
    workspace_write_mode: str = "ask",
    max_model_turns: int = 8,
    max_tool_calls: int = 24,
    allowed_direct_effects: tuple[str, ...] | None = None,
    lumen_snapshot: dict[str, object] | None = None,
    lumen_snapshot_frozen: bool = False,
    resume: list[dict[str, str]] | None = None,
) -> AsyncIterator[dict]:
    """Stream chat events while awaiting durable execution-boundary hooks."""
    params = {
        "model": model,
        "custom_llm_provider": custom_llm_provider,
        "api_base": api_base,
        "api_key": api_key,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "reasoning_effort": reasoning_effort,
        "execution_hooks": execution_hooks,
        "response_format": response_format,
        "execution_protocol_version": execution_protocol_version,
        "approval_mode": approval_mode,
        "workspace_write_mode": workspace_write_mode,
        "max_model_turns": max_model_turns,
        "max_tool_calls": max_tool_calls,
        "allowed_direct_effects": frozenset(allowed_direct_effects) if allowed_direct_effects is not None else None,
    }
    ctx = ToolContext(
        project_id=project_id,
        user_id=user_id,
        selected_tool_ids=selected_tool_ids,
        tools_enabled=tools_enabled,
        selected_mcp_ids=selected_mcp_ids,
        expected_mcp_credential_versions=expected_mcp_credential_versions,
        expected_extension_fingerprints=expected_extension_fingerprints,
        run_id=run_id,
        lumen_snapshot=lumen_snapshot,
        lumen_snapshot_frozen=lumen_snapshot_frozen,
        execution_hooks=execution_hooks,
        managed_search=managed_search,
        managed_fetch=managed_fetch,
        managed_advisor=managed_advisor,
        binding_session=contracts.ToolBindingSession(),
    )
    if execution_protocol_version == 2:
        initial_bindings = await bindings.v2_tool_bindings(ctx)
        params["v2_bindings"] = {
            name: binding
            for name, binding in initial_bindings.items()
            if getattr(binding, "load_policy", None) != "on_demand"
        }
        if "list_available_tools" in params["v2_bindings"]:
            params["v2_bindings"]["list_available_tools"] = bindings._catalog_binding(
                params["v2_bindings"],
                include_managed=True,
            )
    graph = _build_graph(params, ctx)
    config = {"configurable": {"thread_id": run_id}} if run_id else None
    graph_input: dict[str, list[dict]] | Command = (
        Command(resume=resume) if resume is not None else {"messages": messages}
    )
    async for mode, chunk in graph.astream(graph_input, config=config, stream_mode=["custom", "updates"]):
        if mode == "custom":
            yield chunk
            continue
        interrupts = chunk.get("__interrupt__") if isinstance(chunk, dict) else None
        if not isinstance(interrupts, tuple):
            continue
        for interrupt_item in interrupts:
            payload = getattr(interrupt_item, "value", None)
            if isinstance(payload, dict) and payload.get("kind") == "tool_approval":
                yield {"type": "input.interrupted", "payload": payload}
