"""Ephemeral realtime PCM16 relay; only aggregate byte counts and provider token usage escape."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import websockets
from fastapi import WebSocket, WebSocketDisconnect
from websockets.exceptions import ConnectionClosed

from lumen.services.usage_breakdown import UsageBreakdown

from .realtime_transport import CLOSE_TIMEOUT_SECONDS, RealtimeTransportError, upstream_connection_spec

_MAX_FRAME_BYTES = 64 * 1024
_MAX_PCM_BYTES = 32 * 1024
_IDLE_SECONDS = 60
_USAGE_DRAIN_SECONDS = 15
_MAX_METERED_RESPONSES = 4096


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
    # Monotonic seconds from completed upstream open handshake to completed close.
    connected_seconds: Decimal | None = None


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

    @property
    def pending(self) -> bool:
        """Provider work was observed whose final usage has not arrived yet."""
        return not self._invalid and (bool(self._in_flight) or self._pending)

    def request_sent(self, message: dict) -> None:
        # A client-requested response can start I/O before response.created reaches us.
        if self._provider == "openai" and message.get("type") in {"response.create", "input_audio_buffer.commit"}:
            self._pending = True

    def observe(self, message: dict) -> None:
        if self._invalid:
            return
        if self._provider == "openai":
            self._observe_openai(message)
        else:
            self._observe_gemini(message)

    def _observe_openai(self, message: dict) -> None:
        event = message.get("type")
        if event == "error":
            # A provider rejection acknowledges the attempted request without starting a response.
            # Any response already created remains tracked in _in_flight until response.done.
            self._pending = False
            return
        if event not in {"response.created", "response.done"}:
            return
        response = message.get("response")
        response_id = response.get("id") if isinstance(response, dict) else None
        if not isinstance(response_id, str) or not response_id or len(response_id) > 128:
            self._invalid = True
            return
        if event == "response.created":
            self._pending = False
            if response_id not in self._responses:
                self._in_flight.add(response_id)
            return
        usage = UsageBreakdown.from_openai_media(response.get("usage"))
        previous = self._responses.get(response_id)
        if (usage is None or usage.modality_usage_invalid or (previous is not None and previous != usage)
                or (previous is None and len(self._responses) >= _MAX_METERED_RESPONSES)):
            self._invalid = True
            return
        self._responses[response_id] = usage
        self._in_flight.discard(response_id)
        self._pending = False

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


def _object(value: str | bytes) -> dict[str, Any]:
    if not isinstance(value, str) or len(value.encode("utf-8")) > _MAX_FRAME_BYTES:
        raise RealtimeTransportError("realtime frame exceeds limit")
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError) as exc:
        raise RealtimeTransportError("invalid realtime JSON frame") from exc
    if not isinstance(parsed, dict):
        raise RealtimeTransportError("invalid realtime event")
    return parsed


def _client_event(message: dict, provider: str, *, wire: str, voice: str | None = None,
                  allow_transcription: bool = True) -> tuple[dict | None, int, bool]:
    event = message.get("type")
    if wire == "openai":
        if event == "input_audio_buffer.append":
            audio, size = _pcm(message.get("audio"))
            return {"type": event, "audio": audio}, size, False
        if event in {"input_audio_buffer.commit", "input_audio_buffer.clear", "response.cancel", "response.create"}:
            if event == "response.create" and message.get("response") not in (None, {}):
                raise RealtimeTransportError("unsupported realtime response configuration")
            return {"type": event}, 0, False
        if event == "session.update":
            session = message.get("session")
            if not isinstance(session, dict) or set(session) - {"type", "instructions", "audio", "output_modalities"}:
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
            safe = {key: session[key] for key in ("instructions", "audio") if key in session}
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
        if event == "response.cancel":
            if provider == "openai":
                return {"type": "response.cancel"}, 0, False
            # Gemini Live has automatic barge-in but no response.cancel command.
            # Close the provider session rather than triggering another turn.
            return None, 0, True
        if event == "session.close":
            return None, 0, True
    raise RealtimeTransportError("unsupported realtime client event")


def _provider_events(message: dict, provider: str, *, wire: str) -> tuple[list[dict], int]:
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
                             wire: str, outgoing: int, output_limit: int) -> tuple[asyncio.Task, int]:
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
        parsed = _object(response)
        meter.observe(parsed)
        # The client has already stopped: count provider audio against the bound, forward nothing.
        _, size = _provider_events(parsed, provider, wire=wire)
        if outgoing + size > output_limit:
            raise RealtimeTransportError("session output exceeds duration bound")
        outgoing += size
        server = asyncio.create_task(upstream.recv())
    return server, outgoing


async def relay_audio(
    websocket: WebSocket,
    route: dict,
    *,
    session_id: str,
    voice: str,
    instructions: str | None,
    max_duration_seconds: int,
    wire: str = "native",
    is_canceled: Callable[[], Awaitable[bool]],
    token_usage: bool = False,
    session_time: bool = False,
) -> AudioMeter:
    """Relay strictly bounded frames and return PCM byte counts; never log or persist audio.

    ``token_usage`` sessions additionally meter provider-reported token usage. OpenAI's
    input transcription is a separately billed model whose rates are not frozen, so it is
    neither configured nor accepted for token-basis sessions.
    """
    provider = route["provider_type"]
    input_rate = 24000 if provider == "openai" else 16000
    if session_time and max_duration_seconds <= CLOSE_TIMEOUT_SECONDS:
        raise RealtimeTransportError("session duration must exceed the upstream close timeout")
    relay_limit = max_duration_seconds - CLOSE_TIMEOUT_SECONDS if session_time else max_duration_seconds
    output_limit = 24000 * 2 * max_duration_seconds
    url, headers = upstream_connection_spec(route)
    incoming = outgoing = 0
    tokens = TokenUsageMeter(provider) if token_usage else None
    loop = asyncio.get_running_loop()
    connected_ns: int | None = None
    hard_close: asyncio.TimerHandle | None = None

    def measurement(*, failed: bool = False) -> AudioMeter:
        elapsed = (Decimal(time.monotonic_ns() - connected_ns) / Decimal(1_000_000_000)
                   if connected_ns is not None else None)
        usage, state = tokens.result() if tokens is not None else (None, "unmetered")
        if failed and tokens is not None and state != "invalid":
            state = "incomplete"
        return AudioMeter(incoming, outgoing, input_rate, 24000, usage=usage, usage_state=state,
                          connected_seconds=elapsed)

    try:
        async with asyncio.timeout(None) as deadline, websockets.connect(
            url, additional_headers=headers, max_size=_MAX_FRAME_BYTES,
            open_timeout=10, close_timeout=CLOSE_TIMEOUT_SECONDS,
        ) as upstream:
            # Provider-connected time: open handshake complete (context entry) to context exit.
            connected_ns = time.monotonic_ns()
            connected_at = loop.time()
            if session_time:
                # Leave the bounded close handshake inside the funded envelope. The backstop
                # aborts transport just before max; actual connected nanoseconds are never clamped.
                hard_at = connected_at + max_duration_seconds - 0.001
                hard_close = loop.call_at(hard_at, upstream.transport.abort)
                deadline.reschedule(hard_at)
            if provider == "openai":
                input_config: dict = {"format": {"type": "audio/pcm", "rate": 24000}}
                if tokens is None:
                    input_config["transcription"] = {"model": "gpt-4o-mini-transcribe"}
                input_config["turn_detection"] = {"type": "server_vad"}
                session = {"type": "realtime", "audio": {"input": input_config,
                           "output": {"format": {"type": "audio/pcm", "rate": 24000}, "voice": voice}}}
                if instructions:
                    session["instructions"] = instructions
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
                                           "provider_type": provider, "input_sample_rate_hz": input_rate,
                                           "output_sample_rate_hz": 24000})
            # Session basis counts from upstream open; legacy PCM mode retains its post-setup deadline.
            started = connected_at if session_time else loop.time()
            client = asyncio.create_task(websocket.receive_text())
            server = asyncio.create_task(upstream.recv())
            last_activity = started
            try:
                while True:
                    now = loop.time()
                    elapsed = now - started
                    if elapsed >= relay_limit or now - last_activity >= _IDLE_SECONDS or await is_canceled():
                        break
                    done, _ = await asyncio.wait({client, server}, timeout=min(1, relay_limit - elapsed),
                                                 return_when=asyncio.FIRST_COMPLETED)
                    if not done:
                        continue
                    if client in done:
                        try:
                            request = _object(client.result())
                        except WebSocketDisconnect:
                            break
                        message, size, close = _client_event(request, provider, wire=wire, voice=voice,
                                                             allow_transcription=tokens is None)
                        if close:
                            break
                        if incoming + size > 2 * input_rate * max_duration_seconds:
                            raise RealtimeTransportError("session input exceeds duration bound")
                        if tokens is not None:
                            tokens.request_sent(message)
                        await upstream.send(json.dumps(message, separators=(",", ":")))
                        last_activity = loop.time()
                        incoming += size
                        client = asyncio.create_task(websocket.receive_text())
                    if server in done:
                        try:
                            response = server.result()
                        except ConnectionClosed as exc:
                            if exc.code != 1000:
                                raise RealtimeTransportError("upstream realtime connection closed unexpectedly") from exc
                            break
                        parsed = _object(response)
                        if tokens is not None:
                            tokens.observe(parsed)
                        events, size = _provider_events(parsed, provider, wire=wire)
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
                if tokens is not None and tokens.pending:
                    server, outgoing = await _drain_final_usage(upstream, server, tokens, provider=provider,
                        wire=wire, outgoing=outgoing, output_limit=output_limit)
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
