"""빌트인 AI 채팅 확장 API — MCP 서버 / 커스텀 HTTP 툴 관리.

- 관리자 라우터(require_admin): scope='global' 확장 CRUD (전체 적용).
- 사용자 라우터(get_token_info): 본인 scope='user' 확장 CRUD (본인 적용) + 활성 global 열람.

⚠️ MCP 서버는 등록·관리 전용(실행은 langchain-mcp-adapters 핀 이동 후). 커스텀 툴 실행은 후속 슬라이스.
소유권/스코프 검증은 extensions_store 가 강제하고, 예외를 HTTP 상태로 매핑한다.
"""

from __future__ import annotations

import secrets
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from lumen.auth import get_token_info, require_admin
from lumen.services import extensions_store as es
from lumen.services import mcp_oauth

admin_router = APIRouter(dependencies=[Depends(require_admin)])
user_router = APIRouter()


# --- 요청 모델 ---
class McpServerBody(BaseModel):
    """A user-owned public remote MCP source.

    User sources are HTTPS Streamable HTTP only. OAuth is detected after
    registration; arbitrary headers, commands, and secret variables are rejected.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, max_length=100)
    transport: str | None = Field(default=None, max_length=20)
    url: str | None = Field(default=None, max_length=500)
    is_active: bool | None = None


class AdminMcpServerBody(McpServerBody):
    """Administrator-approved OAuth or static-header MCP source."""

    headers: dict | None = None
    auth_mode: Literal["none", "oauth", "admin"] | None = None
    oauth_scopes: list[str] | None = None
    oauth_client_id: str | None = Field(default=None, max_length=512)
    oauth_client_secret: str | None = Field(default=None, max_length=4096)
    load_policy: Literal["preloaded", "on_demand"] | None = None


class CustomToolBody(BaseModel):
    name: str | None = Field(default=None, max_length=64)
    description: str | None = Field(default=None, max_length=500)
    method: str | None = Field(default=None, max_length=10)
    url: str | None = Field(default=None, max_length=500)
    params_schema: dict | None = None
    timeout_seconds: int | None = Field(default=None, ge=1, le=60)
    is_active: bool | None = None

    model_config = ConfigDict(extra="forbid", protected_namespaces=())


class AdminCustomToolBody(CustomToolBody):
    """Administrator-only global custom tool policy."""

    load_policy: Literal["preloaded", "on_demand"] | None = None


class SkillBody(BaseModel):
    """스킬 정의 — 선택 시 채팅 system 프리앰블에 주입되는 지침(SKILL.md 본문).

    instructions 는 시크릿/프로프라이어터리로 취급되어 AES-256-GCM 으로 암호화 저장된다.
    """

    name: str | None = Field(default=None, max_length=100)
    description: str | None = Field(default=None, max_length=500)
    instructions: str | None = Field(default=None, max_length=20000)
    is_active: bool | None = None


def _http(exc: Exception) -> HTTPException:
    if isinstance(exc, es.ExtensionNotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, es.ExtensionForbidden):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, es.ExtensionValidationError):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, mcp_oauth.McpOAuthError):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=503, detail=str(exc))


_EXC = (
    es.ExtensionNotFound,
    es.ExtensionForbidden,
    es.ExtensionValidationError,
    es.ExtensionSecretUnavailable,
    es.ChatStorageUnavailable,
)


# ---------------------------------------------------------------------------
# 관리자 (global)
# ---------------------------------------------------------------------------
@admin_router.get("/admin/mcp-servers")
async def admin_list_mcp():
    try:
        return await es.list_global("mcp")
    except _EXC as exc:
        raise _http(exc) from exc


@admin_router.post("/admin/mcp-servers", status_code=201)
async def admin_create_mcp(body: AdminMcpServerBody):
    try:
        return await es.create("mcp", body.model_dump(exclude_unset=True), scope="global")
    except _EXC as exc:
        raise _http(exc) from exc


@admin_router.patch("/admin/mcp-servers/{item_id}")
async def admin_update_mcp(item_id: int, body: AdminMcpServerBody):
    try:
        return await es.update("mcp", item_id, body.model_dump(exclude_unset=True), admin=True)
    except _EXC as exc:
        raise _http(exc) from exc


@admin_router.delete("/admin/mcp-servers/{item_id}", status_code=204)
async def admin_delete_mcp(item_id: int):
    try:
        await es.delete("mcp", item_id, admin=True)
    except _EXC as exc:
        raise _http(exc) from exc


@admin_router.get("/admin/custom-tools")
async def admin_list_tools():
    try:
        return await es.list_global("tool")
    except _EXC as exc:
        raise _http(exc) from exc


@admin_router.post("/admin/custom-tools", status_code=201)
async def admin_create_tool(body: AdminCustomToolBody):
    try:
        return await es.create("tool", body.model_dump(exclude_unset=True), scope="global")
    except _EXC as exc:
        raise _http(exc) from exc


@admin_router.patch("/admin/custom-tools/{item_id}")
async def admin_update_tool(item_id: int, body: AdminCustomToolBody):
    try:
        return await es.update("tool", item_id, body.model_dump(exclude_unset=True), admin=True)
    except _EXC as exc:
        raise _http(exc) from exc


@admin_router.delete("/admin/custom-tools/{item_id}", status_code=204)
async def admin_delete_tool(item_id: int):
    try:
        await es.delete("tool", item_id, admin=True)
    except _EXC as exc:
        raise _http(exc) from exc


@admin_router.get("/admin/skills")
async def admin_list_skills():
    try:
        return await es.list_global("skill")
    except _EXC as exc:
        raise _http(exc) from exc


@admin_router.post("/admin/skills", status_code=201)
async def admin_create_skill(body: SkillBody):
    try:
        return await es.create("skill", body.model_dump(exclude_unset=True), scope="global")
    except _EXC as exc:
        raise _http(exc) from exc


@admin_router.patch("/admin/skills/{item_id}")
async def admin_update_skill(item_id: int, body: SkillBody):
    try:
        return await es.update("skill", item_id, body.model_dump(exclude_unset=True), admin=True)
    except _EXC as exc:
        raise _http(exc) from exc


@admin_router.delete("/admin/skills/{item_id}", status_code=204)
async def admin_delete_skill(item_id: int):
    try:
        await es.delete("skill", item_id, admin=True)
    except _EXC as exc:
        raise _http(exc) from exc


# ---------------------------------------------------------------------------
# 사용자 (본인 스코프 + 활성 global 열람)
# ---------------------------------------------------------------------------
def _owner(token_info: dict) -> tuple[str, str]:
    return token_info["user_id"], token_info["project_id"]


@user_router.get("/mcp-servers")
async def user_list_mcp(token_info: dict = Depends(get_token_info)):
    uid, pid = _owner(token_info)
    # 선택적 기능 목록: 저장소 미가용/데이터 없음은 빈 목록으로 graceful 처리(503 아님).
    try:
        return await es.list_for_user("mcp", user_id=uid, project_id=pid)
    except es.ChatStorageUnavailable:
        return []
    except _EXC as exc:
        raise _http(exc) from exc


@user_router.post("/mcp-servers", status_code=201)
async def user_create_mcp(body: McpServerBody, token_info: dict = Depends(get_token_info)):
    uid, pid = _owner(token_info)
    try:
        source = await es.create(
            "mcp", body.model_dump(exclude_unset=True), scope="user", owner_user_id=uid, owner_project_id=pid
        )
        return {**source, **(await mcp_oauth.detect(source["id"], user_id=uid, project_id=pid))}
    except _EXC + (mcp_oauth.McpOAuthError,) as exc:
        raise _http(exc) from exc


@user_router.patch("/mcp-servers/{item_id}")
async def user_update_mcp(item_id: int, body: McpServerBody, token_info: dict = Depends(get_token_info)):
    uid, pid = _owner(token_info)
    try:
        source = await es.update(
            "mcp", item_id, body.model_dump(exclude_unset=True), requester_user_id=uid, requester_project_id=pid
        )
        if "url" in body.model_fields_set:
            return {**source, **(await mcp_oauth.detect(source["id"], user_id=uid, project_id=pid))}
        return source
    except _EXC + (mcp_oauth.McpOAuthError,) as exc:
        raise _http(exc) from exc


@user_router.delete("/mcp-servers/{item_id}", status_code=204)
async def user_delete_mcp(item_id: int, token_info: dict = Depends(get_token_info)):
    uid, pid = _owner(token_info)
    try:
        await es.delete("mcp", item_id, requester_user_id=uid, requester_project_id=pid)
    except _EXC as exc:
        raise _http(exc) from exc


@user_router.get("/mcp-servers/{item_id}/oauth")
async def user_get_mcp_oauth_status(item_id: int, token_info: dict = Depends(get_token_info)):
    uid, pid = _owner(token_info)
    try:
        return await mcp_oauth.status(item_id, user_id=uid, project_id=pid)
    except _EXC + (mcp_oauth.McpOAuthError,) as exc:
        raise _http(exc) from exc


@user_router.post("/mcp-servers/{item_id}/oauth/start")
async def user_start_mcp_oauth(
    item_id: int, request: Request, response: Response, token_info: dict = Depends(get_token_info)
):
    uid, pid = _owner(token_info)
    initiator_nonce = secrets.token_urlsafe(32)
    try:
        result = await mcp_oauth.begin(
            item_id,
            user_id=uid,
            project_id=pid,
            initiator_nonce=initiator_nonce,
            return_origin=request.headers.get("origin"),
        )
    except _EXC + (mcp_oauth.McpOAuthError,) as exc:
        raise _http(exc) from exc
    response.set_cookie(
        mcp_oauth.INITIATOR_COOKIE,
        initiator_nonce,
        max_age=600,
        path="/api/v1/chat/mcp-oauth/callback",
        secure=mcp_oauth.callback_cookie_secure(),
        httponly=True,
        samesite="lax",
    )
    return result


@user_router.delete("/mcp-servers/{item_id}/oauth", status_code=204)
async def user_disconnect_mcp_oauth(item_id: int, token_info: dict = Depends(get_token_info)):
    uid, pid = _owner(token_info)
    try:
        await mcp_oauth.disconnect(item_id, user_id=uid, project_id=pid)
    except _EXC + (mcp_oauth.McpOAuthError,) as exc:
        raise _http(exc) from exc
    return Response(status_code=204)


@user_router.get("/custom-tools")
async def user_list_tools(token_info: dict = Depends(get_token_info)):
    uid, pid = _owner(token_info)
    # 선택적 기능 목록: 저장소 미가용/데이터 없음은 빈 목록으로 graceful 처리(503 아님).
    try:
        return await es.list_for_user("tool", user_id=uid, project_id=pid)
    except es.ChatStorageUnavailable:
        return []
    except _EXC as exc:
        raise _http(exc) from exc


@user_router.post("/custom-tools", status_code=201)
async def user_create_tool(body: CustomToolBody, token_info: dict = Depends(get_token_info)):
    uid, pid = _owner(token_info)
    try:
        return await es.create(
            "tool", body.model_dump(exclude_unset=True), scope="user", owner_user_id=uid, owner_project_id=pid
        )
    except _EXC as exc:
        raise _http(exc) from exc


@user_router.patch("/custom-tools/{item_id}")
async def user_update_tool(item_id: int, body: CustomToolBody, token_info: dict = Depends(get_token_info)):
    uid, pid = _owner(token_info)
    try:
        return await es.update(
            "tool", item_id, body.model_dump(exclude_unset=True), requester_user_id=uid, requester_project_id=pid
        )
    except _EXC as exc:
        raise _http(exc) from exc


@user_router.delete("/custom-tools/{item_id}", status_code=204)
async def user_delete_tool(item_id: int, token_info: dict = Depends(get_token_info)):
    uid, pid = _owner(token_info)
    try:
        await es.delete("tool", item_id, requester_user_id=uid, requester_project_id=pid)
    except _EXC as exc:
        raise _http(exc) from exc


@user_router.get("/skills")
async def user_list_skills(token_info: dict = Depends(get_token_info)):
    uid, pid = _owner(token_info)
    # 선택적 기능 목록: 저장소 미가용/데이터 없음은 빈 목록으로 graceful 처리(503 아님).
    try:
        return await es.list_for_user("skill", user_id=uid, project_id=pid)
    except es.ChatStorageUnavailable:
        return []
    except _EXC as exc:
        raise _http(exc) from exc


@user_router.post("/skills", status_code=201)
async def user_create_skill(body: SkillBody, token_info: dict = Depends(get_token_info)):
    uid, pid = _owner(token_info)
    try:
        return await es.create(
            "skill", body.model_dump(exclude_unset=True), scope="user", owner_user_id=uid, owner_project_id=pid
        )
    except _EXC as exc:
        raise _http(exc) from exc


@user_router.patch("/skills/{item_id}")
async def user_update_skill(item_id: int, body: SkillBody, token_info: dict = Depends(get_token_info)):
    uid, pid = _owner(token_info)
    try:
        return await es.update(
            "skill", item_id, body.model_dump(exclude_unset=True), requester_user_id=uid, requester_project_id=pid
        )
    except _EXC as exc:
        raise _http(exc) from exc


@user_router.delete("/skills/{item_id}", status_code=204)
async def user_delete_skill(item_id: int, token_info: dict = Depends(get_token_info)):
    uid, pid = _owner(token_info)
    try:
        await es.delete("skill", item_id, requester_user_id=uid, requester_project_id=pid)
    except _EXC as exc:
        raise _http(exc) from exc
