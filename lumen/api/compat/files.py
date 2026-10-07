"""OpenAI Files contract with bounded streaming multipart admission."""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import tempfile
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from python_multipart import MultipartParser
from python_multipart.exceptions import MultipartParseError
from python_multipart.multipart import parse_options_header

from lumen.api.compat.openai import openai_error_response
from lumen.auth import Principal, require_api_key_scopes
from lumen.config import get_settings
from lumen.models.batch_contracts import OpenAIFileDeleted, OpenAIFileList, OpenAIFileObject
from lumen.services import batch_files

router = APIRouter()
_OVERHEAD = 64 * 1024
_ID = re.compile(r"file-([0-9a-f]{32})\Z")
_upload_slots: asyncio.Semaphore | None = None
_upload_capacity: int | None = None


def _internal_id(value: str) -> str:
    match = _ID.fullmatch(value)
    if match is None:
        raise batch_files.BatchFileNotFound()
    return str(uuid.UUID(hex=match[1]))


def _public_id(value: str) -> str:
    return "file-" + uuid.UUID(value).hex


def _object(view: batch_files.BatchFileView) -> OpenAIFileObject:
    return OpenAIFileObject(id=_public_id(view.id), bytes=view.size_bytes, created_at=int(view.created_at.timestamp()),
                            filename=view.filename, purpose=view.purpose,
                            status={"uploading": "uploaded", "processed": "processed"}.get(view.state, "error"),
                            expires_at=int(view.expires_at.timestamp()) if view.expires_at else None)


def _error(exc: batch_files.BatchFileError):
    response = openai_error_response(exc.status_code, str(exc), code=exc.code)
    if exc.status_code == 429:
        response.headers["Retry-After"] = "1"
    return response


def _available():
    if not get_settings().batch_enabled:
        raise batch_files.BatchFileUnavailable()


@asynccontextmanager
async def _slot():
    global _upload_slots, _upload_capacity
    capacity = get_settings().batch_upload_slots
    if _upload_slots is None:
        _upload_slots, _upload_capacity = asyncio.Semaphore(capacity), capacity
    # Settings are process-static. Do not replace a semaphore with active holders.
    if _upload_capacity != capacity and _upload_slots._value == _upload_capacity:
        _upload_slots, _upload_capacity = asyncio.Semaphore(capacity), capacity
    if _upload_slots.locked():
        exc = batch_files.BatchFileInputError("upload_slots_exhausted", "Too many file uploads")
        exc.status_code = 429
        raise exc
    await _upload_slots.acquire()  # Uncontended acquire never yields; no wait queue.
    try:
        yield
    finally:
        _upload_slots.release()


class _UploadParser:
    """Write only file bytes to our 0600 spool; never use framework form spools."""

    def __init__(self, boundary: bytes, spool, cap: int):
        self.spool, self.cap, self.size = spool, cap, 0
        self.filename = "batch.jsonl"
        self.purpose = bytearray()
        self.seen: set[bytes] = set()
        self.field = b""
        self.header_name, self.header_value = bytearray(), bytearray()
        self.headers: dict[bytes, bytes] = {}
        self.header_bytes = 0
        self.finished = False
        self.parser = MultipartParser(boundary, {
            "on_part_begin": self.part_begin, "on_header_field": self.header_field,
            "on_header_value": self.header_data, "on_header_end": self.header_end,
            "on_headers_finished": self.headers_finished, "on_part_data": self.data,
            "on_end": self.end,
        })

    def part_begin(self):
        self.headers = {}
        self.header_bytes = 0

    def header_field(self, data, start, end):
        self._header(data[start:end], self.header_name)

    def header_data(self, data, start, end):
        self._header(data[start:end], self.header_value)

    def _header(self, data, target):
        self.header_bytes += len(data)
        if self.header_bytes > 8192:
            raise batch_files.BatchFileInputError("invalid_request", "Multipart headers too large")
        target.extend(data)

    def header_end(self):
        key = bytes(self.header_name).lower()
        if key in self.headers:
            raise batch_files.BatchFileInputError("invalid_request")
        self.headers[key] = bytes(self.header_value)
        self.header_name.clear()
        self.header_value.clear()

    def headers_finished(self):
        disposition, options = parse_options_header(self.headers.get(b"content-disposition", b""))
        field = options.get(b"name")
        if disposition != b"form-data" or field not in {b"file", b"purpose"} or field in self.seen:
            raise batch_files.BatchFileInputError("invalid_request", "Expected one file and purpose=batch")
        self.seen.add(field)
        self.field = field
        if field == b"file":
            name = options.get(b"filename")
            if name is None:
                raise batch_files.BatchFileInputError("invalid_request", "File is required")
            try:
                self.filename = name.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise batch_files.BatchFileInputError("invalid_request") from exc
        elif b"filename" in options:
            raise batch_files.BatchFileInputError("invalid_request")

    def data(self, data, start, end):
        length = end - start
        if self.field == b"file":
            self.size += length
            if self.size > self.cap:
                exc = batch_files.BatchFileInputError("request_too_large", "File exceeds the JSONL byte limit")
                exc.status_code = 413
                raise exc
            self.spool.write(data[start:end])
        else:
            if len(self.purpose) + length > 32:
                raise batch_files.BatchFileInputError("invalid_purpose", "Only purpose=batch is supported")
            self.purpose.extend(data[start:end])

    def end(self):
        self.finished = True


async def _spool(request: Request, path: Path, fd: int) -> tuple[str, int]:
    cap = get_settings().batch_jsonl_max_bytes
    mime, options = parse_options_header(request.headers.get("content-type", ""))
    boundary = options.get(b"boundary")
    if mime != b"multipart/form-data" or not boundary or len(boundary) > 200:
        os.close(fd)
        raise batch_files.BatchFileInputError("invalid_request", "Expected multipart/form-data")
    total = 0
    with os.fdopen(fd, "wb") as spool:
        parser = _UploadParser(boundary, spool, cap)
        try:
            async for data in request.stream():
                total += len(data)
                if total > cap + _OVERHEAD:
                    exc = batch_files.BatchFileInputError("request_too_large")
                    exc.status_code = 413
                    raise exc
                # Receive buffers can be large; write fixed-size slices off-loop.
                for offset in range(0, len(data), 64 * 1024):
                    write = asyncio.create_task(asyncio.to_thread(parser.parser.write, data[offset:offset + 64 * 1024]))
                    try:
                        await asyncio.shield(write)
                    except asyncio.CancelledError:
                        await asyncio.gather(write, return_exceptions=True)
                        raise
            parser.parser.finalize()
        except MultipartParseError as exc:
            raise batch_files.BatchFileInputError("invalid_request", "Invalid multipart body") from exc
        if not parser.finished or parser.seen != {b"file", b"purpose"} or parser.purpose != b"batch" or not parser.size:
            raise batch_files.BatchFileInputError("invalid_request", "Expected a nonempty file and purpose=batch")
        return parser.filename, parser.size


_UPLOAD_SCHEMA = {"security": [{"APIKeyBearer": []}, {"XApiKey": []}], "requestBody": {
    "required": True, "content": {"multipart/form-data": {"schema": {"type": "object",
    "required": ["file", "purpose"], "properties": {"file": {"type": "string", "format": "binary"},
    "purpose": {"type": "string", "enum": ["batch"]}}}}}}}


@router.post("/files", response_model=OpenAIFileObject, openapi_extra=_UPLOAD_SCHEMA)
async def upload_file(request: Request, principal: Principal = Depends(require_api_key_scopes("compat:files:write"))):
    path = None
    try:
        _available()
        content_length = request.headers.get("content-length")
        if content_length is not None:
            if not content_length.isascii() or not content_length.isdecimal():
                raise batch_files.BatchFileInputError("invalid_request", "Invalid Content-Length")
            if int(content_length) > get_settings().batch_jsonl_max_bytes + _OVERHEAD:
                exc = batch_files.BatchFileInputError("request_too_large")
                exc.status_code = 413
                raise exc
        async with _slot():
            directory = tempfile.gettempdir()
            free = await asyncio.to_thread(shutil.disk_usage, directory)
            if free.free < get_settings().batch_jsonl_max_bytes + _OVERHEAD:
                raise batch_files.BatchFileUnavailable()
            fd, name = tempfile.mkstemp(prefix="lumen-batch-", suffix=".jsonl", dir=directory)
            path = Path(name)
            os.fchmod(fd, 0o600)
            filename, size = await _spool(request, path, fd)
            return _object(await batch_files.create_batch_file(project_id=principal["project_id"], user_id=principal["user_id"],
                api_key_id=principal.get("api_key_id"), filename=filename, path=path, size_bytes=size))
    except batch_files.BatchFileError as exc:
        return _error(exc)
    except OSError:
        return _error(batch_files.BatchFileUnavailable())
    finally:
        if path is not None:
            path.unlink(missing_ok=True)


@router.get("/files", response_model=OpenAIFileList)
async def list_files(purpose: str | None = None, after: str | None = None, limit: str = "20", order: str = "desc",
                     principal: Principal = Depends(require_api_key_scopes("compat:files:read"))):
    try:
        _available()
        if not limit.isascii() or not limit.isdecimal() or not 1 <= int(limit) <= 100:
            raise batch_files.BatchFileInputError("invalid_request", "limit must be between 1 and 100")
        rows, has_more = await batch_files.list_batch_files(project_id=principal["project_id"], user_id=principal["user_id"],
            purpose=purpose, after=_internal_id(after) if after else None, limit=int(limit), order=order)
        data = [_object(row) for row in rows]
        return OpenAIFileList(data=data, first_id=data[0].id if data else None, last_id=data[-1].id if data else None,
                              has_more=has_more)
    except batch_files.BatchFileError as exc:
        return _error(exc)


@router.get("/files/{file_id}", response_model=OpenAIFileObject)
async def get_file(file_id: str, principal: Principal = Depends(require_api_key_scopes("compat:files:read"))):
    try:
        _available()
        return _object(await batch_files.get_batch_file(project_id=principal["project_id"], user_id=principal["user_id"],
                                                       file_id=_internal_id(file_id)))
    except batch_files.BatchFileError as exc:
        return _error(exc)


@router.delete("/files/{file_id}", response_model=OpenAIFileDeleted)
async def delete_file(file_id: str, principal: Principal = Depends(require_api_key_scopes("compat:files:delete"))):
    try:
        _available()
        await batch_files.delete_batch_file(project_id=principal["project_id"], user_id=principal["user_id"],
                                            file_id=_internal_id(file_id))
        return OpenAIFileDeleted(id=file_id)
    except batch_files.BatchFileError as exc:
        return _error(exc)


@router.get("/files/{file_id}/content")
async def file_content(file_id: str, principal: Principal = Depends(require_api_key_scopes("compat:files:read"))):
    try:
        _available()
        download = await batch_files.open_batch_file_content(project_id=principal["project_id"], user_id=principal["user_id"],
                                                             file_id=_internal_id(file_id))
        return StreamingResponse(download.chunks(), media_type=download.mime_type,
            headers={"Content-Length": str(download.size_bytes), "Content-Disposition": "attachment; filename*=UTF-8''" + quote(download.name, safe=""),
                     "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})
    except batch_files.BatchFileError as exc:
        return _error(exc)
