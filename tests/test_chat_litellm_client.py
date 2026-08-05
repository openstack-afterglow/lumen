"""litellm_client 토큰/비용 계산 + 스트리밍 usage 계측 폴백 단위 테스트.

litellm 의 로컬 계산(token_counter/cost_per_token)만 사용 — 네트워크 불요.
핵심 회귀 방지: 스트리밍이 usage 를 주지 않아도 과금이 0이 되지 않아야 한다.
"""

from decimal import Decimal

from lumen.services import litellm_client

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
