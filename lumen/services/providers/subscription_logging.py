"""Request-local LiteLLM bookkeeping that never retains subscription secrets or raw payloads."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from litellm.litellm_core_utils.litellm_logging import Logging


class _AllowlistDetails(dict[str, Any]):
    _ALLOWED = frozenset(
        {
            "call_type",
            "combined_usage_object",
            "completion_start_time",
            "end_time",
            "litellm_call_id",
            "litellm_params",
            "litellm_trace_id",
            "model",
            "provider",
            "response_cost",
            "start_time",
            "status",
            "stream",
            "usage",
        }
    )

    def __setitem__(self, key: str, value: Any) -> None:
        if key in self._ALLOWED:
            super().__setitem__(key, value)

    def update(self, *args: Any, **kwargs: Any) -> None:
        incoming = dict(*args, **kwargs)
        for key, value in incoming.items():
            self[key] = value

    def setdefault(self, key: str, default: Any = None) -> Any:
        if key not in self._ALLOWED:
            return default
        return super().setdefault(key, default)


def _usage(value: Any) -> dict[str, int] | None:
    usage = getattr(value, "usage", None)
    if usage is None and isinstance(value, dict):
        usage = value.get("usage")
    if usage is None:
        return None
    if hasattr(usage, "model_dump"):
        usage = usage.model_dump()
    if not isinstance(usage, dict):
        return None
    allowed: dict[str, int] = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens", "input_tokens", "output_tokens"):
        item = usage.get(key)
        if isinstance(item, int) and not isinstance(item, bool) and item >= 0:
            allowed[key] = item
    return allowed or None


class SubscriptionLogging(Logging):
    """LiteLLM-compatible logger with a closed, secret-free state surface."""

    def __init__(
        self,
        *,
        model: str,
        provider: str,
        fixed_api_base: str,
        call_id: str,
        stream: bool,
    ) -> None:
        started_at = datetime.now(UTC)
        safe_params = {
            "no_log": True,
            "api_base": fixed_api_base,
            "custom_llm_provider": provider,
        }
        super().__init__(
            model=model,
            messages=[],
            stream=stream,
            call_type="acompletion",
            start_time=started_at,
            litellm_call_id=call_id,
            function_id=call_id,
            kwargs=safe_params,
            log_raw_request_response=False,
        )
        self.messages = []
        self.input_messages = None
        self.optional_params: dict[str, Any] = {}
        self.custom_llm_provider = provider
        self.model_call_details = _AllowlistDetails(
            {
                "litellm_trace_id": self.litellm_trace_id,
                "litellm_call_id": call_id,
                "litellm_params": safe_params,
                "model": model,
                "provider": provider,
                "start_time": started_at,
                "stream": stream,
                "call_type": "acompletion",
                "status": "created",
            }
        )

    def _record(self, status: str, *, result: Any = None, end_time: Any = None) -> None:
        self.model_call_details["status"] = status
        if end_time is not None:
            self.model_call_details["end_time"] = end_time
        usage = _usage(result)
        if usage is not None:
            self.model_call_details["usage"] = usage

    def update_environment_variables(
        self,
        litellm_params: dict,
        optional_params: dict,
        model: str | None = None,
        user: str | None = None,
        **additional_params: Any,
    ) -> None:
        if model:
            self.model = model
            self.model_call_details["model"] = model
        stream_options = additional_params.get("stream_options")
        if isinstance(stream_options, dict):
            self.stream_options = {
                "include_usage": bool(stream_options.get("include_usage")),
            }
        self.optional_params = {}
        self.model_call_details["litellm_params"] = {
            "no_log": True,
            "api_base": self.model_call_details["litellm_params"]["api_base"],
            "custom_llm_provider": self.custom_llm_provider,
        }

    def update_from_kwargs(
        self,
        kwargs: dict,
        litellm_params: dict | None = None,
        optional_params: dict | None = None,
        model: str | None = None,
        user: str | None = None,
        **additional_params: Any,
    ) -> None:
        self.update_environment_variables({}, {}, model=model, **additional_params)

    def update_messages(self, messages: list) -> None:
        self.messages = []
        self.input_messages = None

    def pre_call(
        self,
        input: Any,
        api_key: Any,
        model: str | None = None,
        additional_args: dict | None = None,
    ) -> None:
        if model:
            self.model = model
            self.model_call_details["model"] = model
        self._record("request_started")

    def post_call(
        self,
        original_response: Any,
        input: Any = None,
        api_key: Any = None,
        additional_args: dict | None = None,
    ) -> None:
        self._record("response_received", result=original_response)

    def success_handler(
        self,
        result: Any = None,
        start_time: Any = None,
        end_time: Any = None,
        cache_hit: Any = None,
        **kwargs: Any,
    ) -> None:
        self._record("succeeded", result=result, end_time=end_time or datetime.now(UTC))

    async def async_success_handler(
        self,
        result: Any = None,
        start_time: Any = None,
        end_time: Any = None,
        cache_hit: Any = None,
        **kwargs: Any,
    ) -> None:
        self.success_handler(result=result, start_time=start_time, end_time=end_time, cache_hit=cache_hit)

    def failure_handler(
        self,
        exception: Any,
        traceback_exception: Any,
        start_time: Any = None,
        end_time: Any = None,
    ) -> None:
        self._record("failed", end_time=end_time or datetime.now(UTC))

    async def async_failure_handler(
        self,
        exception: Any,
        traceback_exception: Any,
        start_time: Any = None,
        end_time: Any = None,
    ) -> None:
        self.failure_handler(exception, traceback_exception, start_time=start_time, end_time=end_time)

    async def dispatch_success_handlers(
        self,
        result: Any = None,
        start_time: Any = None,
        end_time: Any = None,
        cache_hit: Any = None,
        **kwargs: Any,
    ) -> None:
        await self.async_success_handler(
            result=result,
            start_time=start_time,
            end_time=end_time,
            cache_hit=cache_hit,
        )

    def should_run_logging(self, *args: Any, **kwargs: Any) -> bool:
        return False

    def _response_cost_calculator(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def _response_cost_calculator_async(self, *args: Any, **kwargs: Any) -> None:
        return None
