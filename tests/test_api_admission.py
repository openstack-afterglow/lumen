"""Entry admission, bounded receive, and lease cleanup (tests intentionally not run)."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from lumen.services.infrastructure.api_load import (
    ApiAdmission,
    ApiAdmissionMiddleware,
    ApiLoadMeter,
    ApiLoadMiddleware,
)


def policy(**overrides):
    values = dict(api_max_active_requests=1, api_max_sse_connections=1,
                  api_max_websocket_connections=1, api_max_body_bytes=64)
    values.update(overrides)
    return SimpleNamespace(**values)


def http_scope(path="/v1/messages", headers=()):
    return {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
            "method": "POST", "scheme": "http", "path": path, "raw_path": path.encode(),
            "query_string": b"", "headers": list(headers),
            "client": ("127.0.0.1", 1234), "server": ("guest", 80), "root_path": ""}


async def capture_call(app, scope, chunks=()):
    messages = []
    frames = iter(chunks)

    async def receive():
        return next(frames, {"type": "http.disconnect"})

    async def send(message):
        messages.append(message)

    await app(scope, receive, send)
    return messages


@pytest.mark.asyncio
@pytest.mark.parametrize("draining", [False, True])
async def test_entry_rejection_precedes_routing_body_auth_provider_and_run_admission(draining):
    state = SimpleNamespace(draining=False)
    admission = ApiAdmission(settings=lambda: policy(), state=state)
    lease = admission.reserve("requests")
    state.draining = draining
    side_effects = AsyncMock()
    receive = AsyncMock(side_effect=AssertionError("body must not be consumed"))
    sent = []

    async def send(message):
        sent.append(message)

    await ApiAdmissionMiddleware(side_effects, admission=admission)(http_scope(), receive, send)
    side_effects.assert_not_awaited()
    receive.assert_not_awaited()
    assert sent[0]["status"] == (503 if draining else 429)
    if not draining:
        assert (b"retry-after", b"1") in sent[0]["headers"]
    assert admission.snapshot()["reserved_requests"] == 1
    lease.release()
    lease.release()
    assert admission.snapshot()["reserved_requests"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/v1/health", "/v1/ready", "/v1/internal/drain"])
@pytest.mark.parametrize("draining", [False, True])
async def test_management_and_readiness_are_excluded_from_capacity_and_body_cap(path, draining):
    state = SimpleNamespace(draining=False)
    admission = ApiAdmission(settings=lambda: policy(api_max_body_bytes=1), state=state)
    lease = admission.reserve("requests")
    state.draining = draining
    meter = ApiLoadMeter()

    async def management(_scope, _receive, send):
        assert admission.snapshot()["reserved_requests"] == 1
        assert meter.active_requests == meter.active_sse == 0
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ready"})

    app = ApiAdmissionMiddleware(ApiLoadMiddleware(management, meter=meter), admission=admission)
    result = await capture_call(app, http_scope(path, [(b"content-length", b"9999")]))
    assert result[0]["status"] == 200
    assert admission.snapshot()["reserved_requests"] == 1
    lease.release()


@pytest.mark.asyncio
async def test_content_length_cap_rejects_before_route_or_receive():
    admission = ApiAdmission(settings=lambda: policy(api_max_body_bytes=4))
    route = AsyncMock()
    receive = AsyncMock(side_effect=AssertionError("cannot read an oversized body"))
    sent = []

    async def send(message):
        sent.append(message)

    await ApiAdmissionMiddleware(route, admission=admission)(
        http_scope(headers=[(b"content-length", b"5")]), receive, send)
    assert sent[0]["status"] == 413
    route.assert_not_awaited()
    receive.assert_not_awaited()
    assert admission.snapshot()["reserved_requests"] == 0


class Body(BaseModel):
    text: str


@pytest.mark.asyncio
@pytest.mark.parametrize("declared_length", [None, b"1"])
async def test_streamed_body_count_precedes_validated_route_side_effects(declared_length):
    body = b'{"text":"hello"}'
    admission = ApiAdmission(settings=lambda: policy(api_max_body_bytes=len(body) - 1))
    provider = AsyncMock()
    run_admission = AsyncMock()
    app = FastAPI()
    app.add_middleware(ApiAdmissionMiddleware, admission=admission)

    @app.post("/v1/messages")
    async def route(payload: Body):
        await provider(payload.text)
        await run_admission()
        return {"ok": True}

    headers = [(b"content-type", b"application/json")]
    if declared_length is not None:
        headers.append((b"content-length", declared_length))
    result = await capture_call(app, http_scope(headers=headers), [
        {"type": "http.request", "body": body[:5], "more_body": True},
        {"type": "http.request", "body": body[5:], "more_body": False},
    ])
    assert [item["status"] for item in result if item["type"] == "http.response.start"] == [413]
    assert json.loads(result[-1]["body"]) == {"detail": "request body too large"}
    provider.assert_not_awaited()
    run_admission.assert_not_awaited()
    assert admission.snapshot()["reserved_requests"] == 0


@pytest.mark.asyncio
async def test_body_exact_bound_and_existing_smaller_bounded_read_are_preserved():
    admission = ApiAdmission(settings=lambda: policy(api_max_body_bytes=8))
    app = FastAPI()
    app.add_middleware(ApiAdmissionMiddleware, admission=admission)

    @app.post("/v1/exact")
    async def exact(request: Request):
        return {"size": len(await request.body())}

    @app.post("/v1/smaller")
    async def smaller(request: Request):
        async for chunk in request.stream():
            if len(chunk) > 2:
                raise HTTPException(status_code=413, detail="smaller route limit")
        return {"ok": True}

    frames = [{"type": "http.request", "body": b"12345678", "more_body": False}]
    exact_result = await capture_call(app, http_scope("/v1/exact", [(b"content-length", b"8")]), frames)
    assert exact_result[0]["status"] == 200
    assert json.loads(exact_result[-1]["body"]) == {"size": 8}
    smaller_result = await capture_call(app, http_scope("/v1/smaller"), frames)
    assert smaller_result[0]["status"] == 413
    assert json.loads(smaller_result[-1]["body"]) == {"detail": "smaller route limit"}
    assert admission.snapshot()["reserved_requests"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("end", ["completion", "disconnect", "exception", "cancel", "send_exception"])
async def test_http_reservation_releases_exactly_once_on_every_exit_path(end):
    admission = ApiAdmission(settings=lambda: policy())
    entered = asyncio.Event()
    finish = asyncio.Event()
    connection_ended = asyncio.Event()

    async def app(_scope, receive, send):
        entered.set()
        await finish.wait()
        if end == "exception":
            raise RuntimeError("route failed")
        if end == "disconnect":
            assert (await receive())["type"] == "http.disconnect"
        else:
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})
        assert admission.snapshot()["reserved_requests"] == 1
        connection_ended.set()
        # Settlement after the final byte/disconnect remains part of admitted work.
        await asyncio.sleep(0)

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        if end == "send_exception" and message["type"] == "http.response.body":
            raise OSError("client disappeared")

    task = asyncio.create_task(ApiAdmissionMiddleware(app, admission=admission)(http_scope(), receive, send))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert admission.snapshot()["reserved_requests"] == 1
        if end == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            finish.set()
            if end in {"exception", "send_exception"}:
                with pytest.raises(RuntimeError if end == "exception" else OSError):
                    await task
            else:
                await asyncio.wait_for(connection_ended.wait(), 2)
                await task
    finally:
        finish.set()
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    assert admission.snapshot()["reserved_requests"] == 0
    lease = admission.reserve("requests")
    lease.release()
    lease.release()
    assert admission.snapshot()["reserved_requests"] == 0


@pytest.mark.asyncio
async def test_limit_reduction_and_drain_never_interrupt_admitted_http_work():
    limits = policy(api_max_active_requests=2)
    state = SimpleNamespace(draining=False)
    admission = ApiAdmission(settings=lambda: limits, state=state)
    entered = asyncio.Event()
    finish = asyncio.Event()

    async def app(_scope, _receive, send):
        entered.set()
        await finish.wait()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"completed"})

    gate = ApiAdmissionMiddleware(app, admission=admission)
    task = asyncio.create_task(capture_call(gate, http_scope()))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        limits.api_max_active_requests = 1
        assert (await capture_call(gate, http_scope()))[0]["status"] == 429
        state.draining = True
        assert (await capture_call(gate, http_scope()))[0]["status"] == 503
        assert not task.done()
    finally:
        finish.set()
        result = await task
    assert result[0]["status"] == 200
    assert admission.snapshot()["reserved_requests"] == 0


@pytest.mark.asyncio
async def test_loopback_envelope_adds_reservations_without_breaking_verified_load(monkeypatch):
    import lumen.main as main
    from lumen.services.infrastructure.guest_api import verified_load

    monkeypatch.setattr(main.app.state, "draining", False)
    monkeypatch.setattr(main, "get_registry", lambda: SimpleNamespace(ready=True))
    monkeypatch.setattr(main, "get_settings", lambda: SimpleNamespace(chat_checkpointer_postgres_url=""))
    monkeypatch.setattr("lumen.db.check_db", AsyncMock(return_value=True))
    lease = main.api_admission.reserve("sse")
    try:
        request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), app=main.app)
        response = await main.ready(request, include_load=True)
        body = json.loads(response.body)
        assert body["reserved_sse"] == 1
        assert body["reserved_requests"] == body["reserved_ws"] == 0
        assert body["active_sse"] == 0  # Reservation is not counted as actual streaming load.
        verified = verified_load(response.body)
        assert verified is not None
        assert set(verified) == {"active_requests", "active_sse", "active_ws", "ttft_samples",
                                 "p95_ttft_ms", "observed_at"}
        assert verified == {key: body[key] for key in verified}
    finally:
        lease.release()
