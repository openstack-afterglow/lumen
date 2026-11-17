from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from lumen.services.conversation_store import _project_run_activity


def event(seq, kind, **payload):
    return SimpleNamespace(
        seq=seq, type=kind, created_at=datetime(2026, 11, 17, tzinfo=UTC) + timedelta(seconds=seq),
        payload=SimpleNamespace(model_dump=lambda **_: payload),
    )


def test_work_log_preserves_lifecycle_permission_failure_and_observed_time():
    events = [
        event(1, "run.started"),
        event(2, "run.stage.changed", stage="model_request"),
        event(3, "tool.approval_required", call_id="call", name="mutate", redacted_arguments={"target": "safe"},
              preview=[{"type": "text", "text": "Update target?"}], effect="external_mutation",
              destination="https://example.invalid", expires_at="2026-11-17T01:00:00Z"),
        event(4, "tool.approval_resolved", call_id="call", decision="approve", decided_at="2026-11-17T00:00:04Z"),
        event(5, "tool.call.started", call_id="call", name="mutate", arguments={}, source="mcp"),
        event(6, "tool.call.completed", call_id="call", name="mutate", status="failed",
              content=[{"type": "text", "text": "Permission denied"}], error_code="forbidden"),
        event(7, "run.completed", status="completed"),
    ]
    activity = _project_run_activity(events, durations_ms={})
    assert [item["kind"] for item in activity] == ["started", "stage", "approval", "tool", "terminal"]
    assert activity[0]["createdAt"] == "2026-11-17T00:00:01Z"
    assert activity[-1]["createdAt"] == "2026-11-17T00:00:07Z"
    assert activity[2]["decision"] == "approve"
    assert activity[2]["arguments"] == {"target": "safe"}
    assert activity[3]["status"] == "failed"
    assert activity[3]["durationMs"] == 1000
    assert activity[3]["content"][0]["text"] == "Permission denied"


def test_work_log_preserves_child_outcome_and_public_reasoning_only():
    activity = _project_run_activity([
        event(1, "child.created", child_run_id="child", call_id="call", wait_group_id="wait", agent_id=1,
              access="read", ordinal=0, status="queued"),
        event(2, "part.completed", message_id="answer", part_index=0,
              part={"type": "reasoning", "visibility": "hidden", "text": "private"}),
        event(3, "part.completed", message_id="answer", part_index=1,
              part={"type": "reasoning", "visibility": "user", "text": "Public summary"}),
        event(4, "child.completed", child_run_id="child", call_id="call", wait_group_id="wait",
              status="failed", error_code="unavailable", summary="Safe child failure", artifacts=[]),
    ], durations_ms={})
    assert [item["kind"] for item in activity] == ["child", "reasoning"]
    assert activity[0]["status"] == "failed"
    assert activity[0]["summary"] == "Safe child failure"
    assert activity[1]["text"] == "Public summary"
    assert "private" not in str(activity)
