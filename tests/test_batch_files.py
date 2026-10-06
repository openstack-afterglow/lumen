from __future__ import annotations

import hashlib
import io
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

from lumen.models.chat_batches import ChatBatchFile
from lumen.services import assets
from lumen.services import batch_files as files


@pytest.fixture
def settings(monkeypatch):
    value = SimpleNamespace(batch_enabled=True, batch_jsonl_max_bytes=200_000_000, batch_input_ttl_days=30)
    monkeypatch.setattr(files, "get_settings", lambda: value)
    monkeypatch.setattr(files, "_config", lambda: {"bucket": "lumen-assets", "encryption": "none"})
    return value


class MemorySession:
    def __init__(self):
        self.rows = {}
        self.transitions = []

    def add(self, row):
        self.rows[row.id] = row

    async def commit(self):
        self.transitions.append([(r.state, r.scan_result) for r in self.rows.values()])

    async def get(self, model, key, **kwargs):
        return self.rows.get(key)


@pytest.fixture
def session(monkeypatch, settings):
    value = MemorySession()

    @asynccontextmanager
    async def factory():
        yield value

    monkeypatch.setattr(files, "_session", factory)
    return value


@pytest.mark.asyncio
async def test_jsonl_is_scanned_before_private_storage_and_publication(tmp_path, monkeypatch, session):
    path = tmp_path / "file.jsonl"
    path.write_bytes(b'{"custom_id":"a"}\n')
    calls = []

    async def scan(path):
        calls.append("scan")
        assert next(iter(session.rows.values())).state == "uploading"

    def put(path, **kwargs):
        calls.append("put")
        assert calls == ["scan", "put"]
        assert kwargs["mime_type"] == "application/jsonl"
        assert kwargs["key"].startswith("batch-files/input/")
        assert kwargs["bucket"] == assets.project_bucket_name("lumen-assets", "p")

    monkeypatch.setattr(assets, "scan_file", scan)
    monkeypatch.setattr(assets, "put_file", put)
    monkeypatch.setattr(assets, "inspect_file", lambda *a, **k: pytest.fail("JSONL must not use media MIME policy"))
    result = await files.create_batch_file(project_id="p", user_id="u", api_key_id=4,
        filename="../data.jsonl", path=path, size_bytes=path.stat().st_size)
    assert calls == ["scan", "put"]
    assert result.state == "processed"
    assert result.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert session.rows[result.id].scan_result == "clean"
    assert "/" not in result.filename


@pytest.mark.asyncio
async def test_scan_failure_never_stores_or_reads_bytes(tmp_path, monkeypatch, session):
    path = tmp_path / "file"
    path.write_bytes(b"malware")

    async def scan(path):
        raise assets.AssetError("unsafe")

    monkeypatch.setattr(assets, "scan_file", scan)
    monkeypatch.setattr(assets, "put_file", lambda *a, **k: pytest.fail("unscanned write"))
    with pytest.raises(files.BatchFileInputError, match="security"):
        await files.create_batch_file(project_id="p", user_id="u", api_key_id=None,
                                     filename="data", path=path, size_bytes=7)
    row = next(iter(session.rows.values()))
    assert row.state == "error"

    async def owned(*a, **k):
        return row

    monkeypatch.setattr(files, "_owned", owned)
    monkeypatch.setattr(files, "_download", lambda *a, **k: pytest.fail("unscanned read"))
    with pytest.raises(files.BatchFileInputError, match="input file unavailable"):
        await files.open_batch_file_content(project_id="p", user_id="u", file_id=row.id)


@pytest.mark.asyncio
async def test_upload_service_cap_precedes_scan(tmp_path, monkeypatch, session, settings):
    settings.batch_jsonl_max_bytes = 4
    path = tmp_path / "file"
    path.write_bytes(b"12345")
    monkeypatch.setattr(assets, "scan_file", lambda *a: pytest.fail("oversized scan"))
    with pytest.raises(files.BatchFileInputError) as failure:
        await files.create_batch_file(project_id="p", user_id="u", api_key_id=None, filename="f", path=path, size_bytes=5)
    assert failure.value.code == "request_too_large"
    assert not session.rows


async def _reader(monkeypatch, data):
    body = io.BytesIO(data)
    row = SimpleNamespace(size_bytes=len(data))

    async def input_row(file_id):
        return row

    async def download(row, *, start_byte):
        body.seek(start_byte)
        return assets.AssetDownload(body, "f.jsonl", "application/jsonl", len(data) - start_byte)

    monkeypatch.setattr(files, "_input_row", input_row)
    monkeypatch.setattr(files, "_download", download)
    return body


@pytest.mark.asyncio
async def test_reader_crlf_final_line_and_byte_cursors(monkeypatch):
    data = b'{}\r\n{"x":1}\n{}'
    body = await _reader(monkeypatch, data)
    first = await files.read_input_chunk(file_id="f", start_byte=0, max_rows=1, max_bytes=100, max_line_bytes=100)
    assert first == files.InputChunk([files.InputLine(0, b"{}\r\n")], 4, False)
    assert body.closed
    await _reader(monkeypatch, data)
    rest = await files.read_input_chunk(file_id="f", start_byte=4, max_rows=10, max_bytes=100, max_line_bytes=100)
    assert rest == files.InputChunk([files.InputLine(4, b'{"x":1}\n'), files.InputLine(12, b"{}")], 14, True)


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", [b"", b"\n", b"\r\n"])
async def test_reader_accepts_exact_four_mib_line(monkeypatch, suffix):
    cap = 4 * 1024 * 1024
    await _reader(monkeypatch, b"a" * cap + suffix)
    result = await files.read_input_chunk(file_id="f", start_byte=0, max_rows=100, max_bytes=1024 * 1024, max_line_bytes=cap)
    assert len(result.lines) == 1
    assert len(result.lines[0].raw) == cap + len(suffix)
    assert result.eof


@pytest.mark.asyncio
@pytest.mark.parametrize("data,code", [(b"{}\n\xff\n", "invalid_utf8"), (b"{}\n12345\n", "line_too_large")])
async def test_reader_reports_failing_line_start_and_closes(monkeypatch, data, code):
    body = await _reader(monkeypatch, data)
    with pytest.raises(files.BatchFileInputError) as failure:
        await files.read_input_chunk(file_id="f", start_byte=0, max_rows=100, max_bytes=100, max_line_bytes=4)
    assert failure.value.code == code
    assert failure.value.start_byte == 3
    assert body.closed


@pytest.mark.asyncio
async def test_chunk_byte_budget_never_returns_second_row_over_budget(monkeypatch):
    await _reader(monkeypatch, b"123\n456\n")
    result = await files.read_input_chunk(file_id="f", start_byte=0, max_rows=100, max_bytes=7, max_line_bytes=10)
    assert result == files.InputChunk([files.InputLine(0, b"123\n")], 4, False)


class MultipartStore:
    def __init__(self):
        self.events = []
        self.part_lengths = []

    def head_object(self, **kwargs):
        raise ClientError({"Error": {"Code": "NoSuchKey"}}, "HeadObject")

    def upload_part(self, **kwargs):
        self.events.append("part")
        self.part_lengths.append(len(kwargs["Body"]))
        return {"ETag": str(kwargs["PartNumber"])}

    def complete_multipart_upload(self, **kwargs):
        self.events.append("complete")

    def abort_multipart_upload(self, **kwargs):
        self.events.append("abort")

    def delete_object(self, **kwargs):
        self.events.append("delete")


@pytest.mark.asyncio
async def test_results_are_unscanned_deterministic_eight_mib_parts_and_retry_rewrites(monkeypatch, settings):
    store = MultipartStore()
    monkeypatch.setattr(assets, "object_client", lambda c: store)
    monkeypatch.setattr(assets, "abort_multipart_uploads", lambda *a, **k: store.events.append("abort_old"))
    monkeypatch.setattr(assets, "start_multipart", lambda *a, **k: "upload")

    settings.batch_jsonl_max_bytes = 1  # Upload policy cannot cap generated results.
    monkeypatch.setattr(assets, "scanned_chunks", lambda *a: pytest.fail("generated results must not be scanned"))
    batch_id = str(uuid.uuid4())
    payload = b"a" * (8 * 1024 * 1024 + 17)

    async def lines():
        for start in range(0, len(payload), 64 * 1024):
            yield payload[start:start + 64 * 1024]

    first = await files.write_result_file(project_id="p", batch_id=batch_id, epoch=2, kind="output", lines=lines())
    second = await files.write_result_file(project_id="p", batch_id=batch_id, epoch=2, kind="output", lines=lines())
    assert first == second
    assert first.file_id == files.result_file_id(batch_id, 2, "output")
    assert first.file_id != files.result_file_id(batch_id, 3, "output")
    assert first.file_id != files.result_file_id(batch_id, 2, "error")
    assert first.size_bytes == len(payload)
    assert first.sha256 == hashlib.sha256(payload).hexdigest()
    assert store.part_lengths == [8 * 1024 * 1024, 17] * 2
    assert store.events.count("abort_old") == 2
    row = files.result_file_row(stored=first, project_id="p", user_id="u", api_key_id=None,
                               filename="out.jsonl", expires_at=datetime.now(UTC) + timedelta(days=7))
    assert isinstance(row, ChatBatchFile)
    assert (row.purpose, row.state, row.scan_result) == ("batch_output", "processed", "not_required")


@pytest.mark.asyncio
async def test_result_storage_failure_aborts_without_publication(monkeypatch, settings):
    store = MultipartStore()
    monkeypatch.setattr(assets, "object_client", lambda c: store)
    monkeypatch.setattr(assets, "abort_multipart_uploads", lambda *a, **k: None)
    monkeypatch.setattr(assets, "start_multipart", lambda *a, **k: "upload")

    def rejected(**kwargs):
        raise OSError("S3 write unavailable")

    async def lines():
        yield b"{}\n"

    monkeypatch.setattr(store, "upload_part", rejected)
    with pytest.raises(files.BatchFileUnavailable) as failure:
        await files.write_result_file(project_id="p", batch_id=str(uuid.uuid4()), epoch=1, kind="output", lines=lines())
    assert failure.value.code == "result_storage_unavailable"
    assert store.events == ["abort"]


@pytest.mark.asyncio
async def test_empty_result_has_no_file_and_removes_old_retry_object(monkeypatch, settings):
    store = MultipartStore()
    monkeypatch.setattr(assets, "object_client", lambda c: store)
    monkeypatch.setattr(assets, "abort_multipart_uploads", lambda *a, **k: store.events.append("abort_old"))

    async def empty():
        if False:
            yield b""

    assert await files.write_result_file(project_id="p", batch_id=str(uuid.uuid4()), epoch=1, kind="error", lines=empty()) is None
    assert store.events == ["abort_old", "delete"]


@pytest.mark.asyncio
async def test_completed_result_recovery_checks_head_hash_and_preserves_object(monkeypatch, settings):
    store = MultipartStore()
    payload = b'{"id":"already-completed"}\n'
    body = io.BytesIO(payload)
    monkeypatch.setattr(store, "head_object", lambda **kwargs: {"ContentLength": len(payload)})
    monkeypatch.setattr(store, "get_object", lambda **kwargs: {"Body": body}, raising=False)
    monkeypatch.setattr(assets, "object_client", lambda c: store)
    monkeypatch.setattr(assets, "abort_multipart_uploads", lambda *a, **k: store.events.append("abort_old"))
    monkeypatch.setattr(assets, "start_multipart", lambda *a, **k: "new-attempt")

    async def lines():
        yield payload

    result = await files.write_result_file(project_id="p", batch_id=str(uuid.uuid4()), epoch=1, kind="output", lines=lines())
    assert result.sha256 == hashlib.sha256(payload).hexdigest()
    assert result.size_bytes == len(payload)
    assert body.closed
    assert store.events == ["abort_old", "abort"]


@pytest.mark.asyncio
async def test_delete_asset_reports_accepted_batch_pin_without_run(monkeypatch):
    row = SimpleNamespace(id="a", project_id="p", user_id="u", status="clean")

    class Session:
        async def execute(self, statement):
            return SimpleNamespace(scalar_one_or_none=lambda: row)

        async def scalar(self, statement):
            # No message/run pins: only the not-yet-materialized batch item.
            return "a" if "chat_batch_items" in str(statement) else None

        async def commit(self):
            pass

    @asynccontextmanager
    async def factory():
        yield Session()

    monkeypatch.setattr(assets, "_require_session_factory", lambda: factory)
    result = await assets.delete_asset(asset_id="a", project_id="p", user_id="u")
    assert result == {"id": "a", "pending_cleanup": True}
    assert row.status == "deleting"


@pytest.mark.asyncio
@pytest.mark.parametrize("reply,accepted", [(b"stream: OK\0", True), (b"stream: virus FOUND\0", False), (b"size limit exceeded", False)])
async def test_file_and_stream_scanning_share_fail_closed_instream(tmp_path, monkeypatch, reply, accepted):
    import struct

    path = tmp_path / "input"
    path.write_bytes(b"{}\n")
    writers = []

    class Writer:
        def __init__(self):
            self.data = bytearray()
            self.closed = False

        def write(self, chunk):
            self.data.extend(chunk)

        async def drain(self):
            pass

        def close(self):
            self.closed = True

        async def wait_closed(self):
            pass

    class Reader:
        async def read(self, size):
            return reply

    async def connect(*args):
        writer = Writer()
        writers.append(writer)
        return Reader(), writer

    monkeypatch.setattr(assets, "_asset_config", lambda: {"scanner_host": "scanner", "scanner_port": 3310})
    monkeypatch.setattr(assets, "_assert_scanner_host", lambda host: None)
    monkeypatch.setattr(assets.asyncio, "open_connection", connect)

    async def source():
        yield b"{}\n"

    async def stream_scan():
        async for _ in assets.scanned_chunks(source()):
            pass

    for operation in (lambda: assets.scan_file(path), stream_scan):
        if accepted:
            await operation()
        else:
            with pytest.raises(assets.AssetError):
                await operation()
    expected = b"zINSTREAM\0" + struct.pack("!I", 3) + b"{}\n" + struct.pack("!I", 0)
    assert len(writers) == 2
    assert all(bytes(writer.data) == expected and writer.closed for writer in writers)


@pytest.mark.asyncio
async def test_generated_output_is_readable_without_upload_scan(monkeypatch, session):
    row = SimpleNamespace(purpose="batch_output", state="processed", scan_result="not_required")
    body = io.BytesIO(b"{}\n")

    async def owned(*args, **kwargs):
        return row

    async def download(result):
        assert result is row
        return assets.AssetDownload(body, "output.jsonl", "application/jsonl", 3)

    monkeypatch.setattr(files, "_owned", owned)
    monkeypatch.setattr(files, "_download", download)
    result = await files.open_batch_file_content(project_id="p", user_id="u", file_id="generated")
    assert b"".join([chunk async for chunk in result.chunks()]) == b"{}\n"
    assert body.closed
