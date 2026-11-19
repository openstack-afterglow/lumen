"""Private scanned JSONL files, bounded validation reads and durable result storage."""
from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from botocore.exceptions import ClientError
from sqlalchemy import and_, or_, select
from sqlalchemy.exc import SQLAlchemyError

from lumen.config import get_settings
from lumen.db import get_session_factory, is_db_available, mark_db_unhealthy
from lumen.models.chat_batches import ChatBatch, ChatBatchFile
from lumen.services import assets
from lumen.services.batches import BatchError, BatchInputError, BatchNotFound, BatchUnavailable

_TERMINAL = ("completed", "failed", "cancelled", "expired")
_PART_BYTES = 8 * 1024 * 1024
_MIME = "application/jsonl"
_gc_project_cursor: str | None = None
_gc_multipart_markers: dict[str, dict[str, str]] = {}


class BatchFileError(BatchError):
    status_code = 400

    def __init__(self, code: str, message: str | None = None, *, start_byte: int | None = None):
        super().__init__(code, message, start_byte=start_byte)


class BatchFileNotFound(BatchFileError, BatchNotFound):
    status_code = 404

    def __init__(self):
        super().__init__("file_not_found", "File not found")


class BatchFileInputError(BatchFileError, BatchInputError):
    pass


class BatchFileUnavailable(BatchFileError, BatchUnavailable):
    status_code = 503

    def __init__(self, code: str = "batch_unavailable"):
        super().__init__(code, "Batch file storage is unavailable")


@dataclass(frozen=True)
class BatchFileView:
    id: str
    purpose: Literal["batch", "batch_output"]
    filename: str
    size_bytes: int
    sha256: str | None
    state: str
    created_at: datetime
    expires_at: datetime | None


@dataclass(frozen=True)
class InputLine:
    start_byte: int
    raw: bytes


@dataclass(frozen=True)
class InputChunk:
    lines: list[InputLine]
    next_byte: int
    eof: bool


@dataclass(frozen=True)
class StoredResult:
    file_id: str
    bucket: str
    object_key: str
    size_bytes: int
    sha256: str


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _view(row: ChatBatchFile) -> BatchFileView:
    return BatchFileView(row.id, row.purpose, row.filename, row.size_bytes, row.sha256,
                         row.state, _utc(row.created_at), _utc(row.expires_at))


def _enabled() -> None:
    if not get_settings().batch_enabled:
        raise BatchFileUnavailable()


@asynccontextmanager
async def _session():
    _enabled()
    factory = get_session_factory()
    if not is_db_available() or factory is None:
        raise BatchFileUnavailable()
    try:
        async with factory() as session:
            yield session
    except SQLAlchemyError as exc:
        mark_db_unhealthy()
        raise BatchFileUnavailable() from exc


def _config():
    try:
        return assets.object_storage_config()
    except assets.AssetUnavailable as exc:
        raise BatchFileUnavailable() from exc


def _visible(row: ChatBatchFile | None) -> bool:
    return row is not None and row.state not in {"deleting", "deleted"} and _utc(row.expires_at) > datetime.now(UTC)


async def _owned(session, *, project_id, user_id, file_id, lock=False):
    stmt = select(ChatBatchFile).where(ChatBatchFile.id == file_id, ChatBatchFile.project_id == project_id,
                                       ChatBatchFile.user_id == user_id)
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    row = (await session.execute(stmt)).scalar_one_or_none()
    if not _visible(row):
        raise BatchFileNotFound()
    return row


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(64 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


async def _finish_io(function, *args, **kwargs):
    """Cancellation must not release a row lock or spool while its thread uses it."""
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await asyncio.gather(task, return_exceptions=True)
        raise


async def create_batch_file(*, project_id, user_id, api_key_id, filename: str, path: Path,
                            size_bytes: int) -> BatchFileView:
    _enabled()
    if not 0 < size_bytes <= get_settings().batch_jsonl_max_bytes or path.stat().st_size != size_bytes:
        raise BatchFileInputError("request_too_large" if size_bytes > get_settings().batch_jsonl_max_bytes else "invalid_file")
    config = _config()
    file_id = str(uuid.uuid4())
    bucket = assets.project_bucket_name(str(config["bucket"]), project_id)
    row = ChatBatchFile(id=file_id, project_id=project_id, user_id=user_id, api_key_id=api_key_id,
                        purpose="batch", filename=assets._sanitized_name(filename), mime_type=_MIME,
                        size_bytes=size_bytes, sha256=None, bucket_name=bucket, object_key=f"batch-files/input/{file_id}",
                        state="uploading", created_at=datetime.now(UTC),
                        expires_at=datetime.now(UTC) + timedelta(days=get_settings().batch_input_ttl_days))
    async with _session() as session:
        session.add(row)
        await session.commit()
    try:
        await assets.scan_file(path)
        digest = await _finish_io(_hash_file, path)
        async with _session() as session:
            current = await session.get(ChatBatchFile, file_id, with_for_update=True, populate_existing=True)
            if current.state != "uploading":
                raise BatchFileNotFound()
            await _finish_io(assets.put_file, path, config=config, bucket=bucket, key=row.object_key, mime_type=_MIME)
            current.state, current.scan_result, current.sha256 = "processed", "clean", digest
            await session.commit()
            return _view(current)
    except BaseException as exc:
        # Cancelled uploads also leave a collectable row; no bytes become readable.
        async with _session() as session:
            current = await session.get(ChatBatchFile, file_id, with_for_update=True)
            if current.state == "uploading":
                current.state, current.scan_result = "error", "error"
                await session.commit()
        if isinstance(exc, (BatchFileError, asyncio.CancelledError)):
            raise
        if isinstance(exc, assets.AssetError) and not isinstance(exc, assets.AssetUnavailable):
            raise BatchFileInputError("file_scan_failed", "File security scan failed") from exc
        raise BatchFileUnavailable() from exc


async def list_batch_files(*, project_id, user_id, purpose: str | None, after: str | None,
                           limit: int, order: Literal["asc", "desc"]) -> tuple[list[BatchFileView], bool]:
    if not 1 <= limit <= 100 or order not in {"asc", "desc"} or purpose not in {None, "batch", "batch_output"}:
        raise BatchFileInputError("invalid_request")
    async with _session() as session:
        stmt = select(ChatBatchFile).where(ChatBatchFile.project_id == project_id, ChatBatchFile.user_id == user_id,
                    ChatBatchFile.state.not_in(("deleting", "deleted")), ChatBatchFile.expires_at > datetime.now(UTC))
        if purpose is not None:
            stmt = stmt.where(ChatBatchFile.purpose == purpose)
        if after is not None:
            cursor = await _owned(session, project_id=project_id, user_id=user_id, file_id=after)
            comparison = (ChatBatchFile.created_at > cursor.created_at) if order == "asc" else (ChatBatchFile.created_at < cursor.created_at)
            tie = (ChatBatchFile.id > cursor.id) if order == "asc" else (ChatBatchFile.id < cursor.id)
            stmt = stmt.where(or_(comparison, and_(ChatBatchFile.created_at == cursor.created_at, tie)))
        ordering = (ChatBatchFile.created_at.asc(), ChatBatchFile.id.asc()) if order == "asc" else (ChatBatchFile.created_at.desc(), ChatBatchFile.id.desc())
        rows = (await session.execute(stmt.order_by(*ordering).limit(limit + 1))).scalars().all()
        return [_view(row) for row in rows[:limit]], len(rows) > limit


async def get_batch_file(*, project_id, user_id, file_id: str) -> BatchFileView:
    async with _session() as session:
        return _view(await _owned(session, project_id=project_id, user_id=user_id, file_id=file_id))


async def delete_batch_file(*, project_id, user_id, file_id: str) -> BatchFileView:
    async with _session() as session:
        row = await _owned(session, project_id=project_id, user_id=user_id, file_id=file_id, lock=True)
        row.state, row.deleted_at = "deleting", datetime.now(UTC)
        await session.commit()
        return _view(row)


async def _download(row: ChatBatchFile, *, start_byte: int | None = None) -> assets.AssetDownload:
    config = _config()
    try:
        client = assets.object_client(config)
        params = {"Bucket": row.bucket_name, "Key": row.object_key}
        if start_byte is not None:
            params["Range"] = f"bytes={start_byte}-"
        response = await asyncio.to_thread(client.get_object, **params)
        body = response["Body"]
        expected = row.size_bytes - (start_byte or 0)
        if response.get("ContentLength") != expected:
            body.close()
            raise ValueError("object size mismatch")
        return assets.AssetDownload(body, row.filename, row.mime_type, expected)
    except Exception as exc:
        raise BatchFileUnavailable() from exc


async def open_batch_file_content(*, project_id, user_id, file_id: str) -> assets.AssetDownload:
    async with _session() as session:
        row = await _owned(session, project_id=project_id, user_id=user_id, file_id=file_id)
        if row.state != "processed" or (row.purpose == "batch" and row.scan_result != "clean"):
            raise BatchFileInputError("input_file_unavailable")
        return await _download(row)


async def _input_row(file_id: str) -> ChatBatchFile:
    async with _session() as session:
        row = await session.get(ChatBatchFile, file_id)
        if row is None or row.purpose != "batch" or row.scan_result != "clean":
            raise BatchFileInputError("input_file_unavailable")
        if not _visible(row) or row.state != "processed":
            pin = (await session.execute(select(ChatBatch.id).where(ChatBatch.input_file_id == file_id,
                                          ChatBatch.status.not_in(_TERMINAL)).limit(1))).scalar_one_or_none()
            if pin is None or row.state == "deleted":
                raise BatchFileInputError("input_file_unavailable")
        return row


async def read_input_chunk(*, file_id: str, start_byte: int, max_rows: int, max_bytes: int,
                           max_line_bytes: int) -> InputChunk:
    if start_byte < 0 or min(max_rows, max_bytes, max_line_bytes) < 1:
        raise BatchFileInputError("invalid_cursor", start_byte=start_byte)
    row = await _input_row(file_id)
    if start_byte > row.size_bytes:
        raise BatchFileInputError("invalid_cursor", start_byte=start_byte)
    if start_byte == row.size_bytes:
        return InputChunk([], start_byte, True)
    download = await _download(row, start_byte=start_byte)
    lines: list[InputLine] = []
    pending = bytearray()
    cursor, consumed = start_byte, 0
    eof = False
    try:
        while len(lines) < max_rows:
            newline = pending.find(b"\n")
            if newline < 0 and not eof:
                # Two extra bytes permit a line exactly at the cap followed by CRLF.
                room = max_line_bytes + 2 - len(pending)
                if room <= 0:
                    raise BatchFileInputError("line_too_large", start_byte=cursor)
                chunk = await asyncio.to_thread(download.body.read, min(64 * 1024, room))
                if chunk:
                    pending.extend(chunk)
                else:
                    eof = True
                continue
            if not pending and eof:
                break
            length = newline + 1 if newline >= 0 else len(pending)
            raw = bytes(pending[:length])
            content = raw[:-1] if raw.endswith(b"\n") else raw
            if raw.endswith(b"\r\n"):
                content = content[:-1]
            if len(content) > max_line_bytes:
                raise BatchFileInputError("line_too_large", start_byte=cursor)
            try:
                content.decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise BatchFileInputError("invalid_utf8", start_byte=cursor) from exc
            # A single line may exceed the chunk budget, never the per-line cap.
            if lines and consumed + length > max_bytes:
                break
            lines.append(InputLine(cursor, raw))
            cursor += length
            consumed += length
            del pending[:length]
            if consumed >= max_bytes:
                break
        return InputChunk(lines, cursor, cursor == row.size_bytes)
    finally:
        await asyncio.to_thread(download.body.close)


def result_file_id(batch_id: str, epoch: int, kind: Literal["output", "error"]) -> str:
    if epoch < 0 or kind not in {"output", "error"}:
        raise ValueError("invalid result identity")
    return str(uuid.uuid5(uuid.UUID(batch_id), f"batch-file:{epoch}:{kind}"))


def _completed_object_hash(client, *, bucket: str, key: str) -> tuple[int, str] | None:
    """Verify an earlier completed attempt without loading its object into memory."""
    try:
        head = client.head_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
            return None
        raise
    response = client.get_object(Bucket=bucket, Key=key)
    body = response["Body"]
    digest, size = hashlib.sha256(), 0
    try:
        while chunk := body.read(64 * 1024):
            digest.update(chunk)
            size += len(chunk)
    finally:
        body.close()
    if head.get("ContentLength") != size:
        raise ValueError("completed object size mismatch")
    return size, digest.hexdigest()


async def _result_chunks(lines: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    """Bound multipart staging even when a generated JSONL row contains media."""
    async for line in lines:
        for offset in range(0, len(line), 64 * 1024):
            yield line[offset:offset + 64 * 1024]


async def write_result_file(*, project_id: str, batch_id: str, epoch: int, kind,
                            lines: AsyncIterator[bytes]) -> StoredResult | None:
    _enabled()
    file_id = result_file_id(batch_id, epoch, kind)
    key = f"batch-files/result/{batch_id}/{epoch}/{kind}.jsonl"
    upload_id = None
    digest, size = hashlib.sha256(), 0
    buffer = bytearray()
    parts = []
    try:
        config = _config()
        bucket = assets.project_bucket_name(str(config["bucket"]), project_id)
        client = assets.object_client(config)
        await asyncio.to_thread(assets.abort_multipart_uploads, client, bucket=bucket, key=key)
        completed = await asyncio.to_thread(_completed_object_hash, client, bucket=bucket, key=key)
        async for chunk in _result_chunks(lines):
            if not chunk:
                continue
            if upload_id is None:
                upload_id = await asyncio.to_thread(assets.start_multipart, client, config=config, bucket=bucket, key=key)
            digest.update(chunk)
            size += len(chunk)
            buffer.extend(chunk)
            while len(buffer) >= _PART_BYTES:
                data = bytes(buffer[:_PART_BYTES])
                del buffer[:_PART_BYTES]
                response = await asyncio.to_thread(client.upload_part, Bucket=bucket, Key=key, UploadId=upload_id,
                                                   PartNumber=len(parts) + 1, Body=data)
                parts.append({"PartNumber": len(parts) + 1, "ETag": response["ETag"]})
        if upload_id is None:
            # Empty retry must not leave a previously completed object for this key.
            await asyncio.to_thread(client.delete_object, Bucket=bucket, Key=key)
            return None
        if completed == (size, digest.hexdigest()):
            # A crash after S3 completion but before DB publication preserves the
            # exact frozen object. Abort this tentative rewrite, then publish it.
            await asyncio.to_thread(client.abort_multipart_upload, Bucket=bucket, Key=key, UploadId=upload_id)
            return StoredResult(file_id, bucket, key, size, digest.hexdigest())
        if buffer:
            response = await asyncio.to_thread(client.upload_part, Bucket=bucket, Key=key, UploadId=upload_id,
                                               PartNumber=len(parts) + 1, Body=bytes(buffer))
            parts.append({"PartNumber": len(parts) + 1, "ETag": response["ETag"]})
        await asyncio.to_thread(client.complete_multipart_upload, Bucket=bucket, Key=key, UploadId=upload_id,
                                MultipartUpload={"Parts": parts})
        return StoredResult(file_id, bucket, key, size, digest.hexdigest())
    except BaseException as exc:
        if upload_id is not None:
            try:
                await asyncio.to_thread(client.abort_multipart_upload, Bucket=bucket, Key=key, UploadId=upload_id)
            except Exception:
                pass  # Orphan sweep retries this abort; never publish failed content.
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise BatchFileUnavailable("result_storage_unavailable") from exc


def result_file_row(*, stored: StoredResult, project_id, user_id, api_key_id, filename: str,
                    expires_at: datetime) -> ChatBatchFile:
    return ChatBatchFile(id=stored.file_id, project_id=project_id, user_id=user_id, api_key_id=api_key_id,
                        purpose="batch_output", filename=assets._sanitized_name(filename), mime_type=_MIME,
                        size_bytes=stored.size_bytes, sha256=stored.sha256, bucket_name=stored.bucket,
                        object_key=stored.object_key, state="processed", scan_result="not_required",
                        created_at=datetime.now(UTC), expires_at=expires_at)


async def gc_batch_files(*, limit: int = 100) -> int:
    if limit < 1:
        return 0
    config = _config()
    client = assets.object_client(config)
    now = datetime.now(UTC)
    deleted = 0
    async with _session() as session:
        pinned = select(ChatBatch.id).where(ChatBatch.input_file_id == ChatBatchFile.id,
                                           ChatBatch.status.not_in(_TERMINAL)).exists()
        ids = (await session.execute(select(ChatBatchFile.id).where(
            or_(ChatBatchFile.expires_at <= now, ChatBatchFile.state.in_(("deleting", "error"))),
            ChatBatchFile.state != "deleted", ~pinned).order_by(ChatBatchFile.expires_at, ChatBatchFile.id).limit(limit))).scalars().all()
    for file_id in ids:
        async with _session() as session, session.begin():
            row = await session.get(ChatBatchFile, file_id, with_for_update=True, populate_existing=True)
            if row.state == "deleted" or (row.state not in {"deleting", "error"} and _utc(row.expires_at) > now):
                continue
            # Locking/current read avoids a stale RR snapshot. Admission must hold
            # this same file lock until its batch pin has committed.
            pins = (await session.execute(select(ChatBatch.id).where(ChatBatch.input_file_id == file_id,
                        ChatBatch.status.not_in(_TERMINAL)).limit(1).with_for_update())).scalar_one_or_none()
            if pins:
                continue
            try:
                await asyncio.to_thread(assets.abort_multipart_uploads, client, bucket=row.bucket_name, key=row.object_key)
                await asyncio.to_thread(client.delete_object, Bucket=row.bucket_name, Key=row.object_key)
            except Exception as exc:
                raise BatchFileUnavailable() from exc
            row.state, row.deleted_at, row.multipart_upload_id = "deleted", row.deleted_at or now, None
            deleted += 1
    # Result multipart attempts have no published file row. Sweep old attempts in
    # known project buckets, protecting the current live finalization epoch.
    global _gc_project_cursor
    project_rows = select(ChatBatch.project_id.label("project_id")).union(
        select(ChatBatchFile.project_id.label("project_id"))).subquery()
    async with _session() as session:
        query = select(project_rows.c.project_id).order_by(project_rows.c.project_id).limit(limit)
        if _gc_project_cursor is not None:
            query = query.where(project_rows.c.project_id > _gc_project_cursor)
        projects = (await session.execute(query)).scalars().all()
        if not projects and _gc_project_cursor is not None:
            projects = (await session.execute(select(project_rows.c.project_id)
                        .order_by(project_rows.c.project_id).limit(limit))).scalars().all()
    if projects:
        _gc_project_cursor = projects[-1]
    remaining = limit
    for project in projects:
        bucket = assets.project_bucket_name(str(config["bucket"]), project)
        try:
            params = {"Bucket": bucket, "Prefix": "batch-files/", "MaxUploads": min(limit, 1000),
                      **_gc_multipart_markers.get(bucket, {})}
            page = await asyncio.to_thread(client.list_multipart_uploads, **params)
            if page.get("IsTruncated"):
                _gc_multipart_markers[bucket] = {"KeyMarker": page["NextKeyMarker"],
                                                 "UploadIdMarker": page["NextUploadIdMarker"]}
            else:
                _gc_multipart_markers.pop(bucket, None)
            if remaining:
                for upload in page.get("Uploads", []):
                    if remaining <= 0:
                        break
                    initiated = upload.get("Initiated")
                    if initiated is None or _utc(initiated) > now - timedelta(hours=1):
                        continue
                    key = upload["Key"]
                    async with _session() as session:
                        row = (await session.execute(select(ChatBatchFile).where(ChatBatchFile.object_key == key))).scalar_one_or_none()
                        active = row is not None and row.state == "uploading" and _utc(row.expires_at) > now
                        fields = key.split("/")
                        if len(fields) == 5 and fields[1] == "result":
                            batch = await session.get(ChatBatch, fields[2])
                            active = active or (batch is not None and batch.status == "finalizing"
                                               and str(batch.finalization_epoch) == fields[3])
                    if not active:
                        await asyncio.to_thread(client.abort_multipart_upload, Bucket=bucket, Key=key, UploadId=upload["UploadId"])
                        remaining -= 1
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") not in {"NoSuchBucket", "404", "NotFound"}:
                raise BatchFileUnavailable() from exc
        except Exception as exc:
            raise BatchFileUnavailable() from exc
    return deleted
