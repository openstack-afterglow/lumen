import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

from lumen.services.conversation_store import _project_run_activity
from lumen.services.durable_runs import execution


class _Payload:
    def __init__(self, value: dict):
        self.value = value

    def model_dump(self, *, mode: str) -> dict:
        assert mode == "json"
        return self.value


def _event(seq: int, event_type: str, payload: dict) -> SimpleNamespace:
    return SimpleNamespace(
        seq=seq,
        type=event_type,
        payload=_Payload(payload),
        created_at=datetime(2026, 7, 28, 12, 0, seq, tzinfo=UTC),
    )


def test_project_run_activity_preserves_interleaved_reasoning_and_tool_order():
    activity = _project_run_activity(
        [
            _event(1, "part.delta", {"part_type": "reasoning", "message_id": 10, "part_index": 0, "delta": "계획"}),
            _event(
                2,
                "tool.call.started",
                {
                    "call_id": "call-1",
                    "name": "mcp__7__search",
                    "arguments": {"query": "afterglow"},
                    "source": "mcp",
                    "category": "MCP · 문서",
                },
            ),
            _event(
                3,
                "part.completed",
                {
                    "message_id": 10,
                    "part_index": 0,
                    "part": {"type": "reasoning", "visibility": "user", "text": "계획을 세웠습니다."},
                },
            ),
            _event(
                4,
                "tool.call.completed",
                {
                    "call_id": "call-1",
                    "name": "mcp__7__search",
                    "status": "completed",
                    "content": [{"type": "text", "text": "결과"}],
                    "error_code": None,
                    "source": "mcp",
                    "category": "MCP · 문서",
                },
            ),
        ],
        durations_ms={"call-1": 250},
    )

    assert [(item["kind"], item["seq"]) for item in activity] == [("reasoning", 1), ("tool", 2)]
    assert activity[0]["text"] == "계획을 세웠습니다."
    assert activity[1] == {
        "id": "tool:call-1",
        "kind": "tool",
        "seq": 2,
        "createdAt": "2026-07-28T12:00:02Z",
        "callId": "call-1",
        "name": "mcp__7__search",
        "source": "mcp",
        "category": "MCP · 문서",
        "arguments": {"query": "afterglow"},
        "status": "completed",
        "content": [{"type": "text", "text": "결과"}],
        "errorCode": None,
        "durationMs": 250,
    }


def test_project_run_activity_ignores_private_reasoning():
    activity = _project_run_activity(
        [
            _event(
                1,
                "part.completed",
                {
                    "message_id": 10,
                    "part_index": 0,
                    "part": {"type": "reasoning", "visibility": "private", "text": "secret"},
                },
            )
        ],
        durations_ms={},
    )

    assert activity == []


def test_loaded_tool_names_replays_catalog_results_in_ordinal_order(monkeypatch):
    segments = [
        SimpleNamespace(
            result_payload={
                "tool_name": "list_available_tools",
                "content": '{"loaded_tools":["custom__1__weather","mcp__2__search"]}',
            }
        ),
        SimpleNamespace(
            result_payload={
                "tool_name": "list_available_tools",
                "content": '{"loaded_tools":["mcp__2__search","custom__3__alerts"]}',
            }
        ),
    ]

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, _query):
            return SimpleNamespace(scalars=lambda: segments)

    monkeypatch.setattr(execution, "_factory", lambda: lambda: _Session())
    monkeypatch.setattr(execution, "load_segment_payload", lambda payload: payload)

    names = asyncio.run(execution._DurableExecutionHooks(run_id="run-1", owner="worker-1").loaded_tool_names())

    assert names == ["custom__1__weather", "mcp__2__search", "custom__3__alerts"]
