"""Real MariaDB coordinator transactions under both snapshot-isolation modes.

Only object storage/scanning and provider credentials are replaced. Admission,
materialization, worker claims, cancellation, projection and publication use SQL.
Provider execution is forbidden: these are orchestration acceptance tests.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4, uuid5

import pytest
from sqlalchemy import delete, event, func, select, text

from lumen.config import get_settings
from lumen.crypto import decrypt_chat_content
from lumen.db import close_db, get_session_factory, init_db
from lumen.models.batch_contracts import NativeBatchCreateRequest, OpenAIBatchCreateRequest
from lumen.models.chat_batches import ChatBatch, ChatBatchFile, ChatBatchItem, ChatBatchProjectQueue
from lumen.models.chat_db import LlmModel, LlmProvider
from lumen.models.chat_infrastructure import ChatWorkerRegistration
from lumen.models.chat_runs import ChatModelCallReservation, ChatRun, ChatRunEventRow, ChatRunSegment
from lumen.services import assets, batch_files, batches
from lumen.services.durable_runs import images
from lumen.services.durable_runs.budgets import retry_deadlocks
from lumen.services.infrastructure.store import register_worker
from lumen.services.providers import routing
from lumen.services.run_store import claim_queued_run, complete_segment_io
from lumen.services.worker_routing import use_read_committed

pytestmark = pytest.mark.integration


def dump_segment_payload(payload: dict) -> str:
    """Ciphertext exactly as a worker checkpoint stores it, via the public completion API."""
    segment = ChatRunSegment(run_id=str(uuid4()), segment_id="fixture", ordinal=1,
                             endpoint="chat_completions", status="provider_started")
    complete_segment_io(segment, result_payload=payload, usage_payload=None)
    return segment.result_payload


@pytest.fixture(params=["ON", "OFF"])
async def batch_db(request, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "batch_enabled", True)
    monkeypatch.setattr(settings, "batch_dispatch_window", 2)
    monkeypatch.setattr(settings, "batch_project_dispatch_window", 3)
    monkeypatch.setattr(settings, "batch_validation_chunk_rows", 2)
    monkeypatch.setattr(batches, "_last_project", None)
    init_db(os.environ["DATABASE_URL"], pool_size=4, max_overflow=0)
    factory = get_session_factory()
    engine = factory.kw["bind"]
    nonce = uuid4().hex
    project_id, user_id = "batch-it-" + nonce, "batch-owner-" + nonce

    def configure_snapshot(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute(f"SET SESSION innodb_snapshot_isolation = {request.param}")
        cursor.close()

    event.listen(engine.sync_engine, "connect", configure_snapshot)
    async with factory() as session, session.begin():
        actual = (await session.execute(text("SELECT @@SESSION.innodb_snapshot_isolation"))).scalar_one()
        assert bool(actual) == (request.param == "ON")
        provider = LlmProvider(name="batch-provider-" + nonce, provider_type="openai", is_active=True,
                               margin_multiplier=Decimal("1"))
        session.add(provider)
        await session.flush()
        model = LlmModel(provider_id=provider.id, model_name="gpt-image-1", model_kind="image", is_active=True,
                         media_pricing={"image_variants": {"1024x1024:high": "0.04"}})
        session.add(model)
        await session.flush()
        model_id, provider_id = model.id, provider.id
    monkeypatch.setattr(routing, "resolve_api_key", lambda _provider: "fixture-private-key")
    monkeypatch.setattr(assets, "asset_pipeline_available", lambda: True)

    async def forbidden_provider(*_args, **_kwargs):
        raise AssertionError("coordinator must never invoke a provider")

    async def no_wake(_run_id):
        return None

    monkeypatch.setattr(images.image_transport, "generate_images", forbidden_provider)
    monkeypatch.setattr(batches, "wake_run", no_wake)
    online = await register_worker(worker_identity="batch-coordinator-" + nonce, boot_id=str(uuid4()),
        capacity=1, protocol_versions=[1, 2], plugin_digest="d" * 64, schema_version=1,
        workload_classes=["online_text"])
    runner = await register_worker(worker_identity="batch-runner-" + nonce, boot_id=str(uuid4()),
        capacity=8, protocol_versions=[1, 2], plugin_digest="d" * 64, schema_version=1,
        workload_classes=["batch"])

    def native(count=1, **overrides):
        return NativeBatchCreateRequest.model_validate({"items": [
            {"custom_id": f"item-{index}", "operation": "images.generations", "body": {
                "model_id": str(model_id), "prompt": "A cobalt cube", "size": "1024x1024", "quality": "high",
                **overrides}} for index in range(count)]})

    async def create(count=1, *, key=None, **overrides):
        return await batches.create_native_batch(project_id=project_id, user_id=user_id, api_key_id=None,
            scopes=None, source="web", idempotency_key=key or str(uuid4()), request=native(count, **overrides))

    async def tick(count=1):
        for _ in range(count):
            await batches.coordinate_once(owner=online)

    async def claim(run_id):
        async def transaction():
            async with factory() as session, session.begin():
                await use_read_committed(session)
                return await claim_queued_run(session, run_id, owner="batch-runner-" + nonce, registration_id=runner)
        return await retry_deadlocks(transaction)

    async def get(batch_id, contract="native"):
        return await batches.get_batch(project_id=project_id, user_id=user_id, batch_id=batch_id, contract=contract)

    async def items(batch_id):
        async with factory() as session:
            return (await session.execute(select(ChatBatchItem).where(ChatBatchItem.batch_id == batch_id)
                                           .order_by(ChatBatchItem.ordinal))).scalars().all()

    async def input_file():
        file_id = str(uuid4())
        async with factory() as session, session.begin():
            session.add(ChatBatchFile(id=file_id, project_id=project_id, user_id=user_id,
                purpose="batch", filename="input.jsonl", mime_type="application/jsonl", size_bytes=10,
                sha256="f" * 64, bucket_name="test", object_key="batch-test/" + file_id, state="processed",
                scan_result="clean", expires_at=datetime.now(UTC) + timedelta(days=1)))
        return file_id

    async def openai(file_id):
        return await batches.create_openai_batch(project_id=project_id, user_id=user_id, api_key_id=None,
            scopes=None, source="web", idempotency_key=None, request=OpenAIBatchCreateRequest(
                input_file_id="file-" + UUID(file_id).hex, endpoint="/v1/chat/completions", completion_window="24h"))

    try:
        yield SimpleNamespace(factory=factory, project_id=project_id, user_id=user_id, owner=online,
            native=native, create=create, tick=tick, claim=claim, get=get, items=items,
            input_file=input_file, openai=openai)
    finally:
        async with factory() as session, session.begin():
            batch_ids = select(ChatBatch.id).where(ChatBatch.project_id == project_id)
            run_ids = select(ChatRun.id).where(ChatRun.project_id == project_id)
            await session.execute(delete(ChatBatchItem).where(ChatBatchItem.batch_id.in_(batch_ids)))
            await session.execute(delete(ChatBatch).where(ChatBatch.project_id == project_id))
            await session.execute(delete(ChatModelCallReservation).where(ChatModelCallReservation.run_id.in_(run_ids)))
            await session.execute(delete(ChatRun).where(ChatRun.project_id == project_id))
            await session.execute(delete(ChatBatchFile).where(ChatBatchFile.project_id == project_id))
            await session.execute(delete(ChatBatchProjectQueue).where(ChatBatchProjectQueue.project_id == project_id))
            await session.execute(delete(ChatWorkerRegistration).where(ChatWorkerRegistration.id.in_([online, runner])))
            await session.execute(delete(LlmModel).where(LlmModel.id == model_id))
            await session.execute(delete(LlmProvider).where(LlmProvider.id == provider_id))
        event.remove(engine.sync_engine, "connect", configure_snapshot)
        await close_db()


async def test_duplicate_native_create_and_conflict(batch_db):
    db = batch_db
    first, second = await asyncio.gather(db.create(2, key="same-key"), db.create(2, key="same-key"))
    assert first[0].id == second[0].id
    assert sorted([first[1], second[1]]) == [False, True]
    with pytest.raises(batches.BatchConflict):
        await db.create(2, key="same-key", prompt="Different request")
    assert len(await db.items(first[0].id)) == 2
    with pytest.raises(batches.BatchNotFound):
        await batches.get_batch(project_id=db.project_id, user_id="foreign", batch_id=first[0].id, contract="native")
    with pytest.raises(batches.BatchNotFound):
        await batches.cancel_batch(project_id="foreign", user_id=db.user_id, batch_id=first[0].id, contract="native")


async def test_validation_failure_has_errors_and_zero_runs(batch_db):
    db = batch_db
    view, _ = await db.create(1, model_id="missing-" + uuid4().hex)
    await db.tick()
    failed = await db.get(view.id)
    assert failed.status == "failed"
    assert failed.errors[0]["custom_id"] == "item-0"
    assert failed.errors[0]["code"] == "invalid_batch_input"
    async with db.factory() as session:
        assert await session.scalar(select(func.count()).select_from(ChatRun).where(ChatRun.batch_id == view.id)) == 0
    assert (await db.items(view.id))[0].run_id is None


async def test_openai_invalid_jsonl_fails_without_materializing(batch_db, monkeypatch):
    db = batch_db
    view = await db.openai(await db.input_file())
    calls = []

    async def read_chunk(**kwargs):
        calls.append(kwargs)
        return batch_files.InputChunk(lines=[batch_files.InputLine(start_byte=0, raw=b'{"method":"GET"}')],
                                      next_byte=16, eof=True)

    monkeypatch.setattr(batch_files, "read_input_chunk", read_chunk)
    await db.tick()
    failed = await db.get(view.id, "openai")
    assert failed.status == "failed" and failed.errors[0]["line"] == 1
    assert calls[0]["max_rows"] <= 100 and calls[0]["max_bytes"] <= 1024 * 1024
    async with db.factory() as session:
        assert await session.scalar(select(func.count()).select_from(ChatRun).where(ChatRun.batch_id == view.id)) == 0


async def test_batch_project_windows_and_deterministic_materialization(batch_db):
    db = batch_db
    first, _ = await db.create(5)
    second, _ = await db.create(5)
    await db.tick(16)
    first_items, second_items = await db.items(first.id), await db.items(second.id)
    first_active = [item for item in first_items if item.run_id]
    second_active = [item for item in second_items if item.run_id]
    assert len(first_active) <= 2 and len(second_active) <= 2
    assert len(first_active) + len(second_active) == 3
    assert first_active and second_active  # round-robin does not monopolize the project
    async with db.factory() as session:
        for view, items in ((first, first_active), (second, second_active)):
            for item in items:
                run = await session.get(ChatRun, item.run_id)
                assert run.workload_class == "batch" and run.batch_id == view.id
                assert run.client_request_id == str(uuid5(UUID(view.id), item.custom_id))
        batch = await session.get(ChatBatch, first.id)
        queue = await session.get(ChatBatchProjectQueue, db.project_id)
        registration = await session.get(ChatWorkerRegistration, db.owner)
        assert batch.lease_owner is None and queue.lease_owner is None
        assert registration.auxiliary_active == 0


async def test_cancel_commit_fences_a_waiting_claim(batch_db):
    db = batch_db
    view, _ = await db.create(3)
    await db.tick(3)
    run_id = next(item.run_id for item in await db.items(view.id) if item.run_id)
    cancelling_committed = asyncio.Event()

    async def cancel():
        result = await batches.cancel_batch(project_id=db.project_id, user_id=db.user_id,
                                            batch_id=view.id, contract="native")
        assert result.status == "cancelling"
        cancelling_committed.set()

    async def claim_after_commit():
        await cancelling_committed.wait()
        return await db.claim(run_id)

    _, claimed = await asyncio.gather(cancel(), claim_after_commit())
    assert claimed is None
    await db.tick(4)
    assert (await db.get(view.id)).status == "cancelled"
    assert all(item.state == "cancelled" for item in await db.items(view.id))


async def test_expiry_of_never_started_items(batch_db):
    db = batch_db
    view, _ = await db.create(3)
    async with db.factory() as session, session.begin():
        row = await session.get(ChatBatch, view.id)
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db.tick(3)
    final = await db.get(view.id)
    assert final.status == "expired" and final.counts["expired"] == 3
    assert all(item.run_id is None and item.error_code == "batch_expired" for item in await db.items(view.id))


async def test_checkpointed_queued_run_is_not_cancelled(batch_db, monkeypatch):
    db = batch_db
    view, _ = await db.create()
    await db.tick(2)
    item = (await db.items(view.id))[0]
    async with db.factory() as session, session.begin():
        session.add(ChatRunSegment(run_id=item.run_id, segment_id="image:1", ordinal=1, endpoint="images",
            status="completed", result_payload=dump_segment_payload({"asset_ids": []})))
    await batches.cancel_batch(project_id=db.project_id, user_id=db.user_id, batch_id=view.id, contract="native")

    async def forbidden_cancel(**_kwargs):
        raise AssertionError("checkpointed queued run must settle, not cancel")

    monkeypatch.setattr(batches.lifecycle, "request_cancelled", forbidden_cancel)
    await db.tick(2)
    async with db.factory() as session:
        run = await session.get(ChatRun, item.run_id)
        assert run.status == "queued" and run.cancel_requested_at is None
    claimed = await db.claim(item.run_id)
    assert claimed is not None and claimed.status == "running"
    assert (await db.get(view.id)).status == "cancelling"


async def test_individual_cancel_preserves_checkpointed_run_for_settlement(batch_db):
    db = batch_db
    view, _ = await db.create()
    await db.tick(2)
    item = (await db.items(view.id))[0]
    async with db.factory() as session, session.begin():
        session.add(ChatRunSegment(run_id=item.run_id, segment_id="image:1", ordinal=1,
            endpoint="images", status="completed", result_payload=dump_segment_payload({"asset_ids": []})))
        session.add(ChatModelCallReservation(run_id=item.run_id, segment_id="image:1",
            bound_credits=Decimal("1"), status="reserved"))
    result = await batches.lifecycle.request_cancelled(
        run_id=item.run_id, project_id=db.project_id, user_id=db.user_id)
    assert result.status == "queued" and not result.terminal
    async with db.factory() as session:
        run = await session.get(ChatRun, item.run_id)
        hold = await session.get(ChatModelCallReservation, (item.run_id, "image:1"))
        assert run.cancel_requested_at is None
        assert hold.status == "reserved" and hold.actual_credits is None
    claimed = await db.claim(item.run_id)
    assert claimed is not None and claimed.status == "running"


async def test_cancel_grace_freezes_unknown_without_releasing_hold(batch_db):
    db = batch_db
    view, _ = await db.create()
    await db.tick(2)
    item = (await db.items(view.id))[0]
    assert await db.claim(item.run_id) is not None
    async with db.factory() as session, session.begin():
        session.add(ChatModelCallReservation(run_id=item.run_id, segment_id="image:1",
                                            bound_credits=Decimal("1"), status="reserved"))
    await batches.cancel_batch(project_id=db.project_id, user_id=db.user_id, batch_id=view.id, contract="native")
    async with db.factory() as session, session.begin():
        row = await session.get(ChatBatch, view.id)
        row.cancelling_at = datetime.now(UTC) - timedelta(seconds=601)
    await db.tick()
    assert (await db.get(view.id)).status == "cancelled"
    item = (await db.items(view.id))[0]
    assert item.state == "unknown" and item.error_code == "provider_result_unknown"
    async with db.factory() as session:
        hold = await session.get(ChatModelCallReservation, (item.run_id, "image:1"))
        assert hold.status == "reserved" and hold.actual_credits is None


@pytest.mark.parametrize("status, hold_status, error_code", [
    (400, "settled", "provider_rejected"),
    (500, "unknown", "provider_result_unknown"),
])
async def test_batch_image_executor_refusal_is_single_call_and_preserves_accounting(
        batch_db, monkeypatch, status, hold_status, error_code):
    from lumen.services.durable_runs.common import _payload
    from lumen.services.providers import image_transport

    db = batch_db
    view, _ = await db.create()
    await db.tick(2)
    item = (await db.items(view.id))[0]
    claimed = await db.claim(item.run_id)
    assert claimed is not None
    calls = []

    async def reject(*_args, **_kwargs):
        calls.append(status)
        raise image_transport.ImageTransportError("upstream rejected request", status_code=status)

    monkeypatch.setattr(image_transport, "generate_images", reject)
    assert await images.execute_image_run(item.run_id, owner=claimed.lease_owner,
        payload={**_payload(claimed), "user_id": db.user_id, "project_id": db.project_id},
        capability_snapshot=claimed.capability_snapshot, pricing_snapshot=claimed.pricing_snapshot)
    async with db.factory() as session:
        run = await session.get(ChatRun, item.run_id)
        hold = await session.get(ChatModelCallReservation, (item.run_id, "image:1"))
        terminal = (await session.execute(select(ChatRunEventRow).where(
            ChatRunEventRow.run_id == item.run_id, ChatRunEventRow.event_type == "run.failed"))).scalar_one()
        assert run.status == "failed"
        assert json.loads(decrypt_chat_content(terminal.payload))["error_code"] == error_code
        assert hold.status == hold_status
        assert hold.actual_credits == (Decimal("0") if status == 400 else None)
    assert calls == [status]


async def test_confirmed_media_rejection_releases_batch_hold_and_terminal_state_atomically(batch_db, monkeypatch):
    from lumen.services.durable_runs import execution, media
    from lumen.services.providers import image_transport

    db = batch_db
    view, _ = await db.create()
    await db.tick(2)
    item = (await db.items(view.id))[0]
    assert await db.claim(item.run_id) is not None
    async with db.factory() as session, session.begin():
        owner = (await session.get(ChatRun, item.run_id)).lease_owner
        session.add(ChatRunSegment(run_id=item.run_id, segment_id="image:1", ordinal=1,
                                   endpoint="image_generation", status="provider_started"))
        session.add(ChatModelCallReservation(run_id=item.run_id, segment_id="image:1",
                                            bound_credits=Decimal("1"), status="reserved"))
    assert media.confirmed_media_rejection(image_transport.ImageTransportError("x", status_code=500)) is None
    assert media.confirmed_media_rejection(image_transport.ImageTransportError("x", status_code=429)) == 429
    original_append_event = execution.append_event

    async def fail_terminal_event(*_args, **_kwargs):
        raise RuntimeError("terminal event write failed")

    monkeypatch.setattr(execution, "append_event", fail_terminal_event)
    with pytest.raises(RuntimeError, match="terminal event write failed"):
        await execution._finish(item.run_id, owner=owner, status="failed", message_id=None,
                                error_code="provider_rejected", safe_message="upstream provider rejected the request")
    async with db.factory() as session:
        run = await session.get(ChatRun, item.run_id)
        hold = await session.get(ChatModelCallReservation, (item.run_id, "image:1"))
        segment = await session.get(ChatRunSegment, (item.run_id, "image:1"))
        assert run.status == "running" and run.usage_reconciled_at is None
        assert hold.status == "reserved" and hold.actual_credits is None
        assert segment.status == "provider_started"

    monkeypatch.setattr(execution, "append_event", original_append_event)
    await execution._finish(item.run_id, owner=owner, status="failed", message_id=None,
                            error_code="provider_rejected", safe_message="upstream provider rejected the request")
    async with db.factory() as session:
        run = await session.get(ChatRun, item.run_id)
        hold = await session.get(ChatModelCallReservation, (item.run_id, "image:1"))
        segment = await session.get(ChatRunSegment, (item.run_id, "image:1"))
        terminal = (await session.execute(select(ChatRunEventRow).where(
            ChatRunEventRow.run_id == item.run_id, ChatRunEventRow.event_type == "run.failed"))).scalar_one()
        assert run.status == "failed"
        assert json.loads(decrypt_chat_content(terminal.payload))["error_code"] == "provider_rejected"
        assert run.usage_reconciled_at is not None
        assert hold.status == "settled" and hold.actual_credits == 0
        assert segment.status == "failed"


async def _completed_text(db, view, *, custom_id="text", status_code=200):
    run_id = str(uuid4())
    envelope = {"status_code": status_code, "request_id": run_id, "body": {"choices": [{"message": {"content": "frozen"}}]}}
    async with db.factory() as session, session.begin():
        batch = await session.get(ChatBatch, view.id)
        batch.status = "in_progress"
        batch.request_total = batch.request_pending = 1
        session.add(ChatRun(id=run_id, run_scope="api", run_kind="api_completion", project_id=db.project_id,
            user_id=db.user_id, model_name="fixture", workload_class="batch", batch_id=view.id,
            client_request_id=str(uuid5(UUID(view.id), custom_id)), request_fingerprint="f" * 64, fingerprint_version=1,
            capability_snapshot={}, pricing_snapshot={}, status="completed" if status_code < 400 else "failed"))
        await session.flush()
        session.add(ChatRunSegment(run_id=run_id, segment_id="api_completion:1", ordinal=1,
            endpoint="chat_completions", status="completed", result_payload=dump_segment_payload({"envelope": envelope})))
        if view.contract == "native":
            item = await session.get(ChatBatchItem, (view.id, 1))
            item.operation, item.run_id, item.state = "chat.completions", run_id, "queued"
        else:
            session.add(ChatBatchItem(batch_id=view.id, ordinal=1, custom_id=custom_id,
                custom_id_hash=hashlib.sha256(custom_id.encode()).hexdigest(), operation="chat.completions",
                request_ciphertext=batches._seal({}), run_id=run_id, state="queued"))
    return run_id, envelope


async def test_native_published_response_is_frozen(batch_db):
    db = batch_db
    view, _ = await db.create()
    run_id, envelope = await _completed_text(db, view)
    await db.tick()
    assert (await db.get(view.id)).status == "completed"
    first, _ = await batches.list_batch_items(project_id=db.project_id, user_id=db.user_id,
                                              batch_id=view.id, after=None, limit=100)
    assert first[0].response == envelope["body"]
    async with db.factory() as session, session.begin():
        segment = await session.get(ChatRunSegment, (run_id, "api_completion:1"))
        segment.result_payload = dump_segment_payload({"envelope": {**envelope, "body": {"late": True}}})
    await db.tick()
    later, _ = await batches.list_batch_items(project_id=db.project_id, user_id=db.user_id,
                                              batch_id=view.id, after=None, limit=100)
    assert later == first


@pytest.mark.parametrize("output_seconds", [None, 3600])
async def test_openai_finalization_publishes_ids_and_status_atomically(batch_db, monkeypatch, output_seconds):
    db = batch_db
    view = await db.openai(await db.input_file())
    _, envelope = await _completed_text(db, view)
    async with db.factory() as session, session.begin():
        batch = await session.get(ChatBatch, view.id)
        batch.created_at = datetime.now(UTC) - timedelta(hours=2)
        batch.output_expires_after_seconds = output_seconds
    writes = {}

    async def write_result(**kwargs):
        # Other connections see finalizing and NO file IDs during both uploads.
        during = await db.get(view.id, "openai")
        assert during.status == "finalizing"
        assert during.output_file_id is None and during.error_file_id is None
        lines = [line async for line in kwargs["lines"]]
        writes[kwargs["kind"]] = lines
        if not lines:
            return None
        data = b"".join(lines)
        return batch_files.StoredResult(file_id=batch_files.result_file_id(view.id, kwargs["epoch"], kwargs["kind"]),
            bucket="test", object_key=f"batch-results/{view.id}/{kwargs['epoch']}/{kwargs['kind']}",
            size_bytes=len(data), sha256=hashlib.sha256(data).hexdigest())

    monkeypatch.setattr(batch_files, "write_result_file", write_result)
    await db.tick()
    final = await db.get(view.id, "openai")
    assert final.status == "completed" and final.output_file_id is not None and final.error_file_id is None
    import json
    row = json.loads(writes["output"][0])
    assert row["custom_id"] == "text" and row["response"] == envelope and row["error"] is None
    assert writes["error"] == []
    async with db.factory() as session:
        file = await session.get(ChatBatchFile, final.output_file_id)
        assert file.state == "processed" and file.purpose == "batch_output"
        assert file.sha256 == hashlib.sha256(writes["output"][0]).hexdigest()
        expected_seconds = output_seconds or get_settings().batch_result_ttl_days * 86400
        assert abs((file.expires_at - file.created_at).total_seconds() - expected_seconds) < 2
        assert file.expires_at.replace(tzinfo=UTC) > datetime.now(UTC)


async def test_result_storage_failure_is_not_empty_success(batch_db, monkeypatch):
    db = batch_db
    view = await db.openai(await db.input_file())
    await _completed_text(db, view)

    async def unavailable(**_kwargs):
        raise batches.BatchUnavailable("result_storage_unavailable")

    monkeypatch.setattr(batch_files, "write_result_file", unavailable)
    await db.tick()
    final = await db.get(view.id, "openai")
    assert final.status == "failed" and final.errors[0]["code"] == "result_storage_unavailable"
    assert final.output_file_id is None and final.error_file_id is None
    assert (await db.items(view.id))[0].result_ciphertext is not None


async def test_expired_materialized_but_never_started_run_has_expired_result(batch_db):
    db = batch_db
    view, _ = await db.create()
    await db.tick(2)
    item = (await db.items(view.id))[0]
    async with db.factory() as session, session.begin():
        row = await session.get(ChatBatch, view.id)
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert await db.claim(item.run_id) is None
    await db.tick(3)
    final = await db.get(view.id)
    item = (await db.items(view.id))[0]
    assert final.status == "expired" and final.counts["expired"] == 1
    assert item.state == "expired" and item.error_code == "batch_expired"
    async with db.factory() as session:
        run = await session.get(ChatRun, item.run_id)
        assert run.status == "canceled" and run.provider_started_at is None


async def test_grace_elapsed_does_not_turn_unstarted_queued_run_into_unknown(batch_db):
    db = batch_db
    view, _ = await db.create()
    await db.tick(2)
    await batches.cancel_batch(project_id=db.project_id, user_id=db.user_id, batch_id=view.id, contract="native")
    async with db.factory() as session, session.begin():
        row = await session.get(ChatBatch, view.id)
        row.cancelling_at = datetime.now(UTC) - timedelta(seconds=601)
    await db.tick(2)
    item = (await db.items(view.id))[0]
    assert item.state == "cancelled"
    assert (await db.get(view.id)).counts["unknown"] == 0


async def test_coordinator_lease_is_registration_owned_and_drain_waits_for_release(batch_db, monkeypatch):
    from lumen.services.infrastructure import store

    db = batch_db
    view, _ = await db.create()
    entered, release = asyncio.Event(), asyncio.Event()
    prepare = batches._prepare

    async def paused_prepare(*args, **kwargs):
        entered.set()
        await release.wait()
        return await prepare(*args, **kwargs)

    monkeypatch.setattr(batches, "_prepare", paused_prepare)
    step = asyncio.create_task(batches.coordinate_once(owner=db.owner))
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)
        async with db.factory() as session:
            row = await session.get(ChatBatch, view.id)
            queue = await session.get(ChatBatchProjectQueue, db.project_id)
            registration = await session.get(ChatWorkerRegistration, db.owner)
            assert row.lease_owner == queue.lease_owner == db.owner
            assert registration.auxiliary_active == 1
        assert await store.start_drain(db.owner)
        assert not await store.acknowledge_drain(db.owner)
    finally:
        release.set()
        await step
    assert not await batches.coordinate_once(owner=db.owner)
    async with db.factory() as session:
        row = await session.get(ChatBatch, view.id)
        queue = await session.get(ChatBatchProjectQueue, db.project_id)
        registration = await session.get(ChatWorkerRegistration, db.owner)
        assert row.lease_owner is None and queue.lease_owner is None
        assert registration.auxiliary_active == 0
    assert await store.acknowledge_drain(db.owner)
