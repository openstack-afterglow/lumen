"""OpenAI realtime-family kinds: transcription, translation and GPT-Live sessions.

Each kind has a fixed upstream endpoint and setup frame, a strict client-event whitelist,
an input-only duration or connected-session billing plan, and is admitted only through
its own compatible WebSocket endpoint.
"""

import asyncio
import base64
import json
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from lumen.api import realtime as native_api
from lumen.api.compat import realtime as compat
from lumen.services.durable_runs import admission, execution, realtime
from lumen.services.durable_runs.errors import (
    DurableRunInputError,
    DurableRunProviderResultUnknown,
    RealtimeKindMismatch,
)
from lumen.services.providers import realtime_protocol, realtime_transport
from lumen.services.providers.errors import ProviderValidationError
from lumen.services.providers.realtime_protocol import AudioMeter
from lumen.services.providers.realtime_transport import RealtimeTransportError

_PCM = base64.b64encode(b"\0\1" * 160).decode()
_RUN = SimpleNamespace(model_name="kind-model")
_TRANSCRIBE = {"realtime_input_per_minute": "0.017"}
_TRANSLATE = {"realtime_input_per_minute": "0.034"}
_LIVE = {"billing_basis": "session", "realtime_session_per_minute": "0.05"}
_TOKENS = {"billing_basis": "tokens", "reservation_usd": "0.5", "token_rates": {"audio": {
    "input_per_million": "32", "output_per_million": "64"}}}


def _route(model, pricing):
    return {"model_kind": "realtime", "provider_type": "openai", "api_model_name": model, "api_base": None,
            "api_key": "upstream-private-key", "provider_auth": None, "media_pricing": pricing,
            "input_price_per_token": Decimal("0.000004"), "output_price_per_token": Decimal("0.000016")}


def test_each_kind_connects_to_its_own_upstream_endpoint():
    expected = {
        "gpt-realtime-2.1-mini": "wss://api.openai.com/v1/realtime?model=gpt-realtime-2.1-mini",
        "gpt-live-transcribe": "wss://api.openai.com/v1/realtime?intent=transcription",
        "gpt-realtime-whisper": "wss://api.openai.com/v1/realtime?intent=transcription",
        "gpt-realtime-translate": "wss://api.openai.com/v1/realtime/translations?model=gpt-realtime-translate",
        "gpt-live-1": "wss://api.openai.com/v1/live/sessions",
    }
    for model, url in expected.items():
        route = _route(model, _TRANSCRIBE)
        assert realtime_transport.upstream_connection_spec(route) == (
            url, {"Authorization": "Bearer " + route["api_key"]})


def test_new_conversation_models_are_ready_with_token_pricing():
    for model in ("gpt-realtime-1.5", "gpt-realtime-2", "gpt-realtime-2.1-mini"):
        route = _route(model, _TOKENS)
        assert realtime_transport.realtime_kind(route) == "conversation"
        assert realtime_transport.realtime_route_ready(route)
        plan = realtime_transport.validate_realtime_request(route, voice="cedar")
        assert plan["billing_basis"] == "tokens" and "realtime_kind" not in plan
        assert realtime_transport.available_realtime_options(route)["default_voice"] == "alloy"


def test_kind_billing_bases_are_restricted_before_any_io():
    for model in ("gpt-live-transcribe", "gpt-realtime-translate"):
        assert realtime_transport.realtime_route_ready(_route(model, _TRANSCRIBE))
        for pricing in (_TOKENS, _LIVE):
            assert not realtime_transport.realtime_route_ready(_route(model, pricing))
        # Input-priced kinds never need nor freeze an administrator output rate.
        plan = realtime_transport.validate_realtime_request(
            _route(model, {**_TRANSCRIBE, "realtime_output_per_minute": "7"}))
        assert set(plan["duration_rates"]) == {"realtime_input"}
    assert realtime_transport.realtime_route_ready(_route("gpt-live-1", _LIVE))
    for pricing in (_TOKENS, {"realtime_input_per_minute": "1", "realtime_output_per_minute": "1"}):
        assert not realtime_transport.realtime_route_ready(_route("gpt-live-1", pricing))


def test_input_priced_kinds_are_reported_priced_while_conversation_still_needs_both_directions():
    from lumen.services.providers import pricing

    def priced(model, media):
        route = {**_route(model, media), "model_kind": "realtime"}
        return pricing.route_media_pricing_available(route)

    # Transcription and translation settle on input audio only; one input rate is a complete price.
    assert priced("gpt-live-transcribe", _TRANSCRIBE) and priced("gpt-realtime-translate", _TRANSLATE)
    # A conversation model on a duration basis still needs separately priced input and output.
    assert not priced("gpt-realtime", _TRANSCRIBE)
    assert priced("gpt-realtime", {**_TRANSCRIBE, "realtime_output_per_minute": "0.2"})
    assert priced("gpt-live-1", _LIVE)
    assert not priced("gpt-live-transcribe", {"realtime_output_per_minute": "0.2"})


def test_kind_options_and_voices():
    transcription = realtime_transport.available_realtime_options(_route("gpt-live-transcribe", _TRANSCRIBE))
    assert (transcription["available_voices"], transcription["default_voice"],
            transcription["input_sample_rate_hz"], transcription["output_sample_rate_hz"]) == ([], None, 24000, 0)
    translation = realtime_transport.available_realtime_options(_route("gpt-realtime-translate", _TRANSLATE))
    assert (translation["available_voices"], translation["default_voice"],
            translation["output_sample_rate_hz"]) == ([], None, 24000)
    live = realtime_transport.available_realtime_options(_route("gpt-live-1", _LIVE))
    assert live["default_voice"] == "marin" and {"quartz", "noeul", "marin", "cedar", "alloy"} <= set(
        live["available_voices"])
    realtime_transport.validate_realtime_request(_route("gpt-live-1", _LIVE), voice="quartz")
    with pytest.raises(ProviderValidationError):
        realtime_transport.validate_realtime_request(_route("gpt-realtime", _TRANSCRIBE | {
            "realtime_output_per_minute": "1"}), voice="quartz")
    for change in ({"voice": "alloy"}, {"instructions": "be brief"}):
        with pytest.raises(ProviderValidationError):
            realtime_transport.validate_realtime_request(_route("gpt-live-transcribe", _TRANSCRIBE), **change)


def test_input_only_duration_plans_reserve_and_settle_input_audio_seconds():
    route = _route("gpt-live-transcribe", {**_TRANSCRIBE, "realtime_output_per_minute": "5"})
    plan = realtime_transport.validate_realtime_request(route, max_duration_seconds=300)
    snapshot = {**plan, "max_duration_seconds": 300}
    # 300 s at $0.017/min; an output rate is neither reserved nor charged.
    assert realtime._envelope_usd(plan, 300) == Decimal("0.085")
    assert realtime._plan_matches(snapshot, realtime_transport.validate_realtime_request(route))
    assert not realtime._plan_matches(snapshot, realtime_transport.validate_realtime_request(
        _route("gpt-realtime", {**_TRANSCRIBE, "realtime_output_per_minute": "5"})))
    checkpoint = json.loads(json.dumps(realtime._checkpoint(AudioMeter(2 * 24000 * 90, 96000, 24000, 24000),
                                                            "duration")))
    cost, components, breakdown, summary = realtime._duration_bill(_RUN, snapshot, checkpoint)
    assert (cost.raw_cost, cost.input_cost, cost.output_cost, breakdown) == (
        Decimal("0.0255"), Decimal("0.0255"), Decimal(0), None)
    assert summary == {"input_audio_seconds": "90"}
    assert [(item["kind"], item["quantity"], Decimal(item["cost_usd"])) for item in components] == [
        ("audio_input_seconds", "90", Decimal("0.0255"))]
    with pytest.raises(DurableRunProviderResultUnknown):
        realtime._duration_bill(_RUN, snapshot, {**checkpoint, "input_bytes": 2 * 24000 * 300 + 2})
    # Translation: 4 s speech + 1 s silence at $0.034/min.
    translation = {**realtime_transport.validate_realtime_request(_route("gpt-realtime-translate", _TRANSLATE)),
                   "max_duration_seconds": 300}
    cost, _, _, _ = realtime._duration_bill(_RUN, translation, {**checkpoint, "input_bytes": 240000})
    assert cost.raw_cost == Decimal("0.0028333333")


def test_live_bills_connected_session_seconds():
    plan = realtime_transport.validate_realtime_request(_route("gpt-live-1", _LIVE), max_duration_seconds=300)
    assert plan["billing_basis"] == "session" and plan["realtime_kind"] == "live"
    assert realtime._envelope_usd(plan, 300) == Decimal("0.25")
    cost, components, _, summary = realtime._session_bill(_RUN, {**plan, "max_duration_seconds": 300},
                                                          {"connected_seconds": "12.5"})
    assert cost.raw_cost == Decimal("0.0104166667") and summary == {"session_seconds": "12.5"}
    assert components[0]["kind"] == "realtime_session_seconds"


def test_transcription_client_session_is_strict():
    model, options = realtime_protocol.transcription_session({"type": "transcription", "audio": {"input": {
        "format": {"type": "audio/pcm", "rate": 24000}, "turn_detection": None, "transcription": {
            "model": "gpt-live-transcribe", "prompt": "names", "keywords": ["Lumen"],
            "languages": ["en", "zh-hant"], "delay": "low"}}}})
    assert model == "gpt-live-transcribe"
    assert options == {"prompt": "names", "keywords": ["Lumen"], "languages": ["en", "zh-hant"], "delay": "low"}
    base = {"model": "gpt-live-transcribe"}
    for audio_input in ({"transcription": base, "turn_detection": {"type": "server_vad"}},
                        {"transcription": base, "format": {"type": "audio/pcm", "rate": 16000}},
                        {"transcription": {**base, "keywords": ["<b>"]}},
                        {"transcription": {**base, "keywords": ["a\nb"]}},
                        {"transcription": {**base, "languages": ["english"]}},
                        {"transcription": {**base, "languages": ["en"] * 9}},
                        {"transcription": {**base, "delay": "instant"}},
                        {"transcription": {**base, "prompt": "x" * 2049}},
                        {"transcription": base, "noise_reduction": None}):
        with pytest.raises(RealtimeTransportError):
            realtime_protocol.transcription_session({"type": "transcription", "audio": {"input": audio_input}})
    with pytest.raises(RealtimeTransportError):
        realtime_protocol.transcription_session({"type": "realtime", "audio": {"input": {"transcription": base}}})


def test_kind_client_event_whitelists():
    def event(message, kind, model="gpt-live-transcribe", wire="openai"):
        return realtime_protocol._client_event(message, "openai", wire=wire, kind=kind, model=model)

    assert event({"type": "input_audio_buffer.append", "audio": _PCM, "extra": 1}, "transcription") == (
        {"type": "input_audio_buffer.append", "audio": _PCM}, 320, False)
    assert event({"type": "input_audio_buffer.commit"}, "transcription") == (
        {"type": "input_audio_buffer.commit"}, 0, False)
    assert event({"type": "session.close"}, "transcription") == (None, 0, True)
    other_model = {"type": "session.update", "session": {"type": "transcription", "audio": {"input": {
        "transcription": {"model": "gpt-realtime-whisper"}}}}}
    assert event({"type": "session.update", "session": {"type": "transcription", "audio": {"input": {
        "transcription": {"model": "gpt-live-transcribe", "delay": "high"}}}}}, "transcription")[0] == (
        realtime_protocol.transcription_update("gpt-live-transcribe", {"delay": "high"}))
    assert event({"type": "session.update", "session": {"audio": {"output": {"language": "es-419"}}}},
                 "translation")[0] == {"type": "session.update", "session": {"audio": {"output": {
                     "language": "es-419"}}}}
    assert event({"type": "session.input_audio_buffer.append", "audio": _PCM}, "translation")[1] == 320
    assert event({"type": "session.instructions.append", "instructions": "slower"}, "live")[0] == {
        "type": "session.instructions.append", "instructions": "slower"}
    assert event({"type": "session.input_audio.mute"}, "live") == ({"type": "session.input_audio.mute"}, 0, False)
    rejected = [
        (other_model, "transcription"),
        ({"type": "response.create"}, "transcription"),
        ({"type": "input_audio_buffer.append", "audio": base64.b64encode(b"odd").decode()}, "transcription"),
        ({"type": "input_audio_buffer.append", "audio": _PCM}, "translation"),
        ({"type": "session.update", "session": {"audio": {"output": {"language": "English"}}}}, "translation"),
        ({"type": "session.update", "session": {"audio": {"output": {"language": "es", "voice": "x"}}}},
         "translation"),
        ({"type": "session.update", "session": {"instructions": "x"}}, "live"),
        ({"type": "session.start", "session": {"model": "gpt-live-1"}}, "live"),
        ({"type": "session.instructions.append", "instructions": "x" * 4097}, "live"),
    ]
    for message, kind in rejected:
        with pytest.raises(RealtimeTransportError):
            event(message, kind)
    with pytest.raises(RealtimeTransportError):
        event({"type": "audio.input.append", "audio": _PCM}, "live", wire="native")


def test_live_session_start_refuses_delegation_storage_history_and_formats():
    start = {"type": "session.start", "event_id": "evt_1", "session": {
        "model": "gpt-live-1", "instructions": "be kind", "delegation": None, "store": False,
        "audio": {"format": {"type": "audio/pcm", "rate": 24000}, "output": {"voice": "quartz"}}}}
    assert realtime_protocol.live_session_start(start) == ("gpt-live-1", "be kind", "quartz")
    for change in ({"delegation": {"type": "responses"}}, {"store": True}, {"input": []}, {"tools": []},
                   {"audio": {"format": {"type": "audio/pcmu", "rate": 8000}}},
                   {"audio": {"format": {"type": "audio/pcm", "rate": 16000}}},
                   {"audio": {"output": {"voice": "quartz", "speed": 2}}}, {"model": None}):
        with pytest.raises(RealtimeTransportError):
            realtime_protocol.live_session_start({**start, "session": {**start["session"], **change}})
    with pytest.raises(RealtimeTransportError):
        realtime_protocol.live_session_start({**start, "type": "session.update"})


def test_kind_provider_errors_are_sanitized_and_output_audio_is_counted():
    error = {"type": "error", "error": {"message": "secret provider detail", "param": "sk-upstream"}}
    failed = {"type": "conversation.item.input_audio_transcription.failed", "item_id": "item_1",
              "content_index": 0, "error": {"message": "secret provider detail"}}
    for kind in ("transcription", "translation", "live"):
        assert realtime_protocol._provider_events(error, "openai", wire="openai", kind=kind) == (
            [{"type": "error", "error": {"type": "server_error", "message": "Realtime provider returned an error"}}], 0)
    events, _ = realtime_protocol._provider_events(failed, "openai", wire="openai", kind="transcription")
    assert events == [{"type": failed["type"], "item_id": "item_1", "content_index": 0,
                       "error": {"type": "server_error", "message": "Realtime provider returned an error"}}]
    delta = {"type": "session.output_audio.delta", "delta": _PCM}
    assert realtime_protocol._provider_events(delta, "openai", wire="openai", kind="live") == ([delta], 320)


class ScriptedUpstream:
    def __init__(self, react):
        self.react = react
        self.sent = []
        self.queue = asyncio.Queue()
        self.transport = SimpleNamespace(abort=lambda: None)

    async def send(self, message):
        parsed = json.loads(message)
        self.sent.append(parsed)
        for event in self.react(parsed):
            await self.queue.put(json.dumps(event))

    async def recv(self):
        return await self.queue.get()


class ScriptedBrowser:
    def __init__(self, frames, respond=lambda _event, _events: ()):
        self.queue = asyncio.Queue()
        for frame in frames:
            self.queue.put_nowait(json.dumps(frame))
        self.respond = respond
        self.events = []

    async def receive_text(self):
        return await self.queue.get()

    async def send_json(self, event):
        self.events.append(event)
        for frame in self.respond(event, self.events):
            await self.queue.put(json.dumps(frame))


def _connect(monkeypatch, upstream):
    captured = {}

    class Connection:
        async def __aenter__(self):
            return upstream

        async def __aexit__(self, *_):
            return None

    def connect(url, **kwargs):
        captured.update(url=url, **kwargs)
        return Connection()

    monkeypatch.setattr(realtime_protocol.websockets, "connect", connect)
    return captured


async def _not_canceled():
    return False


@pytest.mark.asyncio
async def test_transcription_relay_configures_admitted_model_and_never_forwards_close(monkeypatch):
    def react(sent):
        if sent["type"] == "input_audio_buffer.commit":
            return ({"type": "input_audio_buffer.committed", "item_id": "item_1"},
                    {"type": "conversation.item.input_audio_transcription.delta", "item_id": "item_1", "delta": "he"},
                    {"type": "conversation.item.input_audio_transcription.completed", "item_id": "item_1",
                     "transcript": "hello", "usage": {"type": "duration", "seconds": 3}},
                    {"type": "conversation.item.input_audio_transcription.failed", "item_id": "item_2",
                     "error": {"message": "provider secret detail"}})
        return ()

    upstream = ScriptedUpstream(react)
    captured = _connect(monkeypatch, upstream)
    browser = ScriptedBrowser([{"type": "input_audio_buffer.append", "audio": _PCM},
                               {"type": "input_audio_buffer.commit"}],
                              lambda event, _: [{"type": "session.close"}] if event["type"].endswith(".failed") else [])
    options = {"languages": ["en"], "delay": "low"}
    meter = await realtime_protocol.relay_audio(browser, _route("gpt-live-transcribe", _TRANSCRIBE),
        session_id="session", voice=None, instructions=None, max_duration_seconds=30, wire="openai",
        is_canceled=_not_canceled, transcription=options)
    assert captured["url"] == "wss://api.openai.com/v1/realtime?intent=transcription"
    assert upstream.sent[0] == {"type": "session.update", "session": {"type": "transcription", "audio": {"input": {
        "format": {"type": "audio/pcm", "rate": 24000},
        "transcription": {"model": "gpt-live-transcribe", "languages": ["en"], "delay": "low"},
        "turn_detection": None}}}}
    assert [item["type"] for item in upstream.sent[1:]] == ["input_audio_buffer.append", "input_audio_buffer.commit"]
    assert any(event.get("transcript") == "hello" for event in browser.events)
    assert "provider secret detail" not in repr(browser.events)
    assert (meter.input_bytes, meter.output_bytes) == (320, 0)


@pytest.mark.asyncio
async def test_translation_close_drains_translated_audio_until_session_closed(monkeypatch):
    def react(sent):
        if sent["type"] == "session.update":
            return ({"type": "session.updated", "session": {"type": "translation"}},)
        if sent["type"] == "session.input_audio_buffer.append":
            return ({"type": "session.output_audio.delta", "delta": _PCM},)
        if sent["type"] == "session.close":
            return ({"type": "session.output_audio.delta", "delta": _PCM},
                    {"type": "session.output_transcript.delta", "delta": "La linterna"},
                    {"type": "session.closed"})
        return ()

    upstream = ScriptedUpstream(react)
    captured = _connect(monkeypatch, upstream)

    def respond(event, events):
        audio = [item for item in events if item["type"] == "session.output_audio.delta"]
        return [{"type": "session.close"}] if event["type"] == "session.output_audio.delta" and len(audio) == 1 else []

    browser = ScriptedBrowser([{"type": "session.update", "session": {"audio": {"output": {"language": "es"}}}},
                               {"type": "session.input_audio_buffer.append", "audio": _PCM}], respond)
    meter = await realtime_protocol.relay_audio(browser, _route("gpt-realtime-translate", _TRANSLATE),
        session_id="session", voice=None, instructions=None, max_duration_seconds=30, wire="openai",
        is_canceled=_not_canceled)
    assert captured["url"] == "wss://api.openai.com/v1/realtime/translations?model=gpt-realtime-translate"
    # Lumen sends nothing proactively; the first upstream frame is the client's language update.
    assert [item["type"] for item in upstream.sent] == [
        "session.update", "session.input_audio_buffer.append", "session.close"]
    assert [event["type"] for event in browser.events] == [
        "session.updated", "session.output_audio.delta", "session.output_audio.delta",
        "session.output_transcript.delta", "session.closed"]
    assert (meter.input_bytes, meter.output_bytes) == (320, 640)


@pytest.mark.asyncio
async def test_live_session_starts_from_admitted_values_and_drains_to_session_closed(monkeypatch):
    def react(sent):
        if sent["type"] == "session.start":
            return ({"type": "session.started", "session": {"model": "gpt-live-1"}},)
        if sent["type"] == "session.input_audio.append":
            return ({"type": "session.output_audio.delta", "delta": _PCM},)
        if sent["type"] == "session.close":
            return ({"type": "session.output_audio.delta", "delta": _PCM},
                    {"type": "session.closed", "reason": "client_closed", "usage": {"seconds": 8.0}})
        return ()

    upstream = ScriptedUpstream(react)
    captured = _connect(monkeypatch, upstream)

    def respond(event, events):
        audio = [item for item in events if item["type"] == "session.output_audio.delta"]
        return [{"type": "session.close"}] if event["type"] == "session.output_audio.delta" and len(audio) == 1 else []

    browser = ScriptedBrowser([{"type": "session.instructions.append", "instructions": "slower"},
                               {"type": "session.input_audio.append", "audio": _PCM}], respond)
    meter = await realtime_protocol.relay_audio(browser, _route("gpt-live-1", _LIVE), session_id="session",
        voice="quartz", instructions="be kind", max_duration_seconds=30, wire="openai",
        is_canceled=_not_canceled, session_time=True)
    assert captured["url"] == "wss://api.openai.com/v1/live/sessions"
    start = upstream.sent[0]
    assert start["type"] == "session.start" and start["event_id"].startswith("lumen_")
    assert start["session"] == {"model": "gpt-live-1", "instructions": "be kind", "audio": {
        "format": {"type": "audio/pcm", "rate": 24000}, "output": {"voice": "quartz"}}}
    assert [item["type"] for item in upstream.sent[1:]] == [
        "session.instructions.append", "session.input_audio.append", "session.close"]
    assert browser.events[-1] == {"type": "session.closed", "reason": "client_closed", "usage": {"seconds": 8.0}}
    assert (meter.input_bytes, meter.output_bytes) == (320, 640)
    assert meter.connected_seconds is not None


@pytest.mark.asyncio
async def test_close_drain_is_bounded_when_session_closed_never_arrives(monkeypatch):
    monkeypatch.setattr(realtime_protocol, "_USAGE_DRAIN_SECONDS", 0.2)
    upstream = ScriptedUpstream(lambda _sent: ())
    _connect(monkeypatch, upstream)
    browser = ScriptedBrowser([{"type": "session.close"}])
    meter = await asyncio.wait_for(realtime_protocol.relay_audio(browser, _route("gpt-live-1", _LIVE),
        session_id="session", voice="marin", instructions=None, max_duration_seconds=30, wire="openai",
        is_canceled=_not_canceled, session_time=True), timeout=5)
    assert upstream.sent[-1] == {"type": "session.close"} and meter.output_bytes == 0


@pytest.mark.asyncio
async def test_kind_models_never_run_on_the_native_wire(monkeypatch):
    _connect(monkeypatch, ScriptedUpstream(lambda _sent: ()))
    with pytest.raises(RealtimeTransportError):
        await realtime_protocol.relay_audio(ScriptedBrowser([]), _route("gpt-live-1", _LIVE), session_id="session",
            voice="marin", instructions=None, max_duration_seconds=30, is_canceled=_not_canceled)


def _routing(monkeypatch, route):
    monkeypatch.setattr(admission, "existing_run_for_intent", AsyncMock(return_value=None))
    monkeypatch.setattr(realtime.routing, "resolve_api_model", AsyncMock(return_value={
        **route, "provider_id": 1, "api_provider": "openai"}))
    precheck = AsyncMock(side_effect=AssertionError("kind mismatch must fail before credit or storage"))
    monkeypatch.setattr(realtime.credit, "precheck", precheck)


@pytest.mark.asyncio
async def test_native_and_plain_endpoints_reject_other_kinds_before_reservation(monkeypatch):
    _routing(monkeypatch, _route("gpt-live-1", _LIVE))
    for kind in ("conversation", "transcription", "translation"):
        with pytest.raises(DurableRunInputError, match="live models are not available") as failure:
            await realtime.admit_realtime_session({"model_id": "gpt-live-1"}, project_id="p", user_id="u",
                                                  client_request_id=str(uuid4()), kind=kind)
    # The native session endpoint maps this admission refusal to HTTP 422.
    assert native_api._error(failure.value).status_code == 422
    _routing(monkeypatch, _route("gpt-live-transcribe", _TRANSCRIBE))
    with pytest.raises(DurableRunInputError, match="transcription options"):
        await realtime.admit_realtime_session({"model_id": "gpt-live-transcribe", "transcription": {
            "delay": "instant"}}, project_id="p", user_id="u", client_request_id=str(uuid4()),
            source="api", kind="transcription")


def test_conversation_intents_keep_their_fingerprint():
    request = {"model_id": "gpt-realtime", "voice": "alloy", "instructions": None}
    legacy = {"model_id": "gpt-realtime", "provider_id": None, "voice": "alloy", "instructions": None,
              "max_duration_seconds": 300}
    assert realtime._fingerprint(realtime._intent(request)) == realtime._fingerprint(legacy)
    assert realtime._fingerprint(realtime._intent({**request, "voice": None, "transcription": {}},
                                                  "transcription")) != realtime._fingerprint(legacy)


@pytest.mark.asyncio
async def test_native_wire_cannot_run_a_kind_session_admitted_elsewhere(monkeypatch):
    route = _route("gpt-live-1", _LIVE)
    pricing = {**realtime_transport.validate_realtime_request(route, voice="marin", max_duration_seconds=60),
               "max_duration_seconds": 60}
    monkeypatch.setattr(realtime, "consume_ticket", AsyncMock(return_value=("u", "p")))
    monkeypatch.setattr(realtime, "_start", AsyncMock(return_value=(
        {}, pricing, {"voice": "marin", "instructions": None, "realtime_kind": "live"}, "owner")))
    monkeypatch.setattr(realtime.routing, "resolve_model_snapshot", AsyncMock(return_value=route))
    finish = AsyncMock()
    monkeypatch.setattr(execution, "_finish", finish)
    monkeypatch.setattr(realtime_protocol, "relay_audio", AsyncMock(side_effect=AssertionError("no provider I/O")))
    with pytest.raises(DurableRunInputError, match="compatible wire"):
        await realtime.run_realtime_session(object(), run_id="run", token="t" * 40, wire="native")
    assert finish.await_args.kwargs["status"] == "failed"


def _compat_app(monkeypatch, admission_mock):
    app = FastAPI()
    app.include_router(compat.router)
    monkeypatch.setattr(compat.api_key_store, "verify_key", AsyncMock(return_value={
        "user_id": "owner", "project_id": "project", "api_key_id": 37, "scopes": ["compat:realtime:write"],
        "roles": ["member", "lumen-audio_user"], "service_system_admin": False}))
    monkeypatch.setattr(compat, "get_settings", lambda: SimpleNamespace(chat_api_hosts="testserver"))
    monkeypatch.setattr(compat, "origin_allowed", lambda _websocket: True)
    monkeypatch.setattr(compat, "admit_realtime_session", admission_mock)

    async def connected(websocket, **kwargs):
        await websocket.send_json({"type": "connected", "wire": kwargs["wire"]})
    monkeypatch.setattr(compat, "run_realtime_session", connected)
    return app


def _closed_with(socket, code):
    with pytest.raises(WebSocketDisconnect) as closed:
        socket.receive_json()
    assert closed.value.code == code


def test_compat_endpoints_discover_and_validate_kind_setup(monkeypatch):
    admission_mock = AsyncMock(return_value={"session_id": str(uuid4()), "connect_token": "one-use-token"})
    app = _compat_app(monkeypatch, admission_mock)
    headers = {"Authorization": "Bearer scoped-key"}
    session = {"type": "transcription", "audio": {"input": {"transcription": {
        "model": "gpt-live-transcribe", "languages": ["ko"]}, "turn_detection": None}}}
    with TestClient(app) as client:
        with client.websocket_connect("/v1/realtime?intent=transcription", headers=headers) as socket:
            socket.send_json({"type": "session.update", "session": session})
            assert socket.receive_json() == {"type": "connected", "wire": "openai"}
        assert admission_mock.call_args.args[0] == {"model_id": "gpt-live-transcribe", "voice": None,
                                                    "instructions": None, "transcription": {"languages": ["ko"]}}
        assert admission_mock.call_args.kwargs["kind"] == "transcription"
        assert admission_mock.call_args.kwargs["provider_type"] == "openai"

        with client.websocket_connect("/v1/realtime/translations?model=gpt-realtime-translate",
                                      headers=headers) as socket:
            assert socket.receive_json() == {"type": "connected", "wire": "openai"}
        assert admission_mock.call_args.args[0] == {"model_id": "gpt-realtime-translate", "voice": None,
                                                    "instructions": None}
        assert admission_mock.call_args.kwargs["kind"] == "translation"

        with client.websocket_connect("/v1/live/sessions", headers=headers) as socket:
            socket.send_json({"type": "session.start", "session": {"model": "gpt-live-1", "instructions": "hi",
                              "audio": {"output": {"voice": "quartz"}}}})
            assert socket.receive_json() == {"type": "connected", "wire": "openai"}
        assert admission_mock.call_args.args[0] == {"model_id": "gpt-live-1", "voice": "quartz", "instructions": "hi"}
        assert admission_mock.call_args.kwargs["kind"] == "live"

        calls = admission_mock.await_count
        rejected = [
            ("/v1/realtime?intent=transcription", {"type": "session.update", "session": {
                **session, "audio": {"input": {**session["audio"]["input"],
                                               "turn_detection": {"type": "server_vad"}}}}}),
            ("/v1/realtime?intent=transcription&model=gpt-realtime-whisper", {"type": "session.update",
                                                                              "session": session}),
            ("/v1/realtime?intent=translation", None),
            ("/v1/realtime/translations?model=gpt-realtime-translate&voice=alloy", None),
            ("/v1/live/sessions", {"type": "session.start", "session": {"model": "gpt-live-1",
                                                                        "delegation": {"type": "responses"}}}),
            ("/v1/live/sessions", {"type": "session.start", "session": {"model": "gpt-live-1", "store": True}}),
            ("/v1/live/sessions", {"type": "session.start", "session": {"model": "gpt-live-1", "input": []}}),
            ("/v1/live/sessions", {"type": "session.update", "session": {"model": "gpt-live-1"}}),
        ]
        for path, frame in rejected:
            with client.websocket_connect(path, headers=headers) as socket:
                if frame is not None:
                    socket.send_json(frame)
                _closed_with(socket, 4400)
        assert admission_mock.await_count == calls

        # A transcription/translation/live model on plain /v1/realtime fails admission's kind check.
        admission_mock.side_effect = RealtimeKindMismatch("realtime live models are not available on this endpoint")
        with client.websocket_connect("/v1/realtime?model=gpt-live-1", headers=headers) as socket:
            _closed_with(socket, 4400)
        assert admission_mock.call_args.kwargs["kind"] == "conversation"

        # Every other admission failure keeps the established generic close, so a model of another
        # transport or an unavailable route is not distinguishable from an internal error.
        for failure in (DurableRunInputError("realtime provider or model is unavailable"),
                        ProviderValidationError("unsupported realtime voice")):
            admission_mock.side_effect = failure
            with client.websocket_connect("/v1/realtime?model=gpt-realtime", headers=headers) as socket:
                _closed_with(socket, 1011)


@pytest.mark.asyncio
async def test_native_capabilities_advertise_options_only_for_conversation_models(monkeypatch):
    from lumen.api import conversations

    gates = {"feature_gates": {name: {"available": True} for name in ("audio_input", "audio_output")}}
    monkeypatch.setattr(conversations.capabilities, "runtime_capabilities", lambda: {})
    monkeypatch.setattr(conversations.capabilities, "effective_runtime_capabilities", lambda *_args: {})
    for model, pricing, voice in (("gpt-realtime-2", _TRANSCRIBE | {"realtime_output_per_minute": "1"}, "alloy"),
                                  ("gpt-live-1", _LIVE, None), ("gpt-live-transcribe", _TRANSCRIBE, None)):
        resolved = {**_route(model, pricing), "model_name": model, "capabilities": gates}
        monkeypatch.setattr(conversations.routing, "resolve_model_by_id", AsyncMock(return_value=resolved))
        result = await conversations.get_chat_capabilities(model_id=1, model_kind="realtime", token_info={})
        assert result["default_voice"] == voice
        assert (result["max_duration_seconds"] > 0) is (voice is not None)


def _transcription(model, **options):
    return realtime_protocol.transcription_session({"type": "transcription", "audio": {"input": {
        "transcription": {"model": model, **options}, "turn_detection": None}}})


def test_transcription_options_follow_what_each_model_accepts():
    # Probed against the provider: gpt-realtime-whisper takes only language/delay and answers the
    # others with a session error after the upstream session exists; gpt-live-transcribe takes all.
    assert _transcription("gpt-realtime-whisper", language="en", delay="low") == (
        "gpt-realtime-whisper", {"language": "en", "delay": "low"})
    for option in ({"languages": ["en"]}, {"prompt": "names"}, {"keywords": ["lantern"]}):
        with pytest.raises(realtime_transport.RealtimeTransportError):
            _transcription("gpt-realtime-whisper", **option)
    assert _transcription("gpt-live-transcribe", prompt="p", keywords=["k"], languages=["en", "ko"], delay="high")[1] == {
        "prompt": "p", "keywords": ["k"], "languages": ["en", "ko"], "delay": "high"}
    assert _transcription("gpt-live-transcribe", language="ko")[1] == {"language": "ko"}
    with pytest.raises(realtime_transport.RealtimeTransportError):
        _transcription("gpt-live-transcribe", language="en", languages=["en"])
    with pytest.raises(realtime_transport.RealtimeTransportError):
        _transcription("gpt-live-transcribe", language="english!")
