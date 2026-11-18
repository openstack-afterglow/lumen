"""Direct OpenAI Realtime and Google Gemini Live WebSocket route validation and options.

Realtime voice sessions connect directly to canonical upstream WebSocket endpoints
using configured API keys only. Subscription auth and custom proxies are rejected.
The explicit billing plan (separate PCM input/output duration rates, provider-connected
session time, or token rates plus a funding envelope) is frozen at session admission
and re-validated before connection. OpenAI token-basis conversation models without a
configured envelope get a Lumen-owned hold derived from the admitted duration.

OpenAI realtime-family models have one fixed *kind* each: ``conversation`` (GA
Realtime speech-to-speech), ``transcription`` (transcription-only sessions),
``translation`` (speech translation) and ``live`` (GPT-Live voice sessions). Each kind
has its own upstream endpoint, client event contract and permitted billing basis.
"""

from __future__ import annotations

import json
import re
from decimal import ROUND_CEILING, Decimal
from urllib.parse import quote

from .audio_transport import _GEMINI_VOICES, _OPENAI_VOICES
from .credentials import api_key_route_configured
from .errors import ProviderValidationError
from .pricing import exact_duration_price, frozen_token_pricing, media_billing_basis, media_reservation_usd

# Model -> realtime kind. Every other OpenAI model is not a direct realtime route.
_OPENAI_REALTIME_KINDS = {
    "gpt-realtime": "conversation",
    # Dated snapshots resolve the same catalog entry through its declared alias.
    "gpt-realtime-2025-08-28": "conversation",
    "gpt-realtime-mini": "conversation",
    "gpt-realtime-mini-2025-12-15": "conversation",
    "gpt-realtime-1.5": "conversation",
    "gpt-realtime-2": "conversation",
    "gpt-realtime-2.1": "conversation",
    "gpt-realtime-2.1-mini": "conversation",
    "gpt-4o-realtime-preview": "conversation",
    "gpt-4o-mini-realtime-preview": "conversation",
    "gpt-live-transcribe": "transcription",
    "gpt-realtime-whisper": "transcription",
    # Realtime transcription sessions; its file-STT row keeps a separate stt tariff.
    "gpt-transcribe": "transcription",
    "gpt-realtime-translate": "translation",
    "gpt-live-1": "live",
}
REALTIME_KINDS = ("conversation", "transcription", "translation", "live")
# GPT-Live adds its own voices to the standard OpenAI set; marin is its default.
_LIVE_VOICES = _OPENAI_VOICES | frozenset({
    "quartz", "ripple", "vesper", "willow", "stone", "gleam", "meridian", "bossa", "tempo", "beacon", "delta",
    "cinder", "nuri", "juni", "flitz", "brise", "sillage", "nira", "harema", "shida", "noeul",
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
# Billing basis -> required duration families, per realtime kind. Transcription and
# translation are priced by input audio duration only; GPT-Live by connected session time.
_KIND_BASES: dict[str, dict[str, tuple[str, ...]]] = {
    "conversation": {"duration": ("realtime_input", "realtime_output"), "session": ("realtime_session",),
                     "tokens": ()},
    "transcription": {"duration": ("realtime_input",)},
    "translation": {"duration": ("realtime_input",)},
    "live": {"session": ("realtime_session",)},
}
# Extra native session request fields each kind accepts (beyond model, provider, kind, duration).
_SESSION_FIELDS = {
    "conversation": ["voice", "instructions"],
    "transcription": [],
    "translation": ["output_language"],
    "live": ["voice", "instructions"],
}
# OpenAI conversation models whose published contract makes reasoning effort configurable.
# Reasoning tokens settle as reported output usage; the session default is "low".
REASONING_EFFORTS = ("minimal", "low", "medium", "high", "xhigh")
_CONFIGURABLE_REASONING_MODELS = frozenset({"gpt-realtime-2", "gpt-realtime-2.1"})


def reasoning_configurable(model: object) -> bool:
    return model in _CONFIGURABLE_REASONING_MODELS


# Conversation models whose published contract lists image input and function calling.
# Both need token billing: images and tool text settle as reported modality tokens.
_CONVERSATION_FEATURE_MODELS = frozenset({"gpt-realtime-2.1", "gpt-realtime-2.1-mini", "gpt-realtime-2",
                                          "gpt-realtime-1.5"})
MAX_FUNCTION_TOOLS = 16
MAX_FUNCTION_TOOLS_BYTES = 16384
_TOOL_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")
_MAX_TOOL_DESCRIPTION_CHARS = 1024
# Per-response limits pinned in every OpenAI token-basis conversation session, so one
# response has a known worst case: output is capped by max_output_tokens, and input by
# the instruction part (instructions and tool definitions, each byte-bounded; a token is
# assumed to cover at least one UTF-8 byte, plus fixed overhead) and the retention-ratio
# truncation limit on the conversation after instructions (audio, text, image, tool items).
RESPONSE_MAX_OUTPUT_TOKENS = 4096
RESPONSE_POST_INSTRUCTION_TOKENS = 16384
_INSTRUCTION_TOKEN_BOUND = 4 * _MAX_INSTRUCTIONS_CHARS + MAX_FUNCTION_TOOLS_BYTES + 512
# Lumen-owned token-basis hold for OpenAI conversation models without a configured
# reservation_usd. OpenAI documents realtime audio as 1 token per 100 ms of user audio
# and 1 token per 50 ms of assistant audio. Each admitted second reserves the user-audio
# tokens re-read as uncached input by up to 10 later responses, plus the assistant-audio
# tokens and 10 transcript text tokens of output, each at the model's highest frozen rate
# in that direction. Two worst-case responses are added on top: the relay continues only
# while confirmed usage plus two worst-case responses fits the hold.
_HOLD_INPUT_TOKENS_PER_SECOND = 10 * 10
_HOLD_OUTPUT_TOKENS_PER_SECOND = 20 + 10
_USD_QUANTUM = Decimal("0.0000000001")
_PER_MILLION = Decimal(1_000_000)


class RealtimeTransportError(RuntimeError):
    """The direct realtime provider WebSocket failed or returned invalid messages."""

    def __init__(self, message: str, *, meter: object | None = None) -> None:
        super().__init__(message)
        self.meter = meter


def _model(route: dict) -> tuple[str, str]:
    if not isinstance(route, dict) or route.get("model_kind") != "realtime" or not api_key_route_configured(route):
        raise ProviderValidationError("realtime route is not directly executable")
    provider, model = route.get("provider_type"), route.get("api_model_name")
    if not isinstance(model, str):
        raise ProviderValidationError("unsupported realtime provider or model")
    if provider == "openai" and model in _OPENAI_REALTIME_KINDS:
        bases = {"https://api.openai.com", "https://api.openai.com/v1"}
    elif provider == "gemini" and model in _GEMINI_REALTIME_MODELS:
        bases = {"https://generativelanguage.googleapis.com", "https://generativelanguage.googleapis.com/v1beta"}
    else:
        raise ProviderValidationError("unsupported realtime provider or model")
    base = route.get("api_base")
    if base is not None and (not isinstance(base, str) or base.rstrip("/") not in bases):
        raise ProviderValidationError("unsupported realtime provider endpoint")
    return provider, model


def realtime_kind(route: dict) -> str:
    """The model's fixed realtime kind; Gemini Live is always a conversation."""
    provider, model = _model(route)
    return _OPENAI_REALTIME_KINDS[model] if provider == "openai" else "conversation"


def duration_families(model_name: object) -> tuple[str, ...] | None:
    """Duration families a known OpenAI realtime model settles on, or ``None`` to keep the default."""
    kind = _OPENAI_REALTIME_KINDS.get(model_name) if isinstance(model_name, str) else None
    return _KIND_BASES[kind].get("duration") if kind else None


def _checked_rate(value: Decimal | None) -> Decimal:
    if (
        value is None
        or value <= 0
        or value >= Decimal("100000000")
        or value != value.quantize(Decimal("0.0000000001"))
    ):
        raise ProviderValidationError("exact configured realtime price is unavailable")
    return value


def _direction_rates(token_pricing: dict) -> tuple[Decimal, Decimal, Decimal]:
    """``(text input, highest input, highest output)`` per-token USD of a frozen token plan."""
    text_input = Decimal(token_pricing["input_price_per_token"])
    inputs, outputs = [text_input], [Decimal(token_pricing["output_price_per_token"])]
    for rates in (token_pricing.get("token_rates") or {}).values():
        for field, bucket in (("input_per_million", inputs), ("output_per_million", outputs)):
            if field in rates:
                bucket.append(Decimal(rates[field]) / _PER_MILLION)
    return text_input, max(inputs), max(outputs)


def _derived_token_hold(token_pricing: dict, max_duration_seconds: int) -> Decimal:
    """The duration-proportional part described at ``_HOLD_INPUT_TOKENS_PER_SECOND``."""
    _, max_input, max_output = _direction_rates(token_pricing)
    per_second = _HOLD_INPUT_TOKENS_PER_SECOND * max_input + _HOLD_OUTPUT_TOKENS_PER_SECOND * max_output
    return (per_second * max_duration_seconds).quantize(_USD_QUANTUM, rounding=ROUND_CEILING)


def response_bound_usd(token_pricing: dict) -> Decimal:
    """Worst-case USD of one response under the pinned per-response limits."""
    text_input, max_input, max_output = _direction_rates(token_pricing)
    worst = (_INSTRUCTION_TOKEN_BOUND * text_input + RESPONSE_POST_INSTRUCTION_TOKENS * max_input
             + RESPONSE_MAX_OUTPUT_TOKENS * max_output)
    return worst.quantize(_USD_QUANTUM, rounding=ROUND_CEILING)


def _billing_plan(route: dict, max_duration_seconds: int) -> dict:
    """Freeze exactly one billing basis; never mix duration, session and token rates."""
    provider, _ = _model(route)
    kind = realtime_kind(route)
    pricing = route.get("media_pricing")
    try:
        basis = media_billing_basis("realtime", pricing)
    except (ProviderValidationError, ValueError) as exc:
        raise ProviderValidationError("exact configured realtime price is unavailable") from exc
    if basis not in _KIND_BASES[kind]:
        raise ProviderValidationError(f"realtime {kind} models do not support {basis} billing")
    if basis != "tokens":
        rates = {}
        # Only the kind's families are frozen; other configured rates (e.g. an output
        # rate on an input-priced transcription model) are never required nor charged.
        for family in _KIND_BASES[kind][basis]:
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
        plan = {"billing_basis": basis, "media_pricing": dict(pricing), "duration_rates": rates}
        if kind != "conversation":
            plan["realtime_kind"] = kind
        return plan
    try:
        envelope = media_reservation_usd(pricing)
        token_pricing = frozen_token_pricing(route)
    except (ProviderValidationError, ValueError) as exc:
        raise ProviderValidationError("exact configured realtime token price is unavailable") from exc
    audio = (token_pricing.get("token_rates") or {}).get("audio") or {}
    # Realtime needs both text and audio directions, including explicit free rates.
    # Cache policy belongs to the shared calculator: optional modality cache uses its
    # frozen input rate; cached text without an explicit cache rate fails settlement closed.
    if (any(token_pricing.get(key) is None for key in (
            "input_price_per_token", "output_price_per_token"))
            or any(audio.get(key) is None for key in ("input_per_million", "output_per_million"))):
        raise ProviderValidationError("exact configured realtime token price is unavailable")
    plan: dict = {"billing_basis": "tokens", "token_pricing": token_pricing}
    if provider == "openai":
        # OpenAI sessions pin per-response limits, so the guard has a real per-response bound.
        bound = response_bound_usd(token_pricing)
        plan["response_bound_usd"] = format(bound, "f")
        if envelope is None:
            # Only OpenAI documents the audio token rate the derived hold relies on.
            envelope = _derived_token_hold(token_pricing, max_duration_seconds) + 2 * bound
            plan["reservation_source"] = "duration_policy"
        elif envelope < 2 * bound:
            # A configured reservation keeps precedence but must cover two worst-case responses.
            raise ProviderValidationError("configured realtime reservation is below two worst-case responses")
    if envelope is None or envelope <= 0 or envelope >= Decimal("100000000"):
        raise ProviderValidationError("realtime token billing requires a positive reservation_usd")
    return {**plan, "reservation_usd": format(envelope, "f")}


def conversation_features(route: dict) -> dict[str, bool]:
    """Image input and function calling: published for the model and settled as tokens."""
    provider, model = _model(route)
    pricing = route.get("media_pricing")
    try:
        tokens = media_billing_basis("realtime", pricing) == "tokens"
    except (ProviderValidationError, ValueError):
        tokens = False
    enabled = (provider == "openai" and tokens and realtime_kind(route) == "conversation"
               and model in _CONVERSATION_FEATURE_MODELS)
    image_rates = ((pricing or {}).get("token_rates") or {}).get("image") or {}
    return {"image_input": enabled and "input_per_million" in image_rates, "function_calling": enabled}


def function_tools(tools: object, tool_choice: object = None) -> tuple[list[dict], object]:
    """Validate client function tools; hosted, MCP and other tool types are refused."""
    invalid = ProviderValidationError("unsupported realtime function tools")
    if not isinstance(tools, list) or not tools or len(tools) > MAX_FUNCTION_TOOLS:
        raise invalid
    names: set[str] = set()
    clean: list[dict] = []
    for tool in tools:
        if (not isinstance(tool, dict) or set(tool) - {"type", "name", "description", "parameters"}
                or tool.get("type") != "function"):
            raise invalid
        name, description, parameters = tool.get("name"), tool.get("description"), tool.get("parameters")
        if not isinstance(name, str) or not _TOOL_NAME.fullmatch(name) or name in names:
            raise invalid
        if description is not None and (not isinstance(description, str)
                                        or len(description) > _MAX_TOOL_DESCRIPTION_CHARS):
            raise invalid
        if parameters is not None and not isinstance(parameters, dict):
            raise invalid
        names.add(name)
        clean.append({key: tool[key] for key in ("type", "name", "description", "parameters") if key in tool})
    try:
        size = len(json.dumps(clean, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode())
    except (TypeError, ValueError) as exc:
        raise invalid from exc
    if size > MAX_FUNCTION_TOOLS_BYTES:
        raise invalid
    if tool_choice is not None and tool_choice not in ("auto", "none", "required") and not (
            isinstance(tool_choice, dict) and set(tool_choice) == {"type", "name"}
            and tool_choice["type"] == "function" and tool_choice["name"] in names):
        raise invalid
    return clean, tool_choice


def validate_realtime_request(
    route: dict,
    *,
    voice: str | None = None,
    instructions: str | None = None,
    max_duration_seconds: int = _DEFAULT_MAX_DURATION_SECONDS,
    reasoning_effort: str | None = None,
    tools: list | None = None,
    tool_choice: object = None,
) -> dict:
    """Return the JSON-serializable billing plan to freeze before any provider I/O."""
    provider, model = _model(route)
    kind = realtime_kind(route)
    if type(max_duration_seconds) is not int or not _MIN_DURATION_SECONDS <= max_duration_seconds <= _MAX_DURATION_SECONDS:
        raise ProviderValidationError("unsupported realtime session duration")
    if instructions is not None and (not isinstance(instructions, str) or len(instructions) > _MAX_INSTRUCTIONS_CHARS):
        raise ProviderValidationError("realtime instructions are invalid or too long")
    if reasoning_effort is not None and (
            provider != "openai" or not reasoning_configurable(model) or reasoning_effort not in REASONING_EFFORTS):
        raise ProviderValidationError("unsupported realtime reasoning effort")
    if tools is not None or tool_choice is not None:
        if not conversation_features(route)["function_calling"]:
            raise ProviderValidationError("realtime function calling is unavailable for this model")
        function_tools(tools, tool_choice)
    if kind in {"transcription", "translation"}:
        # No speech output and no system prompt: nothing else may be frozen at admission.
        if voice is not None or instructions is not None:
            raise ProviderValidationError(f"realtime {kind} sessions accept no voice or instructions")
        return _billing_plan(route, max_duration_seconds)
    allowed_voices = _LIVE_VOICES if kind == "live" else _OPENAI_VOICES if provider == "openai" else _GEMINI_VOICES
    if voice is not None and (not isinstance(voice, str) or voice not in allowed_voices):
        raise ProviderValidationError("unsupported realtime voice")
    return _billing_plan(route, max_duration_seconds)


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
    """Return executable voices, PCM16 sample rates, duration bounds and session fields.

    Transcription and translation sessions have no selectable voice; transcription
    produces no audio output at all. Reasoning effort is advertised only where configurable.
    """
    provider, model = _model(route)
    validate_realtime_request(route)
    kind = realtime_kind(route)
    if kind == "live":
        voices, default = sorted(_LIVE_VOICES), "marin"
    elif kind in {"transcription", "translation"}:
        voices, default = [], None
    else:
        voices = sorted(_GEMINI_VOICES if provider == "gemini" else _OPENAI_VOICES)
        default = "Kore" if provider == "gemini" else "alloy"
    reasoning = provider == "openai" and reasoning_configurable(model)
    features = conversation_features(route)
    fields = [*_SESSION_FIELDS[kind], *(["reasoning_effort"] if reasoning else []),
              *(["tools", "tool_choice"] if features["function_calling"] else [])]
    return {
        "realtime_kind": kind,
        "available_voices": voices,
        "default_voice": default,
        "input_sample_rate_hz": 16000 if provider == "gemini" else 24000,
        "output_sample_rate_hz": 0 if kind == "transcription" else 24000,
        "max_duration_seconds": _MAX_DURATION_SECONDS,
        "default_duration_seconds": _DEFAULT_MAX_DURATION_SECONDS,
        "realtime_session_fields": fields,
        "available_reasoning_efforts": list(REASONING_EFFORTS) if reasoning else [],
        "default_reasoning_effort": "low" if reasoning else None,
        "realtime_image_input": features["image_input"],
        "realtime_function_calling": features["function_calling"],
    }


def upstream_connection_spec(route: dict) -> tuple[str, dict[str, str]]:
    """Build a server-only upstream handshake; Gemini's URL contains the API key, never log it."""
    provider, model = _model(route)
    api_key = route["api_key"].strip()
    if provider == "openai":
        url = {
            "conversation": f"wss://api.openai.com/v1/realtime?model={model}",
            # The transcription model is configured by the first session.update, never the URL.
            "transcription": "wss://api.openai.com/v1/realtime?intent=transcription",
            "translation": f"wss://api.openai.com/v1/realtime/translations?model={model}",
            # GPT-Live selects its model in the mandatory session.start frame.
            "live": "wss://api.openai.com/v1/live/sessions",
        }[_OPENAI_REALTIME_KINDS[model]]
        return url, {"Authorization": f"Bearer {api_key}"}
    return (
        "wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta."
        f"GenerativeService.BidiGenerateContent?key={quote(api_key, safe='')}",
        {},
    )
