"""Image/audio token prices, duration units and canonical modality usage."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from lumen.api.models import ModelCreateRequest
from lumen.models.chat_db import LlmModel, LlmProvider, LlmProviderAccount, LlmProviderCredential
from lumen.services import completion_api, credit, provider_metadata
from lumen.services.chat_admission import _run_snapshots
from lumen.services.provider_client import ProviderObject
from lumen.services.providers import pricing, routing
from lumen.services.providers.errors import ProviderValidationError
from lumen.services.usage_breakdown import ModalityTokens, UsageBreakdown

_TEXT = {"input_price_per_token": "0.000002", "output_price_per_token": "0.000004",
         "cache_read_price_per_token": "0.0000005"}
_AUDIO_RATES = {"audio": {"input_per_million": "10", "cache_read_per_million": "1", "output_per_million": "20"}}


def _cost(snapshot, breakdown, required=None):
    return credit.usage_cost_from_pricing_snapshot(
        snapshot, prompt_tokens=breakdown.input_tokens, completion_tokens=breakdown.output_tokens,
        breakdown=breakdown, required_modalities=required,
    )


def test_media_pricing_keeps_omission_distinct_from_zero_and_rejects_ambiguous_numbers():
    stored = pricing.validate_media_pricing("realtime", {
        "billing_basis": "tokens", "reservation_usd": "0.5",
        "realtime_input_per_hour": "3", "realtime_input_per_minute": "0.05",
        "token_rates": {"audio": {"input_per_million": "0", "output_per_million": "80"}},
    })
    assert stored["token_rates"] == {"audio": {"input_per_million": "0", "output_per_million": "80"}}
    assert {"realtime_input_per_hour", "realtime_input_per_minute"} <= set(stored)
    assert "cache_read_per_million" not in stored["token_rates"]["audio"]
    assert pricing.validate_media_pricing("text", {"token_rates": {"image": {"input_per_million": "1.5"}}}) == {
        "token_rates": {"image": {"input_per_million": "1.5"}},
    }
    image = {"billing_basis": "tokens", "token_rates": {"image": {"output_per_million": "32"}}}
    assert pricing.validate_media_pricing("image", image) == image
    for kind, value in (
        ("text", {"token_rates": {"audio": {"input_per_million": 0.1}}}),
        ("text", {"token_rates": {"audio": {"input_per_million": " 1"}}}),
        ("text", {"token_rates": {"audio": {"cache_read_per_million": "1"}}}),
        ("text", {"token_rates": {"video": {"input_per_million": "1"}}}),
        ("text", {"image_per_unit": "0.04"}),
        ("stt", {"audio_input_per_hour": "0.36", "audio_per_minute": "0.007"}),
        ("realtime", {"realtime_session_per_minute": "0.05", "realtime_session_per_second": "0.001"}),
        ("image", {**image, "reservation_usd": "0"}),
        ("image", {"reservation_usd": "1", "image_per_unit": "0.04"}),
        ("tts", {"billing_basis": "session"}),
    ):
        with pytest.raises(ProviderValidationError):
            pricing.validate_media_pricing(kind, value)


def test_duration_units_convert_exactly_only_at_calculation():
    minute = {"billing_basis": "session", "realtime_session_per_minute": "0.05"}
    hour = {"billing_basis": "session", "realtime_session_per_hour": "3"}
    assert pricing.media_billing_basis("realtime", minute) == "session"
    assert pricing.media_billing_basis("stt", {"audio_per_minute": "0.006"}) == "duration"
    assert pricing.exact_duration_price(minute, "realtime_session") == (Decimal("0.05"), 60)
    assert pricing.duration_cost_usd(minute, "realtime_session", 7) == Decimal("0.0058333333")
    assert pricing.duration_cost_usd(hour, "realtime_session", 7) == Decimal("0.0058333333")
    assert pricing.duration_cost_usd({"audio_per_minute": "0.006"}, "audio_input", 90) == Decimal("0.009")
    assert pricing.media_pricing_available("realtime", minute)
    assert not pricing.media_pricing_available("realtime", {"billing_basis": "session"})


def test_openai_audio_cache_split_bills_each_modality_once():
    usage = UsageBreakdown.from_openai_media({
        "input_tokens": 1000, "output_tokens": 50,
        "input_token_details": {"audio_tokens": 800, "text_tokens": 200, "cached_tokens": 400,
                                "cached_tokens_details": {"audio_tokens": 300, "text_tokens": 100}},
        "output_token_details": {"text_tokens": 50, "audio_tokens": 0},
    })
    cost = _cost({**_TEXT, "token_rates": _AUDIO_RATES}, usage, ("audio_input",))
    # text 100*2 + cache 100*0.5 + output 50*4; audio 500*10 + cache 300*1 (per million)
    assert cost.raw_cost == Decimal("0.00575")
    assert cost.input_cost == Decimal("0.0052")
    assert cost.output_cost == Decimal("0.0002")
    assert cost.cache_read_cost == Decimal("0.00035")
    components = {item["kind"]: item["quantity"] for item in credit.token_usage_components(
        cost, segment_id="s", source="media", model_name="m", metadata={})}
    assert components == {"input_tokens": "100", "output_tokens": "50", "cache_read_input_tokens": "100",
                          "audio_input_tokens": "500", "audio_cache_read_input_tokens": "300"}
    assert sum(Decimal(item["cost_usd"]) for item in credit.token_usage_components(
        cost, segment_id="s", source="media", model_name="m", metadata={})) == cost.raw_cost


def test_multimodal_text_model_keeps_legacy_aggregate_billing_until_modality_is_required():
    aggregate = UsageBreakdown.from_totals(1000, 100, cache_read_input_tokens=200)
    legacy = _cost(dict(_TEXT), aggregate)
    assert _cost({**_TEXT, "token_rates": _AUDIO_RATES}, aggregate).raw_cost == legacy.raw_cost
    with pytest.raises(ValueError, match="audio_input"):
        _cost({**_TEXT, "token_rates": _AUDIO_RATES, "required_token_modalities": ["audio_input"]}, aggregate)


def test_invalid_or_ambiguous_modality_usage_fails_closed_only_when_priced():
    over = UsageBreakdown.from_openai_media({
        "input_tokens": 100, "output_tokens": 0, "input_tokens_details": {"image_tokens": 150},
    })
    assert over.modality_usage_invalid and over.input_tokens == 100
    assert _cost(dict(_TEXT), over).raw_cost == Decimal("0.0002")
    with pytest.raises(ValueError):
        _cost({**_TEXT, "token_rates": {"image": {"input_per_million": "8"}}}, over)
    unsplit = UsageBreakdown.from_gemini({
        "promptTokenCount": 1000, "cachedContentTokenCount": 400, "candidatesTokenCount": 10,
        "promptTokensDetails": [{"modality": "TEXT", "tokenCount": 200}, {"modality": "AUDIO", "tokenCount": 800}],
    })
    assert not unsplit.reported("audio", "input")
    with pytest.raises(ValueError):
        _cost({**_TEXT, "token_rates": _AUDIO_RATES}, unsplit, ("audio_input",))
    cached_without_rate = UsageBreakdown.from_openai_media({
        "input_tokens": 100, "output_tokens": 0,
        "input_token_details": {"audio_tokens": 100, "text_tokens": 0, "cached_tokens": 50,
                                "cached_tokens_details": {"audio_tokens": 50}},
    })
    with pytest.raises(ValueError, match="cache"):
        _cost({**_TEXT, "token_rates": {"audio": {"input_per_million": "10"}}}, cached_without_rate)


@pytest.mark.parametrize("usage", [
    {"prompt_tokens": 100.5, "completion_tokens": 0},
    {"prompt_tokens": 100, "completion_tokens": 0, "prompt_tokens_details": {"cached_tokens": -1}},
    {"prompt_tokens": 100, "completion_tokens": 0, "prompt_tokens_details": {"cached_tokens": 150}},
    {"prompt_tokens": 100, "completion_tokens": 0,
     "cache_read_input_tokens": 150, "cache_creation_5m_input_tokens": 0},
])
def test_malformed_runtime_counts_cannot_be_clamped_into_modality_bills(usage):
    normalized = UsageBreakdown.from_runtime(usage)
    assert normalized.modality_usage_invalid
    with pytest.raises(ValueError):
        _cost({**_TEXT, "token_rates": _AUDIO_RATES}, normalized)


def test_gemini_thoughts_and_cache_split_are_canonical_and_roundtrip():
    usage = UsageBreakdown.from_gemini({
        "promptTokenCount": 1000, "cachedContentTokenCount": 350, "candidatesTokenCount": 40,
        "thoughtsTokenCount": 10,
        "promptTokensDetails": [{"modality": "TEXT", "tokenCount": 100}, {"modality": "AUDIO", "tokenCount": 900}],
        "cacheTokensDetails": [{"modality": "TEXT", "tokenCount": 50}, {"modality": "AUDIO", "tokenCount": 300}],
        "candidatesTokensDetails": [{"modality": "TEXT", "tokenCount": 40}],
    })
    assert (usage.input_tokens, usage.output_tokens, usage.cache_read_input_tokens) == (1000, 50, 350)
    # text 50*2 + cache 50*0.5 + output 50*4; audio 600*10 + cache 300*1 (per million)
    assert _cost({**_TEXT, "token_rates": _AUDIO_RATES}, usage, ("audio_input",)).raw_cost == Decimal("0.006625")
    persisted = usage.as_usage_dict()
    assert UsageBreakdown.from_canonical(persisted) == usage
    assert UsageBreakdown.from_runtime(persisted) == usage
    assert (UsageBreakdown(0, 0) + usage) == usage
    tampered = {**persisted, "modality_tokens": {"audio": {"input_tokens": 1001, "cache_read_input_tokens": 0}}}
    with pytest.raises(ValueError):
        UsageBreakdown.from_canonical(tampered)
    assert UsageBreakdown.from_runtime(tampered).modality_usage_invalid


def test_interactions_tool_tokens_are_counted_once():
    base = {"total_input_tokens": 100, "total_output_tokens": 25, "total_tool_use_tokens": 50,
            "input_tokens_by_modality": [{"modality": "text", "tokens": 100}]}
    assert UsageBreakdown.from_gemini_interactions({**base, "total_tokens": 125}).input_tokens == 100
    assert UsageBreakdown.from_gemini_interactions({**base, "total_tokens": 175}).input_tokens == 150
    assert UsageBreakdown.from_gemini_interactions(base) is None


def test_rounds_without_a_modality_report_make_the_sum_unknown():
    reported = UsageBreakdown.from_openai_media({
        "input_tokens": 10, "output_tokens": 0, "input_tokens_details": {"text_tokens": 4, "audio_tokens": 6},
    })
    both = reported + reported
    assert both.modality_tokens["audio"].input_tokens == 12
    assert not (reported + UsageBreakdown.from_totals(10, 0)).reported("audio", "input")


def test_live_usage_and_frozen_snapshot_price_modalities_identically():
    usage = UsageBreakdown.from_runtime(ProviderObject(
        prompt_tokens=1000, completion_tokens=100,
        prompt_tokens_details={"audio_tokens": 600, "cached_tokens": 0},
        completion_tokens_details={"audio_tokens": 80},
    ))
    live = provider_metadata.cost_from_usage(
        "gpt-audio", usage.input_tokens, usage.output_tokens,
        input_price_per_token=Decimal("0.000002"), output_price_per_token=Decimal("0.000004"),
        price_source="manual", breakdown=usage, allow_catalog_cache=False, allow_catalog_prices=False,
        token_rates=_AUDIO_RATES, required_modalities=("audio_input",),
    )
    frozen = _cost({"input_price_per_token": "0.000002", "output_price_per_token": "0.000004",
                    "token_rates": _AUDIO_RATES}, usage, ("audio_input",))
    # text 400*2 + output 20*4; audio 600*10 + output 80*20 (per million)
    assert live.raw_cost == frozen.raw_cost == Decimal("0.00848")
    for cost in (live, frozen):
        assert cost.input_cost == Decimal("0.0068")
        assert cost.output_cost == Decimal("0.00168")
        assert cost.raw_cost == cost.input_cost + cost.output_cost + cost.cache_read_cost


def test_admission_freezes_rates_and_rejects_unmeterable_priced_inputs():
    route = {"model_name": "m", "provider_id": 1, "model_id": 2, "provider_name": "p", "config_version_hash": "h",
             "input_price_per_token": Decimal("0.000002"), "output_price_per_token": Decimal("0.000004"),
             "media_pricing": {"token_rates": {"image": {"input_per_million": "8"}}}}
    image = [SimpleNamespace(type="image")]
    _, gemini = _run_snapshots({**route, "provider_type": "gemini"}, {}, parts=image)
    assert gemini["token_rates"] == {"image": {"input_per_million": "8"}}
    assert gemini["required_token_modalities"] == ["image_input"]
    _, text_only = _run_snapshots({**route, "provider_type": "openai"}, {}, parts=[SimpleNamespace(type="text")])
    assert text_only["required_token_modalities"] == []
    with pytest.raises(HTTPException) as rejected:
        _run_snapshots({**route, "provider_type": "openai"}, {}, parts=image)
    assert rejected.value.status_code == 422
    _, legacy = _run_snapshots({**route, "media_pricing": None, "provider_type": "openai"}, {}, parts=image)
    assert "token_rates" not in legacy


def test_media_models_reject_cache_write_prices():
    with pytest.raises(ValueError):
        ModelCreateRequest(provider_id=1, model_name="gpt-image-2", model_kind="image",
                           cache_write_price_per_million="1")


def test_token_basis_needs_consumed_text_rates_and_media_cache_is_never_free():
    stt = {"billing_basis": "tokens", "reservation_usd": "1", "token_rates": {"audio": {"input_per_million": "1"}}}
    assert not pricing.media_pricing_available("stt", stt, text_input_priced=True)
    assert pricing.media_pricing_available("stt", stt, text_output_priced=True)
    cached_text = UsageBreakdown.from_totals(100, 0, cache_read_input_tokens=40)
    media = {"model_kind": "image", "input_price_per_token": "0.000005", "output_price_per_token": None}
    with pytest.raises(ValueError, match="cache_read"):
        _cost(media, cached_text)
    # Legacy text snapshots keep billing an unpriced cache at 0 as partial.
    assert _cost({**media, "model_kind": "text"}, cached_text).pricing_status == "partial"


def _direct_cost(snapshot, usage, *, model="synthetic"):
    rate_keys = (
        "input_price_per_token", "output_price_per_token",
        "input_price_per_token_above_200k", "output_price_per_token_above_200k",
        "cache_read_price_per_token", "cache_read_price_per_token_above_200k",
        "cache_write_price_per_token", "cache_write_price_per_token_above_200k",
        "cache_write_1h_price_per_token", "cache_write_1h_price_per_token_above_200k",
    )
    return provider_metadata.cost_from_usage(
        model, usage.input_tokens, usage.output_tokens,
        **{key: Decimal(str(snapshot[key])) if snapshot.get(key) is not None else None for key in rate_keys},
        price_source=snapshot.get("price_source", "manual"), breakdown=usage,
        cache_price_sources=snapshot.get("cache_price_sources"),
        long_context_threshold_tokens=snapshot.get("long_context_threshold_tokens", 200_000),
        token_rates=snapshot.get("token_rates"),
        token_rates_above_200k=snapshot.get("token_rates_above_200k"),
        token_rates_follow_text=snapshot.get("token_rates_follow_text") is True,
        required_modalities=snapshot.get("required_token_modalities", ()),
        allow_catalog_prices=False, allow_catalog_cache=False,
    )


def _tier_snapshot():
    return {
        **_TEXT, "input_price_per_token_above_200k": "0.000004",
        "output_price_per_token_above_200k": "0.000008",
        "cache_read_price_per_token_above_200k": "0.000001",
        "cache_write_price_per_token": "0.0000025",
        "cache_write_price_per_token_above_200k": "0.000005",
        "cache_write_1h_price_per_token": "0.000004",
        "cache_write_1h_price_per_token_above_200k": "0.000008",
        "cache_price_sources": {key: "native_catalog" for key in (
            "cache_read", "cache_creation_5m", "cache_creation_1h",
        )},
        "long_context_threshold_tokens": 272_000,
        "token_rates_follow_text": True,
        "required_token_modalities": ["audio_input", "audio_output"],
    }


@pytest.mark.parametrize("prompt", [200_001, 272_000, 272_001])
def test_text_equivalent_modality_keeps_text_tiers_and_exact_cache_totals(prompt):
    snapshot = {
        **_tier_snapshot(),
        "token_rates": {"audio": {
            "input_per_million": "2", "cache_read_per_million": "0.5", "output_per_million": "4",
        }},
    }
    usage = UsageBreakdown(
        input_tokens=prompt, output_tokens=10, cache_read_input_tokens=125,
        cache_creation_5m_input_tokens=3, cache_creation_1h_input_tokens=2,
        modality_tokens={"audio": ModalityTokens(input_tokens=1000, output_tokens=4, cache_read_input_tokens=100)},
    )
    tier = prompt > 272_000
    for cost in (_direct_cost(snapshot, usage), _cost(snapshot, usage)):
        assert cost.input_cost == (prompt - 130) * Decimal("0.000004" if tier else "0.000002")
        assert cost.output_cost == 10 * Decimal("0.000008" if tier else "0.000004")
        assert cost.cache_read_cost == 125 * Decimal("0.000001" if tier else "0.0000005")
        assert cost.cache_creation_5m_cost == 3 * Decimal("0.000005" if tier else "0.0000025")
        assert cost.cache_creation_1h_cost == 2 * Decimal("0.000008" if tier else "0.000004")
        assert cost.raw_cost == (
            cost.input_cost + cost.output_cost + cost.cache_read_cost
            + cost.cache_creation_5m_cost + cost.cache_creation_1h_cost
        )
        components = cost.pricing_snapshot.get("token_components", cost.pricing_snapshot)
        assert components["input"]["tokens"] == prompt - 130
        assert components["output"]["tokens"] == 10
        assert components["cache_read"]["tokens"] == 125
        assert not any(key.startswith("audio_") for key in components)
    frozen = _cost(snapshot, usage)
    ledger = credit.token_usage_components(frozen, segment_id="s", source="executor", model_name="m", metadata={})
    assert sum(Decimal(item["cost_usd"]) for item in ledger) == frozen.raw_cost


@pytest.mark.parametrize("prompt", [272_000, 272_001])
@pytest.mark.parametrize("explicit_tier", [False, True])
def test_distinct_modality_prices_stay_flat_without_explicit_frozen_tier(prompt, explicit_tier):
    snapshot = {**_tier_snapshot(), "token_rates": _AUDIO_RATES, "token_rates_follow_text": False}
    if explicit_tier:
        snapshot["token_rates_above_200k"] = {"audio": {
            "input_per_million": "30", "cache_read_per_million": "3", "output_per_million": "60",
        }}
    usage = UsageBreakdown(
        input_tokens=prompt, output_tokens=10, cache_read_input_tokens=125,
        modality_tokens={"audio": ModalityTokens(input_tokens=1000, output_tokens=4, cache_read_input_tokens=100)},
    )
    tier = prompt > 272_000
    audio_tier = tier and explicit_tier
    expected_input = (prompt - 1025) * Decimal("0.000004" if tier else "0.000002")
    expected_input += 900 * Decimal("0.000030" if audio_tier else "0.000010")
    expected_cache = 25 * Decimal("0.000001" if tier else "0.0000005")
    expected_cache += 100 * Decimal("0.000003" if audio_tier else "0.000001")
    expected_output = 6 * Decimal("0.000008" if tier else "0.000004")
    expected_output += 4 * Decimal("0.000060" if audio_tier else "0.000020")
    for cost in (_direct_cost(snapshot, usage), _cost(snapshot, usage)):
        assert cost.input_cost == expected_input
        assert cost.output_cost == expected_output
        assert cost.cache_read_cost == expected_cache
        assert cost.raw_cost == expected_input + expected_cache + expected_output


@pytest.mark.parametrize("billing", [_direct_cost, _cost])
def test_equal_rate_bucket_retains_required_usage_and_missing_cache_validation(billing):
    snapshot = {**_tier_snapshot(), "token_rates": {"audio": {"input_per_million": "2"}}}
    with pytest.raises(ValueError, match="audio_input"):
        billing(snapshot, UsageBreakdown.from_totals(272_001, 10))
    cached = UsageBreakdown(
        input_tokens=272_001, output_tokens=10, cache_read_input_tokens=100,
        modality_tokens={"audio": ModalityTokens(input_tokens=1000, output_tokens=4, cache_read_input_tokens=100)},
    )
    snapshot["required_token_modalities"] = ["audio_input"]
    with pytest.raises(ValueError, match="cache read token rate"):
        billing(snapshot, cached)
    snapshot["token_rates"] = {"audio": {"cache_read_per_million": "0.5"}}
    with pytest.raises(ValueError, match="audio_input token rate"):
        billing(snapshot, cached)
    snapshot["token_rates"] = {"audio": {"input_per_million": "2", "cache_read_per_million": "0.5"}}
    with pytest.raises(ValueError, match="provider modality usage is invalid"):
        billing(snapshot, UsageBreakdown(
            input_tokens=cached.input_tokens, output_tokens=cached.output_tokens,
            modality_tokens=cached.modality_tokens, modality_usage_invalid=True,
        ))


@pytest.mark.parametrize("model", [
    "gemini-3.1-pro-preview", "gemini-3.1-pro-preview-customtools", "gemini-2.5-pro",
])
@pytest.mark.parametrize("prompt", [199_999, 200_000, 200_001])
def test_real_native_gemini_audio_uses_text_context_tier(model, prompt, monkeypatch):
    entry = provider_metadata.native_catalog_entry(model, "gemini")
    token = entry["token_pricing"]
    tier = token["long_context"]
    million = Decimal("1000000")
    snapshot = {
        "input_price_per_token": str(Decimal(token["input"]) / million),
        "output_price_per_token": str(Decimal(token["output"]) / million),
        "cache_read_price_per_token": str(Decimal(token["cache_read"]) / million),
        "input_price_per_token_above_200k": str(Decimal(tier["input"]) / million),
        "output_price_per_token_above_200k": str(Decimal(tier["output"]) / million),
        "cache_read_price_per_token_above_200k": str(Decimal(tier["cache_read"]) / million),
        "long_context_threshold_tokens": tier["threshold_tokens"],
        "cache_price_sources": {"cache_read": "native_catalog"},
        "token_rates": entry["media_pricing"]["token_rates"],
        "token_rates_follow_text": True,
        "required_token_modalities": ["audio_input"],
    }
    usage = UsageBreakdown.from_gemini({
        "promptTokenCount": prompt, "cachedContentTokenCount": 350, "candidatesTokenCount": 40,
        "thoughtsTokenCount": 10,
        "promptTokensDetails": [{"modality": "TEXT", "tokenCount": prompt - 900},
                               {"modality": "AUDIO", "tokenCount": 900}],
        "cacheTokensDetails": [{"modality": "TEXT", "tokenCount": 50}, {"modality": "AUDIO", "tokenCount": 300}],
        "candidatesTokensDetails": [{"modality": "TEXT", "tokenCount": 40}],
    })
    assert (usage.input_tokens, usage.output_tokens, usage.cache_read_input_tokens) == (prompt, 50, 350)
    applicable = tier if prompt > tier["threshold_tokens"] else token
    expected_input = (prompt - 350) * Decimal(applicable["input"]) / million
    expected_output = 50 * Decimal(applicable["output"]) / million
    expected_cache = 350 * Decimal(applicable["cache_read"]) / million

    catalog_direct = provider_metadata.cost_from_usage(
        model, usage.input_tokens, usage.output_tokens,
        input_price_per_token=None, output_price_per_token=None,
        price_source=None, provider_type="gemini", breakdown=usage,
        token_rates=snapshot["token_rates"], token_rates_follow_text=True,
        required_modalities=("audio_input",),
    )

    def forbid_catalog(*_args, **_kwargs):
        raise AssertionError("frozen settlement must not read the catalog")

    monkeypatch.setattr(provider_metadata, "native_catalog_entry", forbid_catalog)
    monkeypatch.setattr(provider_metadata, "bundled_cache_rates", forbid_catalog)
    for cost in (catalog_direct, _direct_cost(snapshot, usage, model=model), _cost(snapshot, usage)):
        assert cost.input_cost == expected_input
        assert cost.output_cost == expected_output
        assert cost.cache_read_cost == expected_cache
        assert cost.raw_cost == expected_input + expected_output + expected_cache
        assert cost.pricing_status == "priced"


@pytest.mark.parametrize("prompt", [272_000, 272_001])
def test_explicit_modality_tier_overrides_text_equivalent_base_bucket(prompt):
    snapshot = {
        **_tier_snapshot(),
        "token_rates": {"audio": {
            "input_per_million": "2", "cache_read_per_million": "0.5", "output_per_million": "4",
        }},
        "token_rates_above_200k": {"audio": {
            "input_per_million": "30", "cache_read_per_million": "3", "output_per_million": "60",
        }},
    }
    usage = UsageBreakdown(
        input_tokens=prompt, output_tokens=10, cache_read_input_tokens=125,
        modality_tokens={"audio": ModalityTokens(input_tokens=1000, output_tokens=4, cache_read_input_tokens=100)},
    )
    if prompt > 272_000:
        expected_input = (prompt - 1025) * Decimal("0.000004") + 900 * Decimal("0.000030")
        expected_output = 6 * Decimal("0.000008") + 4 * Decimal("0.000060")
        expected_cache = 25 * Decimal("0.000001") + 100 * Decimal("0.000003")
    else:
        expected_input = (prompt - 125) * Decimal("0.000002")
        expected_output = 10 * Decimal("0.000004")
        expected_cache = 125 * Decimal("0.0000005")
    for cost in (_direct_cost(snapshot, usage), _cost(snapshot, usage)):
        assert cost.input_cost == expected_input
        assert cost.output_cost == expected_output
        assert cost.cache_read_cost == expected_cache
        assert cost.raw_cost == expected_input + expected_output + expected_cache


@pytest.mark.parametrize("billing", [_direct_cost, _cost])
def test_text_equivalent_bucket_rejects_unmarked_inconsistent_modality_shares(billing):
    snapshot = {**_tier_snapshot(), "required_token_modalities": [],
                "token_rates": {"audio": {"input_per_million": "2"}}}
    with pytest.raises(ValueError, match="provider modality usage is invalid"):
        billing(snapshot, UsageBreakdown(
            input_tokens=10, output_tokens=0, modality_tokens={"audio": ModalityTokens(input_tokens=11)},
        ))


@pytest.mark.parametrize("explicit_flag", [False, True])
def test_equal_manual_audio_prices_never_inherit_text_context_tier(explicit_flag):
    snapshot = {
        **_tier_snapshot(),
        "token_rates": {"audio": {
            "input_per_million": "2", "cache_read_per_million": "0.5", "output_per_million": "4",
        }},
    }
    if explicit_flag:
        snapshot["token_rates_follow_text"] = False
    else:
        # Legacy snapshots carry no origin flag and must retain literal prices.
        snapshot.pop("token_rates_follow_text")
    usage = UsageBreakdown(
        input_tokens=272_001, output_tokens=10, cache_read_input_tokens=125,
        modality_tokens={"audio": ModalityTokens(input_tokens=1000, output_tokens=4, cache_read_input_tokens=100)},
    )
    expected_input = 270_976 * Decimal("0.000004") + 900 * Decimal("0.000002")
    expected_output = 6 * Decimal("0.000008") + 4 * Decimal("0.000004")
    expected_cache = 25 * Decimal("0.000001") + 100 * Decimal("0.0000005")
    for cost in (_direct_cost(snapshot, usage), _cost(snapshot, usage)):
        assert cost.input_cost == expected_input
        assert cost.output_cost == expected_output
        assert cost.cache_read_cost == expected_cache
        assert cost.raw_cost == expected_input + expected_output + expected_cache
        components = cost.pricing_snapshot.get("token_components", cost.pricing_snapshot)
        assert components["audio_input"]["tokens"] == 900
        assert components["audio_cache_read"]["tokens"] == 100


@pytest.mark.parametrize("model_name", [
    "gemini-3.1-pro-preview", "gemini-3.1-pro-preview-customtools", "gemini-2.5-pro",
])
@pytest.mark.parametrize("prompt", [200_000, 200_001])
@pytest.mark.parametrize("manual_category", [
    None, "input", "cache_read", "cache_read_free", "input_equal", "cache_read_equal", "output_equal",
])
def test_resolved_native_gemini_keeps_required_audio_and_effective_tiers(
    model_name, prompt, manual_category, monkeypatch,
):
    monkeypatch.setenv("LUMEN_MODALITY_TEST_KEY", "synthetic-key")
    monkeypatch.setattr(routing, "derive_encryption_subkey", lambda _domain: b"modality-test-routing-key")
    provider = LlmProvider(
        id=1, name="native-gemini", provider_type="gemini", auth_mode="api_key",
        api_base=None, is_active=True, margin_multiplier=Decimal("1"), selection_policy="priority",
        accounts=[LlmProviderAccount(
            id=2, provider_id=1, name="audio-account", is_active=True, priority=0, weight=1,
            credentials=[LlmProviderCredential(
                id=3, account_id=2, name="audio-credential", auth_mode="api_key",
                api_key_env="LUMEN_MODALITY_TEST_KEY", generation=0, is_active=True,
                priority=0, weight=1, subscription_status="disconnected",
            )],
        )],
    )
    model = LlmModel(
        id=10, provider_id=1, model_name=model_name, model_kind="text", is_active=True,
        input_price=None, output_price=None, media_pricing=None, price_metadata=None,
    )
    if manual_category == "input":
        model.input_price = Decimal("0.000003")
    elif manual_category in {"cache_read", "cache_read_free"}:
        model.cache_read_price = Decimal("0" if manual_category == "cache_read_free" else "0.000001")
    entry = provider_metadata.native_catalog_entry(model_name, "gemini")
    token = entry["token_pricing"]
    applicable = token["long_context"] if prompt > token["long_context"]["threshold_tokens"] else token
    million = Decimal("1000000")
    if manual_category == "input_equal":
        model.input_price = Decimal(token["input"]) / million
    elif manual_category == "cache_read_equal":
        model.cache_read_price = Decimal(token["cache_read"]) / million
    elif manual_category == "output_equal":
        model.output_price = Decimal(token["output"]) / million
    route = routing._resolved_model(model, provider)
    _, snapshot = _run_snapshots(route, {}, parts=[SimpleNamespace(type="audio")], summary_route=route)
    assert snapshot["token_rates_follow_text"] is (manual_category is None)
    assert snapshot["token_rates"]["audio"]["input_per_million"]
    assert snapshot["required_token_modalities"] == ["audio_input"]
    admitted = completion_api.billing_route(
        route, [{"role": "user", "content": [{"type": "input_audio"}]}], protocol="chat",
    )
    assert admitted["required_token_modalities"] == ["audio_input"]

    def forbid_catalog(*_args, **_kwargs):
        raise AssertionError("frozen settlement must not read the catalog")

    monkeypatch.setattr(provider_metadata, "native_catalog_entry", forbid_catalog)
    monkeypatch.setattr(provider_metadata, "bundled_cache_rates", forbid_catalog)
    missing_audio = UsageBreakdown.from_totals(prompt, 50, cache_read_input_tokens=350)
    for billing in (_cost, _direct_cost):
        with pytest.raises(ValueError, match="provider did not report audio_input"):
            billing(snapshot, missing_audio)
    reported = UsageBreakdown(
        input_tokens=prompt, output_tokens=50, cache_read_input_tokens=350,
        modality_tokens={"audio": ModalityTokens(input_tokens=900, cache_read_input_tokens=300)},
    )
    audio_input_rate = Decimal(applicable["input"]) / million
    audio_cache_rate = Decimal(applicable["cache_read"]) / million
    text_input_rate = model.input_price if model.input_price is not None else audio_input_rate
    text_cache_rate = model.cache_read_price if model.cache_read_price is not None else audio_cache_rate
    expected_input = (prompt - 950) * text_input_rate + 600 * audio_input_rate
    expected_cache = 50 * text_cache_rate + 300 * audio_cache_rate
    expected_output = 50 * (model.output_price if model.output_price is not None else Decimal(applicable["output"]) / million)
    for frozen in (snapshot, snapshot["summary_route"]):
        observed = _cost(frozen, reported, required=["audio_input"])
        assert observed.input_cost == expected_input
        assert observed.cache_read_cost == expected_cache
        assert observed.output_cost == expected_output
        assert observed.raw_cost == expected_input + expected_cache + expected_output
        components = observed.pricing_snapshot["token_components"]
        assert components["cache_read"]["tokens"] == (350 if manual_category is None else 50)


@pytest.mark.parametrize("model", [
    "gemini-3.1-pro-preview", "gemini-3.1-pro-preview-customtools", "gemini-2.5-pro",
])
def test_direct_native_gemini_zero_cache_audio_keeps_text_context_tier(model):
    entry = provider_metadata.native_catalog_entry(model, "gemini")
    tier = entry["token_pricing"]["long_context"]
    usage = UsageBreakdown.from_gemini({
        "promptTokenCount": 200_001, "candidatesTokenCount": 50,
        "promptTokensDetails": [
            {"modality": "TEXT", "tokenCount": 199_101},
            {"modality": "AUDIO", "tokenCount": 900},
        ],
    })
    assert usage.cache_read_input_tokens == 0
    assert usage.reported("audio", "input")
    cost = provider_metadata.cost_from_usage(
        model, usage.input_tokens, usage.output_tokens,
        input_price_per_token=None, output_price_per_token=None,
        price_source=None, provider_type="gemini", breakdown=usage,
        token_rates=entry["media_pricing"]["token_rates"], token_rates_follow_text=True,
        required_modalities=("audio_input",),
    )
    expected_input = Decimal("200001") * Decimal(tier["input"]) / Decimal("1000000")
    expected_output = Decimal("50") * Decimal(tier["output"]) / Decimal("1000000")
    assert cost.input_cost == expected_input
    assert cost.output_cost == expected_output
    assert cost.cache_read_cost == 0
    assert cost.raw_cost == expected_input + expected_output
    assert cost.pricing_snapshot["input"]["tokens"] == 200_001
    assert "audio_input" not in cost.pricing_snapshot
