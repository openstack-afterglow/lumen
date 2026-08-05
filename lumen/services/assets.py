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
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import boto3
from botocore.config import Config
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

_IMAGE_MIMES = {"image/jpeg", "image/png", "image/webp"}
_AUDIO_MIMES = {"audio/mpeg", "audio/wav", "audio/x-wav", "audio/mp4", "audio/ogg", "audio/webm"}
_VIDEO_MIMES = {"video/mp4", "video/webm"}
_ALLOWED_MIMES = _IMAGE_MIMES | _AUDIO_MIMES | _VIDEO_MIMES | {"application/pdf"}
_CONTROL_OR_PATH = re.compile(r"[\x00-\x1f\x7f/\\]+")


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


def _asset_config() -> dict[str, str | int]:
    settings = get_settings()
    endpoint = settings.chat_asset_s3_endpoint.strip()
    bucket = settings.chat_asset_s3_bucket.strip()
    access_key = settings.chat_asset_s3_access_key.strip()
    secret_key = settings.chat_asset_s3_secret_key.strip()
    scanner_host = settings.chat_clamav_host.strip()
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
        or not scanner_host
        or not 1 <= port <= 65535
        or not 1 <= ttl <= 3600
        or encryption not in {"AES256", "aws:kms"}
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
        config=Config(signature_version="s3v4", retries={"max_attempts": 2, "mode": "standard"}),
    )


def _put_object(path: Path, *, config: dict[str, str | int], key: str, asset: InspectedAsset) -> None:
    params: dict[str, object] = {
        "ContentType": asset.mime_type,
        "ServerSideEncryption": str(config["encryption"]),
    }
    if config["encryption"] == "aws:kms":
        params["SSEKMSKeyId"] = str(config["kms_key"])
    with path.open("rb") as source:
        _s3_client(config).upload_fileobj(source, str(config["bucket"]), key, ExtraArgs=params)


def _delete_object(*, config: dict[str, str | int], key: str) -> None:
    _s3_client(config).delete_object(Bucket=str(config["bucket"]), Key=key)


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


async def create_uploaded_asset(*, path: Path, original_name: str, user_id: str, project_id: str) -> dict:
    config = _asset_config()
    inspected = await inspect_file_async(path, original_name=original_name)
    factory = _require_session_factory()
    asset_id = str(uuid.uuid4())
    key = f"chat-assets/{asset_id}"
    row = ChatAsset(
        id=asset_id,
        project_id=project_id,
        user_id=user_id,
        object_key=key,
        original_name=inspected.original_name,
        mime_type=inspected.mime_type,
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
        await asyncio.to_thread(_put_object, path, config=config, key=key, asset=inspected)
    except Exception as exc:
        logger.warning("chat asset object upload failed asset_id=%s", asset_id, exc_info=True)
        await _set_status(asset_id, status="failed")
        raise AssetUnavailable("chat asset object storage is unavailable") from exc
    try:
        return await _set_status(asset_id, status="clean")
    except Exception:
        # The object is intentionally retained for the cleanup worker if the DB transition fails.
        raise


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
    try:
        return await asyncio.to_thread(
            _s3_client(config).generate_presigned_url,
            "get_object",
            Params={"Bucket": str(config["bucket"]), "Key": asset.object_key},
            ExpiresIn=int(config["signed_url_ttl"]),
        )
    except Exception as exc:
        raise AssetUnavailable("chat asset object storage is unavailable") from exc


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
