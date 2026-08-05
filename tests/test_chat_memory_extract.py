"""자동 메모리 추출(memory_extract) 단위 테스트 — ADD/UPDATE ops 반영 + id 검증 + 미지정 스킵."""

from __future__ import annotations

from decimal import Decimal

import pytest

from lumen.services import memory_extract as me


class _Msg:
    def __init__(self, content):
        self.message = type("M", (), {"content": content})()


class _Resp:
    def __init__(self, content):
        self.choices = [_Msg(content)]
        self.usage = {"prompt_tokens": 10, "completion_tokens": 5}


class TestParseOps:
    def test_plain_array(self):
        assert me._parse_ops('[{"op":"add","content":"서울 거주"}]') == [{"op": "add", "content": "서울 거주"}]

    def test_code_fence_and_noise(self):
        assert me._parse_ops('설명\n```json\n[{"op":"update","id":3,"content":"x"}]\n```') == [
            {"op": "update", "id": 3, "content": "x"}
        ]

    def test_invalid_returns_empty(self):
        assert me._parse_ops("not json") == []
        assert me._parse_ops("") == []
        assert me._parse_ops('{"op":"add"}') == []  # 배열 아님


class TestGenerateMemory:
    async def test_no_memory_model_skips(self, monkeypatch):
        called = {"acompletion": False}

        async def fake_resolve():
            return None

        async def fake_acompletion(*a, **k):
            called["acompletion"] = True
            return _Resp("[]")

        monkeypatch.setattr(me.ps, "resolve_memory_model", fake_resolve)
        monkeypatch.setattr(me.litellm_client, "acompletion", fake_acompletion)
        await me.generate_memory_if_applicable(conversation_id="c1", project_id="p1", run_id="r1", user_id="u1")
        assert called["acompletion"] is False  # 모델 미지정 → 호출 안 함

    async def test_add_update_and_delete_deltas_are_returned_for_one_atomic_job_commit(self, monkeypatch):
        async def fake_resolve():
            return {
                "model_name": "tiny",
                "provider_name": "p",
                "provider_type": "openai",
                "api_base": None,
                "api_key": "k",
                "margin_multiplier": Decimal("1.0"),
                "input_price_per_token": None,
                "output_price_per_token": None,
                "price_source": None,
            }

        async def fake_list_memories(*, user_id, project_id):
            assert (user_id, project_id) == ("u1", "p1")
            return [
                {
                    "id": 5,
                    "scope": "project",
                    "project_id": "p1",
                    "category": "general",
                    "content": "기존 사실",
                    "status": "active",
                    "is_active": True,
                },
                {
                    "id": 6,
                    "scope": "project",
                    "project_id": "p1",
                    "category": "habit",
                    "content": "낡은 습관",
                    "status": "active",
                    "is_active": True,
                },
                {
                    "id": 7,
                    "scope": "account",
                    "project_id": None,
                    "category": "preference",
                    "content": "계정 전체 사실",
                    "status": "active",
                    "is_active": True,
                },
            ]

        async def fake_list_messages_for_run(run_id, *, user_id, project_id):
            assert (run_id, user_id, project_id) == ("r1", "u1", "p1")
            return [{"role": "user", "content": "나는 커피를 좋아해"}, {"role": "assistant", "content": "알겠어요"}]

        async def fake_acompletion(_model, messages, **_kwargs):
            assert "assistant: 알겠어요" in messages[-1]["content"]
            assert "계정 전체 사실" not in messages[-1]["content"]
            return _Resp(
                '[{"op":"add","category":"interest","content":"커피 선호"},'
                '{"op":"update","id":5,"category":"preference","content":"갱신된 사실"},'
                '{"op":"delete","id":6},'
                '{"op":"update","id":7,"category":"general","content":"계정 변경 불가"},'
                '{"op":"update","id":999,"category":"general","content":"소유 아님"}]'
            )

        async def fake_credit(**_kwargs):
            return Decimal("0")

        monkeypatch.setattr(me.ps, "resolve_memory_model", fake_resolve)
        monkeypatch.setattr(me.ms, "list_memories", fake_list_memories)
        monkeypatch.setattr(me.cs, "list_messages_for_run", fake_list_messages_for_run)
        monkeypatch.setattr(me.litellm_client, "acompletion", fake_acompletion)
        monkeypatch.setattr(me.credit, "apply_usage", fake_credit)

        result = await me.generate_memory_if_applicable(
            conversation_id="c1", project_id="p1", run_id="r1", user_id="u1"
        )

        assert result == [
            {"op": "add", "category": "interest", "content": "커피 선호"},
            {
                "op": "update",
                "id": 5,
                "category": "preference",
                "content": "갱신된 사실",
                "expected_state_fingerprint": me.ms.memory_state_fingerprint(
                    "기존 사실",
                    category="general",
                    status="active",
                    is_active=True,
                ),
            },
            {
                "op": "delete",
                "id": 6,
                "expected_state_fingerprint": me.ms.memory_state_fingerprint(
                    "낡은 습관",
                    category="habit",
                    status="active",
                    is_active=True,
                ),
            },
        ]

    async def test_transient_provider_failure_is_retryable(self, monkeypatch):
        async def fake_resolve():
            return {
                "model_name": "tiny",
                "provider_name": "p",
                "provider_type": "openai",
                "api_base": None,
                "api_key": "k",
            }

        async def fake_list_messages_for_run(*_args, **_kwargs):
            return [{"role": "user", "content": "계속 기억해"}]

        async def fake_list_memories(**_kwargs):
            return []

        async def failing_completion(*_args, **_kwargs):
            raise RuntimeError("temporary provider outage")

        monkeypatch.setattr(me.ps, "resolve_memory_model", fake_resolve)
        monkeypatch.setattr(me.cs, "list_messages_for_run", fake_list_messages_for_run)
        monkeypatch.setattr(me.ms, "list_memories", fake_list_memories)
        monkeypatch.setattr(me.litellm_client, "acompletion", failing_completion)

        with pytest.raises(me.MemoryExtractionRetryable):
            await me.generate_memory_if_applicable(conversation_id="c1", project_id="p1", run_id="r1", user_id="u1")

    def test_normalize_ops_rejects_unknown_categories_and_duplicate_targets(self):
        existing = {
            5: {
                "content": "현재 습관",
                "category": "general",
                "status": "active",
                "is_active": True,
            }
        }
        expected_state_fingerprint = me.ms.memory_state_fingerprint(
            "현재 습관",
            category="general",
            status="active",
            is_active=True,
        )

        assert me._normalize_ops(
            [
                {"op": "add", "category": "unknown", "content": "제외"},
                {"op": "add", "category": "interest", "content": "커피 선호"},
                {"op": "add", "category": "habit", "content": "커피 선호"},
                {"op": "update", "id": 5, "category": "habit", "content": "첫 갱신"},
                {"op": "delete", "id": 5},
                {"op": "delete", "id": 99},
            ],
            existing,
        ) == [
            {"op": "add", "category": "interest", "content": "커피 선호"},
            {
                "op": "update",
                "id": 5,
                "category": "habit",
                "content": "첫 갱신",
                "expected_state_fingerprint": expected_state_fingerprint,
            },
        ]
