"""Guest-only drain authorization, fencing, readiness, and HTTP admission contracts."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

import lumen.main as main
from lumen.services.infrastructure.api_load import ApiAdmissionMiddleware, ApiLoadMiddleware
from lumen.services.infrastructure.guest_api import verified_load

TOKEN = "per-boot-test-drain-token"
DRAIN_HEADERS = {"X-Lumen-Drain-Token": TOKEN}


@pytest.fixture(autouse=True)
def guest_state(monkeypatch):
    monkeypatch.setattr(main.app.state, "internal_drain_token", TOKEN)
    monkeypatch.setattr(main.app.state, "draining", False)
    monkeypatch.setattr(main.app.state, "drain_fence", None)
    monkeypatch.setattr(main.api_load_meter, "active_requests", 0)
    monkeypatch.setattr(main.api_load_meter, "active_sse", 0)
    monkeypatch.setattr(main.api_load_meter, "active_ws", 0)

    async def healthy_database():
        return True

    monkeypatch.setattr("lumen.db.check_db", healthy_database)
    monkeypatch.setattr(main, "get_registry", lambda: SimpleNamespace(ready=True))
    monkeypatch.setattr(main, "get_settings", lambda: SimpleNamespace(chat_checkpointer_postgres_url=""))


def guest_client(host="127.0.0.1"):
    client_address = None if host is None else (host, 12345)
    return AsyncClient(
        transport=ASGITransport(app=main.app, client=client_address),
        base_url="http://guest",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("host", ["127.0.0.1", "::1"])
async def test_loopback_authenticated_drain_is_applied(host):
    async with guest_client(host) as client:
        response = await client.post("/v1/internal/drain", headers=DRAIN_HEADERS, json={"fence": 0})
    assert response.status_code == 200
    assert response.json() == {"draining": True, "drain_fence": 0, "drain_acknowledged": True}
    assert main.app.state.draining is True
    assert main.app.state.drain_fence == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("host", ["198.51.100.10", "localhost", "::ffff:127.0.0.1", None])
async def test_nonloopback_cannot_drain_even_with_token_and_forwarded_headers(host):
    headers = {
        **DRAIN_HEADERS,
        "X-Forwarded-For": "127.0.0.1",
        "Forwarded": "for=127.0.0.1",
    }
    async with guest_client(host) as client:
        response = await client.post("/v1/internal/drain", headers=headers, json={"fence": 3})
    assert response.status_code == 403
    assert main.app.state.draining is False
    assert main.app.state.drain_fence is None


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", [{}, {"X-Lumen-Drain-Token": ""}, {"X-Lumen-Drain-Token": "wrong"}])
async def test_missing_empty_or_wrong_token_never_drains(headers):
    async with guest_client() as client:
        response = await client.post("/v1/internal/drain", headers=headers, json={"fence": 3})
    assert response.status_code == 403
    assert main.app.state.draining is False
    assert main.app.state.drain_fence is None


@pytest.mark.asyncio
@pytest.mark.parametrize("configured_token", [None, ""])
@pytest.mark.parametrize("headers", [{}, {"X-Lumen-Drain-Token": ""}, DRAIN_HEADERS])
async def test_disabled_token_cannot_be_enabled_by_request_or_environment(monkeypatch, configured_token, headers):
    monkeypatch.setattr(main.app.state, "internal_drain_token", configured_token)
    monkeypatch.setenv("LUMEN_INTERNAL_DRAIN_TOKEN", TOKEN)
    async with guest_client() as client:
        response = await client.post("/v1/internal/drain", headers=headers, json={"fence": 0})
    assert response.status_code == 403
    assert main.app.state.draining is False
    assert main.app.state.drain_fence is None


@pytest.mark.asyncio
async def test_boot_token_is_constant_time_compared_and_not_rotated_from_environment(monkeypatch):
    compared = []
    compare_digest = main.hmac.compare_digest

    def capture_compare(actual, expected):
        compared.append((actual, expected))
        return compare_digest(actual, expected)

    monkeypatch.setattr(main.hmac, "compare_digest", capture_compare)
    monkeypatch.setenv("LUMEN_INTERNAL_DRAIN_TOKEN", "different-next-boot-token")
    async with guest_client() as client:
        response = await client.post("/v1/internal/drain", headers=DRAIN_HEADERS, json={"fence": 4})
    assert response.status_code == 200
    assert compared == [(TOKEN.encode(), TOKEN.encode())]
    assert TOKEN not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    {}, {"fence": -1}, {"fence": True}, {"fence": False}, {"fence": 1.0},
    {"fence": "1"}, {"fence": None}, [], None,
])
async def test_fence_requires_a_nonnegative_json_integer(body):
    async with guest_client() as client:
        response = await client.post("/v1/internal/drain", headers=DRAIN_HEADERS, json=body)
    assert response.status_code == 422
    assert main.app.state.draining is False
    assert main.app.state.drain_fence is None


@pytest.mark.asyncio
async def test_malformed_body_does_not_apply_drain():
    async with guest_client() as client:
        response = await client.post(
            "/v1/internal/drain", headers={**DRAIN_HEADERS, "Content-Type": "application/json"},
            content=b"{broken",
        )
    assert response.status_code == 422
    assert main.app.state.draining is False


@pytest.mark.asyncio
async def test_fence_is_monotonic_and_same_fence_replay_is_idempotent():
    async with guest_client() as client:
        for fence in (0, 7, 7, 12):
            response = await client.post("/v1/internal/drain", headers=DRAIN_HEADERS, json={"fence": fence})
            assert response.status_code == 200
            assert response.json()["drain_fence"] == fence
        stale = await client.post("/v1/internal/drain", headers=DRAIN_HEADERS, json={"fence": 11})
        assert stale.status_code == 409
        invalid = await client.post("/v1/internal/drain", headers=DRAIN_HEADERS, json={"fence": -1})
        assert invalid.status_code == 422
        unauthorized = await client.post("/v1/internal/drain", json={"fence": 13})
        assert unauthorized.status_code == 403
    assert main.app.state.draining is True
    assert main.app.state.drain_fence == 12


@pytest.mark.asyncio
async def test_public_readiness_is_unchanged_until_drain_and_hides_private_state():
    async with guest_client("198.51.100.10") as client:
        before = await client.get("/v1/ready")
    assert before.status_code == 200
    assert before.json() == {"status": "ok", "database": True, "plugins": True, "checkpointer": None}
    async with guest_client() as client:
        await client.post("/v1/internal/drain", headers=DRAIN_HEADERS, json={"fence": 9})
    async with guest_client("198.51.100.10") as client:
        after = await client.get("/v1/ready")
        health = await client.get("/v1/health")
    assert after.status_code == 503
    assert after.json() == {"status": "unavailable", "database": True, "plugins": True, "checkpointer": None}
    assert health.status_code == 200
    assert health.json() == {"status": "ok"}


@pytest.mark.asyncio
@pytest.mark.parametrize("host", ["198.51.100.10", "localhost", None])
async def test_load_details_remain_loopback_only_while_draining(host):
    async with guest_client() as client:
        await client.post("/v1/internal/drain", headers=DRAIN_HEADERS, json={"fence": 1})
    async with guest_client(host) as client:
        response = await client.get("/v1/ready?include_load=1", headers={"X-Forwarded-For": "127.0.0.1"})
    assert response.status_code == 403
    assert "active_requests" not in response.json()


@pytest.mark.asyncio
@pytest.mark.parametrize("host", ["127.0.0.1", "::1"])
@pytest.mark.parametrize("failure", ["database", "plugins", "checkpointer"])
@pytest.mark.parametrize("draining", [False, True])
async def test_local_load_is_fresh_and_real_even_when_dependencies_are_down(monkeypatch, host, failure, draining):
    async def unavailable_database():
        return failure != "database"

    monkeypatch.setattr("lumen.db.check_db", unavailable_database)
    monkeypatch.setattr(main, "get_registry", lambda: SimpleNamespace(ready=failure != "plugins"))
    if failure == "checkpointer":
        monkeypatch.setattr(main, "get_settings", lambda: SimpleNamespace(chat_checkpointer_postgres_url="configured"))
        monkeypatch.setattr("lumen.services.checkpointer.chat_checkpointer", SimpleNamespace(available=False))
    if draining:
        async with guest_client() as client:
            await client.post("/v1/internal/drain", headers=DRAIN_HEADERS, json={"fence": 8})
    # These are actual meter counters, not a fabricated snapshot or dependency fallback.
    monkeypatch.setattr(main.api_load_meter, "active_requests", 3)
    monkeypatch.setattr(main.api_load_meter, "active_sse", 2)
    monkeypatch.setattr(main.api_load_meter, "active_ws", 4)
    expected_load = main.api_load_meter.snapshot()
    started = datetime.now(UTC)
    async with guest_client(host) as client:
        response = await client.get("/v1/ready?include_load=1")
    finished = datetime.now(UTC)
    assert response.status_code == 200
    state = response.json()
    assert state["ready"] is False
    assert state["status"] == "unavailable"
    assert state["draining"] is draining
    assert state["drain_acknowledged"] is draining
    assert state["drain_fence"] == (8 if draining else None)
    for key, value in expected_load.items():
        if key != "observed_at":  # A fresh snapshot is taken per request; bounded below.
            assert state[key] == value
    observed_at = datetime.fromisoformat(state["observed_at"])
    assert observed_at.utcoffset().total_seconds() == 0
    assert started <= observed_at <= finished
    verified = verified_load(response.content)
    assert verified is not None
    assert verified["active_ws"] == 4


@pytest.mark.asyncio
async def test_healthy_local_readiness_reports_no_drain_and_live_snapshot():
    async with guest_client() as client:
        response = await client.get("/v1/ready?include_load=1")
    assert response.status_code == 200
    state = response.json()
    assert state["ready"] is True
    assert state["draining"] is False
    assert state["drain_fence"] is None
    assert state["drain_acknowledged"] is False
    assert state["active_requests"] == 0
    assert state["active_sse"] == 0
    assert state["active_ws"] == 0
    assert datetime.fromisoformat(state["observed_at"]).tzinfo is not None


@pytest.mark.asyncio
async def test_failed_snapshot_is_an_error_not_fabricated_zero_load(monkeypatch):
    def failed_snapshot():
        raise RuntimeError("counter snapshot unavailable")

    monkeypatch.setattr(main.api_load_meter, "snapshot", failed_snapshot)
    async with guest_client() as client:
        with pytest.raises(RuntimeError, match="counter snapshot unavailable"):
            await client.get("/v1/ready?include_load=1")


@pytest.mark.asyncio
async def test_http_admission_closes_but_existing_sse_finishes():
    started = asyncio.Event()
    release = asyncio.Event()
    messages = []

    async def streaming_app(_scope, _receive, send):
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"text/event-stream")]})
        await send({"type": "http.response.body", "body": b"data: live\n\n", "more_body": True})
        started.set()
        await release.wait()
        await send({"type": "http.response.body", "body": b"data: done\n\n", "more_body": False})

    async def capture(message):
        messages.append(message)

    middleware = ApiAdmissionMiddleware(
        ApiLoadMiddleware(streaming_app, meter=main.api_load_meter), admission=main.api_admission,
    )
    task = asyncio.create_task(middleware(
        {"type": "http", "path": "/v1/messages", "method": "POST"}, lambda: None, capture,
    ))
    try:
        await asyncio.wait_for(started.wait(), 2)
        async with guest_client() as client:
            drained = await client.post("/v1/internal/drain", headers=DRAIN_HEADERS, json={"fence": 5})
            assert drained.status_code == 200
            for method, path in (("GET", "/"), ("GET", "/v1/models"), ("POST", "/v1/messages")):
                rejected = await client.request(method, path)
                assert rejected.status_code == 503
                assert rejected.json() == {"detail": "guest draining"}
            load = (await client.get("/v1/ready?include_load=1")).json()
        assert load["active_requests"] == 1
        assert load["active_sse"] == 1
        assert task.done() is False
    finally:
        release.set()
        await task
    assert messages[-1]["body"] == b"data: done\n\n"
    assert main.api_load_meter.snapshot()["active_requests"] == 0
    assert main.api_load_meter.snapshot()["active_sse"] == 0


@pytest.mark.asyncio
async def test_public_readiness_observes_drain_applied_during_dependency_probe(monkeypatch):
    probe_started = asyncio.Event()
    release_probe = asyncio.Event()

    async def delayed_database():
        probe_started.set()
        await release_probe.wait()
        return True

    monkeypatch.setattr("lumen.db.check_db", delayed_database)
    async with guest_client() as client:
        probe = asyncio.create_task(client.get("/v1/ready"))
        try:
            await asyncio.wait_for(probe_started.wait(), 2)
            response = await client.post("/v1/internal/drain", headers=DRAIN_HEADERS, json={"fence": 6})
            assert response.status_code == 200
        finally:
            release_probe.set()
            readiness = await probe
    assert readiness.status_code == 503
    assert readiness.json()["status"] == "unavailable"


@pytest.mark.asyncio
async def test_public_dependency_failure_remains_unavailable_without_private_counters(monkeypatch):
    async def unavailable_database():
        return False

    monkeypatch.setattr("lumen.db.check_db", unavailable_database)
    async with guest_client("198.51.100.10") as client:
        response = await client.get("/v1/ready")
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable", "database": False, "plugins": True, "checkpointer": None}


@pytest.mark.asyncio
async def test_http_gate_passes_work_through_before_guest_drain():
    called = []
    messages = []

    async def admitted_app(scope, _receive, send):
        called.append(scope["path"])
        await send({"type": "http.response.start", "status": 201, "headers": []})
        await send({"type": "http.response.body", "body": b"admitted"})

    async def capture(message):
        messages.append(message)

    await ApiAdmissionMiddleware(admitted_app, admission=main.api_admission)(
        {"type": "http", "path": "/v1/messages", "method": "POST"}, lambda: None, capture,
    )
    assert called == ["/v1/messages"]
    assert messages[0]["status"] == 201
    assert messages[-1]["body"] == b"admitted"


def websocket_scope(path="/v1/realtime"):
    return {"type": "websocket", "path": path, "headers": [], "query_string": b"",
            "extensions": {"websocket.http.response": {}}}


@pytest.mark.asyncio
async def test_websocket_gate_passes_sockets_unchanged_before_drain():
    called = []
    sent = []

    async def socket_app(scope, receive, send):
        called.append(scope["path"])
        assert await receive() == {"type": "websocket.connect"}
        await send({"type": "websocket.accept"})

    async def connect():
        return {"type": "websocket.connect"}

    async def capture(message):
        sent.append(message)

    await ApiAdmissionMiddleware(socket_app, admission=main.api_admission)(websocket_scope(), connect, capture)
    assert called == ["/v1/realtime"]
    assert sent == [{"type": "websocket.accept"}]


def test_drain_gate_runs_before_load_meter_so_rejections_are_not_live_work():
    layers = [middleware.cls for middleware in main.app.user_middleware]
    assert layers.index(ApiAdmissionMiddleware) < layers.index(ApiLoadMiddleware)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/v1/realtime", "/v1beta/realtime", "/v1/chat/realtime/sessions/abc/ws"])
async def test_draining_rejects_new_websocket_before_accept_and_keeps_live_socket(path):
    called = []
    messages = []

    async def socket_app(scope, _receive, _send):
        called.append(scope["path"])

    async def connect():
        return {"type": "websocket.connect"}

    async def capture(message):
        messages.append(message)

    main.api_load_meter.active_ws = 1  # An accepted pre-drain socket remains counted.
    async with guest_client() as client:
        response = await client.post("/v1/internal/drain", headers=DRAIN_HEADERS, json={"fence": 2})
    assert response.status_code == 200
    await ApiAdmissionMiddleware(socket_app, admission=main.api_admission)(websocket_scope(path), connect, capture)
    assert called == []
    assert messages[0]["type"] == "websocket.http.response.start"
    assert messages[0]["status"] == 503
    assert messages[1]["type"] == "websocket.http.response.body"
    assert main.api_load_meter.snapshot()["active_ws"] == 1


@pytest.mark.parametrize(("raw", "expected"), [("0", 0), ("5", 5), ("0012", 12)])
def test_boot_drain_advisory_closes_admission_before_serving(monkeypatch, raw, expected):
    monkeypatch.setenv("LUMEN_INTERNAL_DRAIN_TOKEN", TOKEN)
    monkeypatch.setenv("LUMEN_INTERNAL_DRAIN_FENCE", raw)
    state = SimpleNamespace()
    main._initialize_guest_drain_state(state)
    assert state.internal_drain_token == TOKEN
    assert state.draining is True
    assert state.drain_fence == expected


@pytest.mark.parametrize("token", [None, "", TOKEN])
def test_boot_without_drain_advisory_is_open(monkeypatch, token):
    if token is None:
        monkeypatch.delenv("LUMEN_INTERNAL_DRAIN_TOKEN", raising=False)
    else:
        monkeypatch.setenv("LUMEN_INTERNAL_DRAIN_TOKEN", token)
    monkeypatch.delenv("LUMEN_INTERNAL_DRAIN_FENCE", raising=False)
    state = SimpleNamespace()
    main._initialize_guest_drain_state(state)
    assert state.internal_drain_token == (token or "")
    assert state.draining is False
    assert state.drain_fence is None


@pytest.mark.parametrize("raw", ["", "-1", "+5", " 5", "5 ", "5\n", "1.0", "٣", "５", "true"])
def test_invalid_boot_drain_advisory_refuses_to_start(monkeypatch, raw):
    monkeypatch.setenv("LUMEN_INTERNAL_DRAIN_TOKEN", TOKEN)
    monkeypatch.setenv("LUMEN_INTERNAL_DRAIN_FENCE", raw)
    state = SimpleNamespace()
    with pytest.raises(RuntimeError, match="invalid guest boot drain advisory"):
        main._initialize_guest_drain_state(state)
    assert vars(state) == {}


@pytest.mark.parametrize("token", [None, ""])
def test_boot_drain_advisory_requires_per_boot_token(monkeypatch, token):
    if token is None:
        monkeypatch.delenv("LUMEN_INTERNAL_DRAIN_TOKEN", raising=False)
    else:
        monkeypatch.setenv("LUMEN_INTERNAL_DRAIN_TOKEN", token)
    monkeypatch.setenv("LUMEN_INTERNAL_DRAIN_FENCE", "3")
    state = SimpleNamespace()
    with pytest.raises(RuntimeError, match="invalid guest boot drain advisory"):
        main._initialize_guest_drain_state(state)
    assert vars(state) == {}


@pytest.mark.asyncio
async def test_stalled_dependency_probe_is_bounded_and_keeps_local_counters(monkeypatch):
    cancelled = asyncio.Event()

    async def stalled_database():
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr("lumen.db.check_db", stalled_database)
    monkeypatch.setattr(main.api_load_meter, "active_requests", 2)
    monkeypatch.setattr(main.api_load_meter, "active_ws", 1)
    async with guest_client() as client:
        response = await asyncio.wait_for(client.get("/v1/ready?include_load=1"), 2.5)
    assert response.status_code == 200
    state = response.json()
    assert state["database"] is False
    assert state["ready"] is False
    assert cancelled.is_set()
    verified = verified_load(response.content)
    assert verified is not None
    assert verified["active_requests"] == 2
    assert verified["active_ws"] == 1
