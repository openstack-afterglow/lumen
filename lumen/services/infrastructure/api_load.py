"""Measured public API concurrency and streaming first-text latency for dynamic replicas.

Only one public API process runs per managed Nova guest. This meter intentionally
excludes health/readiness probes and does not mistake SSE pings or run-stage events
for model tokens. It keeps at most one minute of bounded latency observations.
"""
from __future__ import annotations

import inspect
import json
import logging
import math
import re
import time
from collections import deque
from datetime import UTC, datetime
from functools import wraps

import anyio
from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse

logger = logging.getLogger(__name__)

_SESSION_WEBSOCKET = re.compile(r"/v1/chat/realtime/sessions/[^/]+/ws")
_EXCLUDED_PATHS = frozenset({"/v1/health", "/v1/ready", "/v1/internal/drain"})


def _settings():
    from lumen.config import get_settings

    return get_settings()


class ApiReservation:
    """An event-loop-local slot, with idempotent cleanup on overlapping exit paths."""

    def __init__(self, admission, kind: str):
        self.admission = admission
        self.kind = kind
        self.released = False

    def release(self) -> None:
        if not self.released:
            self.released = True
            self.admission._reserved[self.kind] -= 1


class ApiAdmission:
    """Independent reservations; measured load only starts when work actually flows."""

    def __init__(self, *, settings=_settings, state=None):
        self.settings = settings
        self.state = state
        self._reserved = {"requests": 0, "sse": 0, "ws": 0}

    def reserve(self, kind: str) -> ApiReservation:
        # Check and increment without awaiting: one process / one event loop per guest.
        if self.state is not None and self.state.draining:
            raise HTTPException(status_code=503, detail="guest draining")
        name = {"requests": "api_max_active_requests", "sse": "api_max_sse_connections",
                "ws": "api_max_websocket_connections"}[kind]
        if self._reserved[kind] >= getattr(self.settings(), name):
            raise HTTPException(
                status_code=503 if kind == "ws" else 429,
                detail="API capacity exhausted",
                headers={"Retry-After": "1"},
            )
        self._reserved[kind] += 1
        return ApiReservation(self, kind)

    def snapshot(self) -> dict[str, int]:
        return {"reserved_requests": self._reserved["requests"],
                "reserved_sse": self._reserved["sse"], "reserved_ws": self._reserved["ws"]}


# Routers can also be mounted without the public middleware (e.g. embedded apps).
# They still enforce real configured SSE/WS limits, never an unlimited bypass.
api_admission = ApiAdmission()


def _admission(connection):
    return connection.scope.get("lumen.api_admission", api_admission)


async def close_stream_iterator(iterator) -> None:
    """Await stream settlement even when the response's AnyIO scope is cancelled."""
    close = getattr(iterator, "aclose", None)
    if close is not None:
        with anyio.CancelScope(shield=True):
            await close()


def register_sse_resource(request: Request, resource) -> None:
    """Make the ASGI SSE owner close an eagerly opened provider stream.

    A route body generator that never starts (e.g. ``http.response.start`` send
    failed) cannot run its own cleanup; the reservation owner closes these before
    reporting the slot free. ``resource.aclose()`` must be idempotent.
    """
    request.scope.setdefault("lumen.sse_resources", []).append(resource)


async def close_sse_resources(scope) -> None:
    for resource in scope.pop("lumen.sse_resources", ()):
        try:
            await close_stream_iterator(resource)
        except Exception:
            logger.warning("SSE provider stream cleanup failed", exc_info=True)


class _ReservedStreamingResponse(StreamingResponse):
    def __init__(self, response: StreamingResponse, reservation: ApiReservation, *, release_on_exit: bool):
        super().__init__(response.body_iterator, status_code=response.status_code,
                         media_type=response.media_type, background=response.background)
        self.raw_headers = response.raw_headers
        self.reservation = reservation
        self.release_on_exit = release_on_exit

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            try:
                # A send failure/disconnect can leave an async generator suspended
                # at its last yield; explicitly settle it before reporting zero load.
                await close_stream_iterator(self.body_iterator)
            finally:
                try:
                    await close_sse_resources(scope)
                finally:
                    if self.release_on_exit:
                        self.reservation.release()


def admit_sse(endpoint):
    """Reserve after framework auth/schema validation, before the route does any I/O."""
    signature = inspect.signature(endpoint, eval_str=True)

    @wraps(endpoint)
    async def guarded(*args, **kwargs):
        arguments = signature.bind(*args, **kwargs).arguments
        body = arguments.get("body")
        if body is not None and not body.stream:
            return await endpoint(*args, **kwargs)
        request = arguments["request"]
        reservation = _admission(request).reserve("sse")
        middleware_owned = "lumen.http_reservation" in request.scope
        # Middleware is also the cleanup owner if response dispatch never starts.
        request.scope.setdefault("lumen.sse_reservations", []).append(reservation)
        try:
            response = await endpoint(*args, **kwargs)
            if isinstance(response, StreamingResponse):
                return _ReservedStreamingResponse(response, reservation, release_on_exit=not middleware_owned)
            if not middleware_owned:
                reservation.release()
            return response
        except BaseException:
            if not middleware_owned:
                reservation.release()
            raise

    guarded.__signature__ = signature
    return guarded


def admit_websocket(endpoint):
    """Hold the handshake slot before token consumption through the socket lifetime."""
    signature = inspect.signature(endpoint, eval_str=True)

    @wraps(endpoint)
    async def guarded(*args, **kwargs):
        websocket = signature.bind(*args, **kwargs).arguments["websocket"]
        try:
            reservation = websocket.scope.get("lumen.ws_reservation")
            owns_reservation = reservation is None
            if reservation is None:
                reservation = _admission(websocket).reserve("ws")
        except HTTPException as exc:
            if "websocket.http.response" not in websocket.scope.get("extensions", {}):
                raise RuntimeError("WebSocket admission requires websocket.http.response support") from exc
            response = JSONResponse(status_code=503, content={"detail": exc.detail}, headers=exc.headers)
            await websocket.send_denial_response(response)
            return
        try:
            await endpoint(*args, **kwargs)
        finally:
            if owns_reservation:
                reservation.release()

    guarded.__signature__ = signature
    return guarded


class ApiAdmissionMiddleware:
    """HTTP entry gate and bounded receive, outside routing/body parsing and metering."""

    def __init__(self, app, *, admission: ApiAdmission):
        self.app = app
        self.admission = admission

    async def __call__(self, scope, receive, send):
        scope["lumen.api_admission"] = self.admission
        path = scope.get("path", "")
        if scope["type"] == "websocket" and (
            path in {"/v1/realtime", "/v1beta/realtime"} or _SESSION_WEBSOCKET.fullmatch(path)
        ):
            try:
                reservation = self.admission.reserve("ws")
            except HTTPException as exc:
                if "websocket.http.response" not in scope.get("extensions", {}):
                    raise RuntimeError("WebSocket admission requires websocket.http.response support") from exc
                response = JSONResponse(status_code=503, content={"detail": exc.detail}, headers=exc.headers)
                await response(scope, receive, send)
                return
            scope["lumen.ws_reservation"] = reservation

            try:
                await self.app(scope, receive, send)
            finally:
                reservation.release()
            return
        if (scope["type"] == "http" and self.admission.state is not None
                and self.admission.state.draining and path not in _EXCLUDED_PATHS):
            await JSONResponse(status_code=503, content={"detail": "guest draining"})(scope, receive, send)
            return
        if (scope["type"] != "http" or not scope.get("path", "").startswith("/v1/")
                or scope.get("path") in _EXCLUDED_PATHS):
            await self.app(scope, receive, send)
            return
        try:
            reservation = self.admission.reserve("requests")
        except HTTPException as exc:
            await JSONResponse(status_code=exc.status_code, content={"detail": exc.detail},
                               headers=exc.headers)(scope, receive, send)
            return
        scope["lumen.http_reservation"] = reservation
        limit = self.admission.settings().api_max_body_bytes
        received = 0
        exceeded = False
        response_started = False

        def release():
            for sse in scope.get("lumen.sse_reservations", ()):
                sse.release()
            reservation.release()

        async def capped_receive():
            nonlocal received, exceeded
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    exceeded = True
                    raise HTTPException(status_code=413, detail="request body too large")
            return message

        async def guarded_send(message):
            nonlocal response_started
            # FastAPI's JSON parser can turn a receive error into 400; preserve 413.
            if not exceeded:
                if message["type"] == "http.response.start":
                    response_started = True
                await send(message)

        try:
            lengths = [value for key, value in scope.get("headers", ()) if key.lower() == b"content-length"]
            if any(value.strip().isdigit() and int(value) > limit for value in lengths):
                exceeded = True
            else:
                try:
                    await self.app(scope, capped_receive, guarded_send)
                except Exception:
                    if not exceeded:
                        raise
            if exceeded:
                if response_started:
                    # A route which starts responding before reading must close the partial
                    # response; ASGI cannot replace headers already sent to the client.
                    raise RuntimeError("request body limit exceeded after response started")
                await JSONResponse(status_code=413, content={"detail": "request body too large"})(scope, receive, send)
        finally:
            try:
                # Also covers a streaming response that was never dispatched at all.
                await close_sse_resources(scope)
            finally:
                release()


class ApiLoadMeter:
    def __init__(self) -> None:
        self.active_requests = 0
        self.active_sse = 0
        self.active_ws = 0
        self._first_text_ms: deque[tuple[float, int]] = deque(maxlen=1024)

    def observe_first_text(self, started: float) -> None:
        now = time.monotonic()
        self._first_text_ms.append((now, max(0, round((now - started) * 1000))))

    def snapshot(self) -> dict[str, int | str | None]:
        cutoff = time.monotonic() - 60
        while self._first_text_ms and self._first_text_ms[0][0] < cutoff:
            self._first_text_ms.popleft()
        samples = sorted(sample for _, sample in self._first_text_ms)
        return {
            "active_requests": self.active_requests,
            "active_sse": self.active_sse,
            "active_ws": self.active_ws,
            "observed_at": datetime.now(UTC).isoformat(),
            "p95_ttft_ms": samples[math.ceil(len(samples) * 0.95) - 1] if samples else None,
            "ttft_samples": len(samples),
        }


def _first_text(frame: bytes) -> bool:
    """Accept an actual streamed text delta, not a response header/ping/tool event."""
    event = None
    data = None
    for line in frame.splitlines():
        if line.startswith(b"event:"):
            event = line[6:].strip()
        elif line.startswith(b"data:"):
            data = line[5:].strip()
    if not data or data == b"[DONE]":
        return False
    try:
        payload = json.loads(data)
    except (UnicodeDecodeError, ValueError):
        return False
    if not isinstance(payload, dict):
        return False
    if event == b"part.delta":
        part = payload.get("payload")
        return isinstance(part, dict) and part.get("part_type") == "text" and bool(part.get("delta"))
    if event == b"response.output_text.delta" or payload.get("type") == "response.output_text.delta":
        return isinstance(payload.get("delta"), str) and bool(payload["delta"])
    if event == b"content_block_delta" or payload.get("type") == "content_block_delta":
        delta = payload.get("delta")
        return isinstance(delta, dict) and delta.get("type") == "text_delta" and bool(delta.get("text"))
    choices = payload.get("choices")
    return isinstance(choices, list) and any(
        isinstance(choice, dict) and isinstance(choice.get("delta"), dict)
        and isinstance(choice["delta"].get("content"), str) and bool(choice["delta"]["content"])
        for choice in choices
    )


class ApiLoadMiddleware:
    def __init__(self, app, *, meter: ApiLoadMeter):
        self.app = app
        self.meter = meter

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope["type"] == "websocket" and (
            path in {"/v1/realtime", "/v1beta/realtime"} or _SESSION_WEBSOCKET.fullmatch(path)
        ):
            self.meter.active_ws += 1
            try:
                await self.app(scope, receive, send)
            finally:
                self.meter.active_ws -= 1
            return
        if (scope["type"] != "http" or not scope.get("path", "").startswith("/v1/")
                or scope.get("path") in {"/v1/ready", "/v1/health", "/v1/internal/drain"}):
            await self.app(scope, receive, send)
            return
        started = time.monotonic()
        self.meter.active_requests += 1
        is_sse = False
        measure_text = False
        seen_text = False
        pending = bytearray()
        path = scope["path"]
        content_route = (scope.get("method") == "POST" and (
            "completions" in path or path in {"/v1/responses", "/v1/messages", "/v1/claude-gateway/v1/messages"}
        )) or (scope.get("method") == "GET" and path.startswith("/v1/runs/") and path.endswith("/events"))


        async def observe(message):
            nonlocal is_sse, measure_text, seen_text
            if message["type"] == "http.response.start":
                media = next((value for key, value in message.get("headers", ()) if key.lower() == b"content-type"), b"")
                is_sse = b"text/event-stream" in media.lower()
                if is_sse:
                    self.meter.active_sse += 1
                measure_text = content_route and is_sse and message.get("status", 500) < 400
            elif message["type"] == "http.response.body" and measure_text and not seen_text:
                chunk = message.get("body", b"")
                if len(pending) + len(chunk) > 32 * 1024:
                    pending.clear()  # A malformed oversized frame cannot consume unbounded memory.
                if len(chunk) <= 32 * 1024:
                    pending.extend(chunk)
                while b"\n\n" in pending:
                    frame, _, rest = pending.partition(b"\n\n")
                    pending[:] = rest
                    if _first_text(frame):
                        self.meter.observe_first_text(started)
                        seen_text = True
                        pending.clear()
                        break
            await send(message)

        try:
            await self.app(scope, receive, observe)
        finally:
            self.meter.active_requests -= 1
            if is_sse:
                self.meter.active_sse -= 1
