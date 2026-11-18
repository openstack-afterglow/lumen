"""Strict native source validation, route isolation and Decimal precedence (offline)."""
from __future__ import annotations

import copy
from decimal import Decimal
from importlib.resources import files
from types import SimpleNamespace

import pytest

from lumen.services import provider_metadata as metadata
from lumen.services.capabilities import normalize_capabilities, provider_capabilities
from lumen.services.native_catalog import native_catalog, validate_catalog
from lumen.services.providers import image_transport
from lumen.services.providers.errors import ProviderValidationError
from lumen.services.providers.pricing import (
    _effective_capabilities,
    _effective_media_pricing,
    _resolved_base_prices,
    _resolved_context_tiers,
    frozen_token_pricing,
)
from lumen.services.providers.routing import _validate_provider_route
from lumen.services.usage_breakdown import UsageBreakdown


def fixture_catalog():
    return {"schema_version": 1, "catalog_version": "test-2026-11-17", "providers": {"openai": {
        "display_name": "OpenAI", "pricing_sources": ["https://developers.openai.com/api/docs/pricing"],
        "models": [{
            "id": "opaque", "aliases": ["opaque-alias"], "kind": "text", "display_name": "Opaque",
            "status": "active", "context_window": 400000, "max_output_tokens": 1000,
            "input_modalities": ["text", "image"], "output_modalities": ["text"],
            "capabilities": {"vision": True, "tool_call": True, "reasoning": False, "decisions": True,
                             "prompt_caching": True, "structured_output": True, "web_search": False},
            "token_pricing": {"input": "1", "output": "2", "cache_read": "0.1", "cache_write": "1.25",
                              "cache_write_ttl": "30m", "cache_write_long": "2", "cache_write_long_ttl": "12h",
                              "long_context": {"threshold_tokens": 272000, "input": "3", "output": "4",
                                               "cache_read": "0.2", "cache_write": "2.5", "cache_write_long": "4"}},
            "media_pricing": None, "source_urls": ["https://developers.openai.com/api/docs/pricing"],
            "verified_at": "2026-11-17", "notes": None,
        }],
    }}}


@pytest.fixture
def catalog(monkeypatch):
    value = validate_catalog(fixture_catalog())
    monkeypatch.setattr(metadata, "native_catalog", lambda: value)
    return value


def model(**kwargs):
    return SimpleNamespace(model_name="opaque", model_kind="text", input_price=None, output_price=None,
                           cache_read_price=None, cache_write_price=None, cache_write_1h_price=None,
                           media_pricing=None, capabilities=None, capability_source=None, price_source=None,
                           updated_at=None, price_metadata=None, **kwargs)


def test_bundled_catalog_is_valid_and_packaged():
    assert files("lumen").joinpath("data/native_model_catalog.json").is_file()
    catalog = native_catalog()
    assert validate_catalog(catalog) == catalog
    assert set(catalog["providers"]) == {"openai", "anthropic", "gemini", "perplexity"}


@pytest.mark.parametrize("bad", ["-1", "NaN", "Infinity", "-Infinity", 1.2, True, 2])
def test_rejects_invalid_or_nonstring_prices(bad):
    value = fixture_catalog()
    value["providers"]["openai"]["models"][0]["token_pricing"]["input"] = bad
    with pytest.raises(ValueError):
        validate_catalog(value)


@pytest.mark.parametrize("field,value", [
    ("kind", "decisions"), ("status", "unknown"), ("context_window", True),
    ("source_urls", []), ("source_urls", ["https://untrusted.invalid/pricing"]),
    ("verified_at", "yesterday"), ("unknown", True), ("capabilities", {"decisions": 1}),
])
def test_rejects_invalid_model_metadata(field, value):
    catalog = fixture_catalog()
    catalog["providers"]["openai"]["models"][0][field] = value
    with pytest.raises(ValueError):
        validate_catalog(catalog)


@pytest.mark.parametrize("collision", ["opaque", "opaque-alias"])
def test_rejects_duplicate_ids_and_aliases(collision):
    value = fixture_catalog()
    entry = copy.deepcopy(value["providers"]["openai"]["models"][0])
    entry["id"] = collision
    entry["aliases"] = []
    value["providers"]["openai"]["models"].append(entry)
    with pytest.raises(ValueError, match="duplicate"):
        validate_catalog(value)


def test_rejects_unknown_root_provider_token_and_tier_fields():
    for section, field in [("root", "surprise"), ("provider", "surprise"), ("token", "surprise"), ("tier", "surprise")]:
        value = fixture_catalog()
        provider = value["providers"]["openai"]
        target = {"root": value, "provider": provider, "token": provider["models"][0]["token_pricing"],
                  "tier": provider["models"][0]["token_pricing"]["long_context"]}[section]
        target[field] = "1"
        with pytest.raises(ValueError):
            validate_catalog(value)


def test_exact_alias_provider_isolation_and_detached_entries(catalog):
    assert metadata.native_catalog_entry("opaque-new", "openai") == {}
    assert metadata.native_catalog_entry("opaque", "anthropic") == {}
    entry = metadata.native_catalog_entry("openai/opaque-alias", "openai")
    assert entry["id"] == "opaque"
    entry["capabilities"]["decisions"] = False
    assert metadata.native_catalog_entry("opaque", "openai")["capabilities"]["decisions"] is True


def test_compatible_never_reads_catalog(monkeypatch):
    def forbidden():
        raise AssertionError("compatible route consulted native catalog")
    monkeypatch.setattr(metadata, "native_catalog", forbidden)
    assert metadata.native_catalog_entry("opaque", "openai", "https://custom.invalid/v1") == {}
    assert metadata.effective_prices_per_million("opaque", "openai", api_base="https://custom.invalid/v1") == (None, None)
    caps = provider_capabilities("opaque", "openai", "https://custom.invalid/v1")
    assert caps["vision"] is False and caps["decisions"] is False
    override = normalize_capabilities({"decisions": True, "tool_call": True}, caps)
    assert override["decisions"] is True and override["feature_gates"]["decisions"]["available"] is True


def test_decisions_catalog_and_explicit_override(catalog):
    caps = provider_capabilities("opaque", "openai")
    assert caps["decisions"] is True
    assert caps["feature_gates"]["decisions"]["mode"] == "native"
    caps = normalize_capabilities({"decisions": False}, caps)
    assert caps["decisions"] is False and caps["feature_gates"]["decisions"]["available"] is False


def test_manual_zero_and_imported_provenance_do_not_change_precedence(catalog):
    provider = SimpleNamespace(provider_type="openai", api_base=None)
    row = model()
    assert _resolved_base_prices(row, provider)[:3] == (Decimal("0.000001"), Decimal("0.000002"), "native_catalog")
    row.input_price, row.output_price, row.price_source = Decimal(0), Decimal(0), "models.dev"
    assert _resolved_base_prices(row, provider)[:3] == (Decimal(0), Decimal(0), "manual")
    row.capabilities, row.capability_source = {"decisions": False}, "models_dev"
    assert _effective_capabilities(row, "openai")[0]["decisions"] is True
    row.capability_source = "override"
    assert _effective_capabilities(row, "openai")[0]["decisions"] is False


@pytest.mark.parametrize("tokens,expected", [(272000, "0.000001"), (272001, "0.000003")])
def test_catalog_threshold_and_cache_ttl_mapping(catalog, tokens, expected):
    rates = metadata.bundled_cache_rates("opaque", "openai", None)
    assert rates["cache_write_price_per_token"] == Decimal("0.00000125")
    assert rates["cache_write_1h_price_per_token"] == Decimal("0.000002")
    input_rate, _ = metadata._catalog_component_rates("opaque", tokens, 0, "openai")
    assert input_rate == Decimal(expected)
    row = model()
    tiers = _resolved_context_tiers(row, "openai", None)
    assert tiers["long_context_threshold_tokens"] == 272000
    assert frozen_token_pricing(tiers)["long_context_threshold_tokens"] == 272000
    usage = UsageBreakdown.from_totals(tokens, 0, cache_read_input_tokens=1)
    cost = metadata.cost_from_usage("opaque", tokens, 0, input_price_per_token=None, output_price_per_token=None,
                                    price_source=None, provider_type="openai", breakdown=usage)
    cache_rate = "0.0000001" if tokens == 272000 else "0.0000002"
    assert Decimal(cost.pricing_snapshot["cache_read"]["effective_price_per_token"]) == Decimal(cache_rate)


def test_media_catalog_fallback_and_manual_override(catalog):
    entry = catalog["providers"]["openai"]["models"][0]
    entry["kind"] = "image"
    entry["media_pricing"] = {"billing_basis": "unit", "image_per_unit": "0.03"}
    row = model()
    row.model_kind = "image"
    assert _effective_media_pricing(row, "openai", None)["image_per_unit"] == "0.03"
    row.media_pricing = {"billing_basis": "unit", "image_per_unit": "0.01"}
    assert _effective_media_pricing(row, "openai", None)["image_per_unit"] == "0.01"
    assert _effective_media_pricing(row, "openai", "https://custom.invalid") == row.media_pricing
    row.media_pricing = None
    assert _effective_media_pricing(row, "openai", "https://custom.invalid") is None


@pytest.mark.parametrize("provider", ["groq", "deepseek", "mistral", "together_ai", "openrouter", "xai", "ollama", "azure", "bedrock", "vertex", "cohere"])
def test_removed_providers_fail_explicitly(provider):
    with pytest.raises(ValueError, match="unsupported_provider_type"):
        _validate_provider_route(SimpleNamespace(provider_type=provider, api_base=None, auth_mode="api_key"))


def test_tokenizer_assets_stay_offline(monkeypatch):
    import tiktoken.load
    def forbidden(*args, **kwargs):
        raise AssertionError("token accounting must stay offline")
    monkeypatch.setattr(tiktoken.load, "read_file", forbidden)
    metadata._encoding.cache_clear()
    try:
        assert metadata.count_tokens("gpt-4o", text="hello world") == 2
    finally:
        metadata._encoding.cache_clear()


def test_explicit_reasoning_enable_overrides_catalog_denial(catalog):
    from lumen.services.capabilities import reasoning_explicitly_unsupported
    from lumen.services.providers import routing

    row = model()
    row.capabilities = {"reasoning": True}
    row.capability_source = "override"
    effective, _ = routing._effective_capabilities(row, "openai")
    assert effective["reasoning"] is True
    assert reasoning_explicitly_unsupported("opaque", "override", row.capabilities, "openai", None) is False
    assert reasoning_explicitly_unsupported("opaque", None, None, "openai", None) is True


def test_explicit_vision_disable_overrides_catalog_modalities(catalog):
    caps = provider_capabilities("opaque", "openai")
    assert caps["vision"] is True
    caps = normalize_capabilities({"vision": False}, caps)
    assert caps["vision"] is False
    assert caps["feature_gates"]["image_input"]["available"] is False


def test_subscription_discovery_excludes_known_media_kinds(catalog, monkeypatch):
    from lumen.services import model_discovery

    image = copy.deepcopy(catalog["providers"]["openai"]["models"][0])
    image.update(id="image-only", aliases=[], kind="image")
    catalog["providers"]["openai"]["models"].append(image)
    monkeypatch.setattr(model_discovery, "native_catalog", lambda: catalog)
    response = model_discovery._subscription_response(1, "2026-11-17", "chatgpt_device")
    assert response["models"] == ["chatgpt/opaque"]


@pytest.mark.parametrize("published", [False, None])
def test_native_search_override_wins_catalog_false_or_unknown(catalog, published):
    entry = catalog["providers"]["openai"]["models"][0]
    if published is None:
        entry["capabilities"].pop("web_search")
    else:
        entry["capabilities"]["web_search"] = published
    row = model()
    row.capabilities = {"web_search": True}
    row.capability_source = "override"
    effective, source = _effective_capabilities(row, "openai")
    assert source == "override"
    assert effective["web_search"] is True
    assert effective["feature_gates"]["web_search"]["available"] is False
    assert effective["feature_gates"]["web_search"]["mode"] == "none"
    assert effective["feature_gates"]["web_search"]["pricing_available"] is False
    assert effective["feature_gates"]["web_search"]["reason_code"] == "pricing_unavailable"


def test_subscription_still_disables_explicit_native_search(catalog):
    row = model()
    row.capabilities = {"web_search": True}
    row.capability_source = "override"
    effective, _ = _effective_capabilities(row, "anthropic", auth_mode="anthropic_subscription")
    assert effective["web_search"] is False
    assert effective["native_web_search_transport_supported"] is False


def test_override_cannot_enable_unimplemented_search_transport(catalog):
    detected = provider_capabilities("opaque", "unsupported")
    effective = normalize_capabilities({
        "web_search": True, "native_web_search_transport_supported": True,
    }, detected)
    assert effective["native_web_search_transport_supported"] is False
    assert effective["web_search"] is False
    assert effective["feature_gates"]["web_search"]["available"] is False


@pytest.mark.parametrize("patch", [
    {"extra": 1}, {"default_dimensions": True}, {"supports_dimensions": "true"},
    {"min_dimensions": 0}, {"min_dimensions": 1025}, {"max_dimensions": 1},
    {"allowed_dimensions": []}, {"allowed_dimensions": [128, 128]},
    {"allowed_dimensions": [0, 1024]}, {"allowed_dimensions": [128, 256]},
])
def test_embedding_schema_rejects_invalid_dimension_metadata(patch):
    value = fixture_catalog()
    entry = value["providers"]["openai"]["models"][0]
    entry["kind"] = "embedding"
    entry["embedding"] = {
        "default_dimensions": 1024, "supports_dimensions": True,
        "min_dimensions": 8, "max_dimensions": 1024, "allowed_dimensions": None,
        **patch,
    }
    with pytest.raises(ValueError):
        validate_catalog(value)


def test_embedding_metadata_is_optional_and_kind_scoped():
    value = fixture_catalog()
    entry = value["providers"]["openai"]["models"][0]
    entry["embedding"] = {
        "default_dimensions": 1024, "supports_dimensions": False,
        "min_dimensions": None, "max_dimensions": None, "allowed_dimensions": None,
    }
    with pytest.raises(ValueError, match="only valid for kind=embedding"):
        validate_catalog(value)
    entry["kind"] = "embedding"
    assert validate_catalog(value)["providers"]["openai"]["models"][0]["embedding"] == entry["embedding"]
    entry["embedding"] = None
    assert validate_catalog(value)["providers"]["openai"]["models"][0]["embedding"] is None


@pytest.mark.parametrize("provider_type", ["anthropic", "gemini", "perplexity", "chatgpt", "unsupported"])
def test_decisions_override_cannot_enable_non_openai_transport(provider_type):
    detected = provider_capabilities("gpt-6-luna", provider_type, "https://compatible.invalid")
    effective = normalize_capabilities({
        "decisions": True, "decisions_transport_supported": True,
    }, detected)
    assert effective["decisions_transport_supported"] is False
    assert effective["decisions"] is False
    assert effective["feature_gates"]["decisions"]["available"] is False


@pytest.mark.parametrize("limit", [None, 0, -1, True, "4096"])
def test_unknown_output_limit_is_not_emitted(catalog, limit):
    catalog["providers"]["openai"]["models"][0]["max_output_tokens"] = limit
    caps = provider_capabilities("opaque", "openai")
    assert "max_output_tokens" not in caps
    assert "max_output_tokens" not in normalize_capabilities({"max_output_tokens": None}, caps)


def test_positive_output_limit_survives_null_override(catalog):
    caps = provider_capabilities("opaque", "openai")
    assert caps["max_output_tokens"] == 1000
    assert normalize_capabilities({"max_output_tokens": None}, caps)["max_output_tokens"] == 1000


def test_pdf_unknown_is_not_provider_denial(catalog):
    assert metadata.catalog_model_metadata("opaque", "openai")["supports_pdf_input"] is None
    caps = provider_capabilities("opaque", "openai")
    assert caps["capability_observations"]["supports_pdf_input"] is None
    assert caps["feature_gates"]["document_input"]["reason_code"] == "metadata_unknown"
    catalog["providers"]["openai"]["models"][0]["input_modalities"].append("pdf")
    caps = provider_capabilities("opaque", "openai")
    assert caps["feature_gates"]["document_input"]["available"] is True


def test_reasoning_catalog_override_and_imported_fallback_precedence(catalog):
    from lumen.services.chat_admission import _validated_reasoning_effort

    entry = catalog["providers"]["openai"]["models"][0]
    entry["capabilities"]["reasoning"] = True
    entry["context_window"] = None
    entry["reasoning_options"] = None
    row = model()
    row.capability_source = "models_dev"
    row.capabilities = {
        "reasoning_options": [{"type": "effort", "values": ["high"]}],
        "context_limit": 88000, "tool_call": False,
    }
    caps, _ = _effective_capabilities(row, "openai")
    assert caps["context_limit"] == 88000
    assert caps["reasoning_options"] == row.capabilities["reasoning_options"]
    assert caps["tool_call"] is True
    assert _validated_reasoning_effort("high", {"capabilities": caps}) == "high"
    entry["reasoning_options"] = [{"type": "effort", "values": ["low"]}]
    entry["context_window"] = 400000
    caps, _ = _effective_capabilities(row, "openai")
    assert caps["reasoning_options"] == entry["reasoning_options"]
    assert caps["context_limit"] == 400000
    row.capability_source = "override"
    caps, _ = _effective_capabilities(row, "openai")
    assert caps["reasoning_options"] == row.capabilities["reasoning_options"]
    assert caps["context_limit"] == 88000


@pytest.mark.parametrize("options", [
    "high", [{"type": "effort", "values": []}], [{"type": "effort", "values": ["high", "high"]}],
    [{"type": "budget_tokens", "min": True}], [{"type": "budget_tokens", "min": -1}],
    [{"type": "budget_tokens", "min": 8, "max": 2}], [{"type": "toggle", "enabled": True}],
    [{"type": "effort", "values": ["high"]}, {"type": "effort", "values": ["low"]}],
])
def test_reasoning_options_schema_rejects_invalid_shape(options):
    value = fixture_catalog()
    value["providers"]["openai"]["models"][0]["reasoning_options"] = options
    with pytest.raises(ValueError):
        validate_catalog(value)


def test_distinct_modality_rate_requires_explicit_complete_tier(catalog):
    entry = catalog["providers"]["openai"]["models"][0]
    entry["media_pricing"] = {"token_rates": {"audio": {
        "input_per_million": "9", "cache_read_per_million": "0.1",
    }}}
    with pytest.raises(ValueError, match="distinct tiered modality"):
        validate_catalog(catalog)
    entry["token_pricing"]["long_context"]["token_rates"] = {"audio": {"input_per_million": "12"}}
    validate_catalog(catalog)
    tiers = _resolved_context_tiers(model(), "openai", None)
    assert tiers["token_rates_above_200k"] == {"audio": {
        "input_per_million": "12", "cache_read_per_million": "0.2",
    }}
    assert tiers["token_rates_follow_text"] is True
    entry["token_pricing"]["long_context"]["token_rates"]["audio"]["output_per_million"] = "9"
    with pytest.raises(ValueError, match="corresponding base rate"):
        validate_catalog(catalog)


@pytest.mark.parametrize("manual", [
    {"cache_read_price": Decimal(0)}, {"input_price": Decimal(0)}, {},
])
def test_public_price_origin_matches_resolved_base_prices(catalog, manual):
    from lumen.models.chat_db import LlmModel
    from lumen.services.providers.pricing import _model_public

    row = LlmModel(id=1, provider_id=1, model_name="opaque", model_kind="text", **manual)
    provider = SimpleNamespace(provider_type="openai", api_base=None)
    source = _resolved_base_prices(row, provider)[2]
    public = _model_public(row, provider_type="openai", api_provider="openai")
    assert public["price_origin"] == source
    assert public["effective_price_source"] == source


@pytest.mark.parametrize("provider,model_name", [
    ("anthropic", "claude-fable-5"), ("anthropic", "claude-sonnet-5"),
    ("openai", "gpt-4o"), ("openai", "gpt-4o-mini"), ("openai", "gpt-5.3-codex"),
    ("openai", "gpt-5.5"), ("openai", "gpt-5.6-luna"), ("openai", "gpt-5.6-sol"),
    ("openai", "gpt-5.6-terra"),
])
def test_official_pdf_support_is_preserved_by_real_catalog(provider, model_name):
    caps = provider_capabilities(model_name, provider)
    assert caps["capability_observations"]["supports_pdf_input"] is True
    assert caps["feature_gates"]["document_input"]["available"] is True


@pytest.mark.parametrize("provider,model_name", [
    ("anthropic", "claude-fable-5"), ("anthropic", "claude-fable-5-1"),
    ("openai", "gpt-5.3-codex"), ("openai", "gpt-5.5"), ("openai", "gpt-5.6-luna"),
    ("openai", "gpt-5.6-sol"), ("openai", "gpt-5.6-terra"), ("openai", "gpt-6-astra"),
    ("openai", "gpt-6-luna"), ("openai", "gpt-6-sol"),
])
def test_official_reasoning_effort_survives_imported_capability_rows(provider, model_name):
    from lumen.services.chat_admission import _validated_reasoning_effort

    row = model()
    row.model_name = model_name
    row.capability_source = "models_dev"
    row.capabilities = {
        "reasoning": True, "reasoning_options": [{"type": "effort", "values": ["high"]}],
        "context_limit": 64000,
    }
    caps, _ = _effective_capabilities(row, provider)
    assert _validated_reasoning_effort("high", {"capabilities": caps}) == "high"
    assert caps["reasoning_options"] == metadata.catalog_model_metadata(model_name, provider)["reasoning_options"]


def test_subscription_catalog_capabilities_do_not_enable_api_pricing(catalog):
    row = model()
    row.model_name = "chatgpt/opaque"
    provider = SimpleNamespace(provider_type="chatgpt", api_base=None)
    assert _resolved_base_prices(row, provider) == (None, None, "unpriced", None)
    tiers = _resolved_context_tiers(row, "chatgpt", None)
    assert tiers["input_price_per_token_above_200k"] is None
    assert tiers["output_price_per_token_above_200k"] is None
    assert tiers["token_rates_follow_text"] is False
    caps, _ = _effective_capabilities(row, "chatgpt", "chatgpt_device")
    assert caps["context_limit"] == 400000
    assert caps["metadata_status"] == "observed"
    assert caps["vision"] is True
    assert caps["function_calling"] is True
    assert caps["output_budget_enforced"] is False


def test_unit_priced_catalog_images_cover_every_supported_preset():
    unit_images = []
    sizes = tuple(image_transport._SIZES)
    qualities = ("auto", "low", "medium", "high", "xhigh", "max", "1k", "2k", "4k")
    for provider, block in native_catalog()["providers"].items():
        for entry in block["models"]:
            pricing = entry.get("media_pricing") or {}
            if entry["kind"] != "image" or pricing.get("billing_basis") != "unit":
                continue
            unit_images.append((provider, entry["id"]))
            route = {
                "model_kind": "image", "provider_type": provider, "api_model_name": entry["id"],
                "api_base": None, "provider_auth": None, "api_key": "synthetic-key", "media_pricing": pricing,
            }
            # Probe all transport-supported presets independently of the catalog's existing variant keys.
            probe = {**route, "media_pricing": {"image_variants": {
                f"{size}:{quality}": "1" for size in sizes for quality in qualities
            }}}
            accepted = set()
            for size in sizes:
                for quality in qualities:
                    try:
                        image_transport.validate_image_request(probe, size=size, quality=quality, n=1, edit=False)
                    except ProviderValidationError:
                        continue
                    accepted.add(f"{size}:{quality}")
                    assert image_transport.validate_image_request(
                        route, size=size, quality=quality, n=1, edit=False,
                    ) > 0, (provider, entry["id"], size, quality)
            assert accepted
            assert accepted <= pricing["image_variants"].keys()
    assert unit_images, "the regression must exercise actual bundled unit-priced image entries"


@pytest.mark.parametrize("size", ["auto", "1024x1024", "1536x1024", "1024x1536"])
@pytest.mark.parametrize("edit", [False, True])
def test_gemini_25_flash_image_catalog_route_passes_validation(size, edit):
    row = model()
    row.model_name = "gemini/gemini-2.5-flash-image"
    row.model_kind = "image"
    route = {
        "model_kind": "image", "provider_type": "gemini", "api_model_name": "gemini-2.5-flash-image",
        "api_base": None, "provider_auth": None, "api_key": "synthetic-key",
        "media_pricing": _effective_media_pricing(row, "gemini", None),
    }
    assert image_transport.validate_image_request(route, size=size, quality="auto", n=1, edit=edit) == Decimal("0.039")
    assert image_transport.image_route_ready(route)
