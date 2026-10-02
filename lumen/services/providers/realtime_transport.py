"""Direct OpenAI Realtime and Google Gemini Live WebSocket route validation and options.

Realtime voice sessions connect directly to canonical upstream WebSocket endpoints
using configured API keys only. Subscription auth and custom proxies are rejected.
The explicit billing plan (separate PCM input/output duration rates, provider-connected
session time, or token rates plus a funding envelope) is frozen at session admission
and re-validated before connection.
"""

from __future__ import annotations

from decimal import Decimal
from urllib.parse import quote

from .audio_transport import _GEMINI_VOICES, _OPENAI_VOICES
from .errors import ProviderValidationError
from .pricing import exact_duration_price, frozen_token_pricing, media_billing_basis, media_reservation_usd

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
# Upstream close-handshake timeout, not additional session funding.
CLOSE_TIMEOUT_SECONDS = 5
_DURATION_FAMILIES = {"duration": ("realtime_input", "realtime_output"), "session": ("realtime_session",)}


class RealtimeTransportError(RuntimeError):
    """The direct realtime provider WebSocket failed or returned invalid messages."""

    def __init__(self, message: str, *, meter: object | None = None) -> None:
        super().__init__(message)
        self.meter = meter


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


def _checked_rate(value: Decimal | None) -> Decimal:
    if (
        value is None
        or value <= 0
        or value >= Decimal("100000000")
        or value != value.quantize(Decimal("0.0000000001"))
    ):
        raise ProviderValidationError("exact configured realtime price is unavailable")
    return value


def _billing_plan(route: dict) -> dict:
    """Freeze exactly one billing basis; never mix duration, session and token rates."""
    pricing = route.get("media_pricing")
    try:
        basis = media_billing_basis("realtime", pricing)
    except (ProviderValidationError, ValueError) as exc:
        raise ProviderValidationError("exact configured realtime price is unavailable") from exc
    if basis in _DURATION_FAMILIES:
        rates = {}
        for family in _DURATION_FAMILIES[basis]:
            try:
                exact = exact_duration_price(pricing, family)
            except (ProviderValidationError, ValueError) as exc:
                raise ProviderValidationError("exact configured realtime price is unavailable") from exc
            if exact is None:
                raise ProviderValidationError("exact configured realtime price is unavailable")
            rate, unit_seconds = exact
            _checked_rate(rate)
            if type(unit_seconds) is not int or unit_seconds not in {1, 60, 3600}:
                raise ProviderValidationError("exact configured realtime price is unavailable")
            rates[family] = {"rate": format(rate, "f"), "unit_seconds": unit_seconds}
        # The requested-unit JSON is frozen verbatim; Decimal conversion happens only at settlement.
        return {"billing_basis": basis, "media_pricing": dict(pricing), "duration_rates": rates}
    if basis != "tokens":
        raise ProviderValidationError("unsupported realtime billing basis")
    try:
        envelope = media_reservation_usd(pricing)
        token_pricing = frozen_token_pricing(route)
    except (ProviderValidationError, ValueError) as exc:
        raise ProviderValidationError("exact configured realtime token price is unavailable") from exc
    if envelope is None or envelope <= 0 or envelope >= Decimal("100000000"):
        raise ProviderValidationError("realtime token billing requires a positive reservation_usd")
    audio = (token_pricing.get("token_rates") or {}).get("audio") or {}
    # Realtime needs both text and audio directions, including explicit free rates.
    # Cache policy belongs to the shared calculator: optional modality cache uses its
    # frozen input rate; cached text without an explicit cache rate fails settlement closed.
    if (any(token_pricing.get(key) is None for key in (
            "input_price_per_token", "output_price_per_token"))
            or any(audio.get(key) is None for key in ("input_per_million", "output_per_million"))):
        raise ProviderValidationError("exact configured realtime token price is unavailable")
    return {"billing_basis": "tokens", "reservation_usd": format(envelope, "f"), "token_pricing": token_pricing}


def validate_realtime_request(
    route: dict,
    *,
    voice: str | None = None,
    instructions: str | None = None,
    max_duration_seconds: int = _DEFAULT_MAX_DURATION_SECONDS,
) -> dict:
    """Return the JSON-serializable billing plan to freeze before any provider I/O."""
    provider, _ = _model(route)
    if type(max_duration_seconds) is not int or not _MIN_DURATION_SECONDS <= max_duration_seconds <= _MAX_DURATION_SECONDS:
        raise ProviderValidationError("unsupported realtime session duration")
    if instructions is not None and (not isinstance(instructions, str) or len(instructions) > _MAX_INSTRUCTIONS_CHARS):
        raise ProviderValidationError("realtime instructions are invalid or too long")
    allowed_voices = _OPENAI_VOICES if provider == "openai" else _GEMINI_VOICES
    if voice is not None and (not isinstance(voice, str) or voice not in allowed_voices):
        raise ProviderValidationError("unsupported realtime voice")
    return _billing_plan(route)


def realtime_route_ready(route: dict) -> bool:
    """Advertise only supported direct realtime routes with a complete explicit billing plan."""
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
