"""Image/audio token prices, duration units and canonical modality usage."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import litellm
import pytest
from fastapi import HTTPException

from lumen.api.models import ModelCreateRequest
from lumen.services import credit, litellm_client
from lumen.services.chat_admission import _run_snapshots
from lumen.services.providers import pricing
from lumen.services.providers.errors import ProviderValidationError
from lumen.services.usage_breakdown import UsageBreakdown

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
    for kind, value in (
        ("text", {"token_rates": {"audio": {"input_per_million": 0.1}}}),
        ("text", {"token_rates": {"audio": {"input_per_million": " 1"}}}),
        ("text", {"token_rates": {"audio": {"cache_read_per_million": "1"}}}),
        ("text", {"token_rates": {"video": {"input_per_million": "1"}}}),
        ("text", {"image_per_unit": "0.04"}),
        ("stt", {"audio_input_per_hour": "0.36", "audio_per_minute": "0.007"}),
        ("realtime", {"realtime_session_per_minute": "0.05", "realtime_session_per_second": "0.001"}),
        ("image", {"billing_basis": "tokens", "token_rates": {"image": {"output_per_million": "32"}}}),
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


def test_live_litellm_usage_and_frozen_snapshot_price_modalities_identically():
    usage = UsageBreakdown.from_runtime(litellm.Usage(
        prompt_tokens=1000, completion_tokens=100,
        prompt_tokens_details={"audio_tokens": 600, "cached_tokens": 0},
        completion_tokens_details={"audio_tokens": 80},
    ))
    live = litellm_client.cost_from_usage(
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
