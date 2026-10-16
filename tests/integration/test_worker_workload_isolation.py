"""Real SQL class, capacity and drain fences; no provider or cloud calls."""

from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, event, select, text

from lumen.db import close_db, get_session_factory, init_db
from lumen.models.chat_batches import ChatBatch, ChatBatchItem
from lumen.models.chat_infrastructure import ChatWorkerRegistration
from lumen.models.chat_runs import ChatRun
from lumen.services.durable_runs.budgets import retry_deadlocks
from lumen.services.durable_runs.execution import queued_run_ids
from lumen.services.infrastructure import store
from lumen.services.run_store import claim_queued_run, renew_run_lease
from lumen.services.worker_routing import use_read_committed

pytestmark = pytest.mark.integration


@pytest.fixture(params=["ON", "OFF"])
async def workload_db(request):
    init_db(os.environ["DATABASE_URL"], pool_size=4, max_overflow=0)
    factory = get_session_factory()
    engine = factory.kw["bind"]
    project_id = "workload-it-" + uuid.uuid4().hex
    registrations = {}

    def configure_snapshot(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute(f"SET SESSION innodb_snapshot_isolation = {request.param}")
        cursor.close()

    event.listen(engine.sync_engine, "connect", configure_snapshot)
    async with factory() as session, session.begin():
        actual = (await session.execute(text("SELECT @@SESSION.innodb_snapshot_isolation"))).scalar_one()
        assert bool(actual) == (request.param == "ON")
        for name, classes in (("online", ["online_text", "online_media"]), ("batch", ["batch"])):
            row = ChatWorkerRegistration(
                id=str(uuid.uuid4()), worker_identity=project_id + "-" + name,
                boot_id=str(uuid.uuid4()), workload_classes=classes,
                protocol_versions=[1, 2], plugin_digest="d" * 64,
                schema_version=1, capacity=1, active_count=0,
            )
            session.add(row)
            registrations[name] = row

    async def add_run(workload_class="online_text", **overrides):
        run = ChatRun(
            id=str(uuid.uuid4()), run_scope="temp", project_id=project_id,
            user_id="owner", model_name="fixture", capability_snapshot={},
            pricing_snapshot={}, client_request_id=str(uuid.uuid4()),
            request_fingerprint="f" * 64, fingerprint_version=1,
            workload_class=workload_class, required_plugin_digest="d" * 64,
            **overrides,
        )
        async with factory() as session, session.begin():
            session.add(run)
        return run

    async def add_batch_run():
        batch = ChatBatch(
            id=str(uuid.uuid4()), project_id=project_id, user_id="owner",
            contract="native", status="in_progress", request_total=1,
            idempotency_key_hash=hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
            request_fingerprint="f" * 64,
        )
        async with factory() as session, session.begin():
            session.add(batch)
        run = await add_run("batch", run_kind="api_completion", batch_id=batch.id)
        async with factory() as session, session.begin():
            session.add(ChatBatchItem(
                batch_id=batch.id, ordinal=1, custom_id="item",
                custom_id_hash=hashlib.sha256(b"item").hexdigest(),
                operation="chat.completions", request_ciphertext="not-used-by-claim",
                capability_snapshot={}, pricing_snapshot={}, required_scopes=[],
                run_id=run.id, state="queued",
            ))
        return batch, run

    async def claim(run, worker="online"):
        registration = registrations[worker]

        async def transaction():
            async with factory() as session, session.begin():
                await use_read_committed(session)
                return await claim_queued_run(
                    session, run.id, owner=registration.worker_identity,
                    registration_id=registration.id,
                )

        return await retry_deadlocks(transaction)

    try:
        yield SimpleNamespace(
            factory=factory, project_id=project_id, registrations=registrations,
            add_run=add_run, add_batch_run=add_batch_run, claim=claim,
        )
    finally:
        async with factory() as session, session.begin():
            owned_batches = select(ChatBatch.id).where(ChatBatch.project_id == project_id)
            await session.execute(delete(ChatBatchItem).where(ChatBatchItem.batch_id.in_(owned_batches)))
            await session.execute(delete(ChatBatch).where(ChatBatch.project_id == project_id))
            await session.execute(delete(ChatRun).where(ChatRun.project_id == project_id))
            await session.execute(delete(ChatWorkerRegistration).where(
                ChatWorkerRegistration.id.in_([row.id for row in registrations.values()])
            ))
        event.remove(engine.sync_engine, "connect", configure_snapshot)
        await close_db()


async def test_fixed_workers_cannot_steal_batch_realtime_or_pinned_work(workload_db):
    db = workload_db
    text_run = await db.add_run()
    media_run = await db.add_run("online_media", run_kind="image")
    _, batch_run = await db.add_batch_run()
    realtime = await db.add_run("realtime", run_kind="realtime")
    pinned = await db.add_run(worker_pool_id=str(uuid.uuid4()))
    other_protocol = await db.add_run(execution_protocol_version=3)
    owned_ids = {run.id for run in (text_run, media_run, batch_run, realtime, pinned, other_protocol)}
    online_candidates = set(await queued_run_ids(
        registration_id=db.registrations["online"].id, limit=100,
    ))
    batch_candidates = set(await queued_run_ids(
        registration_id=db.registrations["batch"].id, limit=100,
    ))
    assert online_candidates & owned_ids == {text_run.id, media_run.id}
    assert batch_candidates & owned_ids == {batch_run.id}
    for excluded in (batch_run, realtime, pinned, other_protocol):
        assert await db.claim(excluded) is None
    assert await db.claim(text_run, "batch") is None
    claimed = await db.claim(batch_run, "batch")
    assert claimed.worker_registration_id == db.registrations["batch"].id
    assert claimed.status == "running"


async def test_live_leases_not_heartbeat_counts_serialize_capacity(workload_db):
    db = workload_db
    first, second = await db.add_run(), await db.add_run()
    results = await asyncio.gather(db.claim(first), db.claim(second))
    assert sum(result is not None for result in results) == 1
    claimed = next(result for result in results if result is not None)
    unclaimed = second if claimed.id == first.id else first
    # A heartbeat cannot erase the live lease that consumes the only slot.
    assert await store.heartbeat_worker(db.registrations["online"].id, accepting=True)
    assert await db.claim(unclaimed) is None
    async with db.factory() as session, session.begin():
        row = await session.get(ChatRun, claimed.id)
        row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    replacement = await db.claim(unclaimed)
    assert replacement.worker_registration_id == claimed.worker_registration_id
    assert replacement.status == "running"


async def test_drain_stops_new_claims_but_keeps_owned_lease_renewable(workload_db):
    db = workload_db
    running, queued = await db.add_run(), await db.add_run()
    claimed = await db.claim(running)
    registration_id = db.registrations["online"].id
    assert await store.start_drain(registration_id)
    assert await store.heartbeat_worker(registration_id, accepting=True)
    assert await db.claim(queued) is None
    async with db.factory() as session, session.begin():
        await use_read_committed(session)
        renewed = await renew_run_lease(session, claimed.id, owner=claimed.lease_owner)
        assert renewed.status == "running"
        registration = await session.get(ChatWorkerRegistration, registration_id)
        assert registration.draining and not registration.accepting
        assert registration.drain_ack_at is None


async def test_batch_cancel_and_deadline_fence_precede_claim(workload_db):
    db = workload_db
    batch, run = await db.add_batch_run()
    async with db.factory() as session, session.begin():
        row = await session.get(ChatBatch, batch.id)
        row.status = "cancelling"
        row.cancelling_at = datetime.now(UTC)
    assert await db.claim(run, "batch") is None
    async with db.factory() as session, session.begin():
        row = await session.get(ChatBatch, batch.id)
        row.status = "in_progress"
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert await db.claim(run, "batch") is None
    async with db.factory() as session:
        untouched = await session.get(ChatRun, run.id)
        assert untouched.status == "queued" and untouched.provider_started_at is None
