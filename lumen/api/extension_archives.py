"""Dashboard-only archive inventory and recovery."""

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError, OperationalError

from lumen.auth import require_scopes
from lumen.services import extension_archive, mcp_settings
from lumen.services.domain_scope import history_domain_id
from lumen.services.extensions_store import ExtensionForbidden

router = APIRouter()
_read = require_scopes("native:extensions:read")
_write = require_scopes("native:extensions:write")


class ArchivedConnection(BaseModel):
    id: int
    kind: Literal["mcp", "tool"]
    original_id: int
    canonical_id: int
    name: str
    archived_at: datetime


class ArchiveList(BaseModel):
    items: list[ArchivedConnection]


class RestoredConnection(BaseModel):
    kind: Literal["mcp", "tool"]
    id: int


async def _archive_owner(principal: dict, domain_id: str | None) -> tuple[str | None, str]:
    selected_domain = history_domain_id(principal)
    if selected_domain is None:
        raise HTTPException(403, "Unrestricted Keystone domain session required")
    try:
        if domain_id is not None:
            await mcp_settings.authority(principal, domain_id, manage=True)
            return None, domain_id
        await mcp_settings.authority(principal, selected_domain)
        return principal["user_id"], selected_domain
    except ExtensionForbidden as exc:
        raise HTTPException(403, str(exc)) from exc


async def _list(principal: dict, domain_id: str | None, limit: int, before_id: int | None) -> dict:
    user_id, target = await _archive_owner(principal, domain_id)
    try:
        return {"items": await extension_archive.list_archives(
            user_id=user_id, domain_id=target, limit=limit, before_id=before_id,
        )}
    except (extension_archive.ArchiveStorageUnavailable, OperationalError) as exc:
        raise HTTPException(503, "Chat storage unavailable") from exc


async def _restore(principal: dict, archive_id: int, domain_id: str | None) -> dict:
    user_id, target = await _archive_owner(principal, domain_id)
    try:
        return await extension_archive.restore_archive(archive_id=archive_id, user_id=user_id, domain_id=target)
    except extension_archive.ArchiveNotFound as exc:
        raise HTTPException(404, "Archived connection not found") from exc
    except (extension_archive.ArchiveStorageUnavailable, OperationalError) as exc:
        raise HTTPException(503, "Chat storage unavailable") from exc
    except IntegrityError as exc:
        raise HTTPException(409, "Restoring this connection would conflict with a live connection") from exc


@router.get("/archived-connections", response_model=ArchiveList)
async def list_personal_archives(
    limit: int = Query(100, ge=1, le=200), before_id: int | None = Query(None, ge=1), principal: dict = Depends(_read),
):
    return await _list(principal, None, limit, before_id)


@router.post("/archived-connections/{archive_id}/restore", response_model=RestoredConnection)
async def restore_personal_archive(archive_id: int, principal: dict = Depends(_write)):
    return await _restore(principal, archive_id, None)


@router.get("/domains/{domain_id}/archived-connections", response_model=ArchiveList)
async def list_domain_archives(
    domain_id: str, limit: int = Query(100, ge=1, le=200), before_id: int | None = Query(None, ge=1),
    principal: dict = Depends(_read),
):
    return await _list(principal, domain_id, limit, before_id)


@router.post("/domains/{domain_id}/archived-connections/{archive_id}/restore", response_model=RestoredConnection)
async def restore_domain_archive(domain_id: str, archive_id: int, principal: dict = Depends(_write)):
    return await _restore(principal, archive_id, domain_id)
