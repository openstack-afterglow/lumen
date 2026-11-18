"""빌트인 AI 채팅 에이전트 저장소 — 프롬프트+모델+파라미터+MCP/툴 묶음 (MySQL chat_agents).

Private CRUD and execution require the same user and verified domain, or exact legacy origin.
instructions 는 AES-256-GCM(chat_content 도메인) 암호문으로 저장하고 조회 시 복호화한다.
The hub shares active public templates, never their private attachments.
"""

from __future__ import annotations

import logging

from lumen_plugin_api.contracts import Namespace, PluginError
from sqlalchemy import or_, select, update
from sqlalchemy.exc import OperationalError

from lumen.crypto import decrypt_chat_content, encrypt_chat_content
from lumen.db import get_session_factory, is_db_available, mark_db_unhealthy
from lumen.models.chat_contracts import validate_plugin_binding_ids
from lumen.models.chat_db import ChatAgent
from lumen.plugins.bindings import resolve_binding
from lumen.services.domain_scope import scope_clause, scope_matches

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


def _public(
    row: ChatAgent, *, owner_view: bool, include_private: bool = True, public_template: bool = False,
) -> dict:
    """Serialize owner configuration or an explicitly public hub template."""
    include_attachments = owner_view and not public_template
    return {
        "id": row.id,
        "owner_user_id": row.owner_user_id,
        "name": row.name,
        "description": row.description,
        "avatar": row.avatar,
        **({"instructions": _dec(row.instructions)} if include_private else {}),
        "model_name": row.model_name,
        **({
            "params": row.params or {},
            "mcp_ids": (row.mcp_ids or []) if include_attachments else [],
            "tool_ids": (row.tool_ids or []) if include_attachments else [],
            "skill_ids": (row.skill_ids or []) if include_attachments else [],
            "plugin_tool_ids": (row.plugin_tool_ids or []) if include_attachments else [],
            "plugin_skill_ids": (row.plugin_skill_ids or []) if include_attachments else [],
        } if include_private else {}),
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


async def _validate_plugin_selection(values: list[str] | None, kind: str, *, user_id: str, project_id: str) -> list[str]:
    try:
        ids = validate_plugin_binding_ids(values or [])
        namespace = Namespace(user_id=user_id, project_id=project_id)
        for identifier in ids:
            await resolve_binding(identifier, kind=kind, namespace=namespace)
        return ids
    except (ValueError, PluginError) as exc:
        raise AgentValidationError("selected plugin binding is unavailable") from exc


async def create_agent(
    *,
    owner_user_id: str,
    project_id: str,
    name: str,
    domain_id: str | None = None,
    description: str | None = None,
    avatar: str | None = None,
    instructions: str | None = None,
    model_name: str | None = None,
    params: dict | None = None,
    mcp_ids: list[int] | None = None,
    tool_ids: list[int] | None = None,
    skill_ids: list[int] | None = None,
    plugin_tool_ids: list[str] | None = None,
    plugin_skill_ids: list[str] | None = None,
    visibility: str = "private",
) -> dict:
    factory = _require_db()
    _validate(name, visibility)
    mcp_ids = _validate_ids(mcp_ids, "MCP")
    tool_ids = _validate_ids(tool_ids, "tool")
    skill_ids = _validate_ids(skill_ids, "skill")
    plugin_tool_ids = await _validate_plugin_selection(plugin_tool_ids, "tool", user_id=owner_user_id, project_id=project_id)
    plugin_skill_ids = await _validate_plugin_selection(plugin_skill_ids, "skill", user_id=owner_user_id, project_id=project_id)
    row = ChatAgent(
        owner_user_id=owner_user_id,
        project_id=project_id,
        domain_id=domain_id,
        name=name.strip(),
        description=(description or None),
        avatar=(avatar or None),
        instructions=_enc(instructions or None),
        model_name=(model_name or None),
        params=(params or None),
        mcp_ids=(mcp_ids or None),
        tool_ids=(tool_ids or None),
        skill_ids=(skill_ids or None),
        plugin_tool_ids=plugin_tool_ids,
        plugin_skill_ids=plugin_skill_ids,
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


def _owned_scope(user_id: str, project_id: str, *, domain_id: str | None):
    return (ChatAgent.owner_user_id == user_id) & scope_clause(
        ChatAgent.domain_id, ChatAgent.project_id, domain_id=domain_id, project_id=project_id,
    )


async def list_agents(
    *, user_id: str, project_id: str, domain_id: str | None = None, include_private: bool = True,
) -> list[dict]:
    """List the caller's domain-owned agents and legacy agents from the selected project."""
    factory = _require_db()
    try:
        async with factory() as session:
            rows = (
                (
                    await session.execute(
                        select(ChatAgent)
                        .where(_owned_scope(user_id, project_id, domain_id=domain_id))
                        .order_by(ChatAgent.updated_at.desc())
                    )
                )
                .scalars()
                .all()
            )
            return [_public(r, owner_view=True, include_private=include_private) for r in rows]
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc


async def list_public(
    *, query: str = "", limit: int = 30, offset: int = 0, user_id: str = "", project_id: str = "",
    domain_id: str | None = None,
) -> list[dict]:
    """Search public templates without exposing their private bindings."""
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
            return [
                _public(
                    row,
                    owner_view=row.owner_user_id == user_id and scope_matches(
                        row.domain_id, row.project_id, domain_id=domain_id, project_id=project_id,
                    ),
                    public_template=True,
                )
                for row in rows
            ]
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc


async def _load_owned(session, agent_id: int, user_id: str, project_id: str, *, domain_id: str | None) -> ChatAgent:
    row = (
        await session.execute(
            select(ChatAgent).where(
                ChatAgent.id == agent_id,
                _owned_scope(user_id, project_id, domain_id=domain_id),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise AgentNotFound(f"에이전트 {agent_id} 를 찾을 수 없습니다")
    return row


async def get_agent(agent_id: int, *, user_id: str, project_id: str, domain_id: str | None = None) -> dict:
    """Read only a caller-owned agent in scope."""
    factory = _require_db()
    try:
        async with factory() as session:
            row = await _load_owned(session, agent_id, user_id, project_id, domain_id=domain_id)
            return _public(row, owner_view=True)
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc


async def update_agent(
    agent_id: int, *, user_id: str, project_id: str, patch: dict, domain_id: str | None = None,
) -> dict:
    """Update only a caller-owned agent in scope."""
    factory = _require_db()
    _validate(patch.get("name"), patch.get("visibility"))
    for field, label in (("mcp_ids", "MCP"), ("tool_ids", "tool"), ("skill_ids", "skill")):
        if field in patch:
            _validate_ids(patch[field], label)
    for field, kind in (("plugin_tool_ids", "tool"), ("plugin_skill_ids", "skill")):
        if field in patch:
            patch[field] = await _validate_plugin_selection(patch[field], kind, user_id=user_id, project_id=project_id)
    try:
        async with factory() as session, session.begin():
            row = await _load_owned(session, agent_id, user_id, project_id, domain_id=domain_id)
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
            for field in ("plugin_tool_ids", "plugin_skill_ids"):
                if field in patch:
                    setattr(row, field, patch[field])
            if patch.get("visibility"):
                row.visibility = patch["visibility"]
            await session.flush()
            return _public(row, owner_view=True)
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc


async def delete_agent(agent_id: int, *, user_id: str, project_id: str, domain_id: str | None = None) -> None:
    factory = _require_db()
    try:
        async with factory() as session, session.begin():
            row = await _load_owned(session, agent_id, user_id, project_id, domain_id=domain_id)
            await session.delete(row)
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc


async def clone_agent(agent_id: int, *, user_id: str, project_id: str, domain_id: str | None = None) -> dict:
    """Clone an active public template or a private agent owned in the current scope."""
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
                            _owned_scope(user_id, project_id, domain_id=domain_id),
                        ),
                    )
                )
            ).scalar_one_or_none()
            if src is None:
                raise AgentNotFound(f"에이전트 {agent_id} 를 찾을 수 없습니다")
            clone = ChatAgent(
                owner_user_id=user_id,
                project_id=project_id,
                domain_id=domain_id,
                name=src.name,
                description=src.description,
                avatar=src.avatar,
                instructions=src.instructions,
                model_name=src.model_name,
                params=src.params,
                mcp_ids=None,
                tool_ids=None,
                skill_ids=None,
                plugin_tool_ids=[],
                plugin_skill_ids=[],
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


async def load_active_agent(
    session, agent_id: int, *, user_id: str, project_id: str, domain_id: str | None = None,
) -> ChatAgent | None:
    """Check current ownership and activity without using the agent configuration."""
    return (
        await session.execute(
            select(ChatAgent).where(
                ChatAgent.id == agent_id,
                ChatAgent.is_active.is_(True),
                _owned_scope(user_id, project_id, domain_id=domain_id),
            )
        )
    ).scalar_one_or_none()


async def get_agent_for_run(
    agent_id: int, *, user_id: str, project_id: str, domain_id: str | None = None,
) -> dict | None:
    """Load only an active caller-owned agent for an executable snapshot."""
    factory = _require_db()
    try:
        async with factory() as session:
            row = await load_active_agent(session, agent_id, user_id=user_id, project_id=project_id, domain_id=domain_id)
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
                "plugin_tool_ids": row.plugin_tool_ids or [],
                "plugin_skill_ids": row.plugin_skill_ids or [],
                "execution_policy": row.execution_policy or {},
                "delegable_agent_ids": row.delegable_agent_ids or [],
            }
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc
