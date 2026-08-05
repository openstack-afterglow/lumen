"""ORM models for owned chat assets (migration 040)."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import BIGINT, CHAR, INT, JSON, VARCHAR, DateTime, ForeignKey, Index, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from lumen.db import Base


def _now() -> datetime:
    return datetime.now(UTC)


class ChatAsset(Base):
    __tablename__ = "chat_assets"

    id: Mapped[str] = mapped_column(CHAR(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(VARCHAR(64), nullable=False)
    user_id: Mapped[str] = mapped_column(VARCHAR(64), nullable=False)
    object_key: Mapped[str] = mapped_column(VARCHAR(255), nullable=False, unique=True)
    original_name: Mapped[str] = mapped_column(VARCHAR(255), nullable=False)
    mime_type: Mapped[str] = mapped_column(VARCHAR(127), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BIGINT, nullable=False)
    sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    status: Mapped[str] = mapped_column(VARCHAR(20), nullable=False, default="uploading")
    media_metadata: Mapped[dict | None] = mapped_column(JSON)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleting_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)

    __table_args__ = (
        Index("idx_chat_assets_owner_status", "project_id", "user_id", "status"),
        Index("idx_chat_assets_expiry", "expires_at"),
    )


class ChatMessageAsset(Base):
    __tablename__ = "chat_message_assets"

    message_id: Mapped[int] = mapped_column(
        BIGINT,
        ForeignKey("chat_messages.id", ondelete="CASCADE"),
        primary_key=True,
    )
    asset_id: Mapped[str] = mapped_column(
        CHAR(36),
        ForeignKey("chat_assets.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    part_index: Mapped[int] = mapped_column(INT, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)

    __table_args__ = (UniqueConstraint("message_id", "part_index", name="uq_chat_message_assets_part"),)


class ChatRunAsset(Base):
    __tablename__ = "chat_run_assets"

    run_id: Mapped[str] = mapped_column(
        CHAR(36),
        ForeignKey("chat_runs.id", ondelete="CASCADE"),
        primary_key=True,
    )
    asset_id: Mapped[str] = mapped_column(
        CHAR(36),
        ForeignKey("chat_assets.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    purpose: Mapped[str] = mapped_column(VARCHAR(30), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
