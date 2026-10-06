"""Dynamic API demand is measured from actual in-flight responses, not worker queue size."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from lumen.main import ready
from lumen.services.infrastructure.api_load import ApiLoadMeter, ApiLoadMiddleware


@pytest.mark.asyncio
async def test_streaming_api_load_counts_sse_and_only_first_model_text():
    meter = ApiLoadMeter()
    flowing = asyncio.Event()
    release = asyncio.Event()
    messages = []

    async def app(_scope, _receive, send):
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"text/event-stream")]})
        await send({"type": "http.response.body", "body": b"event: ping\ndata: {\"type\":\"ping\"}\n\n",
                    "more_body": True})
        await send({"type": "http.response.body", "body": b"event: content_block_delta\ndata: "
                    + json.dumps({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hello"}}).encode()
                    + b"\n\n", "more_body": True})
        flowing.set()
        await release.wait()
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    async def capture(message):
        messages.append(message)

    middleware = ApiLoadMiddleware(app, meter=meter)
    task = asyncio.create_task(middleware(
        {"type": "http", "path": "/v1/messages", "method": "POST"}, lambda: None, capture,
    ))
    try:
        await asyncio.wait_for(flowing.wait(), 2)
        assert meter.snapshot()["active_requests"] == 1
        assert meter.snapshot()["active_sse"] == 1
        assert meter.snapshot()["ttft_samples"] == 1
        assert meter.snapshot()["p95_ttft_ms"] is not None
    finally:
        release.set()
        await task
    assert meter.snapshot()["active_requests"] == 0
    assert meter.snapshot()["active_sse"] == 0
    assert len(messages) == 4


@pytest.mark.asyncio
async def test_readiness_probe_does_not_create_api_demand_and_remote_load_is_rejected():
    meter = ApiLoadMeter()

    async def app(_scope, _receive, send):
        assert meter.snapshot()["active_requests"] == 0
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def discard(_message):
        pass

    await ApiLoadMiddleware(app, meter=meter)(
        {"type": "http", "path": "/v1/ready", "method": "GET"}, lambda: None, discard,
    )
    assert meter.snapshot()["ttft_samples"] == 0
    with pytest.raises(HTTPException) as denied:
        await ready(SimpleNamespace(client=SimpleNamespace(host="198.51.100.10")), include_load=True)
    assert denied.value.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/v1/realtime", "/v1beta/realtime", "/v1/chat/realtime/sessions/test-session/ws"])
async def test_websocket_counts_disconnect_cleanup_until_application_exit(path):
    from datetime import UTC, datetime
    meter = ApiLoadMeter()
    connected = asyncio.Event()
    disconnect = asyncio.Event()
    disconnected = asyncio.Event()
    finish = asyncio.Event()

    async def receive():
        await disconnect.wait()
        return {"type": "websocket.disconnect", "code": 1000}

    async def app(_scope, receive, send):
        await send({"type": "websocket.accept"})
        connected.set()
        await receive()
        disconnected.set()
        await finish.wait()

    async def send(message):
        assert message["type"] == "websocket.accept"

    task = asyncio.create_task(ApiLoadMiddleware(app, meter=meter)(
        {"type": "websocket", "path": path}, receive, send))
    try:
        await asyncio.wait_for(connected.wait(), 2)
        snapshot = meter.snapshot()
        assert snapshot["active_ws"] == 1
        assert snapshot["active_requests"] == snapshot["active_sse"] == 0
        assert 0 <= (datetime.now(UTC) - datetime.fromisoformat(snapshot["observed_at"])).total_seconds() < 2
        disconnect.set()
        await asyncio.wait_for(disconnected.wait(), 2)
        assert meter.active_ws == 1  # Settlement/cleanup still belongs to the session.
    finally:
        disconnect.set()
        finish.set()
        await task
    assert meter.active_ws == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("end", ["exception", "cancel", "close"])
async def test_websocket_releases_exactly_once_on_all_exit_paths(end):
    meter = ApiLoadMeter()
    connected = asyncio.Event()
    release = asyncio.Event()

    async def app(_scope, _receive, send):
        connected.set()
        await release.wait()
        if end == "exception":
            raise RuntimeError("socket failed")
        if end == "close":
            await send({"type": "websocket.close", "code": 1000})
            assert meter.active_ws == 1

    async def send(_message):
        pass

    task = asyncio.create_task(ApiLoadMiddleware(app, meter=meter)(
        {"type": "websocket", "path": "/v1/realtime"}, lambda: None, send))
    await asyncio.wait_for(connected.wait(), 2)
    assert meter.active_ws == 1
    if end == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    elif end == "exception":
        release.set()
        with pytest.raises(RuntimeError):
            await task
    else:
        release.set()
        await task
    assert meter.active_ws == meter.active_requests == meter.active_sse == 0


@pytest.mark.asyncio
async def test_unrelated_websocket_routes_are_not_guest_demand():
    meter = ApiLoadMeter()
    async def app(_scope, _receive, _send):
        assert meter.active_ws == 0
    await ApiLoadMiddleware(app, meter=meter)(
        {"type": "websocket", "path": "/admin/socket"}, lambda: None, lambda _: None)
    assert meter.active_ws == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("end", ["completion", "disconnect", "exception", "cancel"])
async def test_http_sse_meter_and_reservations_cover_application_cleanup(end):
    from lumen.services.infrastructure.api_load import ApiAdmission, ApiAdmissionMiddleware

    limits = SimpleNamespace(api_max_active_requests=1, api_max_sse_connections=1,
                             api_max_websocket_connections=1, api_max_body_bytes=64)
    admission = ApiAdmission(settings=lambda: limits)
    meter = ApiLoadMeter()
    streaming = asyncio.Event()
    finish = asyncio.Event()
    ended = asyncio.Event()
    cleanup = asyncio.Event()

    async def app(scope, receive, send):
        scope.setdefault("lumen.sse_reservations", []).append(admission.reserve("sse"))
        assert admission.snapshot()["reserved_sse"] == 1
        assert meter.active_sse == 0  # Reservation alone is not measured stream demand.
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"text/event-stream")]})
        streaming.set()
        try:
            await finish.wait()
            if end == "exception":
                raise RuntimeError("stream failed")
            if end == "disconnect":
                await receive()
            else:
                await send({"type": "http.response.body", "body": b"done", "more_body": False})
        finally:
            ended.set()
            await cleanup.wait()

    async def receive():
        return {"type": "http.disconnect"}

    async def discard(_message):
        pass

    middleware = ApiAdmissionMiddleware(ApiLoadMiddleware(app, meter=meter), admission=admission)
    task = asyncio.create_task(middleware(
        {"type": "http", "path": "/v1/messages", "method": "POST"}, receive, discard))
    try:
        await asyncio.wait_for(streaming.wait(), 2)
        assert meter.active_requests == meter.active_sse == 1
        assert admission.snapshot() == {"reserved_requests": 1, "reserved_sse": 1, "reserved_ws": 0}
        if end == "cancel":
            task.cancel()
        else:
            finish.set()
        await asyncio.wait_for(ended.wait(), 2)
        assert meter.active_requests == meter.active_sse == 1
        assert admission.snapshot() == {"reserved_requests": 1, "reserved_sse": 1, "reserved_ws": 0}
        assert not task.done()  # Drain cannot delete a guest still settling/cleaning up.
        cleanup.set()
        if end == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await task
        elif end == "exception":
            with pytest.raises(RuntimeError, match="stream failed"):
                await task
        else:
            await task
    finally:
        finish.set()
        cleanup.set()
        if not task.done():
            await task
    assert meter.active_requests == meter.active_sse == meter.active_ws == 0
    assert admission.snapshot() == {"reserved_requests": 0, "reserved_sse": 0, "reserved_ws": 0}


@pytest.mark.asyncio
@pytest.mark.parametrize("end", ["close", "disconnect", "exception", "cancel"])
async def test_websocket_meter_and_reservation_cover_session_and_asgi_cleanup(end):
    from starlette.websockets import WebSocket

    from lumen.services.infrastructure.api_load import ApiAdmission, ApiAdmissionMiddleware, admit_websocket

    admission = ApiAdmission(settings=lambda: SimpleNamespace(api_max_websocket_connections=1))
    meter = ApiLoadMeter()
    entered = asyncio.Event()
    finish = asyncio.Event()
    settling = asyncio.Event()
    settle = asyncio.Event()
    asgi_cleanup = asyncio.Event()
    cleanup = asyncio.Event()
    incoming = asyncio.Queue()
    incoming.put_nowait({"type": "websocket.connect"})

    @admit_websocket
    async def endpoint(websocket):
        await websocket.accept()
        entered.set()
        try:
            await finish.wait()
            if end == "exception":
                raise RuntimeError("session failed")
            if end == "disconnect":
                await websocket.receive()
            else:
                await websocket.close()
        finally:
            settling.set()
            await settle.wait()

    async def app(scope, receive, send):
        try:
            await endpoint(WebSocket(scope, receive, send))
        finally:
            asgi_cleanup.set()
            await cleanup.wait()

    async def discard(_message):
        pass

    gate = ApiAdmissionMiddleware(ApiLoadMiddleware(app, meter=meter), admission=admission)
    task = asyncio.create_task(gate(
        {"type": "websocket", "path": "/v1/realtime", "headers": [], "query_string": b""},
        incoming.get, discard))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        if end == "cancel":
            task.cancel()
        else:
            if end == "disconnect":
                incoming.put_nowait({"type": "websocket.disconnect", "code": 1000})
            finish.set()
        await asyncio.wait_for(settling.wait(), 2)
        assert meter.active_ws == admission.snapshot()["reserved_ws"] == 1
        settle.set()
        await asyncio.wait_for(asgi_cleanup.wait(), 2)
        assert meter.active_ws == admission.snapshot()["reserved_ws"] == 1
        assert not task.done()
        cleanup.set()
        if end == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await task
        elif end == "exception":
            with pytest.raises(RuntimeError, match="session failed"):
                await task
        else:
            await task
    finally:
        finish.set()
        settle.set()
        cleanup.set()
        if not task.done():
            await task
    assert meter.active_ws == admission.snapshot()["reserved_ws"] == 0
