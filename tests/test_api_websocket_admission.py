"""Native SSE and all realtime wires reserve before durable/provider work.

The Uvicorn regression deliberately uses Server.serve with a bound loopback socket;
TestClient alone cannot establish deployment support for websocket.http.response.
"""
from __future__ import annotations

import asyncio
import json
import socket
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

import httpx
import pytest
import uvicorn
import websockets
from fastapi import FastAPI, Request
from websockets.exceptions import InvalidStatus

from lumen.api import completions
from lumen.api import realtime as native
from lumen.api.compat import realtime as compat
from lumen.auth import get_principal
from lumen.service_authority import SERVICE_CAPABILITIES
from lumen.services.durable_runs import realtime as durable_realtime
from lumen.services.durable_runs.errors import DurableRunNotFound
from lumen.services.infrastructure.api_load import ApiAdmission, ApiAdmissionMiddleware
from lumen.services.providers import realtime_protocol

_SESSION_ID = "00000000-0000-0000-0000-000000000001"
_PATHS = (
    f"/v1/chat/realtime/sessions/{_SESSION_ID}/ws",
    "/v1/realtime?model=gpt-realtime",
    "/v1beta/realtime",
)
_HEADERS = {"x-realtime-token": "one-use-ticket", "authorization": "Bearer scoped-key"}
_SETUP = {"setup": {"model": "models/gemini-2.5-flash-live"}}


@pytest.fixture
def gateway(monkeypatch):
    settings = SimpleNamespace(
        api_max_active_requests=8,
        api_max_sse_connections=1,
        api_max_websocket_connections=1,
        api_max_body_bytes=4096,
    )
    state = SimpleNamespace(draining=False)
    admission = ApiAdmission(settings=lambda: settings, state=state)
    app = FastAPI()
    app.include_router(completions.router, prefix="/v1")
    app.include_router(native.router, prefix="/v1")
    app.include_router(compat.router)
    app.add_middleware(ApiAdmissionMiddleware, admission=admission)

    async def principal():
        return {"user_id": "owner", "project_id": "project", "auth_type": "keystone",
                "api_key_id": None, "source": "web", "scopes": (), "roles": ["member", *SERVICE_CAPABILITIES], "is_system_admin": False}

    app.dependency_overrides[get_principal] = principal
    verify = AsyncMock(return_value={"user_id": "owner", "project_id": "project", "api_key_id": 37,
                                     "scopes": ["compat:realtime:write"], "roles": ["member", *SERVICE_CAPABILITIES], "service_system_admin": False})
    create = AsyncMock(return_value={"session_id": _SESSION_ID, "connect_token": "compat-ticket"})
    consume = AsyncMock(side_effect=AssertionError("denial must not consume a ticket"))
    upstream = AsyncMock(side_effect=AssertionError("denial must not connect upstream"))
    monkeypatch.setattr(compat, "get_settings", lambda: SimpleNamespace(chat_api_hosts=""))
    monkeypatch.setattr(native, "origin_allowed", lambda _socket: True)
    monkeypatch.setattr(compat, "origin_allowed", lambda _socket: True)
    monkeypatch.setattr(compat.api_key_store, "verify_key", verify)
    monkeypatch.setattr(compat, "admit_realtime_session", create)
    monkeypatch.setattr(durable_realtime, "consume_ticket", consume)
    monkeypatch.setattr(realtime_protocol, "relay_audio", upstream)
    return SimpleNamespace(app=app, admission=admission, state=state,
                           verify=verify, create=create, consume=consume, upstream=upstream)


@pytest.fixture(params=["middleware", "decorator"])
def socket_app(request, gateway):
    if request.param == "middleware":
        return gateway.app
    app = FastAPI()
    app.include_router(native.router, prefix="/v1")
    app.include_router(compat.router)

    async def standalone(scope, receive, send):
        # Embedded routers need the decorators even without admission middleware.
        scope["lumen.api_admission"] = gateway.admission
        await app(scope, receive, send)

    return standalone


def _scope(path):
    url = urlsplit(path)
    return {"type": "websocket", "asgi": {"version": "3.0", "spec_version": "2.4"},
            "scheme": "ws", "path": url.path, "raw_path": url.path.encode(),
            "query_string": url.query.encode(), "root_path": "", "subprotocols": [],
            "headers": [(key.encode(), value.encode()) for key, value in _HEADERS.items()],
            "client": ("127.0.0.1", 12345), "server": ("127.0.0.1", 80),
            "extensions": {"websocket.http.response": {}}}


async def _socket_call(app, path, *, incoming=None, sent=None):
    incoming = incoming if incoming is not None else asyncio.Queue()
    incoming.put_nowait({"type": "websocket.connect"})
    if path == "/v1beta/realtime":
        incoming.put_nowait({"type": "websocket.receive", "text": json.dumps(_SETUP)})
    sent = sent if sent is not None else []

    async def send(message):
        sent.append(message)

    await app(_scope(path), incoming.get, send)
    return sent


def _assert_no_durable_work(gateway):
    gateway.verify.assert_not_awaited()
    gateway.create.assert_not_awaited()
    gateway.consume.assert_not_awaited()
    gateway.upstream.assert_not_awaited()


@pytest.mark.parametrize("path", _PATHS)
@pytest.mark.parametrize("reason", ["full", "draining"])
async def test_all_websocket_wires_deny_http503_before_auth_ticket_or_durable_work(gateway, socket_app, path, reason):
    held = gateway.admission.reserve("ws") if reason == "full" else None
    gateway.state.draining = reason == "draining"
    try:
        messages = await _socket_call(socket_app, path)
        assert [message["type"] for message in messages] == [
            "websocket.http.response.start", "websocket.http.response.body"]
        assert messages[0]["status"] == 503
        assert json.loads(messages[1]["body"]) == {
            "detail": "API capacity exhausted" if reason == "full" else "guest draining"}
        if reason == "full":
            assert (b"retry-after", b"1") in messages[0]["headers"]
        _assert_no_durable_work(gateway)
        assert gateway.admission.snapshot()["reserved_ws"] == int(held is not None)
    finally:
        if held is not None:
            held.release()
    assert gateway.admission.snapshot() == {"reserved_requests": 0, "reserved_sse": 0, "reserved_ws": 0}


@pytest.mark.parametrize("path", _PATHS)
@pytest.mark.parametrize("end", ["success", "disconnect", "error", "cancel"])
async def test_websocket_slot_covers_session_and_releases_on_every_exit(gateway, socket_app, monkeypatch, path, end):
    entered = asyncio.Event()
    finish = asyncio.Event()
    incoming = asyncio.Queue()
    sent = []

    async def session(websocket, **_kwargs):
        assert gateway.admission.snapshot()["reserved_ws"] == 1
        entered.set()
        if end == "disconnect":
            await websocket.receive_json()
            raise AssertionError("disconnect must raise WebSocketDisconnect")
        await finish.wait()
        if end == "error":
            raise RuntimeError("session failed")

    monkeypatch.setattr(native, "run_realtime_session", session)
    monkeypatch.setattr(compat, "run_realtime_session", session)
    task = asyncio.create_task(_socket_call(socket_app, path, incoming=incoming, sent=sent))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert gateway.admission.snapshot()["reserved_ws"] == 1
        assert sent[0]["type"] == "websocket.accept"
        # A second socket, even on a different wire, shares the same process cap.
        denied = await _socket_call(socket_app, _PATHS[1] if path == _PATHS[0] else _PATHS[0])
        assert denied[0]["type"] == "websocket.http.response.start"
        assert denied[0]["status"] == 503
        assert gateway.admission.snapshot()["reserved_ws"] == 1
        if end == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            if end == "disconnect":
                incoming.put_nowait({"type": "websocket.disconnect", "code": 1000})
            else:
                finish.set()
            await asyncio.wait_for(task, 2)
            closes = [message for message in sent if message["type"] == "websocket.close"]
            if end != "disconnect":
                assert closes[-1]["code"] == (1011 if end == "error" else 1000)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert gateway.admission.snapshot() == {"reserved_requests": 0, "reserved_sse": 0, "reserved_ws": 0}
    # Exercise reuse, not only the counter value.
    slot = gateway.admission.reserve("ws")
    slot.release()
    gateway.consume.assert_not_awaited()
    gateway.upstream.assert_not_awaited()
    assert gateway.create.await_count == (0 if path == _PATHS[0] else 1)
    assert gateway.verify.await_count == (0 if path == _PATHS[0] else 1)


@pytest.mark.parametrize("path", _PATHS)
async def test_websocket_slot_releases_when_preaccept_auth_raises(gateway, socket_app, monkeypatch, path):
    if path == _PATHS[0]:
        def broken_origin(_websocket):
            assert gateway.admission.snapshot()["reserved_ws"] == 1
            raise RuntimeError("origin check failed")
        monkeypatch.setattr(native, "origin_allowed", broken_origin)
    else:
        gateway.verify.side_effect = RuntimeError("key store failed")
    with pytest.raises(RuntimeError):
        await _socket_call(socket_app, path)
    assert gateway.admission.snapshot()["reserved_ws"] == 0
    gateway.create.assert_not_awaited()
    gateway.consume.assert_not_awaited()
    gateway.upstream.assert_not_awaited()


async def test_native_events_sse_429_precedes_first_event_query(gateway, monkeypatch):
    events = AsyncMock(side_effect=AssertionError("full SSE capacity must reject before event query"))
    monkeypatch.setattr(completions.queries, "owned_events", events)
    held = gateway.admission.reserve("sse")
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(gateway.app), base_url="http://test") as client:
            response = await client.get("/v1/runs/run-1/events")
        assert response.status_code == 429
        assert response.headers["retry-after"] == "1"
        assert response.json() == {"detail": "API capacity exhausted"}
        events.assert_not_awaited()
        assert gateway.admission.snapshot() == {"reserved_requests": 0, "reserved_sse": 1, "reserved_ws": 0}
    finally:
        held.release()


@pytest.mark.parametrize("end", ["success", "query_error", "cancel"])
async def test_native_events_releases_sse_after_query_or_response_exit(gateway, monkeypatch, end):
    entered = asyncio.Event()

    async def events(**_kwargs):
        assert gateway.admission.snapshot()["reserved_sse"] == 1
        entered.set()
        if end == "query_error":
            raise DurableRunNotFound("run missing")
        if end == "cancel":
            await asyncio.Event().wait()
        return [], True

    monkeypatch.setattr(completions.queries, "owned_events", events)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(gateway.app), base_url="http://test") as client:
        task = asyncio.create_task(client.get("/v1/runs/run-1/events"))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            if end == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                response = await asyncio.wait_for(task, 2)
                assert response.status_code == (404 if end == "query_error" else 200)
                if end == "success":
                    assert response.headers["content-type"].startswith("text/event-stream")
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert gateway.admission.snapshot() == {"reserved_requests": 0, "reserved_sse": 0, "reserved_ws": 0}


@pytest.mark.parametrize("end", ["disconnect", "error", "cancel"])
async def test_native_event_stream_itself_owns_sse_release(gateway, monkeypatch, end):
    # Invoke the decorated real route without middleware: cleanup must belong to
    # the streaming response too, not be rescued solely by middleware finally.
    entered = asyncio.Event()
    finish = asyncio.Event()
    calls = 0

    async def events(**_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return [], False
        entered.set()
        await finish.wait()
        raise RuntimeError("poll failed")

    monkeypatch.setattr(completions.queries, "owned_events", events)
    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
             "method": "GET", "path": "/v1/runs/run-1/events", "headers": [],
             "lumen.api_admission": gateway.admission}
    response = await completions.run_events(
        run_id="run-1", request=Request(scope), after_seq=None, last_event_id=None,
        token_info={"user_id": "owner", "project_id": "project"},
    )
    assert gateway.admission.snapshot()["reserved_sse"] == 1
    disconnected = asyncio.Event()

    async def receive():
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(_message):
        pass

    task = asyncio.create_task(response(scope, receive, send))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert gateway.admission.snapshot()["reserved_sse"] == 1
        if end == "disconnect":
            disconnected.set()
            await asyncio.wait_for(task, 2)
        elif end == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            finish.set()
            with pytest.raises(RuntimeError, match="poll failed"):
                await asyncio.wait_for(task, 2)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert gateway.admission.snapshot()["reserved_sse"] == 0


@asynccontextmanager
async def _uvicorn_loopback(app):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.setblocking(False)
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=port, lifespan="off", ws="websockets",
        log_level="error", timeout_graceful_shutdown=2,
    ))
    # Do not call run(): serve() stays on the pytest-owned event loop.
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if task.done():
                    await task
                    raise AssertionError("Uvicorn exited before startup")
                await asyncio.sleep(0.01)
        yield f"ws://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, 5)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            listener.close()


@pytest.mark.parametrize("path", _PATHS)
@pytest.mark.parametrize("reason", ["full", "draining"])
async def test_real_uvicorn_websocket_http503_denial_extension(gateway, socket_app, path, reason):
    held = gateway.admission.reserve("ws") if reason == "full" else None
    gateway.state.draining = reason == "draining"
    try:
        async with _uvicorn_loopback(socket_app) as base:
            with pytest.raises(InvalidStatus) as denied:
                async with websockets.connect(base + path, additional_headers=_HEADERS,
                                              proxy=None, open_timeout=2):
                    pytest.fail("overloaded or draining socket must never upgrade")
            response = denied.value.response
            assert response.status_code == 503  # Not handshake-close's implicit HTTP 403.
            assert response.headers["content-type"].startswith("application/json")
            assert json.loads(response.body) == {
                "detail": "API capacity exhausted" if reason == "full" else "guest draining"}
            if reason == "full":
                assert response.headers["retry-after"] == "1"
            _assert_no_durable_work(gateway)
            assert gateway.admission.snapshot()["reserved_ws"] == int(held is not None)
    finally:
        if held is not None:
            held.release()
    assert gateway.admission.snapshot() == {"reserved_requests": 0, "reserved_sse": 0, "reserved_ws": 0}
