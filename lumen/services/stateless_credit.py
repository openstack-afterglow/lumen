"""Atomic content-free stateless holds, sharing the durable wallet lock order."""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select, text

from lumen.models.billing import Payer
from lumen.models.chat_db import ChatUsageLog
from lumen.models.stateless_reservations import StatelessCallReservation
from lumen.services import credit
from lumen.services.durable_runs.budgets import retry_deadlocks


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class ChargeExceedsReservation(ValueError):
    pass


async def reserve(*, event_id: str, user_id: str, project_id: str, api_key_id: int | None,
                  bound: Decimal, payer: Payer | None = None) -> None:
    payer = credit.billing_payer(user_id, payer)
    async def transaction():
        async with credit._require_db()() as session, session.begin():
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
            await credit.reserve_call_credit_in_transaction(
                session, user_id=user_id, project_id=project_id, api_key_id=api_key_id, bound=bound, payer=payer,
            )
            now = _now()
            session.add(StatelessCallReservation(
                event_id=event_id, user_id=credit.actor_id(user_id, api_key_id),
                project_id=credit.actor_id(project_id, api_key_id), api_key_id=api_key_id, **payer.columns(),
                bound_credits=bound, status="reserved", created_at=now,
            ))
            await credit.refresh_domain_reserved(session, payer)
    await retry_deadlocks(transaction)


async def finish(event_id: str, *, uncertain: bool, payer: Payer | None = None) -> None:
    """Definite no-charge failures release; uncertain calls remain held until disposition."""
    async def transaction():
        async with credit._require_db()() as session, session.begin():
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
            identity = await session.get(StatelessCallReservation, event_id)
            if identity is None:
                return
            owner = _row_payer(identity)
            if payer is not None and payer != owner:
                raise ValueError("stateless reservation payer mismatch")
            await credit.lock_payer(session, owner, user_id=identity.user_id, project_id=identity.project_id,
                                    api_key_id=identity.api_key_id)
            row = (await session.execute(select(StatelessCallReservation).where(
                StatelessCallReservation.event_id == event_id).with_for_update()
                .execution_options(populate_existing=True))).scalar_one()
            if row is not None and row.status in {"reserved", "unknown"}:
                row.status = "unknown" if uncertain else "released"
                row.settled_at = None if uncertain else _now()
                if not uncertain and owner.domain_id is not None:
                    await credit.release_domain_in_transaction(
                        session, payer=owner, reference=event_id, user_id=row.user_id,
                    )
    await retry_deadlocks(transaction)


def _row_payer(row: StatelessCallReservation) -> Payer:
    if row.payer_domain_id is not None:
        return Payer("domain", None, row.payer_domain_id)
    return credit.billing_payer(row.payer_user_id or row.user_id)


async def settle(*, event_id: str, payer: Payer | None = None, **usage) -> Decimal:
    """Hold and ledger change in one transaction; retries never debit twice."""
    async def transaction():
        async with credit._require_db()() as session, session.begin():
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
            identity = await session.get(StatelessCallReservation, event_id)
            if identity is None:
                raise ValueError("stateless reservation is missing")
            owner = _row_payer(identity)
            if payer is not None and payer != owner:
                raise ValueError("stateless reservation payer mismatch")
            await credit.lock_payer(session, owner, user_id=usage["user_id"], project_id=usage["project_id"],
                                    api_key_id=usage.get("api_key_id"))
            row = (await session.execute(select(StatelessCallReservation).where(
                StatelessCallReservation.event_id == event_id).with_for_update()
                .execution_options(populate_existing=True))).scalar_one()
            key_id = usage.get("api_key_id")
            if (row.user_id != credit.actor_id(usage["user_id"], key_id)
                    or row.project_id != credit.actor_id(usage["project_id"], key_id) or row.api_key_id != key_id):
                raise ValueError("stateless reservation owner mismatch")
            if row.status == "settled":
                existing = (await session.execute(select(ChatUsageLog).where(
                    ChatUsageLog.event_id == event_id))).scalar_one()
                return existing.credited_cost
            if row.status not in {"reserved", "unknown"}:
                raise ValueError("stateless reservation is not settleable")
            actual = credit.credits_for_cost(
                usage["usage_cost"].raw_cost, usage["margin_multiplier"], usage.get("credit_per_usd"),
            )
            if actual > row.bound_credits:
                raise ChargeExceedsReservation("provider charge exceeds stateless reservation")
            await credit.apply_usage_in_transaction(session, event_id=event_id, payer=owner, **usage)
            row.actual_credits = actual
            row.status = "settled"
            row.settled_at = _now()
            await credit.refresh_domain_reserved(session, owner)
            return actual
    return await retry_deadlocks(transaction)
