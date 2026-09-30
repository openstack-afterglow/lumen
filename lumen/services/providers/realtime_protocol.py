"""Ephemeral realtime PCM16 relay; only aggregate byte counts escape this module."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import websockets
from fastapi import WebSocket, WebSocketDisconnect
from websockets.exceptions import ConnectionClosed

from .realtime_transport import RealtimeTransportError, upstream_connection_spec

_MAX_FRAME_BYTES = 64 * 1024
_MAX_PCM_BYTES = 32 * 1024
_IDLE_SECONDS = 60


@dataclass(frozen=True)
class AudioMeter:
    input_bytes: int
    output_bytes: int
    input_sample_rate_hz: int
    output_sample_rate_hz: int


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


def _client_event(message: dict, provider: str, *, wire: str, voice: str | None = None) -> tuple[dict | None, int, bool]:
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
            if "transcription" in input_audio and input_audio["transcription"] != {"model": "gpt-4o-mini-transcribe"}:
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
) -> AudioMeter:
    """Relay strictly bounded frames and return PCM byte counts; never log or persist audio."""
    provider = route["provider_type"]
    input_rate = 24000 if provider == "openai" else 16000
    url, headers = upstream_connection_spec(route)
    incoming = outgoing = 0
    async with websockets.connect(url, additional_headers=headers, max_size=_MAX_FRAME_BYTES,
                                  open_timeout=10, close_timeout=5) as upstream:
        if provider == "openai":
            session = {"type": "realtime", "audio": {"input": {
                "format": {"type": "audio/pcm", "rate": 24000},
                "transcription": {"model": "gpt-4o-mini-transcribe"},
                "turn_detection": {"type": "server_vad"},
            }, "output": {"format": {"type": "audio/pcm", "rate": 24000}, "voice": voice}}}
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
        started = asyncio.get_running_loop().time()
        client = asyncio.create_task(websocket.receive_text())
        server = asyncio.create_task(upstream.recv())
        last_activity = started
        try:
            while True:
                now = asyncio.get_running_loop().time()
                elapsed = now - started
                if elapsed >= max_duration_seconds or now - last_activity >= _IDLE_SECONDS or await is_canceled():
                    break
                done, _ = await asyncio.wait({client, server}, timeout=min(1, max_duration_seconds - elapsed),
                                             return_when=asyncio.FIRST_COMPLETED)
                if not done:
                    continue
                if client in done:
                    try:
                        request = _object(client.result())
                    except WebSocketDisconnect:
                        break
                    message, size, close = _client_event(request, provider, wire=wire, voice=voice)
                    if close:
                        break
                    if incoming + size > 2 * input_rate * max_duration_seconds:
                        raise RealtimeTransportError("session input exceeds duration bound")
                    await upstream.send(json.dumps(message, separators=(",", ":")))
                    last_activity = asyncio.get_running_loop().time()
                    incoming += size
                    client = asyncio.create_task(websocket.receive_text())
                if server in done:
                    try:
                        response = server.result()
                    except ConnectionClosed as exc:
                        if exc.code != 1000:
                            raise RealtimeTransportError("upstream realtime connection closed unexpectedly") from exc
                        break
                    events, size = _provider_events(_object(response), provider, wire=wire)
                    last_activity = asyncio.get_running_loop().time()
                    if outgoing + size > 24000 * 2 * max_duration_seconds:
                        raise RealtimeTransportError("session output exceeds duration bound")
                    outgoing += size
                    try:
                        for event in events:
                            await websocket.send_json(event)
                    except (WebSocketDisconnect, RuntimeError):
                        break
                    server = asyncio.create_task(upstream.recv())
        finally:
            client.cancel()
            server.cancel()
            await asyncio.gather(client, server, return_exceptions=True)
    return AudioMeter(incoming, outgoing, input_rate, 24000)
