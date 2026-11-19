"""Payer identity and domain credit persistence."""
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Literal

from sqlalchemy import BIGINT, BOOLEAN, TEXT, VARCHAR, CheckConstraint, Date, Index, Numeric, UniqueConstraint
from sqlalchemy.dialects.mysql import DATETIME
from sqlalchemy.orm import Mapped, mapped_column

from lumen.db import Base


@dataclass(frozen=True)
class Payer:
    scope: Literal["user", "domain"]
    user_id: str | None
    domain_id: str | None

    def __post_init__(self) -> None:
        if not ((self.scope == "user" and self.user_id and self.domain_id is None)
                or (self.scope == "domain" and self.domain_id and self.user_id is None)):
            raise ValueError("invalid billing payer")

    def columns(self) -> dict[str, str | None]:
        return {"payer_user_id": self.user_id, "payer_domain_id": self.domain_id}


def utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class DomainCreditPool(Base):
    __tablename__ = "domain_credit_pools"

    domain_id: Mapped[str] = mapped_column(VARCHAR(64), primary_key=True)
    balance_credits: Mapped[Decimal] = mapped_column(Numeric(18, 8), server_default="0")
    reserved_credits: Mapped[Decimal] = mapped_column(Numeric(18, 8), server_default="0")
    monthly_used_credits: Mapped[Decimal] = mapped_column(Numeric(18, 8), server_default="0")
    period_start: Mapped[date] = mapped_column(Date)
    manager_monthly_limit_credits: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    admin_ceiling_credits: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    is_active: Mapped[bool] = mapped_column(BOOLEAN, server_default="1")
    updated_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), default=utc_now, onupdate=utc_now)

    __table_args__ = (
        CheckConstraint("balance_credits >= 0", name="ck_domain_pool_balance"),
        CheckConstraint("reserved_credits >= 0", name="ck_domain_pool_reserved"),
        CheckConstraint("monthly_used_credits >= 0", name="ck_domain_pool_month"),
        CheckConstraint("manager_monthly_limit_credits >= 0", name="ck_domain_pool_limit"),
        CheckConstraint("admin_ceiling_credits >= 0", name="ck_domain_pool_ceiling"),
    )


class DomainCreditLedger(Base):
    __tablename__ = "domain_credit_ledger"

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    domain_id: Mapped[str] = mapped_column(VARCHAR(64))
    kind: Mapped[str] = mapped_column(VARCHAR(20))
    amount_credits: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    balance_after: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    actor_user_id: Mapped[str | None] = mapped_column(VARCHAR(64))
    reference: Mapped[str] = mapped_column(VARCHAR(190))
    note: Mapped[str | None] = mapped_column(TEXT)
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), default=utc_now)

    __table_args__ = (
        CheckConstraint("kind IN ('topup','adjustment','charge','refund')", name="ck_domain_ledger_kind"),
        UniqueConstraint("kind", "reference", name="uq_domain_ledger_reference"),
        Index("idx_domain_ledger_domain_id", "domain_id", "id"),
        Index("idx_domain_ledger_actor_charge", "actor_user_id", "kind", "created_at"),
    )
