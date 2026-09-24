"""Standalone OpenAI-compatible fake provider for Lumen system tests.

Provides deterministic responses for:
- Standard Chat Completions, Responses, and Anthropic Messages (non-streaming and streaming)
- Small-delta then 2-second pause streaming (proves SSE flush <= 250ms scheduler allowance)
- 500x4 burst streaming (ordered, replayable terminal output)
- First-exchange title generation (detected via system prompt, influenced by first answer)
- Context compaction (strict JSON with summary/title preserving sentinels)
- Controlled failure and error modes (invalid summary JSON, 500 errors, provider pause)
- Test-only control endpoints (GET /_control/stats, POST /_control/configure, POST /_control/reset)
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit


class ProviderState:
    """Thread-safe in-memory state and metrics for the fake provider."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.request_count = 0
        self.completion_count = 0
        self.title_count = 0
        self.summary_count = 0
        self.mode = "normal"
        self.pause_seconds = 2.0
        self.burst_chunks = 500
        self.burst_chunk_size = 4
        self.history: list[dict[str, Any]] = []

    def reset(self) -> None:
        with self.lock:
            self.request_count = 0
            self.completion_count = 0
            self.title_count = 0
            self.summary_count = 0
            self.mode = "normal"
            self.pause_seconds = 2.0
            self.burst_chunks = 500
            self.burst_chunk_size = 4
            self.history.clear()

    def record_call(self, kind: str, meta: dict[str, Any]) -> None:
        with self.lock:
            self.request_count += 1
            if kind == "title":
                self.title_count += 1
            elif kind == "summary":
                self.summary_count += 1
            else:
                self.completion_count += 1
            if len(self.history) >= 200:
                self.history.pop(0)
            self.history.append({"kind": kind, **meta})

    def configure(self, patch: dict[str, Any]) -> None:
        with self.lock:
            if "mode" in patch:
                self.mode = str(patch["mode"])
            if "pause_seconds" in patch:
                self.pause_seconds = float(patch["pause_seconds"])
            if "burst_chunks" in patch:
                self.burst_chunks = int(patch["burst_chunks"])
            if "burst_chunk_size" in patch:
                self.burst_chunk_size = int(patch["burst_chunk_size"])

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "request_count": self.request_count,
                "completion_count": self.completion_count,
                "title_count": self.title_count,
                "summary_count": self.summary_count,
                "mode": self.mode,
                "pause_seconds": self.pause_seconds,
                "burst_chunks": self.burst_chunks,
                "burst_chunk_size": self.burst_chunk_size,
                "history_count": len(self.history),
                "history": list(self.history),
            }


_STATE = ProviderState()


def _analyze_request(req_json: dict[str, Any]) -> tuple[str, dict[str, Any], str | None, list[str]]:
    """Classify the incoming request without logging private or credential content."""
    messages = req_json.get("messages", [])
    if not isinstance(messages, list):
        messages = []

    system_text = " ".join(
        m.get("content", "")
        for m in messages
        if isinstance(m, dict) and m.get("role") == "system" and isinstance(m.get("content"), str)
    )
    user_text = " ".join(
        m.get("content", "")
        for m in messages
        if isinstance(m, dict) and m.get("role") == "user" and isinstance(m.get("content"), str)
    )
    assistant_text = " ".join(
        m.get("content", "")
        for m in messages
        if isinstance(m, dict) and m.get("role") == "assistant" and isinstance(m.get("content"), str)
    )

    trigger: str | None = None
    if "__TRIGGER_SMALL_DELTA_PAUSE__" in user_text:
        trigger = "small_delta_pause"
    elif "__TRIGGER_BURST__" in user_text:
        trigger = "burst"
    elif "__TRIGGER_PAUSE__" in user_text:
        trigger = "pause"
    elif "__TRIGGER_INVALID_SUMMARY_JSON__" in user_text:
        trigger = "invalid_summary_json"
    elif "__TRIGGER_SUMMARY_ERROR__" in user_text:
        trigger = "summary_error"
    elif "__TRIGGER_TITLE_ERROR__" in user_text:
        trigger = "title_error"
    elif "__TRIGGER_PROVIDER_ERROR__" in user_text:
        trigger = "provider_error"

    sentinels = re.findall(r"SENTINEL[_\w\d\-]+", user_text)

    # Title generation discriminator
    if (
        "concise conversation title" in system_text
        or "first user request and the successful assistant answer" in system_text
    ):
        meta = {
            "messages_count": len(messages),
            "has_docker": "docker" in assistant_text.lower() or "docker" in user_text.lower(),
            "user_chars": len(user_text),
            "assistant_chars": len(assistant_text),
        }
        return "title", meta, trigger, sentinels

    # LiteLLM flattens supported OpenAI options into the request body. Accept
    # either that wire shape or the test-control envelope.
    response_format = req_json.get("response_format")
    if not isinstance(response_format, dict):
        extra = req_json.get("extra")
        response_format = extra.get("response_format") if isinstance(extra, dict) else None
    if (
        "Summarize conversation context as user-level reference data" in system_text
        or "Return strict JSON with exactly summary and title" in user_text
        or (isinstance(response_format, dict) and response_format.get("type") == "json_object")
    ):
        meta = {
            "messages_count": len(messages),
            "has_sentinel": bool(sentinels),
            "sentinels_observed": sentinels[:10],
            "user_chars": len(user_text),
        }
        return "summary", meta, trigger, sentinels

    meta = {
        "messages_count": len(messages),
        "stream": bool(req_json.get("stream", False)),
        "sentinels_observed": sentinels[:10],
        "user_chars": len(user_text),
    }
    return "completion", meta, trigger, sentinels


class FakeOpenAIHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        # Avoid recording secrets or headers; log safe operational summary
        sys.stderr.write(f"[fake-openai] {format % args}\n")

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()
        self.close_connection = True

    def _send_error_json(self, status: int, message: str) -> None:
        self._send_json(status, {"error": {"message": message, "type": "fake_openai_error"}})

    def _read_json_body(self) -> dict[str, Any]:
        content_length = int(self.headers.get("Content-Length", 0))
        body_bytes = self.rfile.read(content_length) if content_length > 0 else b"{}"
        try:
            payload = json.loads(body_bytes)
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _send_sse(self, events: list[dict[str, Any]]) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        for event in events:
            event_type = str(event["type"])
            data = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
            self.wfile.write(f"event: {event_type}\ndata: {data}\n\n".encode())
            self.wfile.flush()
        self.close_connection = True

    @staticmethod
    def _responses_object(
        model: str,
        *,
        status: str = "completed",
        text: str = "Hello from fake provider!",
        output: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if output is None:
            output = [
                {
                    "id": "msg_fake123",
                    "type": "message",
                    "status": "completed" if status == "completed" else "in_progress",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": text if status == "completed" else "",
                            "annotations": [],
                        }
                    ],
                }
            ]
        return {
            "id": "resp_fake123",
            "object": "response",
            "created_at": 1700000000,
            "status": status,
            "background": False,
            "error": None,
            "incomplete_details": None,
            "instructions": None,
            "max_output_tokens": None,
            "model": model,
            "output": output,
            "parallel_tool_calls": True,
            "previous_response_id": None,
            "reasoning": {"effort": None, "summary": None},
            "store": False,
            "temperature": 1.0,
            "text": {"format": {"type": "text"}},
            "tool_choice": "auto",
            "tools": [],
            "top_p": 1.0,
            "truncation": "disabled",
            "usage": {
                "input_tokens": 10,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens": 5,
                "output_tokens_details": {"reasoning_tokens": 0},
                "total_tokens": 15,
            },
            "metadata": {},
        }

    def _handle_responses(self, request: dict[str, Any]) -> None:
        model = str(request.get("model") or "fake-gpt-4")
        input_items = request.get("input") if isinstance(request.get("input"), list) else []
        tools = request.get("tools") if isinstance(request.get("tools"), list) else []
        tool_names = [str(tool.get("name")) for tool in tools if isinstance(tool, dict) and tool.get("name")]
        tool_schemas: dict[str, dict[str, str]] = {}
        for tool in tools:
            if not isinstance(tool, dict) or not tool.get("name"):
                continue
            properties = (tool.get("parameters") or {}).get("properties", {})
            tool_schemas[str(tool["name"])] = {
                str(key): str(value.get("type"))
                for key, value in properties.items()
                if isinstance(value, dict) and value.get("type")
            }
        _STATE.record_call(
            "completion",
            {
                "protocol": "responses",
                "stream": bool(request.get("stream")),
                "input_types": [str(item.get("type")) for item in input_items if isinstance(item, dict)],
                "tool_names": tool_names,
                "tool_schemas": tool_schemas,
                "has_prompt_cache_key": isinstance(request.get("prompt_cache_key"), str),
                "has_client_metadata": "client_metadata" in request,
            },
        )

        mode = _STATE.snapshot()["mode"]
        has_tool_output = any(
            isinstance(item, dict) and item.get("type") == "function_call_output" for item in input_items
        )
        text = "CODEX_TOOL_CONTINUATION_OK" if mode == "codex_tool" and has_tool_output else "Hello from fake provider!"

        if mode == "codex_tool" and not has_tool_output:
            command_tool = next(
                (
                    tool
                    for tool in tools
                    if isinstance(tool, dict)
                    and tool.get("type") == "function"
                    and tool.get("name") in {"shell", "exec_command"}
                ),
                None,
            )
            if command_tool is None:
                self._send_error_json(400, "Codex command tool was not advertised")
                return
            tool_name = str(command_tool["name"])
            properties = (command_tool.get("parameters") or {}).get("properties", {})
            argument_name = "cmd" if "cmd" in properties else "command"
            argument_schema = properties.get(argument_name, {})
            command: str | list[str]
            if argument_schema.get("type") == "array":
                command = ["/bin/sh", "-lc", "printf CODEX_TOOL_OK"]
            else:
                command = "printf CODEX_TOOL_OK"
            arguments = json.dumps({argument_name: command}, separators=(",", ":"))
            function_call = {
                "id": "fc_codex_item",
                "type": "function_call",
                "status": "completed",
                "arguments": arguments,
                "call_id": "call_codex_1",
                "name": tool_name,
            }
            completed = self._responses_object(model, output=[function_call])
            if not request.get("stream"):
                self._send_json(200, completed)
                return
            in_progress_item = {**function_call, "status": "in_progress", "arguments": ""}
            self._send_sse(
                [
                    {
                        "type": "response.created",
                        "sequence_number": 0,
                        "response": self._responses_object(model, status="in_progress", output=[]),
                    },
                    {
                        "type": "response.output_item.added",
                        "sequence_number": 1,
                        "output_index": 0,
                        "item": in_progress_item,
                    },
                    {
                        "type": "response.function_call_arguments.delta",
                        "sequence_number": 2,
                        "item_id": "fc_codex_item",
                        "output_index": 0,
                        "delta": arguments,
                    },
                    {
                        "type": "response.function_call_arguments.done",
                        "sequence_number": 3,
                        "item_id": "fc_codex_item",
                        "output_index": 0,
                        "arguments": arguments,
                    },
                    {
                        "type": "response.output_item.done",
                        "sequence_number": 4,
                        "output_index": 0,
                        "item": function_call,
                    },
                    {"type": "response.completed", "sequence_number": 5, "response": completed},
                ]
            )
            return

        completed = self._responses_object(model, text=text)
        if not request.get("stream"):
            self._send_json(200, completed)
            return
        in_progress = self._responses_object(model, status="in_progress", text=text)
        self._send_sse(
            [
                {"type": "response.created", "sequence_number": 0, "response": in_progress},
                {
                    "type": "response.output_text.delta",
                    "sequence_number": 1,
                    "item_id": "msg_fake123",
                    "output_index": 0,
                    "content_index": 0,
                    "delta": text,
                    "logprobs": [],
                },
                {
                    "type": "response.output_text.done",
                    "sequence_number": 2,
                    "item_id": "msg_fake123",
                    "output_index": 0,
                    "content_index": 0,
                    "text": text,
                    "logprobs": [],
                },
                {"type": "response.completed", "sequence_number": 3, "response": completed},
            ]
        )

    def _handle_anthropic_messages(self, request: dict[str, Any]) -> None:
        model = str(request.get("model") or "fake-claude")
        tools = request.get("tools") if isinstance(request.get("tools"), list) else []
        tool_names = [
            tool.get("name") for tool in tools if isinstance(tool, dict) and isinstance(tool.get("name"), str)
        ]
        messages = request.get("messages") if isinstance(request.get("messages"), list) else []
        tool_results = [
            block
            for message in messages
            if isinstance(message, dict) and isinstance(message.get("content"), list)
            for block in message["content"]
            if isinstance(block, dict) and block.get("type") == "tool_result"
        ]
        protocol_headers = sorted(name.lower() for name in self.headers if name.lower().startswith("anthropic-"))
        _STATE.record_call(
            "completion",
            {
                "protocol": "anthropic",
                "model": model,
                "stream": bool(request.get("stream")),
                "request_keys": sorted(request),
                "protocol_headers": protocol_headers,
                "tool_names": tool_names,
                "has_tool_result": bool(tool_results),
            },
        )

        if _STATE.mode == "claude_tool" and not tool_results:
            tool_name = "Bash" if "Bash" in tool_names else (tool_names[0] if tool_names else "Bash")
            tool_input = {
                "command": "printf CLAUDE_LOCAL_TOOL_SENTINEL",
                "description": "Print a deterministic local sentinel",
            }
            message = {
                "id": "msg_fake_anthropic_tool",
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [{"type": "tool_use", "id": "toolu_fake_1", "name": tool_name, "input": tool_input}],
                "stop_reason": "tool_use",
                "stop_sequence": None,
                "usage": {"input_tokens": 10, "output_tokens": 10},
            }
            if not request.get("stream"):
                self._send_json(200, message)
                return
            self._send_sse(
                [
                    {
                        "type": "message_start",
                        "message": {
                            **message,
                            "content": [],
                            "stop_reason": None,
                            "usage": {"input_tokens": 10, "output_tokens": 0},
                        },
                    },
                    {
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {"type": "tool_use", "id": "toolu_fake_1", "name": tool_name, "input": {}},
                    },
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "input_json_delta", "partial_json": json.dumps(tool_input)},
                    },
                    {"type": "content_block_stop", "index": 0},
                    {
                        "type": "message_delta",
                        "delta": {"stop_reason": "tool_use", "stop_sequence": None},
                        "usage": {"output_tokens": 10},
                    },
                    {"type": "message_stop"},
                ]
            )
            return

        text = "Hello from fake provider!"
        if _STATE.mode == "claude_tool":
            result_text = json.dumps(tool_results, ensure_ascii=False)
            text = (
                "CLAUDE_TOOL_CONTINUATION_OK"
                if "CLAUDE_LOCAL_TOOL_SENTINEL" in result_text
                else "CLAUDE_TOOL_RESULT_MISSING"
            )
        message = {
            "id": "msg_fake_anthropic",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
        if not request.get("stream"):
            self._send_json(200, message)
            return
        self._send_sse(
            [
                {
                    "type": "message_start",
                    "message": {
                        **message,
                        "content": [],
                        "stop_reason": None,
                        "usage": {"input_tokens": 10, "output_tokens": 0},
                    },
                },
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                },
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": text},
                },
                {"type": "content_block_stop", "index": 0},
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                    "usage": {"output_tokens": 5},
                },
                {"type": "message_stop"},
            ]
        )

    def do_HEAD(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        if path in ("/health", "/v1/health", "/", "/_control/health"):
            self._send_json(200, {"status": "ok"})
            return

        if path in ("/_control/stats", "/test/stats"):
            self._send_json(200, _STATE.snapshot())
            return

        if path == "/v1/models" and self.headers.get("x-api-key"):
            # Discovery must follow the Anthropic cursor, not just its first page.
            cursor = parse_qs(urlsplit(self.path).query).get("after_id", [None])[0]
            first_id = "claude-sonnet-4-6"
            if cursor is None:
                ids, has_more = [first_id], True
            elif cursor == first_id:
                ids, has_more = ["claude-system-unlisted-2099-01-01", "claude-system-unpriced-2099-01-01"], False
            else:
                self._send_error_json(400, "unknown model cursor")
                return
            self._send_json(
                200,
                {
                    "data": [
                        {"type": "model", "id": mid, "display_name": mid, "created_at": "2099-01-01T00:00:00Z"}
                        for mid in ids
                    ],
                    "has_more": has_more,
                    "first_id": ids[0],
                    "last_id": ids[-1],
                },
            )
            return

        if path in ("/models", "/v1/models"):
            self._send_json(
                200,
                {
                    "object": "list",
                    "data": [
                        {
                            "id": "fake-gpt-4",
                            "object": "model",
                            "created": 1700000000,
                            "owned_by": "fake",
                        }
                    ],
                },
            )
            return

        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def do_POST(self) -> None:
        path = self.path.split("?")[0]

        if path in ("/_control/configure", "/test/configure"):
            content_length = int(self.headers.get("Content-Length", 0))
            body_bytes = self.rfile.read(content_length) if content_length > 0 else b"{}"
            try:
                patch = json.loads(body_bytes)
            except Exception:
                patch = {}
            _STATE.configure(patch)
            self._send_json(200, {"status": "ok", "state": _STATE.snapshot()})
            return

        if path in ("/_control/reset", "/test/reset"):
            _STATE.reset()
            self._send_json(200, {"status": "ok", "reset": True})
            return

        if path in ("/responses", "/v1/responses"):
            self._handle_responses(self._read_json_body())
            return

        if path in ("/messages", "/v1/messages"):
            self._handle_anthropic_messages(self._read_json_body())
            return

        if path not in ("/chat/completions", "/v1/chat/completions"):
            self._send_json(404, {"error": "not found"})
            return

        content_length = int(self.headers.get("Content-Length", 0))
        body_bytes = self.rfile.read(content_length) if content_length > 0 else b"{}"

        try:
            req_json = json.loads(body_bytes)
        except Exception:
            req_json = {}

        model_name = req_json.get("model", "fake-gpt-4")
        is_stream = bool(req_json.get("stream", False))

        kind, meta, trigger, sentinels = _analyze_request(req_json)
        _STATE.record_call(kind, meta)

        snap = _STATE.snapshot()
        effective_mode = trigger or snap["mode"]
        pause_seconds = snap["pause_seconds"]

        # 1. Title generation handler
        if kind == "title":
            if effective_mode in ("title_error", "provider_error"):
                self._send_error_json(500, "simulated title provider failure")
                return
            if effective_mode == "pause":
                time.sleep(pause_seconds)

            # Generate title influenced by first user query and assistant answer
            user_text = " ".join(
                m.get("content", "")
                for m in req_json.get("messages", [])
                if isinstance(m, dict) and m.get("role") == "user" and isinstance(m.get("content"), str)
            )
            assistant_text = " ".join(
                m.get("content", "")
                for m in req_json.get("messages", [])
                if isinstance(m, dict) and m.get("role") == "assistant" and isinstance(m.get("content"), str)
            )

            if "docker" in assistant_text.lower() or "docker" in user_text.lower():
                title = "Docker 배포 가이드"
            elif "mariadb" in assistant_text.lower() or "mariadb" in user_text.lower():
                title = "MariaDB 데이터베이스 구축"
            elif user_text.strip():
                clean_words = [
                    w for w in user_text.split() if not w.startswith("__TRIGGER_") and len(w) > 1 and w != "hello"
                ]
                if clean_words:
                    title = f"{' '.join(clean_words[:3])} 대화 요약"
                else:
                    title = "새 대화 첫 요약"
            else:
                title = "새 대화 첫 요약"

            title = " ".join(title.split()[:6])[:80]

            self._send_json(
                200,
                {
                    "id": f"chatcmpl-title-{int(time.time())}",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": model_name,
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": title,
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 20,
                        "completion_tokens": 6,
                        "total_tokens": 26,
                    },
                },
            )
            return

        # 2. Context compaction handler
        if kind == "summary":
            if effective_mode in ("summary_error", "provider_error"):
                self._send_error_json(500, "simulated context compaction failure")
                return

            if effective_mode == "invalid_summary_json":
                # Return invalid non-JSON payload to trigger compaction_failed
                self._send_json(
                    200,
                    {
                        "id": f"chatcmpl-summary-invalid-{int(time.time())}",
                        "object": "chat.completion",
                        "created": int(time.time()),
                        "model": model_name,
                        "choices": [
                            {
                                "index": 0,
                                "message": {
                                    "role": "assistant",
                                    "content": "Not valid JSON { summary: incomplete",
                                },
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {"prompt_tokens": 30, "completion_tokens": 5, "total_tokens": 35},
                    },
                )
                return

            if effective_mode == "pause":
                time.sleep(pause_seconds)

            sentinel_repr = f"보존 감시값: {', '.join(sentinels)}. " if sentinels else "보존 감시값 없음. "
            summary_text = (
                f"대화 컨텍스트 요약: 이전 아키텍처 결정과 제약 조건을 유지합니다. "
                f"{sentinel_repr}핵심 요구사항이 성공적으로 보존되었습니다."
            )
            title_text = "압축된 대화 요약"

            strict_payload = {"summary": summary_text, "title": title_text}
            self._send_json(
                200,
                {
                    "id": f"chatcmpl-summary-{int(time.time())}",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": model_name,
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": json.dumps(strict_payload, ensure_ascii=False),
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 40,
                        "completion_tokens": 25,
                        "total_tokens": 65,
                    },
                },
            )
            return

        # 3. Standard Non-streaming Completion
        if not is_stream:
            if effective_mode == "provider_error":
                self._send_error_json(500, "simulated provider error")
                return
            if effective_mode == "pause":
                time.sleep(pause_seconds)

            self._send_json(
                200,
                {
                    "id": "chatcmpl-fake123",
                    "object": "chat.completion",
                    "created": 1700000000,
                    "model": model_name,
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": "Hello from fake provider!",
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 5,
                        "total_tokens": 15,
                    },
                },
            )
            return

        # 4. Streaming Completion (SSE)
        if effective_mode == "provider_error":
            self._send_error_json(500, "simulated streaming provider error")
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        # Scenario A: deterministic small-delta then 2s pause
        if effective_mode == "small_delta_pause":
            delta1 = json.dumps(
                {
                    "id": "chatcmpl-fake-pause",
                    "object": "chat.completion.chunk",
                    "created": 1700000000,
                    "model": model_name,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant", "content": "delta1"},
                            "finish_reason": None,
                        }
                    ],
                }
            )
            self.wfile.write(f"data: {delta1}\n\n".encode())
            self.wfile.flush()

            # Pause for 2 seconds to prove consumer gets delta1 within 250ms deadline
            time.sleep(pause_seconds)

            delta2 = json.dumps(
                {
                    "id": "chatcmpl-fake-pause",
                    "object": "chat.completion.chunk",
                    "created": 1700000000,
                    "model": model_name,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": " - remainder of streamed message"},
                            "finish_reason": None,
                        }
                    ],
                }
            )
            self.wfile.write(f"data: {delta2}\n\n".encode())
            self.wfile.flush()

            delta_stop = json.dumps(
                {
                    "id": "chatcmpl-fake-pause",
                    "object": "chat.completion.chunk",
                    "created": 1700000000,
                    "model": model_name,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                }
            )
            self.wfile.write(f"data: {delta_stop}\n\n".encode())
            self.wfile.flush()

            delta_usage = json.dumps(
                {
                    "id": "chatcmpl-fake-pause",
                    "object": "chat.completion.chunk",
                    "created": 1700000000,
                    "model": model_name,
                    "choices": [],
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 8,
                        "total_tokens": 18,
                    },
                }
            )
            self.wfile.write(f"data: {delta_usage}\n\n".encode())
            self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return

        # Scenario B: 500x4 burst streaming
        if effective_mode == "burst":
            burst_count = snap["burst_chunks"]
            for i in range(burst_count):
                chunk_str = f"B{i:03d}"  # Exactly 4 characters
                chunk_payload = json.dumps(
                    {
                        "id": "chatcmpl-fake-burst",
                        "object": "chat.completion.chunk",
                        "created": 1700000000,
                        "model": model_name,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": chunk_str},
                                "finish_reason": None,
                            }
                        ],
                    }
                )
                self.wfile.write(f"data: {chunk_payload}\n\n".encode())
                if i % 10 == 0:
                    self.wfile.flush()

            self.wfile.flush()

            chunk_stop = json.dumps(
                {
                    "id": "chatcmpl-fake-burst",
                    "object": "chat.completion.chunk",
                    "created": 1700000000,
                    "model": model_name,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                }
            )
            self.wfile.write(f"data: {chunk_stop}\n\n".encode())
            self.wfile.flush()

            chunk_usage = json.dumps(
                {
                    "id": "chatcmpl-fake-burst",
                    "object": "chat.completion.chunk",
                    "created": 1700000000,
                    "model": model_name,
                    "choices": [],
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": burst_count,
                        "total_tokens": burst_count + 10,
                    },
                }
            )
            self.wfile.write(f"data: {chunk_usage}\n\n".encode())
            self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return

        # Scenario C: provider pause before standard stream
        if effective_mode == "pause":
            time.sleep(pause_seconds)

        # Standard Default Stream
        chunk1 = json.dumps(
            {
                "id": "chatcmpl-fake123",
                "object": "chat.completion.chunk",
                "created": 1700000000,
                "model": model_name,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": "Hello from fake provider!"},
                        "finish_reason": None,
                    }
                ],
            }
        )

        chunk2 = json.dumps(
            {
                "id": "chatcmpl-fake123",
                "object": "chat.completion.chunk",
                "created": 1700000000,
                "model": model_name,
                "choices": [
                    {
                        "index": 0,
                        "delta": {},
                        "finish_reason": "stop",
                    }
                ],
            }
        )

        chunk3 = json.dumps(
            {
                "id": "chatcmpl-fake123",
                "object": "chat.completion.chunk",
                "created": 1700000000,
                "model": model_name,
                "choices": [],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                },
            }
        )

        self.wfile.write(f"data: {chunk1}\n\n".encode())
        self.wfile.flush()
        self.wfile.write(f"data: {chunk2}\n\n".encode())
        self.wfile.flush()
        self.wfile.write(f"data: {chunk3}\n\n".encode())
        self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


def main() -> None:
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), FakeOpenAIHandler)
    sys.stderr.write(f"[fake-openai] Server listening on port {port}\n")
    server.serve_forever()


if __name__ == "__main__":
    main()
