"""Actual HTTP/SSE boundary tests; no provider SDK or runtime mocking transport."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from lumen.services import provider_client as client
from lumen.services.provider_errors import APIConnectionError, AuthenticationError, BadRequestError, InternalServerError
from lumen.services.providers import chatgpt_transport, credentials
from lumen.services.providers.errors import ProviderSubscriptionError
from lumen.services.usage_breakdown import UsageBreakdown


def sse(*events):
    return "".join("data: " + (event if isinstance(event, str) else json.dumps(event)) + "\r\n\r\n" for event in events).encode()


@asynccontextmanager
async def http_fixture(*replies):
    """Small real HTTP listener, recording requests and optional held-open SSE."""
    requests = []
    tasks = set()
    disconnected = asyncio.Event()

    async def serve(reader, writer):
        tasks.add(asyncio.current_task())
        try:
            raw = await reader.readuntil(b"\r\n\r\n")
            lines = raw.decode().split("\r\n")
            headers = dict(line.split(": ", 1) for line in lines[1:] if ": " in line)
            body = await reader.readexactly(int(headers.get("Content-Length", "0")))
            requests.append({"path": lines[0].split()[1], "headers": headers, "body": json.loads(body)})
            reply = replies[min(len(requests) - 1, len(replies) - 1)]
            if reply.get("disconnect"):
                return
            status = reply.get("status", 200)
            payload = reply.get("body", {})
            data = json.dumps(payload).encode() if isinstance(payload, dict) else payload
            response_headers = {"Content-Type": "application/json", "X-Lumen-Bifrost-Contract": "single-attempt-v1",
                                **reply.get("headers", {})}
            if not reply.get("hold"):
                response_headers["Content-Length"] = str(len(data))
            response_headers["Connection"] = "close"
            writer.write(f"HTTP/1.1 {status} Response\r\n".encode())
            for key, value in response_headers.items():
                writer.write(f"{key}: {value}\r\n".encode())
            writer.write(b"\r\n")
            # Fragment records across TCP writes, including JSON UTF-8 bytes.
            for offset in range(0, len(data), 7):
                writer.write(data[offset:offset + 7])
                await writer.drain()
            if reply.get("hold"):
                await reader.read()
                disconnected.set()
        except (asyncio.IncompleteReadError, ConnectionError):
            disconnected.set()
        finally:
            writer.close()
            await writer.wait_closed()
            tasks.discard(asyncio.current_task())

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    address = server.sockets[0].getsockname()
    try:
        yield SimpleNamespace(url=f"http://127.0.0.1:{address[1]}", requests=requests, disconnected=disconnected)
    finally:
        server.close()
        await server.wait_closed()
        remaining = list(tasks)
        for task in remaining:
            task.cancel()
        await asyncio.gather(*remaining, return_exceptions=True)


async def connect_runtime(monkeypatch, fixture):
    async def runtime():
        return fixture.url, "private-runtime-token"
    monkeypatch.setattr(client, "get_bifrost_runtime", runtime)


def auth(credential_id=3, mode="api_key"):
    return {"provider_id": 1, "account_id": 2, "credential_id": credential_id, "generation": 4, "auth_mode": mode}


@pytest.mark.asyncio
async def test_api_credentials_resolved_after_runtime_ready_and_each_selected_key_sent_once(monkeypatch):
    order = []
    async def resolve(ref):
        order.append(f"resolve-{ref['credential_id']}")
        return {"api_key": f"key-{ref['credential_id']}", "_fingerprint": f"fp-{ref['credential_id']}"}
    monkeypatch.setattr(credentials, "resolve_inference_credential", resolve)
    reply = {"choices": [{"message": {"content": "hello", "tool_calls": [{"function": {"arguments": "{}"}}]}}],
             "usage": {"prompt_tokens": 3, "completion_tokens": 1}}
    async with http_fixture({"body": reply}) as fixture:
        async def runtime():
            order.append("ready")
            return fixture.url, "internal"
        monkeypatch.setattr(client, "get_bifrost_runtime", runtime)
        for selected in [3, 5]:
            result = await client.acompletion("openai/gpt-5", [{"role": "user", "content": "hi"}],
                                              custom_llm_provider="openai", provider_auth=auth(selected),
                                              max_tokens=77, extra={"reasoning_effort": "none", "parallel_tool_calls": False})
            assert result.choices[0].message.content == "hello"
            assert result.choices[0].message.tool_calls[0].function.arguments == "{}"
            assert result.model_dump()["usage"]["prompt_tokens"] == 3
        assert [request["body"]["api_key"] for request in fixture.requests] == ["key-3", "key-5"]
        assert order == ["ready", "resolve-3", "ready", "resolve-5"]
        for request in fixture.requests:
            assert request["path"] == "/v1/execute"
            assert request["body"]["single_attempt"] is True
            assert request["body"]["body"]["max_tokens"] == 77
            assert request["body"]["body"]["reasoning_effort"] == "none"
            assert request["body"]["body"]["parallel_tool_calls"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("status,expected,known", [(400, BadRequestError, True), (401, AuthenticationError, True),
                                                   (503, InternalServerError, False)])
async def test_http_errors_sanitized_not_retried_and_auth_rejection_fenced(monkeypatch, status, expected, known):
    rejected = []
    async def resolve(ref):
        return {"api_key": "upstream-secret", "_fingerprint": "safe-fingerprint"}
    async def mark(ref, fingerprint):
        rejected.append((ref, fingerprint))
    monkeypatch.setattr(credentials, "resolve_inference_credential", resolve)
    monkeypatch.setattr(credentials, "mark_inference_credential_rejected", mark)
    async with http_fixture({"status": status, "body": {"error": {"code": "upstream_rejected", "message": "upstream-secret credential payload"}}}) as fixture:
        await connect_runtime(monkeypatch, fixture)
        with pytest.raises(expected) as raised:
            await client.acompletion("gpt", [], custom_llm_provider="openai", provider_auth=auth())
        assert raised.value.known_rejection is known
        assert "upstream-secret" not in str(raised.value)
        assert "credential payload" not in repr(raised.value)
        assert not hasattr(raised.value, "response")
        assert len(fixture.requests) == 1
        assert rejected == ([(auth(), "safe-fingerprint")] if status == 401 else [])


@pytest.mark.asyncio
async def test_redirect_never_followed(monkeypatch):
    async with http_fixture({"status": 307, "headers": {"Location": "/unexpected"}}) as fixture:
        await connect_runtime(monkeypatch, fixture)
        with pytest.raises(APIConnectionError):
            await client.acompletion("gpt", [], api_key="key", custom_llm_provider="openai")
        assert len(fixture.requests) == 1


@pytest.mark.asyncio
async def test_chat_stream_preserves_tools_citations_reasoning_and_modality_usage(monkeypatch):
    usage = {"prompt_tokens": 20, "completion_tokens": 5,
             "prompt_tokens_details": {"cached_tokens": 4},
             "modality_tokens": {"image": {"input_tokens": 6, "output_tokens": 0}}}
    events = [{"choices": [{"index": 0, "delta": {"content": "한글", "reasoning_content": "thought",
                "tool_calls": [{"index": 0, "id": "c1", "function": {"name": "lookup", "arguments": "{}"}}],
                "annotations": [{"type": "url_citation", "url": "https://example.org"}]}, "finish_reason": None}]},
              {"choices": [], "usage": usage}, "[DONE]"]
    async with http_fixture({"body": sse(*events), "headers": {"Content-Type": "text/event-stream"}}) as fixture:
        await connect_runtime(monkeypatch, fixture)
        stream = await client.acompletion_stream("gemini/model", [], api_key="key", custom_llm_provider="gemini",
                                                  max_tokens=10, native_web_search={}, reasoning_effort="high")
        chunks = [chunk async for chunk in stream]
        assert chunks[0].choices[0].delta.content == "한글"
        assert chunks[0].choices[0].delta.tool_calls[0].function.name == "lookup"
        assert chunks[0].choices[0].delta.annotations[0].url == "https://example.org"
        assert chunks[1].usage.modality_tokens.image.input_tokens == 6
        assert chunks[1].usage.model_dump() == usage
        assert client.direct_openai_stream_completed(stream)
        assert stream._client.is_closed
        body = fixture.requests[0]["body"]["body"]
        assert body["web_search_options"] == {}
        assert body["max_tokens"] == 10
        assert body["reasoning_effort"] == "high"


@pytest.mark.asyncio
async def test_chat_finish_reason_does_not_mask_truncated_stream(monkeypatch):
    data = sse({"choices": [{"index": 0, "delta": {"content": "partial"}, "finish_reason": "stop"}]})
    async with http_fixture({"body": data, "headers": {"Content-Type": "text/event-stream"}}) as fixture:
        await connect_runtime(monkeypatch, fixture)
        stream = await client.acompletion_stream("gpt", [], custom_llm_provider="openai")
        assert (await anext(stream)).choices[0].delta.content == "partial"
        with pytest.raises(APIConnectionError):
            await anext(stream)
        assert not stream.completed
        assert stream._client.is_closed
        assert len(fixture.requests) == 1


@pytest.mark.asyncio
async def test_cancellation_closes_held_http_stream(monkeypatch):
    async with http_fixture({"body": b": waiting\n\n", "hold": True,
                             "headers": {"Content-Type": "text/event-stream"}}) as fixture:
        await connect_runtime(monkeypatch, fixture)
        stream = await client.acompletion_stream("gpt", [], custom_llm_provider="openai")
        task = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stream._client.is_closed
        await asyncio.wait_for(fixture.disconnected.wait(), 2)


@pytest.mark.asyncio
async def test_native_responses_events_and_explicit_options_round_trip(monkeypatch):
    completed = {"type": "response.completed", "response": {"id": "r", "status": "completed", "output": [
        {"type": "reasoning", "id": "reason", "encrypted_content": "opaque", "summary": []}],
        "usage": {"input_tokens": 5, "output_tokens": 1}}}
    async with http_fixture({"body": sse(completed), "headers": {"Content-Type": "text/event-stream"}}) as fixture:
        await connect_runtime(monkeypatch, fixture)
        stream = await client.aresponses(model="gpt", input="hello", stream=True, api_base=None, api_key="key",
                                         custom_llm_provider="openai", provider_auth=None, max_output_tokens=11,
                                         reasoning={"effort": "none"}, context_management=[{"type": "compaction", "compact_threshold": 100}])
        result = [event async for event in stream]
        assert result[0].model_dump() == completed
        assert stream.completed
        body = fixture.requests[0]["body"]
        assert body["protocol"] == "responses"
        assert body["body"]["max_output_tokens"] == 11
        assert body["body"]["context_management"][0]["compact_threshold"] == 100


@pytest.mark.asyncio
async def test_native_responses_nullable_error_is_success_not_a_provider_failure(monkeypatch):
    response = {"id": "resp-null-error", "object": "response", "status": "completed", "error": None,
        "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "accepted"}]}],
        "usage": {"input_tokens": 3, "output_tokens": 2}}
    async with http_fixture({"body": response}) as fixture:
        await connect_runtime(monkeypatch, fixture)
        result = await client.aresponses(model="gpt", input="hello", stream=False, api_base=None, api_key="key",
                                        custom_llm_provider="openai", provider_auth=None)
        assert result.output[0].content[0].text == "accepted"
        assert result.error is None
        assert len(fixture.requests) == 1
    terminal = {"type": "response.completed", "error": None, "response": response}
    async with http_fixture({"body": sse(terminal), "headers": {"Content-Type": "text/event-stream"}}) as fixture:
        await connect_runtime(monkeypatch, fixture)
        stream = await client.aresponses(model="gpt", input="hello", stream=True, api_base=None, api_key="key",
                                        custom_llm_provider="openai", provider_auth=None)
        assert [event.model_dump() async for event in stream] == [terminal]
        assert stream.completed


@pytest.mark.asyncio
async def test_native_anthropic_subscription_bearer_headers_cache_compaction_usage(monkeypatch):
    async def resolve(ref):
        return {"access_token": "oauth-secret", "_fingerprint": "fp"}
    monkeypatch.setattr(credentials, "resolve_inference_credential", resolve)
    event = {"type": "message_start", "message": {"usage": {"input_tokens": 3, "cache_read_input_tokens": 10,
                                                              "cache_creation_input_tokens": 7}}}
    async with http_fixture({"body": sse(event, {"type": "message_stop"}),
                             "headers": {"Content-Type": "text/event-stream"}}) as fixture:
        await connect_runtime(monkeypatch, fixture)
        stream = await client.aanthropic_messages(model="anthropic-subscription/claude", messages=[], max_tokens=100,
            stream=True, api_base="https://not-used.example", api_key=None, custom_llm_provider="anthropic",
            provider_auth=auth(mode="anthropic_subscription"), thinking={"type": "adaptive"},
            context_management={"edits": [{"type": "compact_20260112"}]}, output_config={"effort": "high"},
            anthropic_headers={"anthropic-beta": "compact-2026-01-12"})
        events = [item async for item in stream]
        assert events[0].message.usage.cache_creation_input_tokens == 7
        sent = fixture.requests[0]["body"]
        assert sent["api_key"] is None
        assert sent["api_base"] == "https://api.anthropic.com"
        assert sent["headers"]["Authorization"] == "Bearer oauth-secret"
        assert sent["headers"]["anthropic-beta"] == "compact-2026-01-12,oauth-2025-04-20"
        assert sent["body"]["thinking"] == {"type": "adaptive"}
        assert sent["body"]["max_tokens"] == 100
        assert sent["body"]["context_management"]["edits"][0]["type"] == "compact_20260112"


@pytest.mark.asyncio
async def test_embeddings_use_same_resolved_boundary(monkeypatch):
    async def resolve(ref):
        return {"api_key": "embedding-key", "_fingerprint": "fp"}
    monkeypatch.setattr(credentials, "resolve_inference_credential", resolve)
    async with http_fixture({"body": {"data": [{"index": 0, "embedding": [0.1, 0.2]}], "usage": {"prompt_tokens": 2}}}) as fixture:
        await connect_runtime(monkeypatch, fixture)
        result = await client.aembedding("openai/embed", ["one"], dimensions=2, provider_auth=auth())
        assert result.data[0].embedding == [0.1, 0.2]
        assert fixture.requests[0]["body"]["protocol"] == "embeddings"
        assert fixture.requests[0]["body"]["body"]["dimensions"] == 2


@pytest.mark.asyncio
async def test_chatgpt_actual_sse_nonstream_tools_encrypted_reasoning_and_usage(monkeypatch):
    completed = {"type": "response.completed", "response": {"id": "r1", "status": "completed", "output": [
        {"type": "message", "content": [{"type": "output_text", "text": "answer", "annotations": [{"type": "url_citation", "url": "https://example.org"}]}]},
        {"type": "reasoning", "id": "rs1", "encrypted_content": "opaque", "summary": [{"text": "thinking"}]},
        {"type": "function_call", "id": "fc1", "call_id": "call1", "name": "lookup", "arguments": "{}"}],
        "usage": {"input_tokens": 13, "output_tokens": 7, "input_tokens_details": {"cached_tokens": 4}}}}
    async def resolve(ref):
        return {"access_token": "subscription-secret", "account_id": "account", "_fingerprint": "fp"}
    monkeypatch.setattr(credentials, "resolve_inference_credential", resolve)
    async with http_fixture({"body": sse(completed), "headers": {"Content-Type": "text/event-stream"}}) as fixture:
        monkeypatch.setattr(chatgpt_transport, "_CHATGPT_RESPONSES_URL", fixture.url + "/responses")
        result = await client.acompletion("chatgpt/gpt-codex", [{"role": "system", "content": "custom instruction"},
            {"role": "user", "content": "hello"}], custom_llm_provider="chatgpt", provider_auth=auth(mode="chatgpt_device"),
            extra={"reasoning_effort": "low"})
        assert result.choices[0].message.content == "answer"
        assert result.choices[0].message.reasoning_items[0].encrypted_content == "opaque"
        assert result.choices[0].message.tool_calls[0].id == "call1"
        assert result.choices[0].finish_reason == "tool_calls"
        assert UsageBreakdown.from_runtime(result.usage).cache_read_input_tokens == 4
        body = fixture.requests[0]["body"]
        assert body["model"] == "gpt-codex"
        assert "max_output_tokens" not in body
        assert body["reasoning"] == {"effort": "low"}
        assert body["instructions"] == "custom instruction"
        assert body["store"] is False
        assert body["stream"] is True
        assert fixture.requests[0]["headers"]["ChatGPT-Account-Id"] == "account"


@pytest.mark.asyncio
async def test_chatgpt_truncation_never_fabricates_terminal_success(monkeypatch):
    event = {"type": "response.output_text.delta", "delta": "partial"}
    async with http_fixture({"body": sse(event), "headers": {"Content-Type": "text/event-stream"}}) as fixture:
        monkeypatch.setattr(chatgpt_transport, "_CHATGPT_RESPONSES_URL", fixture.url + "/responses")
        stream = await chatgpt_transport.acompletion("chatgpt/gpt", [], credential={"access_token": "secret", "account_id": "a"},
                                                     stream=True, optional_params={})
        assert (await anext(stream)).choices[0].delta.content == "partial"
        with pytest.raises(ProviderSubscriptionError):
            await anext(stream)
        assert not stream.completed
        assert stream._source._client.is_closed
        assert len(fixture.requests) == 1


@pytest.mark.parametrize("provider", ["openai", "anthropic", "gemini", "chatgpt"])
@pytest.mark.parametrize("effort,expected", [("none", "none"), ("ultra", "ultra"), ("auto", None), (None, None)])
def test_explicit_reasoning_is_preserved_for_runtime_conversion(provider, effort, expected):
    assert client._reasoning_params("unknown-model", effort, provider) == (
        {} if expected is None else {"reasoning_effort": expected})


def test_stable_anthropic_cache_marker_is_nonmutating_and_respects_all_explicit_breakpoints(monkeypatch):
    monkeypatch.setattr(client, "catalog_model_metadata", lambda *args: {"supports_prompt_caching": True})
    messages = [{"role": "system", "content": [{"type": "text", "text": "stable"}]},
                {"role": "user", "content": "volatile"}]
    original = json.dumps(messages)
    marked = client._anthropic_cached_messages("claude", messages, "anthropic", None)
    assert marked[0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert json.dumps(messages) == original
    for tools, extra in [([{"type": "function", "cache_control": {"type": "ephemeral"}}], None),
                         (None, {"system": [{"type": "text", "text": "caller", "cache_control": {"type": "ephemeral"}}]})]:
        assert client._anthropic_cached_messages("claude", messages, "anthropic", None, tools=tools, extra=extra) is messages
    explicit = [{"role": "user", "content": [{"type": "text", "text": "x", "cache_control": {"type": "ephemeral"}}]}]
    assert client._anthropic_cached_messages("claude", explicit, "anthropic", None) is explicit
    assert client._anthropic_cached_messages("claude", messages, "anthropic", "https://custom.example") is messages
    assert client._anthropic_cached_messages("gemini", messages, "gemini", None) is messages


@pytest.mark.asyncio
async def test_revoked_credential_resolution_prevents_any_provider_io(monkeypatch):
    async def resolve(ref):
        raise ProviderSubscriptionError("subscription_auth_required", 502)
    monkeypatch.setattr(credentials, "resolve_inference_credential", resolve)
    async with http_fixture({"body": {}}) as fixture:
        await connect_runtime(monkeypatch, fixture)
        with pytest.raises(ProviderSubscriptionError):
            await client.acompletion("gpt", [], custom_llm_provider="openai", provider_auth=auth())
        assert fixture.requests == []


@pytest.mark.asyncio
async def test_native_api_key_responses_preserves_prompt_cache_key_and_false_options(monkeypatch):
    async def resolve(ref):
        return {"api_key": "api-material", "_fingerprint": "fp"}
    monkeypatch.setattr(credentials, "resolve_inference_credential", resolve)
    async with http_fixture({"body": {"id": "r", "object": "response", "status": "completed", "output": []}}) as fixture:
        await connect_runtime(monkeypatch, fixture)
        response = await client.aresponses(model="gpt", input=[], stream=False, api_base=None, api_key=None,
            custom_llm_provider="openai", provider_auth=auth(), prompt_cache_key="stable-key", parallel_tool_calls=False)
        assert response.status == "completed"
        body = fixture.requests[0]["body"]
        assert body["api_key"] == "api-material"
        assert body["body"]["prompt_cache_key"] == "stable-key"
        assert body["body"]["parallel_tool_calls"] is False


@pytest.mark.asyncio
async def test_chatgpt_rejection_marks_only_selected_fingerprint_without_retry(monkeypatch):
    marks = []
    async def resolve(ref):
        return {"access_token": "bearer-secret", "account_id": "a", "_fingerprint": "request-fingerprint"}
    async def mark(ref, fingerprint):
        marks.append((ref, fingerprint))
    monkeypatch.setattr(credentials, "resolve_inference_credential", resolve)
    monkeypatch.setattr(credentials, "mark_inference_credential_rejected", mark)
    async with http_fixture({"status": 401, "body": {"error": "bearer-secret"}}) as fixture:
        monkeypatch.setattr(chatgpt_transport, "_CHATGPT_RESPONSES_URL", fixture.url + "/responses")
        with pytest.raises(ProviderSubscriptionError) as raised:
            await client.acompletion("chatgpt/gpt", [], custom_llm_provider="chatgpt", provider_auth=auth(mode="chatgpt_device"))
        assert raised.value.code == "subscription_auth_required"
        assert "bearer-secret" not in str(raised.value)
        assert marks == [(auth(mode="chatgpt_device"), "request-fingerprint")]
        assert len(fixture.requests) == 1


@pytest.mark.asyncio
async def test_chatgpt_stream_bridges_tool_delta_citations_and_reasoning_native_items(monkeypatch):
    events = [
        {"type": "response.output_item.added", "item": {"type": "function_call", "id": "fc", "call_id": "call", "name": "lookup", "arguments": ""}},
        {"type": "response.function_call_arguments.delta", "item_id": "fc", "delta": "{}"},
        {"type": "response.reasoning_summary_text.delta", "delta": "reason"},
        {"type": "response.output_item.done", "item": {"type": "reasoning", "id": "rs", "encrypted_content": "opaque", "summary": []}},
        {"type": "response.output_text.annotation.added", "annotation": {"type": "url_citation", "url": "https://example.org"}},
        {"type": "response.completed", "response": {"status": "completed", "output": [], "usage": {"input_tokens": 5, "output_tokens": 2}}},
    ]
    async with http_fixture({"body": sse(*events), "headers": {"Content-Type": "text/event-stream"}}) as fixture:
        monkeypatch.setattr(chatgpt_transport, "_CHATGPT_RESPONSES_URL", fixture.url + "/responses")
        stream = await chatgpt_transport.acompletion("chatgpt/gpt", [], credential={"access_token": "secret", "account_id": "a"},
                                                     stream=True, optional_params={})
        chunks = [chunk async for chunk in stream]
        assert chunks[0].choices[0].delta.tool_calls[0].id == "call"
        assert chunks[1].choices[0].delta.tool_calls[0].function.arguments == "{}"
        assert chunks[2].choices[0].delta.reasoning_content == "reason"
        assert chunks[3].choices[0].delta.reasoning_items[0].encrypted_content == "opaque"
        assert chunks[4].choices[0].delta.annotations[0].url == "https://example.org"
        assert chunks[5].choices[0].finish_reason == "tool_calls"
        assert chunks[6].usage.prompt_tokens == 5
        assert stream.completed
        assert stream._source._client.is_closed


@pytest.mark.parametrize("provider", ["azure", "bedrock", "vertex", "ollama", "openrouter", "deepseek", "xai",
                                      "vertex_ai", "azure_ai", "mistral", "cohere", "groq", "together", "together_ai"])
def test_all_owned_runtime_adapters_admit_single_attempt(provider):
    assert client.provider_once_supported(provider)


def test_subscription_request_maps_tool_replay_and_preserves_explicit_features():
    options = {"parallel_tool_calls": False,
               "tools": [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}, "strict": False}}],
               "tool_choice": {"type": "function", "function": {"name": "lookup"}},
               "reasoning_effort": "none", "context_management": [{"type": "compaction", "compact_threshold": 200}]}
    messages = [{"role": "assistant", "content": None, "reasoning_items": [{"type": "reasoning", "id": "rs", "encrypted_content": "opaque"}],
                 "tool_calls": [{"id": "call", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": "call", "content": "result"}]
    body = chatgpt_transport._request_body("chatgpt/gpt", messages, options)
    assert body["input"][0]["encrypted_content"] == "opaque"
    assert body["input"][1]["call_id"] == "call"
    assert body["input"][2] == {"type": "function_call_output", "call_id": "call", "output": "result"}
    assert "max_output_tokens" not in body
    assert "temperature" not in body
    assert body["parallel_tool_calls"] is False
    assert body["tools"][0]["strict"] is False
    assert body["tool_choice"] == {"type": "function", "name": "lookup"}
    assert body["context_management"] == [{"type": "compaction", "compact_threshold": 200}]


@pytest.mark.asyncio
@pytest.mark.parametrize("option,code", [("max_tokens", "subscription_output_budget_unsupported"),
    ("max_output_tokens", "subscription_output_budget_unsupported"),
    ("temperature", "subscription_sampling_unsupported")])
async def test_subscription_explicit_unsupported_constraint_rejected_before_io(monkeypatch, option, code):
    async with http_fixture({"body": {}}) as fixture:
        monkeypatch.setattr(chatgpt_transport, "_CHATGPT_RESPONSES_URL", fixture.url + "/responses")
        with pytest.raises(ProviderSubscriptionError) as raised:
            await chatgpt_transport.acompletion("chatgpt/gpt", [], credential={"access_token": "s", "account_id": "a"},
                                                stream=False, optional_params={option: 10})
        assert raised.value.code == code
        assert fixture.requests == []
    assert not client.provider_once_supported("chatgpt")


@pytest.mark.asyncio
@pytest.mark.parametrize("code,contract", [("runtime_authentication_failed", "single-attempt-v1"),
                                         ("upstream_rejected", "wrong-contract")])
async def test_runtime_local_auth_failure_never_poisons_upstream_credential(monkeypatch, code, contract):
    marks = []
    async def resolve(ref):
        return {"api_key": "valid-key", "_fingerprint": "fp"}
    async def mark(ref, fingerprint):
        marks.append((ref, fingerprint))
    monkeypatch.setattr(credentials, "resolve_inference_credential", resolve)
    monkeypatch.setattr(credentials, "mark_inference_credential_rejected", mark)
    async with http_fixture({"status": 401, "headers": {"X-Lumen-Bifrost-Contract": contract},
                             "body": {"error": {"code": code}}}) as fixture:
        await connect_runtime(monkeypatch, fixture)
        with pytest.raises(APIConnectionError) as raised:
            await client.acompletion("gpt", [], custom_llm_provider="openai", provider_auth=auth())
        assert not raised.value.known_rejection
        assert marks == []
        assert len(fixture.requests) == 1


@pytest.mark.asyncio
async def test_ambiguous_disconnect_is_never_replayed(monkeypatch):
    async with http_fixture({"disconnect": True}) as fixture:
        await connect_runtime(monkeypatch, fixture)
        with pytest.raises(APIConnectionError) as raised:
            await client.acompletion("gpt", [], custom_llm_provider="openai", api_key="key", provider_once=True)
        assert not raised.value.known_rejection
        assert len(fixture.requests) == 1


@pytest.mark.asyncio
async def test_empty_json_is_not_a_successful_completion(monkeypatch):
    async with http_fixture({"body": {}}) as fixture:
        await connect_runtime(monkeypatch, fixture)
        with pytest.raises(APIConnectionError):
            await client.acompletion("gpt", [], custom_llm_provider="openai", api_key="key")
        assert len(fixture.requests) == 1


@pytest.mark.asyncio
async def test_native_header_auth_cannot_override_selected_credential(monkeypatch):
    async with http_fixture({"body": {}}) as fixture:
        await connect_runtime(monkeypatch, fixture)
        with pytest.raises(ValueError, match="provider_headers_reserved"):
            await client.aanthropic_messages(model="claude", messages=[], max_tokens=10, stream=False,
                api_base=None, api_key=None, custom_llm_provider="anthropic", provider_auth=auth(),
                anthropic_headers={"authorization": "Bearer injected"})
        assert fixture.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [413, 415])
async def test_attested_size_and_media_rejections_remain_known(monkeypatch, status):
    async with http_fixture({"status": status, "body": {"error": {"code": "upstream_rejected"}}}) as fixture:
        await connect_runtime(monkeypatch, fixture)
        with pytest.raises(BadRequestError) as raised:
            await client.acompletion("gpt", [], custom_llm_provider="openai", api_key="key")
        assert raised.value.status_code == status
        assert raised.value.known_rejection
        assert len(fixture.requests) == 1


@pytest.mark.asyncio
async def test_chatgpt_structured_output_flattens_chat_schema_on_actual_wire(monkeypatch):
    schema = {"type": "object", "properties": {"answer": {"type": "string"}},
              "required": ["answer"], "additionalProperties": False}
    completed = {"type": "response.completed", "response": {"status": "completed", "output": [
        {"type": "message", "content": [{"type": "output_text", "text": '{"answer":"yes"}'}]}]}}
    async with http_fixture({"body": sse(completed), "headers": {"Content-Type": "text/event-stream"}}) as fixture:
        monkeypatch.setattr(chatgpt_transport, "_CHATGPT_RESPONSES_URL", fixture.url + "/responses")
        response_format = {"type": "json_schema", "json_schema": {"name": "answer", "schema": schema, "strict": True}}
        result = await chatgpt_transport.acompletion("chatgpt/gpt", [{"role": "user", "content": "answer"}],
            credential={"access_token": "secret", "account_id": "a"}, stream=False,
            optional_params={"response_format": response_format, "text": {"verbosity": "low"}})
        assert result.choices[0].message.content == '{"answer":"yes"}'
        assert fixture.requests[0]["body"]["text"] == {
            "verbosity": "low", "format": {"type": "json_schema", "name": "answer", "schema": schema, "strict": True}}
        assert "response_format" not in fixture.requests[0]["body"]
        assert response_format["json_schema"]["schema"] == schema
        assert len(fixture.requests) == 1
