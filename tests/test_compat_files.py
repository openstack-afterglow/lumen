from __future__ import annotations

import asyncio
import io
import stat
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from lumen.api.compat import files as api
from lumen.api.compat.openai import openai_error_response
from lumen.auth import get_principal
from lumen.services import assets, batch_files


@pytest.fixture
def env(monkeypatch):
    settings = SimpleNamespace(batch_enabled=True, batch_upload_slots=1, batch_jsonl_max_bytes=32)
    monkeypatch.setattr(api, "get_settings", lambda: settings)
    monkeypatch.setattr(api, "_upload_slots", None)
    monkeypatch.setattr(api, "_upload_capacity", None)
    app = FastAPI()
    app.include_router(api.router, prefix="/v1")
    principal = {"auth_type": "api_key", "user_id": "u", "project_id": "p", "api_key_id": 7,
                 "scopes": ("compat:files:read", "compat:files:write")}
    app.dependency_overrides[get_principal] = lambda: principal

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return openai_error_response(exc.status_code, str(exc.detail))

    return app, principal, settings


def view(file_id=None):
    return batch_files.BatchFileView(file_id or str(uuid.uuid4()), "batch", "input.jsonl", 3, "a" * 64,
                                    "processed", datetime.now(UTC), datetime.now(UTC) + timedelta(days=30))


@pytest.mark.asyncio
async def test_upload_is_bounded_0600_and_spool_removed(env, monkeypatch):
    app, principal, settings = env
    captured = {}

    async def create(**kwargs):
        captured.update(kwargs)
        assert stat.S_IMODE(kwargs["path"].stat().st_mode) == 0o600
        assert kwargs["path"].read_bytes() == b"{}\n"
        return view()

    monkeypatch.setattr(batch_files, "create_batch_file", create)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/v1/files", files={"file": ("input.jsonl", b"{}\n", "application/octet-stream")},
                                     data={"purpose": "batch"})
    assert response.status_code == 200
    body = response.json()
    assert body["id"].startswith("file-") and len(body["id"]) == 37
    assert body["object"] == "file" and body["purpose"] == "batch" and body["status"] == "processed"
    assert body["bytes"] == 3
    assert captured["project_id"] == "p" and captured["user_id"] == "u" and captured["api_key_id"] == 7
    assert not captured["path"].exists()


@pytest.mark.asyncio
async def test_content_length_precheck_precedes_parser_and_slot(env, monkeypatch):
    app, _, settings = env
    monkeypatch.setattr(api, "_UploadParser", lambda *a: pytest.fail("oversized multipart parsed"))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/v1/files", content=b"invalid", headers={"Content-Length": str(settings.batch_jsonl_max_bytes + api._OVERHEAD + 1)})
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_too_large"
    assert api._upload_slots is None


@pytest.mark.asyncio
async def test_hard_file_cap_with_absent_content_length(env, monkeypatch):
    app, _, settings = env
    monkeypatch.setattr(batch_files, "create_batch_file", lambda **k: pytest.fail("oversized bytes stored"))
    body = (b'--b\r\nContent-Disposition: form-data; name="purpose"\r\n\r\nbatch\r\n'
            b'--b\r\nContent-Disposition: form-data; name="file"; filename="f"\r\n\r\n' + b"x" * 33 + b"\r\n--b--\r\n")

    async def stream():
        for byte in body:
            yield bytes([byte])

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/v1/files", content=stream(), headers={"Content-Type": "multipart/form-data; boundary=b"})
    assert response.status_code == 413
    assert api._upload_slots._value == 1


@pytest.mark.asyncio
async def test_slot_exhaustion_returns_429_without_parsing_and_releases(env, monkeypatch):
    app, _, _ = env
    entered, release = asyncio.Event(), asyncio.Event()

    async def create(**kwargs):
        entered.set()
        await release.wait()
        return view()

    monkeypatch.setattr(batch_files, "create_batch_file", create)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        pending = asyncio.create_task(client.post("/v1/files", files={"file": ("f", b"{}\n")}, data={"purpose": "batch"}))
        await entered.wait()
        rejected = await client.post("/v1/files", content=b"not multipart")
        release.set()
        accepted = await pending
    assert rejected.status_code == 429
    assert rejected.headers["retry-after"] == "1"
    assert rejected.json()["error"]["code"] == "upload_slots_exhausted"
    assert accepted.status_code == 200
    assert api._upload_slots._value == 1


@pytest.mark.asyncio
async def test_free_space_precheck_refuses_before_parser(env, monkeypatch):
    app, _, _ = env
    monkeypatch.setattr(api.shutil, "disk_usage", lambda path: SimpleNamespace(free=0))
    monkeypatch.setattr(api.tempfile, "mkstemp", lambda **k: pytest.fail("spool allocated without capacity"))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/v1/files", files={"file": ("f", b"{}\n")}, data={"purpose": "batch"})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "batch_unavailable"
    assert api._upload_slots._value == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("purpose", ["fine-tune", "batch_output", "assistants"])
async def test_jsonl_upload_rejects_other_file_purposes(env, monkeypatch, purpose):
    app, _, _ = env
    monkeypatch.setattr(batch_files, "create_batch_file", lambda **k: pytest.fail("unsupported purpose stored"))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/v1/files", files={"file": ("f", b"{}\n")}, data={"purpose": purpose})
    assert response.status_code == 400
    assert "error" in response.json()


@pytest.mark.asyncio
async def test_list_dto_and_cursor_owner_parameters(env, monkeypatch):
    app, _, _ = env
    row = view()
    captured = {}

    async def listing(**kwargs):
        captured.update(kwargs)
        return [row], True

    monkeypatch.setattr(batch_files, "list_batch_files", listing)
    cursor = str(uuid.uuid4())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v1/files", params={"after": api._public_id(cursor), "purpose": "batch", "limit": 1, "order": "asc"})
    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "list" and body["has_more"] is True
    assert body["first_id"] == body["last_id"] == api._public_id(row.id)
    assert captured == {"project_id": "p", "user_id": "u", "purpose": "batch", "after": cursor, "limit": 1, "order": "asc"}


@pytest.mark.asyncio
async def test_retrieve_delete_and_content_owner_scoped_and_streamed(env, monkeypatch):
    app, principal, _ = env
    row = view()
    body = io.BytesIO(b"{}\n")
    calls = []

    async def owned(**kwargs):
        calls.append(kwargs)
        if kwargs["user_id"] != "u" or kwargs["project_id"] != "p":
            raise batch_files.BatchFileNotFound()
        return row

    async def content(**kwargs):
        await owned(**kwargs)
        return assets.AssetDownload(body, "input.jsonl", "application/jsonl", 3)

    monkeypatch.setattr(batch_files, "get_batch_file", owned)
    monkeypatch.setattr(batch_files, "delete_batch_file", owned)
    monkeypatch.setattr(batch_files, "open_batch_file_content", content)
    path = "/v1/files/" + api._public_id(row.id)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        retrieved = await client.get(path)
        downloaded = await client.get(path + "/content")
        deleted = await client.delete(path)
        principal["user_id"] = "foreign"
        foreign = await client.get(path)
        principal["user_id"], principal["project_id"] = "u", "foreign"
        foreign_project = await client.get(path + "/content")
        malformed = await client.get("/v1/files/not-a-file")
    assert retrieved.status_code == downloaded.status_code == deleted.status_code == 200
    assert downloaded.content == b"{}\n" and body.closed
    assert downloaded.headers["cache-control"] == "private, no-store"
    assert deleted.json() == {"id": api._public_id(row.id), "object": "file", "deleted": True}
    assert foreign.status_code == foreign_project.status_code == malformed.status_code == 404
    assert all(call["file_id"] == row.id for call in calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path", [("post", "/v1/files"), ("get", "/v1/files"), ("get", "/v1/files/file-" + "0" * 32),
    ("delete", "/v1/files/file-" + "0" * 32), ("get", "/v1/files/file-" + "0" * 32 + "/content")])
async def test_disabled_feature_returns_explicit_openai_error(env, method, path):
    app, _, settings = env
    settings.batch_enabled = False
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.request(method, path)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "batch_unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path,scope", [("get", "/v1/files", "compat:files:read"), ("post", "/v1/files", "compat:files:write")])
async def test_api_key_scope_is_checked_before_file_work(env, method, path, scope):
    app, principal, _ = env
    principal["scopes"] = ()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.request(method, path)
    assert response.status_code == 403
    assert scope in response.json()["error"]["message"]
