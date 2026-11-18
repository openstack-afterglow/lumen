"""Direct, single-attempt OpenAI/Gemini image transport for durable image runs.

``validate_image_request`` resolves a unit price or token hold before admission.
``generate_images`` returns bounded (bytes, MIME) pairs;
it never retrieves image URLs and never retries a provider request.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass
from decimal import Decimal

import httpx

from lumen.services.usage_breakdown import UsageBreakdown

from .credentials import api_key_route_configured, resolve_route_api_key
from .errors import ProviderValidationError
from .image_billing import openai_image_hold, parse_svg_usage, svg_author_pricing
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
# Legacy GPT Image models take presets only; GPT Image 2+ takes custom WxH.
# gpt-image-1 shut down in October 2026.
_OPENAI_PRESET_MODELS = frozenset({
    "gpt-image-1-mini", "gpt-image-1.5", "gpt-image-1.5-2025-12-16", "chatgpt-image-latest",
})
_OPENAI_FLEX_MODELS = frozenset({"gpt-image-2", "gpt-image-2-2026-04-21"})
_OPENAI_FLAGSHIP_MODELS = frozenset({
    "gpt-image-2.5-flare", "gpt-image-2.5-flare-2026-09-08",
    "gpt-image-2.5-sunburst", "gpt-image-2.5-sunburst-2026-09-08",
    "gpt-image-2.6-flare", "gpt-image-2.6-sunburst",
})
_OPENAI_COMPLEXITY_MODELS = frozenset({"gpt-image-2.6-flare", "gpt-image-2.6-sunburst"})
_OPENAI_MODELS = _OPENAI_PRESET_MODELS | _OPENAI_FLEX_MODELS | _OPENAI_FLAGSHIP_MODELS
_COMPLEXITIES = frozenset({"low", "medium", "high"})
_CUSTOM_SIZE = re.compile(r"([1-9][0-9]{0,4})x([1-9][0-9]{0,4})")
_GEMINI_MODELS = frozenset({
    "gemini-2.5-flash-image", "gemini-2.5-flash-image-preview",
    "gemini-3-pro-image", "gemini-3-pro-image-preview",
    "gemini-3.1-flash-image", "gemini-3.1-flash-image-preview",
})
_SIZES = {"auto": None, "1024x1024": "1:1", "1536x1024": "3:2", "1024x1536": "2:3"}
_MIMES = {"image/png", "image/jpeg", "image/webp"}
_FORMAT_MIME = {"png": "image/png", "jpeg": "image/jpeg", "webp": "image/webp",
                "gif": "image/gif", "svg": "image/svg+xml"}
_CUSTOM_SIZE_LIMITS = {"min_pixels": 655_360, "max_pixels": 8_294_400, "dimension_multiple": 16,
                       "max_edge": 3840, "max_aspect_ratio": 3}
GIF_LIMITS = {"min_frame_count": 2, "max_frame_count": 16, "default_frame_count": 9,
              "min_frame_duration_ms": 20, "max_frame_duration_ms": 2000, "default_frame_duration_ms": 200,
              "max_total_pixels": 16 * _CUSTOM_SIZE_LIMITS["max_pixels"]}
IMAGE_OPTION_KEYS = ("complexity", "background", "output_format", "output_compression",
                     "frame_count", "frame_duration_ms")


def image_options(payload: dict) -> dict:
    """Omit unset options on both the provider wire and durable intent."""
    return {key: payload[key] for key in IMAGE_OPTION_KEYS if payload.get(key) is not None}


def reject_streaming(*, stream: object, partial_images: object) -> None:
    if (stream is not None and stream is not False) or (
            partial_images is not None and (type(partial_images) is not int or partial_images != 0)):
        raise ProviderValidationError("image streaming and partial images are not supported")


class ImageTransportError(RuntimeError):
    """An image provider failed or returned an unusable bounded result."""

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        # Only the integer upstream status survives; provider bodies are never retained.
        self.status_code = status_code


@dataclass(frozen=True)
class ImageResult:
    images: list[tuple[bytes, str]]
    usage: UsageBreakdown | None = None
    svg_usage: dict | None = None


def _image_usage(route: dict, result: dict, provider: str) -> UsageBreakdown | None:
    if image_billing_basis(route) != "tokens":
        return None
    raw = result.get("usage")
    try:
        if not isinstance(raw, dict):
            raise ValueError("image usage is missing")
        if provider == "openai":
            model = route["api_model_name"]
            input_details = raw.get("input_tokens_details")
            output_details = raw.get("output_tokens_details")
            if model not in _OPENAI_PRESET_MODELS and isinstance(input_details, dict) and input_details.get("cached_tokens"):
                raise ValueError("direct Images cache usage is unsupported")
            if "output_tokens_details" not in raw and (
                    model not in _OPENAI_PRESET_MODELS or model == "gpt-image-1-mini"):
                # The official response makes the split optional. These models
                # have exclusively image output (SVG authoring is separate).
                output_details = {"image_tokens": raw.get("output_tokens")}
                raw = {**raw, "output_tokens_details": output_details}
            if not isinstance(output_details, dict) or "image_tokens" not in output_details:
                raise ValueError("image output modality usage is missing")
            for details, direction in ((input_details, "input"), (output_details, "output")):
                if isinstance(details, dict) and "text_tokens" in details:
                    text, image = details["text_tokens"], details.get("image_tokens", 0)
                    if (type(text) is not int or text < 0 or type(image) is not int or image < 0
                            or text + image != raw.get(f"{direction}_tokens")):
                        raise ValueError("image modality totals are inconsistent")
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
    if not isinstance(route, dict) or route.get("model_kind") != "image" or not api_key_route_configured(route):
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
    return provider, model


def image_billing_basis(route: dict) -> str:
    return media_billing_basis("image", route.get("media_pricing"))


def _custom_size(size: str) -> bool:
    """WIDTHxHEIGHT per the GPT Image 2+ rules: multiples of 16, ratio within 3:1, edge <= 3840, 0.65–8.29 MP."""
    match = _CUSTOM_SIZE.fullmatch(size)
    if match is None:
        return False
    width, height = int(match[1]), int(match[2])
    limits = _CUSTOM_SIZE_LIMITS
    multiple, ratio = limits["dimension_multiple"], limits["max_aspect_ratio"]
    return (width % multiple == 0 and height % multiple == 0 and max(width, height) <= limits["max_edge"]
            and width <= ratio * height and height <= ratio * width
            and limits["min_pixels"] <= width * height <= limits["max_pixels"])


def validate_image_request(
    route: dict, *, size: str, quality: str, n: int, edit: bool, prompt: str = "",
    source_dimensions: tuple[int, int] | list[int] | None = None,
    complexity: str | None = None, background: str | None = None, output_format: str | None = None,
    output_compression: int | None = None, frame_count: int | None = None, frame_duration_ms: int | None = None,
    author_prices: dict[str, dict] | None = None,
) -> Decimal:
    """Return USD per unit or the whole-request token hold, before provider I/O."""
    provider, model = _model(route)
    if not isinstance(size, str) or not isinstance(quality, str):
        raise ProviderValidationError("unsupported image size or quality")
    custom_sizes = provider == "openai" and model not in _OPENAI_PRESET_MODELS
    if size not in _SIZES and not (custom_sizes and _custom_size(size)):
        raise ProviderValidationError("unsupported image size or quality")
    if type(n) is not int or not 1 <= n <= (10 if provider == "openai" else 1):
        raise ProviderValidationError("unsupported image count")
    if type(edit) is not bool:
        raise ProviderValidationError("invalid image operation")
    token_mode = image_billing_basis(route) == "tokens"
    _validate_options(provider, model, size=size, n=n, edit=edit, token_mode=token_mode, complexity=complexity,
                      background=background, output_format=output_format, output_compression=output_compression,
                      frame_count=frame_count, frame_duration_ms=frame_duration_ms)
    if provider == "openai":
        qualities = {"auto", "low", "medium", "high"}
        if model in _OPENAI_FLAGSHIP_MODELS:
            qualities |= {"xhigh", "max"}
    elif model.startswith("gemini-2.5-"):
        qualities = {"auto"}
    else:
        qualities = {"auto", "1k", "2k", "4k"}
    if quality not in qualities:
        raise ProviderValidationError("unsupported image quality")
    if token_mode:
        pricing = validate_media_pricing("image", route.get("media_pricing"))
        if not route_media_pricing_available({**route, "media_pricing": pricing}):
            raise ProviderValidationError("image token pricing is unavailable")
        image_rates = route_token_rates(route).get("image", {})
        if edit and "input_per_million" not in image_rates:
            raise ProviderValidationError("image input token price is unavailable")
        if output_format == "svg":
            author_prices = svg_author_pricing() if author_prices is None else author_prices
            if not author_prices:
                raise ProviderValidationError("SVG authoring rates are unavailable")
        reservation = media_reservation_usd(pricing)
        if reservation is not None:
            return reservation
        if provider != "openai":
            raise ProviderValidationError("image token reservation is unavailable")
        frames = (frame_count or GIF_LIMITS["default_frame_count"]) if output_format == "gif" else 1
        return openai_image_hold(model, size=size, quality=quality, count=n, frames=frames, prompt=prompt,
                                 edit=edit, source_dimensions=source_dimensions, image_rates=image_rates,
                                 text_input_rate=route["input_price_per_token"], author_prices=author_prices)
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


def _validate_options(
    provider: str, model: str, *, size: str, n: int, edit: bool, token_mode: bool, complexity: str | None,
    background: str | None, output_format: str | None, output_compression: int | None,
    frame_count: int | None, frame_duration_ms: int | None,
) -> None:
    flagship = model in _OPENAI_COMPLEXITY_MODELS
    if complexity is not None and (not flagship or not isinstance(complexity, str) or complexity not in _COMPLEXITIES):
        raise ProviderValidationError("unsupported image complexity")
    if provider != "openai" and any(option is not None for option in (
            background, output_format, output_compression, frame_count, frame_duration_ms)):
        raise ProviderValidationError("image output options require an OpenAI Images model")
    if background is not None and background not in ("auto", "opaque", "transparent"):
        raise ProviderValidationError("unsupported image background")
    output = "png" if output_format is None else output_format
    if not isinstance(output, str) or output not in _FORMAT_MIME:
        raise ProviderValidationError("unsupported image output format")
    if output in ("gif", "svg") and not (flagship and token_mode):
        raise ProviderValidationError("GIF and SVG require a token-billed GPT Image 2.6 model")
    if output == "svg" and (edit or n != 1):
        raise ProviderValidationError("SVG is available only for generations with n=1")
    if background == "transparent" and output not in ("png", "webp"):
        raise ProviderValidationError("transparent background requires PNG or WebP")
    if output_compression is not None and (output not in ("jpeg", "webp") or type(output_compression) is not int
                                           or not 0 <= output_compression <= 100):
        raise ProviderValidationError("output compression requires JPEG or WebP and a value from 0 to 100")
    if output != "gif" and (frame_count is not None or frame_duration_ms is not None):
        raise ProviderValidationError("frame options require GIF output")
    if output == "gif":
        frames = GIF_LIMITS["default_frame_count"] if frame_count is None else frame_count
        if type(frames) is not int or not GIF_LIMITS["min_frame_count"] <= frames <= GIF_LIMITS["max_frame_count"]:
            raise ProviderValidationError("GIF frame count must be between 2 and 16")
        if frame_duration_ms is not None and (type(frame_duration_ms) is not int or not
                GIF_LIMITS["min_frame_duration_ms"] <= frame_duration_ms <= GIF_LIMITS["max_frame_duration_ms"]):
            raise ProviderValidationError("GIF frame duration must be between 20 and 2000 milliseconds")
        if size != "auto":
            width, height = map(int, size.split("x"))
            if width * height * frames * n > GIF_LIMITS["max_total_pixels"]:
                raise ProviderValidationError("GIF request exceeds total frame pixel budget")


def _image_mime(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    raise ImageTransportError("image provider returned an unsupported image")


def _openai_result_image(encoded: object, output_format: str | None) -> tuple[bytes, str]:
    if output_format not in ("gif", "svg"):
        return _decode_image(encoded, _FORMAT_MIME.get(output_format))
    raw = _decode_base64(encoded)
    if output_format == "gif" and not raw.startswith((b"GIF87a", b"GIF89a")):
        raise ImageTransportError("image provider returned an invalid GIF")
    # SVG has no fixed signature. The asset inspector parses UTF-8 XML and
    # verifies the static SVG subset before scanning or exposing the result.
    return raw, _FORMAT_MIME[output_format]


def available_image_variants(route: dict) -> list[str]:
    """List executable size:quality choices for the route's selected billing basis."""
    pricing = route.get("media_pricing")
    try:
        if image_billing_basis(route) == "tokens":
            variants = {f"{size}:{quality}" for size in _SIZES
                        for quality in ("auto", "low", "medium", "high", "xhigh", "max", "1k", "2k", "4k")}
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


def available_image_options(route: dict) -> dict:
    provider, model = _model(route)
    options: dict = {}
    token_mode = image_billing_basis(route) == "tokens"
    if token_mode:
        options["image_output_price_per_million"] = route_token_rates(route)["image"]["output_per_million"]
    if provider != "openai":
        return options
    formats = ["png", "jpeg", "webp"]
    options.update(image_output_formats=formats, image_backgrounds=["auto", "opaque", "transparent"])
    if token_mode and model not in _OPENAI_PRESET_MODELS:
        options["image_custom_size"] = dict(_CUSTOM_SIZE_LIMITS)
    if model in _OPENAI_COMPLEXITY_MODELS:
        options["image_complexities"] = ["low", "medium", "high"]
        if token_mode:
            formats.append("gif")
            options["image_gif"] = dict(GIF_LIMITS)
            if svg_author_pricing():
                formats.append("svg")
    return options


def _decode_base64(encoded: object) -> bytes:
    if not isinstance(encoded, str) or not encoded or len(encoded) > _MAX_RESPONSE_BYTES_PER_IMAGE:
        raise ImageTransportError("image provider returned oversized or empty image data")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ImageTransportError("image provider returned invalid image data") from exc
    if not raw or len(raw) > _MAX_IMAGE_BYTES:
        raise ImageTransportError("image provider returned oversized or empty image data")
    return raw


def _decode_image(encoded: object, mime: str | None = None) -> tuple[bytes, str]:
    raw = _decode_base64(encoded)
    detected = _image_mime(raw)
    if mime is not None and (mime not in _MIMES or mime != detected):
        raise ImageTransportError("image provider returned an invalid image type")
    return raw, detected


async def _post(url: str, *, headers: dict, count: int, json_body: dict | None = None,
                form: dict | None = None, files: dict | None = None, relay_ref=None, relay_model=None) -> dict:
    # Streaming avoids buffering an unlimited upstream body in httpx before validation.
    max_bytes = count * _MAX_RESPONSE_BYTES_PER_IMAGE + 65536
    try:
        async with (
            httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False, trust_env=False,
                              transport=httpx.AsyncHTTPTransport(retries=0, trust_env=False)) as client,
            client.stream("POST", url, headers=headers, json=json_body, data=form, files=files) as response,
        ):
            from .relay_history import record_dispatch
            record_dispatch(relay_ref, endpoint=url, model=relay_model, operation="image", status_code=response.status_code)
            if response.status_code != 200:
                raise ImageTransportError(f"image provider returned HTTP {response.status_code}",
                                          status_code=response.status_code)
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


async def generate_images(
    route: dict, *, prompt: str, size: str, quality: str, n: int, source_image: bytes | None = None,
    source_mime: str | None = None, source_dimensions: tuple[int, int] | list[int] | None = None,
    author_prices: dict[str, dict] | None = None, **options,
) -> ImageResult:
    """Execute one direct call, preserving provider usage alongside bounded image bytes."""
    provider, model = _model(route)
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > _MAX_PROMPT_CHARS:
        raise ProviderValidationError("image prompt is empty or too long")
    validate_image_request(route, size=size, quality=quality, n=n, edit=source_image is not None,
                           prompt=prompt, source_dimensions=source_dimensions, author_prices=author_prices, **options)
    if source_image is not None:
        if not isinstance(source_image, bytes) or not 0 < len(source_image) <= _MAX_SOURCE_BYTES:
            raise ProviderValidationError("source image exceeds size limit")
        if source_mime not in _MIMES or _image_mime(source_image) != source_mime:
            raise ProviderValidationError("source image MIME is invalid")
    elif source_mime is not None:
        raise ProviderValidationError("source image MIME without data")
    api_key = await resolve_route_api_key(route)
    if provider == "openai":
        body = {"model": model, "prompt": prompt, "size": size, "quality": quality, "n": n, **image_options(options)}
        headers = {"Authorization": f"Bearer {api_key}"}
        if source_image is None:
            result = await _post("https://api.openai.com/v1/images/generations", headers=headers,
                                 relay_ref=route.get("provider_auth"), relay_model=model,
                                 count=n, json_body=body)
        else:
            result = await _post("https://api.openai.com/v1/images/edits", headers=headers,
                                 relay_ref=route.get("provider_auth"), relay_model=model,
                                 count=n, form=body,
                                 files={"image": (f"source.{source_mime.split('/')[1]}", source_image, source_mime)})
        data = result.get("data")
        if not isinstance(data, list) or len(data) != n:
            raise ImageTransportError("image provider returned an unexpected image count")
        if any(not isinstance(item, dict) or "url" in item for item in data):
            raise ImageTransportError("image provider returned a URL instead of inline data")
        output_format = options.get("output_format")
        usage = _image_usage(route, result, provider)
        svg_usage = None
        if output_format == "svg" or result.get("svg_usage") is not None:
            try:
                if output_format != "svg":
                    raise ValueError("unexpected SVG authoring")
                svg_usage = parse_svg_usage(result.get("svg_usage"))
            except ValueError as exc:
                raise ImageTransportError("SVG authoring usage is unavailable or invalid") from exc
        return ImageResult([_openai_result_image(item.get("b64_json"), output_format) for item in data], usage, svg_usage)

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
        relay_ref=route.get("provider_auth"), relay_model=model,
        headers={"x-goog-api-key": api_key}, count=1,
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
