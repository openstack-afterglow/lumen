"""litellm_client 토큰/비용 계산 + 스트리밍 usage 계측 폴백 단위 테스트.

litellm 의 로컬 계산(token_counter/cost_per_token)만 사용 — 네트워크 불요.
핵심 회귀 방지: 스트리밍이 usage 를 주지 않아도 과금이 0이 되지 않아야 한다.
"""

import json
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest

from lumen.services import litellm_client
from lumen.services.providers import chatgpt_transport, subscriptions
from lumen.services.providers.errors import ProviderSubscriptionError

_MODEL = "gpt-3.5-turbo"
_MESSAGES = [{"role": "user", "content": "안녕하세요, 오늘 날씨 어때요?"}]


class TestExtractUsage:
    def test_prefers_final_usage_dict(self):
        pt, ct = litellm_client.extract_usage(
            _MODEL, _MESSAGES, "맑습니다", {"prompt_tokens": 12, "completion_tokens": 3}
        )
        assert (pt, ct) == (12, 3)

    def test_prefers_final_usage_object(self):
        class _Usage:
            prompt_tokens = 20
            completion_tokens = 7

        pt, ct = litellm_client.extract_usage(_MODEL, _MESSAGES, "맑습니다", _Usage())
        assert (pt, ct) == (20, 7)

    def test_fallback_when_no_usage(self):
        pt, ct = litellm_client.extract_usage(_MODEL, _MESSAGES, "오늘은 맑고 따뜻합니다.", None)
        assert pt > 0
        assert ct > 0

    def test_fallback_when_usage_incomplete(self):
        pt, ct = litellm_client.extract_usage(_MODEL, _MESSAGES, "맑음", {"prompt_tokens": 5})
        assert pt > 0
        assert ct > 0


class TestCostFromUsage:
    def test_imported_prices_win_and_snapshot_is_exact(self, monkeypatch):
        monkeypatch.setattr(
            litellm_client,
            "_litellm_component_rates",
            lambda *_: (_ for _ in ()).throw(AssertionError("stored prices must avoid LiteLLM")),
        )
        cost = litellm_client.cost_from_usage(
            "openai/gpt-test",
            prompt_tokens=1000,
            completion_tokens=500,
            input_price_per_token=Decimal("0.000002"),
            output_price_per_token=Decimal("0.000008"),
            price_source="models.dev",
        )
        assert cost.raw_cost == Decimal("0.0060000000")
        assert cost.input_cost == Decimal("0.0020000000")
        assert cost.output_cost == Decimal("0.0040000000")
        assert cost.pricing_status == "priced"
        assert cost.pricing_snapshot["input"]["effective_price_per_million"] == "2.000000"
        assert cost.pricing_snapshot["output"]["source"] == "models.dev"

    def test_manual_free_rate_is_priced(self, monkeypatch):
        monkeypatch.setattr(
            litellm_client,
            "_litellm_component_rates",
            lambda *_: (_ for _ in ()).throw(AssertionError("explicit free prices must avoid LiteLLM")),
        )
        cost = litellm_client.cost_from_usage(
            _MODEL,
            prompt_tokens=10,
            completion_tokens=5,
            input_price_per_token=Decimal("0"),
            output_price_per_token=Decimal("0"),
            price_source="manual",
        )
        assert cost.raw_cost == Decimal("0")
        assert cost.pricing_status == "priced"

    def test_legacy_partial_stored_price_uses_component_fallback(self, monkeypatch):
        monkeypatch.setattr(
            litellm_client,
            "_litellm_component_rates",
            lambda *_: (Decimal("0.000001"), Decimal("0.000004")),
        )
        cost = litellm_client.cost_from_usage(
            _MODEL,
            prompt_tokens=100,
            completion_tokens=50,
            input_price_per_token=Decimal("0.000003"),
            output_price_per_token=None,
            price_source="manual",
        )
        assert cost.raw_cost == Decimal("0.0005000000")
        assert cost.pricing_status == "priced"
        assert cost.pricing_snapshot["input"]["source"] == "manual"
        assert cost.pricing_snapshot["output"]["source"] == "litellm"

    def test_fallback_failure_is_partial_or_unpriced(self, monkeypatch):
        monkeypatch.setattr(litellm_client, "_litellm_component_rates", lambda *_: (Decimal("0.000001"), None))
        partial = litellm_client.cost_from_usage(
            _MODEL,
            prompt_tokens=10,
            completion_tokens=10,
            input_price_per_token=None,
            output_price_per_token=None,
            price_source=None,
        )
        assert partial.pricing_status == "partial"

        monkeypatch.setattr(litellm_client, "_litellm_component_rates", lambda *_: (None, None))
        unpriced = litellm_client.cost_from_usage(
            _MODEL,
            prompt_tokens=0,
            completion_tokens=0,
            input_price_per_token=None,
            output_price_per_token=None,
            price_source=None,
        )
        assert unpriced.raw_cost == Decimal("0")
        assert unpriced.pricing_status == "unpriced"


class TestCountTokens:
    def test_messages_positive(self):
        assert litellm_client.count_tokens(_MODEL, messages=_MESSAGES) > 0

    def test_text_positive(self):
        assert litellm_client.count_tokens(_MODEL, text="hello world") > 0

    def test_litellm_fallback_receives_resolved_provider_type(self, monkeypatch):
        captured = {}

        def fallback(_model, _prompt, _completion, provider_type=None):
            captured["provider_type"] = provider_type
            return Decimal("0.000001"), Decimal("0.000002")

        monkeypatch.setattr(litellm_client, "_litellm_component_rates", fallback)
        cost = litellm_client.cost_from_usage(
            "anthropic.claude-3",
            prompt_tokens=1,
            completion_tokens=1,
            input_price_per_token=None,
            output_price_per_token=None,
            price_source=None,
            provider_type="bedrock",
        )
        assert cost.pricing_status == "priced"
        assert captured["provider_type"] == "bedrock"


class TestEffectiveDisplayPrices:
    def test_uses_same_provider_aware_litellm_lookup_as_charging(self, monkeypatch):
        captured = {}

        def fallback(model, prompt_tokens, completion_tokens, provider_type=None):
            captured.update(
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                provider_type=provider_type,
            )
            return Decimal("0.000002"), Decimal("0.000008")

        monkeypatch.setattr(litellm_client, "_litellm_component_rates", fallback)
        assert litellm_client.effective_prices_per_million("gpt-5.4", "openai") == (
            Decimal("2.000000"),
            Decimal("8.000000"),
        )
        assert captured == {
            "model": "gpt-5.4",
            "prompt_tokens": 1_000_000,
            "completion_tokens": 1_000_000,
            "provider_type": "openai",
        }

    def test_reads_bundled_deepseek_price_table_without_network(self):
        assert litellm_client.effective_prices_per_million("deepseek-chat", "deepseek") == (
            Decimal("0.28"),
            Decimal("0.42"),
        )
        assert litellm_client.effective_prices_per_million("deepseek-reasoner", "deepseek") == (
            Decimal("0.28"),
            Decimal("0.42"),
        )

    def test_resolves_canonical_perplexity_agent_prices_without_model_family_fallback(self):
        assert litellm_client.effective_prices_per_million("perplexity/perplexity/sonar", "perplexity") == (
            Decimal("0.250000"),
            Decimal("2.500000"),
        )
        assert litellm_client.effective_prices_per_million("perplexity/perplexity/glm-5.3", "perplexity") == (
            Decimal("1.400000"),
            Decimal("4.400000"),
        )
        assert litellm_client.effective_prices_per_million("perplexity/perplexity/glm-5.3-unlisted", "perplexity") == (
            None,
            None,
        )

    def test_sonar_agent_and_legacy_api_prices_do_not_alias(self):
        assert litellm_client.effective_prices_per_million(
            "perplexity/sonar", "perplexity", api_base="https://api.perplexity.ai/v1"
        ) == (Decimal("0.250000"), Decimal("2.500000"))
        assert litellm_client.effective_prices_per_million(
            "perplexity/sonar", "perplexity", api_base="https://api.perplexity.ai"
        ) == (Decimal("1.000000"), Decimal("1.000000"))
        assert (
            litellm_client.official_price_source("perplexity/sonar", "perplexity", api_base="https://api.perplexity.ai")
            is None
        )


class TestReasoningParams:
    """지원 모델에만 reasoning_effort 를 붙이는 gating(비용/동작 변화 방지)."""

    def test_supported_model_adds_effort(self, monkeypatch):
        import litellm

        monkeypatch.setattr(litellm, "supports_reasoning", lambda model: True)
        assert litellm_client._reasoning_params("claude-sonnet-5", "medium", "anthropic") == {
            "reasoning_effort": "medium"
        }

    def test_unsupported_model_returns_empty(self, monkeypatch):
        import litellm

        monkeypatch.setattr(litellm, "supports_reasoning", lambda model: False)
        assert litellm_client._reasoning_params("gpt-4o", "low", "openai") == {}

    def test_auto_omits_and_supported_levels_are_forwarded(self, monkeypatch):
        import litellm

        monkeypatch.setattr(litellm, "supports_reasoning", lambda model: True)
        assert litellm_client._reasoning_params("claude-sonnet-5", "", "anthropic") == {}
        assert litellm_client._reasoning_params("claude-sonnet-5", "auto", "anthropic") == {}
        assert litellm_client._reasoning_params("claude-sonnet-5", "off", "anthropic") == {}
        assert litellm_client._reasoning_params("claude-sonnet-5", "ultra", "anthropic") == {
            "reasoning_effort": "ultra"
        }
        assert litellm_client._reasoning_params("gpt-5.6-terra", "none", "openai") == {"reasoning_effort": "none"}
        assert litellm_client._reasoning_params("claude-sonnet-5", "unknown", "anthropic") == {}
        assert litellm_client._reasoning_params("claude-sonnet-5", None, "anthropic") == {}

    def test_supports_reasoning_error_is_safe(self, monkeypatch):
        import litellm

        def _boom(model):
            raise RuntimeError("db missing")

        monkeypatch.setattr(litellm, "supports_reasoning", _boom)
        assert litellm_client._reasoning_params("some-model", "low", None) == {}


@pytest.mark.asyncio
async def test_chatgpt_subscription_requires_provider_auth(monkeypatch):
    async def forbidden_completion(**kwargs):
        raise AssertionError("LiteLLM fallback must not run")

    import litellm

    monkeypatch.setattr(litellm, "acompletion", forbidden_completion)
    with pytest.raises(ProviderSubscriptionError) as exc_info:
        await litellm_client.acompletion(
            "chatgpt/gpt-5.2-codex",
            [{"role": "user", "content": "hello"}],
            custom_llm_provider="chatgpt",
        )
    assert exc_info.value.code == "subscription_auth_required"


@pytest.mark.asyncio
async def test_chatgpt_subscription_resolves_request_local_credential(monkeypatch):
    captured = {}
    provider_auth = {"provider_id": 7, "generation": 3, "auth_mode": "chatgpt_device"}

    async def resolve(ref):
        assert ref is provider_auth
        return {"access_token": "request-token", "account_id": "account-7", "_fingerprint": "fingerprint"}

    async def complete(model, messages, *, credential, stream, optional_params):
        captured.update(
            model=model,
            messages=messages,
            credential=credential,
            stream=stream,
            optional_params=optional_params,
        )
        return SimpleNamespace(choices=[])

    monkeypatch.setattr(subscriptions, "resolve_subscription_credential", resolve)
    monkeypatch.setattr(chatgpt_transport, "acompletion", complete)

    result = await litellm_client.acompletion(
        "chatgpt/gpt-5.2-codex",
        [{"role": "user", "content": "hello"}],
        custom_llm_provider="chatgpt",
        provider_auth=provider_auth,
        max_tokens=100,
        temperature=0.2,
    )

    assert result.choices == []
    assert captured["model"] == "chatgpt/gpt-5.2-codex"
    assert captured["credential"]["access_token"] == "request-token"
    assert captured["stream"] is False
    assert captured["optional_params"] == {"max_tokens": 100, "temperature": 0.2}


@pytest.mark.asyncio
async def test_anthropic_subscription_pins_oauth_transport_parameters(monkeypatch):
    captured = {}
    provider_auth = {"provider_id": 8, "generation": 4, "auth_mode": "anthropic_subscription"}
    token = "sk-ant-oat01-request-local-token"

    async def resolve(ref):
        assert ref is provider_auth
        return {"access_token": token, "_fingerprint": "fingerprint"}

    async def complete(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(choices=[])

    import litellm
    from litellm.llms.anthropic.common_utils import optionally_handle_anthropic_oauth

    monkeypatch.setattr(subscriptions, "resolve_subscription_credential", resolve)
    monkeypatch.setattr(litellm, "acompletion", complete)

    await litellm_client.acompletion(
        "anthropic-subscription/claude-opus-4-1",
        [{"role": "user", "content": "hello"}],
        custom_llm_provider="anthropic",
        provider_auth=provider_auth,
        extra={
            "api_key": "attacker-key",
            "api_base": "https://evil.example",
            "custom_llm_provider": "openai",
            "litellm_logging_obj": object(),
        },
    )

    headers, _ = optionally_handle_anthropic_oauth({"x-api-key": token}, captured["api_key"])
    assert captured["model"] == "claude-opus-4-1"
    assert captured["api_key"] == token
    assert captured["api_base"] == "https://api.anthropic.com"
    assert captured["custom_llm_provider"] == "anthropic"
    assert captured["litellm_logging_obj"].model_call_details["litellm_params"]["no_log"] is True
    assert headers["authorization"] == f"Bearer {token}"
    assert "x-api-key" not in headers
    assert "oauth" in headers["anthropic-beta"]


@pytest.mark.asyncio
async def test_native_anthropic_forwards_claude_code_fields_and_protocol_headers(monkeypatch):
    captured = {}

    async def create(**kwargs):
        captured.update(kwargs)
        return {"type": "message", "content": []}

    import litellm

    monkeypatch.setattr(litellm.anthropic.messages, "acreate", create)
    await litellm_client.aanthropic_messages(
        model="claude-sonnet",
        messages=[{"role": "user", "content": "hello"}],
        max_tokens=128,
        stream=False,
        api_base="https://anthropic.example/v1",
        api_key="provider-secret",
        custom_llm_provider="anthropic",
        provider_auth=None,
        context_management={"edits": [{"type": "clear_thinking_20251015", "keep": "all"}]},
        output_config={"effort": "high"},
        anthropic_headers={
            "anthropic-beta": "context-management-2025-06-27",
            "anthropic-version": "2023-06-01",
        },
    )

    assert captured["context_management"]["edits"][0]["keep"] == "all"
    assert captured["output_config"] == {"effort": "high"}
    assert captured["extra_headers"] == {
        "anthropic-beta": "context-management-2025-06-27",
        "anthropic-version": "2023-06-01",
    }
    assert captured["api_key"] == "provider-secret"


@pytest.mark.asyncio
async def test_subscription_stream_auth_failure_marks_only_credential_fingerprint(monkeypatch):
    provider_auth = {"provider_id": 7, "generation": 3, "auth_mode": "chatgpt_device"}
    marked = []

    async def resolve(_ref):
        return {"access_token": "request-token", "account_id": "account-7", "_fingerprint": "fingerprint"}

    async def failed_stream():
        raise ProviderSubscriptionError("subscription_auth_required", 502)
        yield

    async def complete(*args, **kwargs):
        return failed_stream()

    async def mark(ref, fingerprint):
        marked.append((ref, fingerprint))

    monkeypatch.setattr(subscriptions, "resolve_subscription_credential", resolve)
    monkeypatch.setattr(chatgpt_transport, "acompletion", complete)
    monkeypatch.setattr(subscriptions, "_mark_subscription_credential_rejected", mark)

    stream = await litellm_client.acompletion_stream(
        "chatgpt/gpt-5.2-codex",
        [{"role": "user", "content": "hello"}],
        custom_llm_provider="chatgpt",
        provider_auth=provider_auth,
    )
    with pytest.raises(ProviderSubscriptionError):
        async for _ in stream:
            pass
    assert marked == [(provider_auth, "fingerprint")]


def test_chatgpt_subscription_tokenizer_receives_bare_model_name(monkeypatch):
    captured = {}

    def token_counter(**kwargs):
        captured.update(kwargs)
        return 17

    monkeypatch.setattr("litellm.token_counter", token_counter)

    tokens = litellm_client.count_tokens(
        "chatgpt/gpt-5.2-codex",
        messages=[{"role": "user", "content": "hello"}],
    )

    assert tokens == 17
    assert captured["model"] == "gpt-5.2-codex"


def test_chatgpt_subscription_missing_static_prices_remains_unpriced(monkeypatch):
    import litellm

    monkeypatch.setitem(
        litellm.model_cost,
        "chatgpt/gpt-5.2-codex",
        {"litellm_provider": "chatgpt", "mode": "responses"},
    )
    monkeypatch.setattr(
        litellm,
        "cost_per_token",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("runtime pricing probe is forbidden")),
    )

    assert litellm_client.effective_prices_per_million(
        "chatgpt/gpt-5.2-codex",
        "chatgpt",
    ) == (None, None)


@pytest.mark.asyncio
async def test_perplexity_requires_explicit_route_credential(monkeypatch):
    async def forbidden_completion(**_kwargs):
        raise AssertionError("Perplexity must not use a process-global credential")

    monkeypatch.setattr("litellm.acompletion", forbidden_completion)

    with pytest.raises(ValueError, match="perplexity_credentials_missing"):
        await litellm_client.acompletion(
            "perplexity/sonar",
            [{"role": "user", "content": "hello"}],
            custom_llm_provider="perplexity",
        )


@pytest.mark.asyncio
async def test_perplexity_agent_hits_responses_http_boundary_with_public_model(monkeypatch):
    captured = {}

    class Sender:
        async def post(self, *, url, headers, **kwargs):
            captured.update(url=url, headers=headers, body=kwargs.get("json"))
            return httpx.Response(
                200,
                request=httpx.Request("POST", url),
                json={
                    "id": "resp_test",
                    "object": "response",
                    "created_at": 1,
                    "status": "completed",
                    "model": "perplexity/sonar",
                    "output": [
                        {
                            "id": "msg_test",
                            "type": "message",
                            "status": "completed",
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": "hello",
                                    "annotations": [],
                                    "logprobs": [],
                                }
                            ],
                        }
                    ],
                    "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                    "error": None,
                    "incomplete_details": None,
                    "instructions": None,
                    "metadata": {},
                },
            )

    sender = Sender()
    monkeypatch.setattr(
        "litellm.llms.custom_httpx.llm_http_handler.get_async_httpx_client",
        lambda **_kwargs: sender,
    )

    result = await litellm_client.acompletion(
        "perplexity/perplexity/sonar",
        [{"role": "user", "content": "hello"}],
        api_base="https://api.perplexity.ai/v1",
        api_key="route-key",
        custom_llm_provider="perplexity",
        tools=[{"type": "function", "function": {"name": "lookup"}}],
    )

    assert result.choices[0].message.content == "hello"
    assert captured["url"] == "https://api.perplexity.ai/v1/responses"
    assert captured["headers"]["Authorization"] == "Bearer route-key"
    assert captured["body"]["model"] == "perplexity/sonar"


@pytest.mark.asyncio
async def test_perplexity_router_pins_chat_completions_and_preserves_canonical_model(monkeypatch):
    captured = {}

    async def completion(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(choices=[])

    monkeypatch.setattr("litellm.acompletion", completion)

    await litellm_client.acompletion_stream(
        "perplexity/perplexity/kimi-k3",
        [{"role": "user", "content": "hello"}],
        api_base="https://api.perplexity.ai/router",
        api_key="route-key",
        custom_llm_provider="perplexity",
        extra={"provider": "must-not-forward"},
    )

    assert captured["model"] == "openai/perplexity/kimi-k3"
    assert captured["api_base"] == "https://api.perplexity.ai/router/v1"
    assert captured["custom_llm_provider"] == "openai"
    assert captured["_skip_responses_api_bridge"] is True
    assert captured["stream"] is True
    assert captured["stream_options"] == {"include_usage": True}
    assert "provider" not in captured


@pytest.mark.asyncio
async def test_perplexity_legacy_sonar_keeps_existing_chat_completion_route(monkeypatch):
    captured = {}

    async def completion(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(choices=[])

    monkeypatch.setattr("litellm.acompletion", completion)

    await litellm_client.acompletion(
        "perplexity/sonar",
        [{"role": "user", "content": "hello"}],
        api_base=None,
        api_key="route-key",
        custom_llm_provider="perplexity",
    )

    assert captured["model"] == "perplexity/sonar"
    assert captured["custom_llm_provider"] == "perplexity"
    assert captured["api_key"] == "route-key"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "builtin_search"),
    [("perplexity/perplexity/sonar", True), ("perplexity/openai/gpt-4.1", False)],
)
async def test_perplexity_agent_preserves_explicit_search_and_builtin_sonar(monkeypatch, model, builtin_search):
    """Hosted search coexists with function tools; built-in Sonar adds no duplicate search tool."""
    captured = {}

    class Sender:
        async def post(self, *, url, headers, **kwargs):
            captured.update(url=url, body=kwargs.get("json"))
            return httpx.Response(
                200,
                request=httpx.Request("POST", url),
                json={
                    "id": "resp_native",
                    "object": "response",
                    "created_at": 1,
                    "status": "completed",
                    "model": "perplexity/sonar",
                    "output": [
                        {
                            "id": "msg_native",
                            "type": "message",
                            "status": "completed",
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": "서울은 맑습니다",
                                    "annotations": [
                                        {
                                            "type": "url_citation",
                                            "url": "https://news.example/seoul",
                                            "title": "Seoul weather",
                                            "start_index": 0,
                                            "end_index": 2,
                                        }
                                    ],
                                    "logprobs": [],
                                }
                            ],
                        }
                    ],
                    "usage": {"input_tokens": 3, "output_tokens": 5, "total_tokens": 8},
                    "error": None,
                    "incomplete_details": None,
                    "instructions": None,
                    "metadata": {},
                },
            )

    sender = Sender()
    monkeypatch.setattr(
        "litellm.llms.custom_httpx.llm_http_handler.get_async_httpx_client",
        lambda **_kwargs: sender,
    )

    result = await litellm_client.acompletion(
        model,
        [{"role": "user", "content": "오늘 서울 날씨"}],
        api_base=None if builtin_search else "https://api.perplexity.ai/v1",
        api_key="route-key",
        custom_llm_provider="perplexity",
        native_web_search={"search_context_size": "medium"},
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "lookup",
                    "description": "Look up a named record.",
                    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
                },
            }
        ],
    )

    assert "web_search_options" not in captured["body"]
    assert [tool for tool in captured["body"]["tools"] if tool.get("type") == "function"] == [
        {
            "type": "function",
            "name": "lookup",
            "description": "Look up a named record.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            "strict": True,
        }
    ]
    search_tools = [tool for tool in captured["body"]["tools"] if tool.get("type") == "web_search"]
    assert search_tools == ([] if builtin_search else [{"type": "web_search", "search_context_size": "medium"}])
    assert result.choices[0].message.annotations == [
        {
            "type": "url_citation",
            "url": "https://news.example/seoul",
            "title": "Seoul weather",
            "start_index": 0,
            "end_index": 2,
        }
    ]


@pytest.mark.asyncio
async def test_native_responses_forwards_prompt_cache_key(monkeypatch):
    import litellm

    captured = {}

    async def responses(**kwargs):
        captured.update(kwargs)
        return {"id": "resp_codex", "object": "response", "status": "completed", "output": []}

    monkeypatch.setattr(litellm, "aresponses", responses)
    result = await litellm_client.aresponses(
        model="openai/gpt-test",
        input=[{"role": "user", "content": "hello"}],
        stream=False,
        api_base="https://provider.example/v1",
        api_key="secret",
        custom_llm_provider="openai",
        provider_auth=None,
        prompt_cache_key="session:codex",
    )

    assert result["id"] == "resp_codex"
    assert captured["prompt_cache_key"] == "session:codex"


def test_perplexity_agent_omits_strict_for_open_object_schema():
    tools = [
        {
            "type": "function",
            "function": {
                "name": "lookup",
                "description": "Look up a record.",
                "parameters": {
                    "type": "object",
                    "properties": {"required_id": {"type": "string"}, "optional_limit": {"type": "integer"}},
                    "required": ["required_id"],
                    "additionalProperties": False,
                },
            },
        }
    ]

    normalized = litellm_client._perplexity_agent_tools(tools)

    assert normalized == tools


@pytest.mark.asyncio
async def test_perplexity_agent_stream_preserves_native_url_citations(monkeypatch):
    """설치된 브리지는 스트림에서 annotation 을 버리므로 래퍼가 다시 붙여야 한다."""
    annotation = {
        "type": "url_citation",
        "url": "https://news.example/busan",
        "title": "Busan",
        "start_index": 2,
        "end_index": 4,
    }
    events = [
        {
            "type": "response.output_text.delta",
            "item_id": "msg_1",
            "output_index": 0,
            "content_index": 0,
            "delta": "부산",
        },
        {
            "type": "response.output_text.annotation.added",
            "item_id": "msg_1",
            "output_index": 0,
            "content_index": 0,
            "annotation_index": 0,
            "annotation": annotation,
        },
        {
            "type": "response.completed",
            "response": {
                "id": "resp_stream",
                "object": "response",
                "created_at": 1,
                "status": "completed",
                "model": "perplexity/sonar",
                "output": [
                    {
                        "id": "msg_1",
                        "type": "message",
                        "status": "completed",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "부산", "annotations": [annotation]}],
                    }
                ],
                "usage": {"input_tokens": 2, "output_tokens": 2, "total_tokens": 4},
                "error": None,
                "incomplete_details": None,
                "instructions": None,
                "metadata": {},
            },
        },
    ]
    body = "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events) + "data: [DONE]\n\n"

    class Sender:
        async def post(self, *, url, headers, **kwargs):
            return httpx.Response(
                200,
                request=httpx.Request("POST", url),
                headers={"content-type": "text/event-stream"},
                content=body.encode(),
            )

    sender = Sender()
    monkeypatch.setattr(
        "litellm.llms.custom_httpx.llm_http_handler.get_async_httpx_client",
        lambda **_kwargs: sender,
    )

    stream = await litellm_client.acompletion_stream(
        "perplexity/perplexity/sonar",
        [{"role": "user", "content": "부산 날씨"}],
        api_base="https://api.perplexity.ai/v1",
        api_key="route-key",
        custom_llm_provider="perplexity",
        native_web_search={"search_context_size": "medium"},
    )
    chunks = [chunk async for chunk in stream]

    assert "".join(choice.delta.content or "" for chunk in chunks for choice in chunk.choices) == "부산"
    streamed_annotations = [
        entry
        for chunk in chunks
        for choice in chunk.choices
        for entry in (getattr(choice.delta, "annotations", None) or [])
    ]
    assert annotation in streamed_annotations
