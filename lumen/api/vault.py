"""Agent Vault REST access for authenticated dashboard users."""

from dataclasses import replace
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import OperationalError

from lumen.auth import ensure_scopes, get_principal
from lumen.services import memory_store
from lumen.services import vault_store as vs


class VaultRoute(APIRoute):
    def get_route_handler(self):
        handle = super().get_route_handler()

        async def translate(request):
            try:
                response = await handle(request)
                response.headers["Cache-Control"] = "no-store"
                return response
            except vs.VaultError as exc:
                raise HTTPException(exc.status, exc.code) from exc
            except RequestValidationError as exc:
                raise HTTPException(422, "invalid_vault_arguments") from exc
            except (OperationalError, memory_store.ChatStorageUnavailable) as exc:
                raise HTTPException(503, "vault_unavailable") from exc

        return translate


router = APIRouter(prefix="/vault", route_class=VaultRoute)


async def dashboard_principal(principal: dict = Depends(get_principal)) -> dict:
    if vs.human_domain(principal) is None:
        raise HTTPException(403, "vault_dashboard_required")
    return principal


def _access(action: str):
    def resolve(principal: dict = Depends(dashboard_principal)) -> vs.VaultScope:
        ensure_scopes(principal, f"native:memory:{action}")
        return vs.VaultScope(principal["user_id"], principal["domain_id"], principal["project_id"])

    return resolve


CanRead = Annotated[vs.VaultScope, Depends(_access("read"))]
CanWrite = Annotated[vs.VaultScope, Depends(_access("write"))]
ScopeName = Literal["domain", "global"]
ProjectKey = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{0,79}$")]
Slug = Annotated[str, Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")]
Content = Annotated[str, Field(min_length=1, max_length=100_000)]
Title = Annotated[str, Field(min_length=1, max_length=200)]
ExpectedHash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
WorkspaceQuery = Annotated[int | None, Query(gt=0, le=2**63 - 1)]


class NoteWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: vs.VaultKind
    slug: Slug
    title: Title
    content: Content
    mode: Literal["create", "append", "replace"] = "create"
    expectedHash: ExpectedHash | None = None
    scope: ScopeName = "domain"
    project_key: ProjectKey | None = None
    role: vs.Role | None = None
    metadata: dict[str, Any] | None = None
    workspace_id: int | None = Field(default=None, gt=0, le=2**63 - 1)


class NoteReplace(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: Content
    title: Title | None = None
    expectedHash: ExpectedHash


class SlotWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: Content
    expectedHash: ExpectedHash | None = None


class Filters(BaseModel):
    kind: vs.VaultKind | None = None
    project_key: ProjectKey | None = None
    role: vs.Role | None = None
    scope: ScopeName | None = None
    limit: int = Field(default=50, ge=1, le=100)
    before: int | None = Field(default=None, gt=0)
    workspace_id: int | None = Field(default=None, gt=0, le=2**63 - 1)

    def store_args(self) -> dict:
        fields = self.model_dump(exclude={"scope", "workspace_id"})
        if self.scope:
            fields["note_scope"] = self.scope
        return fields


@router.get("/notes")
async def notes(access: CanRead, filters: Annotated[Filters, Query()]):
    return await vs.list_notes(replace(access, workspace_id=filters.workspace_id), **filters.store_args())


class SearchFilters(Filters):
    query: str = Field(min_length=1, max_length=4000)


@router.get("/search")
async def search(access: CanRead, filters: Annotated[SearchFilters, Query()]):
    return await vs.list_notes(replace(access, workspace_id=filters.workspace_id), **filters.store_args())


@router.post("/notes", status_code=201)
async def write_note(access: CanWrite, payload: NoteWrite):
    return await vs.put_note(
        replace(access, workspace_id=payload.workspace_id),
        **payload.model_dump(exclude={"scope", "expectedHash", "workspace_id"}),
        note_scope=payload.scope, expected_hash=payload.expectedHash,
    )


@router.get("/notes/{note_id}")
async def read_note(access: CanRead, note_id: int, workspace_id: WorkspaceQuery = None):
    return await vs.read_note(replace(access, workspace_id=workspace_id), note_id)


@router.patch("/notes/{note_id}")
async def replace_note(access: CanWrite, note_id: int, payload: NoteReplace, workspace_id: WorkspaceQuery = None):
    return await vs.replace_note(replace(access, workspace_id=workspace_id), note_id, content=payload.content,
                                 title=payload.title, expected_hash=payload.expectedHash)


@router.get("/notes/{note_id}/revisions")
async def revisions(access: CanRead, note_id: int, limit: Annotated[int, Query(ge=1, le=100)] = 50,
                    before: Annotated[int | None, Query(gt=0)] = None, workspace_id: WorkspaceQuery = None):
    return await vs.revisions(replace(access, workspace_id=workspace_id), note_id, limit=limit, before=before)


@router.get("/document")
async def document(access: CanRead):
    rows = await memory_store.list_memories(user_id=access.user_id, project_id=access.project_id,
                                            domain_id=access.domain_id)
    return {"filename": "memory.md", "content_type": "text/markdown",
            "content": memory_store.render_memory_markdown(rows)}


@router.get("/short-term")
async def short_term(access: CanRead, scope: ScopeName = "domain", project_key: ProjectKey | None = None,
                     role: vs.Role | None = None):
    return await vs.read_slot(access, kind="short_term", slug="memory", note_scope=scope,
                              project_key=project_key, role=role)


@router.put("/short-term")
async def write_short_term(access: CanWrite, payload: SlotWrite, scope: ScopeName = "domain",
                           project_key: ProjectKey | None = None, role: vs.Role | None = None):
    return await vs.put_slot(access, kind="short_term", slug="memory", title="Short-term memory",
                             note_scope=scope, project_key=project_key, role=role,
                             content=payload.content, expected_hash=payload.expectedHash)


@router.get("/profile")
async def profile(access: CanRead):
    return await vs.read_slot(access, kind="profile", slug="profile", note_scope="global")


@router.put("/profile")
async def write_profile(access: CanWrite, payload: SlotWrite):
    return await vs.put_slot(access, kind="profile", slug="profile", title="Profile", note_scope="global",
                             content=payload.content, expected_hash=payload.expectedHash)


@router.get("/coding-standards")
async def coding_standards(access: CanRead):
    return await vs.read_slot(access, kind="coding_standards", slug="coding-standards", note_scope="global")


@router.put("/coding-standards")
async def write_coding_standards(access: CanWrite, payload: SlotWrite):
    return await vs.put_slot(access, kind="coding_standards", slug="coding-standards", title="Coding standards",
                             note_scope="global", content=payload.content, expected_hash=payload.expectedHash)
