"""Input expiry isolates corrupt approvals while preserving committed denial events."""

import base64
import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from cryptography.exceptions import InvalidTag
from sqlalchemy import delete, event, select
from sqlalchemy.exc import OperationalError

from lumen import crypto
from lumen.db import close_db, get_session_factory, init_db
from lumen.models.chat_agent_platform import ChatRunInteraction
from lumen.models.chat_runs import ChatRun, ChatRunEventRow, ChatToolApproval
from lumen.services.durable_runs import interactions
from lumen.services.durable_runs.errors import DurableRunError
from lumen.services.run_store import replay_events

pytestmark = pytest.mark.integration


@pytest.fixture(params=["ON", "OFF"])
async def expiry_store(request, monkeypatch):
    init_db(os.environ["DATABASE_URL"], pool_size=3, max_overflow=1)
    factory = get_session_factory()
    engine = factory.kw["bind"]

    def snapshot_mode(connection, _record):
        cursor = connection.cursor()
        cursor.execute(f"SET SESSION innodb_snapshot_isolation={request.param}")
        cursor.close()

    event.listen(engine.sync_engine, "connect", snapshot_mode)
    nonce = uuid.uuid4().hex
    state = SimpleNamespace(factory=factory, now=datetime.now(UTC), run_ids=[], wakes=[], calls={})
    monkeypatch.setattr(crypto, "_get_key", lambda: bytes.fromhex("ab" * 32))
    monkeypatch.setattr(interactions, "get_settings", lambda: SimpleNamespace(chat_execution_protocol_version=2))
    monkeypatch.setattr(interactions, "_now", lambda: state.now)

    async def wake(run_id):
        # Exercise the worker-facing hint only after another connection can see the commit.
        async with factory() as session:
            run = await session.get(ChatRun, run_id)
            assert run.status == "queued"
            assert run.lease_owner is None and run.lease_expires_at is None
            events = await replay_events(session, run, after_seq=0)
            assert events[-1].type == "run.stage.changed"
            assert events[-1].payload.stage == "queued"
        state.wakes.append(run_id)

    monkeypatch.setattr(interactions, "wake_run", wake)

    async def create_run(order, *, approval_calls=1, with_interaction=False):
        run_id = f"{order:08x}-{nonce[:4]}-{nonce[4:8]}-{nonce[8:12]}-{nonce[12:24]}"
        state.run_ids.append(run_id)
        calls = [{
            "call_id": f"call-{index}", "name": "browser_navigate",
            "arguments": json.dumps({"url": "https://example.com/expiry"}),
            "source": "mcp", "effect": "external_mutation", "tool_definition_hash": "a" * 64,
            "config_fingerprint": "b" * 64, "expected_state_revision": 7, "writer_fence": 11,
        } for index in range(approval_calls)]
        state.calls[run_id] = calls
        async with factory() as session, session.begin():
            session.add(ChatRun(
                id=run_id, run_scope="temp", run_kind="completion", execution_mode="chat",
                project_id=f"expiry-project-{nonce}", user_id=f"expiry-user-{nonce}",
                model_name="expiry-fixture", capability_snapshot={}, pricing_snapshot={},
                client_request_id=str(uuid.uuid4()), request_fingerprint=uuid.uuid4().hex,
                fingerprint_version=1, execution_protocol_version=2,
                status="running" if calls else "awaiting_input", lease_owner="worker#1" if calls else None,
                lease_fence=1, lease_expires_at=state.now + timedelta(minutes=5) if calls else None,
            ))
        if calls:
            await interactions._persist_v2_approval_interrupt(run_id=run_id, owner="worker#1", calls=calls)
        if with_interaction:
            async with factory() as session, session.begin():
                session.add(ChatRunInteraction(
                    run_id=run_id, id=str(uuid.uuid4()), status="pending",
                    request_ciphertext=crypto.encrypt_chat_content('{"question":"continue?"}'),
                    response_schema={"option_ids": [], "allow_multiple": False, "allow_text": True},
                    expires_at=state.now + timedelta(minutes=15),
                ))
        return run_id

    state.create_run = create_run
    try:
        yield state
    finally:
        async with factory() as session, session.begin():
            await session.execute(delete(ChatRunEventRow).where(ChatRunEventRow.run_id.in_(state.run_ids)))
            await session.execute(delete(ChatRunInteraction).where(ChatRunInteraction.run_id.in_(state.run_ids)))
            await session.execute(delete(ChatToolApproval).where(ChatToolApproval.run_id.in_(state.run_ids)))
            await session.execute(delete(ChatRun).where(ChatRun.id.in_(state.run_ids)))
        await close_db()
        event.remove(engine.sync_engine, "connect", snapshot_mode)


@pytest.mark.parametrize("corruption", [
    "forged-dispatch", "legacy-missing-hmac", "unreadable-arguments", "tampered-authentication-tag",
])
@pytest.mark.parametrize("corrupt_first", [True, False], ids=["corrupt-before-valid", "valid-before-corrupt"])
async def test_expiry_commits_and_wakes_independent_runs_without_authorizing_corrupt_input(
    expiry_store, caplog, corruption, corrupt_first,
):
    state = expiry_store
    corrupt_id = await state.create_run(0 if corrupt_first else 2, approval_calls=2, with_interaction=True)
    valid_id = await state.create_run(1, with_interaction=True)
    interaction_only_id = await state.create_run(3, approval_calls=0, with_interaction=True)
    async with state.factory() as session, session.begin():
        corrupt_run = await session.get(ChatRun, corrupt_id)
        original_seq = corrupt_run.last_seq
        # call-0 is processed before the corrupt call-1: its denial/event must also roll back.
        corrupt = await session.get(ChatToolApproval, (corrupt_id, "call-1"))
        if corruption == "forged-dispatch":
            corrupt.tool_name = "browser_delete_everything"
        elif corruption == "legacy-missing-hmac":
            corrupt.dispatch_hmac = None
        elif corruption == "tampered-authentication-tag":
            ciphertext = corrupt.arguments_ciphertext
            assert ciphertext.startswith(crypto._V3_PREFIX)
            raw = bytearray(base64.b64decode(ciphertext[len(crypto._V3_PREFIX):], validate=True))
            # AES-GCM appends its authentication tag: flip a decoded tag byte, not base64 padding.
            raw[-1] ^= 1
            corrupt.arguments_ciphertext = crypto._V3_PREFIX + base64.b64encode(raw).decode("ascii")
            with pytest.raises(InvalidTag):
                crypto.decrypt_chat_content(corrupt.arguments_ciphertext)
        else:
            corrupt.arguments_ciphertext = "not-valid-ciphertext"
    state.now += timedelta(minutes=16)

    assert await interactions.expire_pending_inputs() == [valid_id, interaction_only_id]
    assert state.wakes == [valid_id, interaction_only_id]
    assert "durable input expiry rejected invalid run" in caplog.text
    async with state.factory() as session:
        corrupt_run = await session.get(ChatRun, corrupt_id)
        assert corrupt_run.status == "awaiting_input" and corrupt_run.last_seq == original_seq
        corrupt_rows = list((await session.scalars(select(ChatToolApproval).where(
            ChatToolApproval.run_id == corrupt_id,
        ))).all())
        assert len(corrupt_rows) == 2
        assert all(row.status == "pending" and row.decision_hmac is None and row.decided_at is None
                   for row in corrupt_rows)
        corrupt_interaction = await session.scalar(select(ChatRunInteraction).where(
            ChatRunInteraction.run_id == corrupt_id,
        ))
        assert corrupt_interaction.status == "pending" and corrupt_interaction.decided_at is None
        corrupt_events = await replay_events(session, corrupt_run, after_seq=0)
        assert not any(event.type in {"tool.approval_resolved", "interaction.resolved"} for event in corrupt_events)

        valid_run = await session.get(ChatRun, valid_id)
        approval = await session.get(ChatToolApproval, (valid_id, "call-0"))
        assert valid_run.status == "queued"
        assert approval.status == "denied" and approval.decided_by_user_id is None
        assert approval.decision_hmac and approval.expected_state_revision == 7 and approval.writer_fence == 11
        dispatch_hmac = interactions._validated_approval_dispatch_hmac(run=valid_run, approval=approval)
        interactions._validated_approval_decision_hmac(
            run=valid_run, approval=approval, dispatch_hmac=dispatch_hmac, decision="deny",
        )
        valid_events = await replay_events(session, valid_run, after_seq=0)
        resolved = [event for event in valid_events if event.type == "tool.approval_resolved"]
        assert len(resolved) == 1
        assert resolved[0].payload.decision == "deny" and resolved[0].payload.decided_by_user_id is None
        assert len([event for event in valid_events if event.type == "interaction.resolved"]) == 1
        for run_id in (valid_id, interaction_only_id):
            interaction = await session.scalar(select(ChatRunInteraction).where(ChatRunInteraction.run_id == run_id))
            assert interaction.status == "timeout" and interaction.response_ciphertext is None
            assert interaction.decided_by_user_id is None and interaction.decided_at is not None

    # Consumer recovery accepts the authenticated timeout only as a denial, never an approval.
    async with state.factory() as session, session.begin():
        run = await session.get(ChatRun, valid_id)
        run.status, run.lease_owner, run.lease_fence = "running", "worker#2", 2
        run.lease_expires_at = state.now + timedelta(minutes=1)
    assert await interactions._v2_approval_resume(
        run_id=valid_id, owner="worker#2", calls=state.calls[valid_id],
    ) == [{"call_id": "call-0", "decision": "deny"}]
    for decision in ("approve", "deny"):
        with pytest.raises(DurableRunError):
            await interactions.resolve_tool_approval(
                run_id=corrupt_id, call_id="call-1", decision=decision,
                project_id=corrupt_run.project_id, user_id=corrupt_run.user_id,
            )
    assert await interactions.expire_pending_inputs() == []
    assert state.wakes == [valid_id, interaction_only_id]


async def test_expiry_database_failure_propagates_without_rolling_back_previously_woken_run(expiry_store, monkeypatch):
    state = expiry_store
    committed_id = await state.create_run(0)
    failing_id = await state.create_run(1)
    state.now += timedelta(minutes=16)
    real_append = interactions.append_event

    async def fail_append(session, run, run_event):
        await real_append(session, run, run_event)
        if run.id == failing_id:
            raise OperationalError("expiry journal write", {}, RuntimeError("database unavailable"))

    monkeypatch.setattr(interactions, "append_event", fail_append)
    with pytest.raises(OperationalError):
        await interactions.expire_pending_inputs()
    assert state.wakes == [committed_id]
    async with state.factory() as session:
        committed = await session.get(ChatRun, committed_id)
        failing = await session.get(ChatRun, failing_id)
        assert committed.status == "queued" and failing.status == "awaiting_input"
        approval = await session.get(ChatToolApproval, (failing_id, "call-0"))
        assert approval.status == "pending" and approval.decision_hmac is None
        events = await replay_events(session, failing, after_seq=0)
        assert not any(row.type == "tool.approval_resolved" for row in events)
    monkeypatch.setattr(interactions, "append_event", real_append)
    assert await interactions.expire_pending_inputs() == [failing_id]
    assert state.wakes == [committed_id, failing_id]
