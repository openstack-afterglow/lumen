"""Atomic content-free stateless holds, sharing the durable wallet lock order."""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select, text

from lumen.models.chat_db import ChatUsageLog, UserWallet
from lumen.models.stateless_reservations import StatelessCallReservation
from lumen.services import credit
from lumen.services.durable_runs.budgets import retry_deadlocks


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


async def reserve(*, event_id: str, user_id: str, project_id: str, api_key_id: int | None, bound: Decimal) -> None:
    async def transaction():
        async with credit._require_db()() as session, session.begin():
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
            await credit.reserve_call_credit_in_transaction(
                session, user_id=user_id, project_id=project_id, api_key_id=api_key_id, bound=bound,
            )
            now = _now()
            session.add(StatelessCallReservation(
                event_id=event_id, user_id=user_id, project_id=project_id, api_key_id=api_key_id,
                bound_credits=bound, status="reserved", created_at=now,
            ))
    await retry_deadlocks(transaction)


async def finish(event_id: str, *, uncertain: bool) -> None:
    """Definite no-charge failures release; uncertain calls remain held until disposition."""
    async def transaction():
        async with credit._require_db()() as session, session.begin():
            row = (await session.execute(select(StatelessCallReservation).where(
                StatelessCallReservation.event_id == event_id).with_for_update())).scalar_one_or_none()
            if row is not None and row.status in {"reserved", "unknown"}:
                row.status = "unknown" if uncertain else "released"
                row.settled_at = None if uncertain else _now()
    await retry_deadlocks(transaction)


async def settle(*, event_id: str, **usage) -> Decimal:
    """Hold and ledger change in one transaction; retries never debit twice."""
    async def transaction():
        async with credit._require_db()() as session, session.begin():
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
            # The wallet precedes the hold, as it does during reserve. No network I/O under locks.
            await session.execute(select(UserWallet).where(UserWallet.user_id == usage["user_id"]).with_for_update())
            row = (await session.execute(select(StatelessCallReservation).where(
                StatelessCallReservation.event_id == event_id).with_for_update())).scalar_one()
            if (row.user_id != usage["user_id"] or row.project_id != usage["project_id"]
                    or row.api_key_id != usage.get("api_key_id")):
                raise ValueError("stateless reservation owner mismatch")
            if row.status == "settled":
                existing = (await session.execute(select(ChatUsageLog).where(
                    ChatUsageLog.event_id == event_id))).scalar_one()
                return existing.credited_cost
            if row.status not in {"reserved", "unknown"}:
                raise ValueError("stateless reservation is not settleable")
            actual = await credit.apply_usage_in_transaction(session, event_id=event_id, **usage)
            row.actual_credits = actual
            row.status = "settled"
            row.settled_at = _now()
            return actual
    return await retry_deadlocks(transaction)
