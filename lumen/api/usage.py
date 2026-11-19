"""Dashboard usage for a Keystone session.

The default view is the caller's own usage across every payer. ``scope=domain`` shows the domain
pool's usage and requires live domain-manager authority; other members' identifiers stay masked.
"""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from lumen.api.domain_pools import require_domain_manager
from lumen.auth import Principal, require_scopes
from lumen.models.chat_contracts import UsageRecordPage
from lumen.services import stats as stats_service
from lumen.services.usage import UsageView, list_usage_records, user_usage_summary

router = APIRouter()

_RANGES = ("30d", "90d", "1y", "all")
_BUCKETS = ("hour", "day", "month")
Scope = Literal["self", "domain"]


async def _dashboard(principal: Principal = Depends(require_scopes("usage:read"))) -> Principal:
    if principal["auth_type"] != "keystone":
        raise HTTPException(403, "keystone_session_required")
    return principal


async def _view(scope: Scope = Query(default="self"), principal: Principal = Depends(_dashboard)) -> UsageView:
    if scope == "domain":
        return UsageView(principal["user_id"], await require_domain_manager(principal))
    return UsageView(principal["user_id"])


@router.get("/usage")
async def get_chat_usage(view: UsageView = Depends(_view)):
    return await user_usage_summary(view)


@router.get("/usage/timeseries")
async def get_usage_timeseries(
    bucket: str = Query(default="day"),
    range: str = Query(default="30d"),
    view: UsageView = Depends(_view),
):
    """사용량 시계열 — 시간/일/월 버킷 × source(web/api). 시스템 부담 제외."""
    b = bucket if bucket in _BUCKETS else "day"
    rng = range if range in _RANGES else "30d"
    series = await stats_service.timeseries(b, rng, None, include_system=False, view=view)
    return {"bucket": b, "range": rng, "view": view.scope, "series": series}


@router.get("/usage/keys")
async def get_usage_keys(
    range: str = Query(default="all"),
    view: UsageView = Depends(_view),
):
    """API 키별 사용량(source="api")."""
    rng = range if range in _RANGES else "all"
    keys = await stats_service.by_api_key(rng, None, view=view)
    if view.domain_id is not None:
        # Domain totals do not grant access to the service-key inventory.
        for key in keys:
            key["name"] = key["key_prefix"] = None
    return {"view": view.scope, "keys": keys}


@router.get("/usage/records", response_model=UsageRecordPage)
async def get_usage_records(
    limit: int = Query(default=50, ge=1, le=100),
    before_id: int | None = Query(default=None, gt=0),
    source: Literal["web", "api"] | None = Query(default=None),
    view: UsageView = Depends(_view),
):
    return await list_usage_records(view=view, source=source, before_id=before_id, limit=limit)
