"""Consumer-visible OAuth protocol, tool, usage and stream-lifetime regressions."""
from __future__ import annotations

import json

import httpx
import pytest

from lumen.services.provider_client import ProviderObject
from lumen.services.providers import chatgpt_transport
from lumen.services.providers.errors import ProviderSubscriptionError

pytestmark = pytest.mark.asyncio


class WireStream(httpx.AsyncByteStream):
    def __init__(self, events):
        self.events = events
        self.closed = False

    async def __aiter__(self):
        for event in self.events:
            yield f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode()

    async def aclose(self):
        self.closed = True


def wire(monkeypatch, events, *, status=200):
    requests = []
    source = WireStream(events)

    def handle(request):
        requests.append(request)
        return httpx.Response(status, headers={"content-type": "text/event-stream"}, stream=source)

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda **kwargs: httpx.MockTransport(handle))
    return requests, source


def completed(output, **overrides):
    return {"type": "response.completed", "response": {
        "id": "response-1", "model": "gpt-5.2-codex", "status": "completed", "output": output,
        "usage": {"input_tokens": 12, "output_tokens": 5,
                  "input_tokens_details": {"cached_tokens": 4}, "output_tokens_details": {"reasoning_tokens": 2}},
        **overrides,
    }}


async def invoke(*, stream=False, options=None, messages=None):
    return await chatgpt_transport.acompletion(
        "chatgpt/gpt-5.2-codex", messages or [{"role": "user", "content": "hello"}],
        credential={"access_token": "request-token", "account_id": "request-account"},
        stream=stream, optional_params=options or {},
    )


async def test_subscription_pins_request_local_auth_and_endpoint(monkeypatch, caplog):
    monkeypatch.setenv("CHATGPT_ACCESS_TOKEN", "environment-token-must-not-win")
    requests, source = wire(monkeypatch, [completed([])])
    await invoke(options={"reasoning_effort": "low"})
    [request] = requests
    body = json.loads(request.content)
    assert str(request.url) == "https://chatgpt.com/backend-api/codex/responses"
    assert request.headers["Authorization"] == "Bearer request-token"
    assert request.headers["ChatGPT-Account-Id"] == "request-account"
    assert body["store"] is False and body["stream"] is True
    assert body["reasoning"] == {"effort": "low"}
    assert "reasoning.encrypted_content" in body["include"]
    assert source.closed
    assert "request-token" not in caplog.text and "request-account" not in caplog.text
    assert "environment-token-must-not-win" not in caplog.text


@pytest.mark.parametrize("option", [
    {"api_base": "https://evil.example"}, {"extra_headers": {"Authorization": "evil"}},
    {"store": True}, {"api_key": "evil"}, {"extra_body": {"model": "other"}},
])
async def test_subscription_refuses_transport_and_auth_overrides_before_io(monkeypatch, option):
    requests, _ = wire(monkeypatch, [completed([])])
    with pytest.raises(ValueError, match="reserved"):
        await invoke(options=option)
    assert requests == []


@pytest.mark.parametrize("events", [
    [], [{"type": "response.failed"}],
    [completed([], status="incomplete")],
    [{"type": "response.output_text.delta", "delta": "partial"}],
])
@pytest.mark.parametrize("stream", [False, True])
async def test_subscription_requires_proven_completion_and_closes(monkeypatch, events, stream):
    requests, source = wire(monkeypatch, events)
    with pytest.raises(ProviderSubscriptionError):
        result = await invoke(stream=stream)
        if stream:
            async for _ in result:
                pass
    assert len(requests) == 1
    assert source.closed


async def test_nonstream_merges_all_output_items_reasoning_tools_citations_and_usage(monkeypatch):
    annotation = {"type": "url_citation", "url": "https://docs.example", "title": "Docs"}
    output = [
        {"id": "reason-1", "type": "reasoning", "encrypted_content": "sealed",
         "summary": [{"type": "summary_text", "text": "reason-one"}]},
        {"id": "message-1", "type": "message", "role": "assistant",
         "content": [{"type": "output_text", "text": "first", "annotations": [annotation]}]},
        {"id": "message-2", "type": "message", "role": "assistant",
         "content": [{"type": "output_text", "text": "second", "annotations": []}]},
        {"id": "function-1", "call_id": "call-1", "type": "function_call", "name": "lookup", "arguments": '{"key":"value"}'},
        {"id": "function-2", "call_id": "call-2", "type": "function_call", "name": "other", "arguments": "{}"},
    ]
    requests, source = wire(monkeypatch, [completed(output)])
    response = await invoke()
    assert isinstance(response, ProviderObject)
    [choice] = response.choices
    assert choice.message.content == "firstsecond"
    assert choice.message.reasoning_content == "reason-one"
    assert choice.message.reasoning_items[0].encrypted_content == "sealed"
    assert choice.message.annotations == [annotation]
    assert choice.finish_reason == "tool_calls"
    assert [call.id for call in choice.message.tool_calls] == ["call-1", "call-2"]
    assert [call.function.name for call in choice.message.tool_calls] == ["lookup", "other"]
    assert response.usage.prompt_tokens == 12 and response.usage.completion_tokens == 5
    assert response.usage.prompt_tokens_details.cached_tokens == 4
    assert response.usage.completion_tokens_details.reasoning_tokens == 2
    assert source.closed and len(requests) == 1


async def test_stream_preserves_one_tool_call_without_duplicating_done_arguments(monkeypatch):
    tool = {"id": "function-1", "call_id": "call-1", "type": "function_call", "name": "lookup", "arguments": ""}
    final_tool = {**tool, "arguments": '{"key":"value"}'}
    events = [
        {"type": "response.output_item.added", "output_index": 0, "item": tool},
        {"type": "response.function_call_arguments.delta", "item_id": "function-1", "delta": '{"key":'},
        {"type": "response.function_call_arguments.delta", "item_id": "function-1", "delta": '"value"}'},
        {"type": "response.function_call_arguments.done", "item_id": "function-1", "arguments": final_tool["arguments"]},
        {"type": "response.output_item.done", "output_index": 0, "item": final_tool},
        completed([final_tool]),
    ]
    requests, source = wire(monkeypatch, events)
    stream = await invoke(stream=True)
    chunks = [chunk async for chunk in stream]
    tools = [call for chunk in chunks for choice in chunk.choices for call in choice.delta.get("tool_calls", [])]
    assert [call.id for call in tools if call.get("id")] == ["call-1"]
    assert [call.function.name for call in tools if call.function.get("name")] == ["lookup"]
    assert "".join(call.function.get("arguments", "") for call in tools) == '{"key":"value"}'
    assert [chunk.usage.prompt_tokens for chunk in chunks if chunk.usage] == [12]
    assert source.closed and stream.completed and len(requests) == 1


async def test_tool_continuation_retains_call_identity_and_encrypted_reasoning(monkeypatch):
    requests, _ = wire(monkeypatch, [completed([])])
    reasoning = {"id": "reason-1", "type": "reasoning", "encrypted_content": "sealed"}
    await invoke(messages=[
        {"role": "assistant", "content": None, "reasoning_items": [reasoning],
         "tool_calls": [{"id": "call-1", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call-1", "content": "result"},
    ])
    items = json.loads(requests[0].content)["input"]
    assert items[0] == reasoning
    assert items[1] == {"type": "function_call", "call_id": "call-1", "name": "lookup", "arguments": "{}"}
    assert items[2] == {"type": "function_call_output", "call_id": "call-1", "output": "result"}


async def test_unread_subscription_stream_can_be_explicitly_closed(monkeypatch):
    _, source = wire(monkeypatch, [completed([])])
    stream = await invoke(stream=True)
    await stream.aclose()
    assert source.closed
