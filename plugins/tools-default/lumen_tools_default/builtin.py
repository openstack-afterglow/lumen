"""Builtin read-only conversation tools, executed only through ``ConversationAccess``.

Ownership never comes from model-supplied arguments: every handler receives only the
authenticated ``ExecutionContext`` and delegates identity/scope to the host. A missing,
forbidden, or unavailable conversation always degrades to a safe string; handlers never
propagate host exceptions to the model.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from lumen_plugin_api.contracts import ExecutionContext, PluginExport
from lumen_plugin_api.hosts import ConversationAccess
from lumen_plugin_api.tools import ToolExecutionResult, ToolTextPart

LIST_CONVERSATIONS_KEY = "list_my_conversations"
GET_CONVERSATION_DETAIL_KEY = "get_conversation_detail"


@dataclass(frozen=True)
class BuiltinTool:
    export: PluginExport
    input_schema: dict[str, Any]
    handler: Callable[[dict[str, Any], ExecutionContext, ConversationAccess], Awaitable[str]]


async def _list_my_conversations(arguments: dict[str, Any], context: ExecutionContext, conversations: ConversationAccess) -> str:
    try:
        items = await conversations.list(context, limit=20)
    except RuntimeError:
        return "저장소를 일시적으로 사용할 수 없습니다."
    if not items:
        return "현재 대화가 없습니다."
    lines = [f"- {item.get('title') or '(제목 없음)'} (id: {item['id']}, 모델: {item.get('model_name') or '-'})" for item in items]
    return "현재 사용자의 대화 목록:\n" + "\n".join(lines)


async def _get_conversation_detail(arguments: dict[str, Any], context: ExecutionContext, conversations: ConversationAccess) -> str:
    conversation_id = arguments.get("conversation_id")
    if not conversation_id or not isinstance(conversation_id, str):
        return "conversation_id(문자열)가 필요합니다."
    try:
        detail = await conversations.read(conversation_id, context)
    except PermissionError:
        return "해당 대화에 접근할 권한이 없습니다."
    except LookupError:
        return "대화를 찾을 수 없습니다."
    except RuntimeError:
        return "저장소를 일시적으로 사용할 수 없습니다."
    title = detail.get("title") or "(제목 없음)"
    message_count = detail.get("message_count", 0)
    model_name = detail.get("model_name") or "-"
    return f"대화 '{title}' — 메시지 {message_count}개, 모델 {model_name}."


BUILTIN_TOOLS: tuple[BuiltinTool, ...] = (
    BuiltinTool(
        export=PluginExport(key=LIST_CONVERSATIONS_KEY, kind="tool", name="List my conversations"),
        input_schema={"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        handler=_list_my_conversations,
    ),
    BuiltinTool(
        export=PluginExport(key=GET_CONVERSATION_DETAIL_KEY, kind="tool", name="Get conversation detail"),
        input_schema={
            "type": "object",
            "properties": {"conversation_id": {"type": "string", "description": "조회할 대화의 ID"}},
            "required": ["conversation_id"],
            "additionalProperties": False,
        },
        handler=_get_conversation_detail,
    ),
)

_BY_KEY: dict[str, BuiltinTool] = {tool.export.key: tool for tool in BUILTIN_TOOLS}


def builtin_tool(export_key: str) -> BuiltinTool | None:
    return _BY_KEY.get(export_key)


async def execute_builtin(
    tool: BuiltinTool, arguments: dict[str, Any], context: ExecutionContext, conversations: ConversationAccess
) -> ToolExecutionResult:
    try:
        text = await tool.handler(arguments, context, conversations)
    except Exception:
        return ToolExecutionResult(
            status="failed",
            model_content="Tool execution failed.",
            error_code="tool_execution_failed",
        )
    return ToolExecutionResult(
        status="completed",
        model_content=text[:8_192],
        display=[ToolTextPart(text=text)] if text else [],
    )
