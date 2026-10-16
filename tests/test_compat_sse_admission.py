"""SSE admission against real compat routers; provider/run boundaries stay local."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import anyio
import httpx
import pytest
from fastapi import FastAPI

from lumen.api import claude_gateway
from lumen.api.compat import anthropic, openai, responses
from lumen.service_authority import SERVICE_CAPABILITIES
from lumen.services import api_key_store, openai_compat
from lumen.services import completion_api as core
from lumen.services.durable_runs import admission as run_admission
from lumen.services.infrastructure.api_load import ApiAdmission, ApiAdmissionMiddleware, ApiLoadMeter, ApiLoadMiddleware

pytestmark = pytest.mark.asyncio

_PROTOCOLS = ("chat", "responses", "anthropic", "lumen", "gateway")
_HEADERS = {"Authorization": "Bearer sk-afgl-admission"}
_GATEWAY_HEADERS = {"Authorization": "Bearer sk-afgl-gateway"}


def _request(protocol, *, stream=True):
    if protocol == "responses":
        return "/v1/responses", {"model": "test-model", "input": "hello", "stream": stream}
    body = {"model": "lumen" if protocol == "lumen" else "test-model",
            "messages": [{"role": "user", "content": "hello"}], "stream": stream}
    if protocol in {"anthropic", "gateway"}:
        body["max_tokens"] = 16
        return "/v1/claude-gateway/v1/messages" if protocol == "gateway" else "/v1/messages", body
    return "/v1/chat/completions", body


@pytest.fixture
def harness(monkeypatch):
    settings = SimpleNamespace(api_max_active_requests=32, api_max_sse_connections=1,
                               api_max_websocket_connections=1, api_max_body_bytes=65536)
    capacity = ApiAdmission(settings=lambda: settings, state=SimpleNamespace(draining=False))
    meter = ApiLoadMeter()
    app = FastAPI()
    for router in (openai.router, responses.router, anthropic.router):
        app.include_router(router, prefix="/v1")
    app.include_router(claude_gateway.public_router, prefix="/v1/claude-gateway")
    app.add_middleware(ApiLoadMiddleware, meter=meter)
    app.add_middleware(ApiAdmissionMiddleware, admission=capacity)
    attempts = []
    reserve = capacity.reserve

    def record_reservation(kind):
        attempts.append(kind)
        return reserve(kind)

    monkeypatch.setattr(capacity, "reserve", record_reservation)
    monkeypatch.setattr("lumen.auth.get_settings", lambda: SimpleNamespace(chat_api_hosts=""))

    async def verify_key(key):
        if key == "sk-afgl-invalid":
            return None
        return {"user_id": "user", "project_id": "project", "api_key_id": 7,
                "roles": ["member", *SERVICE_CAPABILITIES], "service_system_admin": False,
                "credential_kind": "claude_gateway" if key == "sk-afgl-gateway" else "api_key",
                "scopes": ("models:read",) if key == "sk-afgl-no-scope" else ("compat:completions:write",)}

    monkeypatch.setattr(api_key_store, "verify_key", verify_key)
    monkeypatch.setattr(claude_gateway.gateway, "configured_route", lambda: ("test-model", "anthropic"))
    calls = []
    started = asyncio.Event()
    finish = asyncio.Event()
    stream_closed = asyncio.Event()
    cleanup_started = asyncio.Event()
    cleanup_finished = asyncio.Event()
    control = SimpleNamespace(block=False, stream_error=False, endpoint_error=False,
                              cleanup_block=False, settlement_error=False)

    def boundary(name):
        calls.append((name, capacity.snapshot()["reserved_sse"]))

    async def resolve(model, **_kwargs):
        boundary("resolve")
        if control.endpoint_error:
            raise RuntimeError("resolution failed")
        return {"model_name": model, "api_model_name": model, "provider_name": "openai"}

    async def precheck(*_args, **_kwargs):
        boundary("precheck")

    async def events(protocol):
        boundary("stream")
        try:
            if protocol == "chat":
                yield {"type": "delta", "content": "hello"}
            elif protocol == "responses":
                yield {"type": "response.output_text.delta", "delta": "hello"}
            elif protocol == "anthropic":
                yield {"type": "content_block_delta", "index": 0,
                       "delta": {"type": "text_delta", "text": "hello"}}
            else:
                yield {"kind": "chunk", "data": {"object": "chat.completion.chunk",
                       "choices": [{"index": 0, "delta": {"content": "hello"}, "finish_reason": None}]}}
            started.set()
            if control.block:
                await finish.wait()
            if control.stream_error:
                raise RuntimeError("stream failed")
            if protocol == "responses":
                yield {"type": "response.completed", "response": {"status": "completed"}}
            elif protocol == "anthropic":
                yield {"type": "message_stop"}
            elif protocol == "lumen":
                yield {"kind": "done"}
        finally:
            with anyio.CancelScope(shield=True):
                cleanup_started.set()
                if control.cleanup_block:
                    await cleanup_finished.wait()
                stream_closed.set()
                if control.settlement_error:
                    raise RuntimeError("settlement failed")

    async def once(**_kwargs):
        boundary("nonstream")
        return {"model": "test-model", "content": "hello", "tool_calls": None,
                "finish_reason": "stop", "prompt_tokens": 1, "completion_tokens": 1}

    async def native_responses(**kwargs):
        boundary("provider_responses")
        return events("responses") if kwargs["stream"] else {"id": "resp_test", "object": "response", "status": "completed"}

    async def native_anthropic(**kwargs):
        boundary("provider_anthropic")
        if kwargs["stream"]:
            return events("anthropic")
        return {"id": "msg_test", "type": "message", "role": "assistant", "model": "test-model",
                "content": [{"type": "text", "text": "hello"}], "stop_reason": "end_turn",
                "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1}}

    async def create_temp_run(**_kwargs):
        boundary("create_temp_run")
        return SimpleNamespace(run_id="run-admission")

    async def summary_route(resolved):
        return resolved

    # Keep the virtual route's real normalization, resolution, precheck and durable
    # admission chain. Only its immutable snapshots and DB boundary are replaced.
    monkeypatch.setattr(openai_compat, "get_configured_default_model", lambda: "test-model")
    monkeypatch.setattr(openai_compat, "get_execution_protocol_version", lambda: 1)
    monkeypatch.setattr(openai_compat, "resolve_summary_route", summary_route)
    monkeypatch.setattr(openai_compat, "_run_snapshots", lambda *_args, **_kwargs: ({}, {}))
    monkeypatch.setattr(run_admission, "create_temp_run", create_temp_run)
    monkeypatch.setattr(openai_compat, "execute_lumen_stream", lambda **_kwargs: events("lumen"))
    monkeypatch.setattr(openai_compat, "execute_lumen_nonstream", once)
    monkeypatch.setattr(core, "resolve", resolve)
    monkeypatch.setattr(core, "resolve_api", resolve)
    monkeypatch.setattr(core, "precheck", precheck)
    monkeypatch.setattr(core, "complete_stream", lambda **_kwargs: events("chat"))
    monkeypatch.setattr(core, "complete_once", once)
    monkeypatch.setattr(core, "complete_responses", native_responses)
    monkeypatch.setattr(core, "complete_anthropic", native_anthropic)
    return SimpleNamespace(app=app, capacity=capacity, meter=meter, attempts=attempts,
                           calls=calls, started=started, finish=finish, closed=stream_closed,
                           control=control, settings=settings, cleanup_started=cleanup_started,
                           cleanup_finished=cleanup_finished)


async def _post(harness, protocol, *, stream=True, headers=None, body=None):
    path, default_body = _request(protocol, stream=stream)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=harness.app), base_url="http://test") as client:
        return await client.post(path, json=default_body if body is None else body,
                                 headers=(_GATEWAY_HEADERS if protocol == "gateway" else _HEADERS)
                                 if headers is None else headers)


def _start_stream(harness, protocol):
    """Drive the actual ASGI app without HTTPX buffering the stream to completion."""
    path, body = _request(protocol)
    incoming = asyncio.Queue()
    incoming.put_nowait({"type": "http.request", "body": json.dumps(body).encode(), "more_body": False})
    messages = []
    first_text = asyncio.Event()

    async def send(message):
        messages.append(message)
        if message["type"] == "http.response.body" and b"hello" in message.get("body", b""):
            first_text.set()

    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
             "http_version": "1.1", "method": "POST", "scheme": "http", "path": path,
             "raw_path": path.encode(), "query_string": b"", "root_path": "",
             "headers": [(b"host", b"test"), (b"content-type", b"application/json"),
                         (b"authorization", (_GATEWAY_HEADERS if protocol == "gateway" else _HEADERS)["Authorization"].encode())],
             "client": ("127.0.0.1", 1234), "server": ("test", 80)}
    task = asyncio.create_task(harness.app(scope, incoming.get, send))
    return SimpleNamespace(task=task, incoming=incoming, messages=messages, first_text=first_text)


async def _stop_stream(harness, stream):
    harness.finish.set()
    try:
        await asyncio.wait_for(stream.task, 2)
    finally:
        if not stream.task.done():
            stream.task.cancel()
            await asyncio.gather(stream.task, return_exceptions=True)


def _assert_idle(harness):
    assert harness.capacity.snapshot() == {"reserved_requests": 0, "reserved_sse": 0, "reserved_ws": 0}
    assert harness.meter.active_requests == harness.meter.active_sse == 0


@pytest.mark.parametrize("protocol", _PROTOCOLS)
async def test_saturation_rejects_before_resolution_precheck_provider_or_run_row(harness, protocol):
    harness.control.block = True
    existing = _start_stream(harness, protocol)
    try:
        await asyncio.wait_for(existing.first_text.wait(), 2)
        assert harness.capacity.snapshot()["reserved_sse"] == 1
        assert harness.meter.active_sse == harness.meter.active_requests == 1  # SSE is a subset, not two HTTP slots.
        assert harness.calls and all(reserved == 1 for _, reserved in harness.calls)
        if protocol == "lumen":
            assert sum(name == "create_temp_run" for name, _ in harness.calls) == 1
        before = list(harness.calls)
        # One occupied slot protects all compatibility protocols, not just itself.
        for rejected_protocol in _PROTOCOLS:
            response = await _post(harness, rejected_protocol)
            assert response.status_code == 429
            assert response.headers["Retry-After"] == "1"
            assert harness.calls == before
            assert harness.capacity.snapshot()["reserved_sse"] == 1
        assert harness.meter.active_sse == 1
    finally:
        await _stop_stream(harness, existing)
    _assert_idle(harness)


@pytest.mark.parametrize("protocol", _PROTOCOLS)
@pytest.mark.parametrize("occupied", [False, True])
@pytest.mark.parametrize("rejection", ["missing_auth", "invalid_auth", "scope", "schema"])
async def test_auth_and_schema_reject_before_any_sse_reservation(harness, protocol, occupied, rejection):
    slot = harness.capacity.reserve("sse") if occupied else None
    _, body = _request(protocol)
    headers = _GATEWAY_HEADERS if protocol == "gateway" else _HEADERS
    expected = 401
    if rejection == "missing_auth":
        headers = {}
    elif rejection == "invalid_auth":
        headers = {"Authorization": "Bearer sk-afgl-invalid"}
    elif rejection == "scope":
        headers = {"Authorization": "Bearer sk-afgl-no-scope"}
        expected = 403
    else:
        expected = 422
        if protocol == "responses":
            del body["input"]
        elif protocol in {"anthropic", "gateway"}:
            body["max_tokens"] = 0
        else:
            body["messages"] = []
    before = harness.attempts.count("sse")
    try:
        response = await _post(harness, protocol, headers=headers, body=body)
        assert response.status_code == expected
        assert harness.attempts.count("sse") == before
        assert harness.calls == []
        assert harness.capacity.snapshot()["reserved_sse"] == int(occupied)
    finally:
        if slot is not None:
            slot.release()
    _assert_idle(harness)


@pytest.mark.parametrize("protocol", _PROTOCOLS)
async def test_nonstream_bypasses_sse_capacity_while_stream_is_open(harness, protocol):
    harness.control.block = True
    existing = _start_stream(harness, protocol)
    try:
        await asyncio.wait_for(existing.first_text.wait(), 2)
        before = harness.attempts.count("sse")
        response = await _post(harness, protocol, stream=False)
        assert response.status_code == 200
        assert "text/event-stream" not in response.headers["content-type"]
        assert harness.attempts.count("sse") == before
        assert harness.capacity.snapshot()["reserved_sse"] == harness.meter.active_sse == 1
        if protocol == "lumen":
            assert sum(name == "create_temp_run" for name, _ in harness.calls) == 2
    finally:
        await _stop_stream(harness, existing)
    _assert_idle(harness)


@pytest.mark.parametrize("protocol", _PROTOCOLS)
async def test_completion_releases_and_preserves_protocol_terminal_events(harness, protocol):
    response = await _post(harness, protocol)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "hello" in response.text
    terminal = {"chat": "data: [DONE]", "lumen": "data: [DONE]",
                "responses": "event: response.completed", "anthropic": "event: message_stop",
                "gateway": "event: message_stop"}[protocol]
    assert terminal in response.text
    if protocol != "chat":
        assert response.headers["Cache-Control"] == "no-cache, no-transform"
        assert response.headers["X-Accel-Buffering"] == "no"
    _assert_idle(harness)
    assert harness.meter.snapshot()["ttft_samples"] == 1
    # A second request proves the released capacity is reusable.
    assert (await _post(harness, protocol)).status_code == 200
    assert harness.meter.snapshot()["ttft_samples"] == 2
    _assert_idle(harness)


@pytest.mark.parametrize("protocol", _PROTOCOLS)
async def test_endpoint_exception_releases_before_response_dispatch(harness, protocol):
    harness.control.endpoint_error = True
    with pytest.raises(RuntimeError, match="resolution failed"):
        await _post(harness, protocol)
    assert harness.attempts.count("sse") == 1
    assert harness.calls == [("resolve", 1)]
    _assert_idle(harness)
    harness.control.endpoint_error = False
    assert (await _post(harness, protocol)).status_code == 200
    _assert_idle(harness)


@pytest.mark.parametrize("protocol", _PROTOCOLS)
async def test_precheck_error_response_releases_and_keeps_error_envelope(harness, monkeypatch, protocol):
    async def rejected(*_args, **_kwargs):
        assert harness.capacity.snapshot()["reserved_sse"] == 1
        raise core.CompletionError(403, "quota unavailable")

    monkeypatch.setattr(core, "precheck", rejected)
    response = await _post(harness, protocol)
    assert response.status_code == 403
    assert response.json()["error"]["message"] == "quota unavailable"
    if protocol in {"anthropic", "gateway"}:
        assert response.json()["type"] == "error"
        assert response.json()["error"]["type"] == "permission_error"
    assert not any(name.startswith("provider") or name in {"stream", "create_temp_run"} for name, _ in harness.calls)
    _assert_idle(harness)


@pytest.mark.parametrize("protocol", _PROTOCOLS)
@pytest.mark.parametrize("exit_path", ["stream_exception", "cancellation", "disconnect"])
async def test_stream_exit_releases_exactly_once(harness, protocol, exit_path):
    harness.control.block = True
    existing = _start_stream(harness, protocol)
    try:
        await asyncio.wait_for(existing.first_text.wait(), 2)
        assert harness.capacity.snapshot()["reserved_sse"] == 1
        if exit_path == "stream_exception":
            harness.control.stream_error = True
            harness.finish.set()
            if protocol in {"chat", "lumen"}:
                with pytest.raises((RuntimeError, BaseExceptionGroup)) as failure:
                    await asyncio.wait_for(existing.task, 2)
                errors = [failure.value]
                leaves = []
                while errors:
                    error = errors.pop()
                    if isinstance(error, BaseExceptionGroup):
                        errors.extend(error.exceptions)
                    else:
                        leaves.append(error)
                assert any(isinstance(error, RuntimeError) and str(error) == "stream failed" for error in leaves)
            else:
                await asyncio.wait_for(existing.task, 2)
                text = b"".join(message.get("body", b"") for message in existing.messages)
                assert b"event: error" in text
                assert b"upstream model error" in text
                assert b"stream failed" not in text
        elif exit_path == "cancellation":
            existing.task.cancel()
            harness.finish.set()
            with pytest.raises(asyncio.CancelledError):
                await existing.task
        else:
            existing.incoming.put_nowait({"type": "http.disconnect"})
            harness.finish.set()
            await asyncio.wait_for(existing.task, 2)
        _assert_idle(harness)
    finally:
        # Keep all provider/settlement cleanup request-owned and finish the test read.
        harness.control.stream_error = False
        harness.finish.set()
        if not existing.task.done():
            existing.task.cancel()
        await asyncio.gather(existing.task, return_exceptions=True)
        await asyncio.wait_for(harness.closed.wait(), 2)
    _assert_idle(harness)
    harness.control.block = False
    assert (await _post(harness, protocol)).status_code == 200
    _assert_idle(harness)


@pytest.mark.parametrize("protocol", _PROTOCOLS)
@pytest.mark.parametrize("exit_path", ["completion", "disconnect", "cancellation"])
async def test_private_load_remains_live_until_stream_settlement_finishes(harness, monkeypatch, protocol, exit_path):
    import lumen.main as main
    from lumen.services.infrastructure.guest_api import verified_load

    async def database_ready():
        return True

    monkeypatch.setattr(main, "api_load_meter", harness.meter)
    monkeypatch.setattr(main, "api_admission", harness.capacity)
    monkeypatch.setattr(main, "get_registry", lambda: SimpleNamespace(ready=True))
    monkeypatch.setattr(main, "get_settings", lambda: SimpleNamespace(chat_checkpointer_postgres_url=""))
    monkeypatch.setattr("lumen.db.check_db", database_ready)
    harness.control.block = True
    harness.control.cleanup_block = True
    existing = _start_stream(harness, protocol)
    try:
        await asyncio.wait_for(existing.first_text.wait(), 2)
        await asyncio.wait_for(harness.started.wait(), 2)
        if exit_path == "disconnect":
            existing.incoming.put_nowait({"type": "http.disconnect"})
        elif exit_path == "cancellation":
            existing.task.cancel()
        # Pending provider reads are still part of admitted work after disconnect.
        await asyncio.sleep(0)
        assert harness.meter.active_requests == harness.meter.active_sse == 1
        assert harness.capacity.snapshot()["reserved_sse"] == 1
        harness.finish.set()
        await asyncio.wait_for(harness.cleanup_started.wait(), 2)
        harness.capacity.state.draining = True
        request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"),
                                  app=SimpleNamespace(state=SimpleNamespace(draining=True, drain_fence=7)))
        response = await main.ready(request, include_load=True)
        load = verified_load(response.body)
        assert load is not None
        assert load["active_requests"] == load["active_sse"] == 1
        envelope = json.loads(response.body)
        assert envelope["reserved_requests"] == envelope["reserved_sse"] == 1
        assert envelope["draining"] is True
        assert not existing.task.done()
        harness.cleanup_finished.set()
        if exit_path == "cancellation":
            with pytest.raises(asyncio.CancelledError):
                await existing.task
        else:
            await asyncio.wait_for(existing.task, 2)
        _assert_idle(harness)
        response = await main.ready(request, include_load=True)
        assert verified_load(response.body)["active_requests"] == 0
    finally:
        harness.finish.set()
        harness.cleanup_finished.set()
        if not existing.task.done():
            existing.task.cancel()
        await asyncio.gather(existing.task, return_exceptions=True)


@pytest.mark.parametrize("protocol", ["responses", "anthropic", "gateway"])
async def test_response_start_failure_closes_eager_provider_stream_before_release(harness, monkeypatch, protocol):
    class EagerProviderStream:
        """An upstream opened before HTTP 200 whose route body never starts."""

        def __init__(self):
            self.closed_with = None

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise AssertionError("the response body must never start")

        async def aclose(self):
            await asyncio.sleep(0)  # cleanup may await; the slot must stay occupied meanwhile
            if self.closed_with is None:
                self.closed_with = (harness.capacity.snapshot(), harness.meter.active_requests)

    upstream = EagerProviderStream()

    async def eager(**kwargs):
        assert kwargs["stream"] is True
        return upstream

    monkeypatch.setattr(core, "complete_responses", eager)
    monkeypatch.setattr(core, "complete_anthropic", eager)
    path, body = _request(protocol)
    incoming = asyncio.Queue()
    incoming.put_nowait({"type": "http.request", "body": json.dumps(body).encode(), "more_body": False})

    async def send(message):
        if message["type"] == "http.response.start":
            raise OSError("client connection reset before response start")

    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
             "http_version": "1.1", "method": "POST", "scheme": "http", "path": path,
             "raw_path": path.encode(), "query_string": b"", "root_path": "",
             "headers": [(b"host", b"test"), (b"content-type", b"application/json"),
                         (b"authorization", (_GATEWAY_HEADERS if protocol == "gateway" else _HEADERS)["Authorization"].encode())],
             "client": ("127.0.0.1", 1234), "server": ("test", 80)}
    await asyncio.gather(harness.app(scope, incoming.get, send), return_exceptions=True)

    assert upstream.closed_with is not None
    reserved, active_requests = upstream.closed_with
    assert reserved["reserved_requests"] == reserved["reserved_sse"] == 1
    assert active_requests == 1
    _assert_idle(harness)
