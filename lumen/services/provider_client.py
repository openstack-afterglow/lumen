"""Single-attempt request-local client for Lumen's private Bifrost runtime.

The runtime owns provider wire conversions; this module owns credential freshness,
HTTP lifetime, safe error identity, and the Python mapping/attribute boundary.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from typing import Any

import httpx

from lumen.services.bifrost_runtime import BifrostRuntimeError, get_bifrost_runtime
from lumen.services.provider_errors import (
    APIConnectionError,
    AuthenticationError,
    PermissionDeniedError,
    ProviderError,
    Timeout,
    error_for_status,
)
from lumen.services.provider_metadata import catalog_model_metadata, direct_provider_route
from lumen.services.providers.credentials import ProviderAuthRef, api_model_name, transport_model_name
from lumen.services.providers.errors import ProviderSubscriptionError

_CONTRACT = "single-attempt-v1"
_TIMEOUT = httpx.Timeout(90.0, connect=10.0, read=90.0, write=30.0, pool=10.0)
_PROVIDERS = frozenset({
    "openai", "anthropic", "gemini", "azure", "bedrock", "vertex", "ollama",
    "openrouter", "deepseek", "xai", "perplexity", "chatgpt", "mistral", "cohere", "groq", "together",
})
_PROVIDER_ALIASES = {"vertex_ai": "vertex", "azure_ai": "azure", "together_ai": "together"}
_RESERVED = frozenset({
    "api_base", "api_key", "base_url", "custom_llm_provider", "provider_auth",
    "model", "messages", "provider", "stream", "headers", "extra_headers",
    "single_attempt", "num_retries", "max_retries", "litellm_logging_obj",
})


class ProviderObject(dict):
    """A JSON mapping with recursive SDK-style attribute access, not an SDK type."""

    def __init__(self, value: Mapping[str, Any] | None = None, **kwargs: Any) -> None:
        super().__init__({key: provider_object(item) for key, item in {**(value or {}), **kwargs}.items()})

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name) from None

    def __setattr__(self, name: str, value: Any) -> None:
        self[name] = provider_object(value)

    def model_dump(self, *, exclude_none: bool = False, **_: Any) -> dict[str, Any]:
        def plain(value: Any) -> Any:
            if isinstance(value, Mapping):
                return {key: plain(item) for key, item in value.items() if item is not None or not exclude_none}
            if isinstance(value, list):
                return [plain(item) for item in value]
            return value
        return plain(self)


def provider_object(value: Any) -> Any:
    if isinstance(value, Mapping):
        return ProviderObject(value)
    if isinstance(value, list):
        return [provider_object(item) for item in value]
    return value


def provider_once_supported(provider_type: str | None) -> bool:
    return _PROVIDER_ALIASES.get(provider_type, provider_type) in _PROVIDERS - {"chatgpt"}


def direct_openai_stream_completed(stream: Any) -> bool:
    return getattr(stream, "completed", False) is True


def _reasoning_params(model: str, effort: str | None, custom_llm_provider: str | None) -> dict[str, Any]:
    del model, custom_llm_provider
    if effort is None or effort.strip().lower() in {"", "auto", "off", "disabled", "false"}:
        return {}
    normalized = effort.strip().lower()
    if normalized not in {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}:
        raise ValueError("invalid_reasoning_effort")
    return {"reasoning_effort": normalized}


def _cache_marker_present(value: Any) -> bool:
    if isinstance(value, Mapping):
        return "cache_control" in value or any(_cache_marker_present(item) for item in value.values())
    return isinstance(value, list) and any(_cache_marker_present(item) for item in value)


def _anthropic_cached_messages(
    model: str, messages: list[dict], provider_type: str | None, api_base: str | None,
    *, tools: list[dict] | None = None, extra: dict | None = None,
) -> list[dict]:
    if provider_type != "anthropic" or not direct_provider_route(provider_type, api_base):
        return messages
    if _cache_marker_present(messages) or _cache_marker_present(tools) or _cache_marker_present(extra):
        return messages
    if not catalog_model_metadata(model, provider_type).get("supports_prompt_caching"):
        return messages
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message.get("role") != "system":
            continue
        content = message.get("content")
        if isinstance(content, str) and content:
            updated_content = [{"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}]
        elif isinstance(content, list):
            last = next((n for n in range(len(content) - 1, -1, -1)
                         if isinstance(content[n], dict) and content[n].get("type") == "text"
                         and content[n].get("text")), None)
            if last is None:
                return messages
            updated_content = list(content)
            updated_content[last] = {**content[last], "cache_control": {"type": "ephemeral"}}
        else:
            return messages
        updated = list(messages)
        updated[index] = {**message, "content": updated_content}
        return updated
    return messages


async def _mark_rejected(ref: ProviderAuthRef | None, fingerprint: str | None) -> None:
    if ref is not None and isinstance(fingerprint, str):
        from lumen.services.providers.credentials import mark_inference_credential_rejected

        await mark_inference_credential_rejected(ref, fingerprint)


def _subscription_error(error: ProviderError) -> ProviderSubscriptionError:
    if error.status_code in {401, 403}:
        return ProviderSubscriptionError("subscription_auth_required", 502)
    if error.status_code == 429:
        return ProviderSubscriptionError("subscription_rate_limited", 429)
    if error.status_code >= 500:
        return ProviderSubscriptionError("subscription_upstream_unavailable", 503)
    return ProviderSubscriptionError("subscription_auth_invalid_response", 502)


async def _sse_payloads(response: httpx.Response) -> AsyncIterator[dict[str, Any] | str]:
    data: list[str] = []
    event: str | None = None
    async for line in response.aiter_lines():
        if not line:
            if data:
                raw = "\n".join(data)
                if raw == "[DONE]":
                    yield raw
                else:
                    try:
                        value = json.loads(raw)
                    except (ValueError, UnicodeError):
                        raise APIConnectionError() from None
                    if not isinstance(value, dict):
                        raise APIConnectionError()
                    if event is not None and "type" not in value:
                        value["type"] = event
                    yield value
            data = []
            event = None
        elif line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
        elif line.startswith("event:"):
            event = line[6:].strip()
    if data:
        raise APIConnectionError()


class ProviderStream:
    """Owned HTTP stream; EOF requires a genuine protocol terminal."""

    def __init__(
        self, response: httpx.Response, client: httpx.AsyncClient, protocol: str,
        *, provider_auth: ProviderAuthRef | None = None, fingerprint: str | None = None,
        subscription: bool = False,
    ) -> None:
        self._response = response
        self._client = client
        self._protocol = protocol
        self._source = _sse_payloads(response)
        self._ref = provider_auth
        self._fingerprint = fingerprint
        self._subscription = subscription
        self._closed = False
        self.completed = False
        self.completed_response: ProviderObject | None = None

    def __aiter__(self) -> ProviderStream:
        return self

    async def __anext__(self) -> ProviderObject:
        if self._closed:
            raise StopAsyncIteration
        try:
            payload = await self._source.__anext__()
            if payload == "[DONE]":
                if self._protocol == "chat":
                    self.completed = True
                await self.aclose()
                if not self.completed:
                    raise APIConnectionError()
                raise StopAsyncIteration
            kind = payload.get("type")
            if payload.get("error") is not None or kind in {"error", "response.failed"}:
                raise APIConnectionError()
            if self._protocol == "responses" and kind == "response.completed":
                if payload.get("response", {}).get("status") != "completed":
                    raise APIConnectionError()
                self.completed = True
                self.completed_response = provider_object(payload)
            elif self._protocol == "responses" and kind == "response.incomplete":
                self.completed = False
            elif self._protocol == "messages" and kind == "message_stop":
                self.completed = True
            return provider_object(payload)
        except StopAsyncIteration:
            await self.aclose()
            if not self.completed:
                error = APIConnectionError()
                if self._subscription:
                    raise _subscription_error(error) from None
                raise error from None
            raise
        except (httpx.TimeoutException, httpx.HTTPError, ProviderError) as exc:
            await self.aclose()
            error = exc if isinstance(exc, ProviderError) else Timeout() if isinstance(exc, httpx.TimeoutException) else APIConnectionError()
            if isinstance(error, (AuthenticationError, PermissionDeniedError)):
                await _mark_rejected(self._ref, self._fingerprint)
            if self._subscription:
                raise _subscription_error(error) from None
            raise error from None
        except BaseException:
            await self.aclose()
            raise

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        async def close() -> None:
            try:
                await self._response.aclose()
            finally:
                await self._client.aclose()
        await asyncio.shield(close())


async def _execute(
    protocol: str, provider: str, body: dict[str, Any], *, api_base: str | None,
    api_key: str | None, provider_auth: ProviderAuthRef | None,
    headers: dict[str, str] | None = None,
) -> Any:
    if provider not in _PROVIDERS - {"chatgpt"}:
        raise ValueError("provider_unsupported")
    if any(name.lower() in {"authorization", "proxy-authorization", "x-api-key", "host", "chatgpt-account-id"}
           for name in (headers or {})):
        raise ValueError("provider_headers_reserved")
    try:
        base_url, token = await get_bifrost_runtime()
    except BifrostRuntimeError:
        raise APIConnectionError() from None
    client = httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False, trust_env=False,
                               transport=httpx.AsyncHTTPTransport(retries=0))
    response: httpx.Response | None = None
    fingerprint: str | None = None
    subscription = provider_auth is not None and provider_auth.get("auth_mode") != "api_key"
    try:
        if provider_auth is not None:
            from lumen.services.providers.credentials import resolve_inference_credential

            credential = await resolve_inference_credential(provider_auth)
            fingerprint = credential.get("_fingerprint")
            if subscription:
                if provider_auth.get("auth_mode") != "anthropic_subscription" or provider != "anthropic":
                    raise ProviderSubscriptionError("subscription_protocol_unsupported", 400)
                access_token = credential.get("access_token")
                if not isinstance(access_token, str) or not access_token:
                    raise ProviderSubscriptionError("subscription_auth_required", 502)
                api_base = "https://api.anthropic.com"
                api_key = None
                beta = (headers or {}).get("anthropic-beta", "")
                headers = {**(headers or {}), "Authorization": f"Bearer {access_token}",
                           "anthropic-dangerous-direct-browser-access": "true",
                           "anthropic-beta": ",".join(filter(None, [beta, "oauth-2025-04-20"]))}
            else:
                api_key = credential.get("api_key")
        envelope = {"protocol": protocol, "provider": provider, "api_base": api_base,
                    "api_key": api_key, "headers": headers or {}, "body": body, "single_attempt": True}
        request = client.build_request("POST", f"{base_url.rstrip('/')}/v1/execute", json=envelope,
                                       headers={"Authorization": f"Bearer {token}"})
        response = await client.send(request, stream=bool(body.get("stream")))
        if response.headers.get("X-Lumen-Bifrost-Contract") != _CONTRACT:
            raise APIConnectionError()
        if response.status_code >= 300:
            # Only a runtime-attested upstream rejection may fence a credential
            # or become a known durable rejection. A local runtime 401 is not
            # evidence that the upstream key was refused.
            raw = bytearray()
            async for part in response.aiter_bytes():
                if len(raw) + len(part) > 8192:
                    raise APIConnectionError()
                raw.extend(part)
            try:
                failure = json.loads(raw)
            except (ValueError, UnicodeError):
                raise APIConnectionError() from None
            if not isinstance(failure, dict) or not isinstance(failure.get("error"), dict):
                raise APIConnectionError()
            if failure["error"].get("code") != "upstream_rejected":
                raise APIConnectionError()
            if response.status_code in {401, 403}:
                await _mark_rejected(provider_auth, fingerprint)
            raise error_for_status(response.status_code)
        if body.get("stream"):
            if "text/event-stream" not in response.headers.get("content-type", ""):
                raise APIConnectionError()
            return ProviderStream(response, client, protocol, provider_auth=provider_auth,
                                  fingerprint=fingerprint, subscription=subscription)
        try:
            value = response.json()
        except (ValueError, UnicodeError):
            raise APIConnectionError() from None
        if not isinstance(value, dict) or value.get("error") is not None:
            raise APIConnectionError()
        if protocol == "chat":
            choices = value.get("choices")
            if not isinstance(choices, list) or not choices or not all(
                isinstance(choice, dict) and isinstance(choice.get("message"), dict) for choice in choices
            ):
                raise APIConnectionError()
        elif protocol == "responses" and not isinstance(value.get("output"), list) or protocol == "messages" and not isinstance(value.get("content"), list) or protocol == "embeddings" and not isinstance(value.get("data"), list):
            raise APIConnectionError()
        return provider_object(value)
    except (httpx.TimeoutException, httpx.HTTPError, ProviderError) as exc:
        if response is not None:
            await response.aclose()
        await client.aclose()
        error = exc if isinstance(exc, ProviderError) else Timeout() if isinstance(exc, httpx.TimeoutException) else APIConnectionError()
        if subscription:
            raise _subscription_error(error) from None
        raise error from None
    except BaseException:
        if response is not None:
            await response.aclose()
        await client.aclose()
        raise
    finally:
        if response is not None and not body.get("stream"):
            try:
                await response.aclose()
            finally:
                await client.aclose()


def _provider(model: str, provider: str | None) -> str:
    selected = provider or model.partition("/")[0]
    selected = _PROVIDER_ALIASES.get(selected, selected)
    return selected if provider or selected in _PROVIDERS else "openai"


def _model(model: str, provider: str) -> str:
    name = api_model_name(transport_model_name(model), provider)
    if provider != "perplexity":
        name = name.removeprefix(f"{provider}/")
        for alias, canonical in _PROVIDER_ALIASES.items():
            if canonical == provider:
                name = name.removeprefix(f"{alias}/")
    return name.removeprefix("responses/")


async def _completion(
    model: str, messages: list[dict], *, stream: bool, api_base: str | None,
    api_key: str | None, max_tokens: int | None, temperature: float | None,
    custom_llm_provider: str | None, tools: list[dict] | None,
    native_web_search: dict[str, Any] | None, extra: dict | None,
    provider_auth: ProviderAuthRef | None,
) -> Any:
    provider = _provider(model, custom_llm_provider)
    options = dict(extra or {})
    collision = _RESERVED.intersection(options)
    if collision:
        raise ValueError("provider_transport_options_reserved")
    if native_web_search is not None:
        options["web_search_options"] = dict(native_web_search)
    if max_tokens is not None:
        options["max_tokens"] = max_tokens
    if temperature is not None:
        options["temperature"] = temperature
    if tools is not None:
        options["tools"] = tools
    if provider_auth is not None and provider_auth.get("auth_mode") == "chatgpt_device":
        if provider != "chatgpt":
            raise ProviderSubscriptionError("subscription_auth_required", 502)
        from lumen.services.providers.chatgpt_transport import acompletion as subscription_completion

        return await subscription_completion(model, messages, credential=None, stream=stream,
                                             optional_params=options, single_attempt=True,
                                             provider_auth=provider_auth)
    if provider == "chatgpt" or model.startswith(("chatgpt/", "anthropic-subscription/")) and provider_auth is None:
        raise ProviderSubscriptionError("subscription_auth_required", 502)
    body = {**options, "model": _model(model, provider), "stream": stream,
            "messages": _anthropic_cached_messages(model, messages, provider, api_base, tools=tools, extra=extra)}
    if stream:
        body["stream_options"] = {**body.get("stream_options", {}), "include_usage": True}
    return await _execute("chat", provider, body, api_base=api_base, api_key=api_key, provider_auth=provider_auth)


async def acompletion(
    model: str, messages: list[dict], *, api_base: str | None = None, api_key: str | None = None,
    max_tokens: int | None = None, temperature: float | None = None,
    custom_llm_provider: str | None = None, tools: list[dict] | None = None,
    native_web_search: dict[str, Any] | None = None, extra: dict | None = None,
    provider_auth: ProviderAuthRef | None = None, provider_once: bool = False,
) -> Any:
    if provider_once and not provider_once_supported(_provider(model, custom_llm_provider)):
        raise ValueError("provider_once_unsupported")
    return await _completion(model, messages, stream=False, api_base=api_base, api_key=api_key,
                             max_tokens=max_tokens, temperature=temperature, custom_llm_provider=custom_llm_provider,
                             tools=tools, native_web_search=native_web_search, extra=extra, provider_auth=provider_auth)


async def acompletion_stream(
    model: str, messages: list[dict], *, api_base: str | None = None, api_key: str | None = None,
    max_tokens: int | None = None, temperature: float | None = None,
    custom_llm_provider: str | None = None, tools: list[dict] | None = None,
    native_web_search: dict[str, Any] | None = None, extra: dict | None = None,
    reasoning_effort: str | None = None, provider_auth: ProviderAuthRef | None = None,
) -> Any:
    return await _completion(model, messages, stream=True, api_base=api_base, api_key=api_key,
                             max_tokens=max_tokens, temperature=temperature, custom_llm_provider=custom_llm_provider,
                             tools=tools, native_web_search=native_web_search,
                             extra={**(extra or {}), **_reasoning_params(model, reasoning_effort, custom_llm_provider)},
                             provider_auth=provider_auth)


async def aresponses(
    *, model: str, input: str | list[dict], stream: bool, api_base: str | None,
    api_key: str | None, custom_llm_provider: str | None, provider_auth: ProviderAuthRef | None,
    include: list[str] | None = None, instructions: str | None = None,
    max_output_tokens: int | None = None, metadata: dict | None = None,
    parallel_tool_calls: bool | None = None, prompt_cache_key: str | None = None,
    reasoning: dict | None = None, temperature: float | None = None, text: dict | None = None,
    tool_choice: Any = None, tools: list[dict] | None = None, top_p: float | None = None,
    truncation: str | None = None, user: str | None = None, service_tier: str | None = None,
    safety_identifier: str | None = None, context_management: list[dict] | None = None,
    provider_once: bool = False,
) -> Any:
    provider = _provider(model, custom_llm_provider)
    if provider == "chatgpt" or provider_auth is not None and provider_auth.get("auth_mode") != "api_key":
        raise ProviderSubscriptionError("subscription_protocol_unsupported", 400)
    if provider_once and (stream or not provider_once_supported(provider)):
        raise ValueError("provider_once_unsupported")
    optional = dict(include=include, instructions=instructions, max_output_tokens=max_output_tokens,
                    metadata=metadata, parallel_tool_calls=parallel_tool_calls, prompt_cache_key=prompt_cache_key,
                    reasoning=reasoning, temperature=temperature, text=text, tool_choice=tool_choice,
                    tools=tools, top_p=top_p, truncation=truncation, user=user, service_tier=service_tier,
                    safety_identifier=safety_identifier, context_management=context_management)
    body = {key: value for key, value in optional.items() if value is not None}
    body.update(model=_model(model, provider), input=input, stream=stream)
    return await _execute("responses", provider, body, api_base=api_base, api_key=api_key, provider_auth=provider_auth)


async def aanthropic_messages(
    *, model: str, messages: list[dict], max_tokens: int, stream: bool,
    api_base: str | None, api_key: str | None, custom_llm_provider: str | None,
    provider_auth: ProviderAuthRef | None, system: Any = None, metadata: dict | None = None,
    stop_sequences: list[str] | None = None, temperature: float | None = None,
    thinking: dict | None = None, context_management: dict | None = None,
    tool_choice: dict | None = None, tools: list[dict] | None = None,
    top_k: int | None = None, top_p: float | None = None, container: dict | None = None,
    output_config: dict | None = None, anthropic_headers: dict[str, str] | None = None,
) -> Any:
    provider = _provider(model, custom_llm_provider)
    if provider == "chatgpt":
        raise ProviderSubscriptionError("subscription_protocol_unsupported", 400)
    optional = dict(system=system, metadata=metadata, stop_sequences=stop_sequences, temperature=temperature,
                    thinking=thinking, context_management=context_management, tool_choice=tool_choice,
                    tools=tools, top_k=top_k, top_p=top_p, container=container, output_config=output_config)
    body = {key: value for key, value in optional.items() if value is not None}
    body.update(model=_model(model, provider), messages=messages, max_tokens=max_tokens, stream=stream)
    return await _execute("messages", provider, body, api_base=api_base, api_key=api_key,
                          provider_auth=provider_auth, headers=anthropic_headers)


async def aembedding(
    model: str, input: str | list[str] | list[int] | list[list[int]], *,
    api_base: str | None = None, api_key: str | None = None,
    custom_llm_provider: str | None = None, provider_auth: ProviderAuthRef | None = None,
    dimensions: int | None = None, encoding_format: str | None = None, user: str | None = None,
) -> ProviderObject:
    provider = _provider(model, custom_llm_provider)
    if provider_auth is not None and provider_auth.get("auth_mode") != "api_key":
        raise ProviderSubscriptionError("subscription_protocol_unsupported", 400)
    body: dict[str, Any] = {"model": _model(model, provider), "input": input}
    body.update({key: value for key, value in dict(dimensions=dimensions, encoding_format=encoding_format, user=user).items()
                 if value is not None})
    return await _execute("embeddings", provider, body, api_base=api_base, api_key=api_key, provider_auth=provider_auth)
