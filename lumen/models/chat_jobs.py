"""ORM models for durable chat jobs, derivations, and memory outbox (migration 041)."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import BIGINT, CHAR, INT, JSON, VARCHAR, DateTime, ForeignKey, Index, UniqueConstraint
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.orm import Mapped, mapped_column

from lumen.db import Base


def _now() -> datetime:
    return datetime.now(UTC)


class ChatJob(Base):
    __tablename__ = "chat_jobs"

    id: Mapped[str] = mapped_column(CHAR(36), primary_key=True)
    kind: Mapped[str] = mapped_column(VARCHAR(40), nullable=False)
    run_id: Mapped[str | None] = mapped_column(CHAR(36), ForeignKey("chat_runs.id", ondelete="SET NULL"))
    conversation_id: Mapped[str | None] = mapped_column(
        CHAR(36), ForeignKey("chat_conversations.id", ondelete="SET NULL")
    )
    memory_id: Mapped[int | None] = mapped_column(BIGINT)
    asset_id: Mapped[str | None] = mapped_column(CHAR(36), ForeignKey("chat_assets.id", ondelete="SET NULL"))
    payload: Mapped[str | None] = mapped_column(MEDIUMTEXT)
    progress: Mapped[dict | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(VARCHAR(20), nullable=False, default="queued")
    lease_owner: Mapped[str | None] = mapped_column(VARCHAR(190))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(INT, nullable=False, default=0)
    next_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    error_code: Mapped[str | None] = mapped_column(VARCHAR(100))
    idempotency_key: Mapped[str] = mapped_column(VARCHAR(190), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)

    __table_args__ = (Index("idx_chat_jobs_claim", "status", "next_at"),)


class ChatInputDerivation(Base):
    __tablename__ = "chat_input_derivations"

    id: Mapped[str] = mapped_column(CHAR(36), primary_key=True)
    message_id: Mapped[int | None] = mapped_column(BIGINT, ForeignKey("chat_messages.id", ondelete="CASCADE"))
    temp_thread_id: Mapped[str | None] = mapped_column(CHAR(36))
    turn_ordinal: Mapped[int | None] = mapped_column(INT)
    part_index: Mapped[int] = mapped_column(INT, nullable=False)
    asset_id: Mapped[str] = mapped_column(CHAR(36), ForeignKey("chat_assets.id", ondelete="RESTRICT"), nullable=False)
    asset_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    kind: Mapped[str] = mapped_column(VARCHAR(30), nullable=False)
    content: Mapped[str | None] = mapped_column(MEDIUMTEXT)
    producer_model: Mapped[str | None] = mapped_column(VARCHAR(190))
    producer_version: Mapped[str | None] = mapped_column(VARCHAR(190))
    usage_segment_id: Mapped[str | None] = mapped_column(VARCHAR(190))
    status: Mapped[str] = mapped_column(VARCHAR(20), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)

    __table_args__ = (
        UniqueConstraint(
            "message_id",
            "temp_thread_id",
            "turn_ordinal",
            "part_index",
            "asset_sha256",
            "kind",
            name="uq_chat_input_derivation",
        ),
    )


class ChatMemoryProvenance(Base):
    __tablename__ = "chat_memory_provenance"

    memory_id: Mapped[int] = mapped_column(
        BIGINT,
        ForeignKey("chat_memories.id", ondelete="CASCADE"),
        primary_key=True,
    )
    message_id: Mapped[int] = mapped_column(
        BIGINT,
        ForeignKey("chat_messages.id", ondelete="CASCADE"),
        primary_key=True,
    )
    source_type: Mapped[str] = mapped_column(VARCHAR(30), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        CHAR(36), ForeignKey("chat_conversations.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)


class ChatMemoryOutbox(Base):
    __tablename__ = "chat_memory_outbox"

    change_seq: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    event_key: Mapped[str] = mapped_column(VARCHAR(190), nullable=False, unique=True)
    memory_id: Mapped[int] = mapped_column(BIGINT, ForeignKey("chat_memories.id", ondelete="RESTRICT"), nullable=False)
    mutation: Mapped[str] = mapped_column(VARCHAR(20), nullable=False)
    content_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    required_generations: Mapped[list] = mapped_column(JSON, nullable=False)
    applied_generations: Mapped[list | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(VARCHAR(20), nullable=False, default="queued")
    lease_owner: Mapped[str | None] = mapped_column(VARCHAR(190))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(INT, nullable=False, default=0)
    error_code: Mapped[str | None] = mapped_column(VARCHAR(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)

    __table_args__ = (Index("idx_chat_memory_outbox_claim", "status", "change_seq"),)
