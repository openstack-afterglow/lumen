"""OpenAI Realtime conversation image input, function calling and their token billing bounds.

Only token-billed GPT-Realtime 2.1/2.1-mini/2/1.5 sessions accept images (PNG/JPEG data
URLs, header-checked, size-bounded) and client function tools (no hosted/MCP tools).
Both settle as reported modality tokens under the pinned per-response worst case.
"""

import asyncio
import base64
import json
import struct
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lumen.api import realtime as native_api
from lumen.auth import get_principal
from lumen.services.durable_runs import admission, realtime
from lumen.services.providers import realtime_protocol, realtime_transport
from lumen.services.providers.errors import ProviderValidationError
from lumen.services.providers.realtime_protocol import AudioMeter, TokenUsageMeter
from lumen.services.providers.realtime_transport import RealtimeTransportError

_PCM = base64.b64encode(b"\0\1" * 160).decode()
_RUN = SimpleNamespace(model_name="gpt-realtime-2.1")
_TOKEN_MEDIA = {"billing_basis": "tokens", "token_rates": {
    "audio": {"input_per_million": "32", "cache_read_per_million": "0.4", "output_per_million": "64"},
    "image": {"input_per_million": "5", "cache_read_per_million": "0.5"}}}
_TOOL = {"type": "function", "name": "lookup_order", "description": "Find an order",
         "parameters": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}}


def _route(model="gpt-realtime-2.1", media=None, provider="openai"):
    return {"model_kind": "realtime", "provider_type": provider, "api_model_name": model, "api_base": None,
            "api_key": "upstream-private-key", "provider_auth": None,
            "media_pricing": _TOKEN_MEDIA if media is None else media,
            "input_price_per_token": Decimal("0.000004"), "output_price_per_token": Decimal("0.000024"),
            "cache_read_price_per_token": Decimal("0.0000004")}


def _png(width=640, height=480):
    return (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", width, height)
            + b"\x08\x02\x00\x00\x00" + b"\0" * 16)


def _jpeg(width=800, height=600):
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof0 = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, height, width, 1) + b"\x01\x11\x00"
    return b"\xff\xd8" + app0 + sof0 + b"\0" * 16


def _data_url(raw, mime="image/png"):
    return f"data:{mime};base64," + base64.b64encode(raw).decode()


def test_features_are_published_per_model_and_require_token_billing():
    for model in ("gpt-realtime-2.1", "gpt-realtime-2.1-mini", "gpt-realtime-2", "gpt-realtime-1.5"):
        assert realtime_transport.conversation_features(_route(model)) == {
            "image_input": True, "function_calling": True}
        options = realtime_transport.available_realtime_options(_route(model))
        assert options["realtime_image_input"] is True and options["realtime_function_calling"] is True
        assert {"tools", "tool_choice"} <= set(options["realtime_session_fields"])
    # Not in the published feature list, duration-billed, or missing an image rate.
    off = {"image_input": False, "function_calling": False}
    assert realtime_transport.conversation_features(_route("gpt-realtime")) == off
    assert realtime_transport.conversation_features(_route(media={
        "realtime_input_per_minute": "0.1", "realtime_output_per_minute": "0.2"})) == off
    no_image = {**_TOKEN_MEDIA, "token_rates": {"audio": _TOKEN_MEDIA["token_rates"]["audio"]}}
    assert realtime_transport.conversation_features(_route(media={**no_image, "reservation_usd": "5"})) == {
        "image_input": False, "function_calling": True}
    options = realtime_transport.available_realtime_options(_route("gpt-realtime"))
    assert options["realtime_image_input"] is False and "tools" not in options["realtime_session_fields"]


def test_function_tools_are_bounded_client_functions_only():
    tools, choice = realtime_transport.function_tools([_TOOL], {"type": "function", "name": "lookup_order"})
    assert tools == [_TOOL] and choice == {"type": "function", "name": "lookup_order"}
    for choice in ("auto", "none", "required", None):
        realtime_transport.function_tools([_TOOL], choice)
    many = [{**_TOOL, "name": f"tool_{index}"} for index in range(17)]
    oversized = [{**_TOOL, "description": "x" * 1000, "name": f"t{index}",
                  "parameters": {"type": "object", "description": "y" * 900}} for index in range(9)]
    rejected = [
        ([{"type": "mcp", "server_label": "x"}], None), ([{"type": "web_search"}], None),
        ([{**_TOOL, "type": None}], None), ([_TOOL, _TOOL], None), (many, None), (oversized, None),
        ([{**_TOOL, "name": "has space"}], None), ([{**_TOOL, "parameters": "object"}], None),
        ([{**_TOOL, "strict": True}], None), ([_TOOL], {"type": "function", "name": "other"}),
        ([_TOOL], "always"), ([], None), (None, "auto"),
    ]
    for tools, choice in rejected:
        with pytest.raises(ProviderValidationError):
            realtime_transport.function_tools(tools, choice)
    # Admission freezes tools only where function calling is published and token-billed.
    realtime_transport.validate_realtime_request(_route(), tools=[_TOOL], tool_choice="auto")
    for route in (_route("gpt-realtime"), _route(media={"realtime_input_per_minute": "0.1",
                                                        "realtime_output_per_minute": "0.2"})):
        with pytest.raises(ProviderValidationError, match="function calling"):
            realtime_transport.validate_realtime_request(route, tools=[_TOOL])


def test_image_input_accepts_bounded_png_and_jpeg_data_urls_only():
    png = realtime_protocol.input_image_content(_data_url(_png()), "low")
    assert png == {"type": "input_image", "image_url": _data_url(_png()), "detail": "low"}
    assert realtime_protocol.input_image_content(_data_url(_jpeg(), "image/jpeg"))["type"] == "input_image"
    webp = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\0" * 32
    rejected = [
        _data_url(webp, "image/webp"),  # The Realtime item schema lists PNG and JPEG only.
        _data_url(_png(4096, 512)), _data_url(_jpeg(2049, 10), "image/jpeg"), _data_url(_png(0, 10)),
        _data_url(b"not an image"), _data_url(_png() + b"\0" * (1024 * 1024)),
        "https://example.com/cat.png", "data:image/png;base64,***", None,
    ]
    for image_url in rejected:
        with pytest.raises(RealtimeTransportError):
            realtime_protocol.input_image_content(image_url)
    with pytest.raises(RealtimeTransportError):
        realtime_protocol.input_image_content(_data_url(_png()), "original")


def test_client_events_relay_images_and_function_results_only_where_enabled():
    enabled = {"image_input": True, "function_calling": True}

    def event(message, *, wire="openai", features=enabled):
        return realtime_protocol._client_event(message, "openai", wire=wire, voice="alloy",
                                               model="gpt-realtime-2.1", features=features)

    image = {"type": "message", "role": "user", "content": [{"type": "input_image", "image_url": _data_url(_png())}]}
    assert event({"type": "conversation.item.create", "item": image})[0] == {
        "type": "conversation.item.create", "item": image}
    output = {"type": "function_call_output", "call_id": "call_1", "output": "{\"status\":\"shipped\"}"}
    assert event({"type": "conversation.item.create", "item": output})[0]["item"] == output
    assert event({"type": "conversation.input_image", "image_url": _data_url(_png()), "detail": "high"},
                 wire="native")[0] == {"type": "conversation.item.create", "item": {
                     "type": "message", "role": "user", "content": [
                         {"type": "input_image", "image_url": _data_url(_png()), "detail": "high"}]}}
    assert event({"type": "function_call.output", "call_id": "call_1", "output": "ok"}, wire="native")[0] == {
        "type": "conversation.item.create", "item": {"type": "function_call_output", "call_id": "call_1",
                                                     "output": "ok"}}
    update = event({"type": "session.update", "session": {"tools": [_TOOL], "tool_choice": "required"}})[0]
    assert update == {"type": "session.update", "session": {"tools": [_TOOL], "tool_choice": "required"}}
    rejected = [
        ({"type": "conversation.item.create", "item": image}, "openai", {}),
        ({"type": "conversation.item.create", "item": output}, "openai", {"image_input": True}),
        ({"type": "conversation.input_image", "image_url": _data_url(_png())}, "native", {}),
        ({"type": "function_call.output", "call_id": "call_1", "output": "ok"}, "native", {}),
        ({"type": "conversation.item.create", "item": {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "hi"}]}}, "openai", enabled),
        ({"type": "conversation.item.create", "item": {**image, "role": "assistant"}}, "openai", enabled),
        ({"type": "conversation.item.create", "item": {"type": "message", "role": "user", "content": [
            image["content"][0], image["content"][0]]}}, "openai", enabled),
        ({"type": "conversation.item.create", "item": {**output, "call_id": ""}}, "openai", enabled),
        ({"type": "conversation.item.create", "item": {**output, "output": "x" * 32769}}, "openai", enabled),
        ({"type": "session.update", "session": {"tools": [{"type": "mcp", "server_label": "x"}]}}, "openai", enabled),
        ({"type": "session.update", "session": {"tools": [_TOOL]}}, "openai", {}),
        ({"type": "session.update", "session": {"max_output_tokens": "inf"}}, "openai", enabled),
        ({"type": "session.update", "session": {"truncation": "disabled"}}, "openai", enabled),
        ({"type": "response.create", "response": {"tools": [_TOOL]}}, "openai", enabled),
    ]
    for message, wire, features in rejected:
        with pytest.raises(RealtimeTransportError):
            event(message, wire=wire, features=features)


def test_native_function_call_events_are_projected():
    def native(message):
        return realtime_protocol._provider_events(message, "openai", wire="native")[0]

    assert native({"type": "response.function_call_arguments.delta", "response_id": "r", "item_id": "i",
                   "call_id": "call_1", "delta": "{\"id\":"}) == [
        {"type": "function_call.arguments.delta", "call_id": "call_1", "delta": "{\"id\":"}]
    assert native({"type": "response.function_call_arguments.done", "call_id": "call_1", "name": "lookup_order",
                   "arguments": "{\"id\":\"7\"}"}) == [{"type": "function_call.arguments.done", "call_id": "call_1",
                                                      "name": "lookup_order", "arguments": "{\"id\":\"7\"}"}]
    assert native({"type": "response.function_call_arguments.done", "call_id": "call_1"}) == []


class _Upstream:
    def __init__(self):
        self.sent = []
        self.queue = asyncio.Queue()
        self.transport = SimpleNamespace(abort=lambda: None)

    async def send(self, message):
        parsed = json.loads(message)
        self.sent.append(parsed)
        if parsed["type"] == "response.create":
            await self.queue.put(json.dumps({"type": "response.created", "response": {"id": "resp_2"}}))
            await self.queue.put(json.dumps({"type": "response.done", "response": {"id": "resp_2", "usage": {
                "total_tokens": 60, "input_tokens": 50, "output_tokens": 10,
                "input_token_details": {"text_tokens": 50, "audio_tokens": 0, "image_tokens": 0,
                                        "cached_tokens": 0},
                "output_token_details": {"text_tokens": 10, "audio_tokens": 0}}}}))

    async def recv(self):
        return await self.queue.get()


class _Browser:
    def __init__(self, frames):
        self.queue = asyncio.Queue()
        for frame in frames:
            self.queue.put_nowait(json.dumps(frame))
        self.events = []

    async def receive_text(self):
        return await self.queue.get()

    async def send_json(self, event):
        self.events.append(event)


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
async def test_native_session_configures_tools_relays_images_and_continues_after_function_output(monkeypatch):
    upstream = _Upstream()
    captured = _connect(monkeypatch, upstream)
    browser = _Browser([{"type": "conversation.input_image", "image_url": _data_url(_png())},
                        {"type": "function_call.output", "call_id": "call_1", "output": "shipped"},
                        {"type": "session.close"}])
    meter = await asyncio.wait_for(realtime_protocol.relay_audio(browser, _route(), session_id="session",
        voice="alloy", instructions=None, max_duration_seconds=30, is_canceled=_not_canceled, token_usage=True,
        tools=[_TOOL], tool_choice="auto"), timeout=5)
    session = upstream.sent[0]["session"]
    assert session["tools"] == [_TOOL] and session["tool_choice"] == "auto"
    assert session["max_output_tokens"] == realtime_transport.RESPONSE_MAX_OUTPUT_TOKENS
    # Image sessions accept one image per frame in both directions.
    assert captured["max_size"] == 2 * 1024 * 1024
    # Closing while that response may still be in flight cancels it and drains its final usage.
    sent = [frame["type"] for frame in upstream.sent[1:]]
    assert sent[:3] == ["conversation.item.create", "conversation.item.create", "response.create"]
    assert set(sent[3:]) <= {"response.cancel"}
    assert upstream.sent[1]["item"]["content"][0]["type"] == "input_image"
    assert upstream.sent[2]["item"] == {"type": "function_call_output", "call_id": "call_1", "output": "shipped"}
    # The Lumen-issued response.create is metered and its usage drained before close.
    assert meter.usage_state == "complete" and meter.usage.input_tokens == 50


@pytest.mark.asyncio
async def test_session_image_count_and_unsupported_sessions_are_refused(monkeypatch):
    monkeypatch.setattr(realtime_protocol, "MAX_SESSION_IMAGES", 1)
    _connect(monkeypatch, _Upstream())
    image = {"type": "conversation.input_image", "image_url": _data_url(_png())}
    with pytest.raises(RealtimeTransportError) as failure:
        await asyncio.wait_for(realtime_protocol.relay_audio(_Browser([image, image]), _route(),
            session_id="session", voice="alloy", instructions=None, max_duration_seconds=30,
            is_canceled=_not_canceled, token_usage=True), timeout=5)
    assert failure.value.meter is not None
    # Without token billing (or on an unpublished model) no image or tool path exists.
    with pytest.raises(RealtimeTransportError):
        await realtime_protocol.relay_audio(_Browser([]), _route("gpt-realtime"), session_id="session",
            voice="alloy", instructions=None, max_duration_seconds=30, is_canceled=_not_canceled,
            token_usage=True, tools=[_TOOL])


def test_image_and_tool_tokens_settle_per_modality_within_the_pinned_bound():
    plan = realtime_transport.validate_realtime_request(_route(), max_duration_seconds=60)
    # (16384 + 16384 + 512) text at $4/M + 16384 at $32/M (highest input) + 4096 at $64/M.
    assert plan["response_bound_usd"] == "0.9195520000"
    meter = TokenUsageMeter("openai")
    meter.observe({"type": "response.done", "response": {"id": "resp_1", "usage": {
        "total_tokens": 3340, "input_tokens": 3000, "output_tokens": 340,
        "input_token_details": {"text_tokens": 400, "audio_tokens": 600, "image_tokens": 2000,
                                "cached_tokens": 1200, "cached_tokens_details": {
                                    "text_tokens": 200, "audio_tokens": 0, "image_tokens": 1000}},
        "output_token_details": {"text_tokens": 40, "audio_tokens": 300}}}})
    usage, state = meter.result()
    checkpoint = json.loads(json.dumps(realtime._checkpoint(
        AudioMeter(4800, 4800, 24000, 24000, usage=usage, usage_state=state), "tokens")))
    cost, components, _, _ = realtime._token_bill(_RUN, {**plan, "max_duration_seconds": 60}, checkpoint)
    # text 200*4 + 200*0.4 + 40*24; audio 600*32 + 300*64; image 1000*5 + 1000*0.5 (per M).
    assert cost.raw_cost == Decimal("0.04574")
    kinds = {item["kind"] for item in components}
    assert {"image_input_tokens", "image_cache_read_input_tokens"} <= kinds


@pytest.mark.asyncio
async def test_native_admission_freezes_tools_only_for_function_calling_models(monkeypatch):
    monkeypatch.setattr(admission, "existing_run_for_intent", AsyncMock(return_value=None))
    precheck = AsyncMock(side_effect=realtime.DurableRunInputError("credit boundary"))
    monkeypatch.setattr(realtime.credit, "precheck", precheck)
    for model, error in (("gpt-realtime", ProviderValidationError), ("gpt-realtime-2.1", realtime.DurableRunInputError)):
        monkeypatch.setattr(realtime.routing, "resolve_api_model", AsyncMock(return_value={
            **_route(model), "provider_id": 1, "api_provider": "openai"}))
        with pytest.raises(error):
            await realtime.admit_realtime_session({"model_id": model, "tools": [_TOOL], "tool_choice": "auto"},
                project_id="p", user_id="u", client_request_id=str(uuid4()))
    intent = admission.existing_run_for_intent.await_args.kwargs["intent"]
    assert (intent["tools"], intent["tool_choice"]) == ([_TOOL], "auto")
    precheck.assert_awaited_once()

    app = FastAPI()
    app.include_router(native_api.router, prefix="/v1")
    app.dependency_overrides[get_principal] = lambda: {"user_id": "u", "project_id": "p", "auth_type": "keystone",
                                                       "source": "web", "roles": ["member"], "is_system_admin": False}
    admitted = AsyncMock(return_value={"session_id": str(uuid4())})
    monkeypatch.setattr(native_api, "admit_realtime_session", admitted)
    with TestClient(app) as client:
        def post(body):
            return client.post("/v1/chat/realtime/sessions", json={"model_id": "m", **body},
                               headers={"Idempotency-Key": str(uuid4())})

        assert post({"kind": "live", "tools": [_TOOL]}).status_code == 422
        assert post({"tools": [_TOOL] * 17}).status_code == 422
        assert post({"tools": [_TOOL], "tool_choice": "auto"}).status_code == 201
        assert admitted.await_args.args[0]["tools"] == [_TOOL]
        assert post({}).status_code == 201 and "tools" not in admitted.await_args.args[0]


def _done(response_id, *, text_in=0, audio_in=0, cached_audio=0, text_out=0, audio_out=0):
    return {"type": "response.done", "response": {"id": response_id, "usage": {
        "total_tokens": text_in + audio_in + text_out + audio_out, "input_tokens": text_in + audio_in,
        "output_tokens": text_out + audio_out,
        "input_token_details": {"text_tokens": text_in, "audio_tokens": audio_in, "image_tokens": 0,
                                "cached_tokens": cached_audio, "cached_tokens_details": {
                                    "text_tokens": 0, "audio_tokens": cached_audio, "image_tokens": 0}},
        "output_token_details": {"text_tokens": text_out, "audio_tokens": audio_out}}}}


_CHEAP = {"audio_in": 1000, "cached_audio": 1000, "audio_out": 100}
_WORST_INPUT = {"text_in": 33280, "audio_in": 16384}  # Instructions/tools plus a full uncached context.
_SCENARIOS = {
    "expensive_first_response": [{**_WORST_INPUT, "audio_out": 4096}],
    "cache_miss_after_cheap_cached_responses": [_CHEAP] * 6 + [_WORST_INPUT] + [_CHEAP] * 4,
    "long_reply_after_short_ones": [_CHEAP] * 6 + [{"audio_in": 1000, "audio_out": 4096}] + [_CHEAP] * 4,
}


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", list(_SCENARIOS))
async def test_hold_covers_any_response_within_the_pinned_bound(monkeypatch, scenario):
    """The relay starts a response only while two worst-case responses still fit the hold."""
    script = list(_SCENARIOS[scenario])

    class ScriptUpstream(_Upstream):
        async def send(self, message):
            parsed = json.loads(message)
            self.sent.append(parsed)
            if parsed["type"] == "input_audio_buffer.append" and script:
                response_id = f"resp_{len(script)}"
                await self.queue.put(json.dumps({"type": "response.created", "response": {"id": response_id}}))
                await self.queue.put(json.dumps(_done(response_id, **script.pop(0))))

    upstream = ScriptUpstream()
    _connect(monkeypatch, upstream)

    class Talking(_Browser):
        async def receive_text(self):
            await asyncio.sleep(0.005)
            return json.dumps({"type": "audio.input.append", "audio": _PCM} if script else {"type": "session.close"})

    plan = realtime_transport.validate_realtime_request(_route(), max_duration_seconds=10)
    price, hold, bound = realtime._token_hold({**plan, "max_duration_seconds": 10})
    # Admission funds two worst-case responses on top of the 10 s duration part.
    assert (hold, bound) == (Decimal("1.890304"), Decimal("0.919552"))
    meter = await asyncio.wait_for(realtime_protocol.relay_audio(Talking([]), _route(), session_id="session",
        voice="alloy", instructions=None, max_duration_seconds=10, is_canceled=_not_canceled, token_usage=True,
        token_hold=(price, hold, bound)), timeout=5)
    total = price(meter.usage)
    assert meter.usage_state == "complete" and total <= hold
    # Each scripted response fits the pinned bound, so whatever ran stays settleable within the hold.
    checkpoint = json.loads(json.dumps(realtime._checkpoint(meter, "tokens")))
    assert realtime._token_bill(_RUN, {**plan, "max_duration_seconds": 10}, checkpoint)[0].raw_cost == total


@pytest.mark.asyncio
async def test_client_response_requests_beyond_the_hold_are_never_started(monkeypatch):
    upstream = _Upstream()
    _connect(monkeypatch, upstream)
    browser = _Browser([{"type": "response.create"}])
    price = realtime._token_hold({**realtime_transport.validate_realtime_request(_route()),
                                  "max_duration_seconds": 300})[0]
    # A hold below two worst-case responses (only reachable for a tampered snapshot) refuses at once.
    await asyncio.wait_for(realtime_protocol.relay_audio(browser, _route(), session_id="session", voice="alloy",
        instructions=None, max_duration_seconds=30, wire="openai", is_canceled=_not_canceled, token_usage=True,
        token_hold=(price, Decimal("1"), Decimal("0.919552"))), timeout=5)
    assert [frame["type"] for frame in upstream.sent] == ["session.update"]


def test_derived_hold_exemption_is_openai_only():
    from lumen.services.providers import pricing

    unreserved = dict(_TOKEN_MEDIA)
    for kind_args in ({"provider_type": "openai"},):
        assert pricing.validate_media_pricing("realtime", unreserved, **kind_args)["billing_basis"] == "tokens"
    for provider_type in ("gemini", None):
        with pytest.raises(ProviderValidationError, match="reservation_usd"):
            pricing.validate_media_pricing("realtime", unreserved, provider_type=provider_type)
    gemini = {**_route("gemini-2.5-flash-live", provider="gemini"), "media_pricing": unreserved}
    assert not pricing.route_media_pricing_available(gemini)
    assert not realtime_transport.realtime_route_ready(gemini)
    assert pricing.route_media_pricing_available({**gemini, "media_pricing": {**unreserved, "reservation_usd": "5"}})
    assert pricing.route_media_pricing_available(_route())


@pytest.mark.asyncio
async def test_admission_reserves_the_derived_hold_and_reports_the_frozen_frame_cap(monkeypatch):
    monkeypatch.setattr(admission, "existing_run_for_intent", AsyncMock(return_value=None))
    monkeypatch.setattr(realtime.routing, "resolve_api_model", AsyncMock(return_value={
        **_route(), "provider_id": 1, "api_provider": "openai", "margin_multiplier": Decimal("1")}))
    precheck = AsyncMock()
    monkeypatch.setattr(realtime.credit, "precheck", precheck)
    reserved = {}

    def credits_for_cost(envelope, margin, per_usd):
        reserved["usd"] = envelope
        raise RuntimeError("stop before storage")

    monkeypatch.setattr(realtime.credit, "credits_for_cost", credits_for_cost)
    with pytest.raises(RuntimeError, match="stop before storage"):
        await realtime.admit_realtime_session({"model_id": "gpt-realtime-2.1", "max_duration_seconds": 10},
            project_id="p", user_id="u", client_request_id=str(uuid4()))
    # The credit reservation is sized from the full derived hold (duration part + two worst cases).
    assert reserved["usd"] == Decimal("1.890304")
    run = SimpleNamespace(id="run", model_name="gpt-realtime-2.1", capability_snapshot={"provider_type": "openai"},
                          pricing_snapshot={"realtime_image_input": True})
    monkeypatch.setattr(realtime, "_issue_ticket", AsyncMock(return_value="t" * 40))
    response = await realtime._response(run, user_id="u", project_id="p")
    assert (response["realtime_image_input"], response["max_client_frame_bytes"]) == (True, 2 * 1024 * 1024)
    run.pricing_snapshot = {}
    response = await realtime._response(run, user_id="u", project_id="p")
    assert (response["realtime_image_input"], response["max_client_frame_bytes"]) == (False, 64 * 1024)
