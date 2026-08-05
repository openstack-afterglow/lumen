"""Authenticated project-scoped Git credential and remote code-workspace APIs."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from pydantic import BaseModel, Field, field_validator

from lumen.auth import get_token_info
from lumen.config import get_settings
from lumen.services.agent_workspace_runtime import configured_workspace_policy
from lumen.services.code_workspace_service import (
    CodeWorkspaceConflict,
    CodeWorkspaceNotFound,
    CodeWorkspaceUnavailable,
    assign_conversation_workspace,
    create_git_credential,
    create_workspace,
    get_workspace,
    list_git_credentials,
    list_workspaces,
    request_delete_workspace,
    revoke_git_credential,
    update_git_credential,
)
from lumen.services.code_workspace_store import CodeWorkspaceValidationError

router = APIRouter()


class GitCredentialCreate(BaseModel):
    host: str = Field(min_length=1, max_length=255)
    username: str | None = Field(default=None, max_length=255)
    token: str = Field(min_length=1, max_length=16_384)


class GitCredentialUpdate(BaseModel):
    username: str | None = Field(default=None, max_length=255)
    token: str | None = Field(default=None, min_length=1, max_length=16_384)


class CodeWorkspaceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    source_kind: str = Field(pattern="^(empty|git)$")
    repository_url: str | None = Field(default=None, max_length=2048)
    source_revision: str | None = Field(default=None, max_length=255)
    credential_id: int | None = Field(default=None, gt=0)

    @field_validator("repository_url", "source_revision")
    @classmethod
    def _normalize_optional_text(cls, value: str | None) -> str | None:
        return value.strip() if isinstance(value, str) else value


class ConversationWorkspaceAssignment(BaseModel):
    code_workspace_id: str | None = Field(default=None, max_length=36)


def _project(token_info: dict) -> str:
    project_id = token_info.get("project_id")
    if not isinstance(project_id, str) or not project_id:
        raise HTTPException(status_code=401, detail="project scope is required")
    return project_id


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, CodeWorkspaceNotFound):
        return HTTPException(status_code=404, detail="resource was not found")
    if isinstance(exc, CodeWorkspaceConflict):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, CodeWorkspaceValidationError):
        return HTTPException(status_code=422, detail=str(exc))
    return HTTPException(status_code=503, detail="code workspace service is unavailable")


@router.get("/git-credentials")
async def get_git_credentials(token_info: dict = Depends(get_token_info)):
    try:
        return await list_git_credentials(user_id=token_info["user_id"], project_id=_project(token_info))
    except CodeWorkspaceUnavailable as exc:
        raise _error(exc) from exc


@router.post("/git-credentials", status_code=status.HTTP_201_CREATED)
async def post_git_credential(payload: GitCredentialCreate, token_info: dict = Depends(get_token_info)):
    try:
        return await create_git_credential(
            user_id=token_info["user_id"], project_id=_project(token_info), **payload.model_dump()
        )
    except (CodeWorkspaceValidationError, CodeWorkspaceUnavailable) as exc:
        raise _error(exc) from exc


@router.put("/git-credentials/{credential_id}")
async def put_git_credential(
    credential_id: int, payload: GitCredentialUpdate, token_info: dict = Depends(get_token_info)
):
    try:
        return await update_git_credential(
            credential_id,
            user_id=token_info["user_id"],
            project_id=_project(token_info),
            **payload.model_dump(exclude_unset=True),
        )
    except (CodeWorkspaceValidationError, CodeWorkspaceNotFound, CodeWorkspaceUnavailable) as exc:
        raise _error(exc) from exc


@router.delete("/git-credentials/{credential_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_git_credential(credential_id: int, token_info: dict = Depends(get_token_info)):
    try:
        await revoke_git_credential(credential_id, user_id=token_info["user_id"], project_id=_project(token_info))
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    except (CodeWorkspaceNotFound, CodeWorkspaceUnavailable) as exc:
        raise _error(exc) from exc


@router.post("/code-workspaces", status_code=status.HTTP_202_ACCEPTED)
async def post_code_workspace(
    payload: CodeWorkspaceCreate,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    token_info: dict = Depends(get_token_info),
):
    policy = configured_workspace_policy(get_settings())
    if policy is None:
        raise HTTPException(status_code=422, detail="code workspace runtime is not configured")
    try:
        return await create_workspace(
            user_id=token_info["user_id"],
            project_id=_project(token_info),
            idempotency_key=idempotency_key or "",
            policy=policy,
            **payload.model_dump(),
        )
    except (CodeWorkspaceValidationError, CodeWorkspaceConflict, CodeWorkspaceUnavailable) as exc:
        raise _error(exc) from exc


@router.get("/code-workspaces")
async def get_code_workspaces(token_info: dict = Depends(get_token_info)):
    try:
        return await list_workspaces(user_id=token_info["user_id"], project_id=_project(token_info))
    except CodeWorkspaceUnavailable as exc:
        raise _error(exc) from exc


@router.get("/code-workspaces/{workspace_id}")
async def get_code_workspace(workspace_id: str, token_info: dict = Depends(get_token_info)):
    try:
        return await get_workspace(workspace_id, user_id=token_info["user_id"], project_id=_project(token_info))
    except (CodeWorkspaceNotFound, CodeWorkspaceUnavailable) as exc:
        raise _error(exc) from exc


@router.delete("/code-workspaces/{workspace_id}", status_code=status.HTTP_202_ACCEPTED)
async def delete_code_workspace(workspace_id: str, token_info: dict = Depends(get_token_info)):
    try:
        return await request_delete_workspace(
            workspace_id, user_id=token_info["user_id"], project_id=_project(token_info)
        )
    except (CodeWorkspaceNotFound, CodeWorkspaceUnavailable) as exc:
        raise _error(exc) from exc


@router.put("/conversations/{conversation_id}/code-workspace")
async def put_conversation_code_workspace(
    conversation_id: str, payload: ConversationWorkspaceAssignment, token_info: dict = Depends(get_token_info)
):
    try:
        return await assign_conversation_workspace(
            conversation_id,
            workspace_id=payload.code_workspace_id,
            user_id=token_info["user_id"],
            project_id=_project(token_info),
        )
    except (CodeWorkspaceNotFound, CodeWorkspaceConflict, CodeWorkspaceUnavailable) as exc:
        raise _error(exc) from exc
