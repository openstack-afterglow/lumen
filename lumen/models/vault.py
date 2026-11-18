"""Encrypted Agent Vault notes and immutable revisions (migration 040)."""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import BIGINT, BOOLEAN, CHAR, INT, JSON, VARCHAR, CheckConstraint, DateTime, ForeignKey, Index, Numeric
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.orm import Mapped, mapped_column

from lumen.db import Base


class VaultNote(Base):
    __tablename__ = "memory_vault_notes"

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(VARCHAR(64), nullable=False)
    domain_id: Mapped[str | None] = mapped_column(VARCHAR(64))
    project_key: Mapped[str | None] = mapped_column(VARCHAR(80))
    scope_key: Mapped[str] = mapped_column(CHAR(64), nullable=False, unique=True)
    kind: Mapped[str] = mapped_column(VARCHAR(24), nullable=False)
    role: Mapped[str | None] = mapped_column(VARCHAR(24))
    slug: Mapped[str] = mapped_column(VARCHAR(128), nullable=False)
    title: Mapped[str] = mapped_column(VARCHAR(200), nullable=False)
    content: Mapped[str] = mapped_column("body_ciphertext", MEDIUMTEXT, nullable=False)
    body_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    details: Mapped[dict] = mapped_column("metadata", JSON, nullable=False)
    status: Mapped[str] = mapped_column(VARCHAR(20), nullable=False)
    revision: Mapped[int] = mapped_column(INT, nullable=False)
    supersedes_id: Mapped[int | None] = mapped_column(BIGINT, ForeignKey("memory_vault_notes.id"))
    project_id: Mapped[str | None] = mapped_column(VARCHAR(64))  # Origin only, except workspace memories.
    workspace_id: Mapped[int | None] = mapped_column(BIGINT)
    category: Mapped[str | None] = mapped_column(VARCHAR(32))
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(BOOLEAN, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("idx_memory_vault_notes_scope", "user_id", "domain_id", "status", "kind", "updated_at"),
        CheckConstraint("project_key IS NULL OR domain_id IS NOT NULL", name="ck_memory_vault_project_domain"),
        CheckConstraint(
            "workspace_id IS NULL OR (project_id IS NOT NULL AND domain_id IS NOT NULL)",
            name="ck_memory_vault_workspace_owner",
        ),
    )

    @property
    def scope(self) -> str:
        if self.domain_id is None:
            return "account"
        return "workspace" if self.workspace_id is not None else "project"

    @property
    def vector_project_id(self) -> str | None:
        # Index plugins use the legacy project column as an opaque namespace key.
        return self.domain_id


class VaultRevision(Base):
    __tablename__ = "memory_vault_revisions"

    note_id: Mapped[int] = mapped_column(BIGINT, ForeignKey("memory_vault_notes.id"), primary_key=True)
    revision: Mapped[int] = mapped_column(INT, primary_key=True)
    content: Mapped[str] = mapped_column("body_ciphertext", MEDIUMTEXT, nullable=False)
    body_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    title: Mapped[str] = mapped_column(VARCHAR(200), nullable=False)
    details: Mapped[dict] = mapped_column("metadata", JSON, nullable=False)
    status: Mapped[str] = mapped_column(VARCHAR(20), nullable=False)
    change_kind: Mapped[str] = mapped_column(VARCHAR(20), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
