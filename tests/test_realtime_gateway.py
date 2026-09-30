"""Realtime direct provider gate, ephemeral relay and exact PCM-duration boundaries."""

import asyncio
import base64
import json
from decimal import Decimal

import pytest

from lumen.services.providers import realtime_protocol, realtime_transport
from lumen.services.providers.errors import ProviderValidationError


def _route(provider="openai"):
    return {"model_kind": "realtime", "provider_type": provider,
            "api_model_name": "gpt-realtime" if provider == "openai" else "gemini-2.5-flash-live",
            "api_base": None, "api_key": "upstream-private-key", "provider_auth": None,
            "media_pricing": {"realtime_input_per_minute": "0.012", "realtime_output_per_minute": "0.024"}}


def test_realtime_route_requires_direct_keys_and_both_exact_prices():
    route = _route()
    assert realtime_transport.validate_realtime_request(route, voice="alloy") == (
        Decimal("0.012"), Decimal("0.024"))
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
