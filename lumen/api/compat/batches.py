"""OpenAI-compatible Batch API over the shared Lumen batch coordinator.

Only the endpoints Lumen executes statelessly are accepted; execution is done by
Lumen batch workers at frozen Lumen prices, not by an upstream provider batch.
"""

from __future__ import annotations

import json
import re

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from lumen.api.batches import read_bounded_body
from lumen.api.compat.openai import openai_error_response
from lumen.auth import Principal, ensure_scopes, require_api_key_scopes
from lumen.config import get_settings
from lumen.models.batch_contracts import (
    BATCH_ENDPOINTS,
    OPENAI_BATCH_ID_PREFIX,
    OPENAI_FILE_ID_PREFIX,
    OpenAIBatchCreateRequest,
    OpenAIBatchError,
    OpenAIBatchErrors,
    OpenAIBatchList,
    OpenAIBatchObject,
    OpenAIBatchRequestCounts,
    batch_item_scopes,
    openai_internal_id,
    openai_public_id,
    unix_seconds,
)
from lumen.services import batches as batch_service

router = APIRouter()

_API_KEY_SECURITY = {"security": [{"APIKeyBearer": []}, {"XApiKey": []}]}
_CREATE_BODY = {
    **_API_KEY_SECURITY,
    "requestBody": {
        "required": True,
        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/OpenAIBatchCreateRequest"}}},
    },
}
# Create bodies carry only IDs, endpoint, window and bounded metadata.
_CREATE_BODY_LIMIT = 64 * 1024
_IDEMPOTENCY_KEY = re.compile(r"^[\x20-\x7e]{1,128}$")


def _unavailable() -> JSONResponse | None:
    if get_settings().batch_enabled:
        return None
    return openai_error_response(503, "Batch processing is not enabled", code="batch_unavailable")


def _error(exc: batch_service.BatchError) -> JSONResponse:
    if isinstance(exc, batch_service.BatchNotFound):
        return openai_error_response(404, "No such batch", code=exc.code)
    if isinstance(exc, batch_service.BatchConflict):
        return openai_error_response(409, "Batch request conflicts with its current state", code=exc.code)
    if isinstance(exc, batch_service.BatchInputError):
        status = {"missing_scope": 403, "request_too_large": 413}.get(exc.code, 400)
        type_ = "permission_error" if status == 403 else None
        return openai_error_response(status, f"Invalid batch request: {exc.code}", type_=type_, code=exc.code)
    return openai_error_response(503, "Batch service unavailable", code=exc.code)


def openai_batch(view: batch_service.BatchView) -> OpenAIBatchObject:
    counts = view.counts
    errors = None
    if view.errors:
        errors = OpenAIBatchErrors(data=[
            OpenAIBatchError(code=error["code"], message=error["message"], param=error.get("param"), line=error.get("line"))
            for error in view.errors
        ])
    return OpenAIBatchObject(
        id=openai_public_id(OPENAI_BATCH_ID_PREFIX, view.id),
        endpoint=view.endpoint,
        errors=errors,
        input_file_id=openai_public_id(OPENAI_FILE_ID_PREFIX, view.input_file_id),
        status=view.status,
        output_file_id=openai_public_id(OPENAI_FILE_ID_PREFIX, view.output_file_id) if view.output_file_id else None,
        error_file_id=openai_public_id(OPENAI_FILE_ID_PREFIX, view.error_file_id) if view.error_file_id else None,
        created_at=unix_seconds(view.created_at),
        in_progress_at=unix_seconds(view.in_progress_at),
        expires_at=unix_seconds(view.expires_at),
        finalizing_at=unix_seconds(view.finalizing_at),
        completed_at=unix_seconds(view.completed_at),
        failed_at=unix_seconds(view.failed_at),
        expired_at=unix_seconds(view.expired_at),
        cancelling_at=unix_seconds(view.cancelling_at),
        cancelled_at=unix_seconds(view.cancelled_at),
        # OpenAI-contract views already count cancelled/expired/unknown items as failed.
        request_counts=OpenAIBatchRequestCounts(total=counts["total"], completed=counts["completed"], failed=counts["failed"]),
        metadata=view.metadata,
    )


def _not_found() -> JSONResponse:
    return openai_error_response(404, "No such batch", code="batch_not_found")


@router.post("/batches", response_model=OpenAIBatchObject, openapi_extra=_CREATE_BODY)
async def create_batch(
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    principal: Principal = Depends(require_api_key_scopes("compat:batches:write")),
):
    """Create a batch from an uploaded, scanned `purpose=batch` JSONL file."""
    if (unavailable := _unavailable()) is not None:
        return unavailable
    if idempotency_key is not None and not _IDEMPOTENCY_KEY.fullmatch(idempotency_key):
        return openai_error_response(400, "Idempotency-Key must be 1-128 printable ASCII characters",
                                     code="invalid_idempotency_key")
    try:
        raw = await read_bounded_body(request, _CREATE_BODY_LIMIT)
    except HTTPException as exc:
        return openai_error_response(exc.status_code, "Batch create body is too large", code=str(exc.detail))
    try:
        body = OpenAIBatchCreateRequest.model_validate(json.loads(raw))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return openai_error_response(400, "Request body must be JSON", code="invalid_json")
    except ValidationError as exc:
        first = exc.errors(include_url=False)[0]
        param = ".".join(str(part) for part in first["loc"]) or None
        return JSONResponse(status_code=400, content={"error": {
            "message": first["msg"], "type": "invalid_request_error", "param": param, "code": "invalid_batch_request",
        }})
    missing = sorted(set(batch_item_scopes(BATCH_ENDPOINTS[body.endpoint], contract="openai")) - set(principal["scopes"]))
    if missing:
        return openai_error_response(403, f"API key is missing required scopes: {', '.join(missing)}",
                                     type_="permission_error", code="missing_scope")
    try:
        view = await batch_service.create_openai_batch(
            project_id=principal["project_id"],
            user_id=principal["user_id"],
            api_key_id=principal.get("api_key_id"),
            scopes=tuple(principal["scopes"]),
            source=principal.get("source", "api"),
            request=body,
            idempotency_key=idempotency_key,
        )
    except batch_service.BatchError as exc:
        return _error(exc)
    return openai_batch(view)


@router.get("/batches", response_model=OpenAIBatchList, openapi_extra=_API_KEY_SECURITY)
async def list_batches(
    after: str | None = None,
    limit: int = 20,
    principal: Principal = Depends(require_api_key_scopes("compat:batches:read")),
):
    if (unavailable := _unavailable()) is not None:
        return unavailable
    if not 1 <= limit <= 100:
        return openai_error_response(400, "limit must be between 1 and 100", code="invalid_limit")
    cursor = None
    if after is not None and (cursor := openai_internal_id(OPENAI_BATCH_ID_PREFIX, after)) is None:
        return openai_error_response(400, "after must be a batch ID", code="invalid_cursor")
    try:
        views, has_more = await batch_service.list_batches(
            project_id=principal["project_id"], user_id=principal["user_id"], contract="openai", after=cursor, limit=limit,
        )
    except batch_service.BatchError as exc:
        return _error(exc)
    data = [openai_batch(view) for view in views]
    return OpenAIBatchList(
        data=data,
        first_id=data[0].id if data else None,
        last_id=data[-1].id if data else None,
        has_more=has_more,
    )


@router.get("/batches/{batch_id}", response_model=OpenAIBatchObject, openapi_extra=_API_KEY_SECURITY)
async def retrieve_batch(
    batch_id: str,
    principal: Principal = Depends(require_api_key_scopes("compat:batches:read")),
):
    if (unavailable := _unavailable()) is not None:
        return unavailable
    if (internal_id := openai_internal_id(OPENAI_BATCH_ID_PREFIX, batch_id)) is None:
        return _not_found()
    try:
        view = await batch_service.get_batch(
            project_id=principal["project_id"], user_id=principal["user_id"], batch_id=internal_id, contract="openai",
        )
    except batch_service.BatchError as exc:
        return _error(exc)
    return openai_batch(view)


@router.post("/batches/{batch_id}/cancel", response_model=OpenAIBatchObject, openapi_extra=_API_KEY_SECURITY)
async def cancel_batch(
    batch_id: str,
    principal: Principal = Depends(require_api_key_scopes("compat:batches:write")),
):
    """Cancel: status becomes `cancelling` until running items return or the grace window ends."""
    if (unavailable := _unavailable()) is not None:
        return unavailable
    if (internal_id := openai_internal_id(OPENAI_BATCH_ID_PREFIX, batch_id)) is None:
        return _not_found()
    try:
        scopes = await batch_service.owned_cancel_scopes(
            project_id=principal["project_id"], user_id=principal["user_id"], batch_id=internal_id, contract="openai",
        )
        ensure_scopes(principal, *scopes)
        view = await batch_service.cancel_batch(
            project_id=principal["project_id"], user_id=principal["user_id"], batch_id=internal_id, contract="openai",
        )
    except batch_service.BatchError as exc:
        return _error(exc)
    return openai_batch(view)
