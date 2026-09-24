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
from lumen_plugin_api.contracts import Namespace, PluginError, PluginIdentity
from lumen_plugin_api.skills import SkillSnapshot

from lumen.models.chat_db import ChatSkill
from lumen.plugins import bindings as plugin_bindings
from lumen.plugins import skills_host
from lumen.services import extensions_store as es
from lumen.services.chat_admission import _apply_context

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


_BINDING_ID = "2a2b2c2d-2222-4222-8222-22222222abcd"


class TestSkillsExtensionAccessHost:
    """The core host dispatches int refs to the DB store and UUID refs to plugin
    bindings, stays fail-closed for any non-``skill`` kind, and its own
    ``revalidate`` detects drift via a full-content fingerprint."""

    def _namespace(self, **kw):
        return Namespace(user_id=kw.pop("user_id", "u1"), project_id=kw.pop("project_id", "p1"), **kw)

    async def test_wrong_kind_is_fail_closed(self):
        host = skills_host.make_host()
        with pytest.raises(PluginError):
            await host.resolve("tool", 1, self._namespace())
        with pytest.raises(PluginError):
            await host.list("mcp", self._namespace())

    async def test_missing_project_scope_is_fail_closed(self):
        host = skills_host.make_host()
        with pytest.raises(PluginError):
            await host.resolve("skill", 1, Namespace(user_id="u1"))
        with pytest.raises(PluginError):
            await host.resolve("skill", _BINDING_ID, Namespace(user_id="u1"))

    async def test_noncanonical_binding_ref_is_fail_closed(self, monkeypatch):
        async def unexpected(*args, **kwargs):
            raise AssertionError("invalid skill reference must not reach storage")

        monkeypatch.setattr(plugin_bindings, "resolve_binding", unexpected)
        host = skills_host.make_host()
        for ref in ("not-a-uuid", _BINDING_ID.upper()):
            with pytest.raises(PluginError) as exc:
                await host.resolve("skill", ref, self._namespace())
            assert exc.value.code == "plugin_unavailable"


    async def test_resolve_dispatches_by_ref_shape(self, monkeypatch):
        host = skills_host.make_host()
        db_item = {"id": 7, "scope": "user", "name": "A", "description": None, "instructions": "do A", "is_active": True}
        binding_item = {"id": _BINDING_ID, "kind": "skill", "config": {"name": "B", "instructions": "do B"}, "config_version": 1, "is_active": True}

        async def fake_list_for_user(kind, *, user_id, project_id, active_only=False, reveal_secrets=False):
            assert kind == "skill"
            return [db_item]

        async def fake_resolve_binding(identifier, *, kind, namespace):
            assert kind == "skill"
            if identifier == binding_item["id"]:
                return binding_item
            raise PluginError("plugin_authority_revoked")

        monkeypatch.setattr(es, "list_for_user", fake_list_for_user)
        monkeypatch.setattr(plugin_bindings, "resolve_binding", fake_resolve_binding)

        ns = self._namespace()
        assert await host.resolve("skill", 7, ns) == db_item
        assert await host.resolve("skill", _BINDING_ID, ns) == binding_item
        with pytest.raises(PluginError):
            await host.resolve("skill", 999, ns)

    async def test_host_revalidate_fails_closed_on_drift(self, monkeypatch):
        host = skills_host.make_host()
        item = {"id": 7, "scope": "user", "name": "A", "description": None, "instructions": "do A", "is_active": True}

        async def fake_list_for_user(kind, *, user_id, project_id, active_only=False, reveal_secrets=False):
            return [item]

        monkeypatch.setattr(es, "list_for_user", fake_list_for_user)

        from lumen.plugins.registry import fingerprint as registry_fingerprint

        ns = self._namespace()
        current_fingerprint = registry_fingerprint(item)
        await host.revalidate("skill", 7, current_fingerprint, ns)  # no raise
        with pytest.raises(PluginError):
            await host.revalidate("skill", 7, "0" * 64, ns)


async def test_selected_third_party_skill_does_not_require_default_provider(monkeypatch):
    from lumen.plugins import registry

    binding = {"id": _BINDING_ID, "plugin_id": "replacement-skills"}
    saved_binding = {"binding": binding}

    class ReplacementSkills:
        manifest = SimpleNamespace(id="replacement-skills")

        async def resolve(self, refs, access):
            assert len(refs) == 1 and refs[0].binding_id == _BINDING_ID
            assert (access.namespace.user_id, access.namespace.project_id) == ("u1", "p1")
            return (SkillSnapshot(
                reference=refs[0],
                identity=PluginIdentity(plugin_id="replacement-skills", version="1", config_fingerprint="a" * 64),
                version="1", content_digest="a" * 64, instruction="Use replacement skill",
            ),)

    def selected_plugin(kind, plugin_id):
        assert (kind, plugin_id) == ("skills", "replacement-skills")
        return ReplacementSkills()

    async def revalidate(snapshot, *, namespace):
        assert snapshot is saved_binding
        assert (namespace.user_id, namespace.project_id) == ("u1", "p1")

    monkeypatch.setattr(registry, "get_plugin", selected_plugin)
    monkeypatch.setattr(plugin_bindings, "revalidate", revalidate)
    instructions, frozen = await skills_host.resolve_skills(
        [], [saved_binding], user_id="u1", project_id="p1"
    )
    assert instructions == ["Use replacement skill"]
    assert frozen[0]["provider_id"] == "replacement-skills"
    assert frozen[0]["binding"] is saved_binding
