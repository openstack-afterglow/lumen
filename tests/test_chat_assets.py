from __future__ import annotations

import asyncio
import hashlib
import io
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError
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
        chat_asset_s3_region = "default"
        chat_clamav_host = ""
        chat_asset_s3_server_side_encryption = "AES256"
        chat_clamav_port = 3310
        chat_asset_signed_url_ttl_seconds = 300
        chat_asset_s3_kms_key_id = ""

    monkeypatch.setattr(assets, "get_settings", lambda: Settings())
    assert assets.asset_pipeline_available() is False


def _configured_asset_settings(**overrides):
    values = {
        "chat_asset_s3_endpoint": "https://s3.example.test",
        "chat_asset_s3_bucket": "lumen-assets",
        "chat_asset_s3_access_key": "access",
        "chat_asset_s3_secret_key": "secret",
        "chat_asset_s3_region": "default",
        "chat_asset_s3_server_side_encryption": "none",
        "chat_asset_s3_kms_key_id": "",
        "chat_asset_signed_url_ttl_seconds": 300,
        "chat_clamav_host": "clamav",
        "chat_clamav_port": 3310,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_asset_config_accepts_explicit_unencrypted_ceph_mode(monkeypatch):
    monkeypatch.setattr(assets, "get_settings", lambda: _configured_asset_settings())

    config = assets._asset_config()

    assert config["region"] == "default"
    assert config["encryption"] == "none"


@pytest.mark.parametrize("encryption", ["", "AES192"])
def test_asset_config_rejects_missing_or_unknown_encryption(monkeypatch, encryption):
    monkeypatch.setattr(
        assets,
        "get_settings",
        lambda: _configured_asset_settings(chat_asset_s3_server_side_encryption=encryption),
    )

    with pytest.raises(assets.AssetUnavailable, match="storage or scanner is not configured"):
        assets._asset_config()


def test_asset_config_requires_kms_key(monkeypatch):
    monkeypatch.setattr(
        assets,
        "get_settings",
        lambda: _configured_asset_settings(chat_asset_s3_server_side_encryption="aws:kms"),
    )

    with pytest.raises(assets.AssetUnavailable, match="KMS key is not configured"):
        assets._asset_config()


def test_s3_client_uses_ceph_checksum_and_path_contract(monkeypatch):
    captured: dict[str, object] = {}
    sentinel = object()

    def create_client(service_name, **kwargs):
        captured["service_name"] = service_name
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(assets.boto3, "client", create_client)

    result = assets._s3_client(
        {
            "endpoint": "https://s3.example.test",
            "access_key": "access",
            "secret_key": "secret",
            "region": "default",
        }
    )

    assert result is sentinel
    assert captured["service_name"] == "s3"
    assert captured["region_name"] == "default"
    config = captured["config"]
    assert config.signature_version == "s3v4"
    assert config.s3 == {"addressing_style": "path"}
    assert config.request_checksum_calculation == "when_required"
    assert config.response_checksum_validation == "when_required"


def test_project_bucket_name_is_stable_bounded_and_does_not_expose_project_id():
    first = assets.project_bucket_name("lumen-chat-assets", "project-secret-123")
    second = assets.project_bucket_name("lumen-chat-assets", "project-secret-123")
    other = assets.project_bucket_name("lumen-chat-assets", "project-other")

    assert first == second
    assert first != other
    assert first.startswith("lumen-chat-assets-")
    assert "project-secret-123" not in first
    assert len(first) <= 63


def test_put_object_creates_missing_project_bucket_before_upload(tmp_path: Path, monkeypatch):
    path = tmp_path / "image.png"
    path.write_bytes(b"png")
    calls: list[tuple[str, str]] = []

    class Client:
        def head_bucket(self, *, Bucket):
            calls.append(("head", Bucket))
            if calls.count(("head", Bucket)) == 1:
                raise ClientError(
                    {
                        "Error": {"Code": "NoSuchBucket"},
                        "ResponseMetadata": {"HTTPStatusCode": 404},
                    },
                    "HeadBucket",
                )

        def create_bucket(self, *, Bucket):
            calls.append(("create", Bucket))

        def upload_fileobj(self, source, bucket, key, *, ExtraArgs):
            assert source.read() == b"png"
            assert ExtraArgs["ServerSideEncryption"] == "AES256"
            calls.append(("upload", f"{bucket}/{key}"))

    monkeypatch.setattr(assets, "_s3_client", lambda _config: Client())
    inspected = assets.InspectedAsset("image/png", 3, "a" * 64, {"width": 1, "height": 1}, "image.png")

    assets._put_object(
        path,
        config={"encryption": "AES256", "kms_key": ""},
        bucket="lumen-chat-assets-project",
        key="chat-assets/asset-1",
        asset=inspected,
    )

    assert calls == [
        ("head", "lumen-chat-assets-project"),
        ("create", "lumen-chat-assets-project"),
        ("head", "lumen-chat-assets-project"),
        ("upload", "lumen-chat-assets-project/chat-assets/asset-1"),
    ]


def test_put_object_omits_sse_header_only_for_explicit_none(tmp_path: Path, monkeypatch):
    path = tmp_path / "image.png"
    path.write_bytes(b"png")
    captured: dict[str, object] = {}

    class Client:
        def head_bucket(self, *, Bucket):
            assert Bucket == "lumen-chat-assets-project"

        def upload_fileobj(self, source, bucket, key, *, ExtraArgs):
            captured.update(
                data=source.read(),
                bucket=bucket,
                key=key,
                extra_args=ExtraArgs,
            )

    monkeypatch.setattr(assets, "_s3_client", lambda _config: Client())
    inspected = assets.InspectedAsset("image/png", 3, "a" * 64, {"width": 1, "height": 1}, "image.png")

    assets._put_object(
        path,
        config={"encryption": "none", "kms_key": ""},
        bucket="lumen-chat-assets-project",
        key="chat-assets/asset-1",
        asset=inspected,
    )

    assert captured == {
        "data": b"png",
        "bucket": "lumen-chat-assets-project",
        "key": "chat-assets/asset-1",
        "extra_args": {"ContentType": "image/png"},
    }


@pytest.mark.asyncio
async def test_open_download_reads_the_owned_project_bucket(monkeypatch):
    asset = SimpleNamespace(
        status="clean",
        bucket_name="lumen-chat-assets-project",
        object_key="chat-assets/asset-1",
        original_name="report.csv",
        mime_type="text/csv",
        size_bytes=22,
    )
    body = io.BytesIO(b"name,value\nlatency,12\n")

    async def owned_asset(**kwargs):
        assert kwargs == {
            "asset_id": "asset-1",
            "user_id": "user-1",
            "project_id": "project-1",
        }
        return asset

    class Client:
        def get_object(self, **kwargs):
            assert kwargs == {
                "Bucket": "lumen-chat-assets-project",
                "Key": "chat-assets/asset-1",
            }
            return {"Body": body, "ContentLength": 22}

    monkeypatch.setattr(assets, "_owned_asset", owned_asset)
    monkeypatch.setattr(assets, "_asset_config", lambda: {"bucket": "legacy-assets"})
    monkeypatch.setattr(assets, "_s3_client", lambda _config: Client())

    download = await assets.open_download(
        asset_id="asset-1",
        user_id="user-1",
        project_id="project-1",
    )
    content = b"".join([chunk async for chunk in download.chunks()])

    assert content == b"name,value\nlatency,12\n"
    assert download.name == "report.csv"
    assert body.closed


def test_generated_file_inspection_accepts_bounded_download_only_content(tmp_path: Path):
    path = tmp_path / "report.csv"
    path.write_text("name,value\nlatency,12\n")

    inspected = assets.inspect_generated_file(
        path,
        original_name="../../report.csv",
        media_type="text/csv",
    )

    assert inspected.original_name == "_.._report.csv"
    assert inspected.mime_type == "text/csv"
    assert inspected.size_bytes == path.stat().st_size
    assert len(inspected.sha256) == 64


@pytest.mark.parametrize(
    ("media_type", "payload"),
    [
        ("application/octet-stream", b"opaque"),
        ("text/plain", b"\xff\xfe"),
        ("application/json", b"{not-json}"),
    ],
)
def test_generated_file_inspection_rejects_disallowed_or_invalid_content(
    tmp_path: Path,
    media_type: str,
    payload: bytes,
):
    path = tmp_path / "unsafe-output"
    path.write_bytes(payload)

    with pytest.raises(assets.AssetError):
        assets.inspect_generated_file(
            path,
            original_name="unsafe-output",
            media_type=media_type,
        )


@pytest.mark.asyncio
async def test_generated_bytes_use_the_scanned_asset_pipeline_and_remove_temporary_file(monkeypatch):
    captured: dict[str, object] = {}

    async def create_generated_asset(**kwargs):
        path = kwargs["path"]
        captured.update(kwargs, data=path.read_bytes())
        return {"id": "asset-1"}

    monkeypatch.setattr(assets, "create_generated_asset", create_generated_asset)
    result = await assets.create_generated_asset_bytes(
        data=b"name,value\nlatency,12\n",
        original_name="report.csv",
        media_type="text/csv",
        user_id="user-1",
        project_id="project-1",
        run_id="run-1",
    )

    assert result == {"id": "asset-1"}
    assert captured["data"] == b"name,value\nlatency,12\n"
    assert captured["original_name"] == "report.csv"
    assert captured["media_type"] == "text/csv"
    assert captured["user_id"] == "user-1"
    assert captured["project_id"] == "project-1"
    assert captured["run_id"] == "run-1"
    assert not captured["path"].exists()


@pytest.mark.asyncio
async def test_link_output_assets_requires_clean_owned_asset_and_adds_run_link():
    asset = SimpleNamespace(
        id="asset-1",
        user_id="user-1",
        project_id="project-1",
        status="clean",
    )

    class Result:
        def __init__(self, values):
            self.values = values

        def scalars(self):
            return self

        def all(self):
            return self.values

    class Session:
        def __init__(self):
            self.results = iter((Result([asset]), Result([])))
            self.added = []

        async def execute(self, _query):
            return next(self.results)

        def add(self, row):
            self.added.append(row)

    session = Session()
    run = SimpleNamespace(id="run-1", user_id="user-1", project_id="project-1")

    await assets.link_output_assets_in_transaction(session, run=run, asset_ids=["asset-1"])

    assert len(session.added) == 1
    assert session.added[0].run_id == "run-1"
    assert session.added[0].asset_id == "asset-1"
    assert session.added[0].purpose == "output"


@pytest.mark.asyncio
async def test_link_output_assets_rejects_foreign_asset():
    asset = SimpleNamespace(
        id="asset-1",
        user_id="other-user",
        project_id="project-1",
        status="clean",
    )

    class Result:
        def scalars(self):
            return self

        def all(self):
            return [asset]

    class Session:
        async def execute(self, _query):
            return Result()

    with pytest.raises(assets.AssetError, match="clean owned asset"):
        await assets.link_output_assets_in_transaction(
            Session(),
            run=SimpleNamespace(id="run-1", user_id="user-1", project_id="project-1"),
            asset_ids=["asset-1"],
        )


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


def test_download_route_streams_owned_asset_without_cross_origin_redirect(asset_client, monkeypatch):
    async def open_download(**kwargs):
        assert kwargs == {
            "asset_id": "f6d18ec7-c8d8-4db8-9bd7-2dceb0f0f68e",
            "user_id": "user-1",
            "project_id": "project-1",
        }
        return assets.AssetDownload(
            body=io.BytesIO(b"name,value\nlatency,12\n"),
            name="분석 report.csv",
            mime_type="text/csv",
            size_bytes=22,
        )

    monkeypatch.setattr(asset_api.assets, "open_download", open_download)
    response = asset_client.get("/api/v1/chat/assets/f6d18ec7-c8d8-4db8-9bd7-2dceb0f0f68e/download")

    assert response.status_code == 200
    assert response.content == b"name,value\nlatency,12\n"
    assert response.headers["content-type"] == "text/csv; charset=utf-8"
    assert response.headers["content-length"] == "22"
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["content-disposition"].endswith("filename*=UTF-8''%EB%B6%84%EC%84%9D%20report.csv")


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
