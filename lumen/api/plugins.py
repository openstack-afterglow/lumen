"""Native installed-plugin catalogue; operator authority never derives from API keys."""
from __future__ import annotations

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from lumen_plugin_api.contracts import Namespace, PluginError
from pydantic import BaseModel, ConfigDict, Field, field_validator

from lumen.auth import require_admin, require_scopes
from lumen.plugins import bindings
from lumen.plugins.registry import get_registry
from lumen.services.extensions_store import (
    ChatStorageUnavailable,
    ExtensionForbidden,
    ExtensionNotFound,
    ExtensionValidationError,
)

router = APIRouter()
admin_router = APIRouter(dependencies=[Depends(require_admin)])


class BindingCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["tool", "skill"]
    plugin_id: str = Field(min_length=1, max_length=190)
    export_key: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=190)
    config: dict = Field(default_factory=dict)
    is_active: bool = True


class BindingPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=190)
    config: dict = Field(default_factory=dict)
    is_active: bool = True

    @field_validator("name")
    @classmethod
    def require_non_null_name(cls, name: str | None) -> str:
        if name is None:
            raise ValueError("binding name cannot be null")
        return name


def _namespace(principal) -> Namespace:
    return Namespace(user_id=principal["user_id"], project_id=principal["project_id"])


def _http(exc: Exception) -> HTTPException:
    if isinstance(exc, PluginError):
        return HTTPException(status_code=503 if exc.code in {"plugin_unavailable", "plugin_incompatible"} else 422, detail=exc.code)
    if isinstance(exc, ExtensionForbidden):
        return HTTPException(status_code=403, detail="plugin binding is not accessible")
    if isinstance(exc, ExtensionNotFound):
        return HTTPException(status_code=404, detail="plugin binding not found")
    if isinstance(exc, ExtensionValidationError):
        return HTTPException(status_code=422, detail="plugin binding configuration is invalid")
    return HTTPException(status_code=503, detail="plugin storage is unavailable")


_ERRORS = (PluginError, ExtensionForbidden, ExtensionNotFound, ExtensionValidationError, ChatStorageUnavailable)


@admin_router.get("/admin/plugins")
async def installed_plugins():
    try:
        return get_registry().status()
    except PluginError as exc:
        raise _http(exc) from exc


@admin_router.get("/admin/plugin-bindings")
async def admin_catalogue():
    try:
        return await bindings.list_bindings(None, admin=True)
    except _ERRORS as exc:
        raise _http(exc) from exc


@admin_router.post("/admin/plugin-bindings", status_code=201)
async def admin_create(body: BindingCreate):
    try:
        return await bindings.create_binding(**body.model_dump(), namespace=None, admin=True)
    except _ERRORS as exc:
        raise _http(exc) from exc


@admin_router.patch("/admin/plugin-bindings/{identifier}")
async def admin_update(identifier: UUID, body: BindingPatch):
    try:
        return await bindings.update_binding(str(identifier), patch=body.model_dump(exclude_unset=True), namespace=None, admin=True)
    except _ERRORS as exc:
        raise _http(exc) from exc


@admin_router.delete("/admin/plugin-bindings/{identifier}", status_code=204)
async def admin_delete(identifier: UUID):
    try:
        await bindings.update_binding(str(identifier), patch={}, namespace=None, admin=True, delete=True)
        return Response(status_code=204)
    except _ERRORS as exc:
        raise _http(exc) from exc


@router.get("/plugin-bindings")
async def catalogue(principal=Depends(require_scopes("native:extensions:read"))):
    try:
        return await bindings.list_bindings(_namespace(principal))
    except _ERRORS as exc:
        raise _http(exc) from exc


@router.post("/plugin-bindings", status_code=201)
async def create(body: BindingCreate, principal=Depends(require_scopes("native:extensions:write"))):
    try:
        return await bindings.create_binding(**body.model_dump(), namespace=_namespace(principal), admin=False)
    except _ERRORS as exc:
        raise _http(exc) from exc


@router.patch("/plugin-bindings/{identifier}")
async def update(identifier: UUID, body: BindingPatch, principal=Depends(require_scopes("native:extensions:write"))):
    try:
        return await bindings.update_binding(str(identifier), patch=body.model_dump(exclude_unset=True), namespace=_namespace(principal), admin=False)
    except _ERRORS as exc:
        raise _http(exc) from exc


@router.delete("/plugin-bindings/{identifier}", status_code=204)
async def delete(identifier: UUID, principal=Depends(require_scopes("native:extensions:write"))):
    try:
        await bindings.update_binding(str(identifier), patch={}, namespace=_namespace(principal), admin=False, delete=True)
        return Response(status_code=204)
    except _ERRORS as exc:
        raise _http(exc) from exc
