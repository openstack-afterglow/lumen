"""빌트인 AI 채팅 대화/메시지 저장소 (MySQL chat_conversations / chat_messages).

⚠️ IDOR 방어: 모든 조회/수정 경로에서 대화의 user_id 가 호출자 token_info 의 user_id 와
일치하는지 검증한다(프로젝트 무관 — 한 사용자의 대화는 프로젝트가 달라도 본인 것).
타 사용자 대화 접근은 ConversationForbidden(403), 미존재는 ConversationNotFound(404).
project_id 는 생성 시점 활성 프로젝트를 메타로 보존할 뿐 소유권 판정에는 쓰지 않는다.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC

from sqlalchemy import literal, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import aliased

from lumen.crypto import decrypt_chat_content, encrypt_chat_content
from lumen.db import get_session_factory, is_db_available, mark_db_unhealthy
from lumen.models.chat_db import ChatConversation, ChatMessage, ChatWorkspace
from lumen.models.chat_runs import ChatRun, ChatRunSegment, ChatRunTurn
from lumen.services.message_parts import deserialize_parts, serialize_parts
from lumen.services.message_timestamps import message_timestamps
from lumen.services.run_store import replay_events

logger = logging.getLogger(__name__)


def _enc(value: str | None) -> str | None:
    """평문 → chat_content 암호문. None/빈 문자열은 그대로."""
    return encrypt_chat_content(value) if value else value


def _dec(value: str | None) -> str | None:
    """암호문 → 평문(prefix 없으면 평문 passthrough). None 안전."""
    return decrypt_chat_content(value) if value else value


def _enc_json(value: list | dict | None) -> str | None:
    """tool_calls(JSON 직렬화) → 암호문. None 은 그대로."""
    if value is None:
        return None
    return encrypt_chat_content(json.dumps(value, ensure_ascii=False))


def _dec_json(value: str | None) -> list | dict | None:
    """암호문 → JSON 파싱. None/파싱 실패는 None."""
    if not value:
        return None
    try:
        return json.loads(decrypt_chat_content(value))
    except (json.JSONDecodeError, ValueError, TypeError):
        logger.warning("tool_calls 복호화/파싱 실패")
        return None


class ChatStorageUnavailable(RuntimeError):
    """chat DB 미구성/장애 — fail-closed(503)."""


class ConversationNotFound(LookupError):
    """대화 미존재 — 404."""


class ConversationForbidden(PermissionError):
    """대화 소유자 불일치(타 프로젝트/타 사용자) — 403."""


class WorkspaceNotFound(LookupError):
    """대상 프로젝트 미존재 — 404."""


class WorkspaceForbidden(PermissionError):
    """대상 프로젝트 소유자 불일치 — 403."""


def _require_db():
    if not is_db_available():
        raise ChatStorageUnavailable("chat DB 를 사용할 수 없습니다")
    factory = get_session_factory()
    if factory is None:
        raise ChatStorageUnavailable("chat DB 가 구성되지 않았습니다")
    return factory


def _iso(dt) -> str | None:
    if dt is None:
        return None
    utc = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
    return utc.isoformat().replace("+00:00", "Z")


def _local_iso(dt) -> str | None:
    return dt.isoformat() if dt is not None else None


def _conv_public(row: ChatConversation) -> dict:
    return {
        "id": row.id,
        "project_id": row.project_id,
        "user_id": row.user_id,
        "title": _dec(row.title),
        "model_name": row.model_name,
        "workspace_id": row.workspace_id,
        "active_leaf_id": row.active_leaf_id,
        "parent_conversation_id": row.parent_conversation_id,
        "forked_from_message_id": row.forked_from_message_id,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def _msg_public(row: ChatMessage, *, execution: dict | None = None) -> dict:
    parts = (
        [part.model_dump(by_alias=True, exclude_none=True) for part in deserialize_parts(row.parts)]
        if row.parts is not None
        else None
    )
    citations = [part for part in parts or [] if part["type"] == "citation"] or _dec_json(row.citations)
    return {
        "id": row.id,
        "conversation_id": row.conversation_id,
        "role": row.role,
        "parent_id": row.parent_id,
        "content": _dec(row.content),
        "tool_calls": _dec_json(row.tool_calls),
        "citations": citations,
        "reasoning": _dec(row.reasoning),
        "attachments": _dec_json(row.attachments),
        "parts": parts,
        "status": row.status,
        "token_prompt": row.token_prompt,
        "token_completion": row.token_completion,
        "execution": execution,
        "model_name": row.model_name,
        "created_at": _iso(row.created_at),
        "created_at_local": _local_iso(row.created_at_local),
        "created_timezone": row.created_timezone,
    }


async def _load_owned(session, conv_id: str, user_id: str, project_id: str) -> ChatConversation:
    """Load a conversation only when both the user and requested project own it."""
    row = await session.get(ChatConversation, conv_id)
    if row is None:
        raise ConversationNotFound(f"대화 {conv_id} 를 찾을 수 없습니다")
    if row.user_id != user_id or row.project_id != project_id:
        raise ConversationForbidden("대화에 접근할 권한이 없습니다")
    return row


async def _load_owned_workspace(session, workspace_id: int, user_id: str) -> ChatWorkspace:
    """Validate the selected workspace in the caller's transaction."""
    row = await session.get(ChatWorkspace, workspace_id)
    if row is None:
        raise WorkspaceNotFound(f"프로젝트 {workspace_id} 를 찾을 수 없습니다")
    if row.owner_user_id != user_id:
        raise WorkspaceForbidden("프로젝트에 접근할 권한이 없습니다")
    return row


async def create_conversation(
    *, project_id: str, user_id: str, title: str | None, model_name: str | None, workspace_id: int | None = None
) -> dict:
    factory = _require_db()
    row = ChatConversation(
        id=str(uuid.uuid4()),
        project_id=project_id,
        user_id=user_id,
        title=_enc(title or None),
        model_name=(model_name or None),
        workspace_id=workspace_id,
    )
    try:
        async with factory() as session, session.begin():
            if workspace_id is not None:
                await _load_owned_workspace(session, workspace_id, user_id)
            session.add(row)
            await session.flush()
            return _conv_public(row)
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc


async def list_conversations(*, user_id: str, project_id: str, limit: int = 50, offset: int = 0) -> list[dict]:
    """Project-and-user scoped conversation list."""
    factory = _require_db()
    try:
        async with factory() as session:
            stmt = (
                select(ChatConversation)
                .where(ChatConversation.user_id == user_id, ChatConversation.project_id == project_id)
                .order_by(ChatConversation.updated_at.desc())
                .limit(min(limit, 200))
                .offset(max(offset, 0))
            )
            rows = (await session.execute(stmt)).scalars().all()
            return [_conv_public(r) for r in rows]
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc


async def search_conversations(*, user_id: str, project_id: str, query: str, limit: int = 20) -> list[dict]:
    """Search decrypted titles only after owner/project scoping in the database."""
    normalized_query = query.strip().casefold()
    factory = _require_db()
    try:
        async with factory() as session:
            rows = (
                await session.execute(
                    select(ChatConversation)
                    .where(ChatConversation.user_id == user_id, ChatConversation.project_id == project_id)
                    .order_by(ChatConversation.updated_at.desc())
                )
            ).scalars()
            results: list[dict] = []
            for row in rows:
                public = _conv_public(row)
                title = (public.get("title") or "새 대화").casefold()
                if normalized_query and normalized_query not in title:
                    continue
                results.append(public)
                if len(results) >= min(max(limit, 1), 50):
                    break
            return results
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc


async def get_conversation(conv_id: str, *, user_id: str, project_id: str) -> dict:
    factory = _require_db()
    try:
        async with factory() as session:
            row = await _load_owned(session, conv_id, user_id, project_id)
            return _conv_public(row)
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc


async def delete_conversation(conv_id: str, *, user_id: str, project_id: str) -> None:
    factory = _require_db()
    try:
        async with factory() as session, session.begin():
            row = await _load_owned(session, conv_id, user_id, project_id)
            await session.delete(row)
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc


async def update_title(conv_id: str, *, user_id: str, project_id: str, title: str | None) -> dict:
    """대화 제목 갱신(소유권 검증 + 암호화 저장). 제목 자동 요약 경로용."""
    factory = _require_db()
    try:
        async with factory() as session, session.begin():
            row = await _load_owned(session, conv_id, user_id, project_id)
            row.title = _enc(title or None)
            await session.flush()
            return _conv_public(row)
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc


async def set_workspace(conv_id: str, *, user_id: str, project_id: str, workspace_id: int | None) -> dict:
    """Assign or unassign a conversation after validating workspace ownership."""
    factory = _require_db()
    try:
        async with factory() as session, session.begin():
            row = await _load_owned(session, conv_id, user_id, project_id)
            if workspace_id is not None:
                await _load_owned_workspace(session, workspace_id, user_id)
            row.workspace_id = workspace_id
            await session.flush()
            return _conv_public(row)
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc


def _activity_category(source: object) -> str:
    return {
        "builtin": "기본 도구",
        "managed": "관리형 도구",
        "custom_http": "커스텀 도구",
        "mcp": "MCP",
        "catalog": "도구 준비",
        "workspace": "워크스페이스",
        "agent": "에이전트",
    }.get(source if isinstance(source, str) else "", "도구")


def _project_run_activity(events: list, *, durations_ms: dict[str, int]) -> list[dict]:
    """Project durable, display-safe reasoning and tool events in journal order."""
    activity: list[dict] = []
    by_id: dict[str, dict] = {}
    for event in events:
        payload = event.payload.model_dump(mode="json")
        if event.type == "part.delta" and payload.get("part_type") == "reasoning":
            key = f"reasoning:{payload['message_id']}:{payload['part_index']}"
            item = by_id.get(key)
            if item is None:
                item = {
                    "id": key,
                    "kind": "reasoning",
                    "seq": event.seq,
                    "createdAt": _iso(event.created_at),
                    "text": "",
                    "active": False,
                }
                by_id[key] = item
                activity.append(item)
            item["text"] += payload["delta"]
        elif event.type == "part.completed":
            part = payload.get("part")
            if not isinstance(part, dict) or part.get("type") != "reasoning" or part.get("visibility") != "user":
                continue
            key = f"reasoning:{payload['message_id']}:{payload['part_index']}"
            item = by_id.get(key)
            if item is None:
                item = {
                    "id": key,
                    "kind": "reasoning",
                    "seq": event.seq,
                    "createdAt": _iso(event.created_at),
                    "text": part.get("text", ""),
                    "active": False,
                }
                by_id[key] = item
                activity.append(item)
            else:
                item["text"] = part.get("text", item["text"])
        elif event.type == "tool.call.started":
            source = payload.get("source")
            item = {
                "id": f"tool:{payload['call_id']}",
                "kind": "tool",
                "seq": event.seq,
                "createdAt": _iso(event.created_at),
                "callId": payload["call_id"],
                "name": payload["name"],
                "source": source if isinstance(source, str) else "builtin",
                "category": payload["category"]
                if isinstance(payload.get("category"), str)
                else _activity_category(source),
                "arguments": payload["arguments"],
                "status": "running",
                "content": [],
                "errorCode": None,
                "durationMs": None,
            }
            by_id[item["id"]] = item
            activity.append(item)
        elif event.type == "tool.call.completed":
            item = by_id.get(f"tool:{payload['call_id']}")
            if item is None:
                source = payload.get("source")
                item = {
                    "id": f"tool:{payload['call_id']}",
                    "kind": "tool",
                    "seq": event.seq,
                    "createdAt": _iso(event.created_at),
                    "callId": payload["call_id"],
                    "name": payload["name"],
                    "source": source if isinstance(source, str) else "builtin",
                    "category": payload["category"]
                    if isinstance(payload.get("category"), str)
                    else _activity_category(source),
                    "arguments": {},
                    "status": "running",
                    "content": [],
                    "errorCode": None,
                    "durationMs": None,
                }
                by_id[item["id"]] = item
                activity.append(item)
            item["status"] = payload["status"]
            item["content"] = payload["content"]
            item["errorCode"] = payload.get("error_code")
            item["durationMs"] = durations_ms.get(payload["call_id"])
    return sorted(activity, key=lambda item: int(item["seq"]))


async def list_messages(
    conv_id: str, *, user_id: str, project_id: str, limit: int = 200, offset: int = 0
) -> list[dict]:
    factory = _require_db()
    try:
        async with factory() as session:
            await _load_owned(session, conv_id, user_id, project_id)  # 소유권 검증
            stmt = (
                select(ChatMessage)
                .where(ChatMessage.conversation_id == conv_id)
                .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
                .limit(min(limit, 500))
                .offset(max(offset, 0))
            )
            rows = (await session.execute(stmt)).scalars().all()
            message_ids = [row.id for row in rows]
            execution_by_message: dict[int, dict] = {}
            if message_ids:
                turn_rows = (
                    await session.execute(
                        select(ChatRunTurn, ChatRun)
                        .join(ChatRun, ChatRun.id == ChatRunTurn.run_id)
                        .where(ChatRunTurn.assistant_message_id.in_(message_ids))
                    )
                ).all()
                runs_by_id: dict[str, ChatRun] = {}
                turns_by_run: dict[str, list[ChatRunTurn]] = {}
                for turn, run in turn_rows:
                    if turn.assistant_message_id is None:
                        continue
                    runs_by_id[run.id] = run
                    turns_by_run.setdefault(run.id, []).append(turn)
                final_message_by_run: dict[str, int] = {}
                for run_id, turns in turns_by_run.items():
                    final_turn = max(turns, key=lambda turn: turn.ordinal)
                    final_message_id = int(final_turn.assistant_message_id)
                    final_message_by_run[run_id] = final_message_id
                    run = runs_by_id[run_id]
                    try:
                        request = json.loads(_dec(run.request_payload) or "{}")
                        skill_ids = request.get("skill_ids", [])
                        skill_snapshot = request.get("skill_snapshot", [])
                    except (TypeError, ValueError):
                        skill_ids = []
                        skill_snapshot = []
                    skills = [
                        {"id": item["id"], "name": item["name"]}
                        for item in skill_snapshot
                        if isinstance(item, dict)
                        and isinstance(item.get("id"), int)
                        and isinstance(item.get("name"), str)
                    ]
                    execution_by_message[final_message_id] = {
                        "run_id": run.id,
                        "agent_id": run.agent_id,
                        "skill_ids": skill_ids if isinstance(skill_ids, list) else [],
                        "skills": skills,
                    }
                if final_message_by_run:
                    segments = (
                        await session.execute(
                            select(ChatRunSegment).where(
                                ChatRunSegment.run_id.in_(set(final_message_by_run)),
                                ChatRunSegment.endpoint == "tool",
                                ChatRunSegment.call_id.is_not(None),
                            )
                        )
                    ).scalars()
                    durations_by_run: dict[str, dict[str, int]] = {}
                    for segment in segments:
                        if segment.call_id is None or segment.completed_at is None or segment.created_at is None:
                            continue
                        durations_by_run.setdefault(segment.run_id, {})[segment.call_id] = int(
                            max(0, (segment.completed_at - segment.created_at).total_seconds() * 1_000)
                        )
                    for run_id, final_message_id in final_message_by_run.items():
                        execution = execution_by_message[final_message_id]
                        durations = durations_by_run.get(run_id, {})
                        if durations:
                            execution["tool_durations_ms"] = durations
                        execution["activity"] = _project_run_activity(
                            await replay_events(session, runs_by_id[run_id], after_seq=0),
                            durations_ms=durations,
                        )
            return [_msg_public(row, execution=execution_by_message.get(row.id)) for row in rows]
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc


async def list_messages_for_run(
    run_id: str,
    *,
    user_id: str,
    project_id: str,
) -> list[dict]:
    """Load the exact persisted user/assistant messages for one owned run."""
    factory = _require_db()
    try:
        async with factory() as session:
            run = (
                await session.execute(
                    select(ChatRun).where(
                        ChatRun.id == run_id,
                        ChatRun.user_id == user_id,
                        ChatRun.project_id == project_id,
                        ChatRun.conversation_id.is_not(None),
                    )
                )
            ).scalar_one_or_none()
            if run is None or run.user_message_id is None:
                return []
            turn_message_ids = list(
                (
                    await session.execute(
                        select(ChatRunTurn.assistant_message_id)
                        .where(ChatRunTurn.run_id == run.id, ChatRunTurn.assistant_message_id.is_not(None))
                        .order_by(ChatRunTurn.ordinal)
                    )
                ).scalars()
            )
            message_ids = [run.user_message_id, *turn_message_ids]
            rows = (
                await session.execute(
                    select(ChatMessage).where(
                        ChatMessage.id.in_(message_ids),
                        ChatMessage.conversation_id == run.conversation_id,
                    )
                )
            ).scalars()
            by_id = {row.id: row for row in rows}
            return [_msg_public(by_id[message_id]) for message_id in message_ids if message_id in by_id]
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc


async def add_message(
    conv_id: str,
    *,
    role: str,
    content: str | None,
    tool_calls: list | None = None,
    citations: list | None = None,
    reasoning: str | None = None,
    attachments: list | None = None,
    parts: list[dict] | None = None,
    status: str = "complete",
    token_prompt: int = 0,
    token_completion: int = 0,
    parent_id: int | None = None,
    model_name: str | None = None,
    client_timezone: str | None = None,
    set_leaf: bool = False,
) -> dict:
    """메시지 추가(소유권은 호출부가 이미 검증했다고 가정 — 완료 경로 내부용).

    버전 트리: parent_id 로 부모를 잇는다(같은 parent = 재생성 형제). set_leaf=True 면 대화의
    active_leaf_id 를 이 메시지로 갱신한다(활성 경로의 새 리프). model_name 은 형제 버전 라벨.
    """
    factory = _require_db()
    created_at, created_at_local, created_timezone = message_timestamps(client_timezone)
    row = ChatMessage(
        conversation_id=conv_id,
        role=role,
        parent_id=parent_id,
        content=_enc(content),
        tool_calls=_enc_json(tool_calls),
        citations=_enc_json(citations),
        reasoning=_enc(reasoning),
        attachments=_enc_json(attachments),
        parts=serialize_parts(parts) if parts is not None else None,
        parts_version=1 if parts is not None else None,
        status=status,
        token_prompt=int(token_prompt),
        token_completion=int(token_completion),
        model_name=model_name,
        created_at=created_at,
        created_at_local=created_at_local,
        created_timezone=created_timezone,
    )
    try:
        async with factory() as session, session.begin():
            session.add(row)
            await session.flush()
            if set_leaf:
                conv = await session.get(ChatConversation, conv_id)
                if conv is not None:
                    conv.active_leaf_id = row.id
            return _msg_public(row)
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc


async def complete_message_in_transaction(
    session,
    conv_id: str,
    *,
    message_id: int,
    content: str,
    reasoning: str | None,
    model_name: str,
    canonical_parts: list[dict] | None = None,
    status: str = "complete",
    token_prompt: int = 0,
    token_completion: int = 0,
) -> None:
    """Finalize an assistant placeholder in the caller's transaction."""
    from lumen.services.message_parts import serialize_parts

    if status not in {"complete", "failed", "canceled"}:
        raise ValueError("assistant message status is invalid")
    parts = canonical_parts
    if parts is None:
        parts = []
        if content:
            parts.append({"type": "text", "text": content})
        if reasoning:
            parts.append({"type": "reasoning", "text": reasoning, "visibility": "user"})
    message = await session.get(ChatMessage, message_id)
    conv = await session.get(ChatConversation, conv_id)
    if message is None or message.conversation_id != conv_id or conv is None:
        raise ConversationNotFound(f"메시지 {message_id} 를 대화에서 찾을 수 없습니다")
    message.content = _enc(content)
    message.reasoning = _enc(reasoning)
    message.parts = serialize_parts(parts) if parts else None
    message.parts_version = 1 if parts else None
    message.status = status
    message.model_name = model_name
    message.token_prompt = token_prompt
    message.token_completion = token_completion
    conv.active_leaf_id = message.id


async def complete_message(
    conv_id: str,
    *,
    message_id: int,
    content: str,
    reasoning: str | None,
    model_name: str,
) -> None:
    """Finalize the worker-created assistant placeholder with canonical dual-write data."""
    factory = _require_db()
    try:
        async with factory() as session, session.begin():
            await complete_message_in_transaction(
                session,
                conv_id,
                message_id=message_id,
                content=content,
                reasoning=reasoning,
                model_name=model_name,
            )
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc


def _backtrack(rows: list[ChatMessage], leaf_id: int | None) -> list[ChatMessage]:
    """leaf_id 에서 parent_id 를 역추적한 선형 경로(루트→리프 오름차순). leaf 없으면 빈 경로."""
    by_id = {r.id: r for r in rows}
    path: list[ChatMessage] = []
    cur = by_id.get(leaf_id) if leaf_id else None
    seen: set[int] = set()
    while cur is not None and cur.id not in seen:
        seen.add(cur.id)
        path.append(cur)
        cur = by_id.get(cur.parent_id) if cur.parent_id else None
    path.reverse()
    return path


async def get_active_path(conv_id: str, *, user_id: str, project_id: str) -> dict:
    """active_leaf 에서 역추적한 활성 경로(모델 입력·재개용). {"messages":[오름차순], "active_leaf_id"}."""
    factory = _require_db()
    try:
        async with factory() as session:
            conv = await _load_owned(session, conv_id, user_id, project_id)
            rows = (
                (await session.execute(select(ChatMessage).where(ChatMessage.conversation_id == conv_id)))
                .scalars()
                .all()
            )
            path = _backtrack(list(rows), conv.active_leaf_id)
            return {"messages": [_msg_public(r) for r in path], "active_leaf_id": conv.active_leaf_id}
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc


async def path_ending_at(conv_id: str, *, user_id: str, project_id: str, message_id: int) -> list[dict]:
    """message_id 를 리프로 한 경로(루트→message_id 오름차순). 재생성 모델 입력용."""
    factory = _require_db()
    try:
        async with factory() as session:
            await _load_owned(session, conv_id, user_id, project_id)
            rows = (
                (await session.execute(select(ChatMessage).where(ChatMessage.conversation_id == conv_id)))
                .scalars()
                .all()
            )
            return [_msg_public(r) for r in _backtrack(list(rows), message_id)]
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc


async def list_message_tree(
    conv_id: str,
    *,
    user_id: str,
    project_id: str,
    before_id: int | None = None,
    limit: int = 40,
) -> dict:
    """Fetch one backwards page through the conversation index in a single normal-path query."""
    factory = _require_db()
    page_size = max(1, min(limit, 100))
    try:
        async with factory() as session:
            conversation = aliased(ChatConversation)
            parent = aliased(ChatMessage)
            seed_id = before_id if before_id is not None else conversation.active_leaf_id
            active_path = (
                select(
                    ChatMessage.id.label("id"),
                    ChatMessage.parent_id.label("parent_id"),
                    literal(0).label("depth"),
                )
                .join(conversation, ChatMessage.conversation_id == conversation.id)
                .where(
                    ChatMessage.id == seed_id,
                    conversation.id == conv_id,
                    conversation.user_id == user_id,
                    conversation.project_id == project_id,
                )
                .cte("active_path", recursive=True)
            )
            active_path = active_path.union_all(
                select(
                    parent.id.label("id"),
                    parent.parent_id.label("parent_id"),
                    (active_path.c.depth + 1).label("depth"),
                )
                .join(active_path, parent.id == active_path.c.parent_id)
                .where(active_path.c.depth < page_size)
            )
            rows = (
                await session.execute(
                    select(ChatMessage, conversation.active_leaf_id)
                    .join(active_path, ChatMessage.id == active_path.c.id)
                    .join(conversation, ChatMessage.conversation_id == conversation.id)
                    .where(
                        conversation.id == conv_id,
                        conversation.user_id == user_id,
                        conversation.project_id == project_id,
                    )
                    .order_by(active_path.c.depth.asc())
                    .limit(page_size + 1)
                )
            ).all()
            if not rows:
                # Preserve 404/403 semantics for empty conversations without penalizing
                # the normal non-empty history page.
                conv = await _load_owned(session, conv_id, user_id, project_id)
                return {
                    "messages": [],
                    "tree_nodes": [],
                    "active_leaf_id": conv.active_leaf_id,
                    "has_more": False,
                    "next_before_id": None,
                }
            tree_nodes: list[dict] = []
            if before_id is None:
                # Full-tree metadata is sent only with the initial page. Older pages
                # carry text only; the client retains this lightweight branch map.
                tree_rows = (
                    await session.execute(
                        select(
                            ChatMessage.id,
                            ChatMessage.parent_id,
                            ChatMessage.role,
                            ChatMessage.created_at,
                        )
                        .where(ChatMessage.conversation_id == conv_id)
                        .order_by(ChatMessage.id.asc())
                    )
                ).all()
                tree_nodes = [
                    {
                        "id": row.id,
                        "parent_id": row.parent_id,
                        "role": row.role,
                        "created_at": _iso(row.created_at),
                    }
                    for row in tree_rows
                ]
            has_more = len(rows) > page_size
            page = rows[:page_size]
            page.reverse()
            active_leaf_id = page[-1][1]
            user_message_ids = [message.id for message, _leaf in page if message.role == "user"]
            execution_by_message: dict[int, dict] = {}
            if user_message_ids:
                run_rows = (
                    await session.execute(
                        select(ChatRun)
                        .where(ChatRun.user_message_id.in_(user_message_ids))
                        .order_by(ChatRun.updated_at.desc(), ChatRun.id.desc())
                    )
                ).scalars()
                for run in run_rows:
                    if run.user_message_id is None or run.user_message_id in execution_by_message:
                        continue
                    execution_by_message[run.user_message_id] = {
                        "run_id": run.id,
                        "status": run.status,
                        "retryable": run.status in {"failed", "canceled"},
                    }
            messages = [_msg_public(message, execution=execution_by_message.get(message.id)) for message, _leaf in page]
            return {
                "messages": messages,
                "tree_nodes": tree_nodes,
                "active_leaf_id": active_leaf_id,
                "has_more": has_more,
                "next_before_id": messages[0]["parent_id"] if has_more and messages else None,
            }
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc


async def set_active_leaf(conv_id: str, *, user_id: str, project_id: str, message_id: int) -> dict:
    """활성 리프를 지정 메시지로 이동(형제 버전 전환). 메시지가 이 대화 소속이어야 함."""
    factory = _require_db()
    try:
        async with factory() as session, session.begin():
            conv = await _load_owned(session, conv_id, user_id, project_id)
            msg = await session.get(ChatMessage, message_id)
            if msg is None or msg.conversation_id != conv_id:
                raise ConversationNotFound(f"메시지 {message_id} 를 대화에서 찾을 수 없습니다")
            conv.active_leaf_id = message_id
            return _conv_public(conv)
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc


async def get_message_owned(conv_id: str, *, user_id: str, project_id: str, message_id: int) -> ChatMessage:
    """소유권 검증 후 대화 소속 메시지 raw row 반환(재생성 parent 탐색용, 내부 전용)."""
    factory = _require_db()
    try:
        async with factory() as session:
            await _load_owned(session, conv_id, user_id, project_id)
            msg = await session.get(ChatMessage, message_id)
            if msg is None or msg.conversation_id != conv_id:
                raise ConversationNotFound(f"메시지 {message_id} 를 대화에서 찾을 수 없습니다")
            return msg
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc


async def find_turn_start_user(conv_id: str, *, user_id: str, project_id: str, message_id: int) -> dict | None:
    """message_id 에서 위로 걸어 가장 가까운 role='user' 메시지(재생성 분기점). 없으면 None."""
    factory = _require_db()
    try:
        async with factory() as session:
            await _load_owned(session, conv_id, user_id, project_id)
            rows = (
                (await session.execute(select(ChatMessage).where(ChatMessage.conversation_id == conv_id)))
                .scalars()
                .all()
            )
            by_id = {r.id: r for r in rows}
            cur = by_id.get(message_id)
            seen: set[int] = set()
            while cur is not None and cur.id not in seen:
                if cur.role == "user":
                    return _msg_public(cur)
                seen.add(cur.id)
                cur = by_id.get(cur.parent_id) if cur.parent_id else None
            return None
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc


async def fork_conversation(conv_id: str, *, user_id: str, project_id: str, message_id: int) -> dict:
    """message_id 까지의 경로를 새 대화로 복사(분기). 소유자 동일, 원본 독립."""
    factory = _require_db()
    try:
        async with factory() as session, session.begin():
            conv = await _load_owned(session, conv_id, user_id, project_id)
            rows = list(
                (await session.execute(select(ChatMessage).where(ChatMessage.conversation_id == conv_id)))
                .scalars()
                .all()
            )
            by_id = {r.id: r for r in rows}
            if message_id not in by_id:
                raise ConversationNotFound(f"메시지 {message_id} 를 대화에서 찾을 수 없습니다")
            path = _backtrack(rows, message_id)  # message_id 를 리프로 간주해 역추적

            new_conv = ChatConversation(
                id=str(uuid.uuid4()),
                project_id=conv.project_id,
                user_id=user_id,
                title=conv.title,  # 암호문 그대로 복사
                model_name=conv.model_name,
                parent_conversation_id=conv_id,
                forked_from_message_id=message_id,
            )
            session.add(new_conv)
            await session.flush()

            id_map: dict[int, int] = {}  # old id -> new id (parent 재구성)
            new_leaf_id = None
            for r in path:
                new_parent = id_map.get(r.parent_id) if r.parent_id else None
                nr = ChatMessage(
                    conversation_id=new_conv.id,
                    role=r.role,
                    parent_id=new_parent,
                    content=r.content,  # 암호문 그대로 복사(재암호화 불필요)
                    tool_calls=r.tool_calls,
                    token_prompt=r.token_prompt,
                    token_completion=r.token_completion,
                    model_name=r.model_name,
                )
                session.add(nr)
                await session.flush()
                id_map[r.id] = nr.id
                new_leaf_id = nr.id
            new_conv.active_leaf_id = new_leaf_id
            return _conv_public(new_conv)
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc
