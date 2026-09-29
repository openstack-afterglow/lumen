"""Direct OpenAI Realtime and Google Gemini Live WebSocket route validation and options.

Realtime voice sessions connect directly to canonical upstream WebSocket endpoints
using configured API keys only. Subscription auth and custom proxies are rejected.
Configured per-minute input/output audio prices are frozen at session admission.
"""

from __future__ import annotations

from decimal import Decimal
from urllib.parse import quote

from .audio_transport import _GEMINI_VOICES, _OPENAI_VOICES
from .errors import ProviderValidationError
from .pricing import exact_media_price

_OPENAI_REALTIME_MODELS = frozenset({
    "gpt-realtime",
    "gpt-realtime-mini",
    "gpt-realtime-2.1",
    "gpt-4o-realtime-preview",
    "gpt-4o-mini-realtime-preview",
})
_GEMINI_REALTIME_MODELS = frozenset({
    "gemini-2.5-flash-live",
    "gemini-2.5-flash-native-audio",
    "gemini-2.0-flash-live-001",
    "gemini-3.8-live",
})
_DEFAULT_MAX_DURATION_SECONDS = 300
_MAX_DURATION_SECONDS = 900
_MIN_DURATION_SECONDS = 10
_MAX_INSTRUCTIONS_CHARS = 4096


class RealtimeTransportError(RuntimeError):
    """The direct realtime provider WebSocket failed or returned invalid messages."""


def _model(route: dict) -> tuple[str, str]:
    if not isinstance(route, dict) or route.get("model_kind") != "realtime" or route.get("provider_auth") is not None:
        raise ProviderValidationError("realtime route is not directly executable")
    provider, model = route.get("provider_type"), route.get("api_model_name")
    if not isinstance(model, str):
        raise ProviderValidationError("unsupported realtime provider or model")
    if provider == "openai" and model in _OPENAI_REALTIME_MODELS:
        bases = {"https://api.openai.com", "https://api.openai.com/v1"}
    elif provider == "gemini" and model in _GEMINI_REALTIME_MODELS:
        bases = {"https://generativelanguage.googleapis.com", "https://generativelanguage.googleapis.com/v1beta"}
    else:
        raise ProviderValidationError("unsupported realtime provider or model")
    base = route.get("api_base")
    if base is not None and (not isinstance(base, str) or base.rstrip("/") not in bases):
        raise ProviderValidationError("unsupported realtime provider endpoint")
    if not isinstance(route.get("api_key"), str) or not route["api_key"].strip():
        raise ProviderValidationError("realtime provider credential is unavailable")
    return provider, model


def validate_realtime_request(
    route: dict,
    *,
    voice: str | None = None,
    instructions: str | None = None,
    max_duration_seconds: int = _DEFAULT_MAX_DURATION_SECONDS,
) -> tuple[Decimal, Decimal]:
    """Return exact configured (input_per_minute, output_per_minute) USD rates before I/O."""
    provider, _ = _model(route)
    if type(max_duration_seconds) is not int or not _MIN_DURATION_SECONDS <= max_duration_seconds <= _MAX_DURATION_SECONDS:
        raise ProviderValidationError("unsupported realtime session duration")
    if instructions is not None and (not isinstance(instructions, str) or len(instructions) > _MAX_INSTRUCTIONS_CHARS):
        raise ProviderValidationError("realtime instructions are invalid or too long")
    allowed_voices = _OPENAI_VOICES if provider == "openai" else _GEMINI_VOICES
    if voice is not None and (not isinstance(voice, str) or voice not in allowed_voices):
        raise ProviderValidationError("unsupported realtime voice")
    pricing = route.get("media_pricing")
    in_rate = exact_media_price(pricing, "realtime_input_per_minute")
    out_rate = exact_media_price(pricing, "realtime_output_per_minute")
    for rate in (in_rate, out_rate):
        if (
            rate is None
            or rate <= 0
            or rate >= Decimal("100000000")
            or rate != rate.quantize(Decimal("0.0000000001"))
        ):
            raise ProviderValidationError("exact configured realtime price is unavailable")
    return in_rate, out_rate


def realtime_route_ready(route: dict) -> bool:
    """Advertise only supported direct realtime routes with both input and output rates."""
    if not isinstance(route, dict):
        return False
    try:
        validate_realtime_request(route)
    except ProviderValidationError:
        return False
    return True


def available_realtime_options(route: dict) -> dict[str, object]:
    """Return executable voices, PCM16 sample rates and duration bounds for a ready route."""
    provider, _ = _model(route)
    validate_realtime_request(route)
    voices = sorted(_GEMINI_VOICES if provider == "gemini" else _OPENAI_VOICES)
    return {
        "available_voices": voices,
        "default_voice": "Kore" if provider == "gemini" else "alloy",
        "input_sample_rate_hz": 16000 if provider == "gemini" else 24000,
        "output_sample_rate_hz": 24000,
        "max_duration_seconds": _MAX_DURATION_SECONDS,
        "default_duration_seconds": _DEFAULT_MAX_DURATION_SECONDS,
    }


def upstream_connection_spec(route: dict) -> tuple[str, dict[str, str]]:
    """Build a server-only upstream handshake; Gemini's URL contains the API key, never log it."""
    provider, model = _model(route)
    api_key = route["api_key"].strip()
    if provider == "openai":
        return (
            f"wss://api.openai.com/v1/realtime?model={model}",
            {"Authorization": f"Bearer {api_key}"},
        )
    return (
        "wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta."
        f"GenerativeService.BidiGenerateContent?key={quote(api_key, safe='')}",
        {},
    )
