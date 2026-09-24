"""Durable resource intent, worker registration, budget and delegation ledgers (migration 016).

MariaDB rows are the authority for every cloud effect: an operation row is committed
before OpenStack I/O, an observation is committed only under the same generation and
lease fence, and physical occupancy is released only after provider absence is proven.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import BIGINT, BOOLEAN, CHAR, INT, JSON, VARCHAR, CheckConstraint, DateTime, ForeignKey, Index, Numeric
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.orm import Mapped, mapped_column

from lumen.db import Base

POOL_ROLES = ("api", "worker", "sandbox")
BACKENDS = ("nova", "zun")
RESOURCE_STATES = ("requested", "creating", "unknown", "booting", "ready", "unavailable", "draining", "deleting", "deleted", "failed")
OCCUPYING_STATES = frozenset({"requested", "creating", "unknown", "booting", "ready", "unavailable", "draining", "deleting", "failed"})
OPERATION_ACTIONS = ("create", "observe", "bootstrap", "delete", "ingress_register", "ingress_deregister")
OPERATION_STATUSES = ("claimed", "submitted", "unknown", "succeeded", "failed")
RESERVATION_KINDS = ("credit", "sandbox_seconds", "child_slot", "sandbox_slot")
DELEGATION_STATES = ("prepared", "waiting", "join_ready", "joined", "canceled")


def _now() -> datetime:
    return datetime.now(UTC)


class ChatRuntimePool(Base):
    """Operator-declared capacity pool; configuration is synced, never user-editable."""

    __tablename__ = "chat_runtime_pools"

    id: Mapped[str] = mapped_column(CHAR(36), primary_key=True)
    deployment_id: Mapped[str] = mapped_column(VARCHAR(64), nullable=False)
    name: Mapped[str] = mapped_column(VARCHAR(64), nullable=False)
    role: Mapped[str] = mapped_column(VARCHAR(10), nullable=False)
    backend: Mapped[str] = mapped_column(VARCHAR(10), nullable=False)
    enabled: Mapped[bool] = mapped_column(BOOLEAN, nullable=False, default=False)
    cloud_profile_id: Mapped[str] = mapped_column(VARCHAR(190), nullable=False)
    project_id: Mapped[str] = mapped_column(VARCHAR(64), nullable=False)
    region_name: Mapped[str] = mapped_column(VARCHAR(190), nullable=False)
    image_ref: Mapped[str] = mapped_column(VARCHAR(255), nullable=False)
    profile_digest: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    min_replicas: Mapped[int] = mapped_column(INT, nullable=False, default=0)
    max_replicas: Mapped[int] = mapped_column(INT, nullable=False, default=0)
    slots_per_worker: Mapped[int] = mapped_column(INT, nullable=False, default=1)
    target_wait_seconds: Mapped[int] = mapped_column(INT, nullable=False, default=10)
    boot_timeout_seconds: Mapped[int] = mapped_column(INT, nullable=False, default=600)
    idle_seconds: Mapped[int] = mapped_column(INT, nullable=False, default=300)
    drain_seconds: Mapped[int] = mapped_column(INT, nullable=False, default=300)
    max_lifetime_seconds: Mapped[int] = mapped_column(INT, nullable=False, default=1800)
    db_connection_budget: Mapped[int | None] = mapped_column(INT)
    ingress_pool_id: Mapped[str | None] = mapped_column(VARCHAR(190))
    ingress_vip: Mapped[str | None] = mapped_column(VARCHAR(190))
    desired_revision: Mapped[int] = mapped_column(BIGINT, nullable=False, default=1)
    reconcile_lease_owner: Mapped[str | None] = mapped_column(VARCHAR(190))
    reconcile_lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reconcile_fence: Mapped[int] = mapped_column(BIGINT, nullable=False, default=0)
    low_demand_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    high_demand_samples: Mapped[int] = mapped_column(INT, nullable=False, default=0)
    service_time_estimate_ms: Mapped[int] = mapped_column(INT, nullable=False, default=30_000)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)

    __table_args__ = (
        Index("uq_runtime_pool_name", "deployment_id", "name", unique=True),
        CheckConstraint("role IN ('api','worker','sandbox')", name="ck_runtime_pool_role"),
        CheckConstraint("backend IN ('nova','zun')", name="ck_runtime_pool_backend"),
        CheckConstraint("min_replicas >= 0 AND max_replicas >= min_replicas", name="ck_runtime_pool_replicas"),
    )


class ChatRuntimeResource(Base):
    """One provisioned VM/container generation; occupancy persists until absence is proven."""

    __tablename__ = "chat_runtime_resources"

    id: Mapped[str] = mapped_column(CHAR(36), primary_key=True)
    pool_id: Mapped[str] = mapped_column(CHAR(36), ForeignKey("chat_runtime_pools.id", ondelete="RESTRICT"), nullable=False)
    generation: Mapped[int] = mapped_column(INT, nullable=False, default=1)
    role: Mapped[str] = mapped_column(VARCHAR(10), nullable=False)
    backend: Mapped[str] = mapped_column(VARCHAR(10), nullable=False)
    logical_project_id: Mapped[str | None] = mapped_column(VARCHAR(64))
    logical_user_id: Mapped[str | None] = mapped_column(VARCHAR(64))
    run_id: Mapped[str | None] = mapped_column(CHAR(36), ForeignKey("chat_runs.id", ondelete="RESTRICT"))
    desired_state: Mapped[str] = mapped_column(VARCHAR(20), nullable=False, default="requested")
    observed_state: Mapped[str] = mapped_column(VARCHAR(20), nullable=False, default="requested")
    request_fingerprint: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    cloud_profile_id: Mapped[str] = mapped_column(VARCHAR(190), nullable=False)
    cloud_project_id: Mapped[str] = mapped_column(VARCHAR(64), nullable=False)
    provider_id: Mapped[str | None] = mapped_column(VARCHAR(190))
    address: Mapped[str | None] = mapped_column(VARCHAR(190))
    port: Mapped[int | None] = mapped_column(INT)
    image_ref: Mapped[str] = mapped_column(VARCHAR(255), nullable=False)
    policy_digest: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    bootstrap_token_hash: Mapped[str | None] = mapped_column(CHAR(64))
    bootstrap_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    certificate_fingerprint: Mapped[str | None] = mapped_column(CHAR(64))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    active_slots: Mapped[int] = mapped_column(INT, nullable=False, default=0)
    ingress_member_id: Mapped[str | None] = mapped_column(VARCHAR(190))
    failure_code: Mapped[str | None] = mapped_column(VARCHAR(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("uq_runtime_resource_provider", "cloud_profile_id", "cloud_project_id", "provider_id", unique=True),
        Index("uq_runtime_resource_run_generation", "run_id", "generation", unique=True),
        Index("idx_runtime_resource_pool_state", "pool_id", "observed_state"),
        Index("idx_runtime_resource_desired", "desired_state", "deadline_at"),
    )


class ChatResourceOperation(Base):
    """Durable cloud intent claimed before I/O; an unknown result is never resubmitted."""

    __tablename__ = "chat_resource_operations"

    id: Mapped[str] = mapped_column(CHAR(36), primary_key=True)
    resource_id: Mapped[str] = mapped_column(CHAR(36), ForeignKey("chat_runtime_resources.id", ondelete="RESTRICT"), nullable=False)
    generation: Mapped[int] = mapped_column(INT, nullable=False)
    action: Mapped[str] = mapped_column(VARCHAR(30), nullable=False)
    request_status: Mapped[str] = mapped_column(VARCHAR(20), nullable=False, default="claimed")
    owner: Mapped[str] = mapped_column(VARCHAR(190), nullable=False)
    fence: Mapped[int] = mapped_column(BIGINT, nullable=False)
    provider_request_id: Mapped[str | None] = mapped_column(VARCHAR(190))
    attempts: Mapped[int] = mapped_column(INT, nullable=False, default=0)
    retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(VARCHAR(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)

    __table_args__ = (
        Index("uq_resource_operation_identity", "resource_id", "generation", "action", unique=True),
        Index("idx_resource_operation_retry", "request_status", "retry_at"),
    )


class ChatWorkerRegistration(Base):
    """Worker identity plus boot UUID; host/PID alone cannot survive recycled resources."""

    __tablename__ = "chat_worker_registrations"

    id: Mapped[str] = mapped_column(CHAR(36), primary_key=True)
    worker_identity: Mapped[str] = mapped_column(VARCHAR(190), nullable=False)
    boot_id: Mapped[str] = mapped_column(CHAR(36), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(CHAR(36), ForeignKey("chat_runtime_resources.id", ondelete="SET NULL"))
    resource_generation: Mapped[int | None] = mapped_column(INT)
    certificate_fingerprint: Mapped[str | None] = mapped_column(CHAR(64))
    pool_id: Mapped[str | None] = mapped_column(CHAR(36), ForeignKey("chat_runtime_pools.id", ondelete="SET NULL"))
    protocol_versions: Mapped[list] = mapped_column(JSON, nullable=False)
    plugin_digest: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    schema_version: Mapped[int] = mapped_column(INT, nullable=False)
    capacity: Mapped[int] = mapped_column(INT, nullable=False)
    active_count: Mapped[int] = mapped_column(INT, nullable=False, default=0)
    accepting: Mapped[bool] = mapped_column(BOOLEAN, nullable=False, default=True)
    draining: Mapped[bool] = mapped_column(BOOLEAN, nullable=False, default=False)
    drain_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)

    __table_args__ = (
        Index("uq_worker_registration_boot", "worker_identity", "boot_id", unique=True),
        Index("idx_worker_registration_heartbeat", "accepting", "heartbeat_at"),
    )


class ChatProjectAgentQuota(Base):
    """Per-project caps; zero means delegation/sandbox disabled, never unlimited."""

    __tablename__ = "chat_project_agent_quotas"

    project_id: Mapped[str] = mapped_column(VARCHAR(64), primary_key=True)
    max_active_children: Mapped[int] = mapped_column(INT, nullable=False, default=0)
    max_active_sandboxes: Mapped[int] = mapped_column(INT, nullable=False, default=0)
    max_sandbox_seconds: Mapped[int] = mapped_column(INT, nullable=False, default=0)
    max_credit_reservation: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False, default=Decimal("0"))
    active_children: Mapped[int] = mapped_column(INT, nullable=False, default=0)
    active_sandboxes: Mapped[int] = mapped_column(INT, nullable=False, default=0)
    sandbox_seconds_reserved: Mapped[int] = mapped_column(INT, nullable=False, default=0)
    credits_reserved: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False, default=Decimal("0"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)


class ChatAgentReservation(Base):
    """One reservation per (run, kind); settled exactly once, released exactly once."""

    __tablename__ = "chat_agent_reservations"

    id: Mapped[str] = mapped_column(CHAR(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(VARCHAR(64), nullable=False)
    root_run_id: Mapped[str] = mapped_column(CHAR(36), ForeignKey("chat_runs.id", ondelete="RESTRICT"), nullable=False)
    run_id: Mapped[str] = mapped_column(CHAR(36), ForeignKey("chat_runs.id", ondelete="RESTRICT"), nullable=False)
    kind: Mapped[str] = mapped_column(VARCHAR(20), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    settled_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    status: Mapped[str] = mapped_column(VARCHAR(20), nullable=False, default="reserved")
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)

    __table_args__ = (
        Index("uq_agent_reservation_run_kind", "run_id", "kind", unique=True),
        Index("idx_agent_reservation_root", "root_run_id", "status"),
    )


class ChatDelegationGroup(Base):
    """All delegation calls of one model response form one wait group."""

    __tablename__ = "chat_delegation_groups"

    id: Mapped[str] = mapped_column(CHAR(36), primary_key=True)
    parent_run_id: Mapped[str] = mapped_column(CHAR(36), ForeignKey("chat_runs.id", ondelete="RESTRICT"), nullable=False)
    model_segment_id: Mapped[str] = mapped_column(VARCHAR(190), nullable=False)
    checkpoint_ns: Mapped[str | None] = mapped_column(VARCHAR(190))
    checkpoint_id: Mapped[str | None] = mapped_column(VARCHAR(190))
    state: Mapped[str] = mapped_column(VARCHAR(20), nullable=False, default="prepared")
    join_segment_id: Mapped[str | None] = mapped_column(VARCHAR(190))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)

    __table_args__ = (
        Index("uq_delegation_group_segment", "parent_run_id", "model_segment_id", unique=True),
        Index("uq_delegation_group_join", "join_segment_id", unique=True),
        Index("idx_delegation_group_state", "state", "parent_run_id"),
    )


class ChatDelegationCall(Base):
    """One delegation call; a replayed identical call recovers its child instead of spawning."""

    __tablename__ = "chat_delegation_calls"

    id: Mapped[str] = mapped_column(CHAR(36), primary_key=True)
    group_id: Mapped[str] = mapped_column(CHAR(36), ForeignKey("chat_delegation_groups.id", ondelete="RESTRICT"), nullable=False)
    parent_run_id: Mapped[str] = mapped_column(CHAR(36), ForeignKey("chat_runs.id", ondelete="RESTRICT"), nullable=False)
    call_id: Mapped[str] = mapped_column(VARCHAR(190), nullable=False)
    fingerprint: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    ordinal: Mapped[int] = mapped_column(INT, nullable=False)
    child_run_id: Mapped[str | None] = mapped_column(CHAR(36), ForeignKey("chat_runs.id", ondelete="RESTRICT"))
    state: Mapped[str] = mapped_column(VARCHAR(20), nullable=False, default="prepared")
    result_payload: Mapped[str | None] = mapped_column(MEDIUMTEXT)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)

    __table_args__ = (
        Index("uq_delegation_call_identity", "parent_run_id", "call_id", unique=True),
        Index("idx_delegation_call_child", "child_run_id"),
    )
