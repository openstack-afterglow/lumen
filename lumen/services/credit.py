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
from lumen.models.chat_db import ChatApiKey, ChatQuotaPolicy, ChatUsageLog, UserWallet
from lumen.services import quota_policy
from lumen.services.api_key_store import calculate_effective_limit
from lumen.services.litellm_client import UsageCost
from lumen.services.quota_periods import month_start, week_start
from lumen.services.usage_breakdown import UsageBreakdown

logger = logging.getLogger(__name__)

_CREDIT_QUANTUM = Decimal("0.00000001")  # DECIMAL(18,8)

_USD_QUANTUM = Decimal("0.0000000001")


class ChatStorageUnavailable(RuntimeError):
    """chat DB 미구성/장애 — fail-closed(503)."""


class QuotaExceeded(RuntimeError):
    """월 쿼터 초과 — 402."""


class QuotaLimitConflict(ValueError):
    """A finite weekly override exceeds its finite monthly ceiling."""


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


_CACHE_SNAPSHOT_PRICES = (
    ("cache_read", "cache_read_input_tokens", "cache_read_price_per_token", "cache_read_price_per_token_above_200k"),
    ("cache_creation_5m", "cache_creation_5m_input_tokens", "cache_write_price_per_token", "cache_write_price_per_token_above_200k"),
    ("cache_creation_1h", "cache_creation_1h_input_tokens", "cache_write_1h_price_per_token", "cache_write_1h_price_per_token_above_200k"),
)


def _snapshot_price(pricing_snapshot: dict, key: str, *, required: bool) -> Decimal | None:
    value = pricing_snapshot.get(key) if not required else pricing_snapshot[key]
    if value is None and not required:
        return None
    price = Decimal(str(value))
    if not price.is_finite() or price < 0:
        raise ValueError("durable pricing snapshot is invalid")
    return price


def usage_cost_from_pricing_snapshot(
    pricing_snapshot: dict,
    *,
    prompt_tokens: int,
    completion_tokens: int,
    breakdown: UsageBreakdown | None = None,
) -> UsageCost:
    """Calculate base-model usage exclusively from a durable run's frozen prices.

    ``prompt_tokens`` is total input; the input rate applies to its uncached
    share. A cache category without a frozen rate — including every snapshot
    frozen before cache rates existed — bills 0 and marks the usage partial.
    """
    try:
        input_price = _snapshot_price(pricing_snapshot, "input_price_per_token", required=True)
        output_price = _snapshot_price(pricing_snapshot, "output_price_per_token", required=True)
        cache_prices = {
            name: (_snapshot_price(pricing_snapshot, key, required=False),
                   _snapshot_price(pricing_snapshot, tier_key, required=False))
            for name, _, key, tier_key in _CACHE_SNAPSHOT_PRICES
        }
    except (InvalidOperation, KeyError, TypeError, ValueError) as exc:
        raise ValueError("durable pricing snapshot is invalid") from exc
    if breakdown is None:
        breakdown = UsageBreakdown.from_totals(prompt_tokens, completion_tokens)
    elif (breakdown.input_tokens, breakdown.output_tokens) != (
        max(0, int(prompt_tokens)),
        max(0, int(completion_tokens)),
    ):
        raise ValueError("usage breakdown totals do not match prompt/completion tokens")
    uncached_tokens = breakdown.uncached_input_tokens
    input_cost = (input_price * uncached_tokens).quantize(_USD_QUANTUM, rounding=ROUND_HALF_EVEN)
    output_cost = (output_price * breakdown.output_tokens).quantize(_USD_QUANTUM, rounding=ROUND_HALF_EVEN)
    components: dict[str, dict[str, object]] = {
        "input": {
            "tokens": uncached_tokens,
            "price_per_token": format(input_price, "f"),
            "cost": format(input_cost, "f"),
        },
        "output": {
            "tokens": breakdown.output_tokens,
            "price_per_token": format(output_price, "f"),
            "cost": format(output_cost, "f"),
        },
    }
    cache_costs: dict[str, Decimal] = {}
    unpriced_cache = False
    for name, token_field, _, _ in _CACHE_SNAPSHOT_PRICES:
        tokens = getattr(breakdown, token_field)
        rate, tier_rate = cache_prices[name]
        source = (pricing_snapshot.get("cache_price_sources") or {}).get(name, "manual")
        if breakdown.input_tokens > 200_000 and source == "litellm" and tier_rate is not None:
            rate = tier_rate
        cost = (rate * tokens).quantize(_USD_QUANTUM, rounding=ROUND_HALF_EVEN) if rate is not None else Decimal("0")
        unpriced_cache = unpriced_cache or (tokens > 0 and rate is None)
        cache_costs[name] = cost
        components[name] = {
            "tokens": tokens,
            "price_per_token": format(rate, "f") if rate is not None else None,
            "cost": format(cost, "f"),
            "source": source if rate is not None else None,
        }
    raw_cost = input_cost + output_cost + sum(cache_costs.values(), Decimal("0"))
    return UsageCost(
        raw_cost=raw_cost.quantize(_USD_QUANTUM, rounding=ROUND_HALF_EVEN),
        input_cost=input_cost,
        output_cost=output_cost,
        pricing_status="partial" if unpriced_cache else "priced",
        pricing_snapshot={**pricing_snapshot, "token_components": components},
        cache_read_cost=cache_costs["cache_read"],
        cache_creation_5m_cost=cache_costs["cache_creation_5m"],
        cache_creation_1h_cost=cache_costs["cache_creation_1h"],
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
        wallet = UserWallet(
            user_id=user_id,
            project_id=project_id,
            max_quota_monthly=None,
            max_quota_weekly=None,
            used_quota_this_month=Decimal("0"),
            quota_period_start=_first_of_month(datetime.now(UTC).date()),
        )
        session.add(wallet)
        await session.flush()
    return wallet


async def _ledger_credited_since(
    session,
    *,
    since: datetime,
    user_id: str | None = None,
    api_key_id: int | None = None,
    api_only: bool = False,
) -> Decimal:
    stmt = select(func.coalesce(func.sum(ChatUsageLog.credited_cost), Decimal("0"))).where(
        ChatUsageLog.created_at >= since
    )
    if user_id is not None:
        stmt = stmt.where(ChatUsageLog.user_id == user_id, ChatUsageLog.source != "system")
    if api_key_id is not None:
        stmt = stmt.where(ChatUsageLog.api_key_id == api_key_id)
    if api_only:
        stmt = stmt.where(ChatUsageLog.source == "api")
    return Decimal(str((await session.execute(stmt)).scalar_one()))


async def precheck(user_id: str, project_id: str | None = None, api_key_id: int | None = None) -> None:
    """Reject exhausted monthly or weekly quotas; storage failures fail closed.

    A zero weekly quota means no separate weekly cap. The effective monthly quota
    is still checked first, so weekly usage can never escape its monthly ceiling.
    """
    factory = _require_db()
    try:
        async with factory() as session, session.begin():
            wallet = await _get_or_create_wallet(session, user_id, project_id)
            _maybe_reset_month(wallet)
            system = await quota_policy.get_system_quota(session, user_id)
            if not wallet.is_active:
                raise QuotaExceeded("비활성 지갑입니다")
            if system.monthly > 0 and wallet.used_quota_this_month >= system.monthly:
                raise QuotaExceeded("월 사용 한도를 초과했습니다")
            if system.weekly > 0:
                week_usage = await _ledger_credited_since(
                    session,
                    since=week_start(),
                    user_id=user_id,
                )
                if week_usage >= system.weekly:
                    raise QuotaExceeded("주간 사용 한도를 초과했습니다")

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
                owner_weekly = key_row.owner_weekly_credit_limit

                if owner_lim is not None:
                    if not isinstance(owner_lim, Decimal):
                        try:
                            owner_lim = Decimal(str(owner_lim))
                        except Exception as exc:
                            raise ChatStorageUnavailable("chat DB 오류") from exc
                    if not owner_lim.is_finite() or owner_lim <= 0:
                        raise ChatStorageUnavailable("chat DB 오류")

                if owner_weekly is not None:
                    if not isinstance(owner_weekly, Decimal):
                        try:
                            owner_weekly = Decimal(str(owner_weekly))
                        except Exception as exc:
                            raise ChatStorageUnavailable("chat DB 오류") from exc
                    if not owner_weekly.is_finite() or owner_weekly <= 0:
                        raise ChatStorageUnavailable("chat DB 오류")
                if admin_lim is not None:
                    if not isinstance(admin_lim, Decimal):
                        try:
                            admin_lim = Decimal(str(admin_lim))
                        except Exception as exc:
                            raise ChatStorageUnavailable("chat DB 오류") from exc
                    if not admin_lim.is_finite() or admin_lim <= 0:
                        raise ChatStorageUnavailable("chat DB 오류")

                effective_limit = calculate_effective_limit(owner_lim, admin_lim, system.monthly)
                if effective_limit is not None:
                    month_usage = await _ledger_credited_since(
                        session,
                        since=month_start(),
                        api_key_id=api_key_id,
                        api_only=True,
                    )
                    if month_usage >= effective_limit:
                        raise QuotaExceeded("API 키 월 사용 한도를 초과했습니다")

                effective_weekly = calculate_effective_limit(
                    owner_weekly,
                    None,
                    system.weekly,
                )
                if effective_weekly is not None:
                    key_week_usage = await _ledger_credited_since(
                        session,
                        since=week_start(),
                        api_key_id=api_key_id,
                        api_only=True,
                    )
                    if key_week_usage >= effective_weekly:
                        raise QuotaExceeded("API 키 주간 사용 한도를 초과했습니다")
    except (QuotaExceeded, ChatStorageUnavailable):
        raise
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc
    except Exception as exc:
        raise ChatStorageUnavailable("chat DB 오류") from exc


def _public_limit(value: Decimal) -> str | None:
    return None if value <= 0 else format(value, "f")


def _quota_public(
    wallet: UserWallet,
    *,
    default_monthly: Decimal,
    month_used: Decimal,
    week_used: Decimal,
) -> dict:
    monthly_override = None if wallet.max_quota_monthly is None else Decimal(str(wallet.max_quota_monthly))
    weekly_override = None if wallet.max_quota_weekly is None else Decimal(str(wallet.max_quota_weekly))
    monthly = quota_policy.effective_limit(monthly_override, default_monthly)
    weekly = quota_policy.effective_limit(weekly_override, Decimal("0"))
    return {
        "user_id": wallet.user_id,
        "project_id": wallet.project_id,
        "monthly_credit_limit": _public_limit(monthly),
        "weekly_credit_limit": _public_limit(weekly),
        "configured_monthly_credit_limit": (_public_limit(monthly_override) if monthly_override is not None else None),
        "configured_weekly_credit_limit": (_public_limit(weekly_override) if weekly_override is not None else None),
        "monthly_limit_source": "default" if monthly_override is None else "user",
        "weekly_limit_source": "default" if weekly_override is None else "user",
        "weekly_bound_by_monthly": monthly > 0 and (weekly <= 0 or monthly < weekly),
        "month_credited_cost": format(month_used, "f"),
        "week_credited_cost": format(week_used, "f"),
        "is_active": wallet.is_active,
        "updated_at": wallet.updated_at.isoformat() if wallet.updated_at else None,
    }


async def _user_credited_since_batch(
    session,
    user_ids: set[str],
    *,
    since: datetime,
) -> dict[str, Decimal]:
    if not user_ids:
        return {}
    stmt = (
        select(
            ChatUsageLog.user_id,
            func.coalesce(func.sum(ChatUsageLog.credited_cost), Decimal("0")),
        )
        .where(
            ChatUsageLog.user_id.in_(user_ids),
            ChatUsageLog.source != "system",
            ChatUsageLog.created_at >= since,
        )
        .group_by(ChatUsageLog.user_id)
    )
    rows = (await session.execute(stmt)).all()
    return {
        row_user_id: Decimal(str(total)) for row_user_id, total in rows if row_user_id is not None and total is not None
    }


async def list_user_quotas(user_id: str | None = None) -> dict:
    factory = _require_db()
    try:
        async with factory() as session:
            default_monthly = await quota_policy.default_monthly_limit(session)
            stmt = select(UserWallet)
            if user_id is not None:
                stmt = stmt.where(UserWallet.user_id == user_id)
            wallets = (await session.execute(stmt.order_by(UserWallet.user_id))).scalars().all()
            user_ids = {wallet.user_id for wallet in wallets}
            month_used = await _user_credited_since_batch(
                session,
                user_ids,
                since=month_start(),
            )
            week_used = await _user_credited_since_batch(
                session,
                user_ids,
                since=week_start(),
            )
            credit_per_usd = Decimal(str(get_settings().chat_credit_per_usd))
            return {
                "default_monthly_credit_limit": _public_limit(default_monthly),
                "default_weekly_credit_limit": None,
                "credit_policy": {
                    "credit_per_usd": format(credit_per_usd, "f"),
                    "usd_per_credit": format(
                        (Decimal("1") / credit_per_usd).quantize(_USD_QUANTUM),
                        "f",
                    ),
                    "formula": (
                        "credited_cost = "
                        "(uncached_input_tokens × input_price_per_token + "
                        "cache_read_input_tokens × cache_read_price_per_token + "
                        "cache_creation_5m_input_tokens × cache_write_price_per_token + "
                        "cache_creation_1h_input_tokens × cache_write_1h_price_per_token + "
                        "completion_tokens × output_price_per_token + tool_costs) "
                        "× provider_margin_multiplier × credit_per_usd"
                    ),
                },
                "items": [
                    _quota_public(
                        wallet,
                        default_monthly=default_monthly,
                        month_used=month_used.get(wallet.user_id, Decimal("0")),
                        week_used=week_used.get(wallet.user_id, Decimal("0")),
                    )
                    for wallet in wallets
                ],
            }
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc


def _explicit_limit(value: Decimal | None, *, label: str) -> Decimal:
    if value is None:
        return Decimal("0")
    parsed = Decimal(str(value))
    if not parsed.is_finite() or parsed <= 0:
        raise QuotaLimitConflict(f"{label} 한도는 0보다 큰 유한한 숫자여야 합니다")
    return parsed


async def set_default_monthly_quota(monthly_credit_limit: Decimal | None) -> dict:
    stored = _explicit_limit(monthly_credit_limit, label="기본 월")
    factory = _require_db()
    try:
        async with factory() as session, session.begin():
            row = await session.get(ChatQuotaPolicy, 1)
            if row is None:
                row = ChatQuotaPolicy(
                    id=1,
                    default_monthly_credit_limit=stored,
                )
                session.add(row)
            else:
                row.default_monthly_credit_limit = stored
            await session.flush()
            return {"default_monthly_credit_limit": _public_limit(stored)}
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc


async def set_user_quota(
    user_id: str,
    *,
    monthly_credit_limit: Decimal | None,
    weekly_credit_limit: Decimal | None,
) -> dict:
    monthly = _explicit_limit(monthly_credit_limit, label="월")
    weekly = _explicit_limit(weekly_credit_limit, label="주간")
    if monthly > 0 and weekly > monthly:
        raise QuotaLimitConflict("주간 한도는 월 한도를 초과할 수 없습니다")
    factory = _require_db()
    try:
        async with factory() as session, session.begin():
            default_monthly = await quota_policy.default_monthly_limit(session)
            wallet = await _get_or_create_wallet(session, user_id, None)
            wallet.max_quota_monthly = monthly
            wallet.max_quota_weekly = weekly
            await session.flush()
            month_used = await _ledger_credited_since(
                session,
                since=month_start(),
                user_id=user_id,
            )
            week_used = await _ledger_credited_since(
                session,
                since=week_start(),
                user_id=user_id,
            )
            return _quota_public(
                wallet,
                default_monthly=default_monthly,
                month_used=month_used,
                week_used=week_used,
            )
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc


async def reset_user_quota(user_id: str) -> dict:
    factory = _require_db()
    try:
        async with factory() as session, session.begin():
            default_monthly = await quota_policy.default_monthly_limit(session)
            wallet = await _get_or_create_wallet(session, user_id, None)
            wallet.max_quota_monthly = None
            wallet.max_quota_weekly = None
            await session.flush()
            month_used = await _ledger_credited_since(
                session,
                since=month_start(),
                user_id=user_id,
            )
            week_used = await _ledger_credited_since(
                session,
                since=week_start(),
                user_id=user_id,
            )
            return _quota_public(
                wallet,
                default_monthly=default_monthly,
                month_used=month_used,
                week_used=week_used,
            )
    except OperationalError as exc:
        mark_db_unhealthy()
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
    breakdown: UsageBreakdown | None = None,
) -> Decimal:
    """Append usage and wallet delta in the caller's transaction.

    ``prompt_tokens`` is total input. ``breakdown`` supplies the cache columns;
    without it the row records no cache split (all input uncached).
    """
    if not event_id or len(event_id) > 64:
        raise ValueError("event_id 값이 올바르지 않습니다")
    if breakdown is not None and breakdown.input_tokens != int(prompt_tokens):
        raise ValueError("usage breakdown total does not match prompt_tokens")
    cache_columns = breakdown.cache_fields() if breakdown is not None else {}
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
            **cache_columns,
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
    breakdown: UsageBreakdown | None = None,
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
                        breakdown=breakdown,
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
