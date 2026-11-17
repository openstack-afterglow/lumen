"""Approval recovery with MariaDB and optional real encrypted PostgreSQL checkpoints."""

import asyncio
import copy
import json
import os
import sys
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from langgraph.checkpoint.memory import MemorySaver
from lumen_plugin_api.tools import ToolBinding, ToolDefinition, ToolExecutionResult
from sqlalchemy import delete, select

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
        tool_arguments=None, stream_kwargs={},
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
    state.binding = binding

    async def bindings(_context):
        return {state.binding.definition.name: state.binding}

    async def completion_stream(**_kwargs):
        ordinal = state.provider_calls
        state.provider_calls += 1

        async def chunks():
            if ordinal < len(state.rounds):
                yield {"choices": [{"delta": {"tool_calls": [
                    {"index": index, "id": call_id, "function": {
                        "name": state.binding.definition.name,
                        "arguments": json.dumps(state.tool_arguments or {"target": call_id}),
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
        approval_mode="required_for_mutations", run_id=state.run_id, resume=resume, **state.stream_kwargs,
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


@pytest.mark.parametrize("decision", ["approve", "deny", "revoked"])
async def test_encrypted_postgres_vm_approval_survives_fresh_process(
    approval_recovery, monkeypatch, decision,
):
    """Real MariaDB/PostgreSQL; only model and remote cloud authority are synthetic."""
    from lumen.services import mcp_adapter
    from lumen.services.checkpointer import ChatCheckpointer
    from lumen.services.tool_runtime import bindings as tool_bindings
    from lumen.services.tools import ToolContext

    dsn = os.environ.get("LUMEN_TEST_CHECKPOINTER_POSTGRES_URL")
    if not dsn:
        pytest.skip("set LUMEN_TEST_CHECKPOINTER_POSTGRES_URL to a disposable PostgreSQL store")
    state = approval_recovery
    state.rounds = [["create-vm"]]
    arguments = {
        "name": "encrypted-chat-vm",
        "image_id": "11111111-1111-4111-8111-111111111111",
        "flavor_id": "small",
        "network_id": "22222222-2222-4222-8222-222222222222",
        "boot_volume_size_gb": 24,
        "key_name": "owned-key",
        "security_group_ids": ["33333333-3333-4333-8333-333333333333"],
    }
    state.tool_arguments = arguments
    snapshot = {
        "grant_id": "synthetic-vm-grant", "user_id": state.user, "project_id": state.project,
        "credential_epoch": 3, "selection_generation": 7,
    }
    entry = {
        "name": "afterglow_vm_create", "description": "Create one explicitly configured VM.",
        "effect": "external_mutation",
        "input_schema": {
            "type": "object", "additionalProperties": False,
            "properties": {
                name: {"type": "integer", "minimum": 1} if name == "boot_volume_size_gb"
                else {"type": "array", "items": {"type": "string"}, "minItems": 1}
                if name == "security_group_ids" else {"type": "string", "minLength": 1}
                for name in arguments
            },
            "required": list(arguments),
        },
    }
    authority = SimpleNamespace(active=True, previews=[], mutations=[])

    def bridge(request):
        payload = json.loads(request.content)
        if not authority.active:
            return httpx.Response(403, json={"detail": "grant revoked"})
        if request.url.path.endswith("/snapshot"):
            assert payload == {"user_id": state.user, "project_id": state.project}
            return httpx.Response(200, json={
                "snapshot": snapshot, "entries": [entry], "service_fingerprint": "a" * 64,
            })
        assert payload["snapshot"] == snapshot
        assert payload["arguments"] == arguments
        if request.url.path.endswith("/preview"):
            authority.previews.append(payload)
            return httpx.Response(200, json={
                "resource_identity": f"nova-server:new:{state.project}:{arguments['name']}",
                "current_state": None, "intended_transition": "create",
                "dependent_resources": [
                    f"glance-image:{arguments['image_id']}", f"nova-flavor:{arguments['flavor_id']}",
                    f"neutron-network:{arguments['network_id']}",
                    f"nova-keypair:{state.user}:{arguments['key_name']}",
                    f"neutron-security-group:{arguments['security_group_ids'][0]}",
                ],
                "destructive": False, "estimated_effect": "create VM with 24 GiB boot volume",
                "fingerprint": "b" * 64,
            })
        assert request.url.path.endswith("/execute")
        assert payload["idempotency_key"]
        authority.mutations.append(payload)
        return httpx.Response(200, json={"id": "synthetic-server", "status": "BUILD", "requested_action": "create"})

    original_client = httpx.AsyncClient
    transport = httpx.MockTransport(bridge)
    monkeypatch.setattr(mcp_adapter.httpx, "AsyncClient", lambda **kwargs: original_client(
        transport=transport, **kwargs,
    ))
    monkeypatch.setattr(mcp_adapter, "get_settings", lambda: SimpleNamespace(
        mcp_control_plane_url="https://authority.example.test",
        lumen_mcp_service_token="synthetic-service-token", verify=True,
    ))
    context = ToolContext(
        user_id=state.user, project_id=state.project, run_id=state.run_id,
        lumen_snapshot=[snapshot], lumen_snapshot_frozen=True,
    )
    state.binding, = await tool_bindings._lumen_registry_bindings(context)
    state.stream_kwargs = {"lumen_snapshot": [snapshot], "lumen_snapshot_frozen": True}
    checkpoint = ChatCheckpointer()
    monkeypatch.setattr(graph, "chat_checkpointer", checkpoint)
    assert await checkpoint.start(dsn)
    reconnect_script = """
import asyncio, json, os, sys
from lumen.services import graph
from lumen.services.checkpointer import ChatCheckpointer
async def main():
    checkpoint = ChatCheckpointer()
    assert await checkpoint.start(os.environ["LUMEN_TEST_CHECKPOINTER_POSTGRES_URL"])
    graph.chat_checkpointer = checkpoint
    try:
        print(json.dumps(await graph.pending_interrupt(sys.argv[1]), sort_keys=True))
    finally:
        await checkpoint.close()
asyncio.run(main())
"""

    async def inspect_from_fresh_process(*, wrong_key=False):
        environment = os.environ.copy()
        if wrong_key:
            environment["LUMEN_ENCRYPTION_KEY"] = "ab" * 32
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-c", reconnect_script, state.run_id,
            env=environment, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
        if wrong_key:
            assert process.returncode != 0
            assert b"InvalidTag" in stderr
            return None
        assert process.returncode == 0, stderr.decode()
        return json.loads(stdout)

    try:
        events = await _stream(state)
        assert events[-1]["type"] == "input.interrupted"
        pending = await graph.pending_interrupt(state.run_id)
        call, = pending["calls"]
        assert call["name"] == "afterglow_vm_create"
        assert json.loads(call["arguments"]) == arguments
        assert "24 GiB" in call["preview"][0]["text"]
        assert len(authority.previews) == 1 and authority.mutations == []
        async with checkpoint._pool.connection() as connection:
            rows = await (await connection.execute(
                "SELECT type, blob FROM checkpoint_blobs WHERE thread_id = %s",
                (state.run_id,),
            )).fetchall()
        assert rows
        assert any("afterglow-aesgcm-v1" in row["type"] for row in rows)
        assert all(arguments["name"].encode() not in bytes(row["blob"] or b"") for row in rows)
        await interactions._persist_v2_approval_interrupt(
            run_id=state.run_id, owner=state.owner, calls=pending["calls"],
        )
        before = await _snapshot(state)
        assert before.status == "awaiting_input" and before.requests == ["create-vm"]
        assert authority.mutations == []
        await checkpoint.close()

        # No parent saver connection/state survives. Another interpreter reads the
        # encrypted approval with the original key; a different key cannot.
        restored = await inspect_from_fresh_process()
        assert restored == pending
        await inspect_from_fresh_process(wrong_key=True)
        assert await inspect_from_fresh_process() == pending
        checkpoint = ChatCheckpointer()
        monkeypatch.setattr(graph, "chat_checkpointer", checkpoint)
        assert await checkpoint.start(dsn)
        assert await graph.pending_interrupt(state.run_id) == pending
        after = await _snapshot(state)
        assert after.requests == before.requests
        assert after.approvals["create-vm"].expires_at == before.approvals["create-vm"].expires_at
        await _decide(state, "create-vm", "deny" if decision == "deny" else "approve")
        state.owner = await _claim(state)
        resume = await execution._v2_resume(run_id=state.run_id, owner=state.owner)
        assert authority.mutations == []
        if decision == "revoked":
            authority.active = False
        resumed = await _stream(state, resume)
        result = next(event for event in resumed if event.get("type") == "tool_result")
        assert result["status"] == ("completed" if decision == "approve" else "failed")
        assert len(authority.mutations) == (1 if decision == "approve" else 0)
        if authority.mutations:
            assert authority.mutations[0]["arguments"] == arguments
            assert authority.mutations[0]["snapshot"] == snapshot
        resolved = await _snapshot(state)
        assert resolved.requests == ["create-vm"]
        assert resolved.approvals["create-vm"].status == ("denied" if decision == "deny" else "approved")
    finally:
        if not checkpoint.available:
            await checkpoint.start(dsn)
        if checkpoint.saver is not None:
            await checkpoint.saver.adelete_thread(state.run_id)
        await checkpoint.close()
