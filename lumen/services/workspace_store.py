"""빌트인 AI 채팅 프로젝트(workspace) 저장소 — 대화 그룹 + 공통 지침 (MySQL chat_workspaces).

⚠️ 소유권(IDOR): 조회/수정/삭제 모두 소유자(user_id) 한정. instructions 는 chat_content 암호문.
OpenStack project_id 와 별개 개념 — 완료 경로가 소속 대화에 워크스페이스 지침을 system 으로 주입한다.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from lumen.crypto import decrypt_chat_content, encrypt_chat_content
from lumen.db import get_session_factory, is_db_available, mark_db_unhealthy
from lumen.models.chat_db import ChatWorkspace

logger = logging.getLogger(__name__)


class ChatStorageUnavailable(RuntimeError):
    """chat DB 미구성/장애 — fail-closed(503)."""


class WorkspaceNotFound(LookupError):
    """워크스페이스 미존재 — 404."""


class WorkspaceForbidden(PermissionError):
    """소유자 불일치 — 403."""


class WorkspaceValidationError(ValueError):
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


def _public(row: ChatWorkspace) -> dict:
    return {
        "id": row.id,
        "owner_user_id": row.owner_user_id,
        "name": row.name,
        "description": row.description,
        "instructions": _dec(row.instructions),
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


async def _load_owned(session, workspace_id: int, user_id: str) -> ChatWorkspace:
    row = await session.get(ChatWorkspace, workspace_id)
    if row is None:
        raise WorkspaceNotFound(f"프로젝트 {workspace_id} 를 찾을 수 없습니다")
    if row.owner_user_id != user_id:
        raise WorkspaceForbidden("프로젝트에 접근할 권한이 없습니다")
    return row


async def create_workspace(
    *, owner_user_id: str, name: str, description: str | None = None, instructions: str | None = None
) -> dict:
    factory = _require_db()
    if not name or not name.strip():
        raise WorkspaceValidationError("name 은 필수입니다")
    row = ChatWorkspace(
        owner_user_id=owner_user_id,
        name=name.strip(),
        description=(description or None),
        instructions=_enc(instructions or None),
    )
    try:
        async with factory() as session, session.begin():
            session.add(row)
            await session.flush()
            return _public(row)
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc


async def list_workspaces(*, user_id: str) -> list[dict]:
    factory = _require_db()
    try:
        async with factory() as session:
            rows = (
                (
                    await session.execute(
                        select(ChatWorkspace)
                        .where(ChatWorkspace.owner_user_id == user_id)
                        .order_by(ChatWorkspace.updated_at.desc())
                    )
                )
                .scalars()
                .all()
            )
            return [_public(r) for r in rows]
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc


async def get_workspace(workspace_id: int, *, user_id: str) -> dict:
    factory = _require_db()
    try:
        async with factory() as session:
            return _public(await _load_owned(session, workspace_id, user_id))
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc


async def update_workspace(workspace_id: int, *, user_id: str, patch: dict) -> dict:
    factory = _require_db()
    if "name" in patch and (not patch["name"] or not str(patch["name"]).strip()):
        raise WorkspaceValidationError("name 은 필수입니다")
    try:
        async with factory() as session, session.begin():
            row = await _load_owned(session, workspace_id, user_id)
            if patch.get("name"):
                row.name = str(patch["name"]).strip()
            if "description" in patch:
                row.description = patch["description"] or None
            if "instructions" in patch:
                row.instructions = _enc(patch["instructions"] or None)
            await session.flush()
            return _public(row)
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc


async def delete_workspace(workspace_id: int, *, user_id: str) -> None:
    factory = _require_db()
    try:
        async with factory() as session, session.begin():
            row = await _load_owned(session, workspace_id, user_id)
            await session.delete(row)
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc


async def get_instructions_for_run(workspace_id: int | None, *, user_id: str) -> str | None:
    """완료 경로용 — 소유한 워크스페이스의 지침(복호화). 미소유/미존재 시 None(조용히 무시).

    ⚠️ 반환 지침은 복호화 평문 — system 주입 전용.
    """
    if workspace_id is None:
        return None
    factory = _require_db()
    try:
        async with factory() as session:
            row = await session.get(ChatWorkspace, workspace_id)
            if row is None or row.owner_user_id != user_id:
                return None
            return _dec(row.instructions)
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc
