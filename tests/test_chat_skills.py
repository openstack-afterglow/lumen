"""채팅 스킬(Claude Agent Skills 계열) — store 암호화/검증 + system 프리앰블 주입 테스트.

검증 대상:
- instructions 암호화 저장(_apply_fields) + 조회 복호화(_public_skill).
- 빈 지침 / 과길이 지침 거부.
- 주입 순서: 메모리 → 워크스페이스 → 스킬 → 에이전트(구체적일수록 뒤).
- 스킬은 opt-in — 미선택 시 주입 없음.

crypto 마스터키를 patch 해 DB 없이 순수 로직만 검증한다.
"""

from types import SimpleNamespace

import pytest

from lumen.api.completions import _apply_context
from lumen.models.chat_db import ChatSkill
from lumen.services import extensions_store as es

_VALID_KEY_HEX = "a" * 64


@pytest.fixture(autouse=True)
def _crypto_key(monkeypatch):
    monkeypatch.setattr(
        "lumen.crypto.get_settings",
        lambda: SimpleNamespace(get_lumen_encryption_key=_VALID_KEY_HEX),
    )


def _skill_row(**kw) -> ChatSkill:
    row = ChatSkill()
    row.id = kw.pop("id", 1)
    row.scope = kw.pop("scope", "user")
    row.name = kw.pop("name", "코드리뷰")
    row.description = kw.pop("description", None)
    row.instructions = kw.pop("instructions", "")
    row.is_active = kw.pop("is_active", True)
    row.created_at = None
    return row


class TestSkillStore:
    def test_instructions_encrypted_at_rest(self):
        row = _skill_row()
        es._apply_fields("skill", row, {"name": "s", "instructions": "항상 한국어로 답하라"})
        assert row.instructions  # 암호문 존재
        assert "한국어" not in row.instructions  # 평문 유출 없음

    def test_public_decrypts_instructions(self):
        row = _skill_row()
        es._apply_fields("skill", row, {"instructions": "테스트 지침"})
        pub = es._public_skill(row)
        assert pub["instructions"] == "테스트 지침"
        assert pub["name"] == "코드리뷰"

    def test_empty_instructions_rejected(self):
        row = _skill_row()
        with pytest.raises(es.ExtensionValidationError):
            es._apply_fields("skill", row, {"instructions": "   "})

    def test_overlong_instructions_rejected(self):
        row = _skill_row()
        with pytest.raises(es.ExtensionValidationError):
            es._apply_fields("skill", row, {"instructions": "x" * 20001})

    def test_description_optional(self):
        row = _skill_row()
        es._apply_fields("skill", row, {"instructions": "i", "description": "설명"})
        assert es._public_skill(row)["description"] == "설명"


class TestSkillInjectionOrder:
    """_apply_context 가 스킬 지침을 워크스페이스 뒤, 에이전트 앞에 배치하는지."""

    def test_skill_between_workspace_and_agent(self):
        msgs, _, _ = _apply_context(
            {"instructions": "AGENT"},
            "WORKSPACE",
            ["mem1"],
            [{"role": "user", "content": "hi"}],
            None,
            None,
            skill_instructions=["SKILL-A", "SKILL-B"],
        )
        systems = [m["content"] for m in msgs if m["role"] == "system"]
        assert systems == [
            "사용자에 대해 기억할 사실:\n- mem1",
            "WORKSPACE",
            "SKILL-A",
            "SKILL-B",
            "AGENT",
        ]

    def test_no_skills_means_no_skill_messages(self):
        msgs, _, _ = _apply_context(
            None, None, [], [{"role": "user", "content": "hi"}], None, None, skill_instructions=[]
        )
        assert [m for m in msgs if m["role"] == "system"] == []
