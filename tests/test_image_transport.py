"""Direct image provider contract: exact billing, inline results and bounded outputs."""

import base64
import json
from decimal import Decimal

import httpx
import pytest

from lumen.services import credit
from lumen.services.provider_metadata import native_catalog_entry
from lumen.services.providers import image_transport, pricing
from lumen.services.providers.errors import ProviderValidationError
from lumen.services.providers.image_billing import output_token_bound

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/"
    "sN8AAAAASUVORK5CYII="
)


def _route(provider, model, variants):
    return {
        "model_kind": "image", "provider_type": provider, "api_model_name": model,
        "api_base": None, "provider_auth": None, "api_key": "configured-key",
        "media_pricing": {"image_variants": variants},
    }


def _mock_provider(monkeypatch, handler):
    client_type = httpx.AsyncClient
    monkeypatch.setattr(image_transport.httpx, "AsyncClient", lambda **kwargs: client_type(
        **{**kwargs, "transport": httpx.MockTransport(handler)},
    ))


@pytest.mark.asyncio
async def test_gemini_interactions_generate_and_edit_reads_only_completed_model_output(monkeypatch):
    requests = []

    def handle(request):
        assert request.url.path == "/v1beta/interactions"
        assert request.headers["x-goog-api-key"] == "configured-key"
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(200, json={
            "status": "completed", "steps": [
                {"type": "thought", "summary": [{"type": "image", "data": base64.b64encode(b"not an output").decode()}]},
                {"type": "model_output", "content": [
                    {"type": "text", "text": "Done"},
                    {"type": "image", "mime_type": "image/png", "data": base64.b64encode(_PNG).decode()},
                ]},
            ],
        })

    _mock_provider(monkeypatch, handle)
    route = _route("gemini", "gemini-3.1-flash-image", {"1536x1024:2k": "0.15"})
    assert image_transport.validate_image_request(route, size="1536x1024", quality="2k", n=1, edit=True) == Decimal("0.15")
    kwargs = {"prompt": "Change the background", "size": "1536x1024", "quality": "2k", "n": 1}
    assert (await image_transport.generate_images(route, **kwargs)).images == [(_PNG, "image/png")]
    assert (await image_transport.generate_images(route, **kwargs, source_image=_PNG, source_mime="image/png")).images == [(_PNG, "image/png")]
    for request in requests:
        assert request["model"] == "gemini-3.1-flash-image"
        assert request["store"] is False
        assert request["response_format"] == {
            "type": "image", "aspect_ratio": "3:2", "image_size": "2K",
        }
        assert request["input"][0] == {"type": "text", "text": "Change the background"}
    assert len(requests[0]["input"]) == 1
    assert requests[1]["input"][1] == {
        "type": "image", "mime_type": "image/png", "data": base64.b64encode(_PNG).decode(),
    }


@pytest.mark.asyncio
async def test_openai_uses_base64_only_and_edits_without_response_format(monkeypatch):
    requests = []

    def handle(request):
        requests.append(request)
        assert request.url.path in ("/v1/images/generations", "/v1/images/edits")
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(_PNG).decode()}]})

    _mock_provider(monkeypatch, handle)
    route = _route("openai", "gpt-image-1.5", {"1024x1024:high": "0.10"})
    kwargs = {"prompt": "Draw a cat", "size": "1024x1024", "quality": "high", "n": 1}
    assert (await image_transport.generate_images(route, **kwargs)).images == [(_PNG, "image/png")]
    assert (await image_transport.generate_images(route, **kwargs, source_image=_PNG, source_mime="image/png")).images == [(_PNG, "image/png")]
    generation, edit = requests
    assert generation.headers["authorization"] == "Bearer configured-key"
    assert json.loads(generation.content) == {"model": "gpt-image-1.5", **kwargs}
    assert b"response_format" not in edit.content
    assert b"response_format" not in generation.content
    assert b'name="image"' in edit.content
    assert b'name="prompt"' in edit.content


@pytest.mark.asyncio
async def test_unsupported_or_unpriced_variants_never_reach_provider(monkeypatch):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(500)

    _mock_provider(monkeypatch, handle)
    route = _route("openai", "gpt-image-1.5", {"1024x1024:high": "0.10"})
    for bad_route, size, quality, n in (
        (route, "1024x1024", "low", 1),
        (_route("openai", "gpt-image-1.5", {"auto:auto": "0"}), "auto", "auto", 1),
        (_route("openai", "unsupported-image", {"auto:auto": "1"}), "auto", "auto", 1),
        (_route("gemini", "gemini-2.5-flash-image", {"auto:auto": "1"}), "auto", "auto", 2),
        (_route("openai", "gpt-image-1.5", {"auto:auto": "0.00000000001"}), "auto", "auto", 1),
        ({**route, "api_key": None}, "1024x1024", "high", 1),
    ):
        with pytest.raises(ProviderValidationError):
            await image_transport.generate_images(bad_route, prompt="draw", size=size, quality=quality, n=n)
    assert calls == []
    assert not image_transport.image_route_ready({**route, "api_key": None})
    assert pricing.validate_media_pricing("image", {"image_variants": {"1024x1024:1k": "0.12"}}) == {
        "image_variants": {"1024x1024:1k": "0.12"},
    }


def test_capability_variant_projection_omits_unexecutable_prices():
    openai = _route("openai", "gpt-image-1.5", {
        "1024x1024:high": "0.12", "auto:auto": "0.08",
        "2048x2048:high": "0.30", "1024x1024:wrong": "0.25", "1536x1024:low": "0",
    })
    assert image_transport.available_image_variants(openai) == ["1024x1024:high", "auto:auto"]
    assert image_transport.max_image_count(openai) == 10
    gemini = _route("gemini", "gemini-3.1-flash-image", {"auto:4k": "0.20"})
    assert image_transport.available_image_variants(gemini) == ["auto:4k"]
    assert image_transport.max_image_count(gemini) == 1
    assert image_transport.available_image_variants({**openai, "api_key": None}) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    {"data": [{"url": "https://example.test/secret.png"}]},
    {"data": [{"b64_json": base64.b64encode(b"not-an-image").decode()}]},
    {"data": [{"b64_json": base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"x" * (5 * 1024 * 1024)).decode()}]},
])
async def test_openai_rejects_urls_invalid_and_oversized_output(monkeypatch, body):
    _mock_provider(monkeypatch, lambda request: httpx.Response(200, json=body))
    route = _route("openai", "gpt-image-2", {"auto:auto": "0.10"})
    with pytest.raises(image_transport.ImageTransportError):
        await image_transport.generate_images(route, prompt="draw", size="auto", quality="auto", n=1)


@pytest.mark.asyncio
async def test_gemini_rejects_incomplete_or_uri_output(monkeypatch):
    payloads = [
        {"status": "in_progress", "steps": [{"type": "model_output", "content": [
            {"type": "image", "mime_type": "image/png", "data": base64.b64encode(_PNG).decode()}]}]},
        {"status": "completed", "steps": [{"type": "model_output", "content": [
            {"type": "image", "uri": "https://example.test/secret.png"}]}]},
    ]
    _mock_provider(monkeypatch, lambda request: httpx.Response(200, json=payloads.pop(0)))
    route = _route("gemini", "gemini-3.1-flash-image", {"auto:auto": "0.10"})
    for _ in range(2):
        with pytest.raises(image_transport.ImageTransportError):
            await image_transport.generate_images(route, prompt="draw", size="auto", quality="auto", n=1)


def _token_route(provider="openai"):
    route = _route(provider, "gpt-image-1.5" if provider == "openai" else "gemini-3.1-flash-image", {"auto:auto": "0.99"})
    return {**route, "input_price_per_token": Decimal("0.000001"), "output_price_per_token": Decimal("0.000002"),
        "cache_read_price_per_token": Decimal("0.0000005"),
        "media_pricing": {**route["media_pricing"], "billing_basis": "tokens", "reservation_usd": "0.01",
            "token_rates": {"image": {"input_per_million": "10", "cache_read_per_million": "2", "output_per_million": "20"}}}}


@pytest.mark.asyncio
async def test_openai_token_images_use_actual_modality_counts_not_unit_prices(monkeypatch):
    route = _token_route()
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(_PNG).decode()}],
            "usage": {"input_tokens": 100, "output_tokens": 200, "total_tokens": 300,
                "input_tokens_details": {"text_tokens": 50, "image_tokens": 50},
                "output_tokens_details": {"text_tokens": 0, "image_tokens": 200}}})

    _mock_provider(monkeypatch, handler)
    result = await image_transport.generate_images(route, prompt="draw", size="auto", quality="auto", n=1)
    cost = credit.usage_cost_from_pricing_snapshot(pricing.frozen_token_pricing(route),
        prompt_tokens=result.usage.input_tokens, completion_tokens=result.usage.output_tokens, breakdown=result.usage)
    assert cost.raw_cost == Decimal("0.00455")
    assert result.images == [(_PNG, "image/png")]
    assert len(calls) == 1
    # n never multiplies the entire-request envelope; transport choices remain available without unit prices.
    assert image_transport.validate_image_request(route, size="auto", quality="auto", n=3, edit=False) == Decimal("0.01")
    assert "1536x1024:high" in image_transport.available_image_variants(route)


@pytest.mark.asyncio
async def test_gemini_image_cache_and_thought_usage_use_frozen_modality_prices(monkeypatch):
    route = _token_route("gemini")
    _mock_provider(monkeypatch, lambda request: httpx.Response(200, json={
        "status": "completed", "steps": [{"type": "model_output", "content": [
            {"type": "image", "mime_type": "image/png", "data": base64.b64encode(_PNG).decode()}]}],
        "usage": {"total_input_tokens": 100, "total_output_tokens": 200, "total_thought_tokens": 5,
            "total_cached_tokens": 40, "total_tokens": 305,
            "input_tokens_by_modality": [{"modality": "text", "tokens": 50}, {"modality": "image", "tokens": 50}],
            "output_tokens_by_modality": [{"modality": "image", "tokens": 200}],
            "cached_tokens_by_modality": [{"modality": "text", "tokens": 10}, {"modality": "image", "tokens": 30}]}}))
    result = await image_transport.generate_images(route, prompt="draw", size="auto", quality="auto", n=1)
    cost = credit.usage_cost_from_pricing_snapshot(pricing.frozen_token_pricing(route),
        prompt_tokens=result.usage.input_tokens, completion_tokens=result.usage.output_tokens, breakdown=result.usage)
    assert result.usage.output_tokens == 205
    assert cost.raw_cost == Decimal("0.004315")


@pytest.mark.asyncio
@pytest.mark.parametrize("usage", [None, {"input_tokens": 100, "output_tokens": 200}])
async def test_token_images_missing_usage_fail_without_retry(monkeypatch, usage):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(_PNG).decode()}], "usage": usage})

    _mock_provider(monkeypatch, handler)
    with pytest.raises(image_transport.ImageTransportError, match="usage"):
        await image_transport.generate_images(_token_route(), prompt="draw", size="auto", quality="auto", n=1)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_invalid_image_modality_usage_is_unpayable(monkeypatch):
    route = _token_route()
    _mock_provider(monkeypatch, lambda request: httpx.Response(200, json={
        "data": [{"b64_json": base64.b64encode(_PNG).decode()}],
        "usage": {"input_tokens": 100, "output_tokens": 200,
            "input_tokens_details": {"image_tokens": 101}, "output_tokens_details": {"image_tokens": 200}}}))
    result = await image_transport.generate_images(route, prompt="draw", size="auto", quality="auto", n=1)
    with pytest.raises(ValueError, match="invalid"):
        credit.usage_cost_from_pricing_snapshot(pricing.frozen_token_pricing(route),
            prompt_tokens=result.usage.input_tokens, completion_tokens=result.usage.output_tokens,
            breakdown=result.usage, required_modalities=("image_output",))


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [
    {"billing_basis": "duration"},
    {"billing_basis": "characters"},
    {"reservation_usd": None},
    {"reservation_usd": "0"},
    {"reservation_usd": "NaN"},
])
async def test_invalid_image_basis_or_envelope_rejects_before_io(monkeypatch, invalid):
    calls = []
    _mock_provider(monkeypatch, lambda request: calls.append(request) or httpx.Response(500))
    route = _token_route()
    route["media_pricing"] = {**route["media_pricing"], **invalid}
    with pytest.raises(ProviderValidationError):
        await image_transport.generate_images(route, prompt="draw", size="auto", quality="auto", n=1)
    assert calls == []


# Rows mirror the live OpenAI Images API (2026-11-10): legacy models take presets only and quality <= high,
# gpt-image-2 adds custom sizes, 2.5/2.6 add xhigh/max, and only 2.6 accepts complexity.
@pytest.mark.parametrize("model,size,quality,complexity,accepted", [
    ("gpt-image-1.5", "1024x1024", "high", None, True),
    ("gpt-image-1.5", "1536x864", "low", None, False),
    ("gpt-image-1.5", "1024x1024", "xhigh", None, False),
    ("gpt-image-1-mini", "1536x864", "low", None, False),
    ("gpt-image-2", "1536x864", "low", None, True),
    ("gpt-image-2", "1024x1024", "xhigh", None, False),
    ("gpt-image-2-2026-04-21", "1536x864", "high", None, True),
    ("gpt-image-2.5-flare", "1536x864", "xhigh", None, True),
    ("gpt-image-2.5-sunburst", "1024x1024", "max", None, True),
    ("gpt-image-2.5-sunburst-2026-09-08", "auto", "max", None, True),
    ("gpt-image-2.5-sunburst", "1024x1024", "low", "low", False),
    ("gpt-image-2.6-flare", "1024x1024", "max", "high", True),
    ("gpt-image-2.6-sunburst", "auto", "auto", "medium", True),
    ("gpt-image-2.6-sunburst", "auto", "auto", "extreme", False),
    ("gpt-image-2.6-flare", "1024x1024", "ultra", None, False),
])
def test_openai_image_models_enforce_the_live_verified_option_matrix(model, size, quality, complexity, accepted):
    route = {**_token_route(), "api_model_name": model}
    call = lambda: image_transport.validate_image_request(  # noqa: E731
        route, size=size, quality=quality, n=1, edit=False, complexity=complexity)
    if accepted:
        assert call() == Decimal("0.01")
    else:
        with pytest.raises(ProviderValidationError):
            call()


@pytest.mark.parametrize("size,accepted", [
    ("1536x864", True), ("2048x704", True), ("2304x768", True),  # ratio exactly 3:1 is allowed
    ("3840x2160", True),  # both edge and total-pixel maximum
    ("1024x640", True),  # total-pixel minimum, 655,360
    ("1000x1000", False),  # not a multiple of 16
    ("1008x640", False),  # 645,120 px is below the minimum
    ("512x512", False),
    ("3856x2160", False),  # edge above 3840
    ("3840x2176", False),  # 8,355,840 px is above the maximum
    ("3072x1008", False),  # ratio above 3:1
    ("1024", False), ("0x1024", False), ("1536X864", False), (" 1536x864", False), ("1536x864\n", False),
])
def test_custom_image_sizes_follow_the_documented_bounds(size, accepted):
    route = {**_token_route(), "api_model_name": "gpt-image-2.5-flare"}
    call = lambda: image_transport.validate_image_request(route, size=size, quality="low", n=1, edit=False)  # noqa: E731
    if accepted:
        call()
    else:
        with pytest.raises(ProviderValidationError):
            call()


def test_custom_size_and_flagship_quality_need_exact_unit_prices_and_stay_model_scoped():
    flex = _route("openai", "gpt-image-2", {"1536x864:high": "0.20", "1024x1024:xhigh": "0.40"})
    assert image_transport.validate_image_request(flex, size="1536x864", quality="high", n=1, edit=False) == Decimal("0.20")
    with pytest.raises(ProviderValidationError):  # valid geometry, but no exact price for this quality
        image_transport.validate_image_request(flex, size="1536x864", quality="low", n=1, edit=False)
    # gpt-image-2 stops at high even when an administrator priced xhigh; the legacy model cannot use custom sizes.
    assert image_transport.available_image_variants(flex) == ["1536x864:high"]
    legacy = _route("openai", "gpt-image-1.5", {"1536x864:high": "0.20"})
    assert image_transport.available_image_variants(legacy) == []
    flagship = {**_token_route(), "api_model_name": "gpt-image-2.5-flare"}
    assert "1024x1024:max" in image_transport.available_image_variants(flagship)
    assert "1024x1024:max" not in image_transport.available_image_variants(_token_route())


@pytest.mark.asyncio
async def test_complexity_and_custom_size_reach_generation_and_edit_wires_only_when_requested(monkeypatch):
    requests = []

    def handle(request):
        requests.append(request)
        # The live 2.5/2.6 responses add a generation_id next to b64_json; it must be tolerated, not forwarded.
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(_PNG).decode(), "generation_id": "gen_1"}]})

    _mock_provider(monkeypatch, handle)
    route = _route("openai", "gpt-image-2.6-flare", {"1536x864:xhigh": "0.30"})
    kwargs = {"prompt": "Draw", "size": "1536x864", "quality": "xhigh", "n": 1}
    assert (await image_transport.generate_images(route, **kwargs, complexity="low")).images == [(_PNG, "image/png")]
    await image_transport.generate_images(route, **kwargs)
    await image_transport.generate_images(route, **kwargs, complexity="high", source_image=_PNG, source_mime="image/png")
    with_complexity, without, edit = requests
    assert json.loads(with_complexity.content) == {"model": "gpt-image-2.6-flare", **kwargs, "complexity": "low"}
    assert "complexity" not in json.loads(without.content)
    assert edit.url.path == "/v1/images/edits"
    assert b'name="complexity"' in edit.content and b"high" in edit.content


@pytest.mark.asyncio
async def test_unsupported_complexity_never_reaches_the_provider(monkeypatch):
    calls = []
    _mock_provider(monkeypatch, lambda request: calls.append(request) or httpx.Response(500))
    for model, provider in (("gpt-image-2.5-flare", "openai"), ("gpt-image-1.5", "openai"),
                            ("gemini-3.1-flash-image", "gemini")):
        route = _route(provider, model, {"auto:auto": "0.10"})
        with pytest.raises(ProviderValidationError, match="complexity"):
            await image_transport.generate_images(route, prompt="draw", size="auto", quality="auto", n=1,
                                                  complexity="low")
    assert calls == []


def test_complexity_is_fingerprinted_only_when_requested():
    from lumen.services.durable_runs import images as durable_images

    base = {"model_id": "7", "prompt": "draw", "size": "auto", "quality": "auto", "n": 1}
    assert "complexity" not in durable_images._intent(base)
    assert "complexity" not in durable_images._intent({**base, "complexity": None})
    assert durable_images._intent({**base, "complexity": "low"})["complexity"] == "low"


def _catalog_route(model):
    catalog = native_catalog_entry(model, "openai")
    route = _route("openai", model, {})
    return {**route, "media_pricing": catalog["media_pricing"],
            "input_price_per_token": Decimal(catalog["token_pricing"]["input"]) / 1_000_000}


@pytest.mark.parametrize("model,image_input,image_output,image_cache", [
    ("gpt-image-2", "8", "30", None),
    ("gpt-image-2-2026-04-21", "8", "30", None),
    ("gpt-image-2.5-sunburst", "8", "30", None),
    ("gpt-image-2.5-sunburst-2026-09-08", "8", "30", None),
    ("gpt-image-2.5-flare", "8", "30", None),
    ("gpt-image-2.5-flare-2026-09-08", "8", "30", None),
    ("gpt-image-2.6-sunburst", "8", "30", None),
    ("gpt-image-2.6-flare", "8", "30", None),
    ("gpt-image-1.5", "8", "32", "2"),
    ("gpt-image-1.5-2025-12-16", "8", "32", "2"),
    ("gpt-image-1-mini", "2.5", "8", "0.25"),
    ("chatgpt-image-latest", "8", "32", "2"),
])
def test_catalog_image_routes_are_priced_without_manual_reservation(model, image_input, image_output, image_cache):
    route = _catalog_route(model)
    rates = pricing.route_token_rates(route)["image"]
    assert (rates["input_per_million"], rates["output_per_million"], rates.get("cache_read_per_million")) == (
        image_input, image_output, image_cache,
    )
    assert "reservation_usd" not in route["media_pricing"]
    assert image_transport.image_route_ready(route)
    assert image_transport.available_image_options(route)["image_output_price_per_million"] == image_output
    assert not image_transport.image_route_ready({**route, "api_base": "https://proxy.example/v1"})


def test_retired_image_id_is_not_directly_executable():
    assert not image_transport.image_route_ready(_route("openai", "gpt-image-1", {"auto:auto": "0.10"}))


@pytest.mark.parametrize("model,size,quality,tokens", [
    ("gpt-image-2-2026-04-21", "auto", "auto", 35579),
    ("gpt-image-2", "1024x1536", "high", 8232),
    ("gpt-image-2.5-sunburst", "1024x1024", "high", 2634),
    ("gpt-image-2.6-sunburst", "1024x1024", "low", 1235),
    ("gpt-image-2.6-flare", "1024x1536", "medium", 1698),
    ("gpt-image-2.6-flare", "2880x2880", "max", 35579),
    ("gpt-image-1.5", "auto", "auto", 9423),
    ("gpt-image-1-mini", "1024x1024", "low", 1125),
])
def test_image_output_token_bounds(model, size, quality, tokens):
    assert output_token_bound(model, size, quality) == tokens


def test_request_hold_scales_with_count_frames_utf8_and_scanned_source():
    route_25 = _catalog_route("gpt-image-2.5-sunburst")
    route_26 = _catalog_route("gpt-image-2.6-flare")
    basic = {"size": "1024x1024", "quality": "high", "prompt": "Draw", "n": 1, "edit": False}
    assert image_transport.validate_image_request(route_25, **basic) == Decimal("0.080320")
    assert image_transport.validate_image_request(
        route_26, **{**basic, "size": "auto", "quality": "auto", "n": 2},
    ) == Decimal("2.136040")
    assert image_transport.validate_image_request(
        route_25, **{**basic, "edit": True}, source_dimensions=(1024, 1024),
    ) == Decimal("0.211392")
    assert image_transport.validate_image_request(
        route_26, **{**basic, "n": 2, "prompt": "그려"}, output_format="gif", frame_count=3,
    ) == Decimal("0.475430")
    with pytest.raises(ProviderValidationError, match="scanned source image dimensions"):
        image_transport.validate_image_request(route_25, **{**basic, "edit": True})


@pytest.mark.parametrize("model,changes", [
    ("gpt-image-2.5-sunburst", {"output_format": "gif"}),
    ("gpt-image-2.5-sunburst", {"output_format": "svg"}),
    ("gpt-image-2.6-flare", {"output_format": "svg", "n": 2}),
    ("gpt-image-2.6-flare", {"output_format": "svg", "edit": True}),
    ("gpt-image-2.6-flare", {"output_format": "jpeg", "background": "transparent"}),
    ("gpt-image-2.6-flare", {"output_compression": 100}),
    ("gpt-image-2.6-flare", {"output_format": "jpeg", "output_compression": 101}),
    ("gpt-image-2.6-flare", {"output_format": "webp", "frame_count": 4}),
    ("gpt-image-2.6-flare", {"output_format": "gif", "frame_count": 1}),
    ("gpt-image-2.6-flare", {"output_format": "gif", "frame_duration_ms": 2001}),
    ("gpt-image-2.6-flare", {"output_format": "gif", "size": "2880x2880", "n": 2}),
])
def test_image_format_validation_rejects_unsupported_combinations(model, changes):
    with pytest.raises(ProviderValidationError):
        image_transport.validate_image_request(
            _catalog_route(model), **{"size": "1024x1024", "quality": "high", "n": 1, "edit": False, **changes},
        )


@pytest.mark.asyncio
async def test_images_wire_forwards_raster_options_and_parses_image_only_usage(monkeypatch):
    route = _catalog_route("gpt-image-2.6-flare")
    options = {"background": "transparent", "output_format": "webp", "output_compression": 0, "complexity": "high"}
    encoded = base64.b64encode(b"RIFF0123WEBP").decode()
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"data": [{"b64_json": encoded}], "usage": {
            "input_tokens": 13, "input_tokens_details": {"image_tokens": 0, "text_tokens": 13},
            "output_tokens": 58, "total_tokens": 71,
        }})

    _mock_provider(monkeypatch, handle)
    args = {"prompt": "Draw", "size": "1024x1024", "quality": "high", "n": 1}
    result = await image_transport.generate_images(route, **args, **options)
    assert result.images == [(b"RIFF0123WEBP", "image/webp")]
    assert result.usage.modality_tokens["image"].output_tokens == 58
    assert json.loads(requests[0].content) == {"model": "gpt-image-2.6-flare", **args, **options}
    await image_transport.generate_images(route, **args, **options, source_image=_PNG,
                                          source_mime="image/png", source_dimensions=(1, 1))
    for key, value in options.items():
        assert f'name="{key}"\r\n\r\n{value}\r\n'.encode() in requests[1].content


@pytest.mark.asyncio
@pytest.mark.parametrize("output_format,bytes_,mime", [
    ("gif", b"GIF89a\x01", "image/gif"),
    ("svg", b'<svg xmlns="http://www.w3.org/2000/svg"/>', "image/svg+xml"),
])
async def test_gif_svg_wire_preserves_authoring_and_format(monkeypatch, output_format, bytes_, mime):
    author_usage = {"model": "gpt-4.1-mini", "input_tokens": 20, "output_tokens": 200,
                    "total_tokens": 220, "reasoning_tokens": 40}
    frames = {"frame_count": 4, "frame_duration_ms": 250} if output_format == "gif" else {}

    def handler(request):
        body = json.loads(request.content)
        assert body["output_format"] == output_format and "svg" not in body
        for name, value in frames.items():
            assert body[name] == value
        data = {"data": [{"b64_json": base64.b64encode(bytes_).decode()}],
                "usage": {"input_tokens": 13, "output_tokens": 58, "total_tokens": 71}}
        if output_format == "svg":
            data["svg_usage"] = author_usage
        return httpx.Response(200, json=data)

    _mock_provider(monkeypatch, handler)
    result = await image_transport.generate_images(
        _catalog_route("gpt-image-2.6-flare"), prompt="Draw", size="1024x1024", quality="low", n=1,
        output_format=output_format, **frames,
    )
    assert result.images == [(bytes_, mime)]
    assert result.svg_usage == (author_usage if output_format == "svg" else None)


@pytest.mark.parametrize("size,count,frames,accepted", [
    ("1024x1024", 2, 5, True),
    ("1040x1024", 2, 5, False),
    ("auto", 1, 2, False),
])
def test_gif_budget_is_bounded_by_the_output_byte_policy(size, count, frames, accepted):
    route = _catalog_route("gpt-image-2.6-flare")
    if accepted:
        assert image_transport.validate_image_request(
            route, size=size, quality="low", n=count, edit=False, output_format="gif", frame_count=frames,
        ) > 0
    else:
        with pytest.raises(ProviderValidationError, match="GIF"):
            image_transport.validate_image_request(
                route, size=size, quality="low", n=count, edit=False, output_format="gif", frame_count=frames,
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["gpt-image-2", "gpt-image-2.5-flare", "gpt-image-2.6-flare"])
async def test_direct_openai_image_cached_usage_fails_closed(monkeypatch, model):
    _mock_provider(monkeypatch, lambda request: httpx.Response(200, json={
        "data": [{"b64_json": base64.b64encode(_PNG).decode()}],
        "usage": {"input_tokens": 12, "output_tokens": 50, "total_tokens": 62,
                  "input_tokens_details": {"text_tokens": 12, "image_tokens": 0, "cached_tokens": 1}},
    }))
    with pytest.raises(image_transport.ImageTransportError, match="usage"):
        await image_transport.generate_images(
            _catalog_route(model), prompt="Draw", size="1024x1024", quality="high", n=1,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_author", [False, True])
async def test_svg_author_selection_wire_and_inconsistent_usage(monkeypatch, bad_author):
    from lumen.services.providers.image_billing import svg_author_pricing

    def handler(request):
        body = json.loads(request.content)
        assert body["svg"] == {"model": "gpt-5", "reasoning": {"effort": "low"}}
        assert "svg_model" not in body and "svg_reasoning_effort" not in body
        return httpx.Response(200, json={
            "data": [{"b64_json": base64.b64encode(b'<svg xmlns="http://www.w3.org/2000/svg"/>').decode()}],
            "usage": {"input_tokens": 12, "output_tokens": 50, "total_tokens": 62},
            "svg_usage": {"model": "gpt-5", "input_tokens": 20, "output_tokens": 200,
                          "total_tokens": 221 if bad_author else 220, "reasoning_tokens": 40},
        })

    _mock_provider(monkeypatch, handler)
    kwargs = dict(prompt="Draw", size="1024x1024", quality="high", n=1, output_format="svg",
                  svg_model="gpt-5", svg_reasoning_effort="low", author_prices={"gpt-5": svg_author_pricing()["gpt-5"]})
    route = _catalog_route("gpt-image-2.6-flare")
    if bad_author:
        with pytest.raises(image_transport.ImageTransportError, match="SVG authoring usage"):
            await image_transport.generate_images(route, **kwargs)
    else:
        assert (await image_transport.generate_images(route, **kwargs)).svg_usage["model"] == "gpt-5"


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["mask", "markup"])
async def test_openai_edit_forwards_ordered_sources_overlay_and_moderation(monkeypatch, field):
    def handler(request):
        assert request.url.path.endswith("/images/edits")
        assert request.content.count(b'name="image[]"') == 2
        assert request.content.index(_PNG) < request.content.index(b"\xff\xd8\xffmock")
        assert f'name="{field}"'.encode() in request.content
        assert b'name="moderation"\r\n\r\nlow\r\n' in request.content
        return httpx.Response(200, json={
            "data": [{"b64_json": base64.b64encode(_PNG).decode()}],
            "usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120,
                      "input_tokens_details": {"text_tokens": 5, "image_tokens": 95}},
        })

    _mock_provider(monkeypatch, handler)
    route = _catalog_route("gpt-image-2.5-flare")
    args = dict(prompt="Draw", size="1024x1024", quality="high", n=1, source_dimensions=(1, 1),
                additional_dimensions=[[1, 1], [1, 1]], moderation="low")
    hold = image_transport.validate_image_request(route, edit=True, source_count=2, overlay=field, **args)
    assert hold == Decimal("0.289216")  # $0.080320 + (3 × 8704 × $8/1M).
    result = await image_transport.generate_images(
        route, source_image=_PNG, source_mime="image/png",
        additional_sources=[(b"\xff\xd8\xffmock", "image/jpeg")],
        overlay_data=(field, _PNG, "image/png"), **args,
    )
    assert result.usage.modality_tokens["image"].input_tokens == 95


@pytest.mark.parametrize("model,fidelity,accepted", [
    ("gpt-image-1.5", "high", True), ("gpt-image-1-mini", "low", True),
    ("gpt-image-1-mini", "high", False), ("gpt-image-2", "low", False),
])
def test_image_input_fidelity_is_model_scoped(model, fidelity, accepted):
    route = _catalog_route(model)
    if accepted:
        assert image_transport.validate_image_request(
            route, size="1024x1024", quality="high", n=1, edit=True,
            input_fidelity=fidelity, source_dimensions=[1, 1],
        ) > 0
    else:
        with pytest.raises(ProviderValidationError, match="fidelity"):
            image_transport.validate_image_request(
                route, size="1024x1024", quality="high", n=1, edit=True,
                input_fidelity=fidelity, source_dimensions=[1, 1],
            )
