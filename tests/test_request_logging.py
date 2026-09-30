"""Operator access records must not expose request targets or credentials."""

import logging

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
