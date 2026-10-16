"""Real MariaDB file pins under both InnoDB snapshot-isolation modes."""
from __future__ import annotations

import io
import os
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, event, text

from lumen.db import close_db, get_session_factory, init_db
from lumen.models.chat_batches import ChatBatch, ChatBatchFile
from lumen.services import assets
from lumen.services import batch_files as files

pytestmark = pytest.mark.integration


class ObjectStore:
    def __init__(self):
        self.objects = {}
        self.deleted = []
        self.aborted = []
        self.uploads = []

    def head_bucket(self, **kwargs):
        return {}

    def put(self, path, *, bucket, key, **kwargs):
        self.objects[(bucket, key)] = path.read_bytes()

    def get_object(self, *, Bucket, Key, Range=None):
        data = self.objects[(Bucket, Key)]
        if Range:
            data = data[int(Range.split("=")[1].split("-")[0]):]
        return {"Body": io.BytesIO(data), "ContentLength": len(data)}

    def delete_object(self, *, Bucket, Key):
        self.deleted.append((Bucket, Key))
        self.objects.pop((Bucket, Key), None)

    def get_paginator(self, operation):
        assert operation == "list_multipart_uploads"
        return self

    def paginate(self, *, Bucket, Prefix):
        yield {"Uploads": [upload for upload in self.uploads if upload["Key"].startswith(Prefix)]}

    def list_multipart_uploads(self, *, Bucket, Prefix, MaxUploads, **kwargs):
        return {"Uploads": [upload for upload in self.uploads
                            if upload["Bucket"] == Bucket and upload["Key"].startswith(Prefix)][:MaxUploads]}

    def abort_multipart_upload(self, *, Bucket, Key, UploadId):
        self.aborted.append((Bucket, Key, UploadId))
        self.uploads = [row for row in self.uploads if row["UploadId"] != UploadId]


@pytest.mark.parametrize("snapshot", ["ON", "OFF"])
async def test_deleted_and_expired_pinned_input_survives_gc_until_terminal(snapshot, monkeypatch, tmp_path):
    init_db(os.environ["DATABASE_URL"], pool_size=2, max_overflow=0)
    factory = get_session_factory()
    engine = factory.kw["bind"]

    def configure(dbapi_connection, record):
        cursor = dbapi_connection.cursor()
        cursor.execute(f"SET SESSION innodb_snapshot_isolation={snapshot}")
        cursor.close()

    event.listen(engine.sync_engine, "connect", configure)
    nonce = uuid.uuid4().hex
    project, user = "file-gc-p-" + nonce, "file-gc-u-" + nonce
    settings = SimpleNamespace(batch_enabled=True, batch_jsonl_max_bytes=200_000_000, batch_input_ttl_days=30)
    monkeypatch.setattr(files, "get_settings", lambda: settings)
    monkeypatch.setattr(files, "_config", lambda: {"bucket": "lumen-assets", "encryption": "none"})
    store = ObjectStore()
    monkeypatch.setattr(assets, "object_client", lambda config: store)
    monkeypatch.setattr(assets, "put_file", store.put)

    async def scan(path):
        return None

    monkeypatch.setattr(assets, "scan_file", scan)
    path = tmp_path / "input.jsonl"
    path.write_bytes(b"{}\n{}\n")
    batch_id = str(uuid.uuid4())
    try:
        async with factory() as session:
            assert bool((await session.execute(text("SELECT @@SESSION.innodb_snapshot_isolation"))).scalar_one()) == (snapshot == "ON")
        uploaded = await files.create_batch_file(project_id=project, user_id=user, api_key_id=None,
                                                filename="input.jsonl", path=path, size_bytes=6)
        for wrong_project, wrong_user in ((project, "other"), ("other", user)):
            with pytest.raises(files.BatchFileNotFound):
                await files.get_batch_file(project_id=wrong_project, user_id=wrong_user, file_id=uploaded.id)
            with pytest.raises(files.BatchFileNotFound):
                await files.delete_batch_file(project_id=wrong_project, user_id=wrong_user, file_id=uploaded.id)
            with pytest.raises(files.BatchFileNotFound):
                await files.open_batch_file_content(project_id=wrong_project, user_id=wrong_user, file_id=uploaded.id)
        async with factory() as session, session.begin():
            # Match admission's same-file lock until the pin commits.
            row = await session.get(ChatBatchFile, uploaded.id, with_for_update=True)
            session.add(ChatBatch(id=batch_id, project_id=project, user_id=user, contract="openai",
                                  endpoint="/v1/chat/completions", status="validating", input_file_id=row.id,
                                  request_fingerprint="f" * 64, created_at=datetime.now(UTC),
                                  expires_at=datetime.now(UTC) + timedelta(hours=24)))
        deleted = await files.delete_batch_file(project_id=project, user_id=user, file_id=uploaded.id)
        assert deleted.state == "deleting"
        with pytest.raises(files.BatchFileNotFound):
            await files.open_batch_file_content(project_id=project, user_id=user, file_id=uploaded.id)
        async with factory() as session, session.begin():
            row = await session.get(ChatBatchFile, uploaded.id)
            row.expires_at = datetime.now(UTC) - timedelta(days=1)
            batch = await session.get(ChatBatch, batch_id)
            batch.status, batch.finalization_epoch = "finalizing", 1
        bucket = assets.project_bucket_name("lumen-assets", project)
        old_key = f"batch-files/result/{batch_id}/0/output.jsonl"
        current_key = f"batch-files/result/{batch_id}/1/output.jsonl"
        store.uploads = [{"Bucket": bucket, "Key": key, "UploadId": upload_id,
                          "Initiated": datetime.now(UTC) - timedelta(hours=2)}
                         for key, upload_id in ((old_key, "orphan"), (current_key, "current"))]
        await files.gc_batch_files(limit=100)
        assert not store.deleted
        assert (bucket, old_key, "orphan") in store.aborted
        assert (bucket, current_key, "current") not in store.aborted
        chunk = await files.read_input_chunk(file_id=uploaded.id, start_byte=0, max_rows=1,
                                              max_bytes=1024, max_line_bytes=4 * 1024 * 1024)
        assert chunk.lines == [files.InputLine(0, b"{}\n")]
        async with factory() as session, session.begin():
            batch = await session.get(ChatBatch, batch_id, with_for_update=True)
            batch.status, batch.failed_at = "failed", datetime.now(UTC)
        await files.gc_batch_files(limit=100)
        assert len(store.deleted) == 1
        assert (bucket, current_key, "current") in store.aborted
        async with factory() as session:
            row = await session.get(ChatBatchFile, uploaded.id)
            assert row.state == "deleted" and row.deleted_at is not None
        with pytest.raises(files.BatchFileInputError):
            await files.read_input_chunk(file_id=uploaded.id, start_byte=0, max_rows=1, max_bytes=1024, max_line_bytes=1024)
    finally:
        async with factory() as session, session.begin():
            await session.execute(delete(ChatBatch).where(ChatBatch.project_id == project))
            await session.execute(delete(ChatBatchFile).where(ChatBatchFile.project_id == project))
        event.remove(engine.sync_engine, "connect", configure)
        await close_db()
