"""Native durable image admission. Image bytes always enter through canonical assets."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, status

from lumen.auth import Principal, require_scopes
from lumen.models.api_requests import ImageEditRequest, ImageGenerationRequest
from lumen.models.chat_contracts import ChatRunDescriptor
from lumen.services import assets, credit
from lumen.services.conversation_store import ChatStorageUnavailable
from lumen.services.durable_runs import errors as durable_errors
from lumen.services.durable_runs.images import admit_image_run
from lumen.services.providers.errors import ProviderValidationError

router = APIRouter()


def image_error(exc: Exception) -> HTTPException:
    if isinstance(exc, durable_errors.DurableRunConflict):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, durable_errors.DurableRunInputError):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, durable_errors.DurableRunNotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, ProviderValidationError):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, credit.QuotaExceeded):
        return HTTPException(status_code=402, detail=str(exc))
    if isinstance(exc, assets.AssetUnavailable):
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, assets.AssetError):
        code = 403 if str(exc) == "asset forbidden" else 404 if str(exc) == "asset not found" else 422
        return HTTPException(status_code=code, detail=str(exc))
    if isinstance(exc, (durable_errors.DurableRunError, ChatStorageUnavailable)):
        return HTTPException(status_code=503, detail=str(exc))
    return HTTPException(status_code=503, detail="image service unavailable")


async def admit(
    payload: ImageGenerationRequest,
    *,
    principal: Principal,
    idempotency_key: UUID,
    input_asset_id: UUID | None = None,
    api_provider: str | None = None,
    required_scopes: tuple[str, ...] | None = None,
) -> ChatRunDescriptor:
    request = payload.model_dump(mode="json")
    if api_provider is not None:
        request["provider_id"] = api_provider
    if input_asset_id is not None:
        request.pop("input_asset_id", None)
        request["source_asset_id"] = str(input_asset_id)
    try:
        return await admit_image_run(
            request,
            project_id=principal["project_id"],
            user_id=principal["user_id"],
            client_request_id=str(idempotency_key),
            source=principal.get("source", "web"),
            api_key_id=principal.get("api_key_id"),
            required_scopes=required_scopes,
        )
    except (
        durable_errors.DurableRunError,
        assets.AssetError,
        assets.AssetUnavailable,
        ChatStorageUnavailable,
        credit.QuotaExceeded,
        ProviderValidationError,
    ) as exc:
        raise image_error(exc) from exc


@router.post("/chat/images/generations", response_model=ChatRunDescriptor, status_code=status.HTTP_202_ACCEPTED)
async def generate_image(
    payload: ImageGenerationRequest,
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
    principal: Principal = Depends(require_scopes("native:images:write")),
):
    return await admit(payload, principal=principal, idempotency_key=idempotency_key)


@router.post("/chat/images/edits", response_model=ChatRunDescriptor, status_code=status.HTTP_202_ACCEPTED)
async def edit_image(
    payload: ImageEditRequest,
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
    principal: Principal = Depends(require_scopes("native:images:write", "native:assets:read")),
):
    return await admit(payload, principal=principal, idempotency_key=idempotency_key, input_asset_id=payload.input_asset_id)
