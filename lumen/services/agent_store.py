"""빌트인 AI 채팅 에이전트 저장소 — 프롬프트+모델+파라미터+MCP/툴 묶음 (MySQL chat_agents).

⚠️ 소유권(IDOR): CRUD와 실행은 호출자 user/project에 한정한다.
instructions 는 AES-256-GCM(chat_content 도메인) 암호문으로 저장하고 조회 시 복호화한다.
허브 복제는 active public 템플릿 또는 같은 프로젝트(레거시 NULL 포함) 소유 에이전트만 private 사본으로 복사한다.
"""

from __future__ import annotations

import logging

from sqlalchemy import or_, select, update
from sqlalchemy.exc import OperationalError

from lumen.crypto import decrypt_chat_content, encrypt_chat_content
from lumen.db import get_session_factory, is_db_available, mark_db_unhealthy
from lumen.models.chat_db import ChatAgent

logger = logging.getLogger(__name__)

_VISIBILITIES = ("private", "public")


class ChatStorageUnavailable(RuntimeError):
    """chat DB 미구성/장애 — fail-closed(503)."""


class AgentNotFound(LookupError):
    """에이전트 미존재 — 404."""


class AgentForbidden(PermissionError):
    """소유자 불일치(비공개 타인 접근/수정) — 403."""


class AgentValidationError(ValueError):
    """입력 검증 실패 — 400."""


def _require_db():
    if not is_db_available():
        raise ChatStorageUnavailable("chat DB 를 사용할 수 없습니다")
    factory = get_session_factory()
    if factory is None:
        raise ChatStorageUnavailable("chat DB 가 구성되지 않았습니다")
    return factory


def _iso(dt) -> str | None:
    return dt.isoformat() if dt else None


def _dec(v: str | None) -> str | None:
    return decrypt_chat_content(v) if v else v


def _enc(v: str | None) -> str | None:
    return encrypt_chat_content(v) if v else v


def _public(row: ChatAgent, *, owner_view: bool = False) -> dict:
    """에이전트 공개 dict. 공개 에이전트는 instructions 를 공유(허브 복제 대상)한다."""
    return {
        "id": row.id,
        "owner_user_id": row.owner_user_id,
        "name": row.name,
        "description": row.description,
        "avatar": row.avatar,
        "instructions": _dec(row.instructions),
        "model_name": row.model_name,
        "params": row.params or {},
        "mcp_ids": row.mcp_ids or [],
        "tool_ids": row.tool_ids or [],
        "skill_ids": getattr(row, "skill_ids", None) or [],
        "visibility": row.visibility,
        "cloned_from_id": row.cloned_from_id,
        "clone_count": row.clone_count,
        "is_owner": owner_view,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def _validate(name: str | None, visibility: str | None) -> None:
    if name is not None and not name.strip():
        raise AgentValidationError("name 은 필수입니다")
    if visibility is not None and visibility not in _VISIBILITIES:
        raise AgentValidationError("visibility 는 private|public 이어야 합니다")


def _validate_ids(value: list | None, label: str) -> list | None:
    if value is None:
        return None
    if len(set(value)) != len(value) or any(not isinstance(item, int) or item < 1 for item in value):
        raise AgentValidationError(f"{label} IDs must be unique positive ids")
    return value


async def create_agent(
    *,
    owner_user_id: str,
    project_id: str,
    name: str,
    description: str | None = None,
    avatar: str | None = None,
    instructions: str | None = None,
    model_name: str | None = None,
    params: dict | None = None,
    mcp_ids: list[int] | None = None,
    tool_ids: list[int] | None = None,
    skill_ids: list[int] | None = None,
    visibility: str = "private",
) -> dict:
    factory = _require_db()
    _validate(name, visibility)
    mcp_ids = _validate_ids(mcp_ids, "MCP")
    tool_ids = _validate_ids(tool_ids, "tool")
    skill_ids = _validate_ids(skill_ids, "skill")
    row = ChatAgent(
        owner_user_id=owner_user_id,
        project_id=project_id,
        name=name.strip(),
        description=(description or None),
        avatar=(avatar or None),
        instructions=_enc(instructions or None),
        model_name=(model_name or None),
        params=(params or None),
        mcp_ids=(mcp_ids or None),
        tool_ids=(tool_ids or None),
        skill_ids=(skill_ids or None),
        visibility=visibility,
    )
    try:
        async with factory() as session, session.begin():
            session.add(row)
            await session.flush()
            return _public(row, owner_view=True)
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc


async def list_agents(*, user_id: str, project_id: str) -> list[dict]:
    """List only the caller's project-owned agents."""
    factory = _require_db()
    try:
        async with factory() as session:
            rows = (
                (
                    await session.execute(
                        select(ChatAgent)
                        .where(ChatAgent.owner_user_id == user_id, ChatAgent.project_id == project_id)
                        .order_by(ChatAgent.updated_at.desc())
                    )
                )
                .scalars()
                .all()
            )
            return [_public(r, owner_view=True) for r in rows]
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc


async def list_public(
    *, query: str = "", limit: int = 30, offset: int = 0, user_id: str = "", project_id: str = ""
) -> list[dict]:
    """Search public templates and mark ownership only within the caller's project."""
    factory = _require_db()
    try:
        async with factory() as session:
            stmt = select(ChatAgent).where(ChatAgent.visibility == "public", ChatAgent.is_active.is_(True))
            q = (query or "").strip()
            if q:
                like = f"%{q}%"
                stmt = stmt.where(or_(ChatAgent.name.like(like), ChatAgent.description.like(like)))
            stmt = stmt.order_by(ChatAgent.clone_count.desc(), ChatAgent.id.desc())
            stmt = stmt.limit(min(max(limit, 1), 100)).offset(max(offset, 0))
            rows = (await session.execute(stmt)).scalars().all()
            return [_public(r, owner_view=(r.owner_user_id == user_id and r.project_id == project_id)) for r in rows]
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc


async def _load(session, agent_id: int) -> ChatAgent:
    row = await session.get(ChatAgent, agent_id)
    if row is None:
        raise AgentNotFound(f"에이전트 {agent_id} 를 찾을 수 없습니다")
    return row


async def _load_owned_project(session, agent_id: int, user_id: str, project_id: str) -> ChatAgent:
    row = (
        await session.execute(
            select(ChatAgent).where(
                ChatAgent.id == agent_id,
                ChatAgent.owner_user_id == user_id,
                ChatAgent.project_id == project_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise AgentNotFound(f"에이전트 {agent_id} 를 찾을 수 없습니다")
    return row


async def get_agent(agent_id: int, *, user_id: str, project_id: str) -> dict:
    """Read only the caller's project-owned agent."""
    factory = _require_db()
    try:
        async with factory() as session:
            row = await _load_owned_project(session, agent_id, user_id, project_id)
            return _public(row, owner_view=True)
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc


async def update_agent(agent_id: int, *, user_id: str, project_id: str, patch: dict) -> dict:
    """Update only a caller-owned project agent."""
    factory = _require_db()
    _validate(patch.get("name"), patch.get("visibility"))
    for field, label in (("mcp_ids", "MCP"), ("tool_ids", "tool"), ("skill_ids", "skill")):
        if field in patch:
            _validate_ids(patch[field], label)
    try:
        async with factory() as session, session.begin():
            row = await _load_owned_project(session, agent_id, user_id, project_id)
            if patch.get("name"):
                row.name = str(patch["name"]).strip()
            if "description" in patch:
                row.description = patch["description"] or None
            if "avatar" in patch:
                row.avatar = patch["avatar"] or None
            if "instructions" in patch:
                row.instructions = _enc(patch["instructions"] or None)
            if "model_name" in patch:
                row.model_name = patch["model_name"] or None
            if "params" in patch:
                row.params = patch["params"] or None
            if "mcp_ids" in patch:
                row.mcp_ids = patch["mcp_ids"] or None
            if "tool_ids" in patch:
                row.tool_ids = patch["tool_ids"] or None
            if "skill_ids" in patch:
                row.skill_ids = patch["skill_ids"] or None
            if patch.get("visibility"):
                row.visibility = patch["visibility"]
            await session.flush()
            return _public(row, owner_view=True)
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc


async def delete_agent(agent_id: int, *, user_id: str, project_id: str) -> None:
    factory = _require_db()
    try:
        async with factory() as session, session.begin():
            row = await _load_owned_project(session, agent_id, user_id, project_id)
            await session.delete(row)
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc


async def clone_agent(agent_id: int, *, user_id: str, project_id: str) -> dict:
    """Clone an active public template, same-project private agent, or owned legacy template."""
    factory = _require_db()
    try:
        async with factory() as session, session.begin():
            src = (
                await session.execute(
                    select(ChatAgent).where(
                        ChatAgent.id == agent_id,
                        ChatAgent.is_active.is_(True),
                        or_(
                            ChatAgent.visibility == "public",
                            (ChatAgent.owner_user_id == user_id)
                            & or_(ChatAgent.project_id == project_id, ChatAgent.project_id.is_(None)),
                        ),
                    )
                )
            ).scalar_one_or_none()
            if src is None:
                raise AgentNotFound(f"에이전트 {agent_id} 를 찾을 수 없습니다")
            clone = ChatAgent(
                owner_user_id=user_id,
                project_id=project_id,
                name=src.name,
                description=src.description,
                avatar=src.avatar,
                instructions=src.instructions,
                model_name=src.model_name,
                params=src.params,
                mcp_ids=None,
                tool_ids=None,
                skill_ids=None,
                delegable_agent_ids=None,
                visibility="private",
                cloned_from_id=src.id,
            )
            session.add(clone)
            if src.owner_user_id != user_id:
                await session.execute(
                    update(ChatAgent).where(ChatAgent.id == src.id).values(clone_count=ChatAgent.clone_count + 1)
                )
            await session.flush()
            return _public(clone, owner_view=True)
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc


async def get_agent_for_run(agent_id: int, *, user_id: str, project_id: str) -> dict | None:
    """Load only an active, caller-owned project agent for executable run snapshots."""
    factory = _require_db()
    try:
        async with factory() as session:
            row = (
                await session.execute(
                    select(ChatAgent).where(
                        ChatAgent.id == agent_id,
                        ChatAgent.owner_user_id == user_id,
                        ChatAgent.project_id == project_id,
                        ChatAgent.is_active.is_(True),
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return {
                "id": row.id,
                "instructions": _dec(row.instructions),
                "model_name": row.model_name,
                "params": row.params or {},
                "mcp_ids": row.mcp_ids or [],
                "role": row.role,
                "tool_ids": row.tool_ids or [],
                "skill_ids": row.skill_ids or [],
                "execution_policy": row.execution_policy or {},
                "delegable_agent_ids": row.delegable_agent_ids or [],
            }
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc
