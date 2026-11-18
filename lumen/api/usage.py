"""Dashboard usage summaries for the verified payer."""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from lumen.auth import Principal, require_scopes
from lumen.models.chat_contracts import UsageRecordPage
from lumen.services import stats as stats_service
from lumen.services.usage import list_usage_records, user_usage_summary

router = APIRouter()

_RANGES = ("30d", "90d", "1y", "all")
_BUCKETS = ("hour", "day", "month")


async def _dashboard(principal: Principal = Depends(require_scopes("usage:read"))) -> Principal:
    if principal["auth_type"] != "keystone":
        raise HTTPException(403, "keystone_session_required")
    return principal


@router.get("/usage")
async def get_chat_usage(token_info: Principal = Depends(_dashboard)):
    return await user_usage_summary(token_info["user_id"], token_info["project_id"], payer=token_info.get("payer"))


@router.get("/usage/timeseries")
async def get_usage_timeseries(
    bucket: str = Query(default="day"),
    range: str = Query(default="30d"),
    token_info: Principal = Depends(_dashboard),
):
    """본인 사용량 시계열 — 시간/일/월 버킷 × source(web/api). 시스템 부담 제외."""
    b = bucket if bucket in _BUCKETS else "day"
    rng = range if range in _RANGES else "30d"
    series = await stats_service.timeseries(
        b, rng, None, user_id=token_info["user_id"], include_system=False, payer=token_info.get("payer"),
    )
    return {"bucket": b, "range": rng, "series": series}


@router.get("/usage/keys")
async def get_usage_keys(
    range: str = Query(default="all"),
    token_info: Principal = Depends(_dashboard),
):
    """본인 API 키별 사용량(source="api")."""
    rng = range if range in _RANGES else "all"
    payer = token_info.get("payer")
    keys = await stats_service.by_api_key(
        rng, None, user_id=token_info["user_id"], payer=payer,
    )
    if payer is not None and payer.domain_id is not None:
        # Domain totals do not grant access to the service-key inventory.
        for key in keys:
            key["name"] = key["key_prefix"] = None
    return {"keys": keys}


@router.get("/usage/records", response_model=UsageRecordPage)
async def get_usage_records(
    limit: int = Query(default=50, ge=1, le=100),
    before_id: int | None = Query(default=None, gt=0),
    source: Literal["web", "api"] | None = Query(default=None),
    token_info: Principal = Depends(_dashboard),
):
    return await list_usage_records(
        user_id=token_info["user_id"],
        source=source,
        before_id=before_id,
        limit=limit,
        payer=token_info.get("payer"),
    )
