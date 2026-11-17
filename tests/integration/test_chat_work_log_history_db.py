"""Real SQL history readback, without providers or cloud mutations."""
import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from lumen.db import close_db, get_session_factory, init_db
from lumen.models.chat_contracts import validate_chat_run_event
from lumen.models.chat_runs import ChatRun
from lumen.services import conversation_store as store
from lumen.services.run_store import append_event

pytestmark = pytest.mark.integration


async def test_indexed_history_restores_work_log_on_final_assistant_and_owned_fork():
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
            for seq, (kind, payload) in enumerate([
                ("run.started", {"model_name": "synthetic", "effective_features": {}}),
                ("run.stage.changed", {"stage": "model_request"}),
                ("run.completed", {"status": "completed", "message_id": str(answer["id"])}),
            ], 1):
                row = await append_event(session, run, validate_chat_run_event({
                    "event_id": f"{run_id}:{seq}", "run_id": run_id, "seq": seq,
                    "created_at": datetime(2026, 11, 17, tzinfo=UTC) + timedelta(seconds=seq),
                    "type": kind, "payload": payload,
                }))
                row.created_at = datetime(2026, 11, 17, tzinfo=UTC) + timedelta(seconds=seq)
        fork = await store.fork_conversation(conv["id"], message_id=answer["id"], **owner)
        for conversation_id in [conv["id"], fork["id"]]:
            for _ in range(2):
                page = await store.list_message_page(conversation_id, limit=1, **owner)
                assert len(page["messages"]) == 1
                message = page["messages"][0]
                assert message["id"] == answer["id"]
                assert message["content"] == "done"
                assert message["execution"]["run_id"] == run_id
                activity = message["execution"]["activity"]
                assert [item["kind"] for item in activity] == ["started", "stage", "terminal"]
                assert activity[-1]["createdAt"] == "2026-11-17T00:00:03Z"
        with pytest.raises(store.ConversationForbidden):
            await store.list_message_page(conv["id"], user_id="foreign", project_id=owner["project_id"])
    finally:
        await close_db()
