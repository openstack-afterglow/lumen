"""빌트인 AI 채팅 사용량 집계 — chat_usage_logs 원장에서 사용자별 요약.

로그인 사용자 본인의 누적/이번달 토큰·크레딧·요청 수 + 월 쿼터(user_wallets)를 반환한다.
대화가 프로젝트 무관(user_id 소유)이므로 사용량도 user_id 기준으로 집계한다.
source="system"(제목 요약 등 시스템 부담)은 사용자 사용량에서 제외한다.
저장소 장애 시에도 found=False 로 안전 반환(항상 200).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import func, select

from lumen.db import get_session_factory, is_db_available
from lumen.models.chat_contracts import UsageRecord, UsageRecordPage
from lumen.models.chat_db import ChatUsageLog, UserWallet

logger = logging.getLogger(__name__)

_EMPTY = {
    "found": False,
    "total_credited_cost": 0.0,
    "lifetime_prompt_tokens": 0,
    "lifetime_completion_tokens": 0,
    "lifetime_request_count": 0,
    "month_credited_cost": 0.0,
    "month_prompt_tokens": 0,
    "month_completion_tokens": 0,
    "month_request_count": 0,
    "quota_used": 0.0,
    "quota_max": 0.0,
    # 접근경로(web vs api) 분해 — 누적 기준.
    "by_source": [],
}

_SUMS = (
    func.coalesce(func.sum(ChatUsageLog.credited_cost), 0),
    func.coalesce(func.sum(ChatUsageLog.prompt_tokens), 0),
    func.coalesce(func.sum(ChatUsageLog.completion_tokens), 0),
    func.count(ChatUsageLog.id),
)


async def user_usage_summary(user_id: str, project_id: str = "", api_key_id: int | None = None) -> dict:
    """본인 누적/이번달 사용량 + 월 쿼터. 실패 시 빈 요약(found=False).

    project_id 인자는 하위호환용으로 받되 사용하지 않는다(사용량은 user_id 기준, 프로젝트 무관).
    """
    if not is_db_available():
        return dict(_EMPTY)
    factory = get_session_factory()
    if factory is None:
        return dict(_EMPTY)
    try:
        month_start = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        base = [ChatUsageLog.user_id == user_id, ChatUsageLog.source != "system"]
        if api_key_id is not None:
            base.append(ChatUsageLog.api_key_id == api_key_id)
        async with factory() as session:
            total = (await session.execute(select(*_SUMS).where(*base))).one()
            month = (await session.execute(select(*_SUMS).where(*base, ChatUsageLog.created_at >= month_start))).one()
            src_rows = (
                await session.execute(
                    select(
                        ChatUsageLog.source,
                        func.coalesce(func.sum(ChatUsageLog.prompt_tokens + ChatUsageLog.completion_tokens), 0),
                        func.coalesce(func.sum(ChatUsageLog.credited_cost), 0),
                        func.count(ChatUsageLog.id),
                    )
                    .where(*base)
                    .group_by(ChatUsageLog.source)
                )
            ).all()
            wallet = await session.get(UserWallet, user_id)
        tc, tp, tct, tcount = total
        mc, mp, mct, mcount = month
        by_source = [
            {"source": s or "web", "tokens": int(t or 0), "credited_cost": float(c or 0), "request_count": int(n or 0)}
            for s, t, c, n in src_rows
        ]
        return {
            "found": tcount > 0,
            "total_credited_cost": float(tc or 0),
            "lifetime_prompt_tokens": int(tp or 0),
            "lifetime_completion_tokens": int(tct or 0),
            "lifetime_request_count": int(tcount or 0),
            "month_credited_cost": float(mc or 0),
            "month_prompt_tokens": int(mp or 0),
            "month_completion_tokens": int(mct or 0),
            "month_request_count": int(mcount or 0),
            "quota_used": float(mc or 0),
            "quota_max": float(wallet.max_quota_monthly) if wallet else 0.0,
            "by_source": by_source,
        }
    except Exception:
        logger.warning("빌트인 채팅 사용량 집계 실패", exc_info=True)
        return dict(_EMPTY)


async def list_usage_records(
    *,
    user_id: str,
    api_key_id: int | None,
    source: str | None,
    before_id: int | None,
    limit: int,
) -> UsageRecordPage:
    """Return a cursor page of user-safe usage ledger fields, excluding system rows."""
    if not is_db_available() or (factory := get_session_factory()) is None:
        return UsageRecordPage(records=[])
    predicates = [ChatUsageLog.user_id == user_id, ChatUsageLog.source.in_(("web", "api"))]
    if api_key_id is not None:
        predicates.append(ChatUsageLog.api_key_id == api_key_id)
    if source is not None:
        predicates.append(ChatUsageLog.source == source)
    if before_id is not None:
        predicates.append(ChatUsageLog.id < before_id)
    try:
        async with factory() as session:
            rows = (
                (
                    await session.execute(
                        select(ChatUsageLog).where(*predicates).order_by(ChatUsageLog.id.desc()).limit(limit + 1)
                    )
                )
                .scalars()
                .all()
            )
    except Exception:
        logger.warning("사용량 원장 조회 실패", exc_info=True)
        return UsageRecordPage(records=[])
    next_before_id = rows[limit - 1].id if len(rows) > limit else None
    return UsageRecordPage(
        records=[
            UsageRecord(
                id=row.id,
                created_at=row.created_at,
                model_name=row.model_name,
                provider=row.provider,
                prompt_tokens=row.prompt_tokens,
                completion_tokens=row.completion_tokens,
                total_tokens=row.prompt_tokens + row.completion_tokens,
                credited_cost=format(row.credited_cost, "f"),
                source=row.source,
                api_key_id=row.api_key_id,
                conversation_id=row.conversation_id,
                run_id=row.run_id,
                pricing_status=row.pricing_status,
            )
            for row in rows[:limit]
        ],
        next_before_id=next_before_id,
    )
