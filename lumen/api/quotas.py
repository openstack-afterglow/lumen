from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, Field

from lumen.auth import require_admin
from lumen.services import credit

router = APIRouter(dependencies=[Depends(require_admin)])


class UserQuotaBody(BaseModel):
    monthly_credit_limit: Decimal | None = Field(
        ...,
        gt=0,
        max_digits=18,
        decimal_places=8,
    )
    weekly_credit_limit: Decimal | None = Field(
        ...,
        gt=0,
        max_digits=18,
        decimal_places=8,
    )


class DefaultQuotaBody(BaseModel):
    monthly_credit_limit: Decimal | None = Field(
        ...,
        gt=0,
        max_digits=18,
        decimal_places=8,
    )

    model_config = {"extra": "forbid"}


def _storage_unavailable(exc: credit.ChatStorageUnavailable) -> HTTPException:
    return HTTPException(status_code=503, detail="chat DB 를 사용할 수 없습니다")


@router.get("/admin/quotas")
async def list_user_quotas(
    user_id: str | None = Query(default=None, min_length=1, max_length=64),
):
    try:
        return await credit.list_user_quotas(user_id)
    except credit.ChatStorageUnavailable as exc:
        raise _storage_unavailable(exc) from exc


@router.put("/admin/quotas/defaults")
async def set_default_quota(body: DefaultQuotaBody):
    try:
        return await credit.set_default_monthly_quota(body.monthly_credit_limit)
    except credit.QuotaLimitConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except credit.ChatStorageUnavailable as exc:
        raise _storage_unavailable(exc) from exc


@router.put("/admin/quotas/{user_id}")
async def set_user_quota(
    body: UserQuotaBody,
    user_id: str = Path(min_length=1, max_length=64),
):
    try:
        return await credit.set_user_quota(
            user_id,
            monthly_credit_limit=body.monthly_credit_limit,
            weekly_credit_limit=body.weekly_credit_limit,
        )
    except credit.ChatStorageUnavailable as exc:
        raise _storage_unavailable(exc) from exc
    except credit.QuotaLimitConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.delete("/admin/quotas/{user_id}")
async def reset_user_quota(
    user_id: str = Path(min_length=1, max_length=64),
):
    try:
        return await credit.reset_user_quota(user_id)
    except credit.ChatStorageUnavailable as exc:
        raise _storage_unavailable(exc) from exc
