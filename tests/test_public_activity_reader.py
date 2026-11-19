import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from cryptography.exceptions import InvalidTag
from sqlalchemy.dialects import mysql

from lumen.services import run_store


def row(run_id, seq, kind, payload):
    return SimpleNamespace(
        run_id=run_id, seq=seq, event_type=kind,
        payload=json.dumps(payload), created_at=datetime(2026, 11, 17, tzinfo=UTC),
    )


class Rows:
    def __init__(self, values):
        self.values = values
        self.closed = False

    def __aiter__(self):
        async def iterate():
            for value in self.values:
                yield value
        return iterate()

    async def close(self):
        self.closed = True


class Session:
    def __init__(self, values):
        self.rows = Rows(values)
        self.calls = []

    async def stream_scalars(self, statement):
        self.calls.append(statement)
        return self.rows


async def test_public_activity_query_batches_runs_filters_tokens_and_bounds_each_run():
    session = Session([
        row("first", 1, "run.started", {"model_name": "model", "effective_features": {}}),
        row("second", 6000, "run.completed", {"status": "completed"}),
    ])
    result = await run_store.read_public_activity_events(session, ["first", "second"])
    assert set(result) == {"first", "second"}
    assert len(session.calls) == 1
    compiled = session.calls[0].compile(dialect=mysql.dialect(), compile_kwargs={"literal_binds": True})
    sql = str(compiled)
    assert "row_number() OVER (PARTITION BY chat_run_events.run_id" in sql
    assert "chat_run_events.event_type IN" in sql
    assert "'part.delta'" not in sql
    assert "'usage.updated'" not in sql
    assert "'part.completed'" in sql
    assert f"activity_ordinal <= {run_store._MAX_PUBLIC_ACTIVITY_EVENTS + 1}" in sql
    assert session.calls[0].get_execution_options()["yield_per"] == 64
    assert session.rows.closed


async def test_consumed_corruption_is_explicit_per_run_without_exposing_payload(caplog):
    session = Session([
        row("bad", 1, "run.stage.changed", {"stage": "PRIVATE_INVALID_PAYLOAD"}),
        row("good", 1, "run.completed", {"status": "completed"}),
    ])
    result = await run_store.read_public_activity_events(session, ["bad", "good"])
    assert result["bad"].events == []
    assert result["bad"].unavailable_code == "history_activity_invalid"
    assert result["good"].unavailable_code is None
    assert result["good"].events[0].type == "run.completed"
    assert "PRIVATE_INVALID_PAYLOAD" not in caplog.text
    assert "reason=invalid" in caplog.text


async def test_public_activity_uses_only_completed_user_reasoning_and_never_hidden_reasoning():
    session = Session([
        row("run", 1, "part.completed", {"message_id": "answer", "part_index": 0,
             "part": {"type": "text", "text": "Body is stored separately"}}),
        row("run", 2, "part.completed", {"message_id": "answer", "part_index": 1,
             "part": {"type": "reasoning", "visibility": "hidden", "text": "PRIVATE"}}),
        row("run", 3, "part.completed", {"message_id": "answer", "part_index": 2,
             "part": {"type": "reasoning", "visibility": "user", "text": "Public summary"}}),
        row("run", 4, "run.completed", {"status": "completed"}),
    ])
    result = (await run_store.read_public_activity_events(session, ["run"]))["run"]
    assert result.unavailable_code is None
    assert [event.type for event in result.events] == ["part.completed", "run.completed"]
    assert result.events[0].payload.part.text == "Public summary"
    assert "PRIVATE" not in str(result)


async def test_event_bound_never_publishes_a_truncated_log(monkeypatch):
    monkeypatch.setattr(run_store, "_MAX_PUBLIC_ACTIVITY_EVENTS", 2)
    session = Session([
        row("run", index, "run.stage.changed", {"stage": "model_request"}) for index in range(1, 4)
    ])
    result = (await run_store.read_public_activity_events(session, ["run"]))["run"]
    assert result.events == []
    assert result.unavailable_code == "history_activity_limit"


async def test_ciphertext_bound_stops_before_decryption(monkeypatch):
    monkeypatch.setattr(run_store, "_MAX_PUBLIC_ACTIVITY_CIPHERTEXT", 1)
    monkeypatch.setattr(run_store, "decrypt_chat_content", lambda _: pytest.fail("oversized row must not decrypt"))
    session = Session([row("run", 1, "run.completed", {"status": "completed"})])
    result = (await run_store.read_public_activity_events(session, ["run"]))["run"]
    assert result.events == []
    assert result.unavailable_code == "history_activity_limit"


async def test_undecryptable_activity_is_unavailable_not_a_body_failure(monkeypatch):
    def invalid(_):
        raise InvalidTag
    monkeypatch.setattr(run_store, "decrypt_chat_content", invalid)
    session = Session([row("run", 1, "run.completed", {"status": "completed"})])
    result = (await run_store.read_public_activity_events(session, ["run"]))["run"]
    assert result.events == []
    assert result.unavailable_code == "history_activity_invalid"


@pytest.mark.parametrize("case", ["orphan", "wrong-name", "settled"])
async def test_invalid_progress_lifecycle_is_explicitly_unavailable(case):
    values = []
    if case != "orphan":
        values.append(row("run", 1, "tool.call.started", {"call_id": "call", "name": "inspect", "arguments": {}}))
    if case == "settled":
        values.append(row("run", 2, "tool.call.completed", {
            "call_id": "call", "name": "inspect", "status": "failed",
            "content": [{"type": "text", "text": "Safe failure"}],
        }))
    values.append(row("run", len(values) + 1, "tool.call.progress", {
        "call_id": "call", "name": "other" if case == "wrong-name" else "inspect", "text": "Public progress",
    }))
    result = (await run_store.read_public_activity_events(Session(values), ["run"]))["run"]
    assert result.events == []
    assert result.unavailable_code == "history_activity_invalid"


async def test_public_reader_selects_and_retains_valid_progress_before_failure():
    session = Session([
        row("run", 1, "tool.call.started", {"call_id": "call", "name": "inspect", "arguments": {}}),
        row("run", 2, "tool.call.progress", {"call_id": "call", "name": "inspect", "text": "  downloaded\\n"}),
        row("run", 3, "tool.call.completed", {
            "call_id": "call", "name": "inspect", "status": "failed",
            "content": [{"type": "text", "text": "Safe failure"}],
        }),
    ])
    result = (await run_store.read_public_activity_events(session, ["run"]))["run"]
    assert result.unavailable_code is None
    assert [event.type for event in result.events] == ["tool.call.started", "tool.call.progress", "tool.call.completed"]
    assert result.events[1].payload.text == "  downloaded\\n"
    compiled = str(session.calls[0].compile(dialect=mysql.dialect(), compile_kwargs={"literal_binds": True}))
    assert "'tool.call.progress'" in compiled
    assert "'part.delta'" not in compiled
