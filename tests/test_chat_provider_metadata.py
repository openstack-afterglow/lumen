"""Offline token estimation and exact Decimal accounting regressions.

Provider-reported usage wins; missing reports retain a local estimate, and
missing catalog prices must remain unknown rather than silently free.
"""

from decimal import Decimal

from lumen.services import provider_metadata
from lumen.services.provider_client import ProviderObject

_MODEL = "gpt-3.5-turbo"
_MESSAGES = [{"role": "user", "content": "안녕하세요, 오늘 날씨 어때요?"}]


class TestExtractUsage:
    def test_prefers_final_usage_dict(self):
        pt, ct = provider_metadata.extract_usage(
            _MODEL, _MESSAGES, "맑습니다", {"prompt_tokens": 12, "completion_tokens": 3}
        )
        assert (pt, ct) == (12, 3)

    def test_prefers_final_usage_object(self):
        usage = ProviderObject(prompt_tokens=20, completion_tokens=7)
        pt, ct = provider_metadata.extract_usage(_MODEL, _MESSAGES, "맑습니다", usage)
        assert (pt, ct) == (20, 7)

    def test_fallback_when_no_usage(self):
        pt, ct = provider_metadata.extract_usage(_MODEL, _MESSAGES, "오늘은 맑고 따뜻합니다.", None)
        assert pt > 0
        assert ct > 0

    def test_fallback_when_usage_incomplete(self):
        pt, ct = provider_metadata.extract_usage(_MODEL, _MESSAGES, "맑음", {"prompt_tokens": 5})
        assert pt > 0
        assert ct > 0


class TestCostFromUsage:
    def test_imported_prices_win_and_snapshot_is_exact(self, monkeypatch):
        monkeypatch.setattr(
            provider_metadata,
            "_catalog_component_rates",
            lambda *_: (_ for _ in ()).throw(AssertionError("stored prices must avoid catalog fallback")),
        )
        cost = provider_metadata.cost_from_usage(
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
            provider_metadata,
            "_catalog_component_rates",
            lambda *_: (_ for _ in ()).throw(AssertionError("explicit free prices must avoid catalog fallback")),
        )
        cost = provider_metadata.cost_from_usage(
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
            provider_metadata,
            "_catalog_component_rates",
            lambda *_: (Decimal("0.000001"), Decimal("0.000004")),
        )
        cost = provider_metadata.cost_from_usage(
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
        monkeypatch.setattr(provider_metadata, "_catalog_component_rates", lambda *_: (Decimal("0.000001"), None))
        partial = provider_metadata.cost_from_usage(
            _MODEL,
            prompt_tokens=10,
            completion_tokens=10,
            input_price_per_token=None,
            output_price_per_token=None,
            price_source=None,
        )
        assert partial.pricing_status == "partial"

        monkeypatch.setattr(provider_metadata, "_catalog_component_rates", lambda *_: (None, None))
        unpriced = provider_metadata.cost_from_usage(
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
        assert provider_metadata.count_tokens(_MODEL, messages=_MESSAGES) > 0

    def test_text_positive(self):
        assert provider_metadata.count_tokens(_MODEL, text="hello world") > 0

    def test_catalog_prices_are_scoped_to_resolved_provider_type(self, monkeypatch):
        monkeypatch.setattr(provider_metadata, "_catalog", lambda: {
            "anthropic/shared-model": {
                "litellm_provider": "anthropic", "input_cost_per_token": "0.000009",
                "output_cost_per_token": "0.000009",
            },
            "bedrock/shared-model": {
                "litellm_provider": "bedrock", "input_cost_per_token": "0.000001",
                "output_cost_per_token": "0.000002",
            },
        })
        cost = provider_metadata.cost_from_usage(
            "shared-model", prompt_tokens=1, completion_tokens=1,
            input_price_per_token=None, output_price_per_token=None,
            price_source=None, provider_type="bedrock",
        )
        assert cost.raw_cost == Decimal("0.0000030000")
        assert cost.pricing_status == "priced"


class TestEffectiveDisplayPrices:
    def test_unknown_anthropic_id_is_unpriced_not_implicitly_free(self):
        model = "claude-onboarding-unlisted-2099-01-01"
        assert provider_metadata.effective_prices_per_million(model, "anthropic") == (None, None)
        usage = provider_metadata.cost_from_usage(
            model,
            10,
            5,
            input_price_per_token=None,
            output_price_per_token=None,
            price_source=None,
            provider_type="anthropic",
        )
        assert usage.pricing_status == "unpriced"

    def test_exact_free_catalog_entry_is_distinct_from_missing_price(self, monkeypatch):
        monkeypatch.setattr(provider_metadata, "_catalog", lambda: {
            "anthropic/explicit-free": {
                "litellm_provider": "anthropic", "input_cost_per_token": 0, "output_cost_per_token": 0,
            },
            "anthropic/missing-output": {
                "litellm_provider": "anthropic", "input_cost_per_token": "0.000002",
            },
        })
        assert provider_metadata.effective_prices_per_million("explicit-free", "anthropic") == (0, 0)
        assert provider_metadata.effective_prices_per_million("missing-output", "anthropic") == (Decimal("2"), None)

    def test_reads_bundled_deepseek_price_table_without_network(self):
        assert provider_metadata.effective_prices_per_million("deepseek-chat", "deepseek") == (
            Decimal("0.28"),
            Decimal("0.42"),
        )
        assert provider_metadata.effective_prices_per_million("deepseek-reasoner", "deepseek") == (
            Decimal("0.28"),
            Decimal("0.42"),
        )

    def test_resolves_canonical_perplexity_agent_prices_without_model_family_fallback(self):
        assert provider_metadata.effective_prices_per_million("perplexity/perplexity/sonar", "perplexity") == (
            Decimal("0.250000"),
            Decimal("2.500000"),
        )
        assert provider_metadata.effective_prices_per_million("perplexity/perplexity/glm-5.3", "perplexity") == (
            Decimal("1.400000"),
            Decimal("4.400000"),
        )
        assert provider_metadata.effective_prices_per_million("perplexity/perplexity/glm-5.3-unlisted", "perplexity") == (
            None,
            None,
        )

    def test_sonar_agent_and_legacy_api_prices_do_not_alias(self):
        assert provider_metadata.effective_prices_per_million(
            "perplexity/sonar", "perplexity", api_base="https://api.perplexity.ai/v1"
        ) == (Decimal("0.250000"), Decimal("2.500000"))
        assert provider_metadata.effective_prices_per_million(
            "perplexity/sonar", "perplexity", api_base="https://api.perplexity.ai"
        ) == (Decimal("1.000000"), Decimal("1.000000"))
        assert (
            provider_metadata.official_price_source("perplexity/sonar", "perplexity", api_base="https://api.perplexity.ai")
            is None
        )


def test_chatgpt_subscription_token_count_matches_bare_model():
    messages = [{"role": "user", "content": "hello"}]
    assert provider_metadata.count_tokens("chatgpt/gpt-5.2-codex", messages=messages) == (
        provider_metadata.count_tokens("gpt-5.2-codex", messages=messages)
    )


def test_chatgpt_subscription_missing_static_prices_remains_unpriced(monkeypatch):
    monkeypatch.setattr(provider_metadata, "_catalog", lambda: {
        "chatgpt/gpt-5.2-codex": {"litellm_provider": "chatgpt", "mode": "responses"},
    })

    assert provider_metadata.effective_prices_per_million(
        "chatgpt/gpt-5.2-codex",
        "chatgpt",
    ) == (None, None)


