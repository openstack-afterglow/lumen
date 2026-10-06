"""Durable Batch chat/Responses: provider-once transport, frozen admission and single-call execution."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, Decimal
from types import SimpleNamespace

import httpx
import litellm
import pytest
from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler

from lumen.config import get_settings
from lumen.services import api_key_store, completion_api, credit, litellm_client
from lumen.services.durable_runs import api_completion, execution
from lumen.services.durable_runs.errors import DurableRunInputError
from lumen.services.providers.errors import ProviderSubscriptionError

_URL = "https://provider.test/v1/responses"


def _disconnecting_handler(base: type | None, calls: list[str]) -> AsyncHTTPHandler:
    def disconnect(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        raise httpx.RemoteProtocolError("Server disconnected without sending a response.", request=request)

    handler = litellm_client.single_attempt_http_handler() if base is None else base()
    handler.client = httpx.AsyncClient(transport=httpx.MockTransport(disconnect))
    return handler


async def test_stock_litellm_handler_resends_a_post_after_a_disconnect():
    calls: list[str] = []
    handler = _disconnecting_handler(AsyncHTTPHandler, calls)
    # The stock handler opens a second connection for the same POST; the test
    # network guard intercepts that duplicate provider request.
    with pytest.raises(AssertionError, match="unmocked outbound HTTP"):
        await handler.post(_URL, json={"input": "hello"})
    assert calls == [_URL]
    await handler.close()


async def test_single_attempt_handler_surfaces_a_disconnect_without_resending():
    calls: list[str] = []
    handler = _disconnecting_handler(None, calls)
    with pytest.raises(httpx.RemoteProtocolError):
        await handler.post(_URL, json={"input": "hello"})
    assert calls == [_URL]
    await handler.close()


@pytest.mark.parametrize(
    ("provider_type", "api_base", "expects_handler"),
    [
        ("openai", "https://gateway.internal/v1", False),
        ("openai", None, True),
        ("anthropic", None, True),
        ("gemini", None, True),
    ],
)
async def test_provider_once_chat_disables_every_retry_layer(monkeypatch, provider_type, api_base, expects_handler):
    seen: list[dict] = []

    async def fake_acompletion(**kwargs):
        seen.append(kwargs)
        return SimpleNamespace(choices=[])

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    await litellm_client.acompletion(
        "model-x", [{"role": "user", "content": "hi"}], api_base=api_base, api_key="k",
        custom_llm_provider=provider_type, max_tokens=16, provider_once=True,
    )
    [kwargs] = seen
    assert kwargs["num_retries"] == 0 and kwargs["max_retries"] == 0
    assert ("client" in kwargs) is expects_handler
    if expects_handler:
        assert type(kwargs["client"]).single_connection_post_request is not AsyncHTTPHandler.single_connection_post_request


async def test_online_chat_call_keeps_litellm_defaults(monkeypatch):
    seen: list[dict] = []

    async def fake_acompletion(**kwargs):
        seen.append(kwargs)
        return SimpleNamespace(choices=[])

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    await litellm_client.acompletion("model-x", [{"role": "user", "content": "hi"}], custom_llm_provider="anthropic")
    assert not {"num_retries", "max_retries", "client"} & set(seen[0])


@pytest.mark.parametrize("provider_type", ["deepseek", "perplexity", "openrouter", None])
async def test_provider_once_refuses_adapters_without_a_single_attempt_proof(monkeypatch, provider_type):
    async def forbidden(**_kwargs):
        raise AssertionError("provider must not be called")

    monkeypatch.setattr(litellm, "acompletion", forbidden)
    with pytest.raises(ValueError, match="provider_once_unsupported"):
        await litellm_client.acompletion(
            "model-x", [{"role": "user", "content": "hi"}], api_key="k",
            custom_llm_provider=provider_type, provider_once=True,
        )


async def test_provider_once_responses_uses_a_single_attempt_client(monkeypatch):
    seen: list[dict] = []

    async def fake_aresponses(**kwargs):
        seen.append(kwargs)
        return {"output": []}

    monkeypatch.setattr(litellm, "aresponses", fake_aresponses)
    await litellm_client.aresponses(
        model="model-x", input="hi", stream=False, api_base="https://gateway.internal/v1", api_key="k",
        custom_llm_provider="openai", provider_auth=None, provider_once=True,
    )
    assert seen[0]["num_retries"] == 0 and seen[0]["max_retries"] == 0 and "client" in seen[0]
    with pytest.raises(ValueError, match="provider_once_unsupported"):
        await litellm_client.aresponses(
            model="model-x", input="hi", stream=True, api_base=None, api_key="k",
            custom_llm_provider="openai", provider_auth=None, provider_once=True,
        )
    assert len(seen) == 1


def _route(**overrides) -> dict:
    route = {
        "model_name": "gpt-test", "api_model_name": "gpt-test", "api_provider": "openai", "provider_type": "openai",
        "provider_id": 1, "model_id": 2, "provider_name": "OpenAI", "model_kind": "text",
        "config_version_hash": "hash", "api_base": None, "api_key": "k", "provider_auth": None,
        "input_price_per_token": Decimal("0.000001"), "output_price_per_token": Decimal("0.000002"),
        "margin_multiplier": Decimal("1"), "capabilities": {"context_limit": 1000}, "cache_price_sources": {},
        "price_source": "manual", "price_version": None,
    }
    route.update(overrides)
    return route


@pytest.fixture
def route_catalog(monkeypatch):
    catalog = {"route": _route(), "tokens": 10}

    async def resolve_api(model, *, provider=None):
        return dict(catalog["route"])

    monkeypatch.setattr(completion_api, "resolve_api", resolve_api)
    monkeypatch.setattr(
        litellm_client, "count_context_tokens",
        lambda *_args, **_kwargs: litellm_client.ContextTokenCount(tokens=catalog["tokens"], measurement="tokenizer"),
    )
    return catalog


async def test_chat_admission_freezes_default_output_and_worst_case_bound(route_catalog):
    prepared = await api_completion.prepare_api_completion_run(
        {"model": "gpt-test", "messages": [{"role": "user", "content": "hi"}]},
        operation="chat.completions", project_id="p", user_id="u",
    )
    per_usd = Decimal(str(get_settings().chat_credit_per_usd))
    usd = Decimal("0.000001") * 1000 + Decimal("0.000002") * 4096 + Decimal("0.0000000005")
    expected = (usd * per_usd).quantize(Decimal("0.00000001"), rounding=ROUND_CEILING)
    assert prepared.payload["max_tokens"] == 4096
    assert prepared.pricing_snapshot["max_output_tokens"] == 4096
    assert prepared.pricing_snapshot["context_limit"] == 1000
    assert Decimal(prepared.pricing_snapshot["bound_credits"]) == expected
    assert prepared.required_scopes == ("compat:completions:write",)
    assert prepared.capability_snapshot["operation"] == "chat.completions"


async def test_responses_admission_sends_the_frozen_output_budget(route_catalog):
    prepared = await api_completion.prepare_api_completion_run(
        {"model": "gpt-test", "input": "hi", "tools": [{"type": "function", "name": "f", "parameters": {}}]},
        operation="responses", project_id="p", user_id="u", required_scopes=("compat:batches:write",),
    )
    assert prepared.payload["options"]["max_output_tokens"] == 4096
    assert prepared.pricing_snapshot["max_output_tokens"] == 4096
    assert prepared.required_scopes == ("compat:batches:write",)


@pytest.mark.parametrize(
    ("operation", "body", "route_overrides", "tokens", "code"),
    [
        ("chat.completions", {"stream": True}, {}, 10, "stream_unsupported"),
        ("chat.completions", {"tools": [{"type": "web_search"}]}, {}, 10, "provider_builtin_tool_unsupported"),
        ("chat.completions", {}, {"capabilities": {}}, 10, "context_window_unknown"),
        ("chat.completions", {}, {"output_price_per_token": None}, 10, "pricing_unavailable"),
        ("chat.completions", {}, {"provider_type": "deepseek"}, 10, "provider_once_unsupported"),
        ("chat.completions", {}, {}, 1001, "context_length_exceeded"),
        ("chat.completions", {"model": "lumen"}, {}, 10, "virtual_model_unsupported"),
        ("responses", {"context_management": [{"type": "compaction"}]}, {}, 10, "provider_compaction_unsupported"),
        ("responses", {"previous_response_id": "resp_1"}, {}, 10, "stateful_responses_not_supported"),
        ("responses", {"service_tier": "priority"}, {}, 10, "service_tier_unsupported"),
    ],
)
async def test_admission_rejects_unbounded_or_unsupported_requests(
    route_catalog, operation, body, route_overrides, tokens, code
):
    route_catalog["route"] = _route(**route_overrides)
    route_catalog["tokens"] = tokens
    base = (
        {"model": "gpt-test", "messages": [{"role": "user", "content": "hi"}]}
        if operation == "chat.completions" else {"model": "gpt-test", "input": "hi"}
    )
    with pytest.raises(DurableRunInputError, match=code):
        await api_completion.prepare_api_completion_run(
            {**base, **body}, operation=operation, project_id="p", user_id="u",
        )


def _key(**overrides):
    row = SimpleNamespace(
        id=7, owner_user_id="u", owner_project_id="p", is_active=True, revoked_at=None,
        expires_at=None, scopes=["models:read", "compat:completions:write"],
    )
    for key, value in overrides.items():
        setattr(row, key, value)
    return row


class _KeySession:
    def __init__(self, row):
        self.row = row
        self.calls = 0

    async def execute(self, _statement):
        self.calls += 1
        return SimpleNamespace(scalar_one_or_none=lambda: self.row)


@pytest.mark.parametrize(
    "row",
    [
        None,
        _key(revoked_at=datetime.now(UTC)),
        _key(is_active=False),
        _key(expires_at=datetime.now(UTC) - timedelta(seconds=1)),
        _key(owner_project_id="other"),
        _key(scopes=["models:read"]),
    ],
)
async def test_run_key_reauthorization_fails_closed(row):
    with pytest.raises(api_key_store.ApiKeyForbidden):
        await api_key_store.authorize_api_key_in_transaction(
            _KeySession(row), api_key_id=7, user_id="u", project_id="p",
            required_scopes=("compat:completions:write",),
        )


async def test_run_key_reauthorization_accepts_live_scoped_key_and_keystone_runs():
    session = _KeySession(_key())
    await api_key_store.authorize_api_key_in_transaction(
        session, api_key_id=7, user_id="u", project_id="p", required_scopes=("compat:completions:write",),
    )
    keystone = _KeySession(None)
    await api_key_store.authorize_api_key_in_transaction(
        keystone, api_key_id=None, user_id="u", project_id="p", required_scopes=("compat:completions:write",),
    )
    assert session.calls == 1 and keystone.calls == 0


async def test_call_reservation_rejects_negative_or_non_finite_bounds_before_any_lock():
    for bound in (Decimal("-0.00000001"), Decimal("NaN")):
        with pytest.raises(credit.QuotaExceeded):
            await credit.reserve_call_credit_in_transaction(
                object(), user_id="u", project_id="p", api_key_id=None, bound=bound,
            )


_PRICING = {
    "input_price_per_token": "0.000001", "output_price_per_token": "0.000002", "token_rates": {},
    "required_token_modalities": [], "margin_multiplier": "1", "chat_credit_per_usd": "1000",
    "context_limit": 100, "max_output_tokens": 16,
}
_PRICING["bound_credits"] = format(credit.call_credit_bound(_PRICING, input_tokens=100, output_tokens=16), "f")
_PAYLOAD = {
    "kind": "api_completion", "operation": "chat.completions", "messages": [{"role": "user", "content": "hi"}],
    "max_tokens": 16, "temperature": None, "tools": None, "tool_choice": None,
    "required_scopes": ["compat:completions:write"], "execution_protocol_version": 1,
    "user_id": "u", "project_id": "p",
}
_CAPABILITY = {"provider_id": 1, "model_id": 2, "provider_type": "openai", "model_name": "gpt-test",
               "config_version_hash": "hash", "operation": "chat.completions"}


class _StatusSession:
    def __init__(self, status):
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def execute(self, _statement):
        return SimpleNamespace(scalar_one_or_none=lambda: self.status)


@pytest.fixture
def executor(monkeypatch):
    state = {"segment": None, "start": "started", "invoke": None, "settle": "completed",
             "invocations": 0, "checkpoints": [], "finished": [], "settled": 0}

    monkeypatch.setattr(api_completion, "_factory", lambda: (lambda: _StatusSession(state["segment"])))

    async def resolve_model_snapshot(_snapshot):
        return {"provider_type": "openai", "model_name": "gpt-test", "api_model_name": "gpt-test"}

    async def segment_start(*_args, **_kwargs):
        if isinstance(state["start"], BaseException):
            raise state["start"]
        return state["start"]

    async def invoke_chat_once(**kwargs):
        state["invocations"] += 1
        assert kwargs["provider_once"] is True
        if isinstance(state["invoke"], BaseException):
            raise state["invoke"]
        return state["invoke"]

    async def checkpoint(_run_id, *, owner, result, usage):
        state["checkpoints"].append((result, usage))

    async def settle(_run_id, *, owner):
        state["settled"] += 1
        return state["settle"]

    async def finish(run_id, **kwargs):
        state["finished"].append(kwargs)

    monkeypatch.setattr(api_completion.routing, "resolve_model_snapshot", resolve_model_snapshot)
    monkeypatch.setattr(api_completion, "_segment_start", segment_start)
    monkeypatch.setattr(api_completion.completion_api, "invoke_chat_once", invoke_chat_once)
    monkeypatch.setattr(api_completion, "_checkpoint", checkpoint)
    monkeypatch.setattr(api_completion, "_settle", settle)
    monkeypatch.setattr(execution, "_finish", finish)
    return state


async def _run_executor():
    return await api_completion._execute(
        str(uuid.uuid4()), owner="worker:1", payload=dict(_PAYLOAD),
        capability_snapshot=dict(_CAPABILITY), pricing_snapshot=dict(_PRICING),
    )


async def test_checkpointed_run_settles_without_a_provider_call(executor):
    executor["segment"] = "completed"
    assert await _run_executor() is True
    assert executor["invocations"] == 0 and executor["settled"] == 1
    assert executor["finished"] == [{"status": "completed", "message_id": None, "owner": "worker:1"}]


async def test_started_but_unrecorded_call_is_never_resent(executor):
    executor["segment"] = "provider_started"
    await _run_executor()
    assert executor["invocations"] == 0 and executor["checkpoints"] == []
    assert executor["finished"][0]["error_code"] == "provider_result_unknown"


async def test_successful_call_checkpoints_the_wire_envelope_before_settlement(executor):
    executor["invoke"] = completion_api.ChatInvocation(
        model="gpt-test", content="hello", tool_calls=None, finish_reason="stop",
        usage={"prompt_tokens": 5, "completion_tokens": 3},
    )
    await _run_executor()
    [(result, usage)] = executor["checkpoints"]
    envelope = result["envelope"]
    assert envelope["status_code"] == 200
    assert envelope["body"]["object"] == "chat.completion"
    assert envelope["body"]["choices"][0]["message"]["content"] == "hello"
    assert envelope["body"]["usage"]["total_tokens"] == 8
    assert usage["token_usage"]["prompt_tokens"] == 5
    assert executor["invocations"] == 1 and executor["settled"] == 1
    assert executor["finished"][0]["status"] == "completed"


async def test_confirmed_provider_rejection_releases_through_settlement(executor):
    executor["invoke"] = litellm.BadRequestError(message="bad", model="gpt-test", llm_provider="openai")
    executor["settle"] = "rejected"
    await _run_executor()
    [(result, usage)] = executor["checkpoints"]
    assert result["envelope"]["status_code"] == 400 and usage is None
    assert executor["invocations"] == 1 and executor["settled"] == 1
    assert executor["finished"][0]["error_code"] == "provider_rejected"


@pytest.mark.parametrize(
    "failure",
    [
        litellm.Timeout(message="slow", model="gpt-test", llm_provider="openai"),
        litellm.APIConnectionError(message="reset", llm_provider="openai", model="gpt-test"),
        litellm.InternalServerError(message="boom", llm_provider="openai", model="gpt-test"),
        ProviderSubscriptionError("subscription_upstream_unavailable", 503),
        httpx.RemoteProtocolError("Server disconnected"),
    ],
)
async def test_ambiguous_provider_failure_keeps_the_hold_unknown(executor, failure):
    executor["invoke"] = failure
    await _run_executor()
    assert executor["invocations"] == 1
    assert executor["checkpoints"] == [] and executor["settled"] == 0
    assert executor["finished"][0]["status"] == "failed"
    assert executor["finished"][0]["error_code"] == "provider_result_unknown"


@pytest.mark.parametrize(
    ("start", "status", "code"),
    [
        (api_key_store.ApiKeyForbidden("revoked"), "failed", "api_key_unauthorized"),
        (credit.QuotaExceeded("limit"), "failed", "quota_exceeded"),
        ("batch_closed", "canceled", "batch_closed"),
        ("canceled", "canceled", "canceled"),
    ],
)
async def test_no_provider_io_without_a_committed_intent(executor, start, status, code):
    executor["start"] = start
    await _run_executor()
    assert executor["invocations"] == 0 and executor["checkpoints"] == []
    assert executor["finished"][0]["status"] == status
    assert executor["finished"][0]["error_code"] == code


async def test_changed_provider_configuration_blocks_io(executor, monkeypatch):
    async def changed(_snapshot):
        return None

    monkeypatch.setattr(api_completion.routing, "resolve_model_snapshot", changed)
    await _run_executor()
    assert executor["invocations"] == 0
    assert executor["finished"][0]["error_code"] == "provider_configuration_changed"
