"""Actual MariaDB legacy backfills and batch pinning in an isolated table namespace."""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from lumen.crypto import decrypt_chat_content, encrypt_chat_content
from lumen.models.chat_batches import ChatBatch, ChatBatchFile, ChatBatchItem, ChatBatchProjectQueue
from lumen.models.chat_infrastructure import ChatRuntimePool, ChatRuntimeResource, ChatWorkerRegistration
from lumen.models.chat_runs import ChatRun
from lumen.scripts.migrate import MIGRATIONS, _statements, load_manifest

pytestmark = pytest.mark.integration


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@pytest.mark.parametrize("snapshot_isolation", ["ON", "OFF"])
async def test_runtime_backfill_and_batch_ledger_survive_repeat_migration(snapshot_isolation):
    engine = create_async_engine(os.environ["DATABASE_URL"], pool_size=1, max_overflow=0)
    prefix = f"rt_{uuid.uuid4().hex[:10]}_"

    def namespace(statement: str) -> str:
        statement = re.sub(r"\b(chat_|llm_|user_wallets\b)", lambda match: prefix + match.group(0), statement)
        # InnoDB foreign-key symbols are database-global, unlike index names.
        return re.sub(r"\b(fk_|ck_|chk_|uq_)", lambda match: prefix + match.group(0), statement)

    def namespace_sql(connection, cursor, statement, parameters, context, executemany):
        # Namespace SQL literals in metadata-guarded CHECK DDL as well as identifiers.
        return namespace(statement), parameters

    event.listen(engine.sync_engine, "before_cursor_execute", namespace_sql, retval=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    migrations = load_manifest()
    additions = [migration for migration in migrations if migration.logical_id in {
        "022-runtime-routing", "023-trusted-identity", "024-batch-ledger",
        "025-batch-expiry-default",
    }]
    tables: list[str] = []
    run_ids = {kind: str(uuid.uuid4()) for kind in (
        "completion", "compaction", "image", "tts", "stt", "realtime",
    )}
    pool_id, api_pool_id, resource_id, fixed_id, managed_id = [str(uuid.uuid4()) for _ in range(5)]
    asset_id = str(uuid.uuid4())

    async def rejected(sql: str, parameters: dict | None = None):
        with pytest.raises(DBAPIError):
            async with engine.begin() as connection:
                await connection.execute(text(sql), parameters or {})

    try:
        async with engine.begin() as connection:
            await connection.exec_driver_sql(f"SET SESSION innodb_snapshot_isolation = {snapshot_isolation}")
            actual = (await connection.exec_driver_sql("SELECT @@SESSION.innodb_snapshot_isolation")).scalar_one()
            assert bool(actual) == (snapshot_isolation == "ON")
            # Immutable pre-routing SQL, never Base.metadata.create_all with today's defaults.
            for migration in migrations:
                if migration in additions:
                    break
                for statement in _statements(MIGRATIONS / migration.relative_path):
                    match = re.match(r"CREATE TABLE (?:IF NOT EXISTS )?(\w+)", statement)
                    if match:
                        table = namespace(match.group(1))
                        assert table.startswith(prefix)
                        tables.append(table)
                    await connection.exec_driver_sql(statement)
            for kind, run_id in run_ids.items():
                await connection.exec_driver_sql("""
                    INSERT INTO chat_runs
                        (id, run_scope, run_kind, project_id, user_id, model_name,
                         capability_snapshot, pricing_snapshot, client_request_id,
                         request_fingerprint, fingerprint_version, execution_protocol_version,
                         execution_mode, depth, status, last_seq, current_ordinal,
                         reserved_credits, descendant_credits_reserved, sandbox_seconds_reserved,
                         source, created_at, updated_at)
                    VALUES (%s, 'temporary', %s, 'project', 'user', 'legacy-model',
                            '{}', '{}', %s, %s, 1, 1, 'chat', 0, 'queued', 0, 0,
                            0, 0, 0, 'api', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """, (run_id, kind, str(uuid.uuid4()), _hash(kind)))
            for identifier, role in ((pool_id, "worker"), (api_pool_id, "api")):
                await connection.exec_driver_sql("""
                    INSERT INTO chat_runtime_pools
                        (id,deployment_id,name,role,backend,cloud_profile_id,project_id,
                         region_name,image_ref,profile_digest)
                    VALUES (%s,'deployment',%s,%s,'nova','cloud','project','region','image',%s)
                """, (identifier, role, role, "a" * 64))
            await connection.exec_driver_sql("""
                INSERT INTO chat_runtime_resources
                    (id,pool_id,role,backend,request_fingerprint,cloud_profile_id,
                     cloud_project_id,image_ref,policy_digest)
                VALUES (%s,%s,'worker','nova',%s,'cloud','cloud-project','image',%s)
            """, (resource_id, pool_id, "b" * 64, "c" * 64))
            for identifier, worker_pool in ((fixed_id, None), (managed_id, pool_id)):
                await connection.exec_driver_sql("""
                    INSERT INTO chat_worker_registrations
                        (id,worker_identity,boot_id,pool_id,protocol_versions,
                         plugin_digest,schema_version,capacity)
                    VALUES (%s,%s,%s,%s,'[1]',%s,21,4)
                """, (identifier, identifier, str(uuid.uuid4()), worker_pool, "d" * 64))
            await connection.exec_driver_sql("""
                INSERT INTO chat_assets
                    (id,project_id,user_id,object_key,original_name,mime_type,size_bytes,
                     sha256,status,created_at,updated_at)
                VALUES (%s,'project','user','input-image','image.png','image/png',1,%s,
                        'ready',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)
            """, (asset_id, "e" * 64))
            for migration in additions:
                for statement in _statements(MIGRATIONS / migration.relative_path):
                    match = re.match(r"CREATE TABLE (?:IF NOT EXISTS )?(\w+)", statement)
                    if match:
                        table = namespace(match.group(1))
                        assert table.startswith(prefix)
                        tables.append(table)
                    await connection.exec_driver_sql(statement)

        async with factory() as session:
            runs = (await session.scalars(select(ChatRun))).all()
            assert {run.run_kind: run.workload_class for run in runs} == {
                "completion": "online_text", "compaction": "online_text",
                "image": "online_media", "tts": "online_media", "stt": "online_media",
                "realtime": "realtime",
            }
            assert all(run.worker_pool_id is None and run.worker_registration_id is None
                       and run.batch_id is None for run in runs)
            assert (await session.get(ChatRuntimePool, pool_id)).workload_class == "online_text"
            assert (await session.get(ChatRuntimePool, api_pool_id)).workload_class is None
            assert (await session.get(ChatWorkerRegistration, fixed_id)).workload_classes == [
                "online_text", "online_media",
            ]
            managed = await session.get(ChatWorkerRegistration, managed_id)
            assert managed.workload_classes == ["online_text"]
            assert managed.auxiliary_active == 0 and managed.drain_ack_at is None
            resource = await session.get(ChatRuntimeResource, resource_id)
            assert resource.accepting is True
            assert resource.idle_since is None and resource.drain_ack_at is None
            assert resource.certificate_not_after is None
            assert resource.pending_certificate_fingerprint is None
            assert resource.previous_certificate_fingerprint is None

        # Retry must preserve newly assigned classes, including a deliberately reclassified run.
        async with engine.begin() as connection:
            await connection.execute(text("""
                UPDATE chat_runs SET workload_class='batch',worker_pool_id=:pool,
                                     worker_registration_id=:worker WHERE id=:id
            """), {"id": run_ids["completion"], "pool": pool_id, "worker": managed_id})
            await connection.execute(text("UPDATE chat_runtime_pools SET workload_class='online_media' WHERE id=:id"),
                                     {"id": pool_id})
            await connection.execute(text("UPDATE chat_worker_registrations SET workload_classes='[\"online_media\"]' WHERE id=:id"),
                                     {"id": managed_id})
            for migration in additions:
                for statement in _statements(MIGRATIONS / migration.relative_path):
                    await connection.exec_driver_sql(statement)
        async with factory() as session:
            rerouted = await session.get(ChatRun, run_ids["completion"])
            assert rerouted.workload_class == "batch" and rerouted.worker_pool_id == pool_id
            assert rerouted.worker_registration_id == managed_id
            assert (await session.get(ChatRun, run_ids["realtime"])).workload_class == "realtime"
            assert (await session.get(ChatRuntimePool, pool_id)).workload_class == "online_media"
            assert (await session.get(ChatWorkerRegistration, managed_id)).workload_classes == ["online_media"]

        default_run_id, default_worker_id = str(uuid.uuid4()), str(uuid.uuid4())
        async with engine.begin() as connection:
            await connection.execute(text("""
                INSERT INTO chat_runs
                    (id,run_scope,project_id,user_id,model_name,capability_snapshot,
                     pricing_snapshot,client_request_id,request_fingerprint,fingerprint_version,
                     execution_protocol_version,execution_mode,depth,status,last_seq,current_ordinal,
                     reserved_credits,descendant_credits_reserved,sandbox_seconds_reserved,
                     source,created_at,updated_at)
                SELECT :new_id,run_scope,project_id,user_id,model_name,capability_snapshot,
                       pricing_snapshot,:request,request_fingerprint,fingerprint_version,
                       execution_protocol_version,execution_mode,depth,status,last_seq,current_ordinal,
                       reserved_credits,descendant_credits_reserved,sandbox_seconds_reserved,
                       source,created_at,updated_at FROM chat_runs WHERE id=:old_id
            """), {"new_id": default_run_id, "request": str(uuid.uuid4()), "old_id": run_ids["compaction"]})
            await connection.execute(text("""
                INSERT INTO chat_worker_registrations
                    (id,worker_identity,boot_id,protocol_versions,plugin_digest,schema_version,capacity)
                VALUES (:id,:id,:boot,'[1]',:digest,24,4)
            """), {"id": default_worker_id, "boot": str(uuid.uuid4()), "digest": "f" * 64})
        async with factory() as session:
            assert (await session.get(ChatRun, default_run_id)).workload_class == "online_text"
            worker = await session.get(ChatWorkerRegistration, default_worker_id)
            assert worker.workload_classes == ["online_text", "online_media"]
            assert worker.auxiliary_active == 0

        file_id, batch_id = str(uuid.uuid4()), str(uuid.uuid4())
        async with engine.begin() as connection:
            # Omit the new columns to exercise SQL defaults independently of ORM insert defaults.
            await connection.execute(text("""
                INSERT INTO chat_batch_files
                    (id,project_id,user_id,purpose,filename,mime_type,bucket_name,object_key,expires_at)
                VALUES (:id,'project','user','batch','input.jsonl','application/jsonl','private','input.jsonl',
                        CURRENT_TIMESTAMP(6) + INTERVAL 30 DAY)
            """), {"id": file_id})
            await connection.execute(text("""
                INSERT INTO chat_batches
                    (id,project_id,user_id,contract,idempotency_key_hash,request_fingerprint,input_file_id)
                VALUES (:id,'project','user','openai',:hash,:hash,:file)
            """), {"id": batch_id, "hash": _hash("request"), "file": file_id})
        async with factory() as session:
            file = await session.get(ChatBatchFile, file_id)
            assert file.state == "uploading" and file.size_bytes == 0 and file.upload_epoch == 0
            batch = await session.get(ChatBatch, batch_id)
            assert batch.status == "validating" and batch.request_total == 0
            assert batch.request_completed == batch.request_failed == 0
            assert batch.validation_byte_cursor == batch.validation_ordinal_cursor == 0
            assert batch.lease_fence == batch.finalization_epoch == 0
            assert batch.expires_at - batch.created_at == timedelta(hours=24)

        body = '{"prompt":"private batch body"}'
        encrypted = encrypt_chat_content(body)
        async with factory() as session, session.begin():
            batch = await session.get(ChatBatch, batch_id)
            batch.request_total = 4
            for ordinal, custom_id, run_id in ((1, "A", run_ids["image"]), (2, "a", None)):
                session.add(ChatBatchItem(
                    batch_id=batch_id, ordinal=ordinal, custom_id=custom_id,
                    custom_id_hash=_hash(custom_id), operation="images.generations",
                    request_ciphertext=encrypted,
                    input_asset_id=asset_id, run_id=run_id,
                ))
            session.add(ChatBatchProjectQueue(project_id="project"))
        async with factory() as session:
            items = (await session.scalars(select(ChatBatchItem).order_by(ChatBatchItem.ordinal))).all()
            assert [item.custom_id for item in items] == ["A", "a"]
            assert all(item.state == "pending" and item.settlement_status == "pending" for item in items)
            assert decrypt_chat_content(items[0].request_ciphertext) == body
            assert items[0].request_ciphertext != body
            assert (await session.get(ChatBatchProjectQueue, "project")).lease_fence == 0
        async with engine.connect() as connection:
            stored = (await connection.execute(text("SELECT request_ciphertext FROM chat_batch_items WHERE ordinal=1"))).scalar_one()
            assert stored == encrypted and "private batch body" not in stored

        await rejected("""
            INSERT INTO chat_batches
                (id,project_id,user_id,contract,idempotency_key_hash,request_fingerprint)
            VALUES (:id,'project','user','openai',:hash,:other)
        """, {"id": str(uuid.uuid4()), "hash": _hash("request"), "other": _hash("different")})
        await rejected("""
            INSERT INTO chat_batches (id,project_id,user_id,contract,request_fingerprint)
            VALUES (:id,'project','user','native',:fingerprint)
        """, {"id": str(uuid.uuid4()), "fingerprint": _hash("without-key")})
        # Optional OpenAI idempotency keys must not turn repeated no-header creates into duplicates.
        async with factory() as session, session.begin():
            for _ in range(2):
                session.add(ChatBatch(
                    id=str(uuid.uuid4()), project_id="project", user_id="user", contract="openai",
                    request_fingerprint=_hash("without-key"),
                    expires_at=datetime.now(UTC) + timedelta(hours=24),
                ))
        # Owner and contract participate in uniqueness, not the request fingerprint.
        async with factory() as session, session.begin():
            for project, contract in (("other-project", "openai"), ("project", "native")):
                session.add(ChatBatch(
                    id=str(uuid.uuid4()), project_id=project, user_id="user", contract=contract,
                    idempotency_key_hash=_hash("request"), request_fingerprint=_hash("request"),
                    expires_at=datetime.now(UTC) + timedelta(hours=24),
                ))
        item_insert = """
            INSERT INTO chat_batch_items
                (batch_id,ordinal,custom_id,custom_id_hash,operation,request_ciphertext,run_id)
            VALUES (:batch,3,:custom,:hash,'images.generations',:payload,:run)
        """
        common = {"batch": batch_id, "payload": encrypted}
        await rejected(item_insert, {**common, "custom": "A", "hash": _hash("A"), "run": None})
        await rejected(item_insert, {**common, "custom": "third", "hash": _hash("third"), "run": run_ids["image"]})
        await rejected(item_insert, {**common, "custom": "third", "hash": _hash("third"), "run": str(uuid.uuid4())})
        await rejected("DELETE FROM chat_batch_files WHERE id=:id", {"id": file_id})
        await rejected("DELETE FROM chat_assets WHERE id=:id", {"id": asset_id})
        await rejected("DELETE FROM chat_runs WHERE id=:id", {"id": run_ids["image"]})
        await rejected("UPDATE chat_batches SET input_file_id=:missing WHERE id=:id",
                       {"id": batch_id, "missing": str(uuid.uuid4())})
        await rejected("UPDATE chat_batch_items SET input_asset_id=:missing WHERE ordinal=1",
                       {"missing": str(uuid.uuid4())})
        for sql in (
            "UPDATE chat_runs SET workload_class='invalid'",
            "UPDATE chat_runtime_pools SET workload_class='realtime'",
            "UPDATE chat_worker_registrations SET workload_classes='[\"realtime\"]'",
            "UPDATE chat_worker_registrations SET auxiliary_active=-1",
            "UPDATE chat_batches SET status='queued'",
            "UPDATE chat_batches SET validation_byte_cursor=-1",
            "UPDATE chat_batches SET validation_ordinal_cursor=-1",
            "UPDATE chat_batches SET request_completed=-1",
            "UPDATE chat_batches SET request_failed=-1",
            "UPDATE chat_batches SET finalization_epoch=-1",
            "UPDATE chat_batch_items SET ordinal=0",
            "UPDATE chat_batch_items SET state='admitted'",
            "UPDATE chat_batch_files SET state='ready'",
            "UPDATE chat_batch_files SET size_bytes=-1",
            "UPDATE chat_batch_files SET upload_epoch=-1",
            "UPDATE chat_batch_project_queues SET lease_fence=-1",
        ):
            await rejected(sql)
        async with engine.begin() as connection:
            for state in ("pending", "queued", "running", "completed", "failed", "cancelled", "expired", "unknown"):
                await connection.execute(text("UPDATE chat_batch_items SET state=:state"), {"state": state})
            expected_indexes = {
                "chat_runs": {
                    "worker_pool_id,workload_class,status,created_at,id",
                    "worker_registration_id,status,lease_expires_at",
                    "batch_id,status",
                },
                "chat_jobs": {"lease_owner,status,lease_expires_at"},
                "chat_memory_outbox": {"lease_owner,status,lease_expires_at"},
                "chat_batch_items": {"batch_id,state,ordinal"},
            }
            for table, expected in expected_indexes.items():
                indexes = (await connection.execute(text("""
                    SELECT GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX SEPARATOR ',')
                    FROM information_schema.STATISTICS
                    WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=:table
                    GROUP BY INDEX_NAME
                """), {"table": prefix + table})).scalars().all()
                assert expected <= set(indexes)
    finally:
        # Only recorded prefixed tables are eligible for cleanup, including
        # user_wallets; unprefixed shared tables are never cleanup candidates.
        async with engine.begin() as connection:
            await connection.exec_driver_sql("SET SESSION FOREIGN_KEY_CHECKS=0")
            try:
                for table in reversed(list(dict.fromkeys(tables))):
                    assert table.startswith(prefix)
                    await connection.exec_driver_sql(f"DROP TABLE IF EXISTS {table}")
            finally:
                await connection.exec_driver_sql("SET SESSION FOREIGN_KEY_CHECKS=1")
        await engine.dispose()
