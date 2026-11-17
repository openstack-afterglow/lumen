from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from lumen.models.chat_contracts import validate_chat_run_event
from lumen.services.conversation_store import _project_run_activity


def event(seq, kind, payload):
    return validate_chat_run_event({
        "event_id": f"run-1:{seq}", "run_id": "run-1", "seq": seq, "type": kind,
        "created_at": datetime(2026, 11, 17, tzinfo=UTC) + timedelta(seconds=seq), "payload": payload,
    })


@pytest.mark.parametrize("text", ["x", "  public progress\n", "🙂" * 4000])
def test_public_progress_contract_preserves_bounded_unicode_without_trimming(text):
    progress = event(1, "tool.call.progress", {"call_id": "call", "name": "inspect", "text": text})
    assert progress.payload.text == text
    assert set(progress.payload.model_dump()) == {"call_id", "name", "text"}


@pytest.mark.parametrize("text", ["", "x" * 4001, None, [], 123])
def test_public_progress_contract_rejects_invalid_text(text):
    with pytest.raises(ValidationError):
        event(1, "tool.call.progress", {"call_id": "call", "name": "inspect", "text": text})


def test_public_progress_contract_rejects_private_or_unrelated_payload_fields():
    with pytest.raises(ValidationError):
        event(1, "tool.call.progress", {
            "call_id": "call", "name": "inspect", "text": "Public status", "private_reasoning": "hidden",
        })


@pytest.mark.parametrize("status", ["completed", "failed"])
def test_shared_projection_keeps_last_32_distinct_adjacent_progress_lines_after_tool_settles(status):
    events = [event(1, "tool.call.started", {"call_id": "call", "name": "inspect", "arguments": {}})]
    lines = [f"  step {index}\n" for index in range(40)]
    for text in lines:
        for _ in range(2):
            events.append(event(len(events) + 1, "tool.call.progress", {"call_id": "call", "name": "inspect", "text": text}))
    events.append(event(len(events) + 1, "tool.call.completed", {
        "call_id": "call", "name": "inspect", "status": status,
        "content": [{"type": "text", "text": "Safe result"}],
        "error_code": "tool_failed" if status == "failed" else None,
    }))
    activity = _project_run_activity(events, durations_ms={})
    assert len(activity) == 1
    assert activity[0]["kind"] == "tool"
    assert activity[0]["status"] == status
    assert activity[0]["progress"] == lines[-32:]
    assert activity[0]["content"][0]["text"] == "Safe result"
