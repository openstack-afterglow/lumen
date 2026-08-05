"""ORM models for the additive v2 web-agent protocol migrations (053-055)."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import BIGINT, BOOLEAN, CHAR, INT, JSON, VARCHAR, DateTime, ForeignKey, Index, UniqueConstraint
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.orm import Mapped, mapped_column

from lumen.db import Base


def _now() -> datetime:
    return datetime.now(UTC)


class ChatRunInteraction(Base):
    __tablename__ = "chat_run_interactions"

    run_id: Mapped[str] = mapped_column(CHAR(36), ForeignKey("chat_runs.id", ondelete="CASCADE"), primary_key=True)
    id: Mapped[str] = mapped_column(CHAR(36), primary_key=True)
    status: Mapped[str] = mapped_column(VARCHAR(20), nullable=False, default="pending")
    request_ciphertext: Mapped[str] = mapped_column(MEDIUMTEXT, nullable=False)
    response_ciphertext: Mapped[str | None] = mapped_column(MEDIUMTEXT)
    response_schema: Mapped[dict] = mapped_column(JSON, nullable=False)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_by_user_id: Mapped[str | None] = mapped_column(VARCHAR(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (Index("idx_chat_run_interactions_pending_expiry", "status", "expires_at"),)


class ChatContextCheckpoint(Base):
    __tablename__ = "chat_context_checkpoints"

    id: Mapped[str] = mapped_column(CHAR(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(CHAR(36), ForeignKey("chat_runs.id", ondelete="CASCADE"), nullable=False)
    conversation_id: Mapped[str | None] = mapped_column(
        CHAR(36), ForeignKey("chat_conversations.id", ondelete="SET NULL")
    )
    source_anchor_message_id: Mapped[int | None] = mapped_column(
        BIGINT, ForeignKey("chat_messages.id", ondelete="SET NULL")
    )
    source_hashes: Mapped[list] = mapped_column(JSON, nullable=False)
    summary_ciphertext: Mapped[str] = mapped_column(MEDIUMTEXT, nullable=False)
    token_estimate: Mapped[int] = mapped_column(INT, nullable=False)
    context_limit: Mapped[int] = mapped_column(INT, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)

    __table_args__ = (
        Index("idx_chat_context_checkpoints_run", "run_id"),
        Index("idx_chat_context_checkpoints_conversation_anchor", "conversation_id", "source_anchor_message_id"),
    )


class ChatGitCredential(Base):
    __tablename__ = "chat_git_credentials"

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    owner_user_id: Mapped[str] = mapped_column(VARCHAR(64), nullable=False)
    owner_project_id: Mapped[str] = mapped_column(VARCHAR(64), nullable=False)
    host: Mapped[str] = mapped_column(VARCHAR(255), nullable=False)
    username: Mapped[str | None] = mapped_column(VARCHAR(255))
    token_ciphertext: Mapped[str] = mapped_column(MEDIUMTEXT, nullable=False)
    credential_version: Mapped[int] = mapped_column(BIGINT, nullable=False, default=1)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)

    __table_args__ = (
        UniqueConstraint("owner_user_id", "owner_project_id", "host", name="uq_chat_git_credentials_owner_host"),
        Index("idx_chat_git_credentials_owner", "owner_user_id", "owner_project_id"),
    )


class ChatCodeWorkspace(Base):
    __tablename__ = "chat_code_workspaces"

    id: Mapped[str] = mapped_column(CHAR(36), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(VARCHAR(64), nullable=False)
    owner_project_id: Mapped[str] = mapped_column(VARCHAR(64), nullable=False)
    name: Mapped[str] = mapped_column(VARCHAR(100), nullable=False)
    source_kind: Mapped[str] = mapped_column(VARCHAR(10), nullable=False)
    repository_url: Mapped[str | None] = mapped_column(VARCHAR(2048))
    source_revision: Mapped[str | None] = mapped_column(VARCHAR(255))
    resolved_base_commit_sha: Mapped[str | None] = mapped_column(CHAR(64))
    working_branch: Mapped[str] = mapped_column(VARCHAR(255), nullable=False)
    credential_id: Mapped[int | None] = mapped_column(
        BIGINT, ForeignKey("chat_git_credentials.id", ondelete="SET NULL")
    )
    credential_version: Mapped[int | None] = mapped_column(BIGINT)
    runtime_workspace_id: Mapped[str | None] = mapped_column(VARCHAR(255))
    lifecycle_status: Mapped[str] = mapped_column(VARCHAR(20), nullable=False, default="creating")
    state_revision: Mapped[int] = mapped_column(BIGINT, nullable=False, default=0)
    runtime_capabilities: Mapped[dict | None] = mapped_column(JSON)
    image_digest: Mapped[str] = mapped_column(VARCHAR(255), nullable=False)
    policy_fingerprint: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    writer_lease_owner: Mapped[str | None] = mapped_column(VARCHAR(190))
    writer_lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    writer_fence: Mapped[int] = mapped_column(BIGINT, nullable=False, default=0)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    error_code: Mapped[str | None] = mapped_column(VARCHAR(100))
    request_fingerprint: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)

    __table_args__ = (
        UniqueConstraint(
            "owner_user_id", "owner_project_id", "request_fingerprint", name="uq_chat_code_workspaces_request"
        ),
        Index("idx_chat_code_workspaces_owner_status", "owner_user_id", "owner_project_id", "lifecycle_status"),
        Index("idx_chat_code_workspaces_expiry", "expires_at", "lifecycle_status"),
    )


class ChatCodeWorkspaceAsset(Base):
    __tablename__ = "chat_code_workspace_assets"

    workspace_id: Mapped[str] = mapped_column(
        CHAR(36), ForeignKey("chat_code_workspaces.id", ondelete="RESTRICT"), primary_key=True
    )
    asset_id: Mapped[str] = mapped_column(CHAR(36), ForeignKey("chat_assets.id", ondelete="RESTRICT"), primary_key=True)
    purpose: Mapped[str] = mapped_column(VARCHAR(30), primary_key=True)
    state_revision: Mapped[int] = mapped_column(BIGINT, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)


class ChatCommand(Base):
    __tablename__ = "chat_commands"

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    owner_user_id: Mapped[str] = mapped_column(VARCHAR(64), nullable=False)
    project_id: Mapped[str] = mapped_column(VARCHAR(64), nullable=False)
    slug: Mapped[str] = mapped_column(VARCHAR(100), nullable=False)
    prompt_template_ciphertext: Mapped[str] = mapped_column(MEDIUMTEXT, nullable=False)
    argument_hint: Mapped[str | None] = mapped_column(VARCHAR(500))
    agent_id: Mapped[int | None] = mapped_column(BIGINT, ForeignKey("chat_agents.id", ondelete="SET NULL"))
    skill_ids: Mapped[list | None] = mapped_column(JSON)
    execution_mode: Mapped[str] = mapped_column(VARCHAR(10), nullable=False, default="chat")
    is_active: Mapped[bool] = mapped_column(BOOLEAN, nullable=False, default=True)
    content_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    source_package_id: Mapped[str | None] = mapped_column(CHAR(36))
    source_package_version: Mapped[str | None] = mapped_column(VARCHAR(64))
    managed_by_package: Mapped[bool] = mapped_column(BOOLEAN, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)

    __table_args__ = (
        UniqueConstraint("project_id", "slug", name="uq_chat_commands_project_slug"),
        Index("idx_chat_commands_owner_project", "owner_user_id", "project_id", "is_active"),
    )


class ChatExtensionPackage(Base):
    __tablename__ = "chat_extension_packages"

    id: Mapped[str] = mapped_column(CHAR(36), primary_key=True)
    name: Mapped[str] = mapped_column(VARCHAR(100), nullable=False)
    version: Mapped[str] = mapped_column(VARCHAR(64), nullable=False)
    description: Mapped[str] = mapped_column(VARCHAR(1000), nullable=False)
    manifest_ciphertext: Mapped[str] = mapped_column(MEDIUMTEXT, nullable=False)
    manifest_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    status: Mapped[str] = mapped_column(VARCHAR(20), nullable=False, default="active")
    created_by_user_id: Mapped[str] = mapped_column(VARCHAR(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)

    __table_args__ = (
        UniqueConstraint("name", "version", name="uq_chat_extension_packages_name_version"),
        Index("idx_chat_extension_packages_status", "status", "name"),
    )


class ChatExtensionPackageInstall(Base):
    __tablename__ = "chat_extension_package_installs"

    id: Mapped[str] = mapped_column(CHAR(36), primary_key=True)
    package_id: Mapped[str] = mapped_column(
        CHAR(36), ForeignKey("chat_extension_packages.id", ondelete="RESTRICT"), nullable=False
    )
    package_name: Mapped[str] = mapped_column(VARCHAR(100), nullable=False)
    owner_user_id: Mapped[str] = mapped_column(VARCHAR(64), nullable=False)
    owner_project_id: Mapped[str] = mapped_column(VARCHAR(64), nullable=False)
    status: Mapped[str] = mapped_column(VARCHAR(20), nullable=False, default="installing")
    installed_version: Mapped[str] = mapped_column(VARCHAR(64), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)

    __table_args__ = (
        UniqueConstraint(
            "owner_user_id", "owner_project_id", "request_fingerprint", name="uq_chat_extension_install_request"
        ),
        UniqueConstraint(
            "owner_user_id", "owner_project_id", "package_name", name="uq_chat_extension_install_project_name"
        ),
        Index("idx_chat_extension_installs_owner_status", "owner_user_id", "owner_project_id", "status"),
    )


class ChatExtensionPackageComponent(Base):
    __tablename__ = "chat_extension_package_components"

    install_id: Mapped[str] = mapped_column(
        CHAR(36), ForeignKey("chat_extension_package_installs.id", ondelete="RESTRICT"), primary_key=True
    )
    component_type: Mapped[str] = mapped_column(VARCHAR(30), primary_key=True)
    component_ref: Mapped[str] = mapped_column(VARCHAR(100), primary_key=True)
    materialized_id: Mapped[int | None] = mapped_column(BIGINT)
    content_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
