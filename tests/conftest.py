"""Isolated Lumen service test fixtures."""

import os
from unittest.mock import MagicMock

os.environ.setdefault("LUMEN_ENCRYPTION_KEY", "0123456789abcdef" * 4)
os.environ.setdefault("CORS_ORIGINS", "http://localhost:3080")

import fakeredis.aioredis
import pytest
from httpx import ASGITransport, AsyncClient

from lumen.auth import get_os_conn, get_principal, require_token
from lumen.config import get_settings
from lumen.main import app


class _LegacyChatPathAdapter:
    """Exercise Lumen handlers through the BFF path while keeping Lumen standalone."""

    def __init__(self, application):
        self.application = application

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            path = scope["path"]
            if path == "/api/v1/chat" or path.startswith("/api/v1/chat/"):
                rewritten = (
                    "/v1/chat/models" if path == "/api/v1/chat/models" else "/v1" + path.removeprefix("/api/v1/chat")
                )
                scope = {**scope, "path": rewritten, "raw_path": rewritten.encode("ascii")}
        await self.application(scope, receive, send)


@pytest.fixture(autouse=True)
async def _reset_lumen_state(monkeypatch):
    """Keep settings and Redis state isolated without a live infrastructure dependency."""
    get_settings.cache_clear()
    fake = fakeredis.aioredis.FakeRedis(decode_responses=False)
    monkeypatch.setattr("lumen.cache._client", fake)
    yield
    monkeypatch.setattr("lumen.cache._client", None)
    get_settings.cache_clear()


@pytest.fixture
def mock_conn():
    conn = MagicMock()
    conn._afterglow_token = "test-token"
    conn._afterglow_project_id = "test-project-123"
    conn._afterglow_user_id = "test-user-123"
    return conn


def _token_info(*, is_system_admin: bool) -> dict[str, object]:
    return {
        "token": "test-token",
        "user_id": "test-user-123",
        "username": "testuser",
        "project_id": "test-project-123",
        "project_name": "test-project",
        "roles": ["admin", "member"] if is_system_admin else ["member"],
        "is_system_admin": is_system_admin,
    }


async def _client_for(mock_conn, *, is_system_admin: bool):
    async def token_override():
        return _token_info(is_system_admin=is_system_admin)

    async def principal_override():
        return {
            **_token_info(is_system_admin=is_system_admin),
            "auth_type": "keystone",
            "api_key_id": None,
            "scopes": (),
            "source": "web",
        }

    async def conn_override():
        yield mock_conn

    app.dependency_overrides[require_token] = token_override
    app.dependency_overrides[get_os_conn] = conn_override
    app.dependency_overrides[get_principal] = principal_override
    try:
        async with AsyncClient(
            transport=ASGITransport(_LegacyChatPathAdapter(app)),
            base_url="http://test",
            headers={"X-Auth-Token": "test-token", "X-Project-Id": "test-project-123"},
        ) as client:
            yield client
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
async def client(mock_conn):
    async for result in _client_for(mock_conn, is_system_admin=False):
        yield result


@pytest.fixture
async def admin_client(mock_conn):
    async for result in _client_for(mock_conn, is_system_admin=True):
        yield result


@pytest.fixture
async def non_admin_client(mock_conn):
    async for result in _client_for(mock_conn, is_system_admin=False):
        yield result
