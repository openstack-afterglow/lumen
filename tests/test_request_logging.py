"""Operator access records must not expose request targets or credentials."""

import logging

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from lumen.request_logging import RequestLoggingMiddleware


async def test_access_log_records_route_status_without_sensitive_request_data(caplog):
    app = FastAPI()
    app.add_middleware(RequestLoggingMiddleware)

    @app.post("/v1/conversations/{conversation_id}/completions")
    async def submit(conversation_id: str):
        return JSONResponse(status_code=202, content={"run_id": conversation_id})

    @app.get("/v1/fail/{conversation_id}")
    async def fail(conversation_id: str):
        raise RuntimeError("upstream Bearer sensitive-value")

    with caplog.at_level(logging.INFO, logger="lumen.request_logging"):
        async with AsyncClient(
            transport=ASGITransport(app, raise_app_exceptions=False), base_url="http://test"
        ) as client:
            accepted = await client.post(
                "/v1/conversations/secret-conversation/completions?ticket=secret-ticket",
                headers={"Authorization": "Bearer sensitive-value"},
            )
            missing = await client.get("/v1/secret-unknown?token=secret-token")
            failed = await client.get("/v1/fail/secret-conversation")

    assert [accepted.status_code, missing.status_code, failed.status_code] == [202, 404, 500]
    assert [record.getMessage() for record in caplog.records] == [
        "http request method=POST route=/v1/conversations/{conversation_id}/completions status=202",
        "http request method=GET route=<unmatched> status=404",
        "http request method=GET route=/v1/fail/{conversation_id} status=500",
    ]
    assert all(record.exc_info is None for record in caplog.records)
    assert not any(
        secret in caplog.text
        for secret in ("secret-conversation", "secret-ticket", "secret-unknown", "secret-token", "sensitive-value")
    )


async def test_access_log_distinguishes_incomplete_response_from_success(caplog):
    async def interrupted(scope, receive, send):
        await send({"type": "http.response.start", "status": 202, "headers": []})
        raise RuntimeError("private upstream response")

    middleware = RequestLoggingMiddleware(interrupted)
    scope = {"type": "http", "method": "POST", "path": "/v1/private-id?token=secret-token"}
    messages = []

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    with caplog.at_level(logging.INFO, logger="lumen.request_logging"):
        with pytest.raises(RuntimeError, match="private upstream response"):
            await middleware(scope, receive, send)

    assert messages[0]["status"] == 202
    assert [(record.getMessage(), record.exc_info) for record in caplog.records] == [
        ("http request method=POST route=<unmatched> status=incomplete", None),
    ]
    assert "private upstream response" not in caplog.text
    assert "secret-token" not in caplog.text
