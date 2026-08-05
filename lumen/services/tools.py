"""빌트인 AI 채팅 내부 플랫폼 툴 — 테넌트 안전 실행.

⚠️ 보안 계약(변경 시 반드시 회귀 테스트):
1. project_id/user_id 는 **LLM 이 준 인자가 아니라 실행 컨텍스트(ToolContext)에서만** 온다.
   LLM 이 인자로 project_id 등을 넣어도 무시한다(화이트리스트 밖).
2. LLM 이 생성하는 인자는 각 툴 스키마의 properties 로 화이트리스트 필터링된다.
3. 리소스 식별자를 받는 툴은 컨텍스트로 **소유권을 재검증**한다(IDOR 방어) — 실패 시 데이터 대신
   안전한 거부 문자열을 반환한다.
4. 노출 툴은 명시 레지스트리(TOOLS)로만 관리한다 — 임의 내부 함수 자동 노출 없음.
5. 툴은 예외를 밖으로 던지지 않는다 — 항상 사용자/LLM 에게 안전한 문자열을 반환(내부정보 미노출).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToolContext:
    """툴 실행 컨텍스트 — 요청 token_info 에서만 채워진다(LLM 입력 아님).

    selected_tool_ids/selected_mcp_ids: 대화별 tool/MCP 선택. None=활성 전체(하위호환),
    []=없음, [id...]=해당 항목만 노출. 커스텀 tool·MCP 서버(extensions_store id) 기준.
    """

    project_id: str
    user_id: str
    tools_enabled: bool = True
    selected_tool_ids: tuple[int, ...] | None = None
    selected_mcp_ids: tuple[int, ...] | None = None
    expected_mcp_credential_versions: tuple[tuple[int, int], ...] | None = None
    expected_extension_fingerprints: tuple[tuple[str, int, str], ...] | None = None
    run_id: str | None = None
    tool_call_id: str | None = None
    lumen_snapshot: dict[str, object] | None = None
    lumen_snapshot_frozen: bool = False

    execution_hooks: object | None = None
    managed_search: dict[str, Any] | None = None
    managed_fetch: dict[str, Any] | None = None
    managed_advisor: dict[str, Any] | None = None
    advisor_visible_messages: tuple[dict[str, Any], ...] = ()
    binding_session: object | None = None


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict  # JSON schema (LLM 이 제공 가능한 인자만 기술)
    handler: Callable[[dict, ToolContext], Awaitable[str]]

    def openai_schema(self) -> dict:
        return {
            "type": "function",
            "function": {"name": self.name, "description": self.description, "parameters": self.parameters},
        }


# ---------------------------------------------------------------------------
# 툴 핸들러 — 모두 ctx(project_id/user_id)로만 스코프를 잡는다.
# ---------------------------------------------------------------------------
async def _list_my_conversations(args: dict, ctx: ToolContext) -> str:
    from lumen.services import conversation_store as cs

    try:
        convs = await cs.list_conversations(user_id=ctx.user_id, project_id=ctx.project_id, limit=20)
    except cs.ChatStorageUnavailable:
        return "저장소를 일시적으로 사용할 수 없습니다."
    if not convs:
        return "현재 대화가 없습니다."
    lines = [f"- {c.get('title') or '(제목 없음)'} (id: {c['id']}, 모델: {c.get('model_name') or '-'})" for c in convs]
    return "현재 사용자의 대화 목록:\n" + "\n".join(lines)


async def _get_conversation_detail(args: dict, ctx: ToolContext) -> str:
    from lumen.services import conversation_store as cs

    conv_id = args.get("conversation_id")
    if not conv_id or not isinstance(conv_id, str):
        return "conversation_id(문자열)가 필요합니다."
    try:
        # 소유권 재검증 — 타 사용자 대화면 Forbidden(프로젝트 무관, user_id 기준).
        conv = await cs.get_conversation(conv_id, user_id=ctx.user_id, project_id=ctx.project_id)
        msgs = await cs.list_messages(conv_id, user_id=ctx.user_id, project_id=ctx.project_id)
    except cs.ConversationForbidden:
        return "해당 대화에 접근할 권한이 없습니다."
    except cs.ConversationNotFound:
        return "대화를 찾을 수 없습니다."
    except cs.ChatStorageUnavailable:
        return "저장소를 일시적으로 사용할 수 없습니다."
    return f"대화 '{conv.get('title') or '(제목 없음)'}' — 메시지 {len(msgs)}개, 모델 {conv.get('model_name') or '-'}."


# ---------------------------------------------------------------------------
# 레지스트리 — 노출 툴 명시 목록
# ---------------------------------------------------------------------------
TOOLS: list[Tool] = [
    Tool(
        name="list_my_conversations",
        description="현재 사용자의 채팅 대화 목록(제목·모델)을 반환한다.",
        parameters={"type": "object", "properties": {}, "required": []},
        handler=_list_my_conversations,
    ),
    Tool(
        name="get_conversation_detail",
        description="특정 대화의 요약(메시지 수·모델)을 반환한다.",
        parameters={
            "type": "object",
            "properties": {"conversation_id": {"type": "string", "description": "조회할 대화의 ID"}},
            "required": ["conversation_id"],
        },
        handler=_get_conversation_detail,
    ),
]

_TOOL_BY_NAME: dict[str, Tool] = {t.name: t for t in TOOLS}


def tool_schemas() -> list[dict]:
    """litellm 요청에 전달할 OpenAI function 스키마 목록."""
    return [t.openai_schema() for t in TOOLS]


async def execute_tool(name: str, args: dict, ctx: ToolContext) -> str:
    """이름으로 툴을 찾아 테넌트 안전하게 실행. 항상 문자열 반환(예외 미전파).

    - 미등록 툴 → 거부 문자열.
    - 스키마 밖 인자(project_id 등) → 화이트리스트로 제거(LLM 이 주입 못 함).
    """
    tool = _TOOL_BY_NAME.get(name)
    if tool is None:
        return f"알 수 없는 툴입니다: {name}"
    if not isinstance(args, dict):
        args = {}
    allowed = set(tool.parameters.get("properties", {}).keys())
    safe_args = {k: v for k, v in args.items() if k in allowed}
    try:
        return await tool.handler(safe_args, ctx)
    except Exception:
        logger.warning("툴 실행 실패 name=%s", name, exc_info=True)
        return "툴 실행 중 오류가 발생했습니다."
