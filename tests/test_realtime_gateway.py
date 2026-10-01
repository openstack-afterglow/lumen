"""Realtime direct provider gate, ephemeral relay and exact PCM/session/token billing boundaries."""

import asyncio
import base64
import json
from decimal import Decimal
from types import SimpleNamespace

import pytest

from lumen.services.durable_runs import realtime
from lumen.services.durable_runs.errors import DurableRunProviderResultUnknown
from lumen.services.providers import realtime_protocol, realtime_transport
from lumen.services.providers.errors import ProviderValidationError
from lumen.services.providers.realtime_protocol import AudioMeter, TokenUsageMeter


def _route(provider="openai"):
    return {"model_kind": "realtime", "provider_type": provider,
            "api_model_name": "gpt-realtime" if provider == "openai" else "gemini-2.5-flash-live",
            "api_base": None, "api_key": "upstream-private-key", "provider_auth": None,
            "media_pricing": {"realtime_input_per_minute": "0.012", "realtime_output_per_minute": "0.024"}}


def test_realtime_route_requires_direct_keys_and_both_exact_prices():
    route = _route()
    plan = realtime_transport.validate_realtime_request(route, voice="alloy")
    assert plan["billing_basis"] == "duration"
    assert {family: Decimal(rate["rate"]) / rate["unit_seconds"] for family, rate in plan["duration_rates"].items()} == {
        "realtime_input": Decimal("0.0002"), "realtime_output": Decimal("0.0004")}
    assert realtime_transport.realtime_route_ready(route)
    assert realtime_transport.available_realtime_options(route)["input_sample_rate_hz"] == 24000
    url, headers = realtime_transport.upstream_connection_spec(route)
    assert url == "wss://api.openai.com/v1/realtime?model=gpt-realtime"
    assert headers["Authorization"] == "Bearer " + route["api_key"]
    for change in ({"api_base": "https://proxy.invalid/v1"}, {"api_key": ""},
                   {"provider_auth": {"auth_mode": "chatgpt_device"}},
                   {"media_pricing": {"realtime_input_per_minute": "0.012"}},
                   {"api_model_name": "gpt-image-1"}):
        assert not realtime_transport.realtime_route_ready({**route, **change})
    with pytest.raises(ProviderValidationError):
        realtime_transport.validate_realtime_request(route, voice="Kore")


def test_gemini_route_only_advertises_gemini_voices_and_canonical_live_endpoint():
    route = {**_route("gemini"), "api_key": "upstream/private+key"}
    options = realtime_transport.available_realtime_options(route)
    assert options["input_sample_rate_hz"] == 16000
    assert "Kore" in options["available_voices"] and "alloy" not in options["available_voices"]
    url, headers = realtime_transport.upstream_connection_spec(route)
    assert url == ("wss://generativelanguage.googleapis.com/ws/"
                   "google.ai.generativelanguage.v1beta.GenerativeService."
                   "BidiGenerateContent?key=upstream%2Fprivate%2Bkey")
    assert headers == {}


class FakeUpstream:
    def __init__(self, provider, incoming):
        self.provider = provider
        self.incoming = incoming
        self.sent = []
        self.queue = asyncio.Queue()

    async def send(self, message):
        parsed = json.loads(message)
        self.sent.append(parsed)
        if (parsed.get("type") == "input_audio_buffer.append" or "realtimeInput" in parsed
                and "audio" in parsed["realtimeInput"]):
            if self.provider == "openai":
                event = {"type": "response.output_audio.delta", "delta": self.incoming}
            else:
                event = {"serverContent": {"modelTurn": {"parts": [{"inlineData": {
                    "mimeType": "audio/pcm;rate=24000", "data": self.incoming}}]},
                    "outputTranscription": {"text": "heard"}}}
            await self.queue.put(json.dumps(event))

    async def recv(self):
        return await self.queue.get()


class FakeBrowser:
    def __init__(self, provider, incoming):
        self.provider = provider
        self.incoming = incoming
        self.queue = asyncio.Queue()
        self.events = []
        self.queue.put_nowait(json.dumps({"type": "audio.input.append", "audio": incoming}))

    async def receive_text(self):
        return await self.queue.get()

    async def send_json(self, event):
        self.events.append(event)
        if event.get("type") == "audio.output.delta":
            await self.queue.put(json.dumps({"type": "session.close"}))


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openai", "gemini"])
async def test_native_realtime_relay_translates_frames_without_persisting_them(monkeypatch, provider):
    encoded = base64.b64encode(b"\0\1" * 160).decode()
    upstream = FakeUpstream(provider, encoded)

    class Connection:
        async def __aenter__(self):
            return upstream
        async def __aexit__(self, *_):
            return None

    captured = {}
    def connect(url, **kwargs):
        captured.update(url=url, **kwargs)
        return Connection()

    monkeypatch.setattr(realtime_protocol.websockets, "connect", connect)
    browser = FakeBrowser(provider, encoded)

    async def not_canceled():
        return False

    meter = await realtime_protocol.relay_audio(browser, _route(provider), session_id="session", voice=(
        "alloy" if provider == "openai" else "Kore"), instructions=None,
        max_duration_seconds=10, is_canceled=not_canceled)
    assert meter.input_bytes == 320 and meter.output_bytes == 320
    assert meter.input_sample_rate_hz == (24000 if provider == "openai" else 16000)
    assert browser.events[0]["type"] == "session.ready"
    assert next(event for event in browser.events if event["type"] == "audio.output.delta")["delta"] == encoded
    if provider == "openai":
        assert upstream.sent[0]["type"] == "session.update"
    else:
        assert "setup" in upstream.sent[0]
    assert "upstream-private-key" not in repr(browser.events)
    if provider == "openai":
        assert "upstream-private-key" in repr(captured["additional_headers"])
        assert "upstream-private-key" not in captured["url"]
    else:
        assert captured["url"].endswith("?key=upstream-private-key")
        assert captured["additional_headers"] == {}

def test_native_cancel_keeps_openai_session_but_closes_gemini_live():
    assert realtime_protocol._client_event({"type": "response.cancel"}, "openai", wire="native") == (
        {"type": "response.cancel"}, 0, False)
    assert realtime_protocol._client_event({"type": "response.cancel"}, "gemini", wire="native") == (
        None, 0, True)


def test_realtime_rejects_non_pcm_and_unrecognized_mutations():
    for invalid in ("", base64.b64encode(b"odd").decode(), "not base64"):
        with pytest.raises(realtime_transport.RealtimeTransportError):
            realtime_protocol._pcm(invalid)
    with pytest.raises(realtime_transport.RealtimeTransportError):
        realtime_protocol._client_event({"type": "session.update", "session": {"model": "other"}},
                                        "openai", wire="openai")
    accepted, size, closed = realtime_protocol._client_event({"type": "session.update", "session": {
        "type": "realtime", "instructions": "Speak clearly", "audio": {"output": {"voice": "alloy"}}}},
        "openai", wire="openai", voice="alloy")
    assert accepted == {"type": "session.update", "session": {
        "instructions": "Speak clearly", "audio": {"output": {"voice": "alloy"}}}}
    assert size == 0 and not closed
    with pytest.raises(realtime_transport.RealtimeTransportError):
        realtime_protocol._client_event({"type": "session.update", "session": {
            "audio": {"output": {"voice": "nova"}}}}, "openai", wire="openai", voice="alloy")
    with pytest.raises(realtime_transport.RealtimeTransportError):
        realtime_protocol._client_event({"type": "response.create", "response": {"model": "other"}},
                                        "openai", wire="openai", voice="alloy")

@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openai", "gemini"])
async def test_compatible_realtime_relays_vendor_events_with_measured_pcm(monkeypatch, provider):
    encoded = base64.b64encode(b"\0\1" * 160).decode()
    upstream = FakeUpstream(provider, encoded)

    class Connection:
        async def __aenter__(self):
            return upstream
        async def __aexit__(self, *_):
            return None

    monkeypatch.setattr(realtime_protocol.websockets, "connect", lambda *_args, **_kwargs: Connection())

    class Browser(FakeBrowser):
        def __init__(self):
            super().__init__(provider, encoded)
            self.queue = asyncio.Queue()
            self.queue.put_nowait(json.dumps({"type": "input_audio_buffer.append", "audio": encoded} if provider == "openai"
                else {"realtimeInput": {"audio": {"data": encoded, "mimeType": "audio/pcm;rate=16000"}}}))

        async def send_json(self, event):
            self.events.append(event)
            if event.get("type") == "response.output_audio.delta" or "serverContent" in event:
                await self.queue.put(json.dumps({"type": "session.close"}))

    async def not_canceled():
        return False

    browser = Browser()
    meter = await realtime_protocol.relay_audio(browser, _route(provider), session_id="session", voice=(
        "alloy" if provider == "openai" else "Kore"), instructions=None,
        max_duration_seconds=10, wire=provider, is_canceled=not_canceled)
    assert meter.input_bytes == 320 and meter.output_bytes == 320
    if provider == "openai":
        assert browser.events[0]["type"] == "response.output_audio.delta"
        assert upstream.sent[1]["type"] == "input_audio_buffer.append"
    else:
        assert browser.events[0]["serverContent"]["outputTranscription"]["text"] == "heard"
        assert upstream.sent[1]["realtimeInput"]["audio"]["mimeType"] == "audio/pcm;rate=16000"

@pytest.mark.asyncio
async def test_provider_audio_is_metered_when_browser_closes_during_send(monkeypatch):
    encoded = base64.b64encode(b"\0\1" * 160).decode()
    upstream = FakeUpstream("openai", encoded)

    class Connection:
        async def __aenter__(self):
            return upstream
        async def __aexit__(self, *_):
            return None

    monkeypatch.setattr(realtime_protocol.websockets, "connect", lambda *_args, **_kwargs: Connection())

    class Disconnected(FakeBrowser):
        async def send_json(self, event):
            if event.get("type") == "audio.output.delta":
                raise WebSocketDisconnect(code=1000)
            self.events.append(event)

    async def not_canceled():
        return False

    from fastapi import WebSocketDisconnect
    browser = Disconnected("openai", encoded)
    meter = await realtime_protocol.relay_audio(browser, _route(), session_id="session", voice="alloy",
        instructions=None, max_duration_seconds=10, is_canceled=not_canceled)
    assert meter.input_bytes == 320 and meter.output_bytes == 320


_RUN = SimpleNamespace(model_name="gpt-realtime")
_PCM_USAGE = {"input_bytes": 48000, "output_bytes": 96000, "input_sample_rate_hz": 24000,
              "output_sample_rate_hz": 24000}


@pytest.mark.parametrize(("unit", "scale"), [("second", 1), ("minute", 60), ("hour", 3600)])
def test_duration_rates_in_any_time_unit_reserve_and_settle_identically(unit, scale):
    route = {**_route(), "media_pricing": {
        f"realtime_input_per_{unit}": format(Decimal("0.0002") * scale, "f"),
        f"realtime_output_per_{unit}": format(Decimal("0.0004") * scale, "f")}}
    plan = realtime_transport.validate_realtime_request(route, max_duration_seconds=10)
    assert realtime._envelope_usd(plan, 10) == Decimal("0.006")
    cost, components, breakdown, summary = realtime._duration_bill(
        _RUN, {**plan, "max_duration_seconds": 10}, _PCM_USAGE)
    assert cost.raw_cost == Decimal("0.001") and breakdown is None
    assert summary == {"input_audio_seconds": "1", "output_audio_seconds": "2"}
    assert [(item["kind"], Decimal(item["cost_usd"])) for item in components] == [
        ("audio_input_seconds", Decimal("0.0002")), ("audio_output_seconds", Decimal("0.0008"))]


def test_conflicting_duration_aliases_are_not_executable():
    route = {**_route(), "media_pricing": {"realtime_input_per_minute": "0.012", "realtime_input_per_hour": "0.73",
                                           "realtime_output_per_minute": "0.024"}}
    assert not realtime_transport.realtime_route_ready(route)


def test_session_basis_bills_connected_time_only_within_reserved_envelope():
    route = {**_route(), "media_pricing": {"billing_basis": "session", "realtime_session_per_minute": "0.06",
                                           "realtime_input_per_minute": "100", "realtime_output_per_minute": "100"}}
    plan = realtime_transport.validate_realtime_request(route, max_duration_seconds=60)
    snapshot = {**plan, "max_duration_seconds": 60}
    # The funding envelope is exactly the admitted maximum, with no close-handshake padding.
    assert realtime._envelope_usd(plan, 60) == Decimal("0.06")
    usage = {**_PCM_USAGE, "connected_seconds": "12.345"}
    cost, components, _, summary = realtime._session_bill(_RUN, snapshot, usage)
    # PCM seconds and the per-direction duration rates are never added to a session bill.
    assert cost.raw_cost == Decimal("0.012345") and summary == {"session_seconds": "12.345"}
    assert [(item["kind"], item["quantity"]) for item in components] == [("realtime_session_seconds", "12.345")]
    for invalid in ("60.000000001", None, "NaN"):
        with pytest.raises(DurableRunProviderResultUnknown):
            realtime._session_bill(_RUN, snapshot, {**usage, "connected_seconds": invalid})


def _token_route(reservation="0.5"):
    return {**_route(), "input_price_per_token": Decimal("0.000004"), "output_price_per_token": Decimal("0.000016"),
            "cache_read_price_per_token": Decimal("0.0000004"), "media_pricing": {
                "billing_basis": "tokens", "reservation_usd": reservation, "token_rates": {"audio": {
                    "input_per_million": "32", "cache_read_per_million": "0.4", "output_per_million": "64"}}}}


def _openai_done(response_id, *, text_in, audio_in, cached_text, cached_audio, text_out, audio_out):
    return {"type": "response.done", "response": {"id": response_id, "status": "completed", "usage": {
        "total_tokens": text_in + audio_in + text_out + audio_out,
        "input_tokens": text_in + audio_in, "output_tokens": text_out + audio_out,
        "input_token_details": {"text_tokens": text_in, "audio_tokens": audio_in, "image_tokens": 0,
                                "cached_tokens": cached_text + cached_audio, "cached_tokens_details": {
                                    "text_tokens": cached_text, "audio_tokens": cached_audio, "image_tokens": 0}},
        "output_token_details": {"text_tokens": text_out, "audio_tokens": audio_out}}}}


def _token_bill(meter, route=None, *, input_bytes=4800, output_bytes=4800):
    usage, state = meter.result()
    plan = realtime_transport.validate_realtime_request(route or _token_route(), max_duration_seconds=60)
    checkpoint = realtime._checkpoint(AudioMeter(input_bytes, output_bytes, 24000, 24000, usage=usage,
                                                 usage_state=state), "tokens")
    # Settlement prices the durable JSON checkpoint, never the in-memory meter.
    return realtime._token_bill(_RUN, {**plan, "max_duration_seconds": 60}, json.loads(json.dumps(checkpoint)))


def _openai_meter(*messages):
    meter = TokenUsageMeter("openai")
    for message in messages:
        meter.observe(message)
    return meter


_FIRST = _openai_done("resp_1", text_in=200, audio_in=800, cached_text=100, cached_audio=300,
                      text_out=100, audio_out=400)
_SECOND = _openai_done("resp_2", text_in=100, audio_in=500, cached_text=0, cached_audio=0,
                       text_out=50, audio_out=250)


def test_token_basis_charges_deduplicated_responses_at_frozen_text_audio_and_cache_rates():
    meter = _openai_meter({"type": "response.created", "response": {"id": "resp_1"}}, _FIRST, _FIRST,
                          {"type": "response.created", "response": {"id": "resp_2"}}, _SECOND)
    cost, components, breakdown, summary = _token_bill(meter)
    # text: 200 uncached*4 + 100 cached*0.4 + 150 out*16; audio: 1000 uncached*32 + 300 cached*0.4 + 650 out*64 (per M)
    assert cost.raw_cost == Decimal("0.07696")
    assert (breakdown.input_tokens, breakdown.output_tokens) == (1600, 800)
    assert summary == {"input_tokens": "1600", "output_tokens": "800"}
    charged = {item["kind"]: (item["quantity"], Decimal(item["cost_usd"])) for item in components}
    assert charged["input_tokens"] == ("200", Decimal("0.0008"))
    assert charged["cache_read_input_tokens"] == ("100", Decimal("0.00004"))
    assert charged["output_tokens"] == ("150", Decimal("0.0024"))
    assert charged["audio_input_tokens"] == ("1000", Decimal("0.032"))
    assert charged["audio_cache_read_input_tokens"] == ("300", Decimal("0.00012"))
    assert charged["audio_output_tokens"] == ("650", Decimal("0.0416"))


def test_token_basis_requires_text_audio_rates_and_envelope_before_io():
    assert realtime_transport.realtime_route_ready(_token_route())
    audio_rates = _token_route()["media_pricing"]["token_rates"]["audio"]
    for change in ({"output_price_per_token": None}, {"input_price_per_token": None},
                   {"media_pricing": {**_token_route()["media_pricing"], "token_rates": {"audio": {
                       key: value for key, value in audio_rates.items() if key != "output_per_million"}}}},
                   {"media_pricing": {key: value for key, value in _token_route()["media_pricing"].items()
                                      if key != "reservation_usd"}}):
        assert not realtime_transport.realtime_route_ready({**_token_route(), **change})


def test_token_basis_holds_unknown_for_conflicting_missing_incomplete_or_over_envelope_usage():
    conflicting = _openai_meter(_FIRST, {**_FIRST, "response": {**_FIRST["response"], "usage": {
        **_FIRST["response"]["usage"], "output_tokens": 501}}})
    in_flight = _openai_meter(_FIRST, {"type": "response.created", "response": {"id": "resp_2"}})
    unreported = _openai_meter({"type": "response.done", "response": {"id": "resp_3", "status": "failed",
                                                                      "usage": None}})
    for meter in (conflicting, in_flight, unreported):
        with pytest.raises(DurableRunProviderResultUnknown):
            _token_bill(meter)
    with pytest.raises(DurableRunProviderResultUnknown, match="exceeds"):
        _token_bill(_openai_meter(_FIRST, _SECOND), _token_route(reservation="0.05"))
    # Provider audio with no provider usage is never billed as zero.
    with pytest.raises(DurableRunProviderResultUnknown, match="missing"):
        _token_bill(_openai_meter())
    assert _token_bill(_openai_meter(), input_bytes=0, output_bytes=0)[0].raw_cost == 0


def _gemini_usage(prompt_audio, prompt_text, response_audio, response_text, thoughts=0):
    return {"usageMetadata": {
        "promptTokenCount": prompt_audio + prompt_text, "responseTokenCount": response_audio + response_text,
        "thoughtsTokenCount": thoughts,
        "promptTokensDetails": [{"modality": "AUDIO", "tokenCount": prompt_audio},
                                {"modality": "TEXT", "tokenCount": prompt_text}],
        "responseTokensDetails": [{"modality": "AUDIO", "tokenCount": response_audio},
                                  {"modality": "TEXT", "tokenCount": response_text}]}}


def test_gemini_live_cumulative_snapshots_bill_the_latest_monotonic_total_once():
    turn = {"serverContent": {"modelTurn": {"parts": []}, "turnComplete": True}}
    meter = TokenUsageMeter("gemini")
    for message in (turn, _gemini_usage(80, 20, 50, 0), turn, _gemini_usage(250, 50, 180, 20, thoughts=10)):
        meter.observe(message)
    route = {**_token_route(), "provider_type": "gemini", "api_model_name": "gemini-2.5-flash-live"}
    cost, _, breakdown, _ = _token_bill(meter, route, input_bytes=3200)
    # 50 text in*4 + 30 text out (incl. thoughts)*16 + 250 audio in*32 + 180 audio out*64 (per M)
    assert (breakdown.input_tokens, breakdown.output_tokens) == (300, 210)
    assert cost.raw_cost == Decimal("0.0202")

    pending = TokenUsageMeter("gemini")
    for message in (_gemini_usage(80, 20, 50, 0), turn):
        pending.observe(message)
    decreasing = TokenUsageMeter("gemini")
    for message in (_gemini_usage(250, 50, 180, 20), _gemini_usage(200, 50, 180, 20)):
        decreasing.observe(message)
    assert pending.result()[1] == "incomplete" and decreasing.result()[1] == "invalid"
    assert (decreasing.result()[0].input_tokens, decreasing.result()[0].output_tokens) == (300, 200)


@pytest.mark.asyncio
async def test_token_relay_omits_separately_billed_transcription_and_drains_final_usage_after_close(monkeypatch):
    encoded = base64.b64encode(b"\0\1" * 160).decode()

    class TokenUpstream(FakeUpstream):
        async def send(self, message):
            parsed = json.loads(message)
            self.sent.append(parsed)
            if parsed.get("type") == "input_audio_buffer.append":
                await self.queue.put(json.dumps({"type": "response.created", "response": {"id": "resp_1"}}))
                await self.queue.put(json.dumps({"type": "response.output_audio.delta", "delta": encoded}))
            elif parsed.get("type") == "response.cancel":
                await self.queue.put(json.dumps(_FIRST))

    upstream = TokenUpstream("openai", encoded)

    class Connection:
        async def __aenter__(self):
            return upstream
        async def __aexit__(self, *_):
            return None

    monkeypatch.setattr(realtime_protocol.websockets, "connect", lambda *_args, **_kwargs: Connection())

    async def not_canceled():
        return False

    meter = await realtime_protocol.relay_audio(FakeBrowser("openai", encoded), _route(), session_id="session",
        voice="alloy", instructions=None, max_duration_seconds=10, is_canceled=not_canceled, token_usage=True)
    assert "transcription" not in upstream.sent[0]["session"]["audio"]["input"]
    assert {"type": "response.cancel"} in upstream.sent
    assert meter.usage_state == "complete" and meter.usage.output_tokens == 500
    assert meter.connected_seconds is not None and meter.connected_seconds >= 0
    with pytest.raises(realtime_transport.RealtimeTransportError):
        realtime_protocol._client_event({"type": "session.update", "session": {"audio": {"input": {
            "transcription": {"model": "gpt-4o-mini-transcribe"}}}}}, "openai", wire="openai",
            voice="alloy", allow_transcription=False)


@pytest.mark.asyncio
async def test_relay_failure_keeps_completed_usage_as_unsettleable_evidence(monkeypatch):
    encoded = base64.b64encode(b"\0\1" * 160).decode()

    class InterruptedUpstream(FakeUpstream):
        async def send(self, message):
            parsed = json.loads(message)
            self.sent.append(parsed)
            if parsed.get("type") == "input_audio_buffer.append":
                await self.queue.put(json.dumps(_FIRST))
                await self.queue.put(RuntimeError("secret-bearing provider failure"))

        async def recv(self):
            item = await self.queue.get()
            if isinstance(item, Exception):
                raise item
            return item

    upstream = InterruptedUpstream("openai", encoded)

    class Connection:
        async def __aenter__(self):
            return upstream
        async def __aexit__(self, *_):
            return None

    monkeypatch.setattr(realtime_protocol.websockets, "connect", lambda *_args, **_kwargs: Connection())

    async def not_canceled():
        return False

    with pytest.raises(realtime_transport.RealtimeTransportError) as failure:
        await realtime_protocol.relay_audio(FakeBrowser("openai", encoded), _route(), session_id="session",
            voice="alloy", instructions=None, max_duration_seconds=10, is_canceled=not_canceled, token_usage=True)
    partial = failure.value.meter
    assert "secret-bearing" not in str(failure.value)
    assert partial.usage_state == "incomplete"
    assert (partial.usage.input_tokens, partial.usage.output_tokens) == (1000, 500)
    assert partial.connected_seconds is not None
    with pytest.raises(DurableRunProviderResultUnknown, match="incomplete"):
        realtime._token_bill(_RUN, realtime_transport.validate_realtime_request(_token_route()),
                             realtime._checkpoint(partial, "tokens"))


def test_requested_response_without_provider_acknowledgement_keeps_usage_unknown():
    meter = TokenUsageMeter("openai")
    meter.request_sent({"type": "response.create"})
    assert meter.result() == (None, "incomplete")
    with pytest.raises(DurableRunProviderResultUnknown, match="incomplete"):
        _token_bill(meter, input_bytes=0, output_bytes=0)


def test_rejected_empty_response_request_does_not_create_unknown_usage():
    meter = TokenUsageMeter("openai")
    meter.request_sent({"type": "response.create"})
    meter.observe({"type": "error", "error": {"code": "response_create_empty"}})
    assert _token_bill(meter, input_bytes=0, output_bytes=0)[0].raw_cost == Decimal(0)
