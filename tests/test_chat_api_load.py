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
