"""빌트인 AI 채팅 크레딧/쿼터 관리 (MySQL user_wallets + chat_usage_logs).

과금 모델: 모델별 raw_cost(USD) × margin_multiplier × credit_per_usd = credited_cost(크레딧).
월간 쿼터 상한(used_quota_this_month ≥ max_quota_monthly)을 초과하면 요청을 차단한다.

- Payer locks come before project quota, runs and holds; all accounting reads use READ COMMITTED.
- Domain credits are prepaid; user wallets retain their existing quota behavior.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import UTC, date, datetime
from decimal import ROUND_CEILING, ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.exc import IntegrityError, OperationalError

from lumen.config import get_settings
from lumen.db import get_session_factory, is_db_available, mark_db_unhealthy
from lumen.models.billing import DomainCreditLedger, Payer
from lumen.models.chat_db import ChatApiKey, ChatProviderUsage, ChatQuotaPolicy, ChatUsageLog, UserWallet
from lumen.services import domain_credit, quota_policy
from lumen.services.api_key_store import calculate_effective_limit
from lumen.services.provider_metadata import UsageCost
from lumen.services.providers.credentials import execution_identity
from lumen.services.quota_periods import month_start, week_start
from lumen.services.usage_breakdown import UsageBreakdown, modality_token_charges

logger = logging.getLogger(__name__)

_CREDIT_QUANTUM = Decimal("0.00000001")  # DECIMAL(18,8)

_USD_QUANTUM = Decimal("0.0000000001")


class ChatStorageUnavailable(RuntimeError):
    """chat DB 미구성/장애 — fail-closed(503)."""


class QuotaExceeded(RuntimeError):
    """월 쿼터 초과 — 402."""

class DomainCreditInsufficient(QuotaExceeded):
    code = "domain_credit_insufficient"

    def __init__(self):
        super().__init__(self.code)


def billing_payer(user_id: str, payer: Payer | None = None) -> Payer:
    return payer if payer is not None else Payer("user", user_id, None)


def run_payer(run) -> Payer:
    domain_id = getattr(run, "payer_domain_id", None)
    return Payer("domain", None, domain_id) if domain_id else billing_payer(run.user_id)


def actor_id(value: str | None, api_key_id: int | None) -> str | None:
    return None if api_key_id is not None and value == f"lumen:key:{api_key_id}" else value


async def lock_wallet(session, user_id: str, project_id: str | None):
    transaction = session.get_transaction()
    marker, locked = session.info.get("locked_user_wallets", (None, {}))
    if marker is not transaction:
        locked = {}
        session.info["locked_user_wallets"] = transaction, locked
    if user_id in locked:
        return locked[user_id]
    wallet = await session.scalar(select(UserWallet).where(UserWallet.user_id == user_id)
                                  .with_for_update().execution_options(populate_existing=True))
    if wallet is None:
        wallet = await _get_or_create_wallet(session, user_id, project_id)
    locked[user_id] = wallet
    return wallet


async def lock_payer(session, payer: Payer, *, user_id: str, project_id: str | None, api_key_id: int | None):
    """Lock pool, then the real user's wallet if needed; do not lock project/run rows here."""
    if payer.domain_id is not None:
        pool = await domain_credit.lock_pool(session, payer.domain_id)
        if pool is None:
            raise DomainCreditInsufficient()
        if api_key_id is None:
            await lock_wallet(session, user_id, project_id)
        return pool
    return await lock_wallet(session, payer.user_id, project_id)


async def lock_run_payer(session, run_id: str):
    from lumen.models.chat_runs import ChatRun

    row = (await session.execute(select(
        ChatRun.user_id, ChatRun.project_id, ChatRun.api_key_id, ChatRun.payer_domain_id,
    ).where(ChatRun.id == run_id))).one_or_none()
    if row is not None:
        await lock_payer(session, run_payer(row), user_id=row.user_id,
                         project_id=row.project_id, api_key_id=row.api_key_id)


async def charge_domain_in_transaction(session, *, payer: Payer, amount: Decimal, reference: str,
                                       user_id: str | None) -> None:
    if payer.domain_id is None:
        raise ValueError("domain payer required")
    pool = await domain_credit.lock_pool(session, payer.domain_id)
    if pool is None or amount > pool.balance_credits:
        raise DomainCreditInsufficient()
    pool.monthly_used_credits += amount
    domain_credit.append_entry(session, pool, kind="charge", amount=-amount, reference=reference,
                               actor_user_id=user_id)


async def release_domain_in_transaction(session, *, payer: Payer, reference: str, user_id: str | None) -> None:
    """Release a proven unused hold; no balance was debited, so its ledger delta is zero."""
    if payer.domain_id is not None:
        pool = await domain_credit.lock_pool(session, payer.domain_id)
        domain_credit.append_entry(session, pool, kind="refund", amount=Decimal(0), reference=reference,
                                   actor_user_id=user_id, note="unused reservation released")
        await domain_credit.refresh_reserved(session, pool)


async def refresh_domain_reserved(session, payer: Payer) -> None:
    if payer.domain_id is not None:
        pool = await domain_credit.lock_pool(session, payer.domain_id)
        if pool is not None:
            await domain_credit.refresh_reserved(session, pool)



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
    if value is None:
        return None
    price = Decimal(str(value))
    if not price.is_finite() or price < 0:
        raise ValueError("durable pricing snapshot is invalid")
    return price


def long_context_threshold_tokens(pricing_snapshot: dict) -> int:
    """Read the frozen threshold, retaining the legacy tier boundary when absent."""
    threshold = pricing_snapshot.get("long_context_threshold_tokens", 200_000)
    if type(threshold) is not int or threshold <= 0:
        raise ValueError("durable pricing snapshot is invalid")
    return threshold


def usage_cost_from_pricing_snapshot(
    pricing_snapshot: dict,
    *,
    prompt_tokens: int,
    completion_tokens: int,
    breakdown: UsageBreakdown | None = None,
    required_modalities: Iterable[str] | None = None,
) -> UsageCost:
    """Calculate token usage exclusively from a durable run's frozen prices.

    ``prompt_tokens`` is total input; the input rate applies to its uncached
    share. A cache category without a frozen rate — including every snapshot
    frozen before cache rates existed — bills 0 and marks the usage partial.
    Frozen ``token_rates`` bill image/audio shares at their own rates. Catalog
    buckets marked ``token_rates_follow_text`` retain text's context tier when
    equivalent; manual rates remain literal. Explicit modality tiers use the
    same threshold. A ``None`` text rate requires a zero text residual.

    ``required_modalities`` describes this actual provider call. An explicit
    empty iterable permits a genuinely text-only segment after media was
    compacted away; omitting it preserves legacy admission requirements.
    """
    try:
        input_price = _snapshot_price(pricing_snapshot, "input_price_per_token", required=True)
        output_price = _snapshot_price(pricing_snapshot, "output_price_per_token", required=True)
        input_tier = _snapshot_price(pricing_snapshot, "input_price_per_token_above_200k", required=False)
        output_tier = _snapshot_price(pricing_snapshot, "output_price_per_token_above_200k", required=False)
        threshold = long_context_threshold_tokens(pricing_snapshot)
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
    base_text_rates = {
        "input": input_price,
        "output": output_price,
        "cache_read": cache_prices["cache_read"][0],
    }
    if breakdown.input_tokens > threshold:
        if input_tier is not None:
            input_price = input_tier
        if output_tier is not None:
            output_price = output_tier
    frozen_required = pricing_snapshot.get("required_token_modalities") or ()
    if not isinstance(frozen_required, (list, tuple)):
        raise ValueError("durable pricing snapshot is invalid")
    text, modality_charges = modality_token_charges(
        breakdown,
        pricing_snapshot.get("token_rates"),
        frozen_required if required_modalities is None else required_modalities,
        base_text_rates=base_text_rates,
        token_rates_above_200k=pricing_snapshot.get("token_rates_above_200k"),
        token_rates_follow_text=pricing_snapshot.get("token_rates_follow_text") is True,
        above_threshold=breakdown.input_tokens > threshold,
    )
    uncached_tokens = text.uncached_input_tokens
    for price, tokens in ((input_price, uncached_tokens), (output_price, text.output_tokens)):
        if price is None and tokens:
            raise ValueError("text token pricing is unavailable for reported usage")
    input_cost = ((input_price or Decimal("0")) * uncached_tokens).quantize(_USD_QUANTUM, rounding=ROUND_HALF_EVEN)
    output_cost = ((output_price or Decimal("0")) * text.output_tokens).quantize(
        _USD_QUANTUM, rounding=ROUND_HALF_EVEN
    )
    components: dict[str, dict[str, object]] = {
        "input": {
            "tokens": uncached_tokens,
            "price_per_token": format(input_price, "f") if input_price is not None else None,
            "cost": format(input_cost, "f"),
        },
        "output": {
            "tokens": text.output_tokens,
            "price_per_token": format(output_price, "f") if output_price is not None else None,
            "cost": format(output_cost, "f"),
        },
    }
    cache_costs: dict[str, Decimal] = {}
    unpriced_cache = False
    sources = pricing_snapshot.get("cache_price_sources") or {}
    for name, token_field, _, _ in _CACHE_SNAPSHOT_PRICES:
        tokens = getattr(text, token_field)
        rate, tier_rate = cache_prices[name]
        source = sources.get(name, "manual")
        if tokens and breakdown.input_tokens > threshold and sources.get(f"{name}_above_200k") == "unpublished":
            raise ValueError("long-context cache-write rate is unpublished")
        # Frozen tiers exist only for real catalog rates (own or inherited by a fallback write).
        if breakdown.input_tokens > threshold and source != "manual" and tier_rate is not None:
            rate = tier_rate
            # A fallback base and its real catalog tier carry separate provenance.
            source = sources.get(f"{name}_above_200k") or source
        if tokens and rate is None and pricing_snapshot.get("model_kind", "text") != "text":
            # Legacy text snapshots bill an unpriced cache at 0 (partial); media never does.
            raise ValueError(f"media {name} token pricing is unavailable for reported usage")
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
    for charge in modality_charges:
        cost = (charge.price_per_token * charge.tokens).quantize(_USD_QUANTUM, rounding=ROUND_HALF_EVEN)
        raw_cost += cost
        if charge.name.endswith("_cache_read"):
            cache_costs["cache_read"] += cost
        elif charge.name.endswith("_input"):
            input_cost += cost
        else:
            output_cost += cost
        components[charge.name] = {
            "tokens": charge.tokens,
            "price_per_token": format(charge.price_per_token, "f"),
            "cost": format(cost, "f"),
        }
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


# (token_components key, ledger usage kind); cache and modality kinds appear only with tokens.
TOKEN_USAGE_COMPONENT_KINDS = (
    ("input", "input_tokens"),
    ("output", "output_tokens"),
    ("cache_read", "cache_read_input_tokens"),
    ("cache_creation_5m", "cache_creation_5m_input_tokens"),
    ("cache_creation_1h", "cache_creation_1h_input_tokens"),
    *(
        (f"{modality}_{category}", f"{modality}_{kind}")
        for modality in ("image", "audio")
        for category, kind in (
            ("input", "input_tokens"),
            ("cache_read", "cache_read_input_tokens"),
            ("output", "output_tokens"),
        )
    ),
)


def token_usage_components(
    usage_cost: UsageCost,
    *,
    segment_id: str,
    source: str,
    model_name: str,
    metadata: dict[str, Any],
) -> list[dict[str, Any]]:
    """Ledger token components of a ``usage_cost_from_pricing_snapshot`` result.

    ``input_tokens`` is the uncached TEXT share; text input/output always
    appear, other categories only with tokens.
    """
    token_components = usage_cost.pricing_snapshot.get("token_components")
    if not isinstance(token_components, dict):
        raise ValueError("usage cost has no token components")
    components: list[dict[str, Any]] = []
    for key, kind in TOKEN_USAGE_COMPONENT_KINDS:
        item = token_components.get(key)
        if not isinstance(item, dict):
            continue
        quantity = int(item["tokens"])
        if key not in ("input", "output") and quantity == 0:
            continue
        cost = Decimal(str(item["cost"]))
        components.append(
            {
                "segment_id": segment_id,
                "kind": kind,
                "quantity": str(quantity),
                "unit": "token",
                "unit_price_usd": format(cost / quantity if quantity else Decimal("0"), "f"),
                "cost_usd": format(cost, "f"),
                "source": source,
                "model_name": model_name,
                "metadata": dict(metadata),
            }
        )
    return components


_WORST_CASE_INPUT_KEYS = tuple(
    key for _, _, rate_key, tier_key in _CACHE_SNAPSHOT_PRICES for key in (rate_key, tier_key)
)


def worst_case_token_rates(pricing_snapshot: dict) -> tuple[Decimal, Decimal, Decimal]:
    """``(input_per_token, output_per_token, rounding_cushion_usd)`` bounding any settlement.

    Maximizes over the frozen text input/cache rates and every image/audio
    input/cache/output rate; the cushion covers one 1e-10 USD rounding per
    billed token category. Absent rates contribute nothing, never a price.
    """
    inputs: list[Decimal] = []
    outputs: list[Decimal] = []
    categories = 5
    modalities: set[str] = set()
    try:
        for key in ("input_price_per_token", "input_price_per_token_above_200k", *_WORST_CASE_INPUT_KEYS):
            if pricing_snapshot.get(key) is not None:
                inputs.append(Decimal(str(pricing_snapshot[key])))
        for key in ("output_price_per_token", "output_price_per_token_above_200k"):
            if pricing_snapshot.get(key) is not None:
                outputs.append(Decimal(str(pricing_snapshot[key])))
        for rates_key in ("token_rates", "token_rates_above_200k"):
            token_rates = pricing_snapshot.get(rates_key) or {}
            if not isinstance(token_rates, dict):
                raise ValueError("durable pricing snapshot is invalid")
            for modality, fields in token_rates.items():
                if not isinstance(fields, dict):
                    raise ValueError("durable pricing snapshot is invalid")
                if modality not in modalities:
                    modalities.add(modality)
                    categories += 3
                for key, value in fields.items():
                    rate = Decimal(str(value)) / Decimal("1000000")
                    (outputs if key == "output_per_million" else inputs).append(rate)
    except (InvalidOperation, TypeError) as exc:
        raise ValueError("durable pricing snapshot is invalid") from exc
    if any(not rate.is_finite() or rate < 0 for rate in (*inputs, *outputs)):
        raise ValueError("durable pricing snapshot is invalid")
    return (
        max(inputs, default=Decimal("0")),
        max(outputs, default=Decimal("0")),
        _USD_QUANTUM * categories,
    )


def call_credit_bound(pricing_snapshot: dict, *, input_tokens: int, output_tokens: int) -> Decimal:
    """Worst-case credits for one provider call under frozen prices, rounded upward.

    ``pricing_snapshot`` carries the frozen token rates plus ``margin_multiplier``
    and ``chat_credit_per_usd``; it is never a live catalog price. A free route
    (every frozen rate zero) bounds to exactly zero. Raises ``ValueError``.
    """
    try:
        input_rate, output_rate, cushion = worst_case_token_rates(pricing_snapshot)
        multiplier = Decimal(str(pricing_snapshot["margin_multiplier"])) * Decimal(
            str(pricing_snapshot["chat_credit_per_usd"])
        )
    except (KeyError, InvalidOperation, TypeError) as exc:
        raise ValueError("durable pricing snapshot is invalid") from exc
    if not multiplier.is_finite() or multiplier < 0:
        raise ValueError("invalid credit conversion")
    cost = input_rate * input_tokens + output_rate * output_tokens
    return ((cost + (cushion if cost else 0)) * multiplier).quantize(_CREDIT_QUANTUM, rounding=ROUND_CEILING)


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
        # A no-op upsert, not an ORM insert: concurrent first requests take the duplicate-key check's exclusive
        # lock in turn instead of failing with 1062 or deadlocking while holding shared locks.
        await session.execute(mysql_insert(UserWallet).values(
            user_id=user_id, project_id=project_id, quota_period_start=_first_of_month(datetime.now(UTC).date()),
        ).on_duplicate_key_update(user_id=UserWallet.user_id))
        # The row may have been committed after this transaction's snapshot: only a locking read sees it.
        wallet = (await session.execute(select(UserWallet).where(UserWallet.user_id == user_id)
                  .with_for_update().execution_options(populate_existing=True))).scalar_one()
    return wallet


async def _ledger_credited_since(
    session,
    *,
    since: datetime,
    user_id: str | None = None,
    api_key_id: int | None = None,
    user_payer_only: bool = False,
    api_only: bool = False,
) -> Decimal:
    stmt = select(func.coalesce(func.sum(ChatUsageLog.credited_cost), Decimal("0"))).where(
        ChatUsageLog.created_at >= since
    )
    if user_id is not None:
        stmt = stmt.where(ChatUsageLog.user_id == user_id, ChatUsageLog.source != "system")
    if user_payer_only:
        stmt = stmt.where(ChatUsageLog.payer_domain_id.is_(None))
    if api_key_id is not None:
        stmt = stmt.where(ChatUsageLog.api_key_id == api_key_id)
    if api_only:
        stmt = stmt.where(ChatUsageLog.source == "api")
    return Decimal(str((await session.execute(stmt)).scalar_one()))


async def reserve_call_credit_in_transaction(session, *, user_id: str, project_id: str | None,
                                             api_key_id: int | None, bound: Decimal,
                                             payer: Payer | None = None, precheck: bool = False) -> None:
    """Check bounded headroom under the payer lock, before recording the caller's hold."""
    payer = billing_payer(user_id, payer)

    if not bound.is_finite() or bound < 0:
        raise QuotaExceeded("호출 사용 한도가 올바르지 않습니다")
    if payer.scope == "user" and payer.user_id != user_id:
        raise ValueError("user payer does not match billing actor")
    account = await lock_payer(session, payer, user_id=user_id, project_id=project_id, api_key_id=api_key_id)

    def exceeded(used, limit):
        return used + bound > limit or (precheck and used >= limit)

    if payer.domain_id is not None:
        pending = await domain_credit.pending(session, payer.domain_id)
        limit = domain_credit.effective_limit(account)
        if (not account.is_active or exceeded(pending, account.balance_credits)
                or (limit is not None and exceeded(account.monthly_used_credits + pending, limit))):
            raise DomainCreditInsufficient()
        if api_key_id is None:
            wallet = await session.get(UserWallet, user_id)
            _maybe_reset_month(wallet)
            if not wallet.is_active:
                raise QuotaExceeded("비활성 지갑입니다")
            system = await quota_policy.get_system_quota(session, user_id)
            await _check_actor_monthly(session, user_id, wallet, system.monthly, exceeded)
        else:
            key = await _domain_key(session, api_key_id, payer.domain_id)
            if any(value is not None for value in (key.owner_monthly_credit_limit, key.admin_monthly_credit_limit,
                                                   key.owner_weekly_credit_limit)):
                key_pending = await domain_credit.pending(session, payer.domain_id, api_key_id=api_key_id)
                await _check_key_caps(session, key, key_pending, exceeded)
        return

    wallet = account
    _maybe_reset_month(wallet)
    if not wallet.is_active:
        raise QuotaExceeded("비활성 지갑입니다")
    system = await quota_policy.get_system_quota(session, user_id)
    await _check_actor_monthly(session, user_id, wallet, system.monthly, exceeded)
    if system.weekly > 0:
        usage = await _ledger_credited_since(session, since=week_start(), user_id=payer.user_id, user_payer_only=True)
        pending = await domain_credit.pending(session, None, actor_user_id=user_id, user_funded=True)
        if exceeded(usage + pending, system.weekly):
            raise QuotaExceeded("주간 사용 한도를 초과했습니다")
    if api_key_id is not None:
        key = await session.get(ChatApiKey, api_key_id)
        partitioned_key = project_id == f"lumen:key:{api_key_id}"
        if (key is None or key.owner_user_id != payer.user_id
                or (not partitioned_key and project_id is not None and key.owner_project_id != project_id)
                or not key.is_active or key.revoked_at is not None):
            raise ChatStorageUnavailable("chat DB 오류")
        key_pending = await domain_credit.pending(session, None, actor_user_id=user_id,
                                                  api_key_id=api_key_id, user_funded=True)
        await _check_key_caps(session, key, key_pending, exceeded, monthly_default=system.monthly,
                              weekly_default=system.weekly)


async def _check_actor_monthly(session, user_id: str, wallet: UserWallet, limit: Decimal, exceeded) -> None:
    """Account for all payer paths under the common actor wallet lock."""
    if limit <= 0:
        return
    ledger = DomainCreditLedger
    domain_spend = await session.scalar(select(func.coalesce(func.sum(-ledger.amount_credits), 0)).where(
        ledger.actor_user_id == user_id, ledger.kind == "charge", ledger.created_at >= month_start(),
    ))
    pending = await domain_credit.pending(session, None, actor_user_id=user_id)
    if exceeded(wallet.used_quota_this_month + domain_spend + pending, limit):
        raise QuotaExceeded("월 사용 한도를 초과했습니다")


async def _domain_key(session, api_key_id: int, domain_id: str) -> ChatApiKey:
    key = await session.get(ChatApiKey, api_key_id)
    if (key is None or getattr(key, "owner_domain_id", None) != domain_id
            or not key.is_active or key.revoked_at is not None):
        raise ChatStorageUnavailable("chat DB 오류")
    return key


async def _check_key_caps(session, key, pending, exceeded, *, monthly_default=Decimal(0), weekly_default=Decimal(0)):
    monthly = calculate_effective_limit(key.owner_monthly_credit_limit, key.admin_monthly_credit_limit, monthly_default)
    if monthly is not None:
        usage = await _ledger_credited_since(session, since=month_start(), api_key_id=key.id, api_only=True)
        if exceeded(usage + pending, monthly):
            raise QuotaExceeded("API 키 월 사용 한도를 초과했습니다")
    weekly = calculate_effective_limit(key.owner_weekly_credit_limit, None, weekly_default)
    if weekly is not None:
        usage = await _ledger_credited_since(session, since=week_start(), api_key_id=key.id, api_only=True)
        if exceeded(usage + pending, weekly):
            raise QuotaExceeded("API 키 주간 사용 한도를 초과했습니다")


async def precheck(user_id: str, project_id: str | None = None, api_key_id: int | None = None,
                   *, bound: Decimal | None = None, payer: Payer | None = None) -> None:
    """Reject exhausted quotas, or insufficient headroom when a call bound is known.

    No hold is recorded here. The worker reruns the bound check under the same
    wallet lock when committing the durable reservation and I/O intent.
    """
    from lumen.services.durable_runs.budgets import retry_deadlocks

    if bound is None and payer is None:
        await _precheck_user(user_id, project_id, api_key_id)
        return

    async def check_bound():
        async with _require_db()() as session, session.begin():
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
            await reserve_call_credit_in_transaction(session, user_id=user_id, project_id=project_id,
                                                      api_key_id=api_key_id, payer=payer,
                                                      bound=bound if bound is not None else Decimal(0),
                                                      precheck=bound is None)

    try:
        await retry_deadlocks(check_bound)
    except OperationalError as exc:  # only a lock conflict that outlived the retries reaches here
        raise ChatStorageUnavailable("chat DB 오류") from exc

async def _precheck_user(user_id: str, project_id: str | None, api_key_id: int | None) -> None:
    """Keep the existing advisory wallet check; bounded calls recheck under lock."""
    from lumen.services.durable_runs.budgets import is_lock_conflict, retry_deadlocks

    async def transaction():
        async with _require_db()() as session, session.begin():
            wallet = await _get_or_create_wallet(session, user_id, project_id)
            _maybe_reset_month(wallet)
            system = await quota_policy.get_system_quota(session, user_id)
            if not wallet.is_active:
                raise QuotaExceeded("비활성 지갑입니다")
            if system.monthly > 0 and wallet.used_quota_this_month >= system.monthly:
                raise QuotaExceeded("월 사용 한도를 초과했습니다")
            if system.weekly > 0:
                used = await _ledger_credited_since(session, since=week_start(), user_id=user_id, user_payer_only=True)
                if used >= system.weekly:
                    raise QuotaExceeded("주간 사용 한도를 초과했습니다")
            if api_key_id is not None:
                key = await session.get(ChatApiKey, api_key_id)
                if (key is None or key.owner_user_id != user_id or not key.is_active or key.revoked_at is not None
                        or (project_id not in (None, f"lumen:key:{api_key_id}", key.owner_project_id))):
                    raise ChatStorageUnavailable("chat DB 오류")
                await _check_key_caps(session, key, Decimal(0), lambda used, limit: used >= limit,
                                      monthly_default=system.monthly, weekly_default=system.weekly)

    try:
        await retry_deadlocks(transaction)
    except (QuotaExceeded, ChatStorageUnavailable):
        raise
    except OperationalError as exc:
        if not is_lock_conflict(exc):
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

def _provider_usage_allocations(
    route: dict, components: list[dict[str, Any]] | None, *, prompt_tokens: int,
    completion_tokens: int, raw_cost: Decimal,
) -> list[dict[str, Any]]:
    """Allocate observed upstream components without charging the wallet twice."""
    grouped: dict[tuple[int, int], dict[str, Any]] = {}

    def allocation(identity: dict) -> dict[str, Any] | None:
        fields = ("provider_id", "provider_account_id", "provider_credential_id", "provider_credential_generation")
        if any(type(identity.get(key)) is not int for key in fields):
            return None
        if any(identity[key] <= 0 for key in fields[:3]) or identity[fields[3]] < 0:
            raise ValueError("invalid provider usage identity")
        key = (identity["provider_account_id"], identity["provider_credential_id"])
        if key not in grouped:
            grouped[key] = {**{name: identity[name] for name in fields},
                "provider_backend": identity.get("provider_backend"),
                "provider_backend_version": identity.get("provider_backend_version"),
                "prompt_tokens": 0, "completion_tokens": 0, "raw_cost": Decimal("0")}
        elif any(grouped[key][name] != identity[name] for name in fields):
            raise ValueError("conflicting provider usage identity")
        return grouped[key]

    for component in components or ():
        metadata = component.get("metadata") or {}
        if not isinstance(metadata, dict):
            raise ValueError("invalid provider usage metadata")
        item = allocation(metadata)
        if item is None:
            continue
        cost = Decimal(str(component.get("cost_usd", "0")))
        if not cost.is_finite() or cost < 0:
            raise ValueError("invalid provider component cost")
        item["raw_cost"] += cost
        if component.get("unit") == "token":
            quantity = Decimal(str(component["quantity"]))
            if not quantity.is_finite() or quantity < 0 or quantity != quantity.to_integral_value():
                raise ValueError("invalid provider component tokens")
            direction = "completion_tokens" if "output" in str(component.get("kind")) else "prompt_tokens"
            item[direction] += int(quantity)
    if not grouped:
        item = allocation(route)
        if item is not None:
            item.update(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens, raw_cost=raw_cost)
    if sum((item["raw_cost"] for item in grouped.values()), Decimal("0")) > raw_cost:
        raise ValueError("provider allocations exceed the customer usage cost")
    return list(grouped.values())



async def apply_usage_in_transaction(
    session,
    *,
    event_id: str,
    user_id: str | None,
    project_id: str | None,
    payer: Payer | None = None,
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
    settled_credits: Decimal | None = None,
    usage_components: list[dict[str, Any]] | None = None,
    breakdown: UsageBreakdown | None = None,
    execution_route: dict[str, Any] | None = None,
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
    payer = billing_payer(user_id, payer)
    if settled_credits is not None and (payer.domain_id is None or charge_wallet):
        raise ValueError("only a prepaid usage projection can supply settled credits")
    credited = (settled_credits if settled_credits is not None
                else credits_for_cost(usage_cost.raw_cost, margin_multiplier, credit_per_usd))
    pricing_snapshot = {
        **usage_cost.pricing_snapshot,
        "margin_multiplier": format(Decimal(margin_multiplier), "f"),
        "chat_credit_per_usd": format(
            Decimal(str(get_settings().chat_credit_per_usd if credit_per_usd is None else credit_per_usd)),
            "f",
        ),
        "credited_cost": format(credited, "f"),
    }
    from lumen.services.providers.relay_history import current_receipts

    pricing_snapshot["provider_relays"] = current_receipts(event_id)
    session.add(
        ChatUsageLog(
            event_id=event_id,
            project_id=actor_id(project_id, api_key_id),
            user_id=actor_id(user_id, api_key_id),
            domain_id=payer.domain_id,
            billing_scope=payer.scope,
            **payer.columns(),
            conversation_id=conversation_id,
            model_name=model_name,
            provider=provider,
            provider_id=(execution_route or {}).get("provider_id"),
            **{
                key: value for key, value in execution_identity(execution_route or {}).items()
                if key in {
                    "provider_account_id", "provider_credential_id", "provider_credential_generation",
                    "provider_backend", "provider_backend_version",
                }
            },
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
    for allocation in _provider_usage_allocations(
        execution_route or {}, usage_components, prompt_tokens=int(prompt_tokens),
        completion_tokens=int(completion_tokens), raw_cost=usage_cost.raw_cost,
    ):
        session.add(ChatProviderUsage(event_id=event_id, **allocation))
    # The unique event row comes first: a concurrent replay of the same event blocks on its key and
    # then replays, instead of racing this transaction to the wallet or pool row.
    await session.flush()
    if charge_wallet and payer.domain_id is not None:
        await charge_domain_in_transaction(session, payer=payer, amount=credited, reference=event_id,
                                           user_id=actor_id(user_id, api_key_id))
    if charge_wallet and payer.scope == "user":
        wallet = await _get_or_create_wallet(session, payer.user_id, project_id)
        _maybe_reset_month(wallet)
        await session.execute(
            update(UserWallet)
            .where(UserWallet.user_id == payer.user_id)
            .values(used_quota_this_month=UserWallet.used_quota_this_month + credited)
        )
    await session.flush()
    return credited


async def apply_usage(
    *,
    event_id: str,
    user_id: str,
    project_id: str,
    payer: Payer | None = None,
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
    execution_route: dict[str, Any] | None = None,
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
                        payer=payer,
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
                        execution_route=execution_route,
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
