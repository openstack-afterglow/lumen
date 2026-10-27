-- Runtime routing, pool projections and durable drain gates (§I).
-- Routing UUIDs deliberately have no FKs; runtime_pool_id keeps its sandbox meaning.
-- Add required fields as nullable first so legacy rows can be classified before defaults.
ALTER TABLE chat_runs
    ADD COLUMN IF NOT EXISTS workload_class VARCHAR(20) NULL,
    ADD COLUMN IF NOT EXISTS worker_pool_id CHAR(36) NULL,
    ADD COLUMN IF NOT EXISTS worker_registration_id CHAR(36) NULL,
    ADD COLUMN IF NOT EXISTS batch_id CHAR(36) NULL;

UPDATE chat_runs
SET workload_class = CASE
    WHEN run_kind IN ('image', 'tts', 'stt') THEN 'online_media'
    WHEN run_kind = 'realtime' THEN 'realtime'
    ELSE 'online_text'
END
WHERE workload_class IS NULL;

ALTER TABLE chat_runs
    MODIFY COLUMN workload_class VARCHAR(20) NOT NULL DEFAULT 'online_text';

CREATE INDEX IF NOT EXISTS idx_chat_runs_worker_pool_workload_status
    ON chat_runs (worker_pool_id, workload_class, status, created_at, id);
CREATE INDEX IF NOT EXISTS idx_chat_runs_worker_registration_lease
    ON chat_runs (worker_registration_id, status, lease_expires_at);
CREATE INDEX IF NOT EXISTS idx_chat_runs_batch_status
    ON chat_runs (batch_id, status);

ALTER TABLE chat_runtime_pools
    ADD COLUMN IF NOT EXISTS workload_class VARCHAR(20) NULL,
    ADD COLUMN IF NOT EXISTS guest_profile_id VARCHAR(190) NULL,
    ADD COLUMN IF NOT EXISTS guest_profile_digest CHAR(64) NULL,
    ADD COLUMN IF NOT EXISTS max_surge INT NULL,
    ADD COLUMN IF NOT EXISTS desired_replicas INT NULL,
    ADD COLUMN IF NOT EXISTS ready_replicas INT NULL,
    ADD COLUMN IF NOT EXISTS provisioning_replicas INT NULL,
    ADD COLUMN IF NOT EXISTS draining_replicas INT NULL,
    ADD COLUMN IF NOT EXISTS queued_count INT NULL,
    ADD COLUMN IF NOT EXISTS oldest_queued_seconds INT NULL,
    ADD COLUMN IF NOT EXISTS last_scale_reason VARCHAR(40) NULL,
    ADD COLUMN IF NOT EXISTS last_reconciled_at DATETIME(6) NULL,
    ADD COLUMN IF NOT EXISTS service_time_cursor_at DATETIME(6) NULL,
    ADD COLUMN IF NOT EXISTS service_time_cursor_id CHAR(36) NULL;

-- API/sandbox pools stay unclassified; preserve operator-selected media/batch pools on retry.
UPDATE chat_runtime_pools SET workload_class = 'online_text'
    WHERE workload_class IS NULL AND role = 'worker';
UPDATE chat_runtime_pools SET max_surge = 1 WHERE max_surge IS NULL;
UPDATE chat_runtime_pools SET desired_replicas = 0 WHERE desired_replicas IS NULL;
UPDATE chat_runtime_pools SET ready_replicas = 0 WHERE ready_replicas IS NULL;
UPDATE chat_runtime_pools SET provisioning_replicas = 0 WHERE provisioning_replicas IS NULL;
UPDATE chat_runtime_pools SET draining_replicas = 0 WHERE draining_replicas IS NULL;
UPDATE chat_runtime_pools SET queued_count = 0 WHERE queued_count IS NULL;

ALTER TABLE chat_runtime_pools
    MODIFY COLUMN max_surge INT NOT NULL DEFAULT 1,
    MODIFY COLUMN desired_replicas INT NOT NULL DEFAULT 0,
    MODIFY COLUMN ready_replicas INT NOT NULL DEFAULT 0,
    MODIFY COLUMN provisioning_replicas INT NOT NULL DEFAULT 0,
    MODIFY COLUMN draining_replicas INT NOT NULL DEFAULT 0,
    MODIFY COLUMN queued_count INT NOT NULL DEFAULT 0;

ALTER TABLE chat_worker_registrations
    ADD COLUMN IF NOT EXISTS workload_classes JSON NULL,
    ADD COLUMN IF NOT EXISTS drain_ack_at DATETIME(6) NULL,
    ADD COLUMN IF NOT EXISTS auxiliary_active INT NULL;

-- Managed workers inherit the pool's singleton class; fixed/unclassified legacy workers retain text/media coverage.
-- Only unset lists are backfilled: retries never overwrite newly registered/admin-selected classes.
UPDATE chat_worker_registrations AS registration
LEFT JOIN chat_runtime_pools AS pool ON pool.id = registration.pool_id
SET registration.workload_classes = CASE
    WHEN registration.pool_id IS NULL OR pool.workload_class IS NULL THEN JSON_ARRAY('online_text', 'online_media')
    ELSE JSON_ARRAY(pool.workload_class)
END
WHERE registration.workload_classes IS NULL;
UPDATE chat_worker_registrations SET auxiliary_active = 0 WHERE auxiliary_active IS NULL;

ALTER TABLE chat_worker_registrations
    MODIFY COLUMN workload_classes JSON NOT NULL DEFAULT '["online_text", "online_media"]',
    MODIFY COLUMN auxiliary_active INT NOT NULL DEFAULT 0;

ALTER TABLE chat_runtime_resources
    ADD COLUMN IF NOT EXISTS guest_profile_id VARCHAR(190) NULL,
    ADD COLUMN IF NOT EXISTS guest_profile_digest CHAR(64) NULL,
    ADD COLUMN IF NOT EXISTS idle_since DATETIME(6) NULL,
    ADD COLUMN IF NOT EXISTS accepting BOOLEAN NULL,
    ADD COLUMN IF NOT EXISTS drain_requested_at DATETIME(6) NULL,
    ADD COLUMN IF NOT EXISTS drain_ack_at DATETIME(6) NULL,
    ADD COLUMN IF NOT EXISTS drain_reason VARCHAR(100) NULL,
    ADD COLUMN IF NOT EXISTS api_counter_snapshot JSON NULL,
    ADD COLUMN IF NOT EXISTS api_counter_snapshot_at DATETIME(6) NULL;

UPDATE chat_runtime_resources SET accepting = TRUE WHERE accepting IS NULL;
ALTER TABLE chat_runtime_resources MODIFY COLUMN accepting BOOLEAN NOT NULL DEFAULT TRUE;

-- Auxiliary owners are registration UUIDs; deletion gates query all live leases, not heartbeat counts.
CREATE INDEX IF NOT EXISTS idx_chat_jobs_owner_lease
    ON chat_jobs (lease_owner, status, lease_expires_at);
CREATE INDEX IF NOT EXISTS idx_chat_memory_outbox_owner_lease
    ON chat_memory_outbox (lease_owner, status, lease_expires_at);

-- MariaDB CHECK additions have no IF NOT EXISTS syntax; use the 021 metadata guard pattern.
SET @runtime_ddl = IF(EXISTS (
    SELECT 1 FROM information_schema.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'chat_runs'
      AND CONSTRAINT_NAME = 'ck_chat_runs_workload_class'
), 'DO 0', 'ALTER TABLE chat_runs ADD CONSTRAINT ck_chat_runs_workload_class CHECK (workload_class IN (''online_text'',''online_media'',''batch'',''realtime''))');
PREPARE runtime_stmt FROM @runtime_ddl;
EXECUTE runtime_stmt;
DEALLOCATE PREPARE runtime_stmt;

SET @runtime_ddl = IF(EXISTS (
    SELECT 1 FROM information_schema.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'chat_runtime_pools'
      AND CONSTRAINT_NAME = 'ck_runtime_pool_workload_class'
), 'DO 0', 'ALTER TABLE chat_runtime_pools ADD CONSTRAINT ck_runtime_pool_workload_class CHECK (workload_class IN (''online_text'',''online_media'',''batch''))');
PREPARE runtime_stmt FROM @runtime_ddl;
EXECUTE runtime_stmt;
DEALLOCATE PREPARE runtime_stmt;

SET @runtime_ddl = IF(EXISTS (
    SELECT 1 FROM information_schema.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'chat_worker_registrations'
      AND CONSTRAINT_NAME = 'ck_worker_registration_auxiliary_active'
), 'DO 0', 'ALTER TABLE chat_worker_registrations ADD CONSTRAINT ck_worker_registration_auxiliary_active CHECK (auxiliary_active >= 0)');
PREPARE runtime_stmt FROM @runtime_ddl;
EXECUTE runtime_stmt;
DEALLOCATE PREPARE runtime_stmt;

SET @runtime_ddl = IF(EXISTS (
    SELECT 1 FROM information_schema.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'chat_worker_registrations'
      AND CONSTRAINT_NAME = 'ck_worker_registration_workload_classes'
), 'DO 0', 'ALTER TABLE chat_worker_registrations ADD CONSTRAINT ck_worker_registration_workload_classes CHECK (JSON_TYPE(workload_classes) = ''ARRAY'' AND JSON_LENGTH(workload_classes) > 0 AND JSON_DEPTH(workload_classes) = 2 AND JSON_CONTAINS(''["online_text","online_media","batch"]'', workload_classes) = 1)');
PREPARE runtime_stmt FROM @runtime_ddl;
EXECUTE runtime_stmt;
DEALLOCATE PREPARE runtime_stmt;
