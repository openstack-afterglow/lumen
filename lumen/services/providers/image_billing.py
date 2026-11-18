"""Lumen funding policy for OpenAI Images. Never used as provider usage evidence."""

from __future__ import annotations

from decimal import ROUND_CEILING, Decimal
from functools import cache

from lumen.services.native_catalog import native_catalog

from .errors import ProviderValidationError

MILLION = Decimal(1_000_000)
_LEGACY_SQUARE = {"low": 9, "medium": 34, "high": 133}
_LEGACY_RECTANGLE = {"low": 13, "medium": 50, "high": 200}
_MINI_SQUARE = {"low": 5, "medium": 11, "high": 36}
_MINI_RECTANGLE = {"low": 6, "medium": 15, "high": 52}
_GRIDS = {"low": 16, "medium": 24, "high": 48, "xhigh": 64, "max": 96}
_IMAGE_2_GRIDS = {"low": 16, "medium": 48, "high": 96}
_IMAGE_26_MULTIPLIERS = {"low": 42, "medium": 33}
# An author sees a prompt/reference image and supplies one bounded document.
_SVG_INPUT_ENVELOPE = 65_536
_SVG_OUTPUT_ENVELOPE = 32_768


def _ceil_div(value: int, divisor: int) -> int:
    return (value + divisor - 1) // divisor


def output_token_bound(model: str, size: str, quality: str) -> int:
    """Use the published estimator with 50% headroom, rounded upward."""
    if not model.startswith("gpt-image-2"):
        # Older model pages publish rounded image-cost estimates, not a token
        # formula. Allow $0.001 of rounding before converting to output tokens.
        square = size == "1024x1024"
        mini = model == "gpt-image-1-mini"
        if mini:
            table = _MINI_SQUARE if square else _MINI_RECTANGLE
        else:
            table = _LEGACY_SQUARE if square else _LEGACY_RECTANGLE
        estimate = _ceil_div((table["high" if quality == "auto" else quality] + 1) * 1000, 8 if mini else 32)
    else:
        width, height = (2880, 2880) if size == "auto" else tuple(map(int, size.split("x")))
        grids = _IMAGE_2_GRIDS if model in {"gpt-image-2", "gpt-image-2-2026-04-21"} else _GRIDS
        quality = next(reversed(grids)) if quality == "auto" else quality
        long, short = max(width, height), min(width, height)
        grid = grids[quality]
        q, remainder = divmod(grid * short, long)
        # Exact half-to-even; floating-point aspect ratios can obscure a tie.
        q += 2 * remainder > long or (2 * remainder == long and q % 2 == 1)
        estimate = _ceil_div(grid * q * (2_000_000 + width * height), 4_000_000)
        if model.startswith("gpt-image-2.6-"):
            estimate = (estimate * _IMAGE_26_MULTIPLIERS.get(quality, 10) + 5) // 10
    return _ceil_div(3 * estimate, 2)


def input_image_token_bound(dimensions: tuple[int, int] | list[int] | None) -> int:
    """Conservative high-fidelity envelope for one scanned raster input."""
    if not isinstance(dimensions, (list, tuple)) or len(dimensions) != 2:
        raise ProviderValidationError("scanned source image dimensions are required")
    width, height = dimensions
    if (type(width) is not int or type(height) is not int or width <= 0 or height <= 0
            or width * height > 40_000_000):
        raise ProviderValidationError("scanned source image dimensions are invalid")
    return 8192 + 512 * _ceil_div(width, 256) * _ceil_div(height, 256)


@cache
def svg_author_pricing() -> dict[str, dict]:
    """Build the exact published text rate lookup; freeze with admission prices."""
    rates = {}
    for entry in native_catalog()["providers"]["openai"]["models"]:
        token = entry["token_pricing"] or {}
        if entry["kind"] != "text" or "input" not in token or "output" not in token:
            continue
        # SVG usage has no modality or cache split. A model with a distinct
        # vision input rate cannot be priced from the authoring summary alone.
        media_input = ((entry["media_pricing"] or {}).get("token_rates", {}).get("image", {}).get("input_per_million"))
        if media_input is not None and Decimal(media_input) != Decimal(token["input"]):
            continue
        tier = token.get("long_context") or {}
        snapshot = {
            "input_price_per_token": format(Decimal(token["input"]) / MILLION, "f"),
            "output_price_per_token": format(Decimal(token["output"]) / MILLION, "f"),
            "long_context_threshold_tokens": tier.get("threshold_tokens", 200_000),
        }
        for direction in ("input", "output"):
            if direction in tier:
                snapshot[f"{direction}_price_per_token_above_200k"] = format(Decimal(tier[direction]) / MILLION, "f")
        for name in (entry["id"], *entry["aliases"]):
            rates[name] = snapshot
    return rates


def svg_author_hold(prices: dict[str, dict], prompt_bytes: int) -> Decimal:
    if not prices:
        raise ProviderValidationError("SVG authoring rates are unavailable")
    # Account for the prompt plus a tiled reference and hidden authoring input.
    # Reserve at the largest known text rates, including long-context tiers.
    return max(
        max(Decimal(price[f"input_price_per_token{suffix}"]) for suffix in ("", "_above_200k")
            if f"input_price_per_token{suffix}" in price) * (prompt_bytes + _SVG_INPUT_ENVELOPE)
        + max(Decimal(price[f"output_price_per_token{suffix}"]) for suffix in ("", "_above_200k")
              if f"output_price_per_token{suffix}" in price) * _SVG_OUTPUT_ENVELOPE
        for price in prices.values()
    )


def openai_image_hold(model: str, *, size: str, quality: str, count: int, frames: int, prompt: str,
                      edit: bool, source_dimensions: tuple[int, int] | list[int] | None,
                      image_rates: dict[str, str], text_input_rate: object, author_prices: dict[str, dict] | None) -> Decimal:
    try:
        prompt_bytes = len(prompt.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ProviderValidationError("image prompt must be valid UTF-8") from exc
    output = output_token_bound(model, size, quality) * count * frames
    image_cost = output * Decimal(image_rates["output_per_million"])
    if edit:
        image_cost += input_image_token_bound(source_dimensions) * Decimal(image_rates["input_per_million"])
    total = image_cost / MILLION + (prompt_bytes + 256) * Decimal(str(text_input_rate))
    if author_prices is not None:
        total += svg_author_hold(author_prices, prompt_bytes)
    if total <= 0 or total >= Decimal(100_000_000):
        raise ProviderValidationError("image credit hold is outside the supported range")
    return total.quantize(Decimal("0.0000000001"), rounding=ROUND_CEILING)


def parse_svg_usage(value: object) -> dict:
    """Check charge-bearing counts; reasoning is part of output, not additive."""
    required = {"model", "input_tokens", "output_tokens", "total_tokens", "reasoning_tokens"}
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("SVG authoring usage is missing or unsupported")
    model = value["model"]
    counts = [value[key] for key in ("input_tokens", "output_tokens", "total_tokens", "reasoning_tokens")]
    if not isinstance(model, str) or not 1 <= len(model) <= 190 or any(type(n) is not int or n < 0 for n in counts):
        raise ValueError("SVG authoring usage is invalid")
    input_tokens, output_tokens, total, reasoning = counts
    if not input_tokens or not output_tokens or input_tokens + output_tokens != total or reasoning > output_tokens:
        raise ValueError("SVG authoring usage is inconsistent")
    return value
