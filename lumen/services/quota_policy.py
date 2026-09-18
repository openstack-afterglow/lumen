"""Resolve mutable system defaults and inherited user quota limits."""

from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal, InvalidOperation
from typing import NamedTuple

from sqlalchemy import select

from lumen.config import get_settings
from lumen.models.chat_db import ChatQuotaPolicy, UserWallet


class SystemQuota(NamedTuple):
    monthly: Decimal
    weekly: Decimal


def _nonnegative(value: object, *, label: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{label} 값이 올바르지 않습니다") from exc
    if not parsed.is_finite() or parsed < 0:
        raise ValueError(f"{label} 값은 유한한 0 이상의 숫자여야 합니다")
    return parsed


def configured_monthly_default() -> Decimal:
    """Return the deployment bootstrap default; zero means unlimited."""
    return _nonnegative(get_settings().chat_default_monthly_quota, label="기본 월 쿼터")


async def default_monthly_limit(session) -> Decimal:
    """Return the runtime default, falling back to deployment configuration."""
    row = await session.get(ChatQuotaPolicy, 1)
    if row is None:
        return configured_monthly_default()
    return _nonnegative(row.default_monthly_credit_limit, label="기본 월 쿼터")


def effective_limit(override: object | None, default: Decimal) -> Decimal:
    """Resolve NULL inheritance while preserving zero as explicit unlimited."""
    if override is None:
        return default
    return _nonnegative(override, label="사용자 쿼터")


async def get_system_quota(session, user_id: str) -> SystemQuota:
    default_monthly = await default_monthly_limit(session)
    wallet = await session.get(UserWallet, user_id)
    if wallet is None:
        return SystemQuota(default_monthly, Decimal("0"))
    return SystemQuota(
        effective_limit(wallet.max_quota_monthly, default_monthly),
        effective_limit(wallet.max_quota_weekly, Decimal("0")),
    )


async def get_system_quotas_batch(session, user_ids: Iterable[str]) -> dict[str, SystemQuota]:
    ids = set(user_ids)
    if not ids:
        return {}
    default_monthly = await default_monthly_limit(session)
    quotas = {user_id: SystemQuota(default_monthly, Decimal("0")) for user_id in ids}
    rows = (
        await session.execute(
            select(UserWallet.user_id, UserWallet.max_quota_monthly, UserWallet.max_quota_weekly).where(
                UserWallet.user_id.in_(ids)
            )
        )
    ).all()
    for user_id, monthly_override, weekly_override in rows:
        quotas[user_id] = SystemQuota(
            effective_limit(monthly_override, default_monthly),
            effective_limit(weekly_override, Decimal("0")),
        )
    return quotas
