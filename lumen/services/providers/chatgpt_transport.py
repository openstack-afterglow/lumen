"""Request-local ChatGPT subscription transport built on LiteLLM Responses transforms."""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import httpx
from litellm.completion_extras.litellm_responses_transformation.handler import (
    ResponsesToCompletionBridgeHandler,
)
from litellm.litellm_core_utils.streaming_handler import CustomStreamWrapper
from litellm.llms.chatgpt.common_utils import (
    CHATGPT_API_BASE,
    get_chatgpt_default_headers,
)
from litellm.llms.chatgpt.responses.transformation import ChatGPTResponsesAPIConfig
from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler
from litellm.llms.custom_httpx.llm_http_handler import BaseLLMHTTPHandler
from litellm.llms.openai.responses.transformation import OpenAIResponsesAPIConfig
from litellm.types.llms.openai import ResponsesAPIStreamEvents
from litellm.types.router import GenericLiteLLMParams
from litellm.types.utils import Choices, Message, ModelResponse

from .credentials import canonical_subscription_model_name, litellm_model_name
from .errors import ProviderSubscriptionError
from .subscription_logging import SubscriptionLogging

_CHATGPT_RESPONSES_URL = f"{CHATGPT_API_BASE}/responses"

_REQUEST_TIMEOUT = httpx.Timeout(90.0, connect=10.0, read=90.0, write=30.0, pool=10.0)
_RESPONSE_KEYS = frozenset(
    {
        "instructions",
        "stream",
        "include",
        "tools",
        "tool_choice",
        "reasoning",
        "previous_response_id",
        "truncation",
    }
)
_FAILURE_EVENTS = frozenset(
    {
        ResponsesAPIStreamEvents.RESPONSE_FAILED,
        ResponsesAPIStreamEvents.RESPONSE_INCOMPLETE,
        ResponsesAPIStreamEvents.ERROR,
    }
)


class StoredChatGPTResponsesConfig(ChatGPTResponsesAPIConfig):
    """ChatGPT Responses config backed only by a request-local credential."""

    def __init__(self, *, access_token: str, account_id: str, session_id: str) -> None:
        OpenAIResponsesAPIConfig.__init__(self)
        self._access_token = access_token
        self._account_id = account_id
        self._session_id = session_id

    def validate_environment(
        self,
        headers: dict,
        model: str,
        litellm_params: GenericLiteLLMParams | None,
    ) -> dict:
        del headers, model, litellm_params
        return get_chatgpt_default_headers(
            self._access_token,
            self._account_id,
            self._session_id,
        )

    def get_complete_url(self, api_base: str | None, litellm_params: dict) -> str:
        del api_base, litellm_params
        return _CHATGPT_RESPONSES_URL


class _StrictAsyncHTTPHandler(AsyncHTTPHandler):
    def create_client(
        self,
        timeout: float | httpx.Timeout | None,
        event_hooks: Any,
        ssl_verify: Any = None,
        shared_session: Any = None,
    ) -> httpx.AsyncClient:
        client = super().create_client(
            timeout=timeout,
            event_hooks=event_hooks,
            ssl_verify=True,
            shared_session=shared_session,
        )
        client.follow_redirects = False
        return client


async def _close(value: Any) -> None:
    close = getattr(value, "aclose", None)
    if close is None:
        close = getattr(value, "close", None)
    if close is None:
        return
    result = close()
    if inspect.isawaitable(result):
        await result


def _subscription_error(error: BaseException) -> ProviderSubscriptionError:
    status_code = getattr(error, "status_code", None)
    if status_code is None:
        response = getattr(error, "response", None)
        status_code = getattr(response, "status_code", None)
    if status_code in {401, 403}:
        return ProviderSubscriptionError("subscription_auth_required", 502)
    if status_code == 429:
        return ProviderSubscriptionError("subscription_rate_limited", 429)
    if isinstance(error, (httpx.TimeoutException, httpx.NetworkError)):
        return ProviderSubscriptionError("subscription_upstream_unavailable", 503)
    if isinstance(status_code, int) and status_code >= 500:
        return ProviderSubscriptionError("subscription_upstream_unavailable", 503)
    return ProviderSubscriptionError("subscription_auth_invalid_response", 502)


class SuccessfulResponsesStream:
    """Reject every stream that does not prove a completed Responses result."""

    def __init__(self, source: AsyncIterator[Any]) -> None:
        self._source = source
        self._completed = False
        self._closed = False

    def __aiter__(self) -> SuccessfulResponsesStream:
        return self

    async def __anext__(self) -> Any:
        try:
            event = await self._source.__anext__()
        except StopAsyncIteration:
            await self.aclose()
            if not self._completed:
                raise ProviderSubscriptionError("subscription_auth_invalid_response", 502) from None
            raise
        except ProviderSubscriptionError:
            await self.aclose()
            raise
        except BaseException as error:
            await self.aclose()
            raise _subscription_error(error) from None

        event_type = getattr(event, "type", None)
        if event_type in _FAILURE_EVENTS:
            await self.aclose()
            raise ProviderSubscriptionError("subscription_auth_invalid_response", 502)
        if event_type == ResponsesAPIStreamEvents.RESPONSE_COMPLETED:
            response = getattr(event, "response", None)
            if getattr(response, "status", None) != "completed":
                await self.aclose()
                raise ProviderSubscriptionError("subscription_auth_invalid_response", 502)
            self._completed = True
        return event

    @property
    def completed_response(self) -> Any:
        return getattr(self._source, "completed_response", None) if self._completed else None

    @property
    def _hidden_params(self) -> dict[str, Any]:
        value = getattr(self._source, "_hidden_params", None)
        return value if isinstance(value, dict) else {}

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        response = getattr(self._source, "response", None)
        await _close(response)
        await _close(self._source)


class _OwnedCompletionIterator:
    def __init__(self, source: Any, guarded: SuccessfulResponsesStream, client: AsyncHTTPHandler) -> None:
        self._source = source
        self._guarded = guarded
        self._client = client
        self._closed = False
        self._source.__aiter__()

    def __aiter__(self) -> _OwnedCompletionIterator:
        return self

    async def __anext__(self) -> Any:
        try:
            return await self._source.__anext__()
        except StopAsyncIteration:
            await self.aclose()
            raise
        except BaseException:
            await self.aclose()
            raise

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await _close(self._source)
        finally:
            try:
                await self._guarded.aclose()
            finally:
                await self._client.close()


def _merge_response_choices(response: ModelResponse) -> ModelResponse:
    choices = sorted(response.choices, key=lambda choice: choice.index)
    if len(choices) <= 1:
        return response

    content: list[str] = []
    reasoning: list[str] = []
    tool_calls: list[Any] = []
    reasoning_items: list[Any] = []
    thinking_blocks: list[Any] = []
    annotations: list[Any] = []
    finish_reason = "stop"
    for choice in choices:
        message = choice.message
        message_content = getattr(message, "content", None)
        if isinstance(message_content, str):
            content.append(message_content)
        message_reasoning = getattr(message, "reasoning_content", None)
        if isinstance(message_reasoning, str):
            reasoning.append(message_reasoning)
        choice_tool_calls = getattr(message, "tool_calls", None)
        if choice_tool_calls:
            tool_calls.extend(choice_tool_calls)
        choice_reasoning_items = getattr(message, "reasoning_items", None)
        if choice_reasoning_items:
            reasoning_items.extend(choice_reasoning_items)
        choice_thinking_blocks = getattr(message, "thinking_blocks", None)
        if choice_thinking_blocks:
            thinking_blocks.extend(choice_thinking_blocks)
        choice_annotations = getattr(message, "annotations", None)
        if choice_annotations:
            annotations.extend(choice_annotations)
        if choice.finish_reason and choice.finish_reason != "stop":
            finish_reason = choice.finish_reason

    if tool_calls:
        finish_reason = "tool_calls"
    response.choices = [
        Choices(
            index=0,
            finish_reason=finish_reason,
            message=Message(
                role="assistant",
                content="".join(content) or None,
                reasoning_content="".join(reasoning) or None,
                tool_calls=tool_calls or None,
                reasoning_items=reasoning_items or None,
                thinking_blocks=thinking_blocks or None,
                annotations=annotations or None,
            ),
        )
    ]
    return response


def _credential_value(credential: dict[str, Any], key: str) -> str:
    value = credential.get(key)
    if not isinstance(value, str) or not value:
        raise ProviderSubscriptionError("subscription_auth_required", 502)
    return value


async def acompletion(
    model: str,
    messages: list[dict],
    *,
    credential: dict,
    stream: bool,
    optional_params: dict,
) -> Any:
    """Execute one ChatGPT subscription request without process-global auth state."""
    canonical_model = litellm_model_name(
        canonical_subscription_model_name(model, "chatgpt_device")
    )
    access_token = _credential_value(credential, "access_token")
    account_id = _credential_value(credential, "account_id")
    call_id = str(uuid4())
    config = StoredChatGPTResponsesConfig(
        access_token=access_token,
        account_id=account_id,
        session_id=call_id,
    )
    logging_obj = SubscriptionLogging(
        model=canonical_model,
        provider="chatgpt",
        fixed_api_base=CHATGPT_API_BASE,
        call_id=call_id,
        stream=stream,
    )
    litellm_params = GenericLiteLLMParams(
        api_base=CHATGPT_API_BASE,
        custom_llm_provider="chatgpt",
        no_log=True,
        stream=True,
    )
    bridge = ResponsesToCompletionBridgeHandler()
    sanitized_optional_params = {
        key: value
        for key, value in optional_params.items()
        if key not in {"api_base", "api_key", "base_url", "extra_body", "extra_headers", "headers"}
    }
    sanitized_optional_params["stream"] = True
    try:
        request_data = bridge.transformation_handler.transform_request(
            model=canonical_model,
            messages=messages,
            optional_params=sanitized_optional_params,
            litellm_params=litellm_params.model_dump(exclude_none=True),
            headers={},
            litellm_logging_obj=logging_obj,
        )
    except ProviderSubscriptionError:
        raise
    except BaseException as error:
        raise _subscription_error(error) from None

    response_params = {
        key: value
        for key, value in request_data.items()
        if key in _RESPONSE_KEYS
    }
    response_params["stream"] = True
    input_items = request_data.get("input")
    if not isinstance(input_items, (str, list)):
        raise ProviderSubscriptionError("subscription_auth_invalid_response", 502)

    http_client = _StrictAsyncHTTPHandler(timeout=_REQUEST_TIMEOUT, ssl_verify=True)
    try:
        raw_stream = await BaseLLMHTTPHandler().async_response_api_handler(
            model=canonical_model,
            input=input_items,
            responses_api_provider_config=config,
            response_api_optional_request_params=response_params,
            custom_llm_provider="chatgpt",
            litellm_params=litellm_params,
            logging_obj=logging_obj,
            timeout=_REQUEST_TIMEOUT,
            client=http_client,
        )
    except BaseException as error:
        await http_client.close()
        if isinstance(error, ProviderSubscriptionError):
            raise
        raise _subscription_error(error) from None

    if not hasattr(raw_stream, "__anext__"):
        await http_client.close()
        raise ProviderSubscriptionError("subscription_auth_invalid_response", 502)

    guarded = SuccessfulResponsesStream(raw_stream)
    if not stream:
        try:
            raw_response = await bridge._collect_response_from_stream_async(guarded)
            model_response = bridge.transformation_handler.transform_response(
                model=canonical_model,
                raw_response=raw_response,
                model_response=ModelResponse(),
                logging_obj=logging_obj,
                request_data=request_data,
                messages=messages,
                optional_params={**sanitized_optional_params, "stream": False},
                litellm_params=litellm_params.model_dump(exclude_none=True),
                encoding=None,
                api_key=None,
                json_mode=None,
            )
            return _merge_response_choices(model_response)
        except ProviderSubscriptionError:
            raise
        except BaseException as error:
            raise _subscription_error(error) from None
        finally:
            await guarded.aclose()
            await http_client.close()

    completion_stream = bridge.transformation_handler.get_model_response_iterator(
        streaming_response=guarded,
        sync_stream=False,
        json_mode=None,
    )
    owned_stream = _OwnedCompletionIterator(completion_stream, guarded, http_client)
    return CustomStreamWrapper(
        completion_stream=owned_stream,
        model=canonical_model,
        custom_llm_provider="chatgpt",
        logging_obj=logging_obj,
    )
