from __future__ import annotations

import asyncio
import hashlib
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lumen.api import assets as asset_api
from lumen.auth import get_token_info
from lumen.services import assets


@pytest.fixture
def asset_client():
    app = FastAPI()
    app.include_router(asset_api.router, prefix="/api/v1/chat")
    app.dependency_overrides[get_token_info] = lambda: {"user_id": "user-1", "project_id": "project-1"}
    return TestClient(app)


def test_inspect_file_uses_bounded_child_and_sanitizes_display_name(tmp_path: Path, monkeypatch):
    path = tmp_path / "payload"
    path.write_bytes(b"image data")
    monkeypatch.setattr(assets, "_inspect_with_bounded_child", lambda _: ("image/png", {"width": 10, "height": 20}))

    inspected = assets.inspect_file(path, original_name=" ../unsafe\x00name.png ")

    assert inspected.mime_type == "image/png"
    assert inspected.original_name == "_unsafe_name.png"
    assert inspected.sha256 == hashlib.sha256(b"image data").hexdigest()


def test_inspector_runs_fixed_argv_with_resource_limited_child(tmp_path: Path, monkeypatch):
    path = tmp_path / "payload"
    path.write_bytes(b"x")
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout=b'{"mime_type":"image/png","metadata":{"width":1,"height":1}}')

    monkeypatch.setattr(assets.subprocess, "run", fake_run)
    assert assets._inspect_with_bounded_child(path) == ("image/png", {"width": 1, "height": 1})
    assert captured["argv"] == [assets.sys.executable, "-m", "lumen.services.asset_inspector", str(path)]
    assert captured["kwargs"]["timeout"] == 10
    assert "preexec_fn" not in captured["kwargs"]
    assert captured["kwargs"]["stderr"] is assets.subprocess.DEVNULL


def test_inspector_rejects_oversized_child_output(tmp_path: Path, monkeypatch):
    path = tmp_path / "payload"
    path.write_bytes(b"x")
    monkeypatch.setattr(
        assets.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=b"x" * (64 * 1024 + 1)),
    )

    with pytest.raises(assets.AssetError, match="파일 형식을 확인하지 못했습니다"):
        assets._inspect_with_bounded_child(path)


@pytest.mark.asyncio
async def test_inspection_runs_off_the_event_loop(tmp_path: Path, monkeypatch):
    path = tmp_path / "payload"
    path.write_bytes(b"x")

    def slow_inspect(*_args, **_kwargs):
        time.sleep(0.05)
        return assets.InspectedAsset("image/png", 1, "a" * 64, {"width": 1, "height": 1}, "x.png")

    monkeypatch.setattr(assets, "inspect_file", slow_inspect)
    inspection = asyncio.create_task(assets.inspect_file_async(path, original_name="x.png"))
    heartbeat = asyncio.Event()

    async def tick():
        await asyncio.sleep(0.001)
        heartbeat.set()

    tick_task = asyncio.create_task(tick())
    await asyncio.wait_for(heartbeat.wait(), timeout=0.02)
    await tick_task
    assert (await inspection).mime_type == "image/png"


def test_asset_pipeline_fails_closed_when_scanner_or_storage_is_missing(monkeypatch):
    class Settings:
        chat_asset_s3_endpoint = ""
        chat_asset_s3_bucket = ""
        chat_asset_s3_access_key = ""
        chat_asset_s3_secret_key = ""
        chat_clamav_host = ""
        chat_asset_s3_server_side_encryption = "AES256"
        chat_clamav_port = 3310
        chat_asset_signed_url_ttl_seconds = 300
        chat_asset_s3_kms_key_id = ""

    monkeypatch.setattr(assets, "get_settings", lambda: Settings())
    assert assets.asset_pipeline_available() is False


def test_upload_route_spools_then_delegates_to_scanned_pipeline(asset_client, monkeypatch):
    captured: dict[str, object] = {}

    async def create_uploaded_asset(*, path, original_name, user_id, project_id):
        captured.update(bytes=path.read_bytes(), original_name=original_name, user_id=user_id, project_id=project_id)
        return {
            "id": "f6d18ec7-c8d8-4db8-9bd7-2dceb0f0f68e",
            "name": "image.png",
            "mime_type": "image/png",
            "size_bytes": 3,
            "sha256": "a" * 64,
            "status": "clean",
            "media_metadata": {"width": 1, "height": 1},
            "created_at": None,
        }

    monkeypatch.setattr(asset_api.assets, "create_uploaded_asset", create_uploaded_asset)
    response = asset_client.post(
        "/api/v1/chat/assets",
        files={"file": ("image.png", b"png", "image/png")},
    )

    assert response.status_code == 201
    assert captured == {
        "bytes": b"png",
        "original_name": "image.png",
        "user_id": "user-1",
        "project_id": "project-1",
    }
    assert response.json()["status"] == "clean"


def test_download_route_redirects_only_after_owned_signed_url(asset_client, monkeypatch):
    async def signed_download_url(**kwargs):
        assert kwargs == {
            "asset_id": "f6d18ec7-c8d8-4db8-9bd7-2dceb0f0f68e",
            "user_id": "user-1",
            "project_id": "project-1",
        }
        return "https://assets.example.test/signed"

    monkeypatch.setattr(asset_api.assets, "signed_download_url", signed_download_url)
    response = asset_client.get(
        "/api/v1/chat/assets/f6d18ec7-c8d8-4db8-9bd7-2dceb0f0f68e/download",
        follow_redirects=False,
    )
    assert response.status_code == 307
    assert response.headers["location"] == "https://assets.example.test/signed"


@pytest.mark.asyncio
async def test_provider_input_materializes_owned_images_only_at_execution_time(monkeypatch):
    class Asset:
        id = "f6d18ec7-c8d8-4db8-9bd7-2dceb0f0f68e"
        status = "clean"
        mime_type = "image/png"

    async def owned_asset(**kwargs):
        assert kwargs["user_id"] == "user-1"
        assert kwargs["project_id"] == "project-1"
        return Asset()

    async def signed_url(**_kwargs):
        return "https://assets.example.test/signed-image"

    monkeypatch.setattr(assets, "_owned_asset", owned_asset)
    monkeypatch.setattr(assets, "signed_download_url", signed_url)

    content = await assets.provider_content_for_input_parts(
        [
            {"type": "text", "text": "describe"},
            {"type": "image", "asset_id": "f6d18ec7-c8d8-4db8-9bd7-2dceb0f0f68e"},
        ],
        user_id="user-1",
        project_id="project-1",
    )

    assert content == [
        {"type": "text", "text": "describe"},
        {"type": "image_url", "image_url": {"url": "https://assets.example.test/signed-image"}},
    ]


@pytest.mark.asyncio
async def test_provider_input_materializes_clean_pdf_only_when_document_gate_is_open(monkeypatch):
    class Asset:
        id = "f6d18ec7-c8d8-4db8-9bd7-2dceb0f0f68e"
        status = "clean"
        mime_type = "application/pdf"

    async def owned_asset(**_kwargs):
        return Asset()

    async def signed_url(**_kwargs):
        return "https://assets.example.test/signed-pdf"

    monkeypatch.setattr(assets, "_owned_asset", owned_asset)
    monkeypatch.setattr(assets, "signed_download_url", signed_url)

    content = await assets.provider_content_for_input_parts(
        [{"type": "document", "asset_id": "f6d18ec7-c8d8-4db8-9bd7-2dceb0f0f68e"}],
        user_id="user-1",
        project_id="project-1",
        allow_document=True,
    )

    assert content == [
        {
            "type": "file",
            "file": {"file_id": "https://assets.example.test/signed-pdf", "format": "application/pdf"},
        }
    ]


def test_asset_error_maps_cross_project_access_to_forbidden():
    assert asset_api._map_error(assets.AssetError("asset forbidden")).status_code == 403
