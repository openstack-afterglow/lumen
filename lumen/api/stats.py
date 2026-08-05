"""빌트인 AI 채팅 관리자 통계 — 전 엔드포인트 require_admin, GET 전용(조회).

전체 시스템(+프로젝트 필터)의 사용량을 사용자별·모델별·월별·전체로 집계해 대시보드에 제공한다.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, Query

from lumen.auth import get_os_conn, require_admin
from lumen.services import stats as stats_service

if TYPE_CHECKING:
    import openstack

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_admin)])

_RANGES = ("30d", "90d", "1y", "all")
_BUCKETS = ("5m", "15m", "hour", "day", "month")


def _norm_range(value: str) -> str:
    return value if value in _RANGES else "all"


async def _identity_names(
    conn: openstack.connection.Connection, *, user_ids: set[str], project_ids: set[str]
) -> tuple[dict[str, str], dict[str, str]]:
    """Resolve aggregate identities once; preserve opaque IDs if Keystone is unavailable."""

    def lookup() -> tuple[dict[str, str], dict[str, str]]:
        users: dict[str, str] = {}
        projects: dict[str, str] = {}
        try:
            for user in conn.identity.users():
                if user.id in user_ids and getattr(user, "name", None):
                    users[user.id] = user.name
        except Exception:
            logger.warning("chat statistics user-name lookup failed", exc_info=True)
        try:
            for project in conn.identity.projects():
                if project.id in project_ids and getattr(project, "name", None):
                    projects[project.id] = project.name
        except Exception:
            logger.warning("chat statistics project-name lookup failed", exc_info=True)
        return users, projects

    if not user_ids and not project_ids:
        return {}, {}
    return await asyncio.to_thread(lookup)


@router.get("/admin/stats")
async def get_stats(
    range: str = Query(default="all"),
    project_id: str | None = Query(default=None),
    user_limit: int = Query(default=20, ge=1, le=200),
    bucket: str = Query(default="month"),
    conn: openstack.connection.Connection = Depends(get_os_conn),
):
    """Dashboard aggregates enriched with the matching Keystone display names."""
    rng = _norm_range(range)
    pid = project_id or None
    b = bucket if bucket in _BUCKETS else "month"
    overview, by_model, monthly, series, users, api_keys, projects = await asyncio.gather(
        stats_service.overview(rng, pid),
        stats_service.by_model(rng, pid),
        stats_service.monthly(rng, pid),
        stats_service.timeseries(b, rng, pid),
        stats_service.by_user(rng, pid, limit=user_limit),
        stats_service.by_api_key(rng, pid),
        stats_service.projects_with_usage(rng),
    )
    user_names, project_names = await _identity_names(
        conn,
        user_ids={row["user_id"] for row in users if row.get("user_id")},
        project_ids=set(projects),
    )
    return {
        "range": rng,
        "project_id": pid,
        "bucket": b,
        "overview": overview,
        "by_model": by_model,
        "monthly": monthly,
        "timeseries": series,
        "by_user": [{**row, "user_name": user_names.get(row["user_id"])} for row in users],
        "by_api_key": api_keys,
        "projects": [{"id": project, "name": project_names.get(project)} for project in projects],
    }


@router.get("/admin/stats/timeseries")
async def get_stats_timeseries(
    bucket: str = Query(default="day"),
    range: str = Query(default="30d"),
    project_id: str | None = Query(default=None),
    model_name: str | None = Query(default=None),
):
    """버킷 전환용 시계열(버킷별 × source web/api/system)."""
    b = bucket if bucket in _BUCKETS else "day"
    return {
        "bucket": b,
        "series": await stats_service.timeseries(
            b, _norm_range(range), project_id or None, model_name=model_name or None
        ),
    }


@router.get("/admin/stats/users")
async def get_stats_users(
    range: str = Query(default="all"),
    project_id: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    conn: openstack.connection.Connection = Depends(get_os_conn),
):
    """Paginated user aggregates enriched with display names."""
    rng = _norm_range(range)
    users = await stats_service.by_user(rng, project_id or None, limit=limit, offset=offset)
    user_names, _ = await _identity_names(
        conn, user_ids={row["user_id"] for row in users if row.get("user_id")}, project_ids=set()
    )
    return {"users": [{**row, "user_name": user_names.get(row["user_id"])} for row in users]}
