"""Bounded direct audio transport and financially observable billing contracts."""

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
    result = await audio_transport.transcribe_audio(
        route, data=_wav(), mime_type="audio/wav", language="en", prompt="Context",
    )
    assert result.text == "Recognized words"
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
    result = await audio_transport.generate_speech(route, text="Read aloud", voice="alloy", format="wav")
    assert (result.data, result.mime_type) == (_wav(), "audio/wav")
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

    result = await audio_transport.generate_speech(route, text="Blue lantern", voice="alloy", format="wav")
    audio, mime = result.data, result.mime_type
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
    result = await audio_transport.generate_speech(route, text="Read aloud", voice="Kore", format="wav")
    assert (result.data, result.mime_type) == (_wav(), "audio/wav")
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
    result = await audio_transport.transcribe_audio(route, data=_wav(), mime_type="audio/wav")
    assert result.text == "Spoken text"
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


def _token_route(*, provider="openai", kind="stt", envelope="0.1"):
    model = ("gpt-4o-transcribe" if provider == "openai" else
             "gemini-2.5-flash-preview-tts" if kind == "tts" else "gemini-2.5-flash")
    return {**_route(provider=provider, model=model, kind=kind, pricing={
        "billing_basis": "tokens", "reservation_usd": envelope,
        "audio_input_per_second": "100", "audio_output_per_second": "100",
        "token_rates": {"audio": {"input_per_million": "10", "cache_read_per_million": "1", "output_per_million": "20"}},
    }), "input_price_per_token": Decimal("0.000002"), "output_price_per_token": Decimal("0.000004"),
        "cache_read_price_per_token": Decimal("0.0000005")}


@pytest.mark.parametrize("kind,provider,model", [
    ("tts", "openai", "tts-1"), ("tts", "openai", "gpt-4o-mini-tts"), ("stt", "openai", "whisper-1"),
])
@pytest.mark.asyncio
async def test_known_duration_only_audio_rejects_tokens_before_io(monkeypatch, kind, provider, model):
    calls = []
    _mock(monkeypatch, lambda request: calls.append(request) or httpx.Response(500))
    route = {**_token_route(provider=provider, kind=kind), "api_model_name": model}
    with pytest.raises(ProviderValidationError):
        if kind == "tts":
            await audio_transport.generate_speech(route, text="hello", voice="alloy", format="wav")
        else:
            await audio_transport.transcribe_audio(route, data=_wav(), mime_type="audio/wav")
    assert calls == []


@pytest.mark.parametrize("kind", ["tts", "stt"])
def test_duration_aliases_produce_exact_identical_charges(kind):
    from lumen.services.durable_runs import audio

    prefix = "audio_output" if kind == "tts" else "audio_input"
    legacy = {"audio_per_second": "0.0001"} if kind == "tts" else {"audio_per_minute": "0.006"}
    configurations = [
        {f"{prefix}_per_second": "0.0001"}, {f"{prefix}_per_minute": "0.006"},
        {f"{prefix}_per_hour": "0.36"}, legacy,
        {f"{prefix}_per_second": "0.0001", f"{prefix}_per_minute": "0.006", f"{prefix}_per_hour": "0.36", **legacy},
    ]
    for media in configurations:
        route = _route(kind=kind, model="tts-1" if kind == "tts" else "whisper-1", pricing=media)
        audio_transport.validate_audio_request(route, kind=kind, format="wav" if kind == "tts" else None)
        frozen = audio._freeze_pricing(route, kind=kind, duration_ms=123456, input_text="hello", margin=Decimal(2), per_usd=Decimal(1000))
        cost, _, _ = audio._bill(frozen, kind=kind, usage={"duration_ms": 123456}, model_name=route["api_model_name"])
        assert cost.raw_cost == Decimal("0.0123456")
        assert audio._reservation_usd(frozen) == Decimal("0.0123456")


def test_duration_multiplies_before_dividing_repeating_second_rate():
    from lumen.services.durable_runs import audio

    for unit, rate in (("minute", "1"), ("hour", "60")):
        route = _route(pricing={f"audio_input_per_{unit}": rate, "audio_per_minute": "1"})
        audio_transport.validate_audio_request(route, kind="stt")
        frozen = audio._freeze_pricing(route, kind="stt", duration_ms=60000, input_text=None, margin=Decimal(2), per_usd=Decimal(1000))
        assert audio._bill(frozen, kind="stt", usage={"duration_ms": 60000}, model_name="whisper-1")[0].raw_cost == Decimal("1")


@pytest.mark.asyncio
async def test_conflicting_duration_aliases_reject_before_io(monkeypatch):
    calls = []
    _mock(monkeypatch, lambda request: calls.append(request) or httpx.Response(500))
    for media in (
        {"audio_input_per_hour": "0.36", "audio_per_minute": "0.007"},
        {"audio_input_per_hour": "60", "audio_input_per_minute": "1.000000000000000000000000001"},
    ):
        with pytest.raises(ProviderValidationError):
            await audio_transport.transcribe_audio(_route(pricing=media), data=_wav(), mime_type="audio/wav")
    assert calls == []


@pytest.mark.asyncio
async def test_openai_stt_observed_modal_tokens_settle_without_duration(monkeypatch):
    from lumen.services.durable_runs import audio

    _mock(monkeypatch, lambda request: httpx.Response(200, json={"text": "heard", "usage": {
        "type": "tokens", "input_tokens": 1000, "output_tokens": 50, "total_tokens": 1050,
        "input_token_details": {"audio_tokens": 800, "text_tokens": 200},
    }}))
    route = _token_route()
    result = await audio_transport.transcribe_audio(route, data=_wav(), mime_type="audio/wav")
    frozen = audio._freeze_pricing(route, kind="stt", duration_ms=1000, input_text=None, margin=Decimal(2), per_usd=Decimal(1000))
    assert result.text == "heard"
    cost, breakdown, components = audio._bill(frozen, kind="stt", usage={"duration_ms": 1000, "tokens": result.usage.as_usage_dict()}, model_name="gpt-4o-transcribe")
    assert cost.raw_cost == Decimal("0.0086")
    assert breakdown.modality_tokens["audio"].as_dict() == {"input_tokens": 800, "cache_read_input_tokens": 0}
    assert breakdown.cache_read_input_tokens == 0
    assert all(component["unit"] != "second" for component in components)
    assert sum(Decimal(component["cost_usd"]) for component in components) == cost.raw_cost
    assert {component["kind"] for component in components} >= {"audio_input_tokens", "input_tokens", "output_tokens"}


@pytest.mark.parametrize("kind,expected_cost", [("stt", "0.00579"), ("tts", "0.01649")])
@pytest.mark.asyncio
async def test_gemini_actual_cache_modality_and_thought_usage_is_billed(monkeypatch, kind, expected_cost):
    from lumen.services.durable_runs import audio

    usage = {"total_input_tokens": 1000 if kind == "stt" else 200,
        "total_output_tokens": 50 if kind == "stt" else 850, "total_thought_tokens": 10,
        "total_cached_tokens": 400 if kind == "stt" else 100,
        "input_tokens_by_modality": [{"modality": "text", "tokens": 200}],
        "output_tokens_by_modality": [{"modality": "text", "tokens": 50}],
        "cached_tokens_by_modality": [{"modality": "text", "tokens": 100}]}
    usage["input_tokens_by_modality" if kind == "stt" else "output_tokens_by_modality"].append({"modality": "audio", "tokens": 800})
    if kind == "stt":
        usage["cached_tokens_by_modality"].append({"modality": "audio", "tokens": 300})
    content = ({"type": "text", "text": "heard"} if kind == "stt" else
               {"type": "audio", "mime_type": "audio/wav", "data": base64.b64encode(_wav()).decode()})
    _mock(monkeypatch, lambda request: httpx.Response(200, json={"status": "completed",
        "steps": [{"type": "model_output", "content": [content]}], "usage": usage}))
    route = _token_route(provider="gemini", kind=kind)
    result = (await audio_transport.transcribe_audio(route, data=_wav(), mime_type="audio/wav") if kind == "stt" else
              await audio_transport.generate_speech(route, text="hello", voice="Kore", format="wav"))
    frozen = audio._freeze_pricing(route, kind=kind, duration_ms=1000, input_text="hello", margin=Decimal(2), per_usd=Decimal(1000))
    cost, breakdown, _ = audio._bill(frozen, kind=kind, usage={"duration_ms": 1000, "tokens": result.usage.as_usage_dict()}, model_name=route["api_model_name"])
    assert cost.raw_cost == Decimal(expected_cost)
    assert breakdown.output_tokens == usage["total_output_tokens"] + 10


@pytest.mark.asyncio
async def test_explicit_characters_charge_original_input_once(monkeypatch):
    from lumen.services.durable_runs import audio

    _mock(monkeypatch, lambda request: httpx.Response(200, content=_wav()))
    text = "  Hi 👋é  "
    route = _route(kind="tts", model="tts-1", pricing={"billing_basis": "characters", "audio_per_character": "0.001",
        "audio_output_per_second": "100", "token_rates": {"audio": {"output_per_million": "100"}}})
    await audio_transport.generate_speech(route, text=text, voice="alloy", format="wav")
    frozen = audio._freeze_pricing(route, kind="tts", duration_ms=1800000, input_text=text, margin=Decimal(2), per_usd=Decimal(1000))
    cost, breakdown, components = audio._bill(frozen, kind="tts", usage={"duration_ms": 10, "input_characters": len(text)}, model_name="tts-1")
    assert audio._reservation_usd(frozen) == cost.raw_cost == Decimal("0.009")
    assert breakdown is None
    assert [(component["unit"], component["quantity"], component["cost_usd"]) for component in components] == [("character", "9", "0.009")]


@pytest.mark.parametrize("envelope", [None, "0", "-1", "NaN", "Infinity", "invalid"])
@pytest.mark.asyncio
async def test_audio_tokens_require_positive_envelope_before_io(monkeypatch, envelope):
    calls = []
    _mock(monkeypatch, lambda request: calls.append(request) or httpx.Response(500))
    with pytest.raises(ProviderValidationError):
        await audio_transport.transcribe_audio(_token_route(envelope=envelope), data=_wav(), mime_type="audio/wav")
    assert calls == []


@pytest.mark.asyncio
async def test_legacy_duration_ignores_provider_duration_usage_and_bills_frozen_seconds(monkeypatch):
    from lumen.services.durable_runs import audio

    _mock(monkeypatch, lambda request: httpx.Response(200, json={"text": "heard", "usage": {"type": "duration", "seconds": 999}}))
    route = _route(pricing={"audio_per_minute": "0.06"})
    result = await audio_transport.transcribe_audio(route, data=_wav(), mime_type="audio/wav")
    assert result.usage is None
    frozen = audio._freeze_pricing(route, kind="stt", duration_ms=1500, input_text=None, margin=Decimal(2), per_usd=Decimal(1000))
    cost, breakdown, components = audio._bill(frozen, kind="stt", usage={"duration_ms": 1500, "tokens": None}, model_name="whisper-1")
    assert cost.raw_cost == Decimal("0.0015") and breakdown is None
    assert [(component["kind"], component["unit"]) for component in components] == [("audio_input_seconds", "second")]


@pytest.mark.parametrize("kind,provider,missing", [
    ("stt", "openai", "audio_rate"), ("stt", "openai", "output_price_per_token"),
    ("tts", "gemini", "audio_rate"), ("tts", "gemini", "input_price_per_token"), ("stt", "gemini", "output_price_per_token"),
])
@pytest.mark.asyncio
async def test_audio_tokens_require_direction_and_text_residual_rates_before_io(monkeypatch, kind, provider, missing):
    calls = []
    _mock(monkeypatch, lambda request: calls.append(request) or httpx.Response(500))
    route = _token_route(provider=provider, kind=kind)
    if missing == "audio_rate":
        direction = "output_per_million" if kind == "tts" else "input_per_million"
        rates = {key: value for key, value in route["media_pricing"]["token_rates"]["audio"].items() if key != direction}
        route["media_pricing"] = {**route["media_pricing"], "token_rates": {"audio": rates}}
    else:
        route[missing] = None
    with pytest.raises(ProviderValidationError):
        if kind == "tts":
            await audio_transport.generate_speech(route, text="hello", voice="Kore", format="wav")
        else:
            await audio_transport.transcribe_audio(route, data=_wav(), mime_type="audio/wav")
    assert not audio_transport.audio_route_ready(route)
    assert calls == []


@pytest.mark.parametrize("kind,expected_cost", [("stt", "0.0082"), ("tts", "0.0164")])
@pytest.mark.asyncio
async def test_audio_tokens_allow_unpriced_unused_text_direction(monkeypatch, kind, expected_cost):
    from lumen.services.durable_runs import audio

    if kind == "stt":
        route = {**_token_route(), "input_price_per_token": None}
        response = {"text": "heard", "usage": {"type": "tokens", "input_tokens": 800, "output_tokens": 50,
            "input_token_details": {"audio_tokens": 800, "text_tokens": 0}}}
    else:
        route = {**_token_route(provider="gemini", kind="tts"), "output_price_per_token": None}
        response = {"status": "completed", "steps": [{"type": "model_output", "content": [
            {"type": "audio", "mime_type": "audio/wav", "data": base64.b64encode(_wav()).decode()}]}], "usage": {
                "total_input_tokens": 200, "total_output_tokens": 800, "total_cached_tokens": 0, "total_thought_tokens": 0,
                "input_tokens_by_modality": [{"modality": "text", "tokens": 200}],
                "output_tokens_by_modality": [{"modality": "audio", "tokens": 800}]}}
    _mock(monkeypatch, lambda request: httpx.Response(200, json=response))
    result = (await audio_transport.transcribe_audio(route, data=_wav(), mime_type="audio/wav") if kind == "stt" else
              await audio_transport.generate_speech(route, text="hello", voice="Kore", format="wav"))
    frozen = audio._freeze_pricing(route, kind=kind, duration_ms=1000, input_text="hello", margin=Decimal(2), per_usd=Decimal(1000))
    cost, _, components = audio._bill(frozen, kind=kind, usage={"duration_ms": 1000, "tokens": result.usage.as_usage_dict()}, model_name=route["api_model_name"])
    assert cost.raw_cost == Decimal(expected_cost)
    assert sum(Decimal(component["cost_usd"]) for component in components) == cost.raw_cost


def _verbose(segments, text="Hello there. General Kenobi."):
    # Shape of an actual whisper-1 verbose_json body, including fields Lumen must drop.
    return {"task": "transcribe", "language": "english", "duration": 3.2, "text": text,
            "segments": segments, "usage": {"type": "duration", "seconds": 4}}


def _provider_segment(start, end, text, **extra):
    return {"id": 0, "seek": 0, "start": start, "end": end, "text": text, "tokens": [50364, 2425],
            "temperature": 0.0, "avg_logprob": -0.2, "compression_ratio": 0.9, "no_speech_prob": 0.01, **extra}


@pytest.mark.asyncio
async def test_whisper_segment_timing_uses_verbose_json_and_keeps_only_provider_ranges(monkeypatch):
    requests = []
    silent = False

    def handle(request):
        requests.append(request)
        if silent:
            return httpx.Response(200, json=_verbose([], text=""))
        if b"verbose_json" not in request.content:
            return httpx.Response(200, json={"text": "plain"})
        return httpx.Response(200, json=_verbose([
            _provider_segment(0, 1.5, " Hello there."), _provider_segment(1.5, 3.04, " General Kenobi."),
        ]))

    _mock(monkeypatch, handle)
    route = _route()
    timed = await audio_transport.transcribe_audio(route, data=_wav(), mime_type="audio/wav",
                                                   language="en", timestamp_granularities=["segment"])
    assert timed.text == "Hello there. General Kenobi."
    assert timed.segments == (
        audio_transport.TranscriptSegment(0.0, 1.5, " Hello there."),
        audio_transport.TranscriptSegment(1.5, 3.04, " General Kenobi."),
    )
    assert timed.usage is None
    body = requests[0].content
    assert b'name="response_format"\r\n\r\nverbose_json\r\n' in body
    assert b'name="timestamp_granularities[]"\r\n\r\nsegment\r\n' in body
    assert body.count(b'name="timestamp_granularities[]"') == 1

    for omitted in (None, []):
        plain = await audio_transport.transcribe_audio(route, data=_wav(), mime_type="audio/wav",
                                                       timestamp_granularities=omitted)
        assert (plain.text, plain.segments) == ("plain", None)
    legacy = await audio_transport.transcribe_audio(route, data=_wav(), mime_type="audio/wav")
    assert (legacy.text, legacy.segments) == ("plain", None)
    for request in requests[1:]:
        assert b'name="response_format"\r\n\r\njson\r\n' in request.content
        assert b"timestamp_granularities" not in request.content

    silent = True
    quiet = await audio_transport.transcribe_audio(route, data=_wav(), mime_type="audio/wav",
                                                   timestamp_granularities=["segment"])
    assert (quiet.text, quiet.segments) == ("", ())


@pytest.mark.parametrize("body", [
    json.dumps({"text": "spoken secret"}).encode(),
    json.dumps(_verbose([], text="spoken secret")).encode(),
    json.dumps(_verbose({"start": 0, "end": 1, "text": "x"})).encode(),
    json.dumps(_verbose(["not a segment"])).encode(),
    json.dumps(_verbose([_provider_segment(True, 1, "x")])).encode(),
    json.dumps(_verbose([_provider_segment(0, "1.0", "x")])).encode(),
    b'{"text":"x","segments":[{"start":NaN,"end":1,"text":"x"}]}',
    b'{"text":"x","segments":[{"start":0,"end":Infinity,"text":"x"}]}',
    json.dumps(_verbose([_provider_segment(-0.1, 1, "x")])).encode(),
    json.dumps(_verbose([_provider_segment(2, 1, "x")])).encode(),
    json.dumps(_verbose([_provider_segment(0, 2, "x"), _provider_segment(1.9, 3, "y")])).encode(),
    json.dumps(_verbose([_provider_segment(1800, 1800.5, "x")])).encode(),
    json.dumps(_verbose([_provider_segment(0, 1, None)])).encode(),
    json.dumps(_verbose([{"start": 0, "end": 1}])).encode(),
    json.dumps(_verbose([_provider_segment(i, i, "") for i in range(4097)])).encode(),
    json.dumps(_verbose([_provider_segment(0, 1, "x" * 65536), _provider_segment(1, 2, "y")])).encode(),
])
@pytest.mark.asyncio
async def test_requested_timing_rejects_missing_or_invalid_provider_segments(monkeypatch, body):
    _mock(monkeypatch, lambda request: httpx.Response(200, content=body))
    with pytest.raises(audio_transport.AudioTransportError) as exc:
        await audio_transport.transcribe_audio(_route(), data=_wav(), mime_type="audio/wav",
                                               timestamp_granularities=["segment"])
    assert "secret" not in str(exc.value) and "sensitive-key" not in str(exc.value)


@pytest.mark.asyncio
async def test_segment_timing_boundaries_accept_touching_ranges_and_source_maximum(monkeypatch):
    segments = [_provider_segment(0, 0, ""), _provider_segment(0, 900, "a"), _provider_segment(900, 1800, "b")]
    _mock(monkeypatch, lambda request: httpx.Response(200, json=_verbose(segments, text="a b")))
    result = await audio_transport.transcribe_audio(_route(), data=_wav(), mime_type="audio/wav",
                                                    timestamp_granularities=("segment",))
    assert [(item.start, item.end) for item in result.segments] == [(0.0, 0.0), (0.0, 900.0), (900.0, 1800.0)]
    assert all(type(item.start) is float and type(item.end) is float for item in result.segments)


@pytest.mark.asyncio
async def test_unsupported_or_invalid_timestamp_requests_fail_before_network(monkeypatch):
    calls = []
    _mock(monkeypatch, lambda request: calls.append(request) or httpx.Response(200, json={"text": "x"}))
    unsupported = [
        _route(model="gpt-4o-transcribe"),
        _route(model="gpt-4o-mini-transcribe"),
        _route(provider="gemini", model="gemini-2.5-flash", pricing={"audio_input_per_second": "0.0001"}),
    ]
    for route in unsupported:
        audio_transport.validate_audio_request(route, kind="stt")
        assert audio_transport.available_timestamp_granularities(route) == []
        with pytest.raises(ProviderValidationError, match="unsupported"):
            await audio_transport.transcribe_audio(route, data=_wav(), mime_type="audio/wav",
                                                   timestamp_granularities=["segment"])
    for invalid in (["word"], ["segment", "segment"], "segment", [None], {"segment": True}):
        with pytest.raises(ProviderValidationError, match="granularities"):
            await audio_transport.transcribe_audio(_route(), data=_wav(), mime_type="audio/wav",
                                                   timestamp_granularities=invalid)
    with pytest.raises(ProviderValidationError):
        audio_transport.validate_audio_request(_route(model="tts-1", kind="tts", pricing={"audio_output_per_second": "0.001"}),
                                               kind="tts", format="mp3", timestamp_granularities=["segment"])
    assert audio_transport.available_timestamp_granularities(_route(pricing={"audio_per_minute": "0"})) == []
    assert audio_transport.available_timestamp_granularities({**_route(), "api_key": None}) == []
    assert audio_transport.available_timestamp_granularities(_route()) == ["segment"]
    assert calls == []


@pytest.mark.asyncio
async def test_transcription_capability_advertises_timing_only_for_executable_whisper(monkeypatch):
    from lumen.api import conversations

    gate = {"feature_gates": {"audio_input": {"available": True, "mode": "native", "pricing_available": True}}}
    route = {**_route(), "model_id": 21, "model_name": "whisper-1", "capabilities": gate}

    async def resolve(model_id, *, model_kind):
        assert (model_id, model_kind) == (21, "stt")
        return route

    monkeypatch.setattr(conversations.routing, "resolve_model_by_id", resolve)
    monkeypatch.setattr(conversations.capabilities, "runtime_capabilities", lambda: {})
    monkeypatch.setattr(conversations.capabilities, "effective_runtime_capabilities", lambda *_: {})
    available = await conversations.get_chat_capabilities(model_id=21, model_kind="stt", token_info={})
    assert available["available_timestamp_granularities"] == ["segment"]
    assert "sensitive-key" not in repr(available)

    route.update(api_model_name="gpt-4o-transcribe", model_name="gpt-4o-transcribe")
    other = await conversations.get_chat_capabilities(model_id=21, model_kind="stt", token_info={})
    assert other["available_timestamp_granularities"] == []

    route.update(api_model_name="whisper-1", capabilities={"feature_gates": {"audio_input": {"available": False}}})
    gated = await conversations.get_chat_capabilities(model_id=21, model_kind="stt", token_info={})
    assert gated["available_timestamp_granularities"] == []
