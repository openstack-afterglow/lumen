"""Durable Batch chat/Responses: provider-once transport, frozen admission and single-call execution."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, Decimal
from types import SimpleNamespace

import httpx
import pytest

from lumen.config import get_settings
from lumen.services import api_key_store, completion_api, credit, provider_client, provider_errors, provider_metadata
from lumen.services.durable_runs import api_completion, execution
from lumen.services.durable_runs.errors import DurableRunInputError
from lumen.services.providers.errors import ProviderSubscriptionError

pytestmark = pytest.mark.usefixtures("current_project_authority")
_RUNTIME = "http://127.0.0.1:18643"


def _runtime(monkeypatch, handler):
    async def ready():
        return _RUNTIME, "internal-token"

    monkeypatch.setattr(provider_client, "get_bifrost_runtime", ready)
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda **kwargs: httpx.MockTransport(handler))


@pytest.mark.parametrize("operation", ["chat", "responses"])
@pytest.mark.parametrize("outcome", ["disconnect", "timeout", 400, 429, 503])
async def test_durable_provider_attempt_never_resends(monkeypatch, operation, outcome):
    calls = []

    def handle(request):
        calls.append(request)
        if outcome == "disconnect":
            raise httpx.RemoteProtocolError("ambiguous disconnect", request=request)
        if outcome == "timeout":
            raise httpx.ReadTimeout("ambiguous timeout", request=request)
        return httpx.Response(outcome, headers={"X-Lumen-Bifrost-Contract": "single-attempt-v1"},
            json={"error": {"code": "upstream_rejected", "message": "upstream-private-secret"}})

    _runtime(monkeypatch, handle)
    expected = {
        "disconnect": provider_errors.APIConnectionError,
        "timeout": provider_errors.Timeout,
        400: provider_errors.BadRequestError,
        429: provider_errors.RateLimitError,
        503: provider_errors.InternalServerError,
    }[outcome]
    with pytest.raises(expected) as failure:
        if operation == "chat":
            await provider_client.acompletion("model-x", [{"role": "user", "content": "hi"}],
                api_key="request-key", custom_llm_provider="openai", provider_once=True)
        else:
            await provider_client.aresponses(model="model-x", input="hi", stream=False,
                api_base=None, api_key="request-key", custom_llm_provider="openai", provider_auth=None,
                provider_once=True)
    assert len(calls) == 1
    assert str(calls[0].url) == f"{_RUNTIME}/v1/execute"
    assert json.loads(calls[0].content)["single_attempt"] is True
    assert "upstream-private-secret" not in str(failure.value)
    assert failure.value.known_rejection is (outcome in {400, 429})


@pytest.mark.parametrize("provider,api_base", [
    ("openai", None), ("openai", "https://gateway.internal/v1"),
    ("anthropic", None), ("gemini", None), ("perplexity", None),
])
@pytest.mark.parametrize("provider_once", [False, True])
async def test_online_and_durable_calls_use_the_same_single_attempt_contract(monkeypatch, provider, api_base, provider_once):
    calls = []

    def handle(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, headers={"X-Lumen-Bifrost-Contract": "single-attempt-v1"},
            json={"choices": [{"message": {"content": "accepted"}, "finish_reason": "stop"}],
                  "usage": {"prompt_tokens": 2, "completion_tokens": 1}})

    _runtime(monkeypatch, handle)
    result = await provider_client.acompletion("model-x", [{"role": "user", "content": "hi"}],
        api_base=api_base, api_key="request-key", custom_llm_provider=provider, provider_once=provider_once)
    assert len(calls) == 1
    assert calls[0]["single_attempt"] is True
    assert calls[0]["provider"] == provider and calls[0]["api_base"] == api_base
    assert result.choices[0].message.content == "accepted"
    assert result.usage.prompt_tokens == 2


@pytest.mark.parametrize("provider_type", ["chatgpt", "unknown-provider"])
async def test_provider_once_refuses_unproven_adapters_before_io(monkeypatch, provider_type):
    def forbidden(request):
        pytest.fail("unsupported route reached transport")

    _runtime(monkeypatch, forbidden)
    with pytest.raises(ValueError, match="provider_once_unsupported"):
        await provider_client.acompletion("model-x", [], custom_llm_provider=provider_type, provider_once=True)


async def test_provider_once_refuses_streaming_responses_before_io(monkeypatch):
    def forbidden(request):
        pytest.fail("durable streaming route reached transport")

    _runtime(monkeypatch, forbidden)
    with pytest.raises(ValueError, match="provider_once_unsupported"):
        await provider_client.aresponses(model="model-x", input="hi", stream=True, api_base=None,
            api_key="request-key", custom_llm_provider="openai", provider_auth=None, provider_once=True)


def _route(**overrides) -> dict:
    route = {
        "model_name": "gpt-test", "api_model_name": "gpt-test", "api_provider": "openai", "provider_type": "openai",
        "provider_id": 1, "model_id": 2, "provider_name": "OpenAI", "model_kind": "text",
        "config_version_hash": "hash", "api_base": None, "api_key": None,
        "provider_account_id": 11, "provider_credential_id": 12, "provider_credential_generation": 3,
        "provider_auth": {"provider_id": 1, "account_id": 11, "credential_id": 12, "generation": 3,
                          "auth_mode": "api_key", "model_id": 2, "required_capabilities": []},
        "input_price_per_token": Decimal("0.000001"), "output_price_per_token": Decimal("0.000002"),
        "margin_multiplier": Decimal("1"), "capabilities": {"context_limit": 1000}, "cache_price_sources": {},
        "price_source": "manual", "price_version": None,
    }
    route.update(overrides)
    return route


@pytest.fixture
def route_catalog(monkeypatch):
    catalog = {"route": _route(), "tokens": 10}

    async def resolve_api(model, *, provider=None, operation="chat"):
        return dict(catalog["route"])

    monkeypatch.setattr(completion_api, "resolve_api", resolve_api)
    monkeypatch.setattr(
        provider_metadata, "count_context_tokens",
        lambda *_args, **_kwargs: provider_metadata.ContextTokenCount(tokens=catalog["tokens"], measurement="tokenizer"),
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
    assert prepared.capability_snapshot["provider_account_id"] == 11
    assert prepared.capability_snapshot["provider_credential_id"] == 12
    assert prepared.capability_snapshot["provider_credential_generation"] == 3
    assert "api_key" not in prepared.capability_snapshot
    route_catalog["route"]["provider_auth"]["generation"] = 99
    assert prepared.capability_snapshot["provider_auth"]["generation"] == 3


async def test_responses_admission_sends_the_frozen_output_budget(route_catalog):
    prepared = await api_completion.prepare_api_completion_run(
        {"model": "gpt-test", "input": "hi", "tools": [{"type": "function", "name": "f", "parameters": {}}]},
        operation="responses", project_id="p", user_id="u", required_scopes=("compat:batches:write",),
    )
    assert prepared.payload["options"]["max_output_tokens"] == 4096
    assert prepared.pricing_snapshot["max_output_tokens"] == 4096
    assert prepared.required_scopes == ("compat:batches:write", "native:tools:execute")


@pytest.mark.parametrize(
    ("operation", "body", "route_overrides", "tokens", "code"),
    [
        ("chat.completions", {"stream": True}, {}, 10, "stream_unsupported"),
        ("chat.completions", {"tools": [{"type": "web_search"}]}, {}, 10, "provider_builtin_tool_unsupported"),
        ("chat.completions", {}, {"capabilities": {}}, 10, "context_window_unknown"),
        ("chat.completions", {}, {"output_price_per_token": None}, 10, "pricing_unavailable"),
        ("chat.completions", {}, {"provider_type": "chatgpt"}, 10, "provider_once_unsupported"),
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
async def test_run_key_reauthorization_fails_closed(row, monkeypatch):
    async def current_authority(user_id, project_id):
        return {"roles": ["member", "lumen-chat_user"], "is_system_admin": False}

    monkeypatch.setattr("lumen.auth.resolve_project_authority", current_authority)
    with pytest.raises(api_key_store.ApiKeyForbidden):
        await api_key_store.authorize_api_key_in_transaction(
            _KeySession(row), api_key_id=7, user_id="u", project_id="p",
            required_scopes=("compat:completions:write",),
        )


async def test_run_key_reauthorization_accepts_live_scoped_key_and_keystone_runs(monkeypatch):
    async def current_authority(user_id, project_id):
        return {"roles": ["member", "lumen-chat_user"], "is_system_admin": False}

    monkeypatch.setattr("lumen.auth.resolve_project_authority", current_authority)
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
    executor["invoke"] = provider_errors.BadRequestError()
    executor["settle"] = "rejected"
    await _run_executor()
    [(result, usage)] = executor["checkpoints"]
    assert result["envelope"]["status_code"] == 400 and usage is None
    assert executor["invocations"] == 1 and executor["settled"] == 1
    assert executor["finished"][0]["error_code"] == "provider_rejected"


@pytest.mark.parametrize(
    "failure",
    [
        provider_errors.Timeout(),
        provider_errors.APIConnectionError(),
        provider_errors.InternalServerError(),
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
