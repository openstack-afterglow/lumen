"""Direct image provider contract: exact billing, inline results and bounded outputs."""

import base64
import json
from decimal import Decimal

import httpx
import pytest

from lumen.services.providers import image_transport, pricing
from lumen.services.providers.errors import ProviderValidationError

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
        transport=httpx.MockTransport(handler), **kwargs,
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
    assert await image_transport.generate_images(route, **kwargs) == [(_PNG, "image/png")]
    assert await image_transport.generate_images(route, **kwargs, source_image=_PNG, source_mime="image/png") == [(_PNG, "image/png")]
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
    assert await image_transport.generate_images(route, **kwargs) == [(_PNG, "image/png")]
    assert await image_transport.generate_images(route, **kwargs, source_image=_PNG, source_mime="image/png") == [(_PNG, "image/png")]
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
    route = _route("openai", "gpt-image-1", {"auto:auto": "0.10"})
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
