"""Owned, scanned object-store assets for canonical chat parts."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import socket
import struct
import subprocess
import sys
import tempfile
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from lumen.config import get_settings
from lumen.db import get_session_factory, is_db_available, mark_db_unhealthy
from lumen.models.chat_assets import ChatAsset, ChatMessageAsset, ChatRunAsset
from lumen.models.chat_contracts import UserAssetInputPart, UserTextInputPart, validate_user_input_parts
from lumen.services.conversation_store import ChatStorageUnavailable

logger = logging.getLogger(__name__)

_MAX_UPLOAD_BYTES = 100 * 1024 * 1024
_MAX_IMAGE_BYTES = 20 * 1024 * 1024
_MAX_IMAGE_PIXELS = 40_000_000
_MAX_PDF_BYTES = 32 * 1024 * 1024
_MAX_PDF_PAGES = 200
_MAX_AUDIO_BYTES = 25 * 1024 * 1024
_MAX_AUDIO_DURATION_MS = 30 * 60 * 1000
_MAX_VIDEO_BYTES = 100 * 1024 * 1024
_MAX_VIDEO_DURATION_MS = 10 * 60 * 1000
_MAX_GENERATED_FILE_BYTES = 5 * 1024 * 1024

_IMAGE_MIMES = {"image/jpeg", "image/png", "image/webp"}
_AUDIO_MIMES = {"audio/mpeg", "audio/wav", "audio/x-wav", "audio/mp4", "audio/ogg", "audio/webm"}
_VIDEO_MIMES = {"video/mp4", "video/webm"}
_ALLOWED_MIMES = _IMAGE_MIMES | _AUDIO_MIMES | _VIDEO_MIMES | {"application/pdf"}
_GENERATED_TEXT_MIMES = {
    "application/json",
    "text/csv",
    "text/markdown",
    "text/plain",
}
_ALLOWED_GENERATED_MIMES = _ALLOWED_MIMES | _GENERATED_TEXT_MIMES
_CONTROL_OR_PATH = re.compile(r"[\x00-\x1f\x7f/\\]+")
_BUCKET_BASE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
_MEDIA_TYPE = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*$")
_PROJECT_BUCKET_HASH_LENGTH = 20


class AssetError(ValueError):
    """A safe, client-visible asset pipeline failure."""


class AssetUnavailable(AssetError):
    """Scanner or object store configuration is incomplete."""


@dataclass(frozen=True)
class InspectedAsset:
    mime_type: str
    size_bytes: int
    sha256: str
    metadata: dict[str, int]
    original_name: str


@dataclass
class AssetDownload:
    """Owned object stream and immutable response metadata."""

    body: Any
    name: str
    mime_type: str
    size_bytes: int

    async def chunks(self) -> AsyncIterator[bytes]:
        try:
            while chunk := await asyncio.to_thread(self.body.read, 64 * 1024):
                if not isinstance(chunk, bytes):
                    raise AssetUnavailable("chat asset object storage returned invalid data")
                yield chunk
        finally:
            await asyncio.to_thread(self.body.close)


def _require_session_factory():
    if not is_db_available():
        raise ChatStorageUnavailable("chat DB 를 사용할 수 없습니다")
    factory = get_session_factory()
    if factory is None:
        raise ChatStorageUnavailable("chat DB 가 구성되지 않았습니다")
    return factory


def _sanitized_name(name: str | None) -> str:
    display = _CONTROL_OR_PATH.sub("_", (name or "asset").strip()).strip(" .")
    return (display or "asset")[:255]


def project_bucket_name(base: str, project_id: str) -> str:
    """Return a stable S3 bucket name without exposing the raw project identifier."""
    prefix = base.strip().lower().strip("-")
    if not prefix or not _BUCKET_BASE.fullmatch(prefix):
        raise AssetUnavailable("chat asset bucket base is invalid")
    prefix = prefix[: 63 - _PROJECT_BUCKET_HASH_LENGTH - 1].rstrip("-")
    if not prefix:
        raise AssetUnavailable("chat asset bucket base is invalid")
    digest = hashlib.sha256(project_id.encode("utf-8")).hexdigest()[:_PROJECT_BUCKET_HASH_LENGTH]
    return f"{prefix}-{digest}"


def _asset_config() -> dict[str, str | int]:
    settings = get_settings()
    endpoint = settings.chat_asset_s3_endpoint.strip()
    bucket = settings.chat_asset_s3_bucket.strip()
    access_key = settings.chat_asset_s3_access_key.strip()
    secret_key = settings.chat_asset_s3_secret_key.strip()
    scanner_host = settings.chat_clamav_host.strip()
    region = settings.chat_asset_s3_region.strip()
    encryption = settings.chat_asset_s3_server_side_encryption.strip()
    try:
        port = int(settings.chat_clamav_port)
        ttl = int(settings.chat_asset_signed_url_ttl_seconds)
    except (TypeError, ValueError) as exc:
        raise AssetUnavailable("chat asset configuration is invalid") from exc
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or bool(parsed.query or parsed.fragment)
        or not bucket
        or not access_key
        or not secret_key
        or not region
        or not scanner_host
        or not 1 <= port <= 65535
        or not 1 <= ttl <= 3600
        or encryption not in {"none", "AES256", "aws:kms"}
    ):
        raise AssetUnavailable("chat asset storage or scanner is not configured")
    kms_key = settings.chat_asset_s3_kms_key_id.strip()
    if encryption == "aws:kms" and not kms_key:
        raise AssetUnavailable("chat asset KMS key is not configured")
    return {
        "endpoint": endpoint,
        "bucket": bucket,
        "access_key": access_key,
        "secret_key": secret_key,
        "region": region,
        "encryption": encryption,
        "kms_key": kms_key,
        "scanner_host": scanner_host,
        "scanner_port": port,
        "signed_url_ttl": ttl,
    }


def asset_pipeline_available() -> bool:
    try:
        _asset_config()
    except AssetUnavailable:
        return False
    return True


_MAX_PARSER_OUTPUT_BYTES = 64 * 1024


def _inspect_with_bounded_child(path: Path) -> tuple[str, dict[str, int]]:
    try:
        result = subprocess.run(
            [sys.executable, "-m", "lumen.services.asset_inspector", str(path)],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise AssetError("파일 형식을 확인하지 못했습니다") from exc
    if result.returncode != 0 or len(result.stdout) > _MAX_PARSER_OUTPUT_BYTES:
        raise AssetError("파일 형식을 확인하지 못했습니다")
    try:
        payload = json.loads(result.stdout)
        mime_type = payload["mime_type"]
        metadata = payload["metadata"]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AssetError("파일 형식을 확인하지 못했습니다") from exc
    if (
        not isinstance(mime_type, str)
        or not isinstance(metadata, dict)
        or not all(isinstance(key, str) and isinstance(value, int) for key, value in metadata.items())
    ):
        raise AssetError("파일 형식을 확인하지 못했습니다")
    return mime_type, metadata


def inspect_file(path: Path, *, original_name: str) -> InspectedAsset:
    size = path.stat().st_size
    if size <= 0 or size > _MAX_UPLOAD_BYTES:
        raise AssetError("파일 크기가 제한을 초과했습니다")
    mime_type, metadata = _inspect_with_bounded_child(path)
    if mime_type not in _ALLOWED_MIMES:
        raise AssetError("지원하지 않는 파일 형식입니다")
    if mime_type in _IMAGE_MIMES and size > _MAX_IMAGE_BYTES:
        raise AssetError("이미지 파일 크기가 제한을 초과했습니다")
    if mime_type == "application/pdf" and size > _MAX_PDF_BYTES:
        raise AssetError("PDF 파일 크기가 제한을 초과했습니다")
    if mime_type in _AUDIO_MIMES and size > _MAX_AUDIO_BYTES:
        raise AssetError("오디오 파일 크기가 제한을 초과했습니다")
    with path.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    return InspectedAsset(
        mime_type=mime_type,
        size_bytes=size,
        sha256=digest,
        metadata=metadata,
        original_name=_sanitized_name(original_name),
    )


async def inspect_file_async(path: Path, *, original_name: str) -> InspectedAsset:
    return await asyncio.to_thread(inspect_file, path, original_name=original_name)


def inspect_generated_file(path: Path, *, original_name: str, media_type: str) -> InspectedAsset:
    """Inspect bounded runtime output before it enters the scanned asset pipeline."""
    size = path.stat().st_size
    normalized_media_type = media_type.strip().lower()
    if size <= 0 or size > _MAX_GENERATED_FILE_BYTES:
        raise AssetError("생성 파일 크기가 제한을 초과했습니다")
    if not _MEDIA_TYPE.fullmatch(normalized_media_type) or normalized_media_type not in _ALLOWED_GENERATED_MIMES:
        raise AssetError("지원하지 않는 생성 파일 형식입니다")
    if normalized_media_type in _ALLOWED_MIMES:
        inspected = inspect_file(path, original_name=original_name)
        if inspected.mime_type != normalized_media_type:
            raise AssetError("생성 파일 형식이 선언된 MIME과 일치하지 않습니다")
        return inspected

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise AssetError("생성 텍스트 파일이 올바른 UTF-8이 아닙니다") from exc
    if any(ord(char) < 0x20 and char not in "\t\r\n" for char in text):
        raise AssetError("생성 텍스트 파일에 허용되지 않는 제어 문자가 있습니다")
    if normalized_media_type == "application/json":
        try:
            json.loads(text)
        except json.JSONDecodeError as exc:
            raise AssetError("생성 JSON 파일이 올바르지 않습니다") from exc
    with path.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    return InspectedAsset(
        mime_type=normalized_media_type,
        size_bytes=size,
        sha256=digest,
        metadata={},
        original_name=_sanitized_name(original_name),
    )


async def inspect_generated_file_async(
    path: Path,
    *,
    original_name: str,
    media_type: str,
) -> InspectedAsset:
    return await asyncio.to_thread(
        inspect_generated_file,
        path,
        original_name=original_name,
        media_type=media_type,
    )


def _assert_scanner_host(host: str) -> None:
    """The scanner host is operator configuration, but must resolve before upload."""
    try:
        socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise AssetUnavailable("chat asset scanner is unavailable") from exc


async def scan_file(path: Path) -> None:
    config = _asset_config()
    host = str(config["scanner_host"])
    _assert_scanner_host(host)
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, int(config["scanner_port"])), timeout=10)
        writer.write(b"zINSTREAM\0")
        with path.open("rb") as source:
            while chunk := source.read(64 * 1024):
                writer.write(struct.pack("!I", len(chunk)))
                writer.write(chunk)
                await writer.drain()
        writer.write(struct.pack("!I", 0))
        await writer.drain()
        reply = await asyncio.wait_for(reader.read(1024), timeout=10)
    except (OSError, TimeoutError) as exc:
        raise AssetUnavailable("chat asset scanner is unavailable") from exc
    finally:
        if "writer" in locals():
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
    if not reply.endswith(b"OK\0"):
        raise AssetError("파일 보안 검사에 실패했습니다")


def _s3_client(config: dict[str, str | int]):
    return boto3.client(
        "s3",
        endpoint_url=str(config["endpoint"]),
        aws_access_key_id=str(config["access_key"]),
        aws_secret_access_key=str(config["secret_key"]),
        region_name=str(config["region"]),
        config=Config(
            signature_version="s3v4",
            retries={"max_attempts": 2, "mode": "standard"},
            s3={"addressing_style": "path"},
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
    )


def _client_error_code(exc: ClientError) -> str:
    error = exc.response.get("Error") if isinstance(exc.response, dict) else None
    return str(error.get("Code") or "") if isinstance(error, dict) else ""


def _ensure_bucket(client, bucket: str) -> None:
    try:
        client.head_bucket(Bucket=bucket)
        return
    except ClientError as exc:
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if status != 404 and _client_error_code(exc) not in {"404", "NoSuchBucket", "NotFound"}:
            raise AssetUnavailable("chat asset project bucket is unavailable") from exc
    try:
        client.create_bucket(Bucket=bucket)
    except ClientError as exc:
        if _client_error_code(exc) != "BucketAlreadyOwnedByYou":
            raise AssetUnavailable("chat asset project bucket could not be created") from exc
    try:
        client.head_bucket(Bucket=bucket)
    except ClientError as exc:
        raise AssetUnavailable("chat asset project bucket is unavailable") from exc


def _put_object(
    path: Path,
    *,
    config: dict[str, str | int],
    bucket: str,
    key: str,
    asset: InspectedAsset,
) -> None:
    params: dict[str, object] = {"ContentType": asset.mime_type}
    if config["encryption"] != "none":
        params["ServerSideEncryption"] = str(config["encryption"])
    if config["encryption"] == "aws:kms":
        params["SSEKMSKeyId"] = str(config["kms_key"])
    client = _s3_client(config)
    _ensure_bucket(client, bucket)
    with path.open("rb") as source:
        client.upload_fileobj(source, bucket, key, ExtraArgs=params)


def _delete_object(*, config: dict[str, str | int], bucket: str, key: str) -> None:
    _s3_client(config).delete_object(Bucket=bucket, Key=key)


def _row(asset: ChatAsset) -> dict:
    return {
        "id": asset.id,
        "name": asset.original_name,
        "mime_type": asset.mime_type,
        "size_bytes": asset.size_bytes,
        "sha256": asset.sha256,
        "status": asset.status,
        "media_metadata": asset.media_metadata or {},
        "created_at": asset.created_at.isoformat() if asset.created_at else None,
    }


async def _store_inspected_asset(
    *,
    path: Path,
    inspected: InspectedAsset,
    user_id: str,
    project_id: str,
) -> dict:
    config = _asset_config()
    factory = _require_session_factory()
    bucket = project_bucket_name(str(config["bucket"]), project_id)
    asset_id = str(uuid.uuid4())
    key = f"chat-assets/{asset_id}"
    row = ChatAsset(
        id=asset_id,
        project_id=project_id,
        user_id=user_id,
        object_key=key,
        original_name=inspected.original_name,
        mime_type=inspected.mime_type,
        bucket_name=bucket,
        size_bytes=inspected.size_bytes,
        sha256=inspected.sha256,
        status="uploading",
        media_metadata=inspected.metadata,
    )
    try:
        async with factory() as session:
            session.add(row)
            await session.commit()
    except SQLAlchemyError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat asset metadata를 저장하지 못했습니다") from exc
    try:
        await _set_status(asset_id, status="scanning")
        await scan_file(path)
    except AssetError:
        await _set_status(asset_id, status="failed")
        raise
    try:
        await asyncio.to_thread(_put_object, path, config=config, bucket=bucket, key=key, asset=inspected)
    except Exception as exc:
        await _set_status(asset_id, status="failed")
        raise AssetUnavailable("chat asset object storage is unavailable") from exc
    try:
        return await _set_status(asset_id, status="clean")
    except Exception:
        # The object is intentionally retained for the cleanup worker if the DB transition fails.
        raise


async def create_uploaded_asset(*, path: Path, original_name: str, user_id: str, project_id: str) -> dict:
    inspected = await inspect_file_async(path, original_name=original_name)
    return await _store_inspected_asset(
        path=path,
        inspected=inspected,
        user_id=user_id,
        project_id=project_id,
    )


async def link_output_assets_in_transaction(session, *, run, asset_ids: list[str]) -> None:
    """Validate and attach canonical output assets inside the caller's run transaction."""
    unique_ids = list(dict.fromkeys(asset_ids))
    if not unique_ids:
        return
    rows = (
        (await session.execute(select(ChatAsset).where(ChatAsset.id.in_(unique_ids)).with_for_update())).scalars().all()
    )
    by_id = {row.id: row for row in rows}
    for asset_id in unique_ids:
        asset = by_id.get(asset_id)
        if (
            asset is None
            or asset.user_id != run.user_id
            or asset.project_id != run.project_id
            or asset.status != "clean"
        ):
            raise AssetError("tool artifact is not a clean owned asset")
    existing = set(
        (
            await session.execute(
                select(ChatRunAsset.asset_id).where(
                    ChatRunAsset.run_id == run.id,
                    ChatRunAsset.purpose == "output",
                    ChatRunAsset.asset_id.in_(unique_ids),
                )
            )
        )
        .scalars()
        .all()
    )
    for asset_id in unique_ids:
        if asset_id not in existing:
            session.add(ChatRunAsset(run_id=run.id, asset_id=asset_id, purpose="output"))


async def create_generated_asset(
    *,
    path: Path,
    original_name: str,
    media_type: str,
    user_id: str,
    project_id: str,
    run_id: str,
) -> dict:
    """Ingest a locally generated file and link it to its owned durable run."""
    inspected = await inspect_generated_file_async(
        path,
        original_name=original_name,
        media_type=media_type,
    )
    result = await _store_inspected_asset(
        path=path,
        inspected=inspected,
        user_id=user_id,
        project_id=project_id,
    )
    from lumen.models.chat_runs import ChatRun

    factory = _require_session_factory()
    try:
        async with factory() as session, session.begin():
            run = (
                await session.execute(select(ChatRun).where(ChatRun.id == run_id).with_for_update())
            ).scalar_one_or_none()
            if run is None or run.user_id != user_id or run.project_id != project_id:
                raise AssetError("generated asset run ownership is invalid")
            await link_output_assets_in_transaction(session, run=run, asset_ids=[result["id"]])
    except SQLAlchemyError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("generated chat asset을 연결하지 못했습니다") from exc
    return result


async def create_generated_asset_bytes(
    *,
    data: bytes,
    original_name: str,
    media_type: str,
    user_id: str,
    project_id: str,
    run_id: str,
) -> dict:
    """Ingest bounded in-memory output from a supported remote tool adapter."""
    if not data or len(data) > _MAX_GENERATED_FILE_BYTES:
        raise AssetError("생성 파일 크기가 제한을 초과했습니다")
    with tempfile.TemporaryDirectory(prefix="lumen-generated-") as directory:
        path = Path(directory) / "payload"
        await asyncio.to_thread(path.write_bytes, data)
        return await create_generated_asset(
            path=path,
            original_name=original_name,
            media_type=media_type,
            user_id=user_id,
            project_id=project_id,
            run_id=run_id,
        )


async def _owned_asset(*, asset_id: str, user_id: str, project_id: str, lock: bool = False) -> ChatAsset:
    factory = _require_session_factory()
    try:
        async with factory() as session:
            query = select(ChatAsset).where(ChatAsset.id == asset_id)
            if lock:
                query = query.with_for_update()
            row = (await session.execute(query)).scalar_one_or_none()
            if row is None:
                raise AssetError("asset not found")
            if row.user_id != user_id or row.project_id != project_id:
                raise AssetError("asset forbidden")
            session.expunge(row)
            return row
    except SQLAlchemyError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat asset을 조회하지 못했습니다") from exc


async def provider_content_for_input_parts(
    parts: list[dict],
    *,
    user_id: str,
    project_id: str,
    allow_document: bool = False,
) -> str | list[dict[str, object]]:
    """Materialize clean, owned provider-supported input assets at execution time."""
    try:
        parsed = validate_user_input_parts(parts)
    except (TypeError, ValueError) as exc:
        raise AssetError("input asset parts are invalid") from exc
    content: list[dict[str, object]] = []
    for part in parsed:
        if isinstance(part, UserTextInputPart):
            content.append({"type": "text", "text": part.text})
            continue
        if not isinstance(part, UserAssetInputPart) or part.type not in {"image", "document"}:
            raise AssetError(f"{part.type} input is not available")
        if part.type == "document" and not allow_document:
            raise AssetError("document input is not available")
        asset = await _owned_asset(asset_id=part.asset_id, user_id=user_id, project_id=project_id)
        expected_mime = "application/pdf" if part.type == "document" else None
        if (
            asset.status != "clean"
            or (expected_mime is not None and asset.mime_type != expected_mime)
            or (expected_mime is None and not asset.mime_type.startswith("image/"))
        ):
            raise AssetError("input asset is not ready")
        url = await signed_download_url(asset_id=asset.id, user_id=user_id, project_id=project_id)
        if part.type == "document":
            content.append({"type": "file", "file": {"file_id": url, "format": "application/pdf"}})
        else:
            content.append({"type": "image_url", "image_url": {"url": url}})
    return content[0]["text"] if len(content) == 1 and content[0]["type"] == "text" else content


async def get_asset(*, asset_id: str, user_id: str, project_id: str) -> dict:
    return _row(await _owned_asset(asset_id=asset_id, user_id=user_id, project_id=project_id))


async def signed_download_url(*, asset_id: str, user_id: str, project_id: str) -> str:
    asset = await _owned_asset(asset_id=asset_id, user_id=user_id, project_id=project_id)
    if asset.status != "clean":
        raise AssetError("asset is not available")
    config = _asset_config()
    bucket = asset.bucket_name or str(config["bucket"])
    try:
        return await asyncio.to_thread(
            _s3_client(config).generate_presigned_url,
            "get_object",
            Params={"Bucket": bucket, "Key": asset.object_key},
            ExpiresIn=int(config["signed_url_ttl"]),
        )
    except Exception as exc:
        raise AssetUnavailable("chat asset object storage is unavailable") from exc


async def open_download(*, asset_id: str, user_id: str, project_id: str) -> AssetDownload:
    """Open an owned object for same-origin streaming through the authenticated BFF."""
    asset = await _owned_asset(asset_id=asset_id, user_id=user_id, project_id=project_id)
    if asset.status != "clean":
        raise AssetError("asset is not available")
    config = _asset_config()
    bucket = asset.bucket_name or str(config["bucket"])

    def get_object():
        return _s3_client(config).get_object(Bucket=bucket, Key=asset.object_key)

    try:
        response = await asyncio.to_thread(get_object)
        body = response["Body"]
        if not callable(getattr(body, "read", None)) or not callable(getattr(body, "close", None)):
            raise TypeError("invalid object body")
        content_length = response.get("ContentLength")
        if isinstance(content_length, int) and content_length != asset.size_bytes:
            body.close()
            raise ValueError("object size mismatch")
    except Exception as exc:
        raise AssetUnavailable("chat asset object storage is unavailable") from exc
    return AssetDownload(
        body=body,
        name=asset.original_name,
        mime_type=asset.mime_type,
        size_bytes=asset.size_bytes,
    )


async def delete_asset(*, asset_id: str, user_id: str, project_id: str) -> dict:
    factory = _require_session_factory()
    try:
        async with factory() as session:
            query = select(ChatAsset).where(ChatAsset.id == asset_id).with_for_update()
            asset = (await session.execute(query)).scalar_one_or_none()
            if asset is None:
                raise AssetError("asset not found")
            if asset.user_id != user_id or asset.project_id != project_id:
                raise AssetError("asset forbidden")
            message_ref = await session.scalar(
                select(ChatMessageAsset.asset_id).where(ChatMessageAsset.asset_id == asset_id).limit(1)
            )
            run_ref = await session.scalar(
                select(ChatRunAsset.asset_id).where(ChatRunAsset.asset_id == asset_id).limit(1)
            )
            asset.status = "deleting"
            asset.deleting_at = datetime.now(UTC)
            await session.commit()
            return {"id": asset_id, "pending_cleanup": bool(message_ref or run_ref)}
    except SQLAlchemyError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat asset을 삭제하지 못했습니다") from exc


async def _set_status(asset_id: str, *, status: str) -> dict:
    factory = _require_session_factory()
    try:
        async with factory() as session:
            row = (
                await session.execute(select(ChatAsset).where(ChatAsset.id == asset_id).with_for_update())
            ).scalar_one()
            row.status = status
            await session.commit()
            await session.refresh(row)
            session.expunge(row)
            return _row(row)
    except SQLAlchemyError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat asset 상태를 갱신하지 못했습니다") from exc
