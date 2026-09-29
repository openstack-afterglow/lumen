"""Bounded direct audio transport and fail-closed duration pricing contracts."""

import base64
import io
import json
import wave
from decimal import Decimal

import httpx
import pytest

from lumen.services.providers import audio_transport
from lumen.services.providers.errors import ProviderValidationError


def _wav():
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(24000)
        stream.writeframes(b"\x00\x00" * 240)
    return buffer.getvalue()


def _route(provider="openai", model="whisper-1", kind="stt", pricing=None):
    return {
        "model_kind": kind, "provider_type": provider, "api_model_name": model,
        "api_key": "sensitive-key", "api_base": None, "provider_auth": None,
        "media_pricing": pricing if pricing is not None else {"audio_per_minute": "0.006"},
    }


def _mock(monkeypatch, handler):
    client = httpx.AsyncClient
    monkeypatch.setattr(audio_transport.httpx, "AsyncClient", lambda **kwargs: client(
        transport=httpx.MockTransport(handler), **kwargs,
    ))


@pytest.mark.asyncio
async def test_whisper_transcribes_multipart_and_converts_configured_rate(monkeypatch):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"text": "Recognized words"})

    _mock(monkeypatch, handle)
    route = _route()
    assert audio_transport.validate_audio_request(route, kind="stt", format="audio/wav") == Decimal("0.0001")
    assert audio_transport.audio_route_ready(route)
    assert await audio_transport.transcribe_audio(
        route, data=_wav(), mime_type="audio/wav", language="en", prompt="Context",
    ) == "Recognized words"
    assert len(requests) == 1
    request = requests[0]
    assert request.url == "https://api.openai.com/v1/audio/transcriptions"
    assert request.headers["authorization"] == "Bearer sensitive-key"
    assert b'filename="audio.wav"' in request.content
    assert b'name="response_format"' in request.content
    assert b"sensitive-key" not in request.content


@pytest.mark.asyncio
async def test_unsupported_or_unpriced_audio_routes_fail_before_network(monkeypatch):
    calls = []
    _mock(monkeypatch, lambda request: calls.append(request) or httpx.Response(500))
    routes = [
        _route(model="unknown-transcribe"),
        _route(provider="gemini", model="unknown-gemini"),
        _route(model="unknown-tts", kind="tts", pricing={"audio_output_per_second": "0.1"}),
        _route(model="tts-1", kind="tts", pricing={"audio_per_character": "0.01"}),
        _route(provider="gemini", model="gemini-2.5-flash-preview-tts", kind="tts", pricing={"audio_per_character": "0.01"}),
        _route(pricing={"audio_per_minute": "0"}),
        _route(pricing={"audio_per_second": "0.0001"}),
        _route(pricing={"audio_per_minute": "0.00000000001"}),
        {**_route(), "api_base": "https://attacker.example"},
        {**_route(), "api_key": None},
        {**_route(), "provider_auth": {"type": "subscription"}},
    ]
    for route in routes:
        with pytest.raises(ProviderValidationError):
            if route["model_kind"] == "tts":
                await audio_transport.generate_speech(route, text="Hello", voice="alloy")
            else:
                await audio_transport.transcribe_audio(route, data=_wav(), mime_type="audio/wav")
        assert not audio_transport.audio_route_ready(route)
    assert calls == []


@pytest.mark.asyncio
async def test_invalid_audio_or_response_never_fetches_url(monkeypatch):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json={"url": "https://attacker.example/secret"})

    _mock(monkeypatch, handle)
    route = _route()
    for data, mime in ((b"RIFF" + b"\x00" * 64, "audio/wav"),
                       (_wav(), "audio/unknown"), (b"ID3", "audio/mpeg")):
        with pytest.raises(ProviderValidationError):
            await audio_transport.transcribe_audio(route, data=data, mime_type=mime)
    assert calls == []
    with pytest.raises(audio_transport.AudioTransportError):
        await audio_transport.transcribe_audio(route, data=_wav(), mime_type="audio/wav")
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_upstream_errors_do_not_echo_credentials_or_body(monkeypatch):
    _mock(monkeypatch, lambda request: httpx.Response(401, text="sensitive-key secret response"))
    with pytest.raises(audio_transport.AudioTransportError) as exc:
        await audio_transport.transcribe_audio(_route(), data=_wav(), mime_type="audio/wav")
    assert "sensitive-key" not in str(exc.value)
    assert "secret response" not in str(exc.value)


@pytest.mark.asyncio
async def test_speech_wire_and_binary_validation_when_exact_model_is_available(monkeypatch):
    requests = []
    _mock(monkeypatch, lambda request: requests.append(request) or httpx.Response(200, content=_wav()))
    route = _route(model="tts-1", kind="tts", pricing={"audio_output_per_second": "0.001"})
    assert audio_transport.validate_audio_request(route, kind="tts", format="wav") == Decimal("0.001")
    assert await audio_transport.generate_speech(route, text="Read aloud", voice="alloy", format="wav") == (_wav(), "audio/wav")
    assert requests[0].url == "https://api.openai.com/v1/audio/speech"
    assert json.loads(requests[0].content) == {
        "model": "tts-1", "input": "Read aloud", "voice": "alloy", "response_format": "wav",
    }


@pytest.mark.asyncio
async def test_openai_streamed_wav_is_normalized_for_playback_and_transcription(monkeypatch):
    canonical = _wav()
    streamed = bytearray(canonical)
    streamed[4:8] = b"\xff" * 4
    streamed[40:44] = b"\xff" * 4
    _mock(monkeypatch, lambda request: httpx.Response(200, content=bytes(streamed)))
    route = _route(model="gpt-4o-mini-tts", kind="tts", pricing={"audio_output_per_second": "0.001"})

    audio, mime = await audio_transport.generate_speech(route, text="Blue lantern", voice="alloy", format="wav")
    assert mime == "audio/wav"
    assert audio == canonical
    with wave.open(io.BytesIO(audio), "rb") as sound:
        assert sound.getnframes() == 240
    with pytest.raises(audio_transport.AudioTransportError):
        audio_transport._output_audio(bytes(streamed[:-1]), "wav")

@pytest.mark.asyncio
async def test_gemini_interactions_parses_completed_inline_speech_only(monkeypatch):
    assert audio_transport.audio_route_ready(_route(provider="gemini", model="gemini-2.5-flash-preview-tts", kind="tts", pricing={"audio_output_per_second": "0.001"}))
    requests = []

    def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"status": "completed", "steps": [
            {"type": "model_output", "content": [{"type": "audio", "mime_type": "audio/wav", "data": base64.b64encode(_wav()).decode()}]},
        ]})

    _mock(monkeypatch, handle)
    route = _route(provider="gemini", model="gemini-2.5-flash-preview-tts", kind="tts", pricing={"audio_output_per_second": "0.001"})
    assert await audio_transport.generate_speech(route, text="Read aloud", voice="Kore", format="wav") == (_wav(), "audio/wav")
    assert requests[0]["input"] == [{"type": "user_input", "content": [{"type": "text", "text": "Read aloud"}]}]
    assert requests[0]["response_format"] == {"type": "audio"}
    assert requests[0]["generation_config"] == {"speech_config": [{"voice": "Kore"}]}
    assert requests[0]["store"] is False


@pytest.mark.asyncio
async def test_gemini_rejects_uri_and_incomplete_audio(monkeypatch):
    route = _route(provider="gemini", model="gemini-2.5-flash-preview-tts", kind="tts", pricing={"audio_output_per_second": "0.001"})
    responses = [
        {"status": "completed", "steps": [{"type": "model_output", "content": [{"type": "audio", "uri": "https://attacker.example"}]}]},
        {"status": "in_progress", "steps": [{"type": "model_output", "content": [{"type": "audio", "mime_type": "audio/wav", "data": base64.b64encode(_wav()).decode()}]}]},
    ]
    _mock(monkeypatch, lambda request: httpx.Response(200, json=responses.pop(0)))
    for _ in range(2):
        with pytest.raises(audio_transport.AudioTransportError):
            await audio_transport.generate_speech(route, text="Read aloud", voice="Kore", format="wav")


@pytest.mark.asyncio
async def test_transcription_response_is_bounded_before_json_parsing(monkeypatch):
    _mock(monkeypatch, lambda request: httpx.Response(200, content=b"x" * (audio_transport._MAX_TRANSCRIPT_BYTES + 1)))
    with pytest.raises(audio_transport.AudioTransportError, match="size limit"):
        await audio_transport.transcribe_audio(_route(), data=_wav(), mime_type="audio/wav")


@pytest.mark.asyncio
async def test_gemini_transcription_reads_only_model_output_text(monkeypatch):
    captured = []

    def handle(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"status": "completed", "steps": [
            {"type": "thought", "content": [{"type": "text", "text": "private text"}]},
            {"type": "model_output", "content": [{"type": "text", "text": "Spoken text"}]},
        ]})

    _mock(monkeypatch, handle)
    route = _route(provider="gemini", model="gemini-2.5-flash", pricing={"audio_input_per_second": "0.0001"})
    assert await audio_transport.transcribe_audio(route, data=_wav(), mime_type="audio/wav") == "Spoken text"
    assert captured[0]["input"][1] == {
        "type": "audio", "mime_type": "audio/wav", "data": base64.b64encode(_wav()).decode(),
    }
    assert captured[0]["store"] is False


@pytest.mark.asyncio
async def test_speech_rejects_false_wav_signature(monkeypatch):
    _mock(monkeypatch, lambda request: httpx.Response(200, content=b"RIFF" + b"\x00" * 4 + b"WAVE" + b"\x00" * 100))
    route = _route(model="tts-1", kind="tts", pricing={"audio_output_per_second": "0.001"})
    with pytest.raises(audio_transport.AudioTransportError):
        await audio_transport.generate_speech(route, text="Read aloud", voice="alloy", format="wav")


@pytest.mark.asyncio
async def test_speech_capability_choices_match_the_selected_provider(monkeypatch):
    from lumen.api import conversations

    gate = {"feature_gates": {"audio_output": {"available": True, "mode": "native", "pricing_available": True}}}
    route = {**_route(provider="gemini", model="gemini-2.5-flash-preview-tts", kind="tts",
                      pricing={"audio_output_per_second": "0.001"}),
             "model_id": 17, "model_name": "gemini-2.5-flash-preview-tts", "capabilities": gate}

    async def resolve(model_id, *, model_kind):
        assert (model_id, model_kind) == (17, "tts")
        return route

    monkeypatch.setattr(conversations.routing, "resolve_model_by_id", resolve)
    monkeypatch.setattr(conversations.capabilities, "runtime_capabilities", lambda: {})
    monkeypatch.setattr(conversations.capabilities, "effective_runtime_capabilities", lambda *_: {})
    available = await conversations.get_chat_capabilities(model_id=17, model_kind="tts", token_info={})
    assert "Kore" in available["available_voices"]
    assert "alloy" not in available["available_voices"]
    assert available["available_formats"] == ["wav"]
    assert "sensitive-key" not in repr(available)

    route["capabilities"] = {"feature_gates": {"audio_output": {"available": False}}}
    unavailable = await conversations.get_chat_capabilities(model_id=17, model_kind="tts", token_info={})
    assert unavailable["available_voices"] == []
    assert unavailable["available_formats"] == []
