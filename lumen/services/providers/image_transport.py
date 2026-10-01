"""Direct, single-attempt OpenAI/Gemini image transport for durable image runs.

``validate_image_request`` resolves a configured ``size:quality`` USD-per-image
rate before admission. ``generate_images`` returns bounded (bytes, MIME) pairs;
it never retrieves image URLs and never retries a provider request.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from decimal import Decimal

import httpx

from lumen.services.usage_breakdown import UsageBreakdown

from .errors import ProviderValidationError
from .pricing import (
    exact_media_price,
    media_billing_basis,
    media_reservation_usd,
    route_media_pricing_available,
    route_token_rates,
    validate_media_pricing,
)

_MAX_IMAGE_BYTES = 5 * 1024 * 1024  # Generated-asset persistence limit.
_MAX_SOURCE_BYTES = 5 * 1024 * 1024
_MAX_PROMPT_CHARS = 32000
_MAX_RESPONSE_BYTES_PER_IMAGE = (_MAX_IMAGE_BYTES * 4 // 3) + 8192
_TIMEOUT = httpx.Timeout(120.0, connect=10.0, read=120.0, write=30.0, pool=10.0)
_OPENAI_MODELS = frozenset({"gpt-image-1", "gpt-image-1-mini", "gpt-image-1.5"})
_GEMINI_MODELS = frozenset({
    "gemini-2.5-flash-image", "gemini-2.5-flash-image-preview",
    "gemini-3-pro-image", "gemini-3-pro-image-preview",
    "gemini-3.1-flash-image", "gemini-3.1-flash-image-preview",
})
_SIZES = {"auto": None, "1024x1024": "1:1", "1536x1024": "3:2", "1024x1536": "2:3"}
_MIMES = {"image/png", "image/jpeg", "image/webp"}


class ImageTransportError(RuntimeError):
    """An image provider failed or returned an unusable bounded result."""


@dataclass(frozen=True)
class ImageResult:
    images: list[tuple[bytes, str]]
    usage: UsageBreakdown | None = None


def _image_usage(route: dict, result: dict, provider: str) -> UsageBreakdown | None:
    if image_billing_basis(route) != "tokens":
        return None
    raw = result.get("usage")
    try:
        if not isinstance(raw, dict):
            raise ValueError("image usage is missing")
        if provider == "openai":
            output_details = raw.get("output_tokens_details")
            if not isinstance(output_details, dict) or "image_tokens" not in output_details:
                raise ValueError("image output modality usage is missing")
            parsed = UsageBreakdown.from_openai_media(raw)
        else:
            output_details = raw.get("output_tokens_by_modality")
            if not isinstance(output_details, list) or not any(
                isinstance(item, dict) and item.get("modality") == "image" for item in output_details
            ):
                raise ValueError("image output modality usage is missing")
            parsed = UsageBreakdown.from_gemini_interactions(raw)
        if parsed is None or "image" not in parsed.modality_tokens:
            raise ValueError("image modality usage is missing")
        return parsed
    except (ValueError, TypeError, KeyError) as exc:
        raise ImageTransportError("image provider token usage is unavailable or invalid") from exc


def _model(route: dict) -> tuple[str, str]:
    """Accept only canonical direct endpoints, API-key authentication and known models."""
    if route.get("model_kind") != "image" or route.get("provider_auth") is not None:
        raise ProviderValidationError("image route is not directly executable")
    provider = route.get("provider_type")
    model = route.get("api_model_name")
    if provider == "openai" and model in _OPENAI_MODELS:
        accepted_bases = {"https://api.openai.com", "https://api.openai.com/v1"}
    elif provider == "gemini" and model in _GEMINI_MODELS:
        accepted_bases = {"https://generativelanguage.googleapis.com",
                          "https://generativelanguage.googleapis.com/v1beta"}
    else:
        raise ProviderValidationError("unsupported image provider or model")
    # A custom proxy is not necessarily compatible with the direct-provider API.
    configured_base = route.get("api_base")
    if configured_base is not None and (not isinstance(configured_base, str)
                                        or configured_base.rstrip("/") not in accepted_bases):
        raise ProviderValidationError("unsupported image provider endpoint")
    if not isinstance(route.get("api_key"), str) or not route["api_key"].strip():
        raise ProviderValidationError("image provider credential is unavailable")
    return provider, model


def image_billing_basis(route: dict) -> str:
    return media_billing_basis("image", route.get("media_pricing"))


def validate_image_request(route: dict, *, size: str, quality: str, n: int, edit: bool) -> Decimal:
    """Return USD per unit or the whole-request token envelope, before provider I/O.

    OpenAI GPT Image 1/1-mini/1.5: sizes auto, 1024x1024, 1536x1024,
    1024x1536; qualities auto, low, medium, high; count 1–10.
    Gemini 2.5 Flash Image: same sizes, quality auto, count 1. Gemini 3
    Pro/3.1 Flash Image: same sizes, quality auto/1k/2k/4k, count 1.
    Gemini dimension presets select output aspect ratio, not guaranteed pixels.
    Unit mode requires an exact ``image_variants`` entry for every requested
    combination, including auto; token mode prices actual provider counts only.
    """
    provider, model = _model(route)
    if not isinstance(size, str) or size not in _SIZES or not isinstance(quality, str):
        raise ProviderValidationError("unsupported image size or quality")
    if type(n) is not int or not 1 <= n <= (10 if provider == "openai" else 1):
        raise ProviderValidationError("unsupported image count")
    if type(edit) is not bool:
        raise ProviderValidationError("invalid image operation")
    if provider == "openai":
        qualities = {"auto", "low", "medium", "high"}
    elif model.startswith("gemini-2.5-"):
        qualities = {"auto"}
    else:
        qualities = {"auto", "1k", "2k", "4k"}
    if quality not in qualities:
        raise ProviderValidationError("unsupported image quality")
    if image_billing_basis(route) == "tokens":
        pricing = validate_media_pricing("image", route.get("media_pricing"))
        reservation = media_reservation_usd(pricing)
        if reservation is None or not route_media_pricing_available({**route, "media_pricing": pricing}):
            raise ProviderValidationError("image token pricing or reservation is unavailable")
        if edit and "input_per_million" not in route_token_rates(route).get("image", {}):
            raise ProviderValidationError("image input token price is unavailable")
        return reservation
    pricing = route.get("media_pricing")
    variants = pricing.get("image_variants") if isinstance(pricing, dict) else None
    variant = f"{size}:{quality}"
    if not isinstance(variants, dict) or variant not in variants:
        raise ProviderValidationError("exact image variant price is unavailable")
    price = exact_media_price(pricing, "image_per_unit", variant=variant)
    if (price is None or price <= 0 or price * n >= Decimal("100000000")
            or price != price.quantize(Decimal("0.0000000001"))):
        raise ProviderValidationError("exact image variant price is unavailable")
    return price


def _image_mime(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    raise ImageTransportError("image provider returned an unsupported image")


def available_image_variants(route: dict) -> list[str]:
    """List executable size:quality choices for the route's selected billing basis."""
    pricing = route.get("media_pricing")
    try:
        if image_billing_basis(route) == "tokens":
            variants = {f"{size}:{quality}" for size in _SIZES for quality in ("auto", "low", "medium", "high", "1k", "2k", "4k")}
        else:
            variants = pricing.get("image_variants") if isinstance(pricing, dict) else None
    except ProviderValidationError:
        return []
    if not isinstance(variants, (dict, set)):
        return []
    available = []
    for variant in variants:
        if not isinstance(variant, str) or ":" not in variant:
            continue
        size, quality = variant.rsplit(":", 1)
        try:
            validate_image_request(route, size=size, quality=quality, n=1, edit=False)
        except ProviderValidationError:
            continue
        available.append(variant)
    return sorted(available)


def image_route_ready(route: dict) -> bool:
    """True only if an API-key direct route has an executable priced variant."""
    return bool(available_image_variants(route))


def max_image_count(route: dict) -> int:
    """Maximum images per direct request; unsupported routes fail closed."""
    provider, _ = _model(route)
    return 10 if provider == "openai" else 1


def _decode_image(encoded: object, mime: str | None = None) -> tuple[bytes, str]:
    if not isinstance(encoded, str) or not encoded or len(encoded) > _MAX_RESPONSE_BYTES_PER_IMAGE:
        raise ImageTransportError("image provider returned oversized or empty image data")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ImageTransportError("image provider returned invalid image data") from exc
    if not raw or len(raw) > _MAX_IMAGE_BYTES:
        raise ImageTransportError("image provider returned oversized or empty image data")
    detected = _image_mime(raw)
    if mime is not None and (mime not in _MIMES or mime != detected):
        raise ImageTransportError("image provider returned an invalid image type")
    return raw, detected


async def _post(url: str, *, headers: dict, count: int, json_body: dict | None = None,
                form: dict | None = None, files: dict | None = None) -> dict:
    # Streaming avoids buffering an unlimited upstream body in httpx before validation.
    max_bytes = count * _MAX_RESPONSE_BYTES_PER_IMAGE + 65536
    try:
        async with (
            httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False, trust_env=False) as client,
            client.stream("POST", url, headers=headers, json=json_body, data=form, files=files) as response,
        ):
            if response.status_code != 200:
                raise ImageTransportError(f"image provider returned HTTP {response.status_code}")
            if response.headers.get("content-length", "").isdecimal() and int(response.headers["content-length"]) > max_bytes:
                raise ImageTransportError("image provider response exceeds size limit")
            chunks = bytearray()
            async for chunk in response.aiter_bytes():
                if len(chunks) + len(chunk) > max_bytes:
                    raise ImageTransportError("image provider response exceeds size limit")
                chunks.extend(chunk)
    except httpx.HTTPError as exc:
        raise ImageTransportError("image provider request failed") from exc
    try:
        result = json.loads(chunks)
    except (ValueError, UnicodeError) as exc:
        raise ImageTransportError("image provider returned invalid JSON") from exc
    if not isinstance(result, dict):
        raise ImageTransportError("image provider returned invalid JSON")
    return result


async def generate_images(route: dict, *, prompt: str, size: str, quality: str, n: int,
                          source_image: bytes | None = None, source_mime: str | None = None) -> ImageResult:
    """Execute one direct call, preserving provider usage alongside bounded image bytes."""
    validate_image_request(route, size=size, quality=quality, n=n, edit=source_image is not None)
    provider, model = _model(route)
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > _MAX_PROMPT_CHARS:
        raise ProviderValidationError("image prompt is empty or too long")
    if source_image is not None:
        if not isinstance(source_image, bytes) or not 0 < len(source_image) <= _MAX_SOURCE_BYTES:
            raise ProviderValidationError("source image exceeds size limit")
        if source_mime not in _MIMES or _image_mime(source_image) != source_mime:
            raise ProviderValidationError("source image MIME is invalid")
    elif source_mime is not None:
        raise ProviderValidationError("source image MIME without data")
    if provider == "openai":
        body = {"model": model, "prompt": prompt, "size": size, "quality": quality, "n": n}
        headers = {"Authorization": f"Bearer {route['api_key']}"}
        if source_image is None:
            result = await _post("https://api.openai.com/v1/images/generations", headers=headers,
                                 count=n, json_body=body)
        else:
            result = await _post("https://api.openai.com/v1/images/edits", headers=headers,
                                 count=n, form=body,
                                 files={"image": ("source.png", source_image, source_mime)})
        data = result.get("data")
        if not isinstance(data, list) or len(data) != n:
            raise ImageTransportError("image provider returned an unexpected image count")
        if any(not isinstance(item, dict) or "url" in item for item in data):
            raise ImageTransportError("image provider returned a URL instead of inline data")
        return ImageResult([_decode_image(item.get("b64_json")) for item in data], _image_usage(route, result, provider))

    inputs: list[dict] = [{"type": "text", "text": prompt}]
    if source_image is not None:
        inputs.append({"type": "image", "mime_type": source_mime,
                       "data": base64.b64encode(source_image).decode("ascii")})
    response_format: dict = {"type": "image"}
    if size != "auto":
        response_format["aspect_ratio"] = _SIZES[size]
    if quality != "auto":
        response_format["image_size"] = quality.upper()
    result = await _post(
        "https://generativelanguage.googleapis.com/v1beta/interactions",
        headers={"x-goog-api-key": route["api_key"]}, count=1,
        json_body={"model": model, "input": inputs, "response_format": response_format, "store": False},
    )
    if result.get("status") != "completed":
        raise ImageTransportError("image provider did not complete image generation")
    steps = result.get("steps")
    if not isinstance(steps, list):
        raise ImageTransportError("image provider returned no output steps")
    images: list[tuple[bytes, str]] = []
    for step in steps:
        if not isinstance(step, dict):
            raise ImageTransportError("image provider returned invalid output steps")
        if step.get("type") != "model_output":
            continue
        contents = step.get("content")
        if not isinstance(contents, list):
            raise ImageTransportError("image provider returned invalid output content")
        for content in contents:
            if not isinstance(content, dict):
                raise ImageTransportError("image provider returned invalid output content")
            if content.get("type") == "image":
                if "uri" in content or "url" in content:
                    raise ImageTransportError("image provider returned a URL instead of inline data")
                images.append(_decode_image(content.get("data"), content.get("mime_type")))
    if len(images) != 1:
        raise ImageTransportError("image provider returned an unexpected image count")
    return ImageResult(images, _image_usage(route, result, provider))
