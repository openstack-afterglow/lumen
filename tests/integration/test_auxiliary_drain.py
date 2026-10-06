"""Real registration/lease drain boundaries with MariaDB snapshot isolation ON/OFF."""
from __future__ import annotations

import asyncio
import os
import re
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, event, select, text
from sqlalchemy.pool import Pool

from lumen.crypto import encrypt_chat_content
from lumen.db import close_db, get_session_factory, init_db
from lumen.models.chat_batches import ChatBatch, ChatBatchProjectQueue
from lumen.models.chat_db import ChatConversation, ChatMemory, ChatMessageGraph
from lumen.models.chat_infrastructure import ChatRuntimePool, ChatRuntimeResource, ChatWorkerRegistration
from lumen.models.chat_jobs import ChatJob, ChatMemoryOutbox
from lumen.models.chat_runs import ChatRun
from lumen.services import auxiliary, memory_jobs, memory_outbox, title_jobs
from lumen.services.infrastructure import store
from lumen.services.worker_routing import use_read_committed

pytestmark = pytest.mark.integration


@pytest.fixture(params=["ON", "OFF"])
async def auxiliary_db(request):
    isolation = request.param

    def configure(connection, _record):
        cursor = connection.cursor()
        cursor.execute(f"SET SESSION innodb_snapshot_isolation={isolation}")
        cursor.close()

    event.listen(Pool, "connect", configure)
    init_db(os.environ["DATABASE_URL"], pool_size=4, max_overflow=1)
    factory = get_session_factory()
    nonce = uuid.uuid4().hex
    # Queue tables are global by design. Use private schema copies so older
    # integration jobs cannot become this test's oldest candidate or FIFO fence.
    engine = factory.kw["bind"]
    queue_tables = ("chat_jobs", "chat_memory_outbox")
    prefix = "aux_" + nonce[:10] + "_"
    async with engine.begin() as connection:
        for table in queue_tables:
            await connection.execute(text(f"CREATE TABLE {prefix}{table} LIKE {table}"))

    def namespace(_connection, _cursor, statement, parameters, _context, _executemany):
        return re.sub(r"\b(chat_jobs|chat_memory_outbox)\b", lambda match: prefix + match.group(0), statement), parameters

    event.listen(engine.sync_engine, "before_cursor_execute", namespace, retval=True)
    owner = {"project_id": f"aux-{nonce}", "user_id": f"aux-{nonce}"}
    graph_id, run_id = str(uuid.uuid4()), str(uuid.uuid4())
    registration_ids, resource_ids, pool_ids = [], [], []
    async with factory() as session, session.begin():
        assert int(await session.scalar(text("SELECT @@SESSION.innodb_snapshot_isolation"))) == (isolation == "ON")
        session.add(ChatMessageGraph(id=graph_id, **owner))
        await session.flush()
        session.add(ChatConversation(id=graph_id, graph_id=graph_id, **owner))
        await session.flush()
        session.add(ChatRun(
            id=run_id, conversation_id=graph_id, run_scope="persistent", status="completed",
            model_name="auxiliary-test", capability_snapshot={}, pricing_snapshot={}, **owner,
            client_request_id=str(uuid.uuid4()), request_fingerprint=nonce, fingerprint_version=1,
        ))

    async def registration(classes=("online_text", "online_media"), *, managed=False):
        registration_id = str(uuid.uuid4())
        registration_ids.append(registration_id)
        resource_id = None
        pool_id = None
        async with factory() as session, session.begin():
            if managed:
                pool_id, resource_id = str(uuid.uuid4()), str(uuid.uuid4())
                pool_ids.append(pool_id)
                resource_ids.append(resource_id)
                session.add(ChatRuntimePool(
                    id=pool_id, deployment_id=nonce, name=registration_id, role="worker", backend="nova",
                    workload_class=classes[0], enabled=True, cloud_profile_id="trusted", project_id="operator",
                    region_name="RegionOne", image_ref="sha256:" + "a" * 64, profile_digest="b" * 64,
                    min_replicas=0, max_replicas=1,
                ))
                await session.flush()
                session.add(ChatRuntimeResource(
                    id=resource_id, pool_id=pool_id, generation=1, role="worker", backend="nova",
                    desired_state="ready", observed_state="ready", request_fingerprint="c" * 64,
                    cloud_profile_id="trusted", cloud_project_id="operator", image_ref="sha256:" + "a" * 64,
                    policy_digest="b" * 64, certificate_fingerprint="d" * 64,
                    certificate_not_after=datetime.now(UTC) + timedelta(hours=1),
                ))
                await session.flush()
            session.add(ChatWorkerRegistration(
                id=registration_id, worker_identity="legacy-" + registration_id, boot_id=str(uuid.uuid4()),
                resource_id=resource_id, pool_id=pool_id, resource_generation=1 if managed else None,
                certificate_fingerprint="d" * 64 if managed else None, protocol_versions=[2],
                workload_classes=list(classes), plugin_digest="e" * 64, schema_version=1, capacity=1,
            ))
        return registration_id

    async def job(kind):
        job_id = str(uuid.uuid4())
        async with factory() as session, session.begin():
            session.add(ChatJob(
                id=job_id, kind=kind, run_id=run_id, conversation_id=graph_id, status="queued",
                payload=encrypt_chat_content('{"expected_title_revision":1}'),
                progress={"state": "prepared"}, idempotency_key=f"{nonce}:{job_id}",
            ))
        return job_id

    async def mutation():
        async with factory() as session, session.begin():
            memory = ChatMemory(scope="project", content=encrypt_chat_content("remember this"), **owner)
            session.add(memory)
            await session.flush()
            row = ChatMemoryOutbox(
                event_key=nonce + str(uuid.uuid4()), memory_id=memory.id, mutation="upsert", content_hash="f" * 64,
                required_generations=[1], applied_generations=[], status="queued",
            )
            session.add(row)
            await session.flush()
            return row.change_seq

    try:
        yield SimpleNamespace(factory=factory, owner=owner, run_id=run_id,
                              registration=registration, job=job, mutation=mutation)
    finally:
        async with factory() as session, session.begin():
            await session.execute(delete(ChatBatchProjectQueue).where(ChatBatchProjectQueue.project_id == owner["project_id"]))
            await session.execute(delete(ChatBatch).where(ChatBatch.project_id == owner["project_id"]))
            memory_ids = select(ChatMemory.id).where(ChatMemory.user_id == owner["user_id"])
            await session.execute(delete(ChatMemoryOutbox).where(ChatMemoryOutbox.memory_id.in_(memory_ids)))
            await session.execute(delete(ChatMemory).where(ChatMemory.user_id == owner["user_id"]))
            await session.execute(delete(ChatJob).where(ChatJob.run_id == run_id))
            await session.execute(delete(ChatRun).where(ChatRun.id == run_id))
            await session.execute(delete(ChatConversation).where(ChatConversation.id == graph_id))
            await session.execute(delete(ChatMessageGraph).where(ChatMessageGraph.id == graph_id))
            if registration_ids:
                await session.execute(delete(ChatWorkerRegistration).where(ChatWorkerRegistration.id.in_(registration_ids)))
            if resource_ids:
                await session.execute(delete(ChatRuntimeResource).where(ChatRuntimeResource.id.in_(resource_ids)))
            if pool_ids:
                await session.execute(delete(ChatRuntimePool).where(ChatRuntimePool.id.in_(pool_ids)))
        event.remove(engine.sync_engine, "before_cursor_execute", namespace)
        async with engine.begin() as connection:
            for table in queue_tables:
                await connection.execute(text(f"DROP TABLE {prefix}{table}"))
        await close_db()
        event.remove(Pool, "connect", configure)


async def _claim_outbox(db, owner):
    async with db.factory() as session, session.begin():
        await use_read_committed(session)
        row = await memory_outbox.claim_next(session, owner=owner)
        return row.change_seq if row is not None else None


@pytest.mark.parametrize("managed", [False, True], ids=["fixed", "managed"])
async def test_online_registration_uuid_owns_all_auxiliary_leases_and_drain_blocks_claims(auxiliary_db, managed):
    db = auxiliary_db
    owner = await db.registration(("online_text",), managed=managed)
    title_id = await db.job("title_generate")
    memory_id = await db.job("memory_extract")
    outbox_id = await db.mutation()
    assert (await title_jobs._claim_one(owner=owner))["job_id"] == title_id
    assert (await memory_jobs._claim_one(owner=owner))[0] == memory_id
    assert await _claim_outbox(db, owner) == outbox_id
    async with db.factory() as session:
        assert (await session.get(ChatJob, title_id)).lease_owner == owner
        assert (await session.get(ChatJob, memory_id)).lease_owner == owner
        assert (await session.get(ChatMemoryOutbox, outbox_id)).lease_owner == owner
        counts = await auxiliary.lease_counts(session, owner)
        assert (counts.runs, counts.titles, counts.memories, counts.outbox, counts.auxiliary) == (0, 1, 1, 1, 3)
    assert await store.start_drain(owner)
    await db.job("title_generate")
    await db.job("memory_extract")
    assert await title_jobs._claim_one(owner=owner) is None
    assert await memory_jobs._claim_one(owner=owner) is None
    assert await _claim_outbox(db, owner) is None
    for model, key in ((ChatJob, title_id), (ChatJob, memory_id), (ChatMemoryOutbox, outbox_id)):
        assert await auxiliary.renew_lease(owner=owner, model=model, key=key)
    async with db.factory() as session:
        for model, key in ((ChatJob, title_id), (ChatJob, memory_id), (ChatMemoryOutbox, outbox_id)):
            row = await session.get(model, key)
            assert row.status == "running" and row.lease_owner == owner
            assert row.lease_expires_at.replace(tzinfo=UTC) > datetime.now(UTC) + timedelta(seconds=90)


@pytest.mark.parametrize("classes", [("batch",), ("online_media",)])
async def test_non_text_and_legacy_unregistered_owners_cannot_start_auxiliary_work(auxiliary_db, classes):
    db = auxiliary_db
    owner = await db.registration(classes)
    await db.job("title_generate")
    await db.job("memory_extract")
    for candidate in (owner, "legacy-" + owner, str(uuid.uuid4())):
        assert await title_jobs._claim_one(owner=candidate) is None
        assert await memory_jobs._claim_one(owner=candidate) is None
        assert await title_jobs.process_one(owner=candidate) is False
        assert await memory_jobs.process_one(owner=candidate) is False
        assert await memory_outbox.process_one(owner=candidate) is False
        async with auxiliary.step(owner=candidate) as admitted:
            assert admitted is False


async def test_heartbeat_is_not_auxiliary_evidence_and_all_coordinator_leases_are_counted(auxiliary_db):
    db = auxiliary_db
    owner = await db.registration()
    now = datetime.now(UTC)
    async with db.factory() as session, session.begin():
        registration = await session.get(ChatWorkerRegistration, owner)
        registration.active_count = 0
        run = await session.get(ChatRun, db.run_id)
        run.status, run.worker_registration_id = "waiting_children", owner
        run.lease_owner, run.lease_expires_at = owner + "#1", now + timedelta(minutes=2)
        session.add(ChatBatch(
            id=str(uuid.uuid4()), contract="native", status="validating", **db.owner,
            idempotency_key_hash=uuid.uuid4().hex * 2, request_fingerprint="a" * 64,
            lease_owner=owner, lease_expires_at=now + timedelta(minutes=2),
        ))
        session.add(ChatBatchProjectQueue(
            project_id=db.owner["project_id"], lease_owner=owner, lease_expires_at=now + timedelta(minutes=2),
        ))
    busy, counts = await auxiliary.worker_status(owner)
    assert busy == 0
    assert (counts.runs, counts.batches, counts.project_queues, counts.total) == (1, 1, 1, 3)


async def test_memory_provider_finishes_and_renews_during_drain_and_loop_cancellation(auxiliary_db, monkeypatch):
    db = auxiliary_db
    owner = await db.registration()
    job_id = await db.job("memory_extract")
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def extract(**_kwargs):
        calls.append("started")
        started.set()
        await release.wait()
        calls.append("finished")
        return None

    monkeypatch.setattr(memory_jobs, "generate_memory_if_applicable", extract)
    task = asyncio.create_task(memory_jobs.process_one(owner=owner))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        busy, counts = await auxiliary.worker_status(owner)
        assert busy == 1 and counts.memories == 1 and counts.runs == 0
        assert await store.start_drain(owner)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and calls == ["started"]
        assert await auxiliary.renew_lease(owner=owner, model=ChatJob, key=job_id)
        assert await memory_jobs._claim_one(owner=owner) is None
    finally:
        release.set()
        results = await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=5)
    assert isinstance(results[0], asyncio.CancelledError)
    assert calls == ["started", "finished"]
    async with db.factory() as session:
        row = await session.get(ChatJob, job_id)
        assert row.status == "completed" and row.lease_owner is None and row.lease_expires_at is None
    busy, counts = await auxiliary.worker_status(owner)
    assert busy == 0 and counts.total == 0


async def test_resource_drain_fence_and_stale_registration_block_claim_before_job_lock(auxiliary_db):
    db = auxiliary_db
    owner = await db.registration(("online_text",), managed=True)
    await db.job("memory_extract")
    async with db.factory() as session, session.begin():
        registration = await session.get(ChatWorkerRegistration, owner)
        resource = await session.get(ChatRuntimeResource, registration.resource_id)
        resource.drain_requested_at = datetime.now(UTC)
    assert await memory_jobs._claim_one(owner=owner) is None
    async with db.factory() as session, session.begin():
        registration = await session.get(ChatWorkerRegistration, owner)
        resource = await session.get(ChatRuntimeResource, registration.resource_id)
        resource.drain_requested_at = None
        registration.heartbeat_at = datetime.now(UTC) - timedelta(seconds=21)
    assert await memory_jobs._claim_one(owner=owner) is None


@pytest.mark.parametrize("managed", [False, True], ids=["fixed", "managed"])
async def test_claim_waiting_on_drain_lock_observes_committed_fence(auxiliary_db, managed):
    from lumen.services.worker_routing import lock_registration

    db = auxiliary_db
    owner = await db.registration(("online_text",), managed=managed)
    job_id = await db.job("memory_extract")
    async with db.factory() as session, session.begin():
        await use_read_committed(session)
        registration, resource = await lock_registration(session, owner)
        registration.accepting, registration.draining = False, True
        if resource is not None:
            resource.accepting = False
            resource.drain_requested_at = datetime.now(UTC)
        claimant = asyncio.create_task(memory_jobs._claim_one(owner=owner))
        await asyncio.sleep(0.05)
        assert not claimant.done()
    assert await asyncio.wait_for(claimant, timeout=5) is None
    async with db.factory() as session:
        job = await session.get(ChatJob, job_id)
        assert job.status == "queued" and job.lease_owner is None


async def test_stale_title_provider_intent_is_terminalized_without_reinference(auxiliary_db, monkeypatch):
    db = auxiliary_db
    owner = await db.registration()
    job_id = await db.job("title_generate")
    async with db.factory() as session, session.begin():
        row = await session.get(ChatJob, job_id)
        row.status = "running"
        row.lease_owner = str(uuid.uuid4())
        row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        row.progress = {"state": "provider_started"}

    async def forbidden(**_kwargs):
        raise AssertionError("an ambiguous title provider intent cannot be re-executed")

    monkeypatch.setattr(title_jobs.title_summary, "generate_title", forbidden)
    assert await title_jobs.process_one(owner=owner)
    async with db.factory() as session:
        row = await session.get(ChatJob, job_id)
        assert (row.status, row.error_code) == ("failed", "provider_result_unknown")
        assert row.lease_owner is None and row.lease_expires_at is None
    busy, counts = await auxiliary.worker_status(owner)
    assert busy == 0 and counts.total == 0


async def test_nonleased_storage_step_is_busy_until_it_finishes_after_drain(auxiliary_db):
    db = auxiliary_db
    owner = await db.registration()
    async with auxiliary.step(owner=owner) as admitted:
        assert admitted
        busy, counts = await auxiliary.worker_status(owner)
        assert busy == 1 and counts.total == 0
        assert await store.start_drain(owner)
        async with auxiliary.step(owner=owner) as another:
            assert another is False
        busy, counts = await auxiliary.worker_status(owner)
        assert busy == 1 and counts.total == 0
    busy, counts = await auxiliary.worker_status(owner)
    assert busy == 0 and counts.total == 0


async def test_renewal_is_exact_and_does_not_resurrect_expired_or_rival_leases(auxiliary_db):
    db = auxiliary_db
    owner = await db.registration()
    rival = await db.registration()
    owned_id = await db.job("memory_extract")
    rival_id = await db.job("memory_extract")
    expired_id = await db.job("memory_extract")
    original = datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=30)
    async with db.factory() as session, session.begin():
        for key, lease_owner, expires in (
            (owned_id, owner, original),
            (rival_id, rival, original),
            (expired_id, owner, datetime.now(UTC) - timedelta(seconds=1)),
        ):
            row = await session.get(ChatJob, key)
            row.status, row.lease_owner, row.lease_expires_at = "running", lease_owner, expires
    assert await auxiliary.renew_lease(owner=owner, model=ChatJob, key=owned_id)
    assert not await auxiliary.renew_lease(owner=owner, model=ChatJob, key=rival_id)
    assert not await auxiliary.renew_lease(owner=owner, model=ChatJob, key=expired_id)
    async with db.factory() as session:
        owned = await session.get(ChatJob, owned_id)
        other = await session.get(ChatJob, rival_id)
        expired = await session.get(ChatJob, expired_id)
        assert owned.lease_expires_at.replace(tzinfo=UTC) > datetime.now(UTC) + timedelta(seconds=90)
        assert other.lease_expires_at.replace(tzinfo=UTC) == original
        assert other.lease_owner == rival
        assert expired.lease_expires_at.replace(tzinfo=UTC) < datetime.now(UTC)


async def test_job_renewer_only_keeps_the_active_claim_alive(auxiliary_db, monkeypatch):
    db = auxiliary_db
    owner = await db.registration()
    active_id = await db.job("memory_extract")
    orphan_id = await db.job("memory_extract")
    original = datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=30)
    async with db.factory() as session, session.begin():
        orphan = await session.get(ChatJob, orphan_id)
        orphan.status, orphan.lease_owner, orphan.lease_expires_at = "running", owner, original
    started, release, renewed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    keys = []
    original_renew = auxiliary.renew_lease

    async def observed_renew(**kwargs):
        value = await original_renew(**kwargs)
        keys.append(kwargs["key"])
        renewed.set()
        return value

    async def extract(**_kwargs):
        started.set()
        await release.wait()
        return None

    monkeypatch.setattr(auxiliary, "RENEW_SECONDS", 0.01)
    monkeypatch.setattr(auxiliary, "renew_lease", observed_renew)
    monkeypatch.setattr(memory_jobs, "generate_memory_if_applicable", extract)
    task = asyncio.create_task(memory_jobs.process_one(owner=owner))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        await asyncio.wait_for(renewed.wait(), timeout=5)
        assert keys and set(keys) == {active_id}
        async with db.factory() as session:
            orphan = await session.get(ChatJob, orphan_id)
            assert orphan.lease_expires_at.replace(tzinfo=UTC) == original
    finally:
        release.set()
        await asyncio.wait_for(task, timeout=5)
