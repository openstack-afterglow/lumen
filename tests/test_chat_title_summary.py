"""대화 제목 자동 요약 단위 테스트 (DB 불요).

검증:
- 제목이 이미 있으면 요약 호출 안 함(중복 방지)
- 요약 모델 미지정(resolve_title_model=None) 시 skip
- 정상 경로: 요약 저장 + 시스템 과금(source="system", charge_wallet=False → 사용자 지갑 미차감)
"""

from decimal import Decimal
from types import SimpleNamespace

import pytest

from lumen.services import title_summary as ts

pytestmark = pytest.mark.asyncio


def _resp(text: str):
    """litellm acompletion 비스트리밍 응답 모의."""
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
        usage=SimpleNamespace(prompt_tokens=12, completion_tokens=4),
    )


class TestGenerateTitle:
    async def test_skips_when_title_exists(self, monkeypatch):
        called = {"acompletion": False}

        async def fake_get(conv_id, **kw):
            return {"id": conv_id, "title": "이미 있는 제목"}

        async def fake_acompletion(*a, **k):
            called["acompletion"] = True
            return _resp("새 제목")

        monkeypatch.setattr(ts.cs, "get_conversation", fake_get)
        monkeypatch.setattr(ts.litellm_client, "acompletion", fake_acompletion)

        await ts.generate_title_if_absent(conversation_id="c1", project_id="p1", user_id="u1")
        assert called["acompletion"] is False

    async def test_skips_when_no_title_model(self, monkeypatch):
        called = {"acompletion": False}

        async def fake_get(conv_id, **kw):
            return {"id": conv_id, "title": None}

        async def fake_resolve():
            return None  # 요약 모델 미지정

        async def fake_acompletion(*a, **k):
            called["acompletion"] = True
            return _resp("x")

        monkeypatch.setattr(ts.cs, "get_conversation", fake_get)
        monkeypatch.setattr(ts.ps, "resolve_title_model", fake_resolve)
        monkeypatch.setattr(ts.litellm_client, "acompletion", fake_acompletion)

        await ts.generate_title_if_absent(conversation_id="c1", project_id="p1", user_id="u1")
        assert called["acompletion"] is False

    async def test_generates_and_charges_system(self, monkeypatch):
        saved = {}
        charged = {}

        async def fake_get(conv_id, **kw):
            return {"id": conv_id, "title": None}

        async def fake_resolve():
            return {
                "model_name": "gpt-4o-mini",
                "provider_name": "openai",
                "provider_type": "openai",
                "api_base": None,
                "api_key": "sk-x",
                "margin_multiplier": Decimal("1.0"),
                "input_price_per_token": Decimal("0.000002"),
                "output_price_per_token": Decimal("0.000008"),
                "price_source": "manual",
            }

        async def fake_list(conv_id, **kw):
            return [
                {"role": "user", "content": "쿠버네티스 파드가 안 뜨는데 도와줘"},
                {"role": "assistant", "content": "로그를 먼저 확인해 봅시다"},
            ]

        async def fake_acompletion(*a, **k):
            return _resp('"파드 기동 문제"\n')  # 따옴표/개행 포함 → _clean 정리 확인

        async def fake_update_title(conv_id, **kw):
            saved["title"] = kw.get("title")
            saved["user_id"] = kw.get("user_id")
            return {"id": conv_id}

        async def fake_apply_usage(**kw):
            charged.update(kw)
            return 0

        monkeypatch.setattr(ts.cs, "get_conversation", fake_get)
        monkeypatch.setattr(ts.ps, "resolve_title_model", fake_resolve)
        monkeypatch.setattr(ts.cs, "list_messages", fake_list)
        monkeypatch.setattr(ts.litellm_client, "acompletion", fake_acompletion)
        monkeypatch.setattr(ts.cs, "update_title", fake_update_title)
        monkeypatch.setattr(ts.credit, "apply_usage", fake_apply_usage)

        await ts.generate_title_if_absent(conversation_id="c1", project_id="p1", user_id="u1")

        # 제목 정리(따옴표/개행 제거) 후 저장, 소유자 컨텍스트 전달
        assert saved["title"] == "파드 기동 문제"
        assert saved["user_id"] == "u1"
        # 시스템 부담 — 사용자 지갑 미차감 + source=system 원장
        assert charged["source"] == "system"
        assert charged["charge_wallet"] is False
        assert charged["user_id"] == "u1"
        assert charged["model_name"] == "gpt-4o-mini"
        assert charged["event_id"] == "title:c1"
