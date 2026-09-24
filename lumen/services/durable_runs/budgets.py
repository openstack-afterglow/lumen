"""Project agent quotas and per-run reservations with a fixed global lock order.

Lock order (never acquire a later class while holding an earlier one out of sequence):
project quota -> conversation/temp thread -> root run -> ancestor runs by depth ->
children by creation order/UUID -> reservation/delegation/approval rows -> resources.
All functions here take an open session and a caller-owned transaction; nothing here
performs network I/O, so a deadlock retry is safe for pure DB callers only.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from lumen.models.chat_infrastructure import ChatAgentReservation, ChatProjectAgentQuota
from lumen.models.chat_runs import ChatModelCallReservation, ChatRun, ChatRunSegment

from .errors import DurableRunConflict, DurableRunError, DurableRunInputError

logger = logging.getLogger(__name__)

_LOCK_ORDER = ("quota", "conversation", "root", "ancestor", "child", "ledger", "resource")
_LOCK_CONFLICT_CODES = frozenset({1020, 1205, 1213})  # concurrent record change, lock timeout, deadlock

_KIND_TO_CAP = {
    "credit": ("max_credit_reservation", "credits_reserved"),
    "sandbox_seconds": ("max_sandbox_seconds", "sandbox_seconds_reserved"),
    "child_slot": ("max_active_children", "active_children"),
    "sandbox_slot": ("max_active_sandboxes", "active_sandboxes"),
}


class BudgetExceeded(DurableRunInputError):
    """The requested reservation does not fit the project cap or the root ceiling."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class LockOrder:
    """In-transaction guard: records the highest lock class taken and rejects regressions."""

    def __init__(self) -> None:
        self._position = -1

    def take(self, lock_class: str) -> None:
        index = _LOCK_ORDER.index(lock_class)
        if index < self._position:
            raise DurableRunError(f"lock order violation: {lock_class} after {_LOCK_ORDER[self._position]}")
        self._position = index


def _is_retryable_lock_conflict(exc: BaseException) -> bool:
    args = getattr(getattr(exc, "orig", None), "args", None) or getattr(exc, "args", None)
    return bool(args and isinstance(args[0], int) and args[0] in _LOCK_CONFLICT_CODES)


async def retry_deadlocks[T](operation: Callable[[], Awaitable[T]], *, attempts: int = 3) -> T:
    """Retry rollback-safe pure DB transactions at most three times; never wrap network I/O."""
    if not 1 <= attempts <= 3:
        raise ValueError("lock conflict retries are bounded to three attempts")
    for attempt in range(1, attempts + 1):
        try:
            return await operation()
        except OperationalError as exc:
            if not _is_retryable_lock_conflict(exc) or attempt == attempts:
                raise
            logger.info("retrying conflicting durable transaction attempt=%d", attempt)
            await asyncio.sleep(0.02 * attempt)
    raise AssertionError("unreachable")


async def lock_project_quota(session: AsyncSession, project_id: str, *, order: LockOrder) -> ChatProjectAgentQuota:
    """First lock class. Create missing rows from configured finite defaults only."""
    order.take("quota")
    row = (
        await session.execute(
            select(ChatProjectAgentQuota).where(ChatProjectAgentQuota.project_id == project_id).with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        from lumen.config import get_settings

        defaults = get_settings().runtime_config.project_quota_defaults
        values = defaults.model_dump()
        values["max_credit_reservation"] = Decimal(values["max_credit_reservation"])
        row = ChatProjectAgentQuota(project_id=project_id, **values)
        session.add(row)
        try:
            await session.flush()
        except IntegrityError:
            await session.rollback()
            raise DurableRunConflict("project quota row was created concurrently; retry") from None
        row = (
            await session.execute(
                select(ChatProjectAgentQuota).where(ChatProjectAgentQuota.project_id == project_id).with_for_update()
            )
        ).scalar_one()
    return row

async def settle_root_sandbox(session: AsyncSession, *, quota: ChatProjectAgentQuota, root: ChatRun, order: LockOrder) -> None:
    """Release the code root's slot and settle only time since its sandbox became ready.

    Call after locking quota and root, before locking any resource for deletion.
    Reservation status makes repeated terminal attempts harmless.
    """
    from lumen.models.chat_infrastructure import ChatRuntimeResource

    ready_at = None
    if root.assigned_resource_id is not None:
        ready_at = (
            await session.execute(
                select(ChatRuntimeResource.ready_at).where(ChatRuntimeResource.id == root.assigned_resource_id)
            )
        ).scalar_one_or_none()
    if ready_at is not None and ready_at.tzinfo is None:
        ready_at = ready_at.replace(tzinfo=UTC)
    used = max(0, int((datetime.now(UTC) - ready_at).total_seconds())) if ready_at else 0
    await release(session, quota=quota, root=root, run_id=root.id, kind="sandbox_slot", order=order)
    await settle(session, quota=quota, root=root, run_id=root.id, kind="sandbox_seconds", actual=used, order=order)

async def close_root_budget(session: AsyncSession, *, quota: ChatProjectAgentQuota, root: ChatRun, order: LockOrder) -> bool:
    """End project holds for the root without discarding persistent spend history.

    Running descendants keep their holds until they finish after this barrier.
    """
    if root.reservation_released_at is not None:
        return False
    order.take("ledger")
    rows = list((await session.execute(
        select(ChatAgentReservation)
        .where(ChatAgentReservation.root_run_id == root.id, ChatAgentReservation.status == "settled")
        .with_for_update()
    )).scalars())
    now = datetime.now(UTC)
    for row in rows:
        if row.kind in {"credit", "sandbox_seconds"} and row.released_at is None:
            spent = Decimal(str(row.settled_amount))
            held = min(spent, Decimal(str(row.amount))) if row.kind == "sandbox_seconds" else spent
            _release_counters(quota, root, row.kind, held)
            row.released_at = now
    quota.credits_reserved = max(
        Decimal("0"), Decimal(str(quota.credits_reserved)) - Decimal(str(root.reserved_credits))
    )
    root.reservation_released_at = now
    return True


async def lock_run(session: AsyncSession, run_id: str, *, order: LockOrder, lock_class: str) -> ChatRun:
    order.take(lock_class)
    run = (await session.execute(select(ChatRun).where(ChatRun.id == run_id).with_for_update())).scalar_one_or_none()
    if run is None:
        raise DurableRunError("chat run was not found")
    return run


def _cap_and_counter(quota: ChatProjectAgentQuota, kind: str) -> tuple[Decimal, Decimal, str]:
    cap_field, counter_field = _KIND_TO_CAP[kind]
    return Decimal(str(getattr(quota, cap_field))), Decimal(str(getattr(quota, counter_field))), counter_field


def _positive(amount: Decimal | int, kind: str) -> Decimal:
    value = Decimal(str(amount))
    if not value.is_finite() or value <= 0:
        raise BudgetExceeded("child_budget_exhausted", f"{kind} reservation must be positive")
    return value


async def reserve(
    session: AsyncSession,
    *,
    quota: ChatProjectAgentQuota,
    root: ChatRun,
    run_id: str,
    kind: str,
    amount: Decimal | int,
    order: LockOrder,
) -> ChatAgentReservation:
    """Reserve against the project cap and the root ceiling; one row per (run, kind)."""
    if kind not in _KIND_TO_CAP:
        raise DurableRunInputError("unknown reservation kind")
    value = _positive(amount, kind)
    cap, used, counter_field = _cap_and_counter(quota, kind)
    if cap <= 0:
        raise BudgetExceeded("delegation_disabled", f"project {kind} cap is zero")
    if used + value > cap:
        raise BudgetExceeded("child_budget_exhausted", f"project {kind} cap exceeded")
    if kind == "credit":
        ceiling = root.credit_ceiling if root.credit_ceiling is not None else root.descendant_credit_ceiling
        if ceiling is None:
            raise BudgetExceeded("child_budget_exhausted", "root run has no explicit credit ceiling")
        if (
            Decimal(str(root.descendant_credits_reserved)) + Decimal(str(root.reserved_credits)) + value
            > Decimal(str(ceiling))
        ):
            raise BudgetExceeded("child_budget_exhausted", "root credit ceiling exceeded")
        root.descendant_credits_reserved = Decimal(str(root.descendant_credits_reserved)) + value
    if kind == "sandbox_seconds":
        if root.sandbox_seconds_ceiling is None:
            raise BudgetExceeded("child_budget_exhausted", "root run has no explicit sandbox ceiling")
        if int(root.sandbox_seconds_reserved) + int(value) > int(root.sandbox_seconds_ceiling):
            raise BudgetExceeded("child_budget_exhausted", "root sandbox ceiling exceeded")
        root.sandbox_seconds_reserved = int(root.sandbox_seconds_reserved) + int(value)
    order.take("ledger")
    existing = (
        await session.execute(
            select(ChatAgentReservation)
            .where(ChatAgentReservation.run_id == run_id, ChatAgentReservation.kind == kind)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise DurableRunConflict(f"{kind} reservation already exists for run {run_id}")
    row = ChatAgentReservation(
        id=str(uuid4()),
        project_id=quota.project_id,
        root_run_id=root.id,
        run_id=run_id,
        kind=kind,
        amount=value,
        status="reserved",
    )
    session.add(row)
    setattr(quota, counter_field, type(getattr(quota, counter_field))(used + value))
    return row


async def _locked_reservation(session: AsyncSession, run_id: str, kind: str, order: LockOrder) -> ChatAgentReservation | None:
    order.take("ledger")
    return (
        await session.execute(
            select(ChatAgentReservation)
            .where(ChatAgentReservation.run_id == run_id, ChatAgentReservation.kind == kind)
            .with_for_update()
        )
    ).scalar_one_or_none()


async def settle(
    session: AsyncSession,
    *,
    quota: ChatProjectAgentQuota,
    root: ChatRun,
    run_id: str,
    kind: str,
    actual: Decimal | int,
    order: LockOrder,
) -> bool:
    """Record actual usage once and release only the unspent remainder; replay is a no-op."""
    row = await _locked_reservation(session, run_id, kind, order)
    if row is None:
        raise DurableRunError(f"{kind} reservation is missing for run {run_id}")
    if row.status != "reserved":
        return False
    spent = Decimal(str(actual))
    if not spent.is_finite() or spent < 0:
        raise DurableRunInputError("settled usage must be a non-negative amount")
    reserved = Decimal(str(row.amount))
    # Incurred credit overage counts against other admissions while the root is live;
    # the terminal barrier drops the project hold without erasing the spent ledger.
    unspent = max(Decimal("0"), reserved - spent)
    _release_counters(quota, root, kind, unspent)
    if kind == "credit" and spent > reserved:
        overrun = spent - reserved
        quota.credits_reserved = Decimal(str(quota.credits_reserved)) + overrun
        root.descendant_credits_reserved = Decimal(str(root.descendant_credits_reserved)) + overrun
    row.settled_amount = min(spent, reserved) if spent <= reserved else spent
    row.status = "settled"
    row.settled_at = datetime.now(UTC)
    if root.reservation_released_at is not None and kind in {"credit", "sandbox_seconds"}:
        # Late children release their own spent hold; sandbox overage was never reserved.
        held = min(spent, reserved) if kind == "sandbox_seconds" else spent
        _release_counters(quota, root, kind, held)
        row.released_at = row.settled_at
    return True


async def release(
    session: AsyncSession,
    *,
    quota: ChatProjectAgentQuota,
    root: ChatRun,
    run_id: str,
    kind: str,
    order: LockOrder,
) -> bool:
    """Release the whole unspent reservation once (cancel/failure before use)."""
    row = await _locked_reservation(session, run_id, kind, order)
    if row is None or row.status != "reserved":
        return False
    _release_counters(quota, root, kind, Decimal(str(row.amount)))
    row.status = "released"
    row.released_at = datetime.now(UTC)
    return True


def _release_counters(quota: ChatProjectAgentQuota, root: ChatRun, kind: str, amount: Decimal) -> None:
    _cap, used, counter_field = _cap_and_counter(quota, kind)
    remaining = max(Decimal("0"), used - amount)
    setattr(quota, counter_field, type(getattr(quota, counter_field))(remaining))
    if kind == "credit":
        root.descendant_credits_reserved = max(Decimal("0"), Decimal(str(root.descendant_credits_reserved)) - amount)
    if kind == "sandbox_seconds":
        root.sandbox_seconds_reserved = max(0, int(root.sandbox_seconds_reserved) - int(amount))


def _credits(amount: Decimal | int, *, label: str) -> Decimal:
    value = Decimal(str(amount))
    if not value.is_finite() or value < 0 or value >= Decimal("10000000000"):
        raise DurableRunInputError(f"{label} must be non-negative DECIMAL(18,8) credits")
    if value != value.quantize(Decimal("0.00000001")):
        raise DurableRunInputError(f"{label} must have at most eight decimal places")
    return value


def _validate_call_scope(quota: ChatProjectAgentQuota, root: ChatRun, run: ChatRun, segment: ChatRunSegment) -> None:
    if (
        root.project_id != quota.project_id or root.parent_run_id is not None
        or run.project_id != quota.project_id
        or (run is not root and (run.root_run_id != root.id or run.parent_run_id is None))
        or segment.run_id != run.id or not segment.segment_id or len(segment.segment_id) > 190
    ):
        raise DurableRunInputError("model call budget run ancestry or segment mismatch")


async def _child_credit_allocation(
    session: AsyncSession, *, root: ChatRun, run: ChatRun, order: LockOrder
) -> ChatAgentReservation:
    row = await _locked_reservation(session, run.id, "credit", order)
    if row is None or row.root_run_id != root.id or row.project_id != root.project_id or row.status != "reserved":
        raise DurableRunConflict("child credit allocation is not active")
    return row


async def reserve_call_credit(
    session: AsyncSession, *, run: ChatRun, root: ChatRun, quota: ChatProjectAgentQuota,
    segment: ChatRunSegment, amount: Decimal,
) -> bool:
    """Reserve before I/O under the caller's quota -> root -> run -> segment locks."""
    _validate_call_scope(quota, root, run, segment)
    amount = _credits(amount, label="call credit bound")
    if segment.status != "prepared":
        raise DurableRunConflict("call segment has already started")
    existing = (
        await session.execute(select(ChatModelCallReservation).where(
            ChatModelCallReservation.run_id == run.id,
            ChatModelCallReservation.segment_id == segment.segment_id,
        ).with_for_update())
    ).scalar_one_or_none()
    if existing is not None:
        raise DurableRunConflict("call credit reservation already exists")
    if run is root:
        if amount:
            cap, used, _counter = _cap_and_counter(quota, "credit")
            if cap <= 0:
                raise BudgetExceeded("delegation_disabled", "project credit cap is zero")
            if used + amount > cap:
                raise BudgetExceeded("child_budget_exhausted", "project credit cap exceeded")
            if root.credit_ceiling is None or (
                Decimal(str(root.reserved_credits)) + Decimal(str(root.descendant_credits_reserved)) + amount
                > Decimal(str(root.credit_ceiling))
            ):
                raise BudgetExceeded("child_budget_exhausted", "root credit ceiling exceeded")
            quota.credits_reserved = used + amount
        root.reserved_credits = Decimal(str(root.reserved_credits)) + amount
    else:
        allocation = await _child_credit_allocation(session, root=root, run=run, order=LockOrder())
        child_total = Decimal(str(run.reserved_credits)) + amount
        if amount and (
            run.credit_ceiling is None
            or child_total > Decimal(str(run.credit_ceiling))
            or child_total > Decimal(str(allocation.amount))
        ):
            raise BudgetExceeded("child_budget_exhausted", "child credit ceiling exceeded")
        run.reserved_credits = child_total

    row = ChatModelCallReservation(run_id=run.id, segment_id=segment.segment_id, bound_credits=amount, status="reserved")
    session.add(row)
    return True


async def settle_call_credit(
    session: AsyncSession, *, run: ChatRun, root: ChatRun, quota: ChatProjectAgentQuota,
    segment: ChatRunSegment, actual: Decimal | None,
) -> bool:
    """Settle observed use exactly once; an indeterminate outcome retains its hold."""
    _validate_call_scope(quota, root, run, segment)
    spent = None if actual is None else _credits(actual, label="call credit actual")
    row = (
        await session.execute(
            select(ChatModelCallReservation)
            .where(ChatModelCallReservation.run_id == run.id, ChatModelCallReservation.segment_id == segment.segment_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        raise DurableRunError("model call reservation is missing")
    if row.status != "reserved":
        return False
    if run is not root:
        await _child_credit_allocation(session, root=root, run=run, order=LockOrder())
    if spent is not None:
        delta = spent - Decimal(str(row.bound_credits))
        run.reserved_credits = Decimal(str(run.reserved_credits)) + delta
        if run is root:
            quota.credits_reserved = Decimal(str(quota.credits_reserved)) + delta
        row.actual_credits = spent
    row.status = "unknown" if spent is None else "settled"
    row.settled_at = datetime.now(UTC)
    return True
