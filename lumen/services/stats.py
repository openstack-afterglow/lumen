"""빌트인 AI 채팅 관리자 통계 — chat_usage_logs 원장 집계.

전체 시스템(+프로젝트 필터) 사용량을 사용자별·모델별·월별·전체로 집계한다.
저장소 장애 시에도 빈 집계로 안전 반환(통계는 부가 화면 — 대시보드를 막지 않는다).

⚠️ source 규칙:
- overview/by_model/monthly 는 모든 source(web/api/system)를 합산 — 시스템 원가까지 추적.
- by_user 는 source="system"(제목 요약 등 시스템 부담)을 제외 — 사용자 실사용만 표기.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import case, func, select

from lumen.db import get_session_factory, is_db_available
from lumen.models.chat_db import ChatApiKey, ChatUsageLog

logger = logging.getLogger(__name__)

_RANGE_DAYS = {"30d": 30, "90d": 90, "1y": 365}

# 시간버킷 — MySQL DATE_FORMAT 기반. 세밀한 버킷은 분을 하한으로 내린다.
_BUCKET_FMT = {"hour": "%Y-%m-%d %H:00:00", "day": "%Y-%m-%d", "month": "%Y-%m"}
_FINE_BUCKET_MINUTES = {"5m": 5, "15m": 15}


def _norm_bucket(value: str) -> str:
    return value if value in _BUCKET_FMT or value in _FINE_BUCKET_MINUTES else "day"


def _bucket_expr(bucket: str):
    normalized = _norm_bucket(bucket)
    if normalized in _FINE_BUCKET_MINUTES:
        minutes = _FINE_BUCKET_MINUTES[normalized]
        rounded_minute = func.floor(func.minute(ChatUsageLog.created_at) / minutes) * minutes
        return func.concat(
            func.date_format(ChatUsageLog.created_at, "%Y-%m-%d %H:"),
            func.lpad(rounded_minute, 2, "0"),
            ":00",
        )
    return func.date_format(ChatUsageLog.created_at, _BUCKET_FMT[normalized])


def _cutoff(range_key: str) -> datetime | None:
    """range → created_at 하한. 'all'/미지정은 None(전체)."""
    days = _RANGE_DAYS.get(range_key)
    if days is None:
        return None
    return datetime.now(UTC) - timedelta(days=days)


def _base_conds(range_key: str, project_id: str | None):
    conds = []
    cutoff = _cutoff(range_key)
    if cutoff is not None:
        conds.append(ChatUsageLog.created_at >= cutoff)
    if project_id:
        conds.append(ChatUsageLog.project_id == project_id)
    return conds


def _factory():
    if not is_db_available():
        return None
    return get_session_factory()


_OVERVIEW_EMPTY = {
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "total_tokens": 0,
    "credited_cost": 0.0,
    "raw_cost": 0.0,
    "request_count": 0,
    "active_users": 0,
    "unpriced_requests": 0,
    "conversation_count": 0,
    "by_source": [],
}


async def overview(range_key: str = "all", project_id: str | None = None) -> dict:
    """전체 요약 KPI + source(web/api/system) 분해."""
    factory = _factory()
    if factory is None:
        return dict(_OVERVIEW_EMPTY)
    conds = _base_conds(range_key, project_id)
    try:
        async with factory() as session:
            totals = (
                await session.execute(
                    select(
                        func.coalesce(func.sum(ChatUsageLog.prompt_tokens), 0),
                        func.coalesce(func.sum(ChatUsageLog.completion_tokens), 0),
                        func.coalesce(func.sum(ChatUsageLog.credited_cost), 0),
                        func.coalesce(func.sum(ChatUsageLog.raw_cost), 0),
                        func.count(ChatUsageLog.id),
                        func.coalesce(
                            func.sum(case((ChatUsageLog.pricing_status.in_(("partial", "unpriced")), 1), else_=0)),
                            0,
                        ),
                        func.count(func.distinct(ChatUsageLog.user_id)),
                        func.count(func.distinct(ChatUsageLog.conversation_id)),
                    ).where(*conds)
                )
            ).one()
            src_rows = (
                await session.execute(
                    select(
                        ChatUsageLog.source,
                        func.coalesce(func.sum(ChatUsageLog.prompt_tokens + ChatUsageLog.completion_tokens), 0),
                        func.coalesce(func.sum(ChatUsageLog.raw_cost), 0),
                        func.count(ChatUsageLog.id),
                    )
                    .where(*conds)
                    .group_by(ChatUsageLog.source)
                )
            ).all()
        pt, ct, credited, raw, req, unpriced, users, convs = totals
        return {
            "prompt_tokens": int(pt or 0),
            "completion_tokens": int(ct or 0),
            "total_tokens": int(pt or 0) + int(ct or 0),
            "credited_cost": float(credited or 0),
            "raw_cost": float(raw or 0),
            "request_count": int(req or 0),
            "unpriced_requests": int(unpriced or 0),
            "active_users": int(users or 0),
            "conversation_count": int(convs or 0),
            "by_source": [
                {"source": s or "web", "tokens": int(t or 0), "raw_cost": float(r or 0), "request_count": int(c or 0)}
                for s, t, r, c in src_rows
            ],
        }
    except Exception:
        logger.warning("채팅 통계 overview 집계 실패", exc_info=True)
        return dict(_OVERVIEW_EMPTY)


async def by_model(range_key: str = "all", project_id: str | None = None) -> list[dict]:
    """모델별 집계(토큰 desc). 모든 source 합산."""
    factory = _factory()
    if factory is None:
        return []
    conds = _base_conds(range_key, project_id)
    try:
        async with factory() as session:
            total_tok = ChatUsageLog.prompt_tokens + ChatUsageLog.completion_tokens
            rows = (
                await session.execute(
                    select(
                        ChatUsageLog.model_name,
                        func.coalesce(func.sum(ChatUsageLog.prompt_tokens), 0),
                        func.coalesce(func.sum(ChatUsageLog.completion_tokens), 0),
                        func.coalesce(func.sum(ChatUsageLog.credited_cost), 0),
                        func.coalesce(func.sum(ChatUsageLog.raw_cost), 0),
                        func.count(ChatUsageLog.id),
                        func.coalesce(
                            func.sum(case((ChatUsageLog.pricing_status.in_(("partial", "unpriced")), 1), else_=0)),
                            0,
                        ),
                    )
                    .where(*conds)
                    .group_by(ChatUsageLog.model_name)
                    .order_by(func.sum(total_tok).desc())
                )
            ).all()
        return [
            {
                "model_name": name,
                "prompt_tokens": int(pt or 0),
                "completion_tokens": int(ct or 0),
                "total_tokens": int(pt or 0) + int(ct or 0),
                "credited_cost": float(credited or 0),
                "raw_cost": float(raw or 0),
                "request_count": int(req or 0),
                "unpriced_requests": int(unpriced or 0),
            }
            for name, pt, ct, credited, raw, req, unpriced in rows
        ]
    except Exception:
        logger.warning("채팅 통계 by_model 집계 실패", exc_info=True)
        return []


async def by_user(
    range_key: str = "all", project_id: str | None = None, limit: int = 20, offset: int = 0
) -> list[dict]:
    """사용자별 집계(토큰 desc). source="system"(시스템 부담)은 제외 — 사용자 실사용만."""
    factory = _factory()
    if factory is None:
        return []
    conds = [*_base_conds(range_key, project_id), ChatUsageLog.source != "system"]
    try:
        async with factory() as session:
            total_tok = ChatUsageLog.prompt_tokens + ChatUsageLog.completion_tokens
            rows = (
                await session.execute(
                    select(
                        ChatUsageLog.user_id,
                        func.coalesce(func.sum(ChatUsageLog.prompt_tokens), 0),
                        func.coalesce(func.sum(ChatUsageLog.completion_tokens), 0),
                        func.coalesce(func.sum(ChatUsageLog.credited_cost), 0),
                        func.coalesce(func.sum(ChatUsageLog.raw_cost), 0),
                        func.count(ChatUsageLog.id),
                    )
                    .where(*conds)
                    .group_by(ChatUsageLog.user_id)
                    .order_by(func.sum(total_tok).desc())
                    .limit(min(max(limit, 1), 200))
                    .offset(max(offset, 0))
                )
            ).all()
        return [
            {
                "user_id": uid,
                "prompt_tokens": int(pt or 0),
                "completion_tokens": int(ct or 0),
                "total_tokens": int(pt or 0) + int(ct or 0),
                "credited_cost": float(credited or 0),
                "raw_cost": float(raw or 0),
                "request_count": int(req or 0),
            }
            for uid, pt, ct, credited, raw, req in rows
        ]
    except Exception:
        logger.warning("채팅 통계 by_user 집계 실패", exc_info=True)
        return []


async def timeseries(
    bucket: str = "day",
    range_key: str = "all",
    project_id: str | None = None,
    *,
    user_id: str | None = None,
    model_name: str | None = None,
    include_system: bool = True,
    api_key_id: int | None = None,
) -> list[dict]:
    """시간버킷(hour/day/month)별 × source(web/api/system)별 시계열(오름차순).

    프론트가 접근경로(web vs api)를 시간축으로 쌓아 "언제·어느 경로로 많이 썼는지" 표시.
    user_id 지정 시 해당 사용자만(개인 사용량 뷰). include_system=False 면 시스템 부담 제외.
    """
    factory = _factory()
    if factory is None:
        return []
    conds = _base_conds(range_key, project_id)
    fine_window_days = {"5m": 1, "15m": 7}.get(_norm_bucket(bucket))
    if fine_window_days is not None:
        conds.append(ChatUsageLog.created_at >= datetime.now(UTC) - timedelta(days=fine_window_days))
    if user_id:
        conds.append(ChatUsageLog.user_id == user_id)
    if model_name:
        conds.append(ChatUsageLog.model_name == model_name)
    if api_key_id is not None:
        conds.append(ChatUsageLog.api_key_id == api_key_id)
    if not include_system:
        conds.append(ChatUsageLog.source != "system")
    bexpr = _bucket_expr(bucket)
    try:
        async with factory() as session:
            rows = (
                await session.execute(
                    select(
                        bexpr,
                        ChatUsageLog.source,
                        func.coalesce(func.sum(ChatUsageLog.prompt_tokens), 0),
                        func.coalesce(func.sum(ChatUsageLog.completion_tokens), 0),
                        func.coalesce(func.sum(ChatUsageLog.credited_cost), 0),
                        func.count(ChatUsageLog.id),
                    )
                    .where(*conds)
                    .group_by(bexpr, ChatUsageLog.source)
                    .order_by(bexpr.asc())
                )
            ).all()
        return [
            {
                "bucket": b,
                "source": s or "web",
                "prompt_tokens": int(pt or 0),
                "completion_tokens": int(ct or 0),
                "total_tokens": int(pt or 0) + int(ct or 0),
                "credited_cost": float(credited or 0),
                "request_count": int(req or 0),
            }
            for b, s, pt, ct, credited, req in rows
        ]
    except Exception:
        logger.warning("채팅 통계 timeseries 집계 실패", exc_info=True)
        return []


async def by_api_key(
    range_key: str = "all",
    project_id: str | None = None,
    *,
    user_id: str | None = None,
    limit: int = 50,
    api_key_id: int | None = None,
) -> list[dict]:
    """API 키별 집계(토큰 desc) — source="api" 만. 폐기된 키도 원장 보존(name None 가능).

    user_id 지정 시 본인 키만(개인 뷰). 미지정 시 전체(관리자 뷰).
    """
    factory = _factory()
    if factory is None:
        return []
    conds = [*_base_conds(range_key, project_id), ChatUsageLog.source == "api", ChatUsageLog.api_key_id.isnot(None)]
    if user_id:
        conds.append(ChatUsageLog.user_id == user_id)
    if api_key_id is not None:
        conds.append(ChatUsageLog.api_key_id == api_key_id)
    total_tok = ChatUsageLog.prompt_tokens + ChatUsageLog.completion_tokens
    try:
        async with factory() as session:
            rows = (
                await session.execute(
                    select(
                        ChatUsageLog.api_key_id,
                        ChatApiKey.name,
                        ChatApiKey.key_prefix,
                        func.coalesce(func.sum(ChatUsageLog.prompt_tokens), 0),
                        func.coalesce(func.sum(ChatUsageLog.completion_tokens), 0),
                        func.coalesce(func.sum(ChatUsageLog.credited_cost), 0),
                        func.count(ChatUsageLog.id),
                    )
                    .outerjoin(ChatApiKey, ChatApiKey.id == ChatUsageLog.api_key_id)
                    .where(*conds)
                    .group_by(ChatUsageLog.api_key_id, ChatApiKey.name, ChatApiKey.key_prefix)
                    .order_by(func.sum(total_tok).desc())
                    .limit(min(max(limit, 1), 200))
                )
            ).all()
        return [
            {
                "api_key_id": kid,
                "name": name,
                "key_prefix": prefix,
                "prompt_tokens": int(pt or 0),
                "completion_tokens": int(ct or 0),
                "total_tokens": int(pt or 0) + int(ct or 0),
                "credited_cost": float(credited or 0),
                "request_count": int(req or 0),
            }
            for kid, name, prefix, pt, ct, credited, req in rows
        ]
    except Exception:
        logger.warning("채팅 통계 by_api_key 집계 실패", exc_info=True)
        return []


def _month_ts(ym: str) -> int:
    """'YYYY-MM' → 해당 월초(UTC) epoch seconds. 파싱 실패 시 0."""
    try:
        year, month = ym.split("-")
        return int(datetime(int(year), int(month), 1, tzinfo=UTC).timestamp())
    except (ValueError, AttributeError):
        return 0


async def monthly(range_key: str = "all", project_id: str | None = None) -> list[dict]:
    """월별 시계열(오름차순). ts(월초 epoch) 포함 — TimeSeriesChart data 호환. 모든 source 합산."""
    factory = _factory()
    if factory is None:
        return []
    conds = _base_conds(range_key, project_id)
    try:
        async with factory() as session:
            month_expr = func.date_format(ChatUsageLog.created_at, "%Y-%m")
            rows = (
                await session.execute(
                    select(
                        month_expr,
                        func.coalesce(func.sum(ChatUsageLog.prompt_tokens), 0),
                        func.coalesce(func.sum(ChatUsageLog.completion_tokens), 0),
                        func.coalesce(func.sum(ChatUsageLog.credited_cost), 0),
                        func.coalesce(func.sum(ChatUsageLog.raw_cost), 0),
                        func.count(ChatUsageLog.id),
                    )
                    .where(*conds)
                    .group_by(month_expr)
                    .order_by(month_expr.asc())
                )
            ).all()
        return [
            {
                "month": ym,
                "ts": _month_ts(ym),
                "prompt_tokens": int(pt or 0),
                "completion_tokens": int(ct or 0),
                "total_tokens": int(pt or 0) + int(ct or 0),
                "credited_cost": float(credited or 0),
                "raw_cost": float(raw or 0),
                "request_count": int(req or 0),
            }
            for ym, pt, ct, credited, raw, req in rows
        ]
    except Exception:
        logger.warning("채팅 통계 monthly 집계 실패", exc_info=True)
        return []


async def projects_with_usage(range_key: str = "all") -> list[str]:
    """usage_logs 에 데이터가 있는 project_id 목록(드롭다운 소스)."""
    factory = _factory()
    if factory is None:
        return []
    conds = _base_conds(range_key, None)
    try:
        async with factory() as session:
            rows = (
                await session.execute(select(ChatUsageLog.project_id).where(*conds).group_by(ChatUsageLog.project_id))
            ).all()
        return [r[0] for r in rows if r[0]]
    except Exception:
        logger.warning("채팅 통계 projects_with_usage 조회 실패", exc_info=True)
        return []
