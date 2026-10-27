"""Pinned-target, request-local ChatGPT OAuth transport with a native Responses bridge.

No environment auth, global SDK callbacks, redirects, retries, or on-disk token
files are involved. A completed response event is required even for nonstreaming
callers, because the Codex endpoint always returns SSE.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from typing import Any
from uuid import uuid4

import httpx

from lumen.services.provider_client import ProviderObject, ProviderStream, _mark_rejected, _subscription_error
from lumen.services.provider_errors import APIConnectionError, ProviderError, Timeout, error_for_status

from .credentials import ProviderAuthRef, canonical_subscription_model_name, transport_model_name
from .errors import ProviderSubscriptionError

_CHATGPT_RESPONSES_URL = "https://chatgpt.com/backend-api/codex/responses"
_REQUEST_TIMEOUT = httpx.Timeout(90.0, connect=10.0, read=90.0, write=30.0, pool=10.0)
_BLOCKED = frozenset({"api_base", "api_key", "base_url", "extra_body", "extra_headers", "headers",
                      "model", "input", "messages", "provider_auth", "stream", "store", "stream_options"})


def _input_content(content: Any) -> Any:
    if not isinstance(content, list):
        return content
    converted = []
    for part in content:
        if not isinstance(part, Mapping):
            raise ValueError("invalid_chat_content")
        kind = part.get("type")
        if kind == "text":
            converted.append({**part, "type": "input_text"})
        elif kind == "image_url":
            image = part.get("image_url")
            if isinstance(image, str):
                converted.append({"type": "input_image", "image_url": image})
            elif isinstance(image, Mapping):
                converted.append({"type": "input_image", **image, "image_url": image.get("url")})
                converted[-1].pop("url", None)
            else:
                raise ValueError("invalid_chat_image")
        elif kind in {"input_text", "input_image", "input_file", "input_audio"}:
            converted.append(dict(part))
        else:
            raise ValueError("subscription_content_unsupported")
    return converted


def _request_body(model: str, messages: list[dict], options: dict) -> dict[str, Any]:
    if _BLOCKED.intersection(options) - {"stream_options"}:
        raise ValueError("subscription_transport_options_reserved")
    body = {key: value for key, value in options.items() if key not in _BLOCKED}
    if any(options.get(key) is not None for key in ("max_tokens", "max_completion_tokens", "max_output_tokens")):
        raise ProviderSubscriptionError("subscription_output_budget_unsupported", 422)
    if any(options.get(key) is not None for key in ("temperature", "top_p", "top_k")):
        raise ProviderSubscriptionError("subscription_sampling_unsupported", 422)
    if "reasoning_effort" in body:
        body["reasoning"] = {**body.get("reasoning", {}), "effort": body.pop("reasoning_effort")}
    if "response_format" in body:
        response_format = body.pop("response_format")
        if isinstance(response_format, Mapping) and response_format.get("type") == "json_schema":
            schema = response_format.get("json_schema")
            if not isinstance(schema, Mapping):
                raise ValueError("invalid_response_format")
            response_format = {**schema, "type": "json_schema"}
        body["text"] = {**body.get("text", {}), "format": response_format}
    tools = body.get("tools")
    if tools is not None:
        body["tools"] = [({"type": "function", **tool["function"]} if tool.get("type") == "function" and "function" in tool
                          else dict(tool)) for tool in tools]
    choice = body.get("tool_choice")
    if isinstance(choice, Mapping) and choice.get("type") == "function" and "function" in choice:
        body["tool_choice"] = {"type": "function", "name": choice["function"]["name"]}
    if "web_search_options" in body:
        body["tools"] = [*body.get("tools", []), {"type": "web_search", **body.pop("web_search_options")}]
    items: list[dict] = []
    instructions: list[str] = []
    for message in messages:
        role = message.get("role")
        content = message.get("content")
        if role in {"system", "developer"}:
            if isinstance(content, str):
                instructions.append(content)
            elif isinstance(content, list) and all(part.get("type") == "text" for part in content):
                instructions.extend(part["text"] for part in content)
            else:
                raise ValueError("subscription_instructions_unsupported")
            continue
        if role == "tool":
            items.append({"type": "function_call_output", "call_id": message["tool_call_id"], "output": content})
            continue
        reasoning_items = message.get("reasoning_items") or []
        items.extend(dict(item) for item in reasoning_items)
        if content is not None:
            if role == "assistant" and isinstance(content, list):
                converted = [{**part, "type": "output_text"} if part.get("type") == "text" else dict(part) for part in content]
            else:
                converted = _input_content(content)
            items.append({"role": role, "content": converted})
        for call in message.get("tool_calls") or []:
            function = call["function"]
            items.append({"type": "function_call", "call_id": call["id"], "name": function["name"],
                          "arguments": function.get("arguments", "")})
    if body.get("instructions") is not None:
        instructions.append(body["instructions"])
    body.update(model=transport_model_name(canonical_subscription_model_name(model, "chatgpt_device")),
                input=items, instructions="\n\n".join(instructions) or "You are a helpful assistant.",
                store=False, stream=True)
    body["include"] = list(dict.fromkeys([*body.get("include", []), "reasoning.encrypted_content"]))
    return body


def _usage(usage: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if usage is None:
        return None
    # Retain native details and explicit unknown-vs-zero; never manufacture cache
    # or modality counters when the endpoint did not report them.
    result = dict(usage)
    if "input_tokens" in usage:
        result["prompt_tokens"] = usage["input_tokens"]
    if "output_tokens" in usage:
        result["completion_tokens"] = usage["output_tokens"]
    if "input_tokens_details" in usage:
        result["prompt_tokens_details"] = usage["input_tokens_details"]
    if "output_tokens_details" in usage:
        result["completion_tokens_details"] = usage["output_tokens_details"]
    return result


def _completed_chat(response: Mapping[str, Any], model: str) -> ProviderObject:
    content: list[str] = []
    reasoning: list[str] = []
    tools: list[dict] = []
    reasoning_items: list[dict] = []
    annotations: list[dict] = []
    for item in response.get("output", []):
        if item.get("type") == "message":
            for part in item.get("content", []):
                if part.get("type") == "output_text":
                    content.append(part.get("text", ""))
                    annotations.extend(part.get("annotations", []))
                elif part.get("type") == "refusal":
                    content.append(part.get("refusal", ""))
        elif item.get("type") == "function_call":
            tools.append({"id": item.get("call_id", item.get("id")), "type": "function",
                          "function": {"name": item.get("name"), "arguments": item.get("arguments", "")}})
        elif item.get("type") == "reasoning":
            reasoning_items.append(dict(item))
            reasoning.extend(part.get("text", "") for part in item.get("summary", []))
    message = {"role": "assistant", "content": "".join(content) or None,
               "tool_calls": tools or None, "reasoning_content": "".join(reasoning) or None,
               "reasoning_items": reasoning_items or None, "annotations": annotations or None}
    return ProviderObject(id=response.get("id"), object="chat.completion", model=response.get("model", model),
                          choices=[{"index": 0, "message": message, "finish_reason": "tool_calls" if tools else "stop"}],
                          usage=_usage(response.get("usage")))


class ChatCompletionStream:
    def __init__(self, source: ProviderStream, model: str) -> None:
        self._source = source
        self._model = model
        self._pending: deque[ProviderObject] = deque()
        self._tools: dict[str, int] = {}

    @property
    def completed(self) -> bool:
        return self._source.completed

    def __aiter__(self) -> ChatCompletionStream:
        return self

    def _chunk(self, delta: dict, finish: str | None = None, usage: Any = None) -> ProviderObject:
        return ProviderObject(object="chat.completion.chunk", model=self._model,
                              choices=[{"index": 0, "delta": delta, "finish_reason": finish}], usage=usage)

    async def __anext__(self) -> ProviderObject:
        try:
            while not self._pending:
                event = await self._source.__anext__()
                kind = event.get("type")
                if kind == "response.output_text.delta":
                    return self._chunk({"content": event.get("delta", "")})
                if kind in {"response.reasoning_summary_text.delta", "response.reasoning_text.delta"}:
                    return self._chunk({"reasoning_content": event.get("delta", "")})
                if kind == "response.output_text.annotation.added":
                    return self._chunk({"annotations": [event["annotation"]]})
                if kind == "response.output_item.added" and event.get("item", {}).get("type") == "function_call":
                    item = event["item"]
                    key = item["id"]
                    self._tools[key] = len(self._tools)
                    return self._chunk({"tool_calls": [{"index": self._tools[key], "id": item.get("call_id", key),
                                                       "type": "function", "function": {"name": item["name"], "arguments": item.get("arguments", "")}}]})
                if kind == "response.function_call_arguments.delta":
                    key = event["item_id"]
                    if key not in self._tools:
                        raise APIConnectionError()
                    return self._chunk({"tool_calls": [{"index": self._tools[key], "function": {"arguments": event.get("delta", "")}}]})
                if kind == "response.output_item.done" and event.get("item", {}).get("type") == "reasoning":
                    return self._chunk({"reasoning_items": [event["item"]]})
                if kind == "response.completed":
                    response = event["response"]
                    self._pending.append(self._chunk({}, "tool_calls" if self._tools else "stop"))
                    if response.get("usage") is not None:
                        self._pending.append(ProviderObject(object="chat.completion.chunk", model=self._model,
                                                            choices=[], usage=_usage(response["usage"])))
                if kind == "response.incomplete":
                    raise APIConnectionError()
            return self._pending.popleft()
        except ProviderError as exc:
            await self.aclose()
            raise _subscription_error(exc) from None
        except BaseException:
            await self.aclose()
            raise

    async def aclose(self) -> None:
        await self._source.aclose()


async def acompletion(
    model: str, messages: list[dict], *, credential: dict | None, stream: bool,
    optional_params: dict, single_attempt: bool = False, provider_auth: ProviderAuthRef | None = None,
) -> Any:
    del single_attempt  # Every call is single-attempt, not just durable calls.
    body = _request_body(model, messages, optional_params)
    client = httpx.AsyncClient(timeout=_REQUEST_TIMEOUT, follow_redirects=False, trust_env=False,
                               transport=httpx.AsyncHTTPTransport(retries=0))
    response: httpx.Response | None = None
    source: ProviderStream | None = None
    fingerprint: str | None = None
    try:
        if provider_auth is not None:
            from .credentials import resolve_inference_credential

            credential = await resolve_inference_credential(provider_auth)
        if not isinstance(credential, dict):
            raise ProviderSubscriptionError("subscription_auth_required", 502)
        access_token, account_id = credential.get("access_token"), credential.get("account_id")
        fingerprint = credential.get("_fingerprint")
        if not isinstance(access_token, str) or not access_token or not isinstance(account_id, str) or not account_id:
            raise ProviderSubscriptionError("subscription_auth_required", 502)
        headers = {"Authorization": f"Bearer {access_token}", "ChatGPT-Account-Id": account_id,
                   "Content-Type": "application/json", "Accept": "text/event-stream",
                   "originator": "codex_cli_rs", "User-Agent": "lumen-subscription/1", "session_id": str(uuid4())}
        request = client.build_request("POST", _CHATGPT_RESPONSES_URL, json=body, headers=headers)
        response = await client.send(request, stream=True)
        from .relay_history import record_dispatch
        record_dispatch(provider_auth, endpoint=_CHATGPT_RESPONSES_URL, model=body.get("model"), operation="responses", status_code=response.status_code)
        if response.status_code >= 300:
            if response.status_code in {401, 403}:
                await _mark_rejected(provider_auth, fingerprint)
            raise error_for_status(response.status_code)
        if "text/event-stream" not in response.headers.get("content-type", ""):
            raise APIConnectionError()
        source = ProviderStream(response, client, "responses", provider_auth=provider_auth,
                                fingerprint=fingerprint, subscription=True)
        if stream:
            return ChatCompletionStream(source, body["model"])
        async for _ in source:
            pass
        if source.completed_response is None:
            raise APIConnectionError()
        return _completed_chat(source.completed_response["response"], body["model"])
    except (ProviderError, httpx.HTTPError) as exc:
        error = exc if isinstance(exc, ProviderError) else Timeout() if isinstance(exc, httpx.TimeoutException) else APIConnectionError()
        raise _subscription_error(error) from None
    finally:
        if source is None or not stream:
            if source is not None:
                await source.aclose()
            else:
                try:
                    if response is not None:
                        await response.aclose()
                finally:
                    await client.aclose()
