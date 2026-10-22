"""Checkpoint/approval-ledger recovery with MariaDB and an in-memory LangGraph saver."""

import base64
import copy
import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langgraph.checkpoint.memory import MemorySaver
from lumen_plugin_api.tools import ToolBinding, ToolDefinition, ToolExecutionResult
from sqlalchemy import delete, select

from lumen import crypto
from lumen.crypto import encrypt_chat_content
from lumen.db import close_db, get_session_factory, init_db
from lumen.models.chat_runs import ChatRun, ChatRunEventRow, ChatToolApproval
from lumen.services import graph, worker_routing
from lumen.services.durable_runs import execution, interactions
from lumen.services.durable_runs.errors import DurableRunError, DurableRunLeaseLost
from lumen.services.infrastructure import store
from lumen.services.run_store import claim_queued_run, replay_events

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("current_project_authority")]


@pytest.fixture
async def approval_recovery(monkeypatch):
    init_db(os.environ["DATABASE_URL"], pool_size=3, max_overflow=1)
    factory = get_session_factory()
    assert factory is not None
    nonce = uuid.uuid4().hex
    run_id = str(uuid.uuid4())
    identity = f"approval-recovery-{nonce}"
    registration_id = await store.register_worker(
        worker_identity=identity, boot_id=str(uuid.uuid4()), capacity=1, protocol_versions=[1, 2],
        plugin_digest="0" * 64, schema_version=1, workload_classes=["online_text"],
    )
    state = SimpleNamespace(
        factory=factory, run_id=run_id, user=f"approval-user-{nonce}", project=f"approval-project-{nonce}",
        identity=identity, registration_id=registration_id, rounds=[["call-1", "call-2"]],
        provider_calls=0, tool_calls=[],
    )

    async def execute_tool(arguments, _context):
        state.tool_calls.append(arguments)
        return ToolExecutionResult(status="completed", model_content="mutation completed")

    binding = ToolBinding(
        definition=ToolDefinition(
            name="mcp_23_mutate", description="Mutate", input_schema={
                "type": "object", "properties": {"target": {"type": "string"}},
                "required": ["target"], "additionalProperties": False,
            }, effect="external_mutation", source="mcp",
        ), execute=execute_tool, config_fingerprint="a" * 64,
        destination_origin="https://playwright.example",
    )

    async def bindings(_context):
        return {"mcp_23_mutate": binding}

    async def completion_stream(**_kwargs):
        ordinal = state.provider_calls
        state.provider_calls += 1

        async def chunks():
            if ordinal < len(state.rounds):
                yield {"choices": [{"delta": {"tool_calls": [
                    {"index": index, "id": call_id, "function": {
                        "name": "mcp_23_mutate", "arguments": json.dumps({"target": call_id}),
                    }} for index, call_id in enumerate(state.rounds[ordinal])
                ]}}]}
            else:
                yield {"choices": [{"delta": {"content": "done"}}]}
            yield {"usage": {"prompt_tokens": 3, "completion_tokens": 1}}

        return chunks()

    monkeypatch.setattr(graph.bindings, "v2_tool_bindings", bindings)
    monkeypatch.setattr(graph.provider_client, "acompletion_stream", completion_stream)
    monkeypatch.setattr(graph.chat_checkpointer, "_saver", MemorySaver())
    monkeypatch.setattr(interactions, "get_settings", lambda: SimpleNamespace(chat_execution_protocol_version=2))
    try:
        async with factory() as session, session.begin():
            session.add(ChatRun(
                id=run_id, run_scope="persistent", project_id=state.project, user_id=state.user,
                model_name="scripted-approval", capability_snapshot={}, pricing_snapshot={},
                request_payload=encrypt_chat_content('{"required_scopes":["native:runs:write"]}'),
                client_request_id=str(uuid.uuid4()), request_fingerprint=nonce, fingerprint_version=1,
                execution_protocol_version=2, status="queued",
            ))
        state.owner = await _claim(state)
        yield state
    finally:
        async with factory() as session, session.begin():
            await session.execute(delete(ChatRunEventRow).where(ChatRunEventRow.run_id == run_id))
            await session.execute(delete(ChatToolApproval).where(ChatToolApproval.run_id == run_id))
            await session.execute(delete(ChatRun).where(ChatRun.id == run_id))
        await close_db()


async def _claim(state):
    async with state.factory() as session, session.begin():
        await worker_routing.use_read_committed(session)
        run = await claim_queued_run(
            session, state.run_id, owner=state.identity, registration_id=state.registration_id,
        )
        assert run is not None
        return run.lease_owner


async def _requeue(state):
    """Model a new worker claim after requeue, including retries of a journaled checkpoint."""
    async with state.factory() as session, session.begin():
        run = await session.get(ChatRun, state.run_id, with_for_update=True)
        run.status = "queued"
        run.lease_owner = None
        run.lease_expires_at = None
    state.owner = await _claim(state)


async def _stream(state, resume=None):
    return [event async for event in graph.stream(
        model="scripted-approval", messages=[{"role": "user", "content": "mutate"}],
        project_id=state.project, user_id=state.user, execution_protocol_version=2,
        approval_mode="required_for_mutations", run_id=state.run_id, resume=resume,
    )]


async def _snapshot(state):
    async with state.factory() as session:
        run = await session.get(ChatRun, state.run_id)
        approvals = list((await session.execute(select(ChatToolApproval).where(
            ChatToolApproval.run_id == state.run_id,
        ).order_by(ChatToolApproval.call_id))).scalars())
        events = await replay_events(session, run, after_seq=0)
        return SimpleNamespace(
            status=run.status, owner=run.lease_owner, lease_expires_at=run.lease_expires_at,
            approvals={row.call_id: row for row in approvals},
            requests=[event.payload.call_id for event in events if event.type == "tool.approval_required"],
            events=len(events),
        )


async def _decide(state, call_id, decision="approve"):
    return await interactions.resolve_tool_approval(
        run_id=state.run_id, call_id=call_id, decision=decision, project_id=state.project, user_id=state.user,
    )


@pytest.mark.parametrize("partially_journaled", [False, True], ids=["missing-ledger", "partial-ledger"])
async def test_checkpoint_without_ledger_recovers_once_and_resumes_only_after_all_decisions(
    approval_recovery, partially_journaled,
):
    state = approval_recovery
    # Consume the real checkpointed interrupt but deliberately omit its MariaDB persistence:
    # this is the crash window before _persist_v2_approval_interrupt commits.
    events = await _stream(state)
    assert events[-1]["type"] == "input.interrupted"
    assert (await _snapshot(state)).approvals == {}
    assert state.provider_calls == 1 and state.tool_calls == []
    if partially_journaled:
        pending = await graph.pending_interrupt(state.run_id)
        await interactions._persist_v2_approval_interrupt(
            run_id=state.run_id, owner=state.owner, calls=[pending["calls"][0]],
        )
        await _requeue(state)

    with pytest.raises(DurableRunLeaseLost, match="approval recovery released lease"):
        await execution._v2_resume(run_id=state.run_id, owner=state.owner)
    first = await _snapshot(state)
    assert first.status == "awaiting_input" and first.owner is None and first.lease_expires_at is None
    assert first.requests == ["call-1", "call-2"]
    assert {row.status for row in first.approvals.values()} == {"pending"}
    assert state.provider_calls == 1 and state.tool_calls == []

    # A stale worker cannot write after the pause; a new claim of the same checkpoint
    # validates existing rows without extending expiry or duplicating requests.
    with pytest.raises(DurableRunLeaseLost):
        await execution._v2_resume(run_id=state.run_id, owner=state.owner)
    await _requeue(state)
    with pytest.raises(DurableRunLeaseLost, match="approval recovery released lease"):
        await execution._v2_resume(run_id=state.run_id, owner=state.owner)
    second = await _snapshot(state)
    assert second.requests == first.requests
    assert {key: row.expires_at for key, row in second.approvals.items()} == {
        key: row.expires_at for key, row in first.approvals.items()
    }

    partial = await _decide(state, "call-1")
    assert partial["run_status"] == "awaiting_input" and partial["pending_approvals"] == 1
    await _requeue(state)
    with pytest.raises(DurableRunLeaseLost, match="approval recovery released lease"):
        await execution._v2_resume(run_id=state.run_id, owner=state.owner)
    assert state.provider_calls == 1 and state.tool_calls == []
    resolved = await _decide(state, "call-2", "deny")
    assert resolved["run_status"] == "queued"
    state.owner = await _claim(state)
    resume = await execution._v2_resume(run_id=state.run_id, owner=state.owner)
    assert resume == {"kind": "tool_approval", "decisions": [
        {"call_id": "call-1", "decision": "approve"}, {"call_id": "call-2", "decision": "deny"},
    ]}
    assert (await _snapshot(state)).status == "running"
    assert state.provider_calls == 1 and state.tool_calls == []
    await _stream(state, resume)
    assert state.tool_calls == [{"target": "call-1"}]
    assert state.provider_calls == 2


async def test_later_round_never_borrows_old_decisions(approval_recovery):
    state = approval_recovery
    state.rounds = [["call-1"], ["call-2", "call-3"]]
    await _stream(state)
    pending = await graph.pending_interrupt(state.run_id)
    await interactions._persist_v2_approval_interrupt(
        run_id=state.run_id, owner=state.owner, calls=pending["calls"],
    )
    await _decide(state, "call-1")
    state.owner = await _claim(state)
    first_resume = await execution._v2_resume(run_id=state.run_id, owner=state.owner)
    await _stream(state, first_resume)
    assert state.provider_calls == 2 and state.tool_calls == [{"target": "call-1"}]
    # A second crash leaves only the old, approved row while a new round is checkpointed.
    with pytest.raises(DurableRunLeaseLost, match="approval recovery released lease"):
        await execution._v2_resume(run_id=state.run_id, owner=state.owner)
    recovered = await _snapshot(state)
    assert recovered.status == "awaiting_input"
    assert recovered.requests == ["call-1", "call-2", "call-3"]
    assert recovered.approvals["call-1"].status == "approved"
    assert recovered.approvals["call-2"].status == recovered.approvals["call-3"].status == "pending"
    assert state.provider_calls == 2 and state.tool_calls == [{"target": "call-1"}]
    await _decide(state, "call-2")
    await _decide(state, "call-3")
    state.owner = await _claim(state)
    resume = await execution._v2_resume(run_id=state.run_id, owner=state.owner)
    assert resume == {"kind": "tool_approval", "decisions": [
        {"call_id": "call-2", "decision": "approve"}, {"call_id": "call-3", "decision": "approve"},
    ]}
    await _stream(state, resume)
    assert state.tool_calls == [{"target": "call-1"}, {"target": "call-2"}, {"target": "call-3"}]
    assert state.provider_calls == 3


@pytest.mark.parametrize("field,value", [
    ("name", "different_tool"), ("arguments", '{"target":"different"}'), ("source", "managed"),
    ("effect", "process"), ("tool_definition_hash", "b" * 64), ("config_fingerprint", "b" * 64),
    ("destination_origin", "https://different.example"), ("preview", [{"type": "text", "text": "different"}]),
    ("expected_state_revision", 1), ("writer_fence", 1),
])
async def test_checkpoint_identity_mismatch_rolls_back_missing_requests(approval_recovery, monkeypatch, field, value):
    state = approval_recovery
    await _stream(state)
    pending = await graph.pending_interrupt(state.run_id)
    # Only the second request exists. Recovery must roll back its insertion of the
    # first missing request when the later existing identity fails validation.
    await interactions._persist_v2_approval_interrupt(
        run_id=state.run_id, owner=state.owner, calls=[pending["calls"][1]],
    )
    await _decide(state, "call-2")
    state.owner = await _claim(state)
    changed = copy.deepcopy(pending)
    changed["calls"][1][field] = value

    async def pending_interrupt(_run_id):
        return changed

    monkeypatch.setattr(graph, "pending_interrupt", pending_interrupt)
    before = await _snapshot(state)
    with pytest.raises(DurableRunError, match="identity changed"):
        await execution._v2_resume(run_id=state.run_id, owner=state.owner)
    after = await _snapshot(state)
    assert after.status == "running" and after.owner == state.owner
    assert set(after.approvals) == {"call-2"}
    assert after.events == before.events and after.requests == ["call-2"]
    assert state.provider_calls == 1 and state.tool_calls == []


@pytest.mark.parametrize("invalid_lease", ["wrong-owner", "expired"])
async def test_recovery_requires_current_owned_lease(approval_recovery, invalid_lease):
    state = approval_recovery
    await _stream(state)
    owner = state.owner
    if invalid_lease == "wrong-owner":
        owner = "stale-worker#0"
    else:
        async with state.factory() as session, session.begin():
            run = await session.get(ChatRun, state.run_id, with_for_update=True)
            run.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    with pytest.raises(DurableRunLeaseLost):
        await execution._v2_resume(run_id=state.run_id, owner=owner)
    snapshot = await _snapshot(state)
    assert snapshot.approvals == {} and snapshot.requests == []
    assert state.provider_calls == 1 and state.tool_calls == []


def _tampered_ciphertext(plaintext: str) -> str:
    ciphertext = encrypt_chat_content(plaintext)
    assert ciphertext.startswith(crypto._V3_PREFIX)
    raw = bytearray(base64.b64decode(ciphertext[len(crypto._V3_PREFIX):]))
    raw[-1] ^= 0x01
    return crypto._V3_PREFIX + base64.b64encode(bytes(raw)).decode()


@pytest.mark.parametrize("corruption", ["undecryptable", "legacy", "forged"])
async def test_unverifiable_oldest_approval_does_not_block_other_expiry(approval_recovery, monkeypatch, corruption):
    state = approval_recovery
    await _stream(state)
    with pytest.raises(DurableRunLeaseLost, match="approval recovery released lease"):
        await execution._v2_resume(run_id=state.run_id, owner=state.owner)
    assert {row.status for row in (await _snapshot(state)).approvals.values()} == {"pending"}

    now = datetime.now(UTC)
    corrupt_run_id = str(uuid.uuid4())
    arguments = '{"target":"corrupt"}'
    async with state.factory() as session, session.begin():
        session.add(ChatRun(
            id=corrupt_run_id, run_scope="persistent", project_id=state.project, user_id=state.user,
            model_name="scripted-approval", capability_snapshot={}, pricing_snapshot={},
            request_payload=encrypt_chat_content('{"required_scopes":["native:runs:write"]}'),
            client_request_id=str(uuid.uuid4()), request_fingerprint=uuid.uuid4().hex, fingerprint_version=1,
            execution_protocol_version=2, status="awaiting_input",
        ))
        await session.flush()
        # The oldest due approval cannot be authenticated, so it must never be
        # auto-denied, resumed, or allowed to abort the shared sweep transaction.
        session.add(ChatToolApproval(
            run_id=corrupt_run_id, call_id="corrupt-call", tool_name="mcp_23_mutate", arguments="{}",
            arguments_ciphertext=(
                _tampered_ciphertext(arguments) if corruption == "undecryptable" else encrypt_chat_content(arguments)
            ),
            dispatch_hmac=None if corruption == "legacy" else "0" * 64,
            preview_fingerprint="0" * 64, source="mcp", effect="external_mutation",
            tool_definition_hash="a" * 64, config_fingerprint="a" * 64, status="pending",
            expires_at=now - timedelta(days=1),
        ))
    wake_run = AsyncMock()
    monkeypatch.setattr(interactions, "_now", lambda: now + timedelta(minutes=16))
    monkeypatch.setattr(interactions, "wake_run", wake_run)
    try:
        # The sweep is global over the shared database, so assert only on this
        # test's rows. limit=1: the unverifiable run is the oldest due approval,
        # yet it must neither abort the sweep nor consume its only slot.
        first = await interactions.expire_pending_inputs(limit=1)
        assert len(first) == 1 and corrupt_run_id not in first
        resumed = first + await interactions.expire_pending_inputs()
        assert state.run_id in resumed and corrupt_run_id not in resumed
        wake_run.assert_any_await(state.run_id)
        expired = await _snapshot(state)
        assert expired.status == "queued" and expired.owner is None
        assert {row.status for row in expired.approvals.values()} == {"denied"}
        assert all(row.decision_hmac and row.decided_by_user_id is None for row in expired.approvals.values())
        async with state.factory() as session:
            corrupt_run = await session.get(ChatRun, corrupt_run_id)
            corrupt = await session.get(ChatToolApproval, (corrupt_run_id, "corrupt-call"))
            corrupt_events = (await session.scalars(select(ChatRunEventRow.event_type).where(
                ChatRunEventRow.run_id == corrupt_run_id,
            ))).all()
        assert corrupt_run.status == "awaiting_input"
        assert corrupt.status == "pending" and corrupt.decision_hmac is None and corrupt.decided_at is None
        assert corrupt_events == []
        assert corrupt_run_id not in await interactions.expire_pending_inputs()
        state.owner = await _claim(state)
        assert await execution._v2_resume(run_id=state.run_id, owner=state.owner) == {
            "kind": "tool_approval",
            "decisions": [{"call_id": "call-1", "decision": "deny"}, {"call_id": "call-2", "decision": "deny"}],
        }
        assert state.provider_calls == 1 and state.tool_calls == []
    finally:
        async with state.factory() as session, session.begin():
            await session.execute(delete(ChatRunEventRow).where(ChatRunEventRow.run_id == corrupt_run_id))
            await session.execute(delete(ChatToolApproval).where(ChatToolApproval.run_id == corrupt_run_id))
            await session.execute(delete(ChatRun).where(ChatRun.id == corrupt_run_id))
