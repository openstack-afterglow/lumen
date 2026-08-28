"""빌트인 AI 채팅 크레딧/쿼터 관리 (MySQL user_wallets + chat_usage_logs).

과금 모델: 모델별 raw_cost(USD) × margin_multiplier × credit_per_usd = credited_cost(크레딧).
월간 쿼터 상한(used_quota_this_month ≥ max_quota_monthly)을 초과하면 요청을 차단한다.

⚠️ 보안/정합성:
- precheck 는 **fail-closed**: DB 장애 시 요청 거부(ChatStorageUnavailable → 503).
- apply_usage 는 **원자적 SQL UPDATE**(used_quota += credited)로 동시요청 race 를 완화한다.
- pre-check ↔ apply_usage 사이 TOCTOU 로 소폭 초과는 허용(하드락 미도입) — 내부 쿼터 용도.
- 스트리밍 중단 시에도 호출부가 finally 에서 apply_usage 를 호출해 부분 사용량을 과금한다.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError, OperationalError

from lumen.config import get_settings
from lumen.db import get_session_factory, is_db_available, mark_db_unhealthy
from lumen.models.chat_db import ChatApiKey, ChatUsageLog, UserWallet
from lumen.services.api_key_store import calculate_effective_limit
from lumen.services.litellm_client import UsageCost

logger = logging.getLogger(__name__)

_CREDIT_QUANTUM = Decimal("0.00000001")  # DECIMAL(18,8)

_USD_QUANTUM = Decimal("0.0000000001")


class ChatStorageUnavailable(RuntimeError):
    """chat DB 미구성/장애 — fail-closed(503)."""


class QuotaExceeded(RuntimeError):
    """월 쿼터 초과 — 402."""


def _require_db():
    if not is_db_available():
        raise ChatStorageUnavailable("chat DB 를 사용할 수 없습니다")
    factory = get_session_factory()
    if factory is None:
        raise ChatStorageUnavailable("chat DB 가 구성되지 않았습니다")
    return factory


def credits_for_cost(raw_cost_usd, margin_multiplier=1.0, credit_per_usd=None) -> Decimal:
    """raw_cost(USD) → 차감 크레딧. 순수 함수(테스트 용이).

    credited = raw_cost × margin_multiplier × credit_per_usd, 8자리로 양자화.
    """
    if credit_per_usd is None:
        credit_per_usd = get_settings().chat_credit_per_usd
    value = Decimal(str(raw_cost_usd)) * Decimal(str(margin_multiplier)) * Decimal(str(credit_per_usd))
    if value < 0:
        value = Decimal("0")
    return value.quantize(_CREDIT_QUANTUM)


def usage_cost_from_pricing_snapshot(
    pricing_snapshot: dict,
    *,
    prompt_tokens: int,
    completion_tokens: int,
) -> UsageCost:
    """Calculate base-model usage exclusively from a durable run's frozen price pair."""
    try:
        input_price = Decimal(str(pricing_snapshot["input_price_per_token"]))
        output_price = Decimal(str(pricing_snapshot["output_price_per_token"]))
    except (InvalidOperation, KeyError, TypeError, ValueError) as exc:
        raise ValueError("durable pricing snapshot is invalid") from exc
    if not input_price.is_finite() or not output_price.is_finite() or input_price < 0 or output_price < 0:
        raise ValueError("durable pricing snapshot is invalid")
    prompt_tokens = max(0, int(prompt_tokens))
    completion_tokens = max(0, int(completion_tokens))
    input_cost = (input_price * prompt_tokens).quantize(_USD_QUANTUM, rounding=ROUND_HALF_EVEN)
    output_cost = (output_price * completion_tokens).quantize(_USD_QUANTUM, rounding=ROUND_HALF_EVEN)
    return UsageCost(
        raw_cost=(input_cost + output_cost).quantize(_USD_QUANTUM, rounding=ROUND_HALF_EVEN),
        input_cost=input_cost,
        output_cost=output_cost,
        pricing_status="priced",
        pricing_snapshot=dict(pricing_snapshot),
    )


def _first_of_month(d: date) -> date:
    return d.replace(day=1)


def _maybe_reset_month(wallet: UserWallet) -> None:
    """새 달이면 used_quota_this_month 를 0으로 리셋(스케줄러 미가동 시 lazy 보정)."""
    today = datetime.now(UTC).date()
    period = wallet.quota_period_start
    if period is None or (period.year, period.month) != (today.year, today.month):
        wallet.used_quota_this_month = Decimal("0")
        wallet.quota_period_start = _first_of_month(today)


async def _get_or_create_wallet(session, user_id: str, project_id: str | None) -> UserWallet:
    wallet = await session.get(UserWallet, user_id)
    if wallet is None:
        default_quota = Decimal(str(get_settings().chat_default_monthly_quota))
        wallet = UserWallet(
            user_id=user_id,
            project_id=project_id,
            max_quota_monthly=default_quota,
            used_quota_this_month=Decimal("0"),
            quota_period_start=_first_of_month(datetime.now(UTC).date()),
        )
        session.add(wallet)
        await session.flush()
    return wallet


async def precheck(
    user_id: str, project_id: str | None = None, api_key_id: int | None = None
) -> None:
    """월 쿼터 초과 시 QuotaExceeded. DB 장애 시 ChatStorageUnavailable(fail-closed).

    max_quota_monthly == 0 은 '무제한'으로 해석(설정 default_monthly_quota=0 대응).
    """
    factory = _require_db()
    try:
        async with factory() as session, session.begin():
            wallet = await _get_or_create_wallet(session, user_id, project_id)
            _maybe_reset_month(wallet)
            if not wallet.is_active:
                raise QuotaExceeded("비활성 지갑입니다")
            if (
                wallet.max_quota_monthly
                and wallet.max_quota_monthly > 0
                and wallet.used_quota_this_month >= wallet.max_quota_monthly
            ):
                raise QuotaExceeded("월 사용 한도를 초과했습니다")

            if api_key_id is not None:
                key_row = await session.get(ChatApiKey, api_key_id)
                if key_row is None or key_row.owner_user_id != user_id:
                    raise ChatStorageUnavailable("chat DB 오류")
                if project_id is not None and key_row.owner_project_id != project_id:
                    raise ChatStorageUnavailable("chat DB 오류")
                if not key_row.is_active or key_row.revoked_at is not None:
                    raise ChatStorageUnavailable("chat DB 오류")

                owner_lim = key_row.owner_monthly_credit_limit
                admin_lim = key_row.admin_monthly_credit_limit

                if owner_lim is not None:
                    if not isinstance(owner_lim, Decimal):
                        try:
                            owner_lim = Decimal(str(owner_lim))
                        except Exception as exc:
                            raise ChatStorageUnavailable("chat DB 오류") from exc
                    if not owner_lim.is_finite() or owner_lim <= 0:
                        raise ChatStorageUnavailable("chat DB 오류")

                if admin_lim is not None:
                    if not isinstance(admin_lim, Decimal):
                        try:
                            admin_lim = Decimal(str(admin_lim))
                        except Exception as exc:
                            raise ChatStorageUnavailable("chat DB 오류") from exc
                    if not admin_lim.is_finite() or admin_lim <= 0:
                        raise ChatStorageUnavailable("chat DB 오류")

                sys_quota = None
                if wallet.max_quota_monthly is not None:
                    try:
                        sys_quota = Decimal(str(wallet.max_quota_monthly))
                        if not sys_quota.is_finite():
                            raise ChatStorageUnavailable("chat DB 오류")
                    except Exception as exc:
                        raise ChatStorageUnavailable("chat DB 오류") from exc
                effective_limit = calculate_effective_limit(owner_lim, admin_lim, sys_quota)

                if effective_limit is not None:
                    month_start = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
                    stmt = select(func.coalesce(func.sum(ChatUsageLog.credited_cost), Decimal("0"))).where(
                        ChatUsageLog.api_key_id == api_key_id,
                        ChatUsageLog.source == "api",
                        ChatUsageLog.created_at >= month_start,
                    )
                    month_usage = (await session.execute(stmt)).scalar_one()
                    if month_usage >= effective_limit:
                        raise QuotaExceeded("API 키 월 사용 한도를 초과했습니다")
    except (QuotaExceeded, ChatStorageUnavailable):
        raise
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc
    except Exception as exc:
        raise ChatStorageUnavailable("chat DB 오류") from exc


async def apply_usage_in_transaction(
    session,
    *,
    event_id: str,
    user_id: str,
    project_id: str,
    model_name: str,
    provider: str | None,
    prompt_tokens: int,
    completion_tokens: int,
    usage_cost: UsageCost,
    margin_multiplier: Decimal,
    credit_per_usd: Decimal | None = None,
    conversation_id: str | None = None,
    source: str = "web",
    api_key_id: int | None = None,
    run_id: str | None = None,
    charge_wallet: bool = True,
    usage_components: list[dict[str, Any]] | None = None,
) -> Decimal:
    """Append usage and wallet delta in the caller's transaction."""
    if not event_id or len(event_id) > 64:
        raise ValueError("event_id 값이 올바르지 않습니다")
    credited = credits_for_cost(usage_cost.raw_cost, margin_multiplier, credit_per_usd)
    pricing_snapshot = {
        **usage_cost.pricing_snapshot,
        "margin_multiplier": format(Decimal(margin_multiplier), "f"),
        "chat_credit_per_usd": format(
            Decimal(str(get_settings().chat_credit_per_usd if credit_per_usd is None else credit_per_usd)),
            "f",
        ),
        "credited_cost": format(credited, "f"),
    }
    session.add(
        ChatUsageLog(
            event_id=event_id,
            project_id=project_id,
            user_id=user_id,
            conversation_id=conversation_id,
            model_name=model_name,
            provider=provider,
            prompt_tokens=int(prompt_tokens),
            completion_tokens=int(completion_tokens),
            raw_cost=usage_cost.raw_cost,
            credited_cost=credited,
            source=source,
            api_key_id=api_key_id,
            pricing_status=usage_cost.pricing_status,
            pricing_snapshot=pricing_snapshot,
            run_id=run_id,
            usage_components=usage_components,
        )
    )
    await session.flush()
    if charge_wallet:
        wallet = await _get_or_create_wallet(session, user_id, project_id)
        _maybe_reset_month(wallet)
        await session.execute(
            update(UserWallet)
            .where(UserWallet.user_id == user_id)
            .values(used_quota_this_month=UserWallet.used_quota_this_month + credited)
        )
    return credited


async def apply_usage(
    *,
    event_id: str,
    user_id: str,
    project_id: str,
    model_name: str,
    provider: str | None,
    prompt_tokens: int,
    completion_tokens: int,
    usage_cost: UsageCost,
    margin_multiplier: Decimal,
    credit_per_usd: Decimal | None = None,
    conversation_id: str | None = None,
    source: str = "web",
    api_key_id: int | None = None,
    run_id: str | None = None,
    charge_wallet: bool = True,
) -> Decimal:
    factory = _require_db()
    try:
        async with factory() as session:
            try:
                async with session.begin():
                    return await apply_usage_in_transaction(
                        session,
                        event_id=event_id,
                        user_id=user_id,
                        project_id=project_id,
                        model_name=model_name,
                        provider=provider,
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        usage_cost=usage_cost,
                        margin_multiplier=margin_multiplier,
                        credit_per_usd=credit_per_usd,
                        conversation_id=conversation_id,
                        source=source,
                        api_key_id=api_key_id,
                        charge_wallet=charge_wallet,
                        run_id=run_id,
                    )
            except IntegrityError as exc:
                await session.rollback()
                existing = (
                    await session.execute(select(ChatUsageLog).where(ChatUsageLog.event_id == event_id))
                ).scalar_one_or_none()
                if existing is None:
                    raise ChatStorageUnavailable("chat DB usage event 저장 오류") from exc
                return existing.credited_cost
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc
