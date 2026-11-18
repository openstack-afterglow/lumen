"""Offline GPT-6.2 catalog, protocol, and hosted tool billing contracts."""

from contextlib import aclosing
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from lumen.models.api_requests import OpenAIChatRequest, ResponsesRequest
from lumen.services import completion_api as core
from lumen.services import credit, openai_tool_policy, provider_client, provider_metadata
from lumen.services.completion_format import prompt_token_details
from lumen.services.provider_errors import OpenAIUsageInvalid
from lumen.services.providers.pricing import _resolved_base_prices, _resolved_cache_prices, _resolved_context_tiers
from lumen.services.usage_breakdown import UsageBreakdown

MODEL = "gpt-6.2-sol"
MESSAGES = [{"role": "user", "content": "hello"}]
OWNER = {"user_id": "text-owner", "project_id": "test-project", "api_key_id": 3}


def route_for(model=MODEL):
    provider = SimpleNamespace(provider_type="openai", api_base=None)
    row = SimpleNamespace(model_kind="text", model_name=model, input_price=None, output_price=None)
    input_rate, output_rate, price_source, _ = _resolved_base_prices(row, provider)
    cache_rates, sources = _resolved_cache_prices(row, "openai", None)
    return {"model_name": model, "api_model_name": model, "provider_type": "openai", "provider_name": "OpenAI",
            "margin_multiplier": Decimal(1), "price_source": price_source, "input_price_per_token": input_rate,
            "output_price_per_token": output_rate, "cache_price_sources": sources,
            **cache_rates, **_resolved_context_tiers(row, "openai", None)}


def input_details():
    return {"cached_tokens": 100, "cache_write_tokens": 500,
            "cache_write_tokens_details": {"ttl_30m_tokens": 200, "ttl_12h_tokens": 300}}


@pytest.mark.parametrize("model,base,expanded", [
    (MODEL, ("2.00", "10.00", "0.10", "2.50", "5.00"), ("4.00", "15.00", "0.20", "5.00", "10.00")),
    ("gpt-6.2-astra", ("8.00", "40.00", "0.40", "10.00", "20.00"), ("16.00", "60.00", "0.80", "20.00", "40.00")),
])
def test_exact_catalog_contract(model, base, expanded):
    entry = provider_metadata.native_catalog_entry(model, "openai")
    assert (entry["context_window"], entry["max_input_tokens"], entry["max_output_tokens"]) == (1050000, 922000, 128000)
    assert entry["reasoning_options"] == [{"type": "effort", "values": ["low", "medium", "high"]}]
    token = entry["token_pricing"]
    fields = ("input", "output", "cache_read", "cache_write", "cache_write_long")
    assert tuple(token[key] for key in fields) == base
    assert tuple(token["long_context"][key] for key in fields) == expanded
    assert token["long_context"]["threshold_tokens"] == 272000
    assert (token["cache_write_ttl"], token["cache_write_long_ttl"]) == ("30m", "12h")
    assert not provider_metadata.native_catalog_entry(model, "openai", "https://custom.invalid/v1")


@pytest.mark.parametrize("protocol", ["chat", "responses"])
@pytest.mark.parametrize("total,rates", [(272000, (2, 10, "0.10", "2.50", 5)), (272001, (4, 15, "0.20", 5, 10))])
def test_mixed_report_prices_each_token_once(protocol, total, rates):
    if protocol == "chat":
        usage = UsageBreakdown.from_runtime({"prompt_tokens": total, "completion_tokens": 50,
                                             "prompt_tokens_details": input_details()})
    else:
        usage = UsageBreakdown.from_responses({"input_tokens": total, "output_tokens": 50,
                                               "input_tokens_details": input_details()})
    assert usage is not None and not usage.modality_usage_invalid
    quantities = (total - 600, 50, 100, 200, 300)
    assert quantities[0] == usage.uncached_input_tokens
    assert sum(usage.cache_fields().values()) + usage.uncached_input_tokens == total
    assert prompt_token_details({**usage.cache_fields(), "openai_cache_ttl": True}) == input_details()
    expected = sum(Decimal(rate) * quantity / 1000000 for rate, quantity in zip(rates, quantities, strict=True))
    direct = provider_metadata.cost_from_usage(
        MODEL, total, 50, input_price_per_token=None, output_price_per_token=None, price_source=None,
        provider_type="openai", breakdown=usage,
    )
    route = route_for()
    batch = credit.usage_cost_from_pricing_snapshot(route, prompt_tokens=total, completion_tokens=50, breakdown=usage)
    assert direct.raw_cost == batch.raw_cost == expected
    assert (
        direct.input_cost + direct.output_cost + direct.cache_read_cost
        + direct.cache_creation_5m_cost + direct.cache_creation_1h_cost
    ) == expected


def test_clearing_manual_base_rates_activates_long_context():
    provider = SimpleNamespace(provider_type="openai", api_base=None)
    row = SimpleNamespace(model_kind="text", model_name=MODEL, input_price=Decimal("0.000002"),
                          output_price=Decimal("0.000010"))
    for manual, expected in [(True, Decimal("0.544102")), (False, Decimal("1.088154"))]:
        if not manual:
            row.input_price = row.output_price = None
        input_rate, output_rate, source, _ = _resolved_base_prices(row, provider)
        tiers = _resolved_context_tiers(row, "openai", None)
        cost = provider_metadata.cost_from_usage(
            MODEL, 272001, 10, input_price_per_token=input_rate, output_price_per_token=output_rate,
            price_source=source, **tiers,
        )
        assert cost.raw_cost == expected


@pytest.mark.parametrize("delta", [
    {"cache_write_tokens_details": None}, {"cache_write_tokens": 501}, {"cached_tokens": 501},
    {"cache_write_tokens_details": {"ttl_30m_tokens": -1, "ttl_12h_tokens": 501}},
])
def test_bad_cache_evidence_is_not_repaired(delta):
    details = {**input_details(), **delta}
    for breakdown in (
        UsageBreakdown.from_responses({"input_tokens": 1000, "output_tokens": 0, "input_tokens_details": details}),
        UsageBreakdown.from_runtime({"prompt_tokens": 1000, "completion_tokens": 0, "prompt_tokens_details": details}),
    ):
        assert breakdown is not None and breakdown.modality_usage_invalid


def test_no_unpublished_restricted_long_write_rate():
    route = route_for("gpt-6-cyber")
    with pytest.raises(ValueError, match="unpublished"):
        credit.usage_cost_from_pricing_snapshot(
            route, prompt_tokens=272001, completion_tokens=0,
            breakdown=UsageBreakdown(272001, 0, cache_creation_1h_input_tokens=1),
        )
    rosalind = provider_metadata.native_catalog_entry("gpt-6-rosalind", "openai")
    assert rosalind["token_pricing"]["input"] == "10.00"
    assert rosalind["token_pricing"]["long_context"]["cache_write_long"] == "50.00"
    assert "does not imply account entitlement" in rosalind["notes"]


@pytest.mark.parametrize("effort", ["none", "minimal", "xhigh", "max"])
def test_explicit_unpublished_reasoning_effort_rejected(effort):
    for options, validator in [({"reasoning_effort": effort}, core.chat_provider_options),
                               ({"reasoning": {"effort": effort}}, core.responses_provider_options)]:
        with pytest.raises(core.CompletionError) as failure:
            validator(options, resolved=route_for())
        assert (failure.value.status_code, failure.value.code) == (422, "reasoning_effort_unsupported")


def test_cache_and_chat_fields_serialize_and_normalize():
    body = OpenAIChatRequest.model_validate({
        "model": MODEL, "messages": MESSAGES, "max_completion_tokens": 32, "reasoning_effort": "high",
        "service_tier": "auto", "prompt_cache_key": "cache-key", "prompt_cache_retention": "24h",
        "response_format": {"type": "json_schema", "json_schema": {"name": "shape", "schema": {"type": "object"}}},
    })
    options = core.chat_provider_options(body.provider_options(), resolved=route_for())
    assert options["service_tier"] == "default"
    assert options["prompt_cache_options"] == {"ttl": "30m"}
    assert "prompt_cache_retention" not in options
    assert options["prompt_cache_key"] == "cache-key"
    assert options["max_completion_tokens"] == 32
    assert options["response_format"]["json_schema"]["schema"] == {"type": "object"}
    assert options["reasoning_effort"] == "high"
    with pytest.raises(core.CompletionError) as failure:
        core.responses_provider_options({"service_tier": "priority"}, resolved=route_for())
    assert (failure.value.status_code, failure.value.code) == (422, "service_tier_unsupported")


@pytest.mark.parametrize("item", [
    {"type": "item_reference", "id": "private-reference"},
    {"id": "private-reference"},
    {"type": None, "id": "private-reference"},
    {"role": "user", "content": [{"arbitrary": {"type": None, "id": "private-reference"}}]},
    {"role": "user", "content": [{"arbitrary": {"type": "item_reference", "id": "private-reference"}}]},
    {"role": "user", "content": [{"data": [{"type": "shell_call", "environment": {"container_id": "private-reference"}}]}]},
    {"role": "user", "content": [{"type": "input_file", "file_id": "private-reference"}]},
    {"type": "mcp_approval_response", "approval_request_id": "private-reference", "approve": True},
    {"role": "user", "content": [{"arbitrary": [{"approval_request_id": "private-reference"}]}]},
    {"type": "tool_search_output", "tools": [{"arbitrary": {"approval_request_id": "private-reference"}}]},
    {"role": "user", "content": [{"arbitrary": {"skill_id": "private-reference"}}]},
])
async def test_provider_references_rejected_before_reservation(item, monkeypatch):
    reserve, dispatch = AsyncMock(), AsyncMock()
    monkeypatch.setattr(core.stateless_credit, "reserve", reserve)
    monkeypatch.setattr(provider_client, "aresponses", dispatch)
    with pytest.raises(core.CompletionError) as failure:
        await core.complete_responses(resolved=route_for(), input=[item], stream=False, options={}, **OWNER)
    assert (failure.value.status_code, failure.value.code) == (422, "provider_reference_unsupported")
    assert "private-reference" not in failure.value.message
    reserve.assert_not_awaited()
    dispatch.assert_not_awaited()


async def test_responses_preserves_complete_inline_history(monkeypatch):
    history = [
        {"role": "user", "content": "Look it up."},
        {"type": "function_call", "id": "fc_inline", "call_id": "call_inline", "name": "lookup", "arguments": "{}"},
        {"type": "function_call_output", "id": "fco_inline", "call_id": "call_inline", "output": "Found it."},
    ]
    upstream = AsyncMock(return_value={
        "status": "completed", "service_tier": "default", "output": [],
        "usage": {"input_tokens": 10, "output_tokens": 1},
    })
    monkeypatch.setattr(core, "_authorize_completion", AsyncMock())
    monkeypatch.setattr(core.stateless_credit, "reserve", AsyncMock())
    monkeypatch.setattr(core.stateless_credit, "settle", AsyncMock(return_value=Decimal("0.1")))
    monkeypatch.setattr(provider_client, "aresponses", upstream)
    await core.complete_responses(resolved=route_for(), input=history, stream=False, options={}, **OWNER)
    assert upstream.await_args.kwargs["input"] == history


def test_prompt_cache_comparison_reference_rejected():
    body = ResponsesRequest.model_validate({"model": MODEL, "input": "test",
                                            "prompt_cache_options": {"mode": "explicit", "prewarm": True,
                                                                     "comparison_response_id": "private-reference"}})
    with pytest.raises(core.CompletionError) as failure:
        core.responses_provider_options(body.model_dump(include={"prompt_cache_options"}, exclude_none=True),
                                        resolved=route_for())
    assert (failure.value.status_code, failure.value.code) == (422, "provider_reference_unsupported")
    assert "private-reference" not in failure.value.message


TOOLS = [
    {"type": "web_search"}, {"type": "code_interpreter", "container": {"type": "auto", "memory_limit": "4g"}},
    {"type": "shell", "environment": {"type": "container_auto", "memory_limit": "4g"}},
]
OUTPUT = [
    {"type": "web_search_call"},
    {"type": "code_interpreter_call", "container_id": "first"},
    {"type": "code_interpreter_call", "container_id": "first"},
    {"type": "shell_call", "environment": {"type": "container_reference", "container_id": "second"}},
    {"type": "shell_call_output"},
]


@pytest.mark.parametrize("tool", [
    {"type": "file_search"}, {"type": "image_generation"}, {"type": "skill"},
    {"type": "code_interpreter", "container": "existing"},
    {"type": "code_interpreter", "container": {"type": "auto", "file_ids": ["existing"]}},
    {"type": "shell", "environment": {"type": "container_reference", "container_id": "existing"}},
    {"type": "shell", "environment": {"type": "container_auto", "skills": [{"type": "skill_reference"}]}},
    {"type": "mcp", "connector_id": "connector"},
    {"type": "mcp", "server_url": "https://mcp.example.test"},
    {"type": "mcp", "server_url": "https://mcp.example.test", "require_approval": "always"},
    {"type": "mcp", "server_url": "https://mcp.example.test", "require_approval": "never", "tunnel_id": "existing"},
])
def test_unsupported_hosted_tools(tool):
    with pytest.raises(openai_tool_policy.UnsupportedHostedTool):
        openai_tool_policy.admit("test", {"tools": [tool]}, metered=True)


def test_token_only_tools_and_bounded_fees():
    tools = [{"type": kind} for kind in ("function", "custom", "computer_use", "apply_patch", "tool_search")]
    tools.append({"type": "mcp", "server_url": "https://mcp.example.test", "require_approval": "never"})
    options = {"tools": tools + TOOLS}
    meter = openai_tool_policy.admit("test", options, metered=True)
    assert options["max_tool_calls"] == 10
    assert meter.bound_usd == Decimal("1.30")
    assert meter.charges(OUTPUT) == {"web_search": {"calls": 1, "cost": "0.01"},
                                      "container": {"blocks": 2, "memory_limit": "4g", "cost": "0.24"}}
    with pytest.raises(OpenAIUsageInvalid):
        meter.charges([{"type": "shell_call"}])
    with pytest.raises(OpenAIUsageInvalid):
        meter.charges([{"type": "web_search_call"}] * 11)
    with pytest.raises(openai_tool_policy.UnsupportedHostedTool):
        openai_tool_policy.admit("test", {"tools": TOOLS, "max_tool_calls": 21}, metered=True)


@pytest.mark.parametrize("kind", ["additional_tools", "tool_search_output"])
def test_nested_tools_cannot_bypass_top_level_policy(kind):
    hidden = {"type": kind, "tools": [{"type": "image_generation"}]}
    with pytest.raises(openai_tool_policy.UnsupportedHostedTool):
        openai_tool_policy.admit([{"role": "user", "content": [hidden]}], {}, metered=True)


def test_legacy_cache_spelling_and_responses_prewarm():
    options = {"prompt_cache_retention": "in_memory"}
    assert core.chat_provider_options(options, resolved=route_for())["prompt_cache_options"] == {"ttl": "30m"}
    body = ResponsesRequest.model_validate({
        "model": MODEL, "input": "test", "prompt_cache_options": {"mode": "explicit", "prewarm": True, "ttl": "12h"},
    })
    options = body.model_dump(include={"prompt_cache_options"}, exclude_none=True)
    assert core.responses_provider_options(options, resolved=route_for())["prompt_cache_options"] == {
        "mode": "explicit", "prewarm": True, "ttl": "12h",
    }


@pytest.mark.parametrize("stream", [False, True])
async def test_chat_options_reach_provider_and_cache_split_is_settled(monkeypatch, stream):
    backend = provider_client.provider_object({"service_tier": "default",
        "choices": [{"message": {"content": "test"}, "delta": {"content": "test"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1000, "completion_tokens": 10, "prompt_tokens_details": input_details()}})

    async def chunks():
        yield backend

    settle = AsyncMock(return_value=Decimal("1"))
    send = AsyncMock(return_value=chunks() if stream else backend)
    monkeypatch.setattr(core, "_authorize_completion", AsyncMock())
    monkeypatch.setattr(core.stateless_credit, "reserve", AsyncMock())
    monkeypatch.setattr(core.stateless_credit, "settle", settle)
    monkeypatch.setattr(provider_client, "acompletion_stream" if stream else "acompletion", send)
    invoke = core.complete_stream if stream else core.complete_once
    result = await invoke(resolved=route_for(), messages=MESSAGES, temperature=None, max_tokens=32,
                          options={"max_completion_tokens": 32, "service_tier": "auto", "reasoning_effort": "low",
                                   "prompt_cache_options": {"ttl": "12h"}, "response_format": {"type": "json_object"}},
                          **OWNER)
    if stream:
        result = [event async for event in result][-1]
    assert send.await_args.kwargs["extra"]["service_tier"] == "default"
    assert send.await_args.kwargs["extra"]["response_format"] == {"type": "json_object"}
    assert send.await_args.kwargs["extra"]["prompt_cache_options"] == {"ttl": "12h"}
    assert result["prompt_tokens"] == 1000 and prompt_token_details(result) == input_details()
    settle.assert_awaited_once()
    assert settle.await_args.kwargs["usage_cost"].raw_cost == Decimal("0.00291")


async def test_distinct_input_limit_rejects_before_send(monkeypatch):
    monkeypatch.setattr(core, "_authorize_completion", AsyncMock())
    monkeypatch.setattr(
        provider_metadata, "count_context_tokens",
        lambda *_args: provider_metadata.ContextTokenCount(tokens=922001, measurement="estimated"),
    )
    reserve, send = AsyncMock(), AsyncMock()
    monkeypatch.setattr(core.stateless_credit, "reserve", reserve)
    monkeypatch.setattr(provider_client, "acompletion", send)
    with pytest.raises(core.CompletionError) as failure:
        await core.complete_once(resolved=route_for(), messages=MESSAGES, temperature=None, max_tokens=32, **OWNER)
    assert (failure.value.status_code, failure.value.code) == (422, "context_length_exceeded")
    reserve.assert_not_awaited()
    send.assert_not_awaited()


@pytest.mark.parametrize("outcome", ["settled", "tier", "truncated"])
async def test_hosted_sse_uses_terminal_evidence(monkeypatch, outcome):
    async def response():
        yield {"type": "response.output_text.delta", "delta": "preview"}
        if outcome != "truncated":
            yield {
                "type": "response.completed",
                "response": {
                    "status": "completed", "service_tier": "default" if outcome == "settled" else "ultrafast",
                    "output": [{"type": "web_search_call"}],
                    "usage": {"input_tokens": 2000, "output_tokens": 10, "input_tokens_details": input_details()},
                },
            }

    reserve, settle, finish = AsyncMock(), AsyncMock(), AsyncMock()
    monkeypatch.setattr(core, "_authorize_completion", AsyncMock())
    monkeypatch.setattr(core.stateless_credit, "reserve", reserve)
    monkeypatch.setattr(core.stateless_credit, "settle", settle)
    monkeypatch.setattr(core.stateless_credit, "finish", finish)
    monkeypatch.setattr(provider_client, "aresponses", AsyncMock(return_value=response()))
    stream = await core.complete_responses(
        resolved=route_for(), input="test", stream=True,
        options={"tools": [{"type": "web_search"}], "max_tool_calls": 1}, **OWNER,
    )
    async with aclosing(stream):
        assert (await anext(stream))["type"] == "response.output_text.delta"
        if outcome == "settled":
            assert (await anext(stream))["type"] == "response.completed"
            settle.assert_awaited_once()
            assert settle.await_args.kwargs["usage_cost"].raw_cost == Decimal("0.01491")
            finish.assert_not_awaited()
        elif outcome == "tier":
            with pytest.raises(core.CompletionError, match="openai_service_tier_mismatch"):
                await anext(stream)
        else:
            with pytest.raises(StopAsyncIteration):
                await anext(stream)
    if outcome != "settled":
        settle.assert_not_awaited()
        finish.assert_awaited_once_with(reserve.await_args.kwargs["event_id"], uncertain=True)
