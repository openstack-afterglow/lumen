"""Exact run-message loading for post-response memory extraction."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from lumen.services import conversation_store as cs

pytestmark = pytest.mark.asyncio


class _Result:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value

    def scalars(self):
        return self.value


class _Session:
    def __init__(self, results):
        self.results = iter(results)

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback):
        return False

    async def execute(self, _statement):
        return next(self.results)


async def test_memory_loader_uses_the_final_run_turn_after_a_long_conversation(monkeypatch):
    # IDs 1–8 represent older history. The run's user message is ID 9, so the
    # extraction path must not use the conversation list's oldest-eight window.
    run = SimpleNamespace(id="run-9", conversation_id="conversation-1", user_message_id=9)
    latest_user = SimpleNamespace(id=9, role="user", content="최신 선호: 터미널 대신 GUI를 사용")
    latest_assistant = SimpleNamespace(id=10, role="assistant", content="최신 응답")
    session = _Session([_Result(run), _Result([10]), _Result([latest_assistant, latest_user])])

    monkeypatch.setattr(cs, "_require_db", lambda: lambda: session)
    monkeypatch.setattr(
        cs,
        "_msg_public",
        lambda row: {"id": row.id, "role": row.role, "content": row.content},
    )

    messages = await cs.list_messages_for_run("run-9", user_id="user-1", project_id="project-1")

    assert messages == [
        {"id": 9, "role": "user", "content": "최신 선호: 터미널 대신 GUI를 사용"},
        {"id": 10, "role": "assistant", "content": "최신 응답"},
    ]
