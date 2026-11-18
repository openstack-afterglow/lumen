"""Prepaid pools. Lock pool → user wallet → project quota → run/hold rows.

READ COMMITTED makes unlocked hold aggregates current after waiting for a pool.
Keys without a human actor skip the wallet; admin mutations only lock the pool.
"""
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import case, func, select, true
from sqlalchemy.dialects.mysql import insert

from lumen.models.billing import DomainCreditLedger, DomainCreditPool, utc_now
from lumen.models.chat_infrastructure import ChatAgentReservation
from lumen.models.chat_runs import ChatModelCallReservation, ChatRun
from lumen.models.stateless_reservations import StatelessCallReservation

ZERO = Decimal(0)
UNSETTLED = ("reserved", "unknown")


def current_period():
    return datetime.now(UTC).date().replace(day=1)


def effective_limit(pool: DomainCreditPool) -> Decimal | None:
    return min((value for value in (pool.manager_monthly_limit_credits, pool.admin_ceiling_credits)
                if value is not None), default=None)


async def lock_pool(session, domain_id: str, *, create: bool = False) -> DomainCreditPool | None:
    transaction = session.get_transaction()
    marker, locked = session.info.get("locked_domain_pools", (None, {}))
    if marker is not transaction:
        locked = {}
        session.info["locked_domain_pools"] = transaction, locked
    if domain_id in locked:
        return locked[domain_id]
    if create:
        await session.execute(insert(DomainCreditPool).values(
            domain_id=domain_id, period_start=current_period(), updated_at=utc_now(),
        ).on_duplicate_key_update(domain_id=DomainCreditPool.domain_id))
    pool = await session.scalar(select(DomainCreditPool).where(DomainCreditPool.domain_id == domain_id)
                                .with_for_update().execution_options(populate_existing=True))
    if pool is not None:
        if pool.period_start != current_period():
            pool.period_start = current_period()
            pool.monthly_used_credits = ZERO
        locked[domain_id] = pool
    return pool


async def pending(session, domain_id: str | None, *, actor_user_id: str | None = None,
                  api_key_id: int | None = None, user_funded: bool = False) -> Decimal:
    """Count provider holds plus unused child allocations; no domain means all payers."""
    call = ChatModelCallReservation
    stateless = StatelessCallReservation
    allocation = ChatAgentReservation

    def funding(column):
        if user_funded:
            return column.is_(None)
        return true() if domain_id is None else column == domain_id

    calls = select(func.coalesce(func.sum(call.bound_credits), 0)).where(
        funding(call.payer_domain_id), call.status.in_(UNSETTLED))
    stateless_calls = select(func.coalesce(func.sum(stateless.bound_credits), 0)).where(
        funding(stateless.payer_domain_id), stateless.status.in_(UNSETTLED))
    committed_child = select(func.coalesce(func.sum(case(
        (call.status.in_(UNSETTLED), call.bound_credits), else_=call.actual_credits)), 0)).where(
        call.run_id == allocation.run_id, call.payer_domain_id.is_not_distinct_from(allocation.payer_domain_id),
    ).scalar_subquery()
    allocations = select(func.coalesce(func.sum(func.greatest(0, allocation.amount - committed_child)), 0)).where(
        funding(allocation.payer_domain_id), allocation.kind == "credit", allocation.status == "reserved",
    )
    if actor_user_id is not None or api_key_id is not None:
        calls = calls.join(ChatRun, ChatRun.id == call.run_id)
        allocations = allocations.join(ChatRun, ChatRun.id == allocation.run_id)
    if actor_user_id is not None:
        calls = calls.where(ChatRun.user_id == actor_user_id)
        allocations = allocations.where(ChatRun.user_id == actor_user_id)
        stateless_calls = stateless_calls.where(stateless.user_id == actor_user_id)
    if api_key_id is not None:
        calls = calls.where(ChatRun.api_key_id == api_key_id)
        allocations = allocations.where(ChatRun.api_key_id == api_key_id)
        stateless_calls = stateless_calls.where(stateless.api_key_id == api_key_id)
    return await session.scalar(select(calls.scalar_subquery() + stateless_calls.scalar_subquery()
                                       + allocations.scalar_subquery()))


async def refresh_reserved(session, pool: DomainCreditPool) -> None:
    await session.flush()
    pool.reserved_credits = await pending(session, pool.domain_id)


def append_entry(session, pool: DomainCreditPool, *, kind: str, amount: Decimal, reference: str,
                 actor_user_id: str | None, note: str | None = None) -> None:
    pool.balance_credits += amount
    session.add(DomainCreditLedger(domain_id=pool.domain_id, kind=kind, amount_credits=amount,
                                   balance_after=pool.balance_credits, actor_user_id=actor_user_id,
                                   reference=reference, note=note))


def public(pool: DomainCreditPool | None, domain_id: str, *, reserved: Decimal | None = None) -> dict:
    def decimal(value):
        return format(value, "f") if value is not None else None

    period = current_period()
    return {
        "domain_id": domain_id,
        "balance_credits": decimal(pool.balance_credits if pool else ZERO),
        "reserved_credits": decimal(reserved if reserved is not None else pool.reserved_credits if pool else ZERO),
        "monthly_used_credits": decimal(pool.monthly_used_credits if pool and pool.period_start == period else ZERO),
        "period_start": period.isoformat(),
        "manager_monthly_limit_credits": decimal(pool.manager_monthly_limit_credits if pool else None),
        "admin_ceiling_credits": decimal(pool.admin_ceiling_credits if pool else None),
        "effective_monthly_limit_credits": decimal(effective_limit(pool) if pool else None),
        "is_active": pool.is_active if pool else False,
        "updated_at": pool.updated_at.isoformat() if pool else None,
    }
