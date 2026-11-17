"""Public progress remains bounded and inspectable after tool failure/completion."""
import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from lumen.db import close_db, get_session_factory, init_db
from lumen.models.chat_contracts import validate_chat_run_event
from lumen.models.chat_runs import ChatRun, ChatRunTurn
from lumen.services import conversation_store as store
from lumen.services.run_store import append_event

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("tool_status", ["completed", "failed"])
async def test_public_tool_progress_survives_indexed_and_legacy_reload(tool_status):
    init_db(os.environ["DATABASE_URL"], pool_size=1, max_overflow=0)
    nonce = uuid.uuid4().hex
    owner = {"user_id": f"progress-user-{nonce}", "project_id": f"progress-project-{nonce}"}
    try:
        conversation = await store.create_conversation(title="Progress", model_name=None, **owner)
        question = await store.add_message(conversation["id"], role="user", content="Inspect", set_leaf=True)
        answer = await store.add_message(
            conversation["id"], role="assistant", content="Valid final answer", parent_id=question["id"], set_leaf=True,
        )
        run_id = str(uuid.uuid4())
        lines = [f"  step {index}\n" for index in range(40)]
        script = [
            ("run.started", {"model_name": "synthetic", "effective_features": {}}),
            ("tool.call.started", {"call_id": "call", "name": "inspect", "arguments": {"target": "owned"}}),
        ]
        for text in lines:
            script.extend([("tool.call.progress", {"call_id": "call", "name": "inspect", "text": text})] * 2)
        script.extend([
            ("tool.call.completed", {
                "call_id": "call", "name": "inspect", "status": tool_status,
                "error_code": "tool_failed" if tool_status == "failed" else None,
                "content": [{"type": "text", "text": f"Safe {tool_status} result"}],
            }),
            ("run.completed", {"status": "completed", "message_id": str(answer["id"])}),
        ])
        factory = get_session_factory()
        async with factory() as session, session.begin():
            run = ChatRun(
                id=run_id, run_scope="persistent", conversation_id=conversation["id"], **owner,
                user_message_id=question["id"], assistant_message_id=answer["id"], model_name="synthetic",
                capability_snapshot={}, pricing_snapshot={}, client_request_id=str(uuid.uuid4()),
                request_fingerprint=nonce, fingerprint_version=1, status="completed",
            )
            session.add(run)
            await session.flush()
            session.add(ChatRunTurn(run_id=run_id, ordinal=0, assistant_message_id=answer["id"]))
            for seq, (kind, payload) in enumerate(script, 1):
                row = await append_event(session, run, validate_chat_run_event({
                    "event_id": f"{run_id}:{seq}", "run_id": run_id, "seq": seq, "type": kind,
                    "created_at": datetime(2026, 11, 17, tzinfo=UTC) + timedelta(seconds=seq), "payload": payload,
                }))
                row.created_at = datetime(2026, 11, 17, tzinfo=UTC) + timedelta(seconds=seq)
        for _ in range(2):
            indexed = await store.list_message_page(conversation["id"], **owner)
            legacy = await store.list_messages(conversation["id"], **owner)
            for message in [indexed["messages"][-1], legacy[-1]]:
                assert message["content"] == "Valid final answer"
                execution = message["execution"]
                assert execution["started_at"] == "2026-11-17T00:00:01Z"
                assert execution["last_seq"] == len(script)
                assert execution["activity_status"] == "available"
                assert execution["activity_error_code"] is None
                assert [item["kind"] for item in execution["activity"]] == ["tool", "terminal"]
                tool = execution["activity"][0]
                assert tool["progress"] == lines[-32:]
                assert len(tool["progress"]) == 32
                assert tool["status"] == tool_status
                assert tool["content"][0]["text"] == f"Safe {tool_status} result"
                assert tool["errorCode"] == ("tool_failed" if tool_status == "failed" else None)
    finally:
        await close_db()
