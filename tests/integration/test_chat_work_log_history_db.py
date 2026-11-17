"""Real SQL history readback, without providers or cloud mutations."""
import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from lumen.db import close_db, get_session_factory, init_db
from lumen.models.chat_contracts import validate_chat_run_event
from lumen.models.chat_runs import ChatRun, ChatRunEventRow, ChatRunTurn
from lumen.services import conversation_store as store
from lumen.services import run_store
from lumen.services.run_store import append_event

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("with_start", [True, False])
@pytest.mark.parametrize("journal_case", ["clean", "irrelevant", "consumed"])
async def test_indexed_history_restores_work_log_on_final_assistant_and_owned_fork(with_start, journal_case, monkeypatch):
    init_db(os.environ["DATABASE_URL"], pool_size=1, max_overflow=0)
    nonce = uuid.uuid4().hex
    owner = {"user_id": f"work-log-{nonce}", "project_id": f"work-project-{nonce}"}
    try:
        conv = await store.create_conversation(title="Work log", model_name=None, **owner)
        user = await store.add_message(conv["id"], role="user", content="inspect", set_leaf=True)
        answer = await store.add_message(conv["id"], role="assistant", content="done", parent_id=user["id"], set_leaf=True)
        run_id = str(uuid.uuid4())
        factory = get_session_factory()
        async with factory() as session, session.begin():
            run = ChatRun(
                id=run_id, run_scope="persistent", conversation_id=conv["id"], **owner,
                user_message_id=user["id"], assistant_message_id=answer["id"], model_name="synthetic",
                capability_snapshot={}, pricing_snapshot={}, client_request_id=str(uuid.uuid4()),
                request_fingerprint=nonce, fingerprint_version=1, status="completed",
            )
            session.add(run)
            await session.flush()
            session.add(ChatRunTurn(run_id=run_id, ordinal=0, assistant_message_id=answer["id"]))
            script = [
                ("run.stage.changed", {"stage": "model_request"}),
                ("tool.approval_required", {
                    "call_id": "call", "name": "inspect", "source": "mcp", "effect": "read",
                    "redacted_arguments": {"target": "<redacted>"}, "preview": [{"type": "text", "text": "Inspect?"}],
                    "expires_at": "2026-11-18T00:00:00Z",
                }),
                ("tool.approval_resolved", {"call_id": "call", "decision": "approve", "decided_by_user_id": None, "decided_at": "2026-11-17T00:00:04Z"}),
                ("tool.call.started", {"call_id": "call", "name": "inspect", "arguments": {}, "source": "mcp"}),
                ("tool.call.completed", {
                    "call_id": "call", "name": "inspect", "status": "failed", "error_code": "unavailable",
                    "content": [{"type": "text", "text": "Safe tool failure"}], "source": "mcp",
                }),
                ("part.completed", {"message_id": str(answer["id"]), "part_index": 0,
                                    "part": {"type": "reasoning", "visibility": "user", "text": "Public summary"}}),
                ("run.completed", {"status": "completed", "message_id": str(answer["id"])}),
            ]
            if with_start:
                script.insert(0, ("run.started", {"model_name": "synthetic", "effective_features": {}}))
            for observed_second, (kind, payload) in enumerate(script, 1):
                if kind == "run.completed" and journal_case == "irrelevant":
                    # Old corrupt token rows must neither be fetched nor decrypted by public history.
                    for _ in range(4096):
                        run.last_seq += 1
                        session.add(ChatRunEventRow(
                            run_id=run_id, seq=run.last_seq, event_type="part.delta", payload="irrelevant-token-corrupt",
                        ))
                seq = run.last_seq + 1
                row = await append_event(session, run, validate_chat_run_event({
                    "event_id": f"{run_id}:{seq}", "run_id": run_id, "seq": seq,
                    "created_at": datetime(2026, 11, 17, tzinfo=UTC) + timedelta(seconds=observed_second),
                    "type": kind, "payload": payload,
                }))
                row.created_at = datetime(2026, 11, 17, tzinfo=UTC) + timedelta(seconds=observed_second)
                if kind == "tool.call.completed" and journal_case == "consumed":
                    row.payload = "corrupt-consumed-tool"
        fork = await store.fork_conversation(conv["id"], message_id=answer["id"], **owner)
        fetched_types = []
        decrypt_calls = []
        original_stream = AsyncSession.stream_scalars
        original_decrypt = run_store.decrypt_chat_content

        async def tracked_stream(session, statement, *args, **kwargs):
            result = await original_stream(session, statement, *args, **kwargs)

            class TrackedRows:
                def __aiter__(self):
                    async def iterate():
                        async for item in result:
                            fetched_types.append(item.event_type)
                            yield item
                    return iterate()

                async def close(self):
                    await result.close()

            return TrackedRows()

        def tracked_decrypt(value):
            assert value != "irrelevant-token-corrupt"
            decrypt_calls.append(value)
            return original_decrypt(value)

        monkeypatch.setattr(AsyncSession, "stream_scalars", tracked_stream)
        monkeypatch.setattr(run_store, "decrypt_chat_content", tracked_decrypt)
        for conversation_id in [conv["id"], fork["id"]]:
            for _ in range(2):
                page = await store.list_message_page(conversation_id, limit=1, **owner)
                assert len(page["messages"]) == 1
                message = page["messages"][0]
                assert message["id"] == answer["id"]
                assert message["content"] == "done"
                assert message["execution"]["run_id"] == run_id
                assert set(message["execution"]) == {
                    "run_id", "status", "last_seq", "started_at", "activity", "activity_status", "activity_error_code",
                }
                legacy = await store.list_messages(conversation_id, limit=1, offset=1, **owner)
                assert legacy[0]["content"] == "done"
                for key in message["execution"]:
                    assert legacy[0]["execution"][key] == message["execution"][key]
                if journal_case == "consumed":
                    assert message["execution"]["activity_status"] == "unavailable"
                    assert message["execution"]["activity_error_code"] == "history_activity_invalid"
                    assert message["execution"]["activity"] is None
                    assert message["execution"]["started_at"] is None
                else:
                    assert message["execution"]["activity_status"] == "available"
                    assert message["execution"]["activity_error_code"] is None
                    assert message["execution"]["started_at"] == ("2026-11-17T00:00:01Z" if with_start else None)
                    activity = message["execution"]["activity"]
                    assert [item["kind"] for item in activity] == ["stage", "approval", "tool", "reasoning", "terminal"]
                    assert set(activity[0]) == {"id", "kind", "seq", "createdAt", "stage", "toolName"}
                    assert set(activity[-1]) == {"id", "kind", "seq", "createdAt", "status", "errorCode", "message"}
                    assert activity[-1]["createdAt"] == f"2026-11-17T00:00:0{8 if with_start else 7}Z"
                    assert activity[1]["decision"] == "approve"
                    assert activity[2]["status"] == "failed"
                    assert activity[2]["content"][0]["text"] == "Safe tool failure"
                    assert activity[3]["text"] == "Public summary"
        assert "part.delta" not in fetched_types
        assert len(fetched_types) == len(script) * 8
        assert len(decrypt_calls) <= len(script) * 8
        with pytest.raises(store.ConversationForbidden):
            await store.list_message_page(conv["id"], user_id="foreign", project_id=owner["project_id"])
    finally:
        await close_db()
