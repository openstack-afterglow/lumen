"""Authenticated routes for the scanned chat asset pipeline."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from lumen.auth import get_token_info
from lumen.services import assets
from lumen.services.conversation_store import ChatStorageUnavailable

router = APIRouter()
_MAX_UPLOAD_BYTES = 100 * 1024 * 1024


class AssetResponse(BaseModel):
    id: str
    name: str
    mime_type: str
    size_bytes: int
    sha256: str
    status: str
    media_metadata: dict = Field(default_factory=dict)
    created_at: str | None = None


def _map_error(exc: Exception) -> HTTPException:
    if isinstance(exc, assets.AssetUnavailable):
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, ChatStorageUnavailable):
        return HTTPException(status_code=503, detail="chat asset storage is unavailable")
    if isinstance(exc, assets.AssetError):
        status = 403 if str(exc) == "asset forbidden" else 404 if str(exc) == "asset not found" else 422
        return HTTPException(status_code=status, detail=str(exc))
    return HTTPException(status_code=502, detail="chat asset pipeline failed")


async def _spool_upload(upload: UploadFile) -> Path:
    fd, name = tempfile.mkstemp(prefix="afterglow-chat-asset-", suffix=".upload")
    total = 0
    try:
        with os.fdopen(fd, "wb") as target:
            while chunk := await upload.read(64 * 1024):
                total += len(chunk)
                if total > _MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="파일 크기가 제한을 초과했습니다")
                target.write(chunk)
        return Path(name)
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise


@router.post("/assets", response_model=AssetResponse, status_code=201)
async def upload_asset(file: UploadFile = File(...), token_info: dict = Depends(get_token_info)):
    path = await _spool_upload(file)
    try:
        result = await assets.create_uploaded_asset(
            path=path,
            original_name=file.filename or "asset",
            user_id=token_info["user_id"],
            project_id=token_info["project_id"],
        )
        return result
    except Exception as exc:
        raise _map_error(exc) from None
    finally:
        path.unlink(missing_ok=True)
        await file.close()


@router.get("/assets/{asset_id}/download", status_code=307)
async def download_asset(asset_id: UUID, token_info: dict = Depends(get_token_info)):
    try:
        url = await assets.signed_download_url(
            asset_id=str(asset_id), user_id=token_info["user_id"], project_id=token_info["project_id"]
        )
        return RedirectResponse(url=url, status_code=307)
    except Exception as exc:
        raise _map_error(exc) from None


@router.get("/assets/{asset_id}", response_model=AssetResponse)
async def get_asset(asset_id: UUID, token_info: dict = Depends(get_token_info)):
    try:
        return await assets.get_asset(
            asset_id=str(asset_id), user_id=token_info["user_id"], project_id=token_info["project_id"]
        )
    except Exception as exc:
        raise _map_error(exc) from None


@router.delete("/assets/{asset_id}", status_code=202)
async def delete_asset(asset_id: UUID, token_info: dict = Depends(get_token_info)):
    try:
        return await assets.delete_asset(
            asset_id=str(asset_id), user_id=token_info["user_id"], project_id=token_info["project_id"]
        )
    except Exception as exc:
        raise _map_error(exc) from None
