"""Persisted native/OpenAI batch, file, item and project dispatch ledgers (migration 024).

Request and result bodies are serialized and encrypted with the existing chat-content
cipher before entering MEDIUMTEXT columns; plaintext bodies never belong in this
ledger. JSON columns contain only metadata and frozen capability/pricing/scope
snapshots. Error fields and validation errors must be sanitized, never raw bodies.
API-key IDs are provenance, not foreign keys. File/asset/run references retain
accepted input and result provenance with RESTRICT. Coordinators lock project queue,
batch, item, then run; project queues do not reuse agent quota or credit ledgers.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import BIGINT, CHAR, INT, JSON, VARCHAR, CheckConstraint, ForeignKey, Index, text
from sqlalchemy.dialects.mysql import DATETIME, MEDIUMTEXT
from sqlalchemy.engine.default import DefaultExecutionContext
from sqlalchemy.orm import Mapped, mapped_column

from lumen.db import Base


def _now() -> datetime:
    return datetime.now(UTC)


def _batch_expires_at(context: DefaultExecutionContext) -> datetime:
    # The insert's created_at default is evaluated first, preserving the exact 24h window.
    return context.get_current_parameters()["created_at"] + timedelta(hours=24)


class ChatBatchFile(Base):
    """Private object lifecycle; deletion preserves the row and owner provenance."""

    __tablename__ = "chat_batch_files"

    id: Mapped[str] = mapped_column(CHAR(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(VARCHAR(64), nullable=False)
    user_id: Mapped[str] = mapped_column(VARCHAR(64), nullable=False)
    # No FK: accepted file provenance survives API-key revocation/removal.
    api_key_id: Mapped[int | None] = mapped_column(BIGINT)
    purpose: Mapped[str] = mapped_column(VARCHAR(20), nullable=False)
    filename: Mapped[str] = mapped_column(VARCHAR(255), nullable=False)
    mime_type: Mapped[str] = mapped_column(VARCHAR(127), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BIGINT, nullable=False, default=0, server_default="0")
    # Unknown during a streaming upload; populated before the object is processed.
    sha256: Mapped[str | None] = mapped_column(CHAR(64))
    bucket_name: Mapped[str] = mapped_column(VARCHAR(63), nullable=False)
    object_key: Mapped[str] = mapped_column(VARCHAR(255), nullable=False)
    state: Mapped[str] = mapped_column(VARCHAR(20), nullable=False, default="uploading", server_default="uploading")
    scan_result: Mapped[str | None] = mapped_column(VARCHAR(20))
    multipart_upload_id: Mapped[str | None] = mapped_column(VARCHAR(1024))
    upload_epoch: Mapped[int] = mapped_column(BIGINT, nullable=False, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DATETIME(fsp=6), nullable=False, default=_now, server_default=text("CURRENT_TIMESTAMP(6)")
    )
    expires_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6))

    __table_args__ = (
        Index("idx_chat_batch_files_owner_cursor", "project_id", "user_id", "created_at", "id"),
        Index("idx_chat_batch_files_expiry_state", "expires_at", "state"),
        Index("uq_chat_batch_files_object_key", "object_key", unique=True),
        CheckConstraint("purpose IN ('batch','batch_output')", name="ck_chat_batch_files_purpose"),
        CheckConstraint(
            "state IN ('uploading','processed','error','deleting','deleted')", name="ck_chat_batch_files_state"
        ),
        CheckConstraint("size_bytes >= 0 AND upload_epoch >= 0", name="ck_chat_batch_files_counters"),
        {"mysql_engine": "InnoDB"},
    )


class ChatBatch(Base):
    """Owner-scoped admission, validation cursor and fenced result publication."""

    __tablename__ = "chat_batches"

    id: Mapped[str] = mapped_column(CHAR(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(VARCHAR(64), nullable=False)
    user_id: Mapped[str] = mapped_column(VARCHAR(64), nullable=False)
    # No FK: accepted batch provenance survives API-key revocation/removal.
    api_key_id: Mapped[int | None] = mapped_column(BIGINT)
    contract: Mapped[str] = mapped_column(VARCHAR(10), nullable=False)
    endpoint: Mapped[str | None] = mapped_column(VARCHAR(64))
    status: Mapped[str] = mapped_column(VARCHAR(20), nullable=False, default="validating", server_default="validating")
    final_target_status: Mapped[str | None] = mapped_column(VARCHAR(20))
    # Declarative Base reserves the Python name metadata; the SQL column remains metadata.
    metadata_json: Mapped[dict] = mapped_column("metadata", JSON, nullable=False, default=dict, server_default=text("('{}')"))
    idempotency_key_hash: Mapped[str | None] = mapped_column(CHAR(64))
    request_fingerprint: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    input_file_id: Mapped[str | None] = mapped_column(
        CHAR(36), ForeignKey("chat_batch_files.id", name="fk_chat_batches_input_file", ondelete="RESTRICT")
    )
    output_file_id: Mapped[str | None] = mapped_column(
        CHAR(36), ForeignKey("chat_batch_files.id", name="fk_chat_batches_output_file", ondelete="RESTRICT")
    )
    error_file_id: Mapped[str | None] = mapped_column(
        CHAR(36), ForeignKey("chat_batch_files.id", name="fk_chat_batches_error_file", ondelete="RESTRICT")
    )
    # The supported expiry anchor is created_at; NULL selects the configured result TTL.
    output_expires_after_seconds: Mapped[int | None] = mapped_column(INT)
    request_total: Mapped[int] = mapped_column(INT, nullable=False, default=0, server_default="0")
    request_completed: Mapped[int] = mapped_column(INT, nullable=False, default=0, server_default="0")
    # Includes cancelled/expired/unknown for OpenAI projection; detail counters are subsets.
    request_failed: Mapped[int] = mapped_column(INT, nullable=False, default=0, server_default="0")
    request_pending: Mapped[int] = mapped_column(INT, nullable=False, default=0, server_default="0")
    request_queued: Mapped[int] = mapped_column(INT, nullable=False, default=0, server_default="0")
    request_running: Mapped[int] = mapped_column(INT, nullable=False, default=0, server_default="0")
    request_cancelled: Mapped[int] = mapped_column(INT, nullable=False, default=0, server_default="0")
    request_expired: Mapped[int] = mapped_column(INT, nullable=False, default=0, server_default="0")
    request_unknown: Mapped[int] = mapped_column(INT, nullable=False, default=0, server_default="0")
    validation_errors: Mapped[list] = mapped_column(JSON, nullable=False, default=list, server_default=text("('[]')"))
    validation_byte_cursor: Mapped[int] = mapped_column(BIGINT, nullable=False, default=0, server_default="0")
    validation_ordinal_cursor: Mapped[int] = mapped_column(INT, nullable=False, default=0, server_default="0")
    lease_owner: Mapped[str | None] = mapped_column(VARCHAR(190))
    lease_fence: Mapped[int] = mapped_column(BIGINT, nullable=False, default=0, server_default="0")
    lease_expires_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6))
    finalization_epoch: Mapped[int] = mapped_column(BIGINT, nullable=False, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DATETIME(fsp=6), nullable=False, default=_now, server_default=text("CURRENT_TIMESTAMP(6)")
    )
    expires_at: Mapped[datetime] = mapped_column(
        DATETIME(fsp=6), nullable=False, default=_batch_expires_at, server_default=text("(CURRENT_TIMESTAMP(6) + INTERVAL 24 HOUR)")
    )
    in_progress_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6))
    finalizing_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6))
    completed_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6))
    failed_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6))
    cancelling_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6))
    cancelled_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6))
    expired_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6))

    __table_args__ = (
        Index("uq_chat_batches_idempotency", "project_id", "user_id", "contract", "idempotency_key_hash", unique=True),
        Index("idx_chat_batches_owner_cursor", "project_id", "user_id", "created_at", "id"),
        Index("idx_chat_batches_status_deadline", "status", "expires_at"),
        Index("idx_chat_batches_input_file", "input_file_id"),
        Index("idx_chat_batches_output_file", "output_file_id"),
        Index("idx_chat_batches_error_file", "error_file_id"),
        CheckConstraint("contract IN ('native','openai')", name="ck_chat_batches_contract"),
        CheckConstraint("contract <> 'native' OR idempotency_key_hash IS NOT NULL", name="ck_chat_batches_native_idempotency"),
        CheckConstraint(
            "status IN ('validating','in_progress','finalizing','completed','failed','cancelling','cancelled','expired')",
            name="ck_chat_batches_status",
        ),
        CheckConstraint(
            "final_target_status IS NULL OR final_target_status IN ('completed','failed','cancelled','expired')",
            name="ck_chat_batches_final_target",
        ),
        CheckConstraint(
            "request_total >= 0 AND request_completed >= 0 AND request_failed >= 0 AND request_pending >= 0 "
            "AND request_queued >= 0 AND request_running >= 0 AND request_cancelled >= 0 "
            "AND request_expired >= 0 AND request_unknown >= 0",
            name="ck_chat_batches_request_counters",
        ),
        CheckConstraint(
            "validation_byte_cursor >= 0 AND validation_ordinal_cursor >= 0 AND lease_fence >= 0 AND finalization_epoch >= 0",
            name="ck_chat_batches_progress_counters",
        ),
        CheckConstraint(
            "output_expires_after_seconds IS NULL OR output_expires_after_seconds BETWEEN 3600 AND 2592000",
            name="ck_chat_batches_output_expiry",
        ),
        {"mysql_engine": "InnoDB"},
    )


class ChatBatchItem(Base):
    """Ordered item with exact custom-ID identity and at most one durable run."""

    __tablename__ = "chat_batch_items"

    batch_id: Mapped[str] = mapped_column(
        CHAR(36), ForeignKey("chat_batches.id", name="fk_chat_batch_items_batch", ondelete="RESTRICT"), primary_key=True
    )
    ordinal: Mapped[int] = mapped_column(INT, primary_key=True, autoincrement=False)
    custom_id: Mapped[str] = mapped_column(VARCHAR(64), nullable=False)
    # SHA-256 of exact UTF-8 bytes, without case-folding or normalization.
    custom_id_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    operation: Mapped[str] = mapped_column(VARCHAR(30), nullable=False)
    # Encrypted serialized bodies only; do not replace these with plaintext JSON.
    request_ciphertext: Mapped[str] = mapped_column(MEDIUMTEXT, nullable=False)
    result_ciphertext: Mapped[str | None] = mapped_column(MEDIUMTEXT)
    # NULL until validation freezes these snapshots; no provider I/O precedes validation.
    capability_snapshot: Mapped[dict | None] = mapped_column(JSON)
    pricing_snapshot: Mapped[dict | None] = mapped_column(JSON)
    required_scopes: Mapped[list | None] = mapped_column(JSON)
    input_asset_id: Mapped[str | None] = mapped_column(
        CHAR(36), ForeignKey("chat_assets.id", name="fk_chat_batch_items_input_asset", ondelete="RESTRICT")
    )
    run_id: Mapped[str | None] = mapped_column(
        CHAR(36), ForeignKey("chat_runs.id", name="fk_chat_batch_items_run", ondelete="RESTRICT")
    )
    state: Mapped[str] = mapped_column(VARCHAR(20), nullable=False, default="pending", server_default="pending")
    error_code: Mapped[str | None] = mapped_column(VARCHAR(100))
    error_message: Mapped[str | None] = mapped_column(VARCHAR(1000))
    http_status: Mapped[int | None] = mapped_column(INT)
    request_id: Mapped[str | None] = mapped_column(VARCHAR(190))
    # A projection of the existing run/hold ledger, never an independent billing record.
    settlement_status: Mapped[str] = mapped_column(VARCHAR(20), nullable=False, default="pending", server_default="pending")

    __table_args__ = (
        Index("uq_chat_batch_items_custom_hash", "batch_id", "custom_id_hash", unique=True),
        Index("uq_chat_batch_items_run", "run_id", unique=True),
        Index("idx_chat_batch_items_state_ordinal", "batch_id", "state", "ordinal"),
        Index("idx_chat_batch_items_input_asset", "input_asset_id"),
        CheckConstraint("ordinal >= 1", name="ck_chat_batch_items_ordinal"),
        CheckConstraint("CHAR_LENGTH(custom_id) BETWEEN 1 AND 64", name="ck_chat_batch_items_custom_id"),
        CheckConstraint(
            "operation IN ('chat.completions','responses','images.generations','images.edits','audio.speech','audio.transcriptions')",
            name="ck_chat_batch_items_operation",
        ),
        CheckConstraint(
            "state IN ('pending','queued','running','completed','failed','cancelled','expired','unknown')",
            name="ck_chat_batch_items_state",
        ),
        CheckConstraint("http_status IS NULL OR http_status BETWEEN 100 AND 599", name="ck_chat_batch_items_http_status"),
        {"mysql_engine": "InnoDB"},
    )


class ChatBatchProjectQueue(Base):
    """Project dispatch serialization and fair batch cursor, independent of agent quotas."""

    __tablename__ = "chat_batch_project_queues"

    project_id: Mapped[str] = mapped_column(VARCHAR(64), primary_key=True)
    # Cursor only, not an ownership/pinning relationship or a foreign key.
    last_batch_id: Mapped[str | None] = mapped_column(CHAR(36))
    lease_owner: Mapped[str | None] = mapped_column(VARCHAR(190))
    lease_fence: Mapped[int] = mapped_column(BIGINT, nullable=False, default=0, server_default="0")
    lease_expires_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6))

    __table_args__ = (
        CheckConstraint("lease_fence >= 0", name="ck_chat_batch_project_queues_fence"),
        {"mysql_engine": "InnoDB"},
    )
