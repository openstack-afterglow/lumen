"""Baseline temporary-thread contract plus additive owned execution metadata."""
import json
import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from lumen.crypto import encrypt_chat_content
from lumen.db import close_db, get_session_factory, init_db
from lumen.models.chat_contracts import validate_chat_run_event
from lumen.models.chat_runs import ChatRun, ChatTempThread
from lumen.services.durable_runs.errors import DurableRunNotFound
from lumen.services.durable_runs.queries import owned_temp_thread
from lumen.services.run_store import append_event

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("status", ["completed", "awaiting_input"])
async def test_temp_thread_baseline_preserves_owned_start_and_approval_without_message_position(status):
    init_db(os.environ["DATABASE_URL"], pool_size=1, max_overflow=0)
    nonce = uuid.uuid4().hex
    thread_id, run_id = str(uuid.uuid4()), str(uuid.uuid4())
    owner = {"user_id": f"temp-work-{nonce}", "project_id": f"temp-project-{nonce}"}
    history = [
        {"role": "user", "parts": [{"type": "text", "text": "Saved question"}]},
        {"role": "assistant", "parts": [{"type": "text", "text": "Saved answer"}]},
    ]
    try:
        factory = get_session_factory()
        async with factory() as session, session.begin():
            session.add(ChatTempThread(
                id=thread_id, **owner, history=encrypt_chat_content(json.dumps(history)),
                active_run_id=run_id if status == "awaiting_input" else None,
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            ))
            run = ChatRun(
                id=run_id, run_scope="temp", temp_thread_id=thread_id, **owner,
                model_name="synthetic", capability_snapshot={}, pricing_snapshot={},
                client_request_id=str(uuid.uuid4()), request_fingerprint=nonce,
                fingerprint_version=1, status=status,
            )
            session.add(run)
            await session.flush()
            script = [
                ("run.started", {"model_name": "synthetic", "effective_features": {}}),
                ("run.stage.changed", {"stage": "model_request"}),
                ("tool.approval_required", {
                    "call_id": "owned-call", "name": "inspect", "source": "mcp", "effect": "read",
                    "redacted_arguments": {"target": "<redacted>"},
                    "preview": [{"type": "text", "text": "Inspect owned resource?"}],
                    "expires_at": "2099-11-18T00:00:00Z",
                }),
            ]
            if status == "completed":
                script.extend([
                    ("tool.approval_resolved", {
                        "call_id": "owned-call", "decision": "deny", "decided_by_user_id": None,
                        "decided_at": "2026-11-17T00:00:04Z",
                    }),
                    ("run.completed", {"status": "completed"}),
                ])
            for seq, (kind, payload) in enumerate(script, 1):
                row = await append_event(session, run, validate_chat_run_event({
                    "event_id": f"{run_id}:{seq}", "run_id": run_id, "seq": seq,
                    "created_at": datetime(2026, 11, 17, tzinfo=UTC) + timedelta(seconds=seq),
                    "type": kind, "payload": payload,
                }))
                row.created_at = datetime(2026, 11, 17, tzinfo=UTC) + timedelta(seconds=seq)
        for _ in range(2):
            snapshot = await owned_temp_thread(thread_id=thread_id, **owner)
            assert set(snapshot) == {"temp_thread_id", "public_history", "active_run", "latest_run", "latest_run_execution"}
            assert snapshot["public_history"] == history
            assert all(set(turn) == {"role", "parts"} for turn in snapshot["public_history"])
            execution = snapshot["latest_run_execution"]
            assert set(execution) == {
                "run_id", "run_kind", "status", "model_name", "last_seq", "started_at",
                "activity", "activity_status", "activity_error_code",
            }
            assert execution["run_id"] == run_id
            assert execution["started_at"] == "2026-11-17T00:00:01Z"
            assert execution["last_seq"] == len(script)
            assert execution["activity_status"] == "available"
            assert execution["activity_error_code"] is None
            assert execution["activity"][0]["kind"] == "stage"
            approval = next(item for item in execution["activity"] if item["kind"] == "approval")
            assert approval["callId"] == "owned-call"
            assert approval["decision"] == ("deny" if status == "completed" else None)
            assert (snapshot["active_run"] is not None) == (status == "awaiting_input")
            assert "messages" not in snapshot and "unpositioned_runs" not in snapshot
        for wrong in [{**owner, "user_id": "foreign"}, {**owner, "project_id": "foreign"}]:
            with pytest.raises(DurableRunNotFound):
                await owned_temp_thread(thread_id=thread_id, **wrong)
        async with factory() as session, session.begin():
            thread = await session.get(ChatTempThread, thread_id)
            thread.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        with pytest.raises(DurableRunNotFound):
            await owned_temp_thread(thread_id=thread_id, **owner)
    finally:
        await close_db()
