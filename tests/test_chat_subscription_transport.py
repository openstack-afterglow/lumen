from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import litellm
import pytest
from litellm.types.llms.openai import (
    BaseLiteLLMOpenAIResponseObject,
    FunctionCallArgumentsDeltaEvent,
    FunctionCallArgumentsDoneEvent,
    OutputItemAddedEvent,
    OutputItemDoneEvent,
    ResponseCompletedEvent,
    ResponsesAPIResponse,
    ResponsesAPIStreamEvents,
)
from litellm.types.router import GenericLiteLLMParams
from litellm.types.utils import Choices, Message, ModelResponse

from lumen.services.providers import chatgpt_transport
from lumen.services.providers.chatgpt_transport import (
    StoredChatGPTResponsesConfig,
    SuccessfulResponsesStream,
    _merge_response_choices,
    acompletion,
)
from lumen.services.providers.errors import ProviderSubscriptionError
from lumen.services.providers.subscription_logging import SubscriptionLogging


def _serialized_state(logger: SubscriptionLogging) -> str:
    safe = {
        "model": logger.model,
        "messages": logger.messages,
        "optional_params": logger.optional_params,
        "model_call_details": logger.model_call_details,
        "stream_options": logger.stream_options,
    }
    return json.dumps(safe, default=str, sort_keys=True)


@pytest.mark.asyncio
async def test_subscription_logger_never_retains_or_dispatches_raw_secrets(monkeypatch):
    secret = "sk-ant-oat01-never-store-this-token"
    account = "account-never-store"
    callback_calls = []
    monkeypatch.setattr(litellm, "error_logs", {})
    monkeypatch.setattr(litellm, "success_callback", [lambda *args, **kwargs: callback_calls.append("sync")])
    monkeypatch.setattr(litellm, "_async_success_callback", [lambda *args, **kwargs: callback_calls.append("async")])
    logger = SubscriptionLogging(
        model="claude-opus-4-1",
        provider="anthropic",
        fixed_api_base="https://api.anthropic.com",
        call_id="call-1",
        stream=True,
    )

    logger.update_from_kwargs(
        kwargs={"api_key": secret, "extra_headers": {"ChatGPT-Account-Id": account}},
        litellm_params={"api_key": secret},
        optional_params={"metadata": {"secret": secret}},
        stream_options={"include_usage": True, "secret": secret},
    )
    logger.pre_call(
        input=[{"role": "user", "content": secret}],
        api_key=secret,
        additional_args={"headers": {"Authorization": f"Bearer {secret}", "ChatGPT-Account-Id": account}},
    )
    logger.post_call(
        original_response={"authorization": secret, "usage": {"prompt_tokens": 2, "completion_tokens": 3}},
        api_key=secret,
    )
    await logger.dispatch_success_handlers(
        result={"content": secret, "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}},
        end_time=datetime.now(UTC),
    )
    logger.failure_handler(RuntimeError(secret), secret)
    await logger.async_failure_handler(RuntimeError(account), account)

    serialized = _serialized_state(logger)
    assert secret not in serialized
    assert account not in serialized
    assert callback_calls == []
    assert litellm.error_logs == {}
    assert logger.model_call_details["litellm_params"] == {
        "no_log": True,
        "api_base": "https://api.anthropic.com",
        "custom_llm_provider": "anthropic",
    }
    assert logger.model_call_details["status"] == "failed"
    assert logger.model_call_details["usage"] == {
        "prompt_tokens": 2,
        "completion_tokens": 3,
        "total_tokens": 5,
    }


def test_subscription_logger_discards_sdk_raw_detail_assignments():
    logger = SubscriptionLogging(
        model="gpt-5.2-codex",
        provider="chatgpt",
        fixed_api_base="https://chatgpt.com/backend-api/codex",
        call_id="call-2",
        stream=False,
    )

    logger.model_call_details["httpx_response"] = {"headers": {"Authorization": "Bearer secret"}}
    logger.model_call_details["response_headers"] = {"ChatGPT-Account-Id": "account-secret"}
    logger.model_call_details["messages"] = [{"content": "secret"}]
    logger.update_messages([{"role": "user", "content": "secret"}])

    serialized = _serialized_state(logger)
    assert "secret" not in serialized
    assert "httpx_response" not in logger.model_call_details
    assert "response_headers" not in logger.model_call_details
    assert "messages" not in logger.model_call_details


class _FakeResponse:
    def __init__(self):
        self.closed = False

    async def aclose(self):
        self.closed = True


class _FakeResponsesStream:
    def __init__(self, events):
        self.events = list(events)
        self.response = _FakeResponse()
        self.closed = False
        self.completed_response = None
        self._hidden_params = {"custom_llm_provider": "chatgpt"}

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.events:
            raise StopAsyncIteration
        event = self.events.pop(0)
        if getattr(event, "type", None) == ResponsesAPIStreamEvents.RESPONSE_COMPLETED:
            self.completed_response = event
        return event

    async def aclose(self):
        self.closed = True


def test_stored_chatgpt_config_pins_auth_endpoint_and_request_shape(monkeypatch):
    monkeypatch.setenv("CHATGPT_ACCESS_TOKEN", "environment-token-must-not-win")
    config = StoredChatGPTResponsesConfig(
        access_token="request-token",
        account_id="request-account",
        session_id="request-session",
    )
    params = GenericLiteLLMParams(api_base="https://evil.example", api_key="evil-key")

    headers = config.validate_environment(
        headers={
            "Authorization": "Bearer evil-token",
            "ChatGPT-Account-Id": "evil-account",
        },
        model="gpt-5.2-codex",
        litellm_params=params,
    )
    request = config.transform_responses_api_request(
        model="gpt-5.2-codex",
        input=[{"role": "user", "content": "hello"}],
        response_api_optional_request_params={
            "stream": False,
            "store": True,
            "temperature": 1,
            "max_output_tokens": 1,
        },
        litellm_params=params,
        headers=headers,
    )

    assert not hasattr(config, "authenticator")
    assert config.get_complete_url("https://evil.example", {}) == (
        "https://chatgpt.com/backend-api/codex/responses"
    )
    assert headers["Authorization"] == "Bearer request-token"
    assert headers["ChatGPT-Account-Id"] == "request-account"
    assert headers["session_id"] == "request-session"
    assert request["stream"] is True
    assert request["store"] is False
    assert "temperature" not in request
    assert "max_output_tokens" not in request
    assert set(request) <= {
        "model",
        "input",
        "instructions",
        "stream",
        "store",
        "include",
        "tools",
        "tool_choice",
        "reasoning",
        "previous_response_id",
        "truncation",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "events",
    [
        [],
        [SimpleNamespace(type=ResponsesAPIStreamEvents.RESPONSE_FAILED)],
        [
            SimpleNamespace(
                type=ResponsesAPIStreamEvents.RESPONSE_COMPLETED,
                response=SimpleNamespace(status="incomplete"),
            )
        ],
    ],
)
async def test_successful_responses_stream_rejects_unproven_completion(events):
    source = _FakeResponsesStream(events)
    stream = SuccessfulResponsesStream(source)

    with pytest.raises(ProviderSubscriptionError) as exc_info:
        async for _ in stream:
            pass

    assert exc_info.value.code == "subscription_auth_invalid_response"
    assert source.closed is True
    assert source.response.closed is True


@pytest.mark.asyncio
async def test_successful_responses_stream_accepts_only_completed_status_and_closes():
    completed = SimpleNamespace(
        type=ResponsesAPIStreamEvents.RESPONSE_COMPLETED,
        response=SimpleNamespace(status="completed"),
    )
    source = _FakeResponsesStream([completed])
    stream = SuccessfulResponsesStream(source)

    assert [event async for event in stream] == [completed]
    assert stream.completed_response is completed
    assert stream._hidden_params == {"custom_llm_provider": "chatgpt"}
    assert source.closed is True
    assert source.response.closed is True


def test_merge_response_choices_preserves_all_text_reasoning_and_tool_calls():
    response = ModelResponse(
        choices=[
            Choices(
                index=2,
                finish_reason="tool_calls",
                message=Message(
                    content="second",
                    reasoning_content="reason-two",
                    tool_calls=[
                        {
                            "id": "call-2",
                            "type": "function",
                            "function": {"name": "two", "arguments": "{}"},
                        }
                    ],
                ),
            ),
            Choices(
                index=0,
                finish_reason="stop",
                message=Message(
                    content="first",
                    reasoning_content="reason-one",
                    tool_calls=[
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "one", "arguments": "{\"value\":1}"},
                        }
                    ],
                ),
            ),
        ]
    )

    merged = _merge_response_choices(response)

    assert len(merged.choices) == 1
    assert merged.choices[0].finish_reason == "tool_calls"
    assert merged.choices[0].message.content == "firstsecond"
    assert merged.choices[0].message.reasoning_content == "reason-onereason-two"
    assert [call.id for call in merged.choices[0].message.tool_calls] == ["call-1", "call-2"]
    assert [call.function.name for call in merged.choices[0].message.tool_calls] == ["one", "two"]


def _completed_response(output):
    return ResponsesAPIResponse(
        id="response-1",
        created_at=1,
        model="gpt-5.2-codex",
        output=output,
        status="completed",
    )


@pytest.mark.asyncio
async def test_acompletion_collects_multi_item_native_stream(monkeypatch):
    completed = ResponseCompletedEvent(
        type=ResponsesAPIStreamEvents.RESPONSE_COMPLETED,
        response=_completed_response(
            [
                {
                    "id": "message-1",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": "first", "annotations": []}],
                },
                {
                    "id": "message-2",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": "second", "annotations": []}],
                },
                {
                    "id": "function-1",
                    "call_id": "call-1",
                    "type": "function_call",
                    "name": "lookup",
                    "arguments": "{\"key\":\"value\"}",
                    "status": "completed",
                },
            ]
        ),
    )
    source = _FakeResponsesStream([completed])
    captured = {}

    async def fake_response_handler(_self, **kwargs):
        captured.update(kwargs)
        return source

    monkeypatch.setattr(
        chatgpt_transport.BaseLLMHTTPHandler,
        "async_response_api_handler",
        fake_response_handler,
    )

    response = await acompletion(
        "chatgpt/gpt-5.2-codex",
        [{"role": "user", "content": "hello"}],
        credential={"access_token": "request-token", "account_id": "request-account"},
        stream=False,
        optional_params={"temperature": 0.2, "extra_headers": {"Authorization": "evil"}},
    )

    assert captured["response_api_optional_request_params"]["stream"] is True
    assert "temperature" not in captured["response_api_optional_request_params"]
    assert "extra_headers" not in captured["response_api_optional_request_params"]
    assert len(response.choices) == 1
    assert response.choices[0].message.content == "firstsecond"
    assert response.choices[0].finish_reason == "tool_calls"
    assert [call.id for call in response.choices[0].message.tool_calls] == ["call-1"]
    assert source.closed is True
    assert source.response.closed is True


@pytest.mark.asyncio
async def test_acompletion_stream_preserves_single_tool_call_and_closes(monkeypatch):
    tool_started = BaseLiteLLMOpenAIResponseObject(
        id="function-1",
        call_id="call-1",
        type="function_call",
        name="lookup",
        arguments="",
        status="in_progress",
    )
    tool_finished = BaseLiteLLMOpenAIResponseObject(
        id="function-1",
        call_id="call-1",
        type="function_call",
        name="lookup",
        arguments="{\"key\":\"value\"}",
        status="completed",
    )
    completed_response = _completed_response([tool_finished.model_dump()])
    source = _FakeResponsesStream(
        [
            OutputItemAddedEvent(
                type=ResponsesAPIStreamEvents.OUTPUT_ITEM_ADDED,
                output_index=0,
                item=tool_started,
            ),
            FunctionCallArgumentsDeltaEvent(
                type=ResponsesAPIStreamEvents.FUNCTION_CALL_ARGUMENTS_DELTA,
                item_id="function-1",
                output_index=0,
                delta="{\"key\":",
            ),
            FunctionCallArgumentsDeltaEvent(
                type=ResponsesAPIStreamEvents.FUNCTION_CALL_ARGUMENTS_DELTA,
                item_id="function-1",
                output_index=0,
                delta="\"value\"}",
            ),
            FunctionCallArgumentsDoneEvent(
                type=ResponsesAPIStreamEvents.FUNCTION_CALL_ARGUMENTS_DONE,
                item_id="function-1",
                output_index=0,
                arguments="{\"key\":\"value\"}",
            ),
            OutputItemDoneEvent(
                type=ResponsesAPIStreamEvents.OUTPUT_ITEM_DONE,
                output_index=0,
                item=tool_finished,
            ),
            ResponseCompletedEvent(
                type=ResponsesAPIStreamEvents.RESPONSE_COMPLETED,
                response=completed_response,
            ),
        ]
    )

    async def fake_response_handler(_self, **kwargs):
        return source

    monkeypatch.setattr(
        chatgpt_transport.BaseLLMHTTPHandler,
        "async_response_api_handler",
        fake_response_handler,
    )

    stream = await acompletion(
        "gpt-5.2-codex",
        [{"role": "user", "content": "hello"}],
        credential={"access_token": "request-token", "account_id": "request-account"},
        stream=True,
        optional_params={},
    )
    chunks = [chunk async for chunk in stream]
    tool_calls = [
        tool_call
        for chunk in chunks
        for choice in chunk.choices
        for tool_call in (choice.delta.tool_calls or [])
    ]

    assert [call.id for call in tool_calls if call.id] == ["call-1"]
    assert [call.function.name for call in tool_calls if call.function and call.function.name] == ["lookup"]
    assert "".join(
        call.function.arguments or ""
        for call in tool_calls
        if call.function is not None
    ) == "{\"key\":\"value\"}"
    assert source.closed is True
    assert source.response.closed is True
