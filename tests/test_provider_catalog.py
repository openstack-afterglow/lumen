"""Frozen catalog boundaries and offline tokenizer behavior (no upstream calls)."""

import hashlib
from decimal import Decimal
from importlib.resources import files

import pytest

from lumen.services import provider_metadata as metadata
from lumen.services.capabilities import provider_capabilities


def test_catalog_asset_is_original_versioned_and_licensed():
    data = files("lumen").joinpath("data")
    raw = data.joinpath("provider_catalog.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == metadata.CATALOG_SHA256
    assert metadata.CATALOG_VERSION == "litellm-1.93.0"
    assert "Copyright (c) 2023 Berri AI" in data.joinpath("LICENSE.litellm").read_text()
    assert "frozen historical metadata" in data.joinpath("NOTICE").read_text()


def test_exact_catalog_lookup_never_crosses_provider_or_infers_family(monkeypatch):
    monkeypatch.setattr(metadata, "_catalog", lambda: {
        "opaque": {"litellm_provider": "openai", "supports_reasoning": False,
                   "input_cost_per_token": Decimal("0"), "output_cost_per_token": Decimal("0.000004")},
    })
    assert metadata.catalog_model_metadata("opaque", "anthropic") == {}
    assert metadata.catalog_model_metadata("opaque-new", "openai") == {}
    entry = metadata.catalog_model_metadata("openai/opaque", "openai")
    assert entry["input_cost_per_token"] == 0
    entry["supports_reasoning"] = True
    assert metadata.catalog_model_metadata("opaque")["supports_reasoning"] is False


@pytest.mark.parametrize("prompt,expected", [(128000, "1"), (128001, "2"), (200000, "2"), (200001, "3")])
def test_exact_decimal_long_context_thresholds(monkeypatch, prompt, expected):
    monkeypatch.setattr(metadata, "_catalog", lambda: {
        "opaque": {"litellm_provider": "gemini", "input_cost_per_token": "1", "output_cost_per_token": "4",
                   "input_cost_per_token_above_128k_tokens": "2", "output_cost_per_token_above_128k_tokens": "5",
                   "input_cost_per_token_above_200k_tokens": "3", "output_cost_per_token_above_200k_tokens": "6"},
    })
    input_rate, output_rate = metadata._catalog_component_rates("opaque", prompt, 1, "gemini")
    assert input_rate == Decimal(expected)
    assert output_rate == input_rate + 3


def test_manual_zero_prices_and_custom_endpoint_do_not_borrow_catalog_rates(monkeypatch):
    monkeypatch.setattr(metadata, "_catalog", lambda: {
        "opaque": {"litellm_provider": "openai", "input_cost_per_token": "1", "output_cost_per_token": "2"},
    })
    assert metadata.effective_prices_per_million("opaque", "openai", api_base="https://custom.invalid/v1") == (None, None)
    cost = metadata.cost_from_usage("opaque", 10, 5, input_price_per_token=Decimal(0),
                                    output_price_per_token=Decimal(0), price_source="manual", provider_type="openai")
    assert cost.raw_cost == 0
    assert cost.pricing_status == "priced"
    assert cost.pricing_snapshot["input"]["source"] == "manual"
    assert cost.pricing_snapshot["output"]["source"] == "manual"


def test_missing_direction_remains_missing_not_free(monkeypatch):
    monkeypatch.setattr(metadata, "_catalog", lambda: {
        "opaque": {"litellm_provider": "openai", "input_cost_per_token": "0"},
    })
    cost = metadata.cost_from_usage("opaque", 10, 5, input_price_per_token=None,
                                    output_price_per_token=None, price_source=None, provider_type="openai")
    assert cost.pricing_status == "partial"
    assert cost.pricing_snapshot["input"]["effective_price_per_token"] == "0"
    assert cost.pricing_snapshot["output"]["effective_price_per_token"] is None
    assert cost.pricing_snapshot["input"]["source"] == "litellm"


def test_tokenizer_assets_are_installed_and_never_download(monkeypatch):
    import urllib.request

    import tiktoken.load

    def network_forbidden(*args, **kwargs):
        raise AssertionError("token accounting must be offline")

    monkeypatch.setattr(urllib.request, "urlopen", network_forbidden)
    monkeypatch.setattr(tiktoken.load, "read_file", network_forbidden)
    metadata._encoding.cache_clear()
    try:
        assert metadata.count_tokens("gpt-4o", text="hello world") == 2
        assert metadata.count_tokens("uncatalogued", text="hello world") == 2
        messages = [{"role": "user", "content": "hello"}]
        plain = metadata.count_context_tokens("gpt-4o", messages)
        tools = metadata.count_context_tokens("gpt-4o", messages, [{"type": "function", "function": {"name": "lookup"}}])
        assert plain.measurement == "estimated"
        assert plain.tokenizer == "tiktoken:0.13.0:o200k_base"
        assert tools.tokens > plain.tokens
    finally:
        metadata._encoding.cache_clear()


def test_image_context_uses_fixed_estimate_not_url_or_base64_length():
    def count(url):
        return metadata.count_context_tokens("gpt-4o", [{"role": "user", "content": [
            {"type": "text", "text": "describe"}, {"type": "image_url", "image_url": {"url": url}},
        ]}])
    short = count("https://private.invalid/image")
    long = count("data:image/png;base64," + "A" * 100000)
    assert short == long
    assert short.tokens >= 250
    assert short.measurement == "estimated"
    assert short.reason_code == "image_tokens_estimated"


@pytest.mark.parametrize("kind", ["audio", "input_audio", "video", "video_url", "file", "input_file"])
def test_unsupported_modality_context_is_unknown(kind):
    result = metadata.count_context_tokens("gpt-4o", [{"role": "user", "content": [{"type": kind}]}])
    assert result.tokens is None
    assert result.measurement == "unknown"


def test_capabilities_do_not_infer_from_model_family(monkeypatch):
    monkeypatch.setattr(metadata, "_catalog", lambda: {})
    caps = provider_capabilities("gpt-4o-new-vision-reasoning", "openai")
    assert caps["vision"] is False
    assert caps["reasoning"] is False
    assert caps["metadata_status"] == "missing"
    assert caps["capability_observations"]["supports_vision"] is None
    assert caps["feature_gates"]["image_input"]["reason_code"] == "metadata_unknown"


@pytest.mark.parametrize("prompt,expected", [(200000, "6"), (200001, "15")])
def test_cache_ttl_categories_keep_exact_thresholds_and_provenance(monkeypatch, prompt, expected):
    from lumen.services.usage_breakdown import UsageBreakdown

    monkeypatch.setattr(metadata, "_catalog", lambda: {
        "opaque": {"litellm_provider": "anthropic",
                   "cache_read_input_token_cost": "0.1", "cache_creation_input_token_cost": "0.2",
                   "cache_creation_input_token_cost_above_1hr": "0.3",
                   "cache_read_input_token_cost_above_200k_tokens": "0.4",
                   "cache_creation_input_token_cost_above_200k_tokens": "0.5",
                   "cache_creation_input_token_cost_above_1hr_above_200k_tokens": "0.6"},
    })
    usage = UsageBreakdown.from_totals(prompt, 1, cache_read_input_tokens=10,
                                      cache_creation_5m_input_tokens=10, cache_creation_1h_input_tokens=10)
    cost = metadata.cost_from_usage("opaque", prompt, 1, input_price_per_token=Decimal(0),
                                    output_price_per_token=Decimal(0), price_source="manual",
                                    provider_type="anthropic", breakdown=usage)
    assert cost.raw_cost == Decimal(expected)
    assert cost.pricing_status == "priced"
    assert cost.cache_read_cost + cost.cache_creation_5m_cost + cost.cache_creation_1h_cost == cost.raw_cost
    for name in ("cache_read", "cache_creation_5m", "cache_creation_1h"):
        assert cost.pricing_snapshot[name]["source"] == "litellm"


def test_fallback_usage_has_no_fabricated_modality_split(monkeypatch):
    monkeypatch.setattr(metadata, "count_tokens", lambda *args, **kwargs: 11 if "messages" in kwargs else 3)
    usage = metadata.extract_usage_breakdown("opaque", [{"role": "user", "content": "hello"}], "ok", None)
    assert (usage.input_tokens, usage.output_tokens) == (11, 3)
    assert usage.modality_tokens == {}
    assert usage.cache_read_input_tokens == 0


@pytest.mark.parametrize("provider,expected", [
    ("chatgpt", False), ("openai", True), ("anthropic", True), ("gemini", True),
    ("vertex_ai", True), ("azure_ai", True), ("unknown", None), (None, None),
    ("mistral", True), ("cohere", True), ("groq", True), ("together", True), ("together_ai", True),
])
def test_output_budget_enforcement_is_an_exact_executor_contract(provider, expected):
    from lumen.services.capabilities import normalize_capabilities

    assert metadata.provider_output_budget_enforced(provider) is expected
    detected = provider_capabilities("uncatalogued", provider)
    assert detected["output_budget_enforced"] is expected
    normalized = normalize_capabilities({"output_budget_enforced": True}, detected)
    assert normalized["output_budget_enforced"] is expected


def test_chatgpt_catalog_metadata_states_output_budget_limitation():
    assert metadata.chatgpt_model_metadata("chatgpt/gpt-5.3-codex")["output_budget_enforced"] is False


@pytest.mark.parametrize("provider", ["vertex", "vertex_ai"])
@pytest.mark.parametrize("model", ["claude-3-5-haiku", "vertex/claude-3-5-haiku", "vertex_ai/claude-3-5-haiku"])
def test_vertex_catalog_publisher_taxonomy_preserves_exact_metadata(provider, model):
    entry = metadata.catalog_model_metadata(model, provider)
    assert entry["litellm_provider"] == "vertex_ai-anthropic_models"
    assert metadata.effective_prices_per_million(model, provider) == (Decimal("1.000000"), Decimal("5.000000"))
    capabilities = provider_capabilities(model, provider)
    assert capabilities["function_calling"] is True
    assert capabilities["context_limit"] == 200000
    assert "vertex_ai/claude-3-5-haiku" in metadata.catalog_models(provider)
    assert metadata.catalog_model_metadata("vertex_ai/claude-3-5-haiku", "anthropic") == {}
    assert metadata.catalog_model_metadata("vertex_ai/claude-3-5-haiku", "gemini") == {}


def test_catalog_taxonomy_aliases_remain_explicit_and_provider_isolated(monkeypatch):
    monkeypatch.setattr(metadata, "_catalog", lambda: {
        "vertex_ai/opaque": {"litellm_provider": "vertex_ai-anthropic_models", "max_input_tokens": 17},
        "vertex_ai/future": {"litellm_provider": "vertex_ai-unrecognized_models"},
        "anthropic/opaque": {"litellm_provider": "anthropic", "max_input_tokens": 19},
        "gemini/opaque": {"litellm_provider": "gemini", "max_input_tokens": 23},
        "azure/instruct": {"litellm_provider": "azure_text"},
        "azure_ai/opaque": {"litellm_provider": "azure_ai"},
        "bedrock/opaque": {"litellm_provider": "bedrock_converse"},
        "bedrock_mantle/opaque": {"litellm_provider": "bedrock_mantle"},
    })
    metadata.catalog_models.cache_clear()
    try:
        assert metadata.catalog_models("vertex") == ("vertex_ai/opaque",)
        assert metadata.catalog_models("vertex_ai") == ("vertex_ai/opaque",)
        assert metadata.catalog_model_metadata("opaque", "vertex")["max_input_tokens"] == 17
        assert metadata.catalog_model_metadata("opaque", "anthropic")["max_input_tokens"] == 19
        assert metadata.catalog_model_metadata("opaque", "gemini")["max_input_tokens"] == 23
        assert metadata.catalog_model_metadata("vertex_ai/future", "vertex") == {}
        assert metadata.catalog_models("azure") == ("azure/instruct",)
        assert metadata.catalog_models("azure_ai") == ("azure_ai/opaque",)
        assert metadata.catalog_models("bedrock") == ("bedrock/opaque",)
    finally:
        metadata.catalog_models.cache_clear()


@pytest.mark.parametrize("provider", ["together", "together_ai"])
def test_together_transport_alias_uses_only_exact_together_catalog_entries(monkeypatch, provider):
    entry = {"litellm_provider": "together_ai", "input_cost_per_token": "0.000001",
             "output_cost_per_token": "0.000002"}
    monkeypatch.setattr(metadata, "_catalog", lambda: {"together_ai/organization/opaque": entry})
    for model in ("organization/opaque", "together/organization/opaque", "together_ai/organization/opaque"):
        assert metadata.catalog_model_metadata(model, provider) == entry
    assert metadata.catalog_model_metadata("together_ai/organization/opaque", "groq") == {}


@pytest.mark.parametrize("model,input_price,output_price", [
    ("command-r", "0.150000", "0.600000"),
    ("command-r-plus", "2.500000", "10.000000"),
])
def test_cohere_chat_catalog_taxonomy_preserves_real_published_entries(model, input_price, output_price):
    entry = metadata.catalog_model_metadata(model, "cohere")
    assert entry["litellm_provider"] == "cohere_chat"
    assert metadata.effective_prices_per_million(model, "cohere") == (Decimal(input_price), Decimal(output_price))
    assert metadata.catalog_model_metadata(f"cohere/{model}", "cohere") == entry
    assert metadata.catalog_model_metadata(f"cohere_chat/{model}", "cohere") == entry
    assert provider_capabilities(model, "cohere")["function_calling"] is True
    assert provider_capabilities(model, "cohere")["context_limit"] == 128000
    assert model in metadata.catalog_models("cohere")
    assert metadata.catalog_model_metadata(model, "openai") == {}
    assert metadata.catalog_model_metadata(model, "groq") == {}
