"""ORM models for the durable MySQL chat run journal (migration 039)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import BIGINT, CHAR, INT, JSON, VARCHAR, DateTime, ForeignKey, Index, Numeric
from sqlalchemy.dialects.mysql import DATETIME as MYSQL_DATETIME
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.orm import Mapped, mapped_column

from lumen.db import Base


def _now() -> datetime:
    return datetime.now(UTC)


class ChatRun(Base):
    __tablename__ = "chat_runs"

    id: Mapped[str] = mapped_column(CHAR(36), primary_key=True)
    run_scope: Mapped[str] = mapped_column(VARCHAR(20), nullable=False)
    conversation_id: Mapped[str | None] = mapped_column(
        CHAR(36), ForeignKey("chat_conversations.id", ondelete="SET NULL")
    )
    temp_thread_id: Mapped[str | None] = mapped_column(CHAR(36))
    user_message_id: Mapped[int | None] = mapped_column(BIGINT, ForeignKey("chat_messages.id", ondelete="SET NULL"))
    assistant_message_id: Mapped[int | None] = mapped_column(
        BIGINT, ForeignKey("chat_messages.id", ondelete="SET NULL")
    )
    project_id: Mapped[str] = mapped_column(VARCHAR(64), nullable=False)
    user_id: Mapped[str] = mapped_column(VARCHAR(64), nullable=False)
    model_name: Mapped[str] = mapped_column(VARCHAR(190), nullable=False)
    source: Mapped[str] = mapped_column(VARCHAR(10), nullable=False, default="web")
    # No FK: API-key revocation must not delete accepted run provenance.
    api_key_id: Mapped[int | None] = mapped_column(BIGINT)
    agent_id: Mapped[int | None] = mapped_column(BIGINT)
    capability_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    pricing_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    request_payload: Mapped[str | None] = mapped_column(MEDIUMTEXT)
    client_request_id: Mapped[str] = mapped_column(CHAR(36), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(VARCHAR(128), nullable=False)
    fingerprint_version: Mapped[int] = mapped_column(INT, nullable=False)
    execution_protocol_version: Mapped[int] = mapped_column(INT, nullable=False, default=1)
    execution_mode: Mapped[str] = mapped_column(VARCHAR(10), nullable=False, default="chat")
    code_workspace_id: Mapped[str | None] = mapped_column(
        CHAR(36), ForeignKey("chat_code_workspaces.id", ondelete="SET NULL")
    )
    parent_run_id: Mapped[str | None] = mapped_column(CHAR(36), ForeignKey("chat_runs.id", ondelete="RESTRICT"))
    root_run_id: Mapped[str | None] = mapped_column(CHAR(36), ForeignKey("chat_runs.id", ondelete="RESTRICT"))
    delegation_call_id: Mapped[str | None] = mapped_column(VARCHAR(190))
    depth: Mapped[int] = mapped_column(INT, nullable=False, default=0)
    policy_snapshot: Mapped[dict | None] = mapped_column(JSON)
    reservation_snapshot: Mapped[dict | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(VARCHAR(30), nullable=False, default="queued")
    last_seq: Mapped[int] = mapped_column(BIGINT, nullable=False, default=0)
    current_ordinal: Mapped[int] = mapped_column(INT, nullable=False, default=0)
    lease_owner: Mapped[str | None] = mapped_column(VARCHAR(190))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finalizer_lease_owner: Mapped[str | None] = mapped_column(VARCHAR(190))
    finalizer_lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reserved_credits: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False, default=Decimal("0"))
    reservation_released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    usage_reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    descendant_credit_ceiling: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    descendant_credits_reserved: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False, default=Decimal("0"))
    sandbox_seconds_ceiling: Mapped[int | None] = mapped_column(INT)
    sandbox_seconds_reserved: Mapped[int] = mapped_column(INT, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)

    __table_args__ = (
        Index("idx_chat_runs_owner_status", "project_id", "user_id", "status"),
        Index("idx_chat_runs_conversation_status", "conversation_id", "status"),
        Index("idx_chat_runs_temp_status", "temp_thread_id", "status"),
        Index("uq_chat_runs_idempotency", "project_id", "user_id", "client_request_id", unique=True),
        Index("idx_chat_runs_root_status", "root_run_id", "status"),
        Index("idx_chat_runs_parent_status", "parent_run_id", "status"),
        Index("uq_chat_runs_parent_delegation", "parent_run_id", "delegation_call_id", unique=True),
    )


class ChatRunProvider(Base):
    __tablename__ = "chat_run_providers"

    run_id: Mapped[str] = mapped_column(
        CHAR(36),
        ForeignKey("chat_runs.id", ondelete="CASCADE"),
        primary_key=True,
    )
    purpose: Mapped[str] = mapped_column(VARCHAR(30), primary_key=True)
    provider_id: Mapped[int | None] = mapped_column(BIGINT, ForeignKey("llm_providers.id", ondelete="SET NULL"))
    model_id: Mapped[int | None] = mapped_column(BIGINT, ForeignKey("llm_models.id", ondelete="SET NULL"))
    provider_label: Mapped[str] = mapped_column(VARCHAR(190), nullable=False)
    model_label: Mapped[str] = mapped_column(VARCHAR(190), nullable=False)
    config_version_hash: Mapped[str] = mapped_column(VARCHAR(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)


class ChatToolApproval(Base):
    __tablename__ = "chat_tool_approvals"

    run_id: Mapped[str] = mapped_column(
        CHAR(36),
        ForeignKey("chat_runs.id", ondelete="CASCADE"),
        primary_key=True,
    )
    call_id: Mapped[str] = mapped_column(VARCHAR(190), primary_key=True)
    tool_name: Mapped[str] = mapped_column(MEDIUMTEXT, nullable=False)
    arguments: Mapped[str] = mapped_column(MEDIUMTEXT, nullable=False)
    arguments_ciphertext: Mapped[str | None] = mapped_column(MEDIUMTEXT)
    dispatch_hmac: Mapped[str | None] = mapped_column(CHAR(64))
    preview_fingerprint: Mapped[str | None] = mapped_column(CHAR(64))
    decision_hmac: Mapped[str | None] = mapped_column(CHAR(64))

    source: Mapped[str | None] = mapped_column(VARCHAR(30))
    effect: Mapped[str | None] = mapped_column(VARCHAR(30))
    tool_definition_hash: Mapped[str | None] = mapped_column(CHAR(64))
    config_fingerprint: Mapped[str | None] = mapped_column(CHAR(64))
    destination_origin: Mapped[str | None] = mapped_column(VARCHAR(255))
    expected_state_revision: Mapped[int | None] = mapped_column(BIGINT)
    writer_fence: Mapped[int | None] = mapped_column(BIGINT)
    status: Mapped[str] = mapped_column(VARCHAR(20), nullable=False, default="pending")
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True).with_variant(MYSQL_DATETIME(fsp=6), "mysql"),
        nullable=False,
        default=_now,
    )
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True).with_variant(MYSQL_DATETIME(fsp=6), "mysql")
    )
    decided_by_user_id: Mapped[str | None] = mapped_column(VARCHAR(64))
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True).with_variant(MYSQL_DATETIME(fsp=6), "mysql"),
        nullable=False,
    )

    __table_args__ = (Index("idx_chat_tool_approvals_pending_expiry", "status", "expires_at"),)


class ChatRunEventRow(Base):
    __tablename__ = "chat_run_events"

    run_id: Mapped[str] = mapped_column(CHAR(36), ForeignKey("chat_runs.id", ondelete="CASCADE"), primary_key=True)
    seq: Mapped[int] = mapped_column(BIGINT, primary_key=True)
    event_type: Mapped[str] = mapped_column(VARCHAR(40), nullable=False)
    payload: Mapped[str] = mapped_column(MEDIUMTEXT, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)


class ChatRunTurn(Base):
    """Idempotency ledger for one assistant turn within a durable run."""

    __tablename__ = "chat_run_turns"

    run_id: Mapped[str] = mapped_column(CHAR(36), ForeignKey("chat_runs.id", ondelete="CASCADE"), primary_key=True)
    ordinal: Mapped[int] = mapped_column(INT, primary_key=True)
    assistant_message_id: Mapped[int | None] = mapped_column(
        BIGINT, ForeignKey("chat_messages.id", ondelete="SET NULL")
    )
    message_event_seq: Mapped[int | None] = mapped_column(BIGINT)
    completion_event_seq: Mapped[int | None] = mapped_column(BIGINT)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)

    __table_args__ = (Index("uq_chat_run_turn_message_event", "run_id", "message_event_seq", unique=True),)


class ChatRunSegment(Base):
    """Durable provider/tool boundary; started segments are never replayed blindly."""

    __tablename__ = "chat_run_segments"

    run_id: Mapped[str] = mapped_column(CHAR(36), ForeignKey("chat_runs.id", ondelete="CASCADE"), primary_key=True)
    segment_id: Mapped[str] = mapped_column(VARCHAR(190), primary_key=True)
    ordinal: Mapped[int] = mapped_column(INT, nullable=False)
    endpoint: Mapped[str] = mapped_column(VARCHAR(40), nullable=False)
    kind: Mapped[str] = mapped_column(VARCHAR(40), nullable=False, default="provider")
    boundary_key: Mapped[str | None] = mapped_column(VARCHAR(255))
    turn_ordinal: Mapped[int | None] = mapped_column(INT)
    call_id: Mapped[str | None] = mapped_column(VARCHAR(190))
    status: Mapped[str] = mapped_column(VARCHAR(30), nullable=False, default="prepared")
    provider_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result_payload: Mapped[str | None] = mapped_column(MEDIUMTEXT)
    usage_payload: Mapped[str | None] = mapped_column(MEDIUMTEXT)
    started_event_seq: Mapped[int | None] = mapped_column(BIGINT)
    completed_event_seq: Mapped[int | None] = mapped_column(BIGINT)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("uq_chat_run_segments_ordinal", "run_id", "ordinal", unique=True),
        Index("uq_chat_run_segments_boundary", "run_id", "boundary_key", unique=True),
    )


class ChatTempThread(Base):
    __tablename__ = "chat_temp_threads"

    id: Mapped[str] = mapped_column(CHAR(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(VARCHAR(64), nullable=False)
    user_id: Mapped[str] = mapped_column(VARCHAR(64), nullable=False)
    history: Mapped[str] = mapped_column(MEDIUMTEXT, nullable=False)
    active_run_id: Mapped[str | None] = mapped_column(CHAR(36))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)

    __table_args__ = (Index("idx_chat_temp_threads_owner_expiry", "project_id", "user_id", "expires_at"),)


class ChatSchedulerLease(Base):
    __tablename__ = "chat_scheduler_leases"

    scheduler_name: Mapped[str] = mapped_column(VARCHAR(100), primary_key=True)
    owner: Mapped[str] = mapped_column(VARCHAR(190), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)
