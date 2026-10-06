"""Native mixed multimodal Batch API.

Routes only authenticate, check scopes, bound/parse input and map the shared batch
coordinator's views; validation, materialization and execution live in
``lumen.services.batches`` and the durable workers.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError

from lumen.auth import Principal, ensure_scopes, require_scopes
from lumen.config import get_settings
from lumen.models.batch_contracts import (
    BatchRequestCounts,
    NativeBatchCreateRequest,
    NativeBatchDescriptor,
    NativeBatchItemPage,
    NativeBatchItemResponse,
    NativeBatchPage,
    batch_item_scopes,
)
from lumen.services import batches as batch_service

router = APIRouter()

# Printable ASCII (RFC 9110 field values cannot carry control characters).
IDEMPOTENCY_KEY_PATTERN = r"^[\x20-\x7e]{1,128}$"
_NATIVE_BATCH_BODY = {
    "requestBody": {
        "required": True,
        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/NativeBatchCreateRequest"}}},
    },
}


def batch_enabled_or_503() -> None:
    if not get_settings().batch_enabled:
        raise HTTPException(status_code=503, detail="batch_unavailable")


async def read_bounded_body(request: Request, limit: int) -> bytes:
    """Reject oversized bodies from Content-Length, then enforce the cap while streaming."""
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            declared_bytes = int(declared)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid_content_length") from exc
        if declared_bytes > limit:
            raise HTTPException(status_code=413, detail="request_too_large")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise HTTPException(status_code=413, detail="request_too_large")
    return bytes(body)


def batch_http_error(exc: batch_service.BatchError) -> HTTPException:
    if isinstance(exc, batch_service.BatchNotFound):
        return HTTPException(status_code=404, detail=exc.code)
    if isinstance(exc, batch_service.BatchConflict):
        return HTTPException(status_code=409, detail=exc.code)
    if isinstance(exc, batch_service.BatchInputError):
        code = {"missing_scope": 403, "request_too_large": 413}.get(exc.code, 422)
        return HTTPException(status_code=code, detail=exc.code)
    return HTTPException(status_code=503, detail=exc.code)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _scopes(principal: Principal) -> tuple[str, ...] | None:
    return None if principal["auth_type"] == "keystone" else tuple(principal["scopes"])


def _batch_uuid(batch_id: str) -> str:
    try:
        return str(UUID(batch_id))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="batch_not_found") from exc


def native_descriptor(view: batch_service.BatchView) -> NativeBatchDescriptor:
    base = f"/v1/chat/batches/{view.id}"
    return NativeBatchDescriptor(
        id=view.id,
        status=view.status,
        created_at=_utc(view.created_at),
        expires_at=_utc(view.expires_at),
        request_counts=BatchRequestCounts(**view.counts),
        metadata=view.metadata,
        links={"self": base, "items": f"{base}/items", "cancel": f"{base}/cancel"},
        errors=view.errors,
    )


def native_item(view: batch_service.BatchItemView) -> NativeBatchItemResponse:
    return NativeBatchItemResponse(
        custom_id=view.custom_id,
        ordinal=view.ordinal,
        operation=view.operation,
        run_id=view.run_id,
        status=view.status,
        response=view.response,
        error=view.error,
        settlement_status=view.settlement_status,
    )


def _parse_create(raw: bytes, *, max_items: int) -> NativeBatchCreateRequest:
    try:
        data = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail="invalid_json") from exc
    # Bound the item list before validating every body.
    if isinstance(data, dict) and isinstance(data.get("items"), list) and len(data["items"]) > max_items:
        raise HTTPException(status_code=422, detail="too_many_items")
    try:
        return NativeBatchCreateRequest.model_validate(data)
    except ValidationError as exc:
        raise RequestValidationError(
            [{**error, "loc": ("body", *error["loc"])} for error in exc.errors(include_url=False)]
        ) from exc


@router.post(
    "/chat/batches",
    response_model=NativeBatchDescriptor,
    status_code=status.HTTP_202_ACCEPTED,
    openapi_extra=_NATIVE_BATCH_BODY,
)
async def create_batch(
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", pattern=IDEMPOTENCY_KEY_PATTERN)],
    principal: Principal = Depends(require_scopes("native:batches:write")),
):
    """Accept a mixed batch; validation and execution continue asynchronously."""
    batch_enabled_or_503()
    settings = get_settings()
    payload = _parse_create(
        await read_bounded_body(request, settings.batch_native_max_bytes),
        max_items=settings.batch_native_max_items,
    )
    item_scopes = {scope for item in payload.items for scope in batch_item_scopes(item.operation, contract="native")}
    ensure_scopes(principal, *item_scopes)
    try:
        view, _created = await batch_service.create_native_batch(
            project_id=principal["project_id"],
            user_id=principal["user_id"],
            api_key_id=principal.get("api_key_id"),
            scopes=_scopes(principal),
            source=principal.get("source", "web"),
            idempotency_key=idempotency_key,
            request=payload,
        )
    except batch_service.BatchError as exc:
        raise batch_http_error(exc) from exc
    return native_descriptor(view)


@router.get("/chat/batches", response_model=NativeBatchPage)
async def list_batches(
    after: UUID | None = Query(default=None, description="Exclusive cursor: the previous page's next_cursor"),
    limit: int = Query(default=20, ge=1, le=100),
    principal: Principal = Depends(require_scopes("native:batches:read")),
):
    batch_enabled_or_503()
    try:
        views, has_more = await batch_service.list_batches(
            project_id=principal["project_id"],
            user_id=principal["user_id"],
            contract="native",
            after=str(after) if after is not None else None,
            limit=limit,
        )
    except batch_service.BatchError as exc:
        raise batch_http_error(exc) from exc
    return NativeBatchPage(
        batches=[native_descriptor(view) for view in views],
        next_cursor=views[-1].id if has_more and views else None,
    )


@router.get("/chat/batches/{batch_id}", response_model=NativeBatchDescriptor)
async def get_batch(batch_id: str, principal: Principal = Depends(require_scopes("native:batches:read"))):
    batch_enabled_or_503()
    try:
        view = await batch_service.get_batch(
            project_id=principal["project_id"],
            user_id=principal["user_id"],
            batch_id=_batch_uuid(batch_id),
            contract="native",
        )
    except batch_service.BatchError as exc:
        raise batch_http_error(exc) from exc
    return native_descriptor(view)


@router.get("/chat/batches/{batch_id}/items", response_model=NativeBatchItemPage)
async def list_batch_items(
    batch_id: str,
    after: int | None = Query(default=None, ge=0, description="Exclusive ordinal cursor"),
    limit: int = Query(default=100, ge=1, le=1000),
    principal: Principal = Depends(require_scopes("native:batches:read")),
):
    batch_enabled_or_503()
    try:
        items, next_cursor = await batch_service.list_batch_items(
            project_id=principal["project_id"],
            user_id=principal["user_id"],
            batch_id=_batch_uuid(batch_id),
            after=after,
            limit=limit,
        )
    except batch_service.BatchError as exc:
        raise batch_http_error(exc) from exc
    return NativeBatchItemPage(items=[native_item(item) for item in items], next_cursor=next_cursor)


@router.post(
    "/chat/batches/{batch_id}/cancel",
    response_model=NativeBatchDescriptor,
    status_code=status.HTTP_202_ACCEPTED,
)
async def cancel_batch(batch_id: str, principal: Principal = Depends(require_scopes("native:batches:write"))):
    """Record durable cancel intent; running items get the bounded result-recovery grace."""
    batch_enabled_or_503()
    try:
        view = await batch_service.cancel_batch(
            project_id=principal["project_id"],
            user_id=principal["user_id"],
            batch_id=_batch_uuid(batch_id),
            contract="native",
        )
    except batch_service.BatchError as exc:
        raise batch_http_error(exc) from exc
    return native_descriptor(view)
