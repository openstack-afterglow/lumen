-- Resource intent, worker registrations, budgets and delegation ledgers.
-- Additive only; ChatRun ancestry/budget columns from 001 are reused, not duplicated.

CREATE TABLE IF NOT EXISTS chat_runtime_pools (
    id CHAR(36) NOT NULL PRIMARY KEY,
    deployment_id VARCHAR(64) NOT NULL,
    name VARCHAR(64) NOT NULL,
    role VARCHAR(10) NOT NULL,
    backend VARCHAR(10) NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT FALSE,
    cloud_profile_id VARCHAR(190) NOT NULL,
    project_id VARCHAR(64) NOT NULL,
    region_name VARCHAR(190) NOT NULL,
    image_ref VARCHAR(255) NOT NULL,
    profile_digest CHAR(64) NOT NULL,
    min_replicas INT NOT NULL DEFAULT 0,
    max_replicas INT NOT NULL DEFAULT 0,
    slots_per_worker INT NOT NULL DEFAULT 1,
    target_wait_seconds INT NOT NULL DEFAULT 10,
    boot_timeout_seconds INT NOT NULL DEFAULT 600,
    idle_seconds INT NOT NULL DEFAULT 300,
    drain_seconds INT NOT NULL DEFAULT 300,
    max_lifetime_seconds INT NOT NULL DEFAULT 1800,
    db_connection_budget INT NULL,
    ingress_pool_id VARCHAR(190) NULL,
    ingress_vip VARCHAR(190) NULL,
    desired_revision BIGINT NOT NULL DEFAULT 1,
    reconcile_lease_owner VARCHAR(190) NULL,
    reconcile_lease_expires_at DATETIME(6) NULL,
    reconcile_fence BIGINT NOT NULL DEFAULT 0,
    low_demand_since DATETIME(6) NULL,
    high_demand_samples INT NOT NULL DEFAULT 0,
    service_time_estimate_ms INT NOT NULL DEFAULT 30000,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    UNIQUE KEY uq_runtime_pool_name (deployment_id, name),
    CONSTRAINT ck_runtime_pool_role CHECK (role IN ('api','worker','sandbox')),
    CONSTRAINT ck_runtime_pool_backend CHECK (backend IN ('nova','zun')),
    CONSTRAINT ck_runtime_pool_replicas CHECK (min_replicas >= 0 AND max_replicas >= min_replicas)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS chat_runtime_resources (
    id CHAR(36) NOT NULL PRIMARY KEY,
    pool_id CHAR(36) NOT NULL,
    generation INT NOT NULL DEFAULT 1,
    role VARCHAR(10) NOT NULL,
    backend VARCHAR(10) NOT NULL,
    logical_project_id VARCHAR(64) NULL,
    logical_user_id VARCHAR(64) NULL,
    run_id CHAR(36) NULL,
    desired_state VARCHAR(20) NOT NULL DEFAULT 'requested',
    observed_state VARCHAR(20) NOT NULL DEFAULT 'requested',
    request_fingerprint CHAR(64) NOT NULL,
    cloud_profile_id VARCHAR(190) NOT NULL,
    cloud_project_id VARCHAR(64) NOT NULL,
    provider_id VARCHAR(190) NULL,
    address VARCHAR(190) NULL,
    port INT NULL,
    image_ref VARCHAR(255) NOT NULL,
    policy_digest CHAR(64) NOT NULL,
    bootstrap_token_hash CHAR(64) NULL,
    bootstrap_expires_at DATETIME(6) NULL,
    certificate_fingerprint CHAR(64) NULL,
    heartbeat_at DATETIME(6) NULL,
    ready_at DATETIME(6) NULL,
    deadline_at DATETIME(6) NULL,
    active_slots INT NOT NULL DEFAULT 0,
    ingress_member_id VARCHAR(190) NULL,
    failure_code VARCHAR(100) NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    deleted_at DATETIME(6) NULL,
    UNIQUE KEY uq_runtime_resource_provider (cloud_profile_id, cloud_project_id, provider_id),
    UNIQUE KEY uq_runtime_resource_run_generation (run_id, generation),
    KEY idx_runtime_resource_pool_state (pool_id, observed_state),
    KEY idx_runtime_resource_desired (desired_state, deadline_at),
    CONSTRAINT fk_runtime_resource_pool FOREIGN KEY (pool_id) REFERENCES chat_runtime_pools (id) ON DELETE RESTRICT,
    CONSTRAINT fk_runtime_resource_run FOREIGN KEY (run_id) REFERENCES chat_runs (id) ON DELETE RESTRICT
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS chat_resource_operations (
    id CHAR(36) NOT NULL PRIMARY KEY,
    resource_id CHAR(36) NOT NULL,
    generation INT NOT NULL,
    action VARCHAR(30) NOT NULL,
    request_status VARCHAR(20) NOT NULL DEFAULT 'claimed',
    owner VARCHAR(190) NOT NULL,
    fence BIGINT NOT NULL,
    provider_request_id VARCHAR(190) NULL,
    attempts INT NOT NULL DEFAULT 0,
    retry_at DATETIME(6) NULL,
    error_code VARCHAR(100) NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    UNIQUE KEY uq_resource_operation_identity (resource_id, generation, action),
    KEY idx_resource_operation_retry (request_status, retry_at),
    CONSTRAINT fk_resource_operation_resource FOREIGN KEY (resource_id) REFERENCES chat_runtime_resources (id) ON DELETE RESTRICT
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS chat_worker_registrations (
    id CHAR(36) NOT NULL PRIMARY KEY,
    worker_identity VARCHAR(190) NOT NULL,
    boot_id CHAR(36) NOT NULL,
    resource_id CHAR(36) NULL,
    pool_id CHAR(36) NULL,
    protocol_versions JSON NOT NULL,
    plugin_digest CHAR(64) NOT NULL,
    schema_version INT NOT NULL,
    capacity INT NOT NULL,
    active_count INT NOT NULL DEFAULT 0,
    accepting BOOLEAN NOT NULL DEFAULT TRUE,
    draining BOOLEAN NOT NULL DEFAULT FALSE,
    drain_started_at DATETIME(6) NULL,
    heartbeat_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    UNIQUE KEY uq_worker_registration_boot (worker_identity, boot_id),
    KEY idx_worker_registration_heartbeat (accepting, heartbeat_at),
    CONSTRAINT fk_worker_registration_resource FOREIGN KEY (resource_id) REFERENCES chat_runtime_resources (id) ON DELETE SET NULL,
    CONSTRAINT fk_worker_registration_pool FOREIGN KEY (pool_id) REFERENCES chat_runtime_pools (id) ON DELETE SET NULL
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS chat_project_agent_quotas (
    project_id VARCHAR(64) NOT NULL PRIMARY KEY,
    max_active_children INT NOT NULL DEFAULT 0,
    max_active_sandboxes INT NOT NULL DEFAULT 0,
    max_sandbox_seconds INT NOT NULL DEFAULT 0,
    max_credit_reservation DECIMAL(18,8) NOT NULL DEFAULT 0,
    active_children INT NOT NULL DEFAULT 0,
    active_sandboxes INT NOT NULL DEFAULT 0,
    sandbox_seconds_reserved INT NOT NULL DEFAULT 0,
    credits_reserved DECIMAL(18,8) NOT NULL DEFAULT 0,
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS chat_agent_reservations (
    id CHAR(36) NOT NULL PRIMARY KEY,
    project_id VARCHAR(64) NOT NULL,
    root_run_id CHAR(36) NOT NULL,
    run_id CHAR(36) NOT NULL,
    kind VARCHAR(20) NOT NULL,
    amount DECIMAL(18,8) NOT NULL,
    settled_amount DECIMAL(18,8) NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'reserved',
    settled_at DATETIME(6) NULL,
    released_at DATETIME(6) NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    UNIQUE KEY uq_agent_reservation_run_kind (run_id, kind),
    KEY idx_agent_reservation_root (root_run_id, status),
    CONSTRAINT fk_agent_reservation_root FOREIGN KEY (root_run_id) REFERENCES chat_runs (id) ON DELETE RESTRICT,
    CONSTRAINT fk_agent_reservation_run FOREIGN KEY (run_id) REFERENCES chat_runs (id) ON DELETE RESTRICT
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS chat_delegation_groups (
    id CHAR(36) NOT NULL PRIMARY KEY,
    parent_run_id CHAR(36) NOT NULL,
    model_segment_id VARCHAR(190) NOT NULL,
    checkpoint_ns VARCHAR(190) NULL,
    checkpoint_id VARCHAR(190) NULL,
    state VARCHAR(20) NOT NULL DEFAULT 'prepared',
    join_segment_id VARCHAR(190) NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    UNIQUE KEY uq_delegation_group_segment (parent_run_id, model_segment_id),
    UNIQUE KEY uq_delegation_group_join (join_segment_id),
    KEY idx_delegation_group_state (state, parent_run_id),
    CONSTRAINT fk_delegation_group_parent FOREIGN KEY (parent_run_id) REFERENCES chat_runs (id) ON DELETE RESTRICT
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS chat_delegation_calls (
    id CHAR(36) NOT NULL PRIMARY KEY,
    group_id CHAR(36) NOT NULL,
    parent_run_id CHAR(36) NOT NULL,
    call_id VARCHAR(190) NOT NULL,
    fingerprint CHAR(64) NOT NULL,
    ordinal INT NOT NULL,
    child_run_id CHAR(36) NULL,
    state VARCHAR(20) NOT NULL DEFAULT 'prepared',
    result_payload MEDIUMTEXT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    UNIQUE KEY uq_delegation_call_identity (parent_run_id, call_id),
    KEY idx_delegation_call_child (child_run_id),
    CONSTRAINT fk_delegation_call_group FOREIGN KEY (group_id) REFERENCES chat_delegation_groups (id) ON DELETE RESTRICT,
    CONSTRAINT fk_delegation_call_parent FOREIGN KEY (parent_run_id) REFERENCES chat_runs (id) ON DELETE RESTRICT,
    CONSTRAINT fk_delegation_call_child FOREIGN KEY (child_run_id) REFERENCES chat_runs (id) ON DELETE RESTRICT
) ENGINE=InnoDB;

ALTER TABLE chat_runs
    ADD COLUMN IF NOT EXISTS lease_fence BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS assigned_resource_id CHAR(36) NULL,
    ADD COLUMN IF NOT EXISTS runtime_pool_id CHAR(36) NULL,
    ADD COLUMN IF NOT EXISTS required_plugin_digest CHAR(64) NULL,
    ADD COLUMN IF NOT EXISTS credit_ceiling DECIMAL(18,8) NULL,
    ADD COLUMN IF NOT EXISTS wall_time_seconds INT NULL,
    ADD COLUMN IF NOT EXISTS deadline_at DATETIME(6) NULL;

ALTER TABLE chat_runs
    ADD INDEX IF NOT EXISTS idx_chat_runs_status_pool (status, runtime_pool_id),
    ADD INDEX IF NOT EXISTS idx_chat_runs_status_plugin_digest (status, required_plugin_digest),
    ADD INDEX IF NOT EXISTS idx_chat_runs_assigned_resource (assigned_resource_id);
