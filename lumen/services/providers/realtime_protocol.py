"""Ephemeral realtime PCM16 relay; only aggregate byte counts and provider token usage escape.

OpenAI realtime kinds keep their vendor event names on the compatible wire. Every
client event is whitelisted per kind and rebuilt from validated fields only; provider
frames pass through except raw provider error detail, which is replaced.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import re
import struct
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import websockets
from fastapi import WebSocket, WebSocketDisconnect
from websockets.exceptions import ConnectionClosed

from lumen.services.usage_breakdown import UsageBreakdown

from .errors import ProviderValidationError
from .realtime_transport import (
    CLOSE_TIMEOUT_SECONDS,
    REASONING_EFFORTS,
    RESPONSE_MAX_OUTPUT_TOKENS,
    RESPONSE_POST_INSTRUCTION_TOKENS,
    RealtimeTransportError,
    conversation_features,
    function_tools,
    realtime_kind,
    reasoning_configurable,
    upstream_connection_spec,
)

_MAX_FRAME_BYTES = 64 * 1024
_MAX_PCM_BYTES = 32 * 1024
# Conversation image input (PNG/JPEG data URLs, the formats the Realtime item schema lists).
# Image-enabled sessions accept larger frames in both directions so an image fits one event.
MAX_INPUT_IMAGE_BYTES = 1024 * 1024
MAX_INPUT_IMAGE_SIDE = 2048
MAX_SESSION_IMAGES = 8
_MAX_IMAGE_FRAME_BYTES = 2 * 1024 * 1024
_IMAGE_DATA_URL = re.compile(r"data:(image/png|image/jpeg);base64,([A-Za-z0-9+/]+={0,2})")
_IMAGE_DETAILS = frozenset({"auto", "low", "high"})
_MAX_CALL_ID_CHARS = 128
_MAX_FUNCTION_OUTPUT_CHARS = 32768
_IDLE_SECONDS = 60
_USAGE_DRAIN_SECONDS = 15
_MAX_METERED_RESPONSES = 4096
_PCM24 = {"type": "audio/pcm", "rate": 24000}
_SAFE_PROVIDER_ERROR = {"type": "server_error", "message": "Realtime provider returned an error"}
_TRANSCRIPTION_DELAYS = frozenset({"minimal", "low", "medium", "high", "xhigh"})
_TRANSCRIPTION_LANGUAGE = re.compile(r"[A-Za-z]{2,3}(-[A-Za-z]{2,4})?")
# Options each transcription-session model takes (probed against the provider 2026-11-10); other
# models accept the full set.
_TRANSCRIPTION_OPTIONS_ALL = frozenset({"prompt", "keywords", "languages", "language", "delay"})
_TRANSCRIPTION_OPTIONS = {"gpt-realtime-whisper": frozenset({"language", "delay"}),
                          # Official: languages[] replaces language; delay is Whisper-only.
                          "gpt-transcribe": frozenset({"prompt", "keywords", "languages"})}
_TRANSLATION_LANGUAGE = re.compile(r"[a-z]{2,3}(-[A-Za-z0-9]{2,8})?")
_MAX_TRANSCRIPTION_PROMPT_CHARS = 2048
_MAX_KEYWORDS = 50
_MAX_KEYWORD_CHARS = 100
_MAX_LANGUAGES = 8
# Kinds whose upstream must receive session.close and be drained until session.closed,
# otherwise translated or spoken audio still in flight is dropped.
_DRAINED_CLOSE_KINDS = frozenset({"translation", "live"})
# Native ticketed Studio events -> vendor events for streamed translation and GPT-Live.
# A translation's target language is fixed at admission, so no native session.update exists.
_NATIVE_STREAM_CLIENT_EVENTS = {
    "translation": {"audio.input.append": "session.input_audio_buffer.append"},
    "live": {"audio.input.append": "session.input_audio.append", "audio.input.mute": "session.input_audio.mute",
             "audio.input.unmute": "session.input_audio.unmute",
             "instructions.append": "session.instructions.append"},
}
_NATIVE_STREAM_TRANSCRIPTS = {"session.input_transcript.delta": "transcript.input.delta",
                              "session.output_transcript.delta": "transcript.output.delta"}
# GPT-Live accepts audio only after session.started.
_LIVE_START_SECONDS = 10
_REALTIME_SETUP_SECONDS = 10
# GPT-Live's session.closed must arrive before the funded deadline's transport abort.
_LIVE_CLOSE_MARGIN_SECONDS = 0.5


@dataclass(frozen=True)
class AudioMeter:
    input_bytes: int
    output_bytes: int
    input_sample_rate_hz: int
    output_sample_rate_hz: int
    # Token-basis sessions only: canonical provider usage and whether it is final.
    # ``usage_state`` is "unmetered" (other bases), "complete", "incomplete" or "invalid".
    usage: UsageBreakdown | None = None
    usage_state: str = "unmetered"
    image_sent: bool = False
    # Monotonic seconds from completed upstream open handshake to completed close.
    connected_seconds: Decimal | None = None
    # GPT-Live only: the provider's final billable voice seconds from ``session.closed`` and
    # its state ("unmetered" for other kinds, "complete", "missing" or "invalid").
    provider_seconds: Decimal | None = None
    provider_seconds_state: str = "unmetered"


def _usage_seconds(usage: object) -> Decimal | None:
    value = usage.get("seconds") if isinstance(usage, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    seconds = Decimal(str(value))
    return seconds if seconds.is_finite() and seconds >= 0 else None


class LiveUsageMeter:
    """GPT-Live voice seconds as the provider reports them; Lumen never substitutes its own clock.

    ``session.usage.updated`` carries the cumulative total so far and is kept only as progress
    (latest value, never summed). ``session.closed`` ``usage.seconds`` is the authoritative final
    value. A missing, malformed or decreasing value leaves the session unsettleable.
    """

    def __init__(self) -> None:
        self.progress: Decimal | None = None
        self.final: Decimal | None = None
        self.invalid = False

    def observe(self, message: dict) -> None:
        event = message.get("type")
        if self.invalid or self.final is not None or event not in {"session.usage.updated", "session.closed"}:
            return
        seconds = _usage_seconds(message.get("usage"))
        if seconds is None or (self.progress is not None and seconds < self.progress):
            self.invalid = True
        elif event == "session.closed":
            self.final = seconds
        else:
            self.progress = seconds

    def result(self) -> tuple[Decimal | None, str]:
        if self.invalid:
            return None, "invalid"
        return self.final, "complete" if self.final is not None else "missing"


def _not_less(before: UsageBreakdown, after: UsageBreakdown) -> bool:
    """Whether a cumulative snapshot never decreases or stops reporting a counter."""
    counters = ("input_tokens", "output_tokens", "cache_read_input_tokens",
                "cache_creation_5m_input_tokens", "cache_creation_1h_input_tokens")
    if any(getattr(after, name) < getattr(before, name) for name in counters):
        return False
    for modality, counts in before.modality_tokens.items():
        current = after.modality_tokens.get(modality)
        if current is None:
            return False
        for name in ("input_tokens", "output_tokens", "cache_read_input_tokens"):
            previous, value = getattr(counts, name), getattr(current, name)
            if previous is not None and (value is None or value < previous):
                return False
    return True


class TokenUsageMeter:
    """Session token usage from provider-reported counters only, never from PCM length.

    OpenAI Realtime reports per-response usage on ``response.done``; repeated frames for
    the same response id are de-duplicated and a conflicting repeat invalidates the meter.
    Gemini Live ``usageMetadata`` is a cumulative session snapshot; the latest monotonic
    snapshot is authoritative and any decrease invalidates the meter.
    """

    def __init__(self, provider: str) -> None:
        self._provider = provider
        self._responses: dict[str, UsageBreakdown] = {}
        self._in_flight: set[str] = set()
        self._snapshot: UsageBreakdown | None = None
        self._pending = False
        self._invalid = False
        self.image_sent = False

    @property
    def pending(self) -> bool:
        """Provider work was observed whose final usage has not arrived yet."""
        return not self._invalid and (bool(self._in_flight) or self._pending)

    @property
    def invalid(self) -> bool:
        return self._invalid

    def request_sent(self, message: dict) -> None:
        # A client-requested response can start I/O before response.created reaches us.
        if self._provider == "openai" and message.get("type") in {"response.create", "input_audio_buffer.commit"}:
            self._pending = True

    def observe(self, message: dict) -> UsageBreakdown | None:
        """Record a provider frame; return a newly confirmed OpenAI per-response usage."""
        if self._invalid:
            return None
        if self._provider == "openai":
            return self._observe_openai(message)
        self._observe_gemini(message)
        return None

    def _observe_openai(self, message: dict) -> UsageBreakdown | None:
        event = message.get("type")
        if event == "error":
            # A provider rejection acknowledges the attempted request without starting a response.
            # Any response already created remains tracked in _in_flight until response.done.
            self._pending = False
            return None
        if event not in {"response.created", "response.done"}:
            return None
        response = message.get("response")
        response_id = response.get("id") if isinstance(response, dict) else None
        if not isinstance(response_id, str) or not response_id or len(response_id) > 128:
            self._invalid = True
            return None
        if event == "response.created":
            self._pending = False
            if response_id not in self._responses:
                self._in_flight.add(response_id)
            return None
        raw_usage = response.get("usage")
        usage = UsageBreakdown.from_openai_media(raw_usage)
        previous = self._responses.get(response_id)
        details = (raw_usage.get("input_tokens_details", raw_usage.get("input_token_details"))
                   if usage is not None else None)
        missing_image_split = (self.image_sent and previous is None and usage is not None and usage.input_tokens > 0
                               and (not isinstance(details, dict) or type(details.get("image_tokens")) is not int
                                    or not usage.reported("image", "input")))
        if (usage is None or usage.modality_usage_invalid or missing_image_split
                or (previous is not None and previous != usage)
                or (previous is None and len(self._responses) >= _MAX_METERED_RESPONSES)):
            self._invalid = True
            return None
        self._responses[response_id] = usage
        self._in_flight.discard(response_id)
        self._pending = False
        return None if previous is not None else usage

    def _observe_gemini(self, message: dict) -> None:
        content = message.get("serverContent")
        # Only content-bearing model output starts billable work; terminal markers may arrive
        # after the usage snapshot and must not reopen a finished turn. Trailing user input that
        # never produced a model turn is assumed unbilled.
        if isinstance(content, dict) and any(content.get(key) for key in ("modelTurn", "outputTranscription")):
            self._pending = True
        if "usageMetadata" not in message:
            return
        usage = UsageBreakdown.from_gemini(message["usageMetadata"])
        if (usage is None or usage.modality_usage_invalid
                or (self._snapshot is not None and not _not_less(self._snapshot, usage))):
            self._invalid = True
            return
        self._snapshot = usage
        self._pending = False

    def result(self) -> tuple[UsageBreakdown | None, str]:
        if self._provider == "openai":
            total: UsageBreakdown | None = None
            for usage in self._responses.values():
                total = usage if total is None else total + usage
        else:
            total = self._snapshot
        # Keep confirmed earlier usage as evidence even when a later frame is invalid.
        state = "invalid" if self._invalid else ("incomplete" if self.pending else "complete")
        return total, state


def _pcm(value: object) -> tuple[str, int]:
    if not isinstance(value, str) or not value or len(value) > (_MAX_PCM_BYTES * 4 // 3 + 8):
        raise RealtimeTransportError("invalid audio frame")
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise RealtimeTransportError("invalid audio frame") from exc
    if not raw or len(raw) > _MAX_PCM_BYTES or len(raw) % 2:
        raise RealtimeTransportError("invalid PCM16 audio frame")
    return value, len(raw)


def _object(value: str | bytes, limit: int = _MAX_FRAME_BYTES) -> dict[str, Any]:
    if not isinstance(value, str) or len(value.encode("utf-8")) > limit:
        raise RealtimeTransportError("realtime frame exceeds limit")
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError) as exc:
        raise RealtimeTransportError("invalid realtime JSON frame") from exc
    if not isinstance(parsed, dict):
        raise RealtimeTransportError("invalid realtime event")
    return parsed


def _image_size(raw: bytes, mime: str) -> tuple[int, int] | None:
    """Width and height read from the PNG IHDR or JPEG SOF header only; nothing is decoded."""
    if mime == "image/png":
        if len(raw) < 24 or raw[:8] != b"\x89PNG\r\n\x1a\n" or raw[12:16] != b"IHDR":
            return None
        return struct.unpack(">II", raw[16:24])
    if raw[:2] != b"\xff\xd8":
        return None
    index = 2
    while index + 4 <= len(raw):
        if raw[index] != 0xFF:
            return None
        marker = raw[index + 1]
        if marker == 0xFF:
            index += 1
            continue
        if marker in {0x01, *range(0xD0, 0xD8)}:
            index += 2
            continue
        length = struct.unpack(">H", raw[index + 2:index + 4])[0]
        if length < 2:
            return None
        if marker in {*range(0xC0, 0xC4), *range(0xC5, 0xC8), *range(0xC9, 0xCC), *range(0xCD, 0xD0)}:
            if index + 9 > len(raw):
                return None
            height, width = struct.unpack(">HH", raw[index + 5:index + 9])
            return width, height
        index += 2 + length
    return None


def client_frame_limit(image_input: bool) -> int:
    """Largest client JSON frame a session accepts; only image-enabled sessions widen it."""
    return _MAX_IMAGE_FRAME_BYTES if image_input else _MAX_FRAME_BYTES


def input_image_content(image_url: object, detail: object = None) -> dict:
    """Validate one data-URL image and return its ``input_image`` content part."""
    match = _IMAGE_DATA_URL.fullmatch(image_url) if isinstance(image_url, str) else None
    if match is None or len(image_url) > _MAX_IMAGE_FRAME_BYTES:
        raise RealtimeTransportError("unsupported realtime image")
    try:
        raw = base64.b64decode(match.group(2), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise RealtimeTransportError("unsupported realtime image") from exc
    size = _image_size(raw, match.group(1)) if 0 < len(raw) <= MAX_INPUT_IMAGE_BYTES else None
    if size is None or not all(0 < side <= MAX_INPUT_IMAGE_SIDE for side in size):
        raise RealtimeTransportError("realtime image exceeds the supported size")
    content = {"type": "input_image", "image_url": image_url}
    if detail is not None:
        if detail not in _IMAGE_DETAILS:
            raise RealtimeTransportError("unsupported realtime image")
        content["detail"] = detail
    return content


def _function_output_item(call_id: object, output: object) -> dict:
    if (not isinstance(call_id, str) or not 0 < len(call_id) <= _MAX_CALL_ID_CHARS
            or not isinstance(output, str) or len(output) > _MAX_FUNCTION_OUTPUT_CHARS):
        raise RealtimeTransportError("invalid realtime function call output")
    return {"type": "function_call_output", "call_id": call_id, "output": output}


def _conversation_item(item: object, features: dict) -> dict:
    """A client ``conversation.item.create`` item: one user image or one function call output."""
    if not isinstance(item, dict):
        raise RealtimeTransportError("unsupported realtime conversation item")
    if item.get("type") == "function_call_output" and features.get("function_calling"):
        if set(item) - {"type", "call_id", "output"}:
            raise RealtimeTransportError("unsupported realtime conversation item")
        return _function_output_item(item.get("call_id"), item.get("output"))
    content = item.get("content")
    if (item.get("type") == "message" and features.get("image_input") and set(item) <= {"type", "role", "content"}
            and item.get("role") == "user" and isinstance(content, list) and len(content) == 1
            and isinstance(content[0], dict) and set(content[0]) <= {"type", "image_url", "detail"}
            and content[0].get("type") == "input_image"):
        return {"type": "message", "role": "user",
                "content": [input_image_content(content[0].get("image_url"), content[0].get("detail"))]}
    raise RealtimeTransportError("unsupported realtime conversation item")


def _string_list(value: object, *, limit: int, valid: Callable[[str], bool]) -> list[str]:
    if not isinstance(value, list) or len(value) > limit or not all(isinstance(item, str) and valid(item)
                                                                     for item in value):
        raise RealtimeTransportError("unsupported realtime transcription options")
    return list(value)


def transcription_session(session: object) -> tuple[str, dict]:
    """Validate a client transcription ``session.update`` body; return its model and tuning options.

    Only ``audio.input.transcription`` may be set. Server VAD is refused (each turn is
    committed explicitly) and the PCM16 24 kHz input format is fixed.
    """
    invalid = RealtimeTransportError("unsupported realtime transcription session")
    if not isinstance(session, dict) or set(session) - {"type", "audio"} or session.get("type") != "transcription":
        raise invalid
    audio = session.get("audio")
    input_audio = audio.get("input") if isinstance(audio, dict) and set(audio) == {"input"} else None
    if not isinstance(input_audio, dict) or set(input_audio) - {"format", "transcription", "turn_detection"}:
        raise invalid
    if input_audio.get("format", _PCM24) != _PCM24:
        raise RealtimeTransportError("realtime PCM format is fixed at admission")
    if input_audio.get("turn_detection") is not None:
        raise RealtimeTransportError("unsupported realtime turn detection")
    transcription = input_audio.get("transcription")
    if not isinstance(transcription, dict) or set(transcription) - {"model", *_TRANSCRIPTION_OPTIONS_ALL}:
        raise invalid
    model = transcription.get("model")
    if not isinstance(model, str) or not model or len(model) > 190:
        raise invalid
    # The provider rejects options a model does not take, after the session already exists upstream.
    if set(transcription) - {"model"} - _TRANSCRIPTION_OPTIONS.get(model, _TRANSCRIPTION_OPTIONS_ALL):
        raise RealtimeTransportError("unsupported realtime transcription options")
    if transcription.get("language") is not None and transcription.get("languages") is not None:
        raise RealtimeTransportError("unsupported realtime transcription options")
    options: dict = {}
    prompt = transcription.get("prompt")
    if prompt is not None:
        if not isinstance(prompt, str) or len(prompt) > _MAX_TRANSCRIPTION_PROMPT_CHARS:
            raise RealtimeTransportError("unsupported realtime transcription options")
        options["prompt"] = prompt
    if transcription.get("keywords") is not None:
        options["keywords"] = _string_list(transcription["keywords"], limit=_MAX_KEYWORDS, valid=lambda item: (
            0 < len(item) <= _MAX_KEYWORD_CHARS and not set(item) & set("<>\r\n")))
    if transcription.get("languages") is not None:
        options["languages"] = _string_list(transcription["languages"], limit=_MAX_LANGUAGES,
                                            valid=lambda item: bool(_TRANSCRIPTION_LANGUAGE.fullmatch(item)))
    language = transcription.get("language")
    if language is not None:
        if not isinstance(language, str) or not _TRANSCRIPTION_LANGUAGE.fullmatch(language):
            raise RealtimeTransportError("unsupported realtime transcription options")
        options["language"] = language
    delay = transcription.get("delay")
    if delay is not None:
        if not isinstance(delay, str) or delay not in _TRANSCRIPTION_DELAYS:
            raise RealtimeTransportError("unsupported realtime transcription options")
        options["delay"] = delay
    return model, options


def transcription_update(model: str, options: dict | None) -> dict:
    """The canonical upstream transcription session; the model is the admitted one."""
    return {"type": "session.update", "session": {"type": "transcription", "audio": {"input": {
        "format": dict(_PCM24), "transcription": {"model": model, **(options or {})}, "turn_detection": None}}}}


def live_session_start(message: object) -> tuple[str, str | None, str | None]:
    """Validate a client GPT-Live ``session.start``; return ``(model, instructions, voice)``.

    Backend delegation (separately billed), provider-side storage and seeded input history
    cannot be metered or bounded, so they are refused; the PCM16 24 kHz format is fixed.
    """
    invalid = RealtimeTransportError("unsupported realtime live session")
    if not isinstance(message, dict) or message.get("type") != "session.start" or set(message) - {
            "type", "event_id", "session"}:
        raise invalid
    event_id = message.get("event_id")
    session = message.get("session")
    if (event_id is not None and (not isinstance(event_id, str) or len(event_id) > 128)) or not isinstance(session, dict):
        raise invalid
    if set(session) - {"model", "instructions", "audio", "delegation", "store"}:
        raise invalid
    if session.get("delegation") is not None:
        raise RealtimeTransportError("realtime live delegation is unsupported")
    if session.get("store") is not None and session["store"] is not False:
        raise RealtimeTransportError("realtime live storage is unsupported")
    model, instructions = session.get("model"), session.get("instructions")
    if not isinstance(model, str) or not model or len(model) > 190:
        raise invalid
    if instructions is not None and (not isinstance(instructions, str) or len(instructions) > 4096):
        raise RealtimeTransportError("invalid realtime instructions")
    audio = session.get("audio", {})
    if not isinstance(audio, dict) or set(audio) - {"format", "output"}:
        raise invalid
    if audio.get("format", _PCM24) != _PCM24:
        raise RealtimeTransportError("realtime PCM format is fixed at admission")
    output = audio.get("output", {})
    if not isinstance(output, dict) or set(output) - {"voice"}:
        raise invalid
    voice = output.get("voice")
    if voice is not None and (not isinstance(voice, str) or not voice or len(voice) > 100):
        raise invalid
    return model, instructions, voice


def _live_start(model: str, instructions: str | None, voice: str | None) -> dict:
    session: dict = {"model": model}
    if instructions:
        session["instructions"] = instructions
    session["audio"] = {"format": dict(_PCM24), "output": {"voice": voice}}
    return {"type": "session.start", "event_id": f"lumen_{uuid.uuid4().hex}", "session": session}


def _translation_update(session: object) -> dict:
    output = None
    if isinstance(session, dict) and set(session) == {"audio"}:
        audio = session["audio"]
        output = audio.get("output") if isinstance(audio, dict) and set(audio) == {"output"} else None
    language = output.get("language") if isinstance(output, dict) and set(output) == {"language"} else None
    if not isinstance(language, str) or not _TRANSLATION_LANGUAGE.fullmatch(language):
        raise RealtimeTransportError("unsupported realtime translation update")
    return {"type": "session.update", "session": {"audio": {"output": {"language": language}}}}


def translation_language(value: object) -> str:
    """A target language a translation ``session.update`` accepts; raises otherwise."""
    return _translation_update({"audio": {"output": {"language": value}}})["session"]["audio"]["output"]["language"]


def _kind_client_event(message: dict, kind: str, model: str | None) -> tuple[dict | None, int, bool]:
    """Whitelisted OpenAI transcription/translation/live client events, rebuilt from validated fields."""
    event = message.get("type")
    if event == "session.close":
        return None, 0, True
    if kind == "transcription":
        if event == "input_audio_buffer.append":
            audio, size = _pcm(message.get("audio"))
            return {"type": event, "audio": audio}, size, False
        if event in {"input_audio_buffer.commit", "input_audio_buffer.clear"}:
            return {"type": event}, 0, False
        if event == "session.update":
            requested, options = transcription_session(message.get("session"))
            if requested != model:
                raise RealtimeTransportError("realtime model is fixed at admission")
            return transcription_update(requested, options), 0, False
    elif kind == "translation":
        if event == "session.input_audio_buffer.append":
            audio, size = _pcm(message.get("audio"))
            return {"type": event, "audio": audio}, size, False
        if event == "session.update":
            return _translation_update(message.get("session")), 0, False
    elif kind == "live":
        if event == "session.input_audio.append":
            audio, size = _pcm(message.get("audio"))
            return {"type": event, "audio": audio}, size, False
        if event in {"session.input_audio.mute", "session.input_audio.unmute"}:
            return {"type": event}, 0, False
        if event == "session.instructions.append":
            instructions = message.get("instructions")
            if not isinstance(instructions, str) or not instructions or len(instructions) > 4096:
                raise RealtimeTransportError("invalid realtime instructions")
            return {"type": event, "instructions": instructions}, 0, False
    raise RealtimeTransportError("unsupported realtime client event")


def _client_event(message: dict, provider: str, *, wire: str, voice: str | None = None,
                  allow_transcription: bool = True, kind: str = "conversation",
                  model: str | None = None, features: dict | None = None) -> tuple[dict | None, int, bool]:
    features = features or {}
    if kind != "conversation":
        if provider != "openai":
            raise RealtimeTransportError("unsupported realtime client event")
        if wire == "native" and kind == "transcription":
            event = message.get("type")
            if event in {"audio.input.append", "audio.input.commit"}:
                message = {**message, "type": event.replace("audio.input.", "input_audio_buffer.")}
            elif event != "session.close":
                raise RealtimeTransportError("unsupported realtime client event")
        elif wire == "native" and kind in _NATIVE_STREAM_CLIENT_EVENTS:
            event = message.get("type")
            if event in _NATIVE_STREAM_CLIENT_EVENTS[kind]:
                message = {**message, "type": _NATIVE_STREAM_CLIENT_EVENTS[kind][event]}
            elif event != "session.close":
                raise RealtimeTransportError("unsupported realtime client event")
        elif wire != "openai":
            raise RealtimeTransportError("unsupported realtime client event")
        return _kind_client_event(message, kind, model)
    event = message.get("type")
    if wire == "openai":
        if event == "input_audio_buffer.append":
            audio, size = _pcm(message.get("audio"))
            return {"type": event, "audio": audio}, size, False
        if event in {"input_audio_buffer.commit", "input_audio_buffer.clear", "response.cancel", "response.create"}:
            if event == "response.create" and message.get("response") not in (None, {}):
                raise RealtimeTransportError("unsupported realtime response configuration")
            return {"type": event}, 0, False
        if event == "conversation.item.create":
            if set(message) - {"type", "event_id", "item"}:
                raise RealtimeTransportError("unsupported realtime conversation item")
            return {"type": event, "item": _conversation_item(message.get("item"), features)}, 0, False
        if event == "session.update":
            session = message.get("session")
            if not isinstance(session, dict) or set(session) - {
                    "type", "instructions", "audio", "output_modalities", "reasoning", "tools", "tool_choice"}:
                raise RealtimeTransportError("unsupported realtime session update")
            if session.get("type", "realtime") != "realtime" or session.get("output_modalities", ["audio"]) != ["audio"]:
                raise RealtimeTransportError("realtime model and modalities cannot change")
            instructions = session.get("instructions")
            if instructions is not None and (not isinstance(instructions, str) or len(instructions) > 4096):
                raise RealtimeTransportError("invalid realtime instructions")
            audio = session.get("audio", {})
            if not isinstance(audio, dict) or set(audio) - {"input", "output"}:
                raise RealtimeTransportError("unsupported realtime audio update")
            input_audio, output_audio = audio.get("input", {}), audio.get("output", {})
            if (not isinstance(input_audio, dict) or not isinstance(output_audio, dict)
                    or set(input_audio) - {"format", "turn_detection", "transcription"}
                    or set(output_audio) - {"format", "voice"}):
                raise RealtimeTransportError("unsupported realtime audio update")
            fmt = {"type": "audio/pcm", "rate": 24000}
            if (input_audio.get("format", fmt) != fmt or output_audio.get("format", fmt) != fmt
                    or output_audio.get("voice", voice) != voice):
                raise RealtimeTransportError("realtime voice and PCM format are fixed at admission")
            if "turn_detection" in input_audio and input_audio["turn_detection"] != {"type": "server_vad"}:
                raise RealtimeTransportError("unsupported realtime turn detection")
            if "transcription" in input_audio and (not allow_transcription
                                                    or input_audio["transcription"] != {"model": "gpt-4o-mini-transcribe"}):
                raise RealtimeTransportError("unsupported realtime transcription")
            if "reasoning" in session and (not reasoning_configurable(model) or not isinstance(
                    session["reasoning"], dict) or set(session["reasoning"]) != {"effort"}
                    or session["reasoning"]["effort"] not in REASONING_EFFORTS):
                raise RealtimeTransportError("unsupported realtime reasoning effort")
            safe = {key: session[key] for key in ("instructions", "audio", "reasoning") if key in session}
            if "tools" in session or "tool_choice" in session:
                if not features.get("function_calling"):
                    raise RealtimeTransportError("unsupported realtime function tools")
                try:
                    tools, choice = function_tools(session.get("tools"), session.get("tool_choice"))
                except ProviderValidationError as exc:
                    raise RealtimeTransportError("unsupported realtime function tools") from exc
                safe["tools"] = tools
                if choice is not None:
                    safe["tool_choice"] = choice
            return {"type": "session.update", "session": safe}, 0, False
        if event == "session.close":
            return None, 0, True
    elif wire == "gemini":
        if "realtimeInput" in message:
            input_data = message["realtimeInput"]
            audio = input_data.get("audio") if isinstance(input_data, dict) else None
            value, size = _pcm(audio.get("data") if isinstance(audio, dict) else None)
            return {"realtimeInput": {"audio": {"data": value, "mimeType": "audio/pcm;rate=16000"}}}, size, False
        if message.get("type") == "session.close":
            return None, 0, True
    else:
        if event == "audio.input.append":
            audio, size = _pcm(message.get("audio"))
            if provider == "openai":
                return {"type": "input_audio_buffer.append", "audio": audio}, size, False
            return {"realtimeInput": {"audio": {"data": audio, "mimeType": "audio/pcm;rate=16000"}}}, size, False
        if event == "audio.input.commit":
            return ({"type": "input_audio_buffer.commit"} if provider == "openai"
                    else {"realtimeInput": {"audioStreamEnd": True}}), 0, False
        if event == "conversation.input_image" and features.get("image_input"):
            if set(message) - {"type", "image_url", "detail"}:
                raise RealtimeTransportError("unsupported realtime image")
            return {"type": "conversation.item.create", "item": {"type": "message", "role": "user", "content": [
                input_image_content(message.get("image_url"), message.get("detail"))]}}, 0, False
        if event == "function_call.output" and features.get("function_calling"):
            return {"type": "conversation.item.create", "item": _function_output_item(
                message.get("call_id"), message.get("output"))}, 0, False
        if event == "response.cancel":
            if provider == "openai":
                return {"type": "response.cancel"}, 0, False
            # Gemini Live has automatic barge-in but no response.cancel command.
            # Close the provider session rather than triggering another turn.
            return None, 0, True
        if event == "session.close":
            return None, 0, True
    raise RealtimeTransportError("unsupported realtime client event")


def _kind_provider_events(message: dict) -> tuple[list[dict], int]:
    """OpenAI transcription/translation/live frames pass through; raw provider error detail never does."""
    event = message.get("type")
    if event == "error" or not isinstance(event, str) or len(event) > 128:
        return [{"type": "error", "error": dict(_SAFE_PROVIDER_ERROR)}], 0
    if "error" in message:
        # e.g. conversation.item.input_audio_transcription.failed keeps its item correlation only.
        safe = {key: message[key] for key in ("type", "item_id", "content_index") if key in message}
        return [{**safe, "error": dict(_SAFE_PROVIDER_ERROR)}], 0
    size = _pcm(message.get("delta"))[1] if event == "session.output_audio.delta" else 0
    return [message], size


def _native_transcription_events(message: dict) -> tuple[list[dict], int]:
    """Project only bounded item-correlated transcript frames; settlement owns session.closed."""
    event = message.get("type")
    item_id = message.get("item_id")
    prefix = "conversation.item.input_audio_transcription."
    if event == "error":
        return [{"type": "error", "code": "upstream_error",
                 "message": "Realtime provider returned an error"}], 0
    if not isinstance(item_id, str) or not item_id or len(item_id) > 256:
        return [], 0
    if event == prefix + "failed":
        return [{"type": "transcript.input.failed", "item_id": item_id}], 0
    if event in {prefix + "delta", prefix + "completed"}:
        source, target = ("delta", "delta") if event.endswith(".delta") else ("transcript", "text")
        text = message.get(source)
        # The input frame already passed the 64-KiB JSON bound; long final transcripts
        # must not disappear while their committed turn is marked drained.
        if isinstance(text, str) and len(text) <= _MAX_FRAME_BYTES:
            return [{"type": "transcript.input." + event.rsplit(".", 1)[1],
                     "item_id": item_id, target: text}], 0
        if event.endswith(".completed"):
            return [{"type": "transcript.input.failed", "item_id": item_id}], 0
    return [], 0


def _native_stream_events(message: dict) -> tuple[list[dict], int]:
    """Project translation/GPT-Live audio and transcripts; settlement owns session.closed."""
    event = message.get("type")
    if event == "error" or "error" in message:
        return [{"type": "error", "code": "upstream_error", "message": "Realtime provider returned an error"}], 0
    if event == "session.output_audio.delta":
        delta, size = _pcm(message.get("delta"))
        return [{"type": "audio.output.delta", "delta": delta, "sample_rate_hz": 24000}], size
    target = _NATIVE_STREAM_TRANSCRIPTS.get(event)
    text = message.get("delta")
    if target is not None and isinstance(text, str) and len(text) <= _MAX_FRAME_BYTES:
        return [{"type": target, "delta": text}], 0
    return [], 0


class NativeFunctionCalls:
    """Correlate client outputs and resume each completed response at most once."""

    def __init__(self) -> None:
        self.owners: dict[str, str] = {}
        self.remaining: dict[str, set[str]] = {}
        self.completed: set[str] = set()

    def observe(self, message: dict) -> bool:
        event = message.get("type")
        if event == "response.function_call_arguments.done":
            call_id, response_id = message.get("call_id"), message.get("response_id")
            if (not all(isinstance(value, str) and 0 < len(value) <= _MAX_CALL_ID_CHARS
                        for value in (call_id, response_id)) or call_id in self.owners):
                return False
            if len(self.owners) >= _MAX_METERED_RESPONSES:
                raise RealtimeTransportError("too many realtime function calls")
            self.owners[call_id] = response_id
            self.remaining.setdefault(response_id, set()).add(call_id)
        elif event == "response.done":
            response = message.get("response")
            response_id = response.get("id") if isinstance(response, dict) else None
            if isinstance(response_id, str) and response_id in self.remaining:
                if response.get("status") == "completed":
                    self.completed.add(response_id)
                else:
                    del self.remaining[response_id]
                    self.completed.discard(response_id)
        return True

    def awaiting(self, call_id: str) -> bool:
        return call_id in self.remaining.get(self.owners.get(call_id), ())

    def answered(self, call_id: str) -> None:
        self.remaining[self.owners[call_id]].remove(call_id)

    def take_ready(self) -> bool:
        for response_id in self.completed:
            if not self.remaining[response_id]:
                self.completed.remove(response_id)
                del self.remaining[response_id]
                return True
        return False


async def _confirmed_session_update(upstream, config: dict, limits: dict, frame_limit: int,
                                    compat_events: list[dict] | None) -> None:
    """Require a matching acknowledgement before the next setup phase or any client traffic."""
    async with asyncio.timeout(_REALTIME_SETUP_SECONDS):
        await upstream.send(json.dumps({"type": "session.update", "session": config}))
        # Each documented creation event may precede the ACK once. Any repeat or
        # unrelated event reaches the strict ACK check below and fails setup.
        creation_events = {"session.created", "conversation.created"}
        while True:
            message = _object(await upstream.recv(), frame_limit)
            event = message.get("type")
            if not isinstance(event, str) or event not in creation_events:
                break
            creation_events.remove(event)
            if compat_events is not None:
                compat_events.append(message)
        session = message.get("session")
        if (message.get("type") != "session.updated" or not isinstance(session, dict)
                or any(session.get(key) != value for key, value in limits.items())):
            raise RealtimeTransportError("realtime provider did not confirm session configuration")
        if compat_events is not None:
            compat_events.append(message)


def _client_realtime_error(wire: str, code: str, message: str) -> dict:
    if wire == "openai":
        return {"type": "error", "error": {"type": "invalid_request_error", "code": code, "message": message}}
    return {"type": "error", "code": code, "message": message}


async def _await_live_started(upstream) -> None:
    """Hold the native session.ready until GPT-Live confirms its session."""
    async with asyncio.timeout(_LIVE_START_SECONDS):
        while True:
            message = _object(await upstream.recv())
            if message.get("type") == "session.started":
                return
            if message.get("type") == "error" or "error" in message:
                raise RealtimeTransportError("realtime live session did not start")


def _provider_events(message: dict, provider: str, *, wire: str,
                     kind: str = "conversation") -> tuple[list[dict], int]:
    if kind != "conversation":
        if wire == "native" and kind == "transcription":
            return _native_transcription_events(message)
        if wire == "native":
            return _native_stream_events(message)
        return _kind_provider_events(message)
    events: list[dict] = []
    pcm_bytes = 0
    if provider == "openai":
        event = message.get("type")
        if event in {"response.output_audio.delta", "response.audio.delta"}:
            delta, pcm_bytes = _pcm(message.get("delta"))
            events.append({"type": "audio.output.delta", "delta": delta, "sample_rate_hz": 24000})
        elif event == "conversation.item.input_audio_transcription.completed":
            text = message.get("transcript")
            if isinstance(text, str) and len(text) <= 4096:
                events.append({"type": "transcript.input.delta", "delta": text})
        elif event in {"response.output_audio_transcript.delta", "response.audio_transcript.delta"}:
            if isinstance(message.get("delta"), str) and len(message["delta"]) <= 4096:
                events.append({"type": "transcript.output.delta", "delta": message["delta"]})
        elif event in {"input_audio_buffer.speech_started", "response.cancelled"}:
            events.append({"type": "session.interrupted"})
        elif event == "response.function_call_arguments.delta":
            if isinstance(message.get("call_id"), str) and isinstance(message.get("delta"), str):
                events.append({"type": "function_call.arguments.delta", "call_id": message["call_id"],
                               "delta": message["delta"]})
        elif event == "response.function_call_arguments.done":
            if all(isinstance(message.get(key), str) for key in ("call_id", "name", "arguments")):
                events.append({"type": "function_call.arguments.done", "call_id": message["call_id"],
                               "name": message["name"], "arguments": message["arguments"]})
        elif event == "error":
            events.append({"type": "error", "code": "upstream_error", "message": "Realtime provider returned an error"})
    else:
        content = message.get("serverContent")
        if isinstance(content, dict):
            turn = content.get("modelTurn")
            for part in (turn.get("parts") or []) if isinstance(turn, dict) else []:
                inline = part.get("inlineData") if isinstance(part, dict) else None
                if isinstance(inline, dict) and isinstance(inline.get("mimeType"), str) and inline["mimeType"].startswith("audio/pcm"):
                    delta, size = _pcm(inline.get("data"))
                    pcm_bytes += size
                    events.append({"type": "audio.output.delta", "delta": delta, "sample_rate_hz": 24000})
            for key, output_type in (("inputTranscription", "transcript.input.delta"), ("outputTranscription", "transcript.output.delta")):
                item = content.get(key)
                text = item.get("text") if isinstance(item, dict) else None
                if isinstance(text, str) and len(text) <= 4096:
                    events.append({"type": output_type, "delta": text})
            if content.get("interrupted") is True:
                events.append({"type": "session.interrupted"})
        elif "error" in message:
            events.append({"type": "error", "code": "upstream_error", "message": "Realtime provider returned an error"})
    if wire == "openai":
        # Preserve recognized OpenAI protocol events, never raw provider errors or usage.
        if provider == "openai" and message.get("type") != "error":
            return [message], pcm_bytes
        return [{"type": "error", "error": {"type": "server_error", "message": "Realtime provider returned an error"}}], 0
    if wire == "gemini":
        return ([message] if provider == "gemini" and "error" not in message else
                [{"error": {"message": "Realtime provider returned an error"}}]), pcm_bytes
    return events, pcm_bytes


async def _drain_final_usage(upstream, server: asyncio.Task, meter: TokenUsageMeter, *, provider: str,
                             wire: str, outgoing: int, output_limit: int,
                             frame_limit: int = _MAX_FRAME_BYTES) -> tuple[asyncio.Task, int]:
    """After the client stops, wait a bounded time for in-flight provider usage; never retry."""
    if server.done() and isinstance(server.exception(), ConnectionClosed):
        return server, outgoing
    if provider == "openai" and meter.pending:
        try:
            await upstream.send(json.dumps({"type": "response.cancel"}))
        except ConnectionClosed:
            return server, outgoing
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _USAGE_DRAIN_SECONDS
    while meter.pending:
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        done, _ = await asyncio.wait({server}, timeout=remaining)
        if not done:
            break
        try:
            response = server.result()
        except ConnectionClosed:
            break
        parsed = _object(response, frame_limit)
        meter.observe(parsed)
        # The client has already stopped: count provider audio against the bound, forward nothing.
        _, size = _provider_events(parsed, provider, wire=wire)
        if outgoing + size > output_limit:
            raise RealtimeTransportError("session output exceeds duration bound")
        outgoing += size
        server = asyncio.create_task(upstream.recv())
    return server, outgoing


async def _drain_live_close(upstream, server: asyncio.Task, live: LiveUsageMeter, websocket, *, wire: str,
                            send_close: bool, until: float, outgoing: int,
                            output_limit: int) -> tuple[asyncio.Task, int]:
    """Request GPT-Live's ``session.closed`` and read to it so provider seconds, not Lumen's clock, settle.

    Frames still go to the client best-effort; a client that already left changes nothing here.
    """
    loop = asyncio.get_running_loop()
    if send_close:
        try:
            await upstream.send(json.dumps({"type": "session.close"}))
        except ConnectionClosed:
            return server, outgoing
    while live.final is None and not live.invalid:
        remaining = until - loop.time()
        if remaining <= 0:
            break
        done, _ = await asyncio.wait({server}, timeout=remaining)
        if not done:
            break
        try:
            parsed = _object(server.result())
        except ConnectionClosed:
            break
        live.observe(parsed)
        events, size = _provider_events(parsed, "openai", wire=wire, kind="live")
        if outgoing + size > output_limit:
            raise RealtimeTransportError("session output exceeds duration bound")
        outgoing += size
        server = asyncio.create_task(upstream.recv())
        try:
            for event in events:
                await websocket.send_json(event)
        except (WebSocketDisconnect, RuntimeError):
            pass
    return server, outgoing


async def relay_audio(
    websocket: WebSocket,
    route: dict,
    *,
    session_id: str,
    voice: str | None,
    instructions: str | None,
    max_duration_seconds: int,
    wire: str = "native",
    is_canceled: Callable[[], Awaitable[bool]],
    token_usage: bool = False,
    session_time: bool = False,
    transcription: dict | None = None,
    output_language: str | None = None,
    reasoning_effort: str | None = None,
    tools: list | None = None,
    tool_choice: object = None,
    token_hold: tuple[Callable[[UsageBreakdown], Decimal], Decimal, Decimal] | None = None,
) -> AudioMeter:
    """Relay strictly bounded frames and return PCM byte counts; never log or persist audio.

    ``token_usage`` sessions additionally meter provider-reported token usage. OpenAI's
    input transcription is a separately billed model whose rates are not frozen, so it is
    neither configured nor accepted for token-basis sessions. OpenAI token sessions pin
    ``max_output_tokens`` and retention-ratio truncation so one response has a known worst
    case; ``token_hold`` is ``(frozen per-response price, reserved USD, that worst case)``
    and the session stops once confirmed usage plus two further worst-case responses
    would exceed the hold, or one response exceeds the worst case.
    Image input and function calling are relayed only where ``conversation_features`` allows.

    Native and compatible non-conversation kinds use the same admitted upstream setup.
    ``transcription`` holds pre-validated tuning options and a GPT-Live ``session.start``
    carries the admitted model, instructions and voice. A compatible translation waits
    for the client's language update; a native one sends the admitted ``output_language``.
    """
    provider = route["provider_type"]
    input_rate = 24000 if provider == "openai" else 16000
    if session_time and max_duration_seconds <= CLOSE_TIMEOUT_SECONDS:
        raise RealtimeTransportError("session duration must exceed the upstream close timeout")
    relay_limit = max_duration_seconds - CLOSE_TIMEOUT_SECONDS if session_time else max_duration_seconds
    output_limit = 24000 * 2 * max_duration_seconds
    from .credentials import resolve_route_api_key

    api_key = await resolve_route_api_key(route)
    url, headers = upstream_connection_spec({**route, "api_key": api_key})
    kind = realtime_kind({**route, "api_key": api_key})
    if kind != "conversation" and wire not in {"openai", "native"}:
        raise RealtimeTransportError("realtime model requires its compatible wire")
    if wire == "native" and kind == "translation" and output_language is None:
        raise RealtimeTransportError("realtime translation language is fixed at admission")
    model = route["api_model_name"]
    incoming = outgoing = 0
    pending_transcriptions = 0
    finished_transcriptions: set[str] = set()
    tokens = TokenUsageMeter(provider) if token_usage else None
    live = LiveUsageMeter() if kind == "live" else None
    features = (conversation_features({**route, "api_key": api_key})
                if tokens is not None and kind == "conversation" and provider == "openai" else {})
    if (tools is not None or tool_choice is not None) and not features.get("function_calling"):
        raise RealtimeTransportError("realtime function calling is unavailable for this session")
    frame_limit = client_frame_limit(features.get("image_input") is True)
    calls = NativeFunctionCalls() if wire == "native" and tools is not None else None
    images = 0
    spent = Decimal(0)
    hold_reached = False
    loop = asyncio.get_running_loop()
    connected_ns: int | None = None
    hard_close: asyncio.TimerHandle | None = None

    def has_response_funding() -> bool:
        return token_hold is None or spent + 2 * token_hold[2] <= token_hold[1]

    def measurement(*, failed: bool = False) -> AudioMeter:
        elapsed = (Decimal(time.monotonic_ns() - connected_ns) / Decimal(1_000_000_000)
                   if connected_ns is not None else None)
        usage, state = tokens.result() if tokens is not None else (None, "unmetered")
        if failed and tokens is not None and state != "invalid":
            state = "incomplete"
        provider_seconds, provider_state = live.result() if live is not None else (None, "unmetered")
        return AudioMeter(incoming, outgoing, input_rate, 0 if kind == "transcription" else 24000,
                          usage=usage, usage_state=state, image_sent=images > 0,
                          connected_seconds=elapsed, provider_seconds=provider_seconds,
                          provider_seconds_state=provider_state)

    try:
        async with asyncio.timeout(None) as deadline, websockets.connect(
            url, additional_headers=headers, max_size=frame_limit,
            open_timeout=10, close_timeout=CLOSE_TIMEOUT_SECONDS,
        ) as upstream:
            from .relay_history import record_dispatch
            record_dispatch(route.get("provider_auth"), endpoint=url, model=route.get("api_model_name"), operation="realtime", status_code=101)
            # Provider-connected time: open handshake complete (context entry) to context exit.
            connected_ns = time.monotonic_ns()
            connected_at = loop.time()
            if session_time:
                # Leave the bounded close handshake inside the funded envelope. The backstop
                # aborts transport just before max; actual connected nanoseconds are never clamped.
                hard_at = connected_at + max_duration_seconds - 0.001
                hard_close = loop.call_at(hard_at, upstream.transport.abort)
                deadline.reschedule(hard_at)
            if kind == "transcription":
                await upstream.send(json.dumps(transcription_update(model, transcription)))
            elif kind == "live":
                await upstream.send(json.dumps(_live_start(model, instructions, voice)))
                if wire == "native":
                    await _await_live_started(upstream)
            elif kind == "translation":
                # A compatible client selects the output language with its own session.update.
                if wire == "native":
                    await upstream.send(json.dumps(_translation_update(
                        {"audio": {"output": {"language": output_language}}})))
            elif provider == "openai":
                input_config: dict = {"format": {"type": "audio/pcm", "rate": 24000}}
                if tokens is None:
                    input_config["transcription"] = {"model": "gpt-4o-mini-transcribe"}
                input_config["turn_detection"] = {"type": "server_vad"}
                session = {"type": "realtime", "audio": {"input": input_config,
                           "output": {"format": {"type": "audio/pcm", "rate": 24000}, "voice": voice}}}
                if instructions:
                    session["instructions"] = instructions
                if reasoning_effort is not None:
                    session["reasoning"] = {"effort": reasoning_effort}
                if tokens is not None:
                    # Hold all client input behind both acknowledgements; VAD cannot start
                    # a response during the first, limits-only setup phase.
                    limits = {"max_output_tokens": RESPONSE_MAX_OUTPUT_TOKENS,
                              "truncation": {"type": "retention_ratio", "retention_ratio": 0.8,
                                             "token_limits": {"post_instructions": RESPONSE_POST_INSTRUCTION_TOKENS}}}
                    compat_events = [] if wire == "openai" else None
                    await _confirmed_session_update(upstream, {"type": "realtime", **limits,
                        "audio": {"input": {"turn_detection": None}}}, limits, frame_limit, compat_events)
                    if tools is not None:
                        session["tools"] = tools
                    if tool_choice is not None:
                        session["tool_choice"] = tool_choice
                    await _confirmed_session_update(upstream, session, limits, frame_limit, compat_events)
                    for event in compat_events or ():
                        await websocket.send_json(event)
                else:
                    await upstream.send(json.dumps({"type": "session.update", "session": session}))
            else:
                config = {"responseModalities": ["AUDIO"], "speechConfig": {
                    "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}}}
                setup: dict = {"model": "models/" + route["api_model_name"], "generationConfig": config,
                               "inputAudioTranscription": {}, "outputAudioTranscription": {}}
                if instructions:
                    setup["systemInstruction"] = {"parts": [{"text": instructions}]}
                await upstream.send(json.dumps({"setup": setup}))
            if wire == "native":
                await websocket.send_json({"type": "session.ready", "session_id": session_id,
                                           "provider_type": provider, "realtime_kind": kind,
                                           "input_sample_rate_hz": input_rate,
                                           "output_sample_rate_hz": 0 if kind == "transcription" else 24000})
            # Session basis counts from upstream open; legacy PCM mode retains its post-setup deadline.
            started = connected_at if session_time else loop.time()
            client = asyncio.create_task(websocket.receive_text())
            server = asyncio.create_task(upstream.recv())
            last_activity = started
            # Stop client input on close, then drain committed transcription items or
            # translation/live output until completion or this bounded drain deadline.
            closing_until: float | None = None
            try:
                while True:
                    now = loop.time()
                    elapsed = now - started
                    if (elapsed >= relay_limit or now - last_activity >= _IDLE_SECONDS or await is_canceled()
                            or (closing_until is not None and now >= closing_until)):
                        break
                    done, _ = await asyncio.wait({server} if closing_until is not None else {client, server},
                                                 timeout=min(1, relay_limit - elapsed),
                                                 return_when=asyncio.FIRST_COMPLETED)
                    if not done:
                        continue
                    if client in done:
                        try:
                            request = _object(client.result(), frame_limit)
                        except WebSocketDisconnect:
                            break
                        message, size, close = _client_event(request, provider, wire=wire, voice=voice,
                                                             allow_transcription=tokens is None, kind=kind,
                                                             model=model, features=features)
                        if close and kind == "transcription" and pending_transcriptions:
                            last_activity = loop.time()
                            closing_until = last_activity + _USAGE_DRAIN_SECONDS
                            continue
                        if close and kind not in _DRAINED_CLOSE_KINDS:
                            break
                        if close:
                            await upstream.send(json.dumps({"type": "session.close"}))
                            last_activity = loop.time()
                            closing_until = last_activity + _USAGE_DRAIN_SECONDS
                        else:
                            if incoming + size > 2 * input_rate * max_duration_seconds:
                                raise RealtimeTransportError("session input exceeds duration bound")
                            item_type = (message.get("item") or {}).get("type")
                            call_id = message["item"]["call_id"] if (
                                wire == "native" and item_type == "function_call_output") else None
                            if call_id is not None and (calls is None or not calls.awaiting(call_id)):
                                await websocket.send_json(_client_realtime_error(wire, "invalid_function_call_id",
                                    "Function call ID is unknown or already answered"))
                            elif (message.get("type") in {"response.create", "input_audio_buffer.commit"}
                                  and not has_response_funding()):
                                await websocket.send_json(_client_realtime_error(wire, "realtime_hold_limit",
                                    "The realtime hold cannot cover another response"))
                            else:
                                if tokens is not None:
                                    tokens.request_sent(message)
                                if item_type == "message" and images >= MAX_SESSION_IMAGES:
                                    raise RealtimeTransportError("too many realtime images")
                                await upstream.send(json.dumps(message, separators=(",", ":")))
                                if item_type == "message":
                                    images += 1
                                    tokens.image_sent = True
                                if call_id is not None:
                                    calls.answered(call_id)
                                incoming += size
                                if kind == "transcription" and message["type"] == "input_audio_buffer.commit":
                                    pending_transcriptions += 1
                                    if pending_transcriptions + len(finished_transcriptions) > _MAX_METERED_RESPONSES:
                                        raise RealtimeTransportError("too many realtime transcription turns")
                            last_activity = loop.time()
                            client = asyncio.create_task(websocket.receive_text())
                    if server in done:
                        try:
                            response = server.result()
                        except ConnectionClosed as exc:
                            if exc.code != 1000:
                                raise RealtimeTransportError("upstream realtime connection closed unexpectedly") from exc
                            break
                        parsed = _object(response, frame_limit)
                        if live is not None:
                            live.observe(parsed)
                        if tokens is not None:
                            added = tokens.observe(parsed)
                            if added is not None and token_hold is not None:
                                price, hold, bound = token_hold
                                try:
                                    cost = price(added)
                                except ValueError:
                                    cost = hold  # Unpriceable usage: stop now; settlement decides.
                                spent += cost
                                # A response above the pinned worst case voids the guard's premise.
                                hold_reached = cost > bound or not has_response_funding()
                            if tokens.invalid:
                                hold_reached = True
                        if kind == "transcription" and parsed.get("type") in {
                            "conversation.item.input_audio_transcription.completed",
                            "conversation.item.input_audio_transcription.failed",
                        }:
                            item_id = parsed.get("item_id")
                            if (pending_transcriptions and isinstance(item_id, str) and 0 < len(item_id) <= 256
                                    and item_id not in finished_transcriptions):
                                finished_transcriptions.add(item_id)
                                pending_transcriptions -= 1
                        if calls is None or calls.observe(parsed):
                            events, size = _provider_events(parsed, provider, wire=wire, kind=kind)
                        else:
                            events, size = [], 0
                        last_activity = loop.time()
                        if outgoing + size > output_limit:
                            raise RealtimeTransportError("session output exceeds duration bound")
                        outgoing += size
                        # Arm the next receive before forwarding so a drain never re-reads this frame.
                        server = asyncio.create_task(upstream.recv())
                        try:
                            for event in events:
                                await websocket.send_json(event)
                        except (WebSocketDisconnect, RuntimeError):
                            break
                        if kind in _DRAINED_CLOSE_KINDS and parsed.get("type") == "session.closed":
                            break
                        if kind == "transcription" and closing_until is not None and not pending_transcriptions:
                            break
                        if hold_reached:
                            break
                    if calls is not None and not tokens.pending and calls.take_ready():
                        if has_response_funding():
                            response_request = {"type": "response.create"}
                            tokens.request_sent(response_request)
                            await upstream.send(json.dumps(response_request))
                        else:
                            await websocket.send_json(_client_realtime_error(wire, "realtime_hold_limit",
                                "The realtime hold cannot cover another response"))
                if tokens is not None and tokens.pending:
                    server, outgoing = await _drain_final_usage(upstream, server, tokens, provider=provider,
                        wire=wire, outgoing=outgoing, output_limit=output_limit, frame_limit=frame_limit)
                if live is not None and live.final is None and not live.invalid:
                    # Only the provider's session.closed settles GPT-Live: request it on every
                    # Lumen-side exit and drain inside the funded close window.
                    server, outgoing = await _drain_live_close(
                        upstream, server, live, websocket, wire=wire, send_close=closing_until is None,
                        until=min(closing_until or loop.time() + _USAGE_DRAIN_SECONDS,
                                  connected_at + max_duration_seconds - _LIVE_CLOSE_MARGIN_SECONDS),
                        outgoing=outgoing, output_limit=output_limit)
                if kind == "transcription" and pending_transcriptions:
                    # Deadline, idle, cancel, disconnect and clean upstream close must
                    # not turn an unfinished committed item into a successful terminal.
                    raise RealtimeTransportError("realtime transcription drain is incomplete")
            finally:
                client.cancel()
                server.cancel()
                await asyncio.gather(client, server, return_exceptions=True)
    except WebSocketDisconnect as exc:
        # Keep the established disconnect contract while making aggregate evidence available.
        exc.meter = measurement(failed=True)
        raise
    except Exception as exc:
        if session_time and isinstance(exc, TimeoutError) and deadline.expired():
            return measurement()
        # Provider failures may include secrets; expose only a safe message and bounded aggregates.
        raise RealtimeTransportError("realtime provider usage is incomplete", meter=measurement(failed=True)) from exc
    finally:
        if hard_close is not None:
            hard_close.cancel()
    return measurement()
