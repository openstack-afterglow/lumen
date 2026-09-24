"""Measured public API concurrency and streaming first-text latency for dynamic replicas.

Only one public API process runs per managed Nova guest. This meter intentionally
excludes health/readiness probes and does not mistake SSE pings or run-stage events
for model tokens. It keeps at most one minute of bounded latency observations.
"""
from __future__ import annotations

import json
import math
import time
from collections import deque


class ApiLoadMeter:
    def __init__(self) -> None:
        self.active_requests = 0
        self.active_sse = 0
        self._first_text_ms: deque[tuple[float, int]] = deque(maxlen=1024)

    def observe_first_text(self, started: float) -> None:
        now = time.monotonic()
        self._first_text_ms.append((now, max(0, round((now - started) * 1000))))

    def snapshot(self) -> dict[str, int | None]:
        cutoff = time.monotonic() - 60
        while self._first_text_ms and self._first_text_ms[0][0] < cutoff:
            self._first_text_ms.popleft()
        samples = sorted(sample for _, sample in self._first_text_ms)
        return {
            "active_requests": self.active_requests,
            "active_sse": self.active_sse,
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
        if (scope["type"] != "http" or not scope.get("path", "").startswith("/v1/")
                or scope.get("path") in {"/v1/ready", "/v1/health"}):
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
            "completions" in path or path in {"/v1/responses", "/v1/messages"}
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
