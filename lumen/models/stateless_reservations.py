"""Content-free credit holds for bounded stateless provider calls."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import BIGINT, CHAR, VARCHAR, CheckConstraint, Index, Numeric
from sqlalchemy.dialects.mysql import DATETIME
from sqlalchemy.orm import Mapped, mapped_column

from lumen.db import Base


class StatelessCallReservation(Base):
    __tablename__ = "chat_stateless_call_reservations"

    event_id: Mapped[str] = mapped_column(CHAR(36), primary_key=True)
    user_id: Mapped[str | None] = mapped_column(VARCHAR(64))
    project_id: Mapped[str | None] = mapped_column(VARCHAR(64))
    payer_user_id: Mapped[str | None] = mapped_column(VARCHAR(64))
    payer_domain_id: Mapped[str | None] = mapped_column(VARCHAR(64))
    api_key_id: Mapped[int | None] = mapped_column(BIGINT)
    bound_credits: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    actual_credits: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    status: Mapped[str] = mapped_column(VARCHAR(20), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)
    settled_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6))

    __table_args__ = (
        Index("idx_stateless_hold_owner", "user_id", "status"),
        Index("idx_stateless_hold_key", "api_key_id", "status"),
        Index("idx_stateless_hold_payer_domain", "payer_domain_id", "status"),
        CheckConstraint("bound_credits >= 0", name="ck_stateless_bound"),
        CheckConstraint("actual_credits IS NULL OR actual_credits >= 0", name="ck_stateless_actual"),
    )
