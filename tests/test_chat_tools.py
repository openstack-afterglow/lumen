"""빌트인 AI 채팅 내부 툴 테넌트 안전 계약 테스트.

핵심 보안 불변식:
- project_id/user_id 는 ToolContext 에서만 — LLM 이 인자로 주입해도 무시된다.
- 타 프로젝트 리소스 식별자 → 소유권 재검증 실패 → 데이터 대신 거부 문자열.
- 미등록 툴/핸들러 예외 → 항상 안전한 문자열(예외 미전파).
"""

from lumen.services import conversation_store as cs
from lumen.services import tool_runtime
from lumen.services.tools import ToolContext, execute_tool, tool_schemas

_CTX = ToolContext(project_id="proj-A", user_id="user-A")


class TestSchemas:
    def test_tool_schemas_openai_format(self):
        schemas = tool_schemas()
        assert len(schemas) >= 2
        names = {s["function"]["name"] for s in schemas}
        assert "list_my_conversations" in names
        assert "get_conversation_detail" in names
        for s in schemas:
            assert s["type"] == "function"
            assert "parameters" in s["function"]

    async def test_run_policy_can_disable_all_tool_schemas_and_execution(self):
        disabled = ToolContext(project_id="proj-A", user_id="user-A", tools_enabled=False)
        assert await tool_runtime.context_tool_schemas(disabled) == []
        assert "disabled" in await tool_runtime.context_execute("list_my_conversations", {}, disabled)


class TestUnknownAndErrors:
    async def test_unknown_tool_safe_rejection(self):
        out = await execute_tool("no_such_tool", {}, _CTX)
        assert "알 수 없는" in out

    async def test_handler_exception_returns_safe_string(self, monkeypatch):
        async def boom(**kwargs):
            raise RuntimeError("internal boom")

        monkeypatch.setattr(cs, "list_conversations", boom)
        out = await execute_tool("list_my_conversations", {}, _CTX)
        assert "오류" in out
        assert "boom" not in out  # 내부 예외 메시지 미노출


class TestTenantSafety:
    async def test_scopes_by_context_not_llm_args(self, monkeypatch):
        captured = {}

        async def fake_list(**kwargs):
            captured.update(kwargs)
            return [{"id": "c1", "title": "t", "model_name": "m"}]

        monkeypatch.setattr(cs, "list_conversations", fake_list)
        # ⚠️ LLM 이 다른 프로젝트/사용자를 인자로 주입 시도 → 반드시 무시되어야 한다.
        out = await execute_tool(
            "list_my_conversations",
            {"project_id": "EVIL-PROJECT", "user_id": "EVIL-USER"},
            _CTX,
        )
        assert captured["user_id"] == "user-A"
        assert captured["project_id"] == "proj-A"
        assert "t" in out

    async def test_foreign_conversation_forbidden(self, monkeypatch):
        async def fake_get(conv_id, **kwargs):
            raise cs.ConversationForbidden("타 프로젝트")

        monkeypatch.setattr(cs, "get_conversation", fake_get)
        out = await execute_tool("get_conversation_detail", {"conversation_id": "other-conv"}, _CTX)
        assert "권한" in out

    async def test_conversation_detail_passes_context_ownership(self, monkeypatch):
        captured = {}

        async def fake_get(conv_id, **kwargs):
            captured["conv_id"] = conv_id
            captured.update(kwargs)
            return {"id": conv_id, "title": "제목", "model_name": "gpt-4o"}

        async def fake_msgs(conv_id, **kwargs):
            return [{"role": "user", "content": "hi"}]

        monkeypatch.setattr(cs, "get_conversation", fake_get)
        monkeypatch.setattr(cs, "list_messages", fake_msgs)
        out = await execute_tool("get_conversation_detail", {"conversation_id": "c1"}, _CTX)
        assert captured["user_id"] == "user-A"
        assert captured["project_id"] == "proj-A"
        assert "메시지 1개" in out

    async def test_missing_required_arg(self):
        out = await execute_tool("get_conversation_detail", {}, _CTX)
        assert "conversation_id" in out
