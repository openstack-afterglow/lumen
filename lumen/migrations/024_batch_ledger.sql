-- Persisted native/OpenAI batches, private files, ordered items and project dispatch queues.
-- Request/result MEDIUMTEXT columns contain encrypted serialized bodies, never plaintext.
-- JSON stores metadata and capability/pricing/scope snapshots; errors must be sanitized.
-- API-key IDs retain provenance without FKs. File/asset/run pins use RESTRICT.
-- Inherit the database charset/collation so CHAR(36) references match existing UUIDs.
-- No chat_runs -> chat_batches FK is needed: routing owns that column/index without a cycle.

CREATE TABLE IF NOT EXISTS chat_batch_files (
    id CHAR(36) NOT NULL PRIMARY KEY,
    project_id VARCHAR(64) NOT NULL,
    user_id VARCHAR(64) NOT NULL,
    api_key_id BIGINT NULL,
    purpose VARCHAR(20) NOT NULL,
    filename VARCHAR(255) NOT NULL,
    mime_type VARCHAR(127) NOT NULL,
    size_bytes BIGINT NOT NULL DEFAULT 0,
    sha256 CHAR(64) NULL,
    bucket_name VARCHAR(63) NOT NULL,
    object_key VARCHAR(255) NOT NULL,
    state VARCHAR(20) NOT NULL DEFAULT 'uploading',
    scan_result VARCHAR(20) NULL,
    multipart_upload_id VARCHAR(1024) NULL,
    upload_epoch BIGINT NOT NULL DEFAULT 0,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    expires_at DATETIME(6) NOT NULL,
    deleted_at DATETIME(6) NULL,
    KEY idx_chat_batch_files_owner_cursor (project_id, user_id, created_at, id),
    KEY idx_chat_batch_files_expiry_state (expires_at, state),
    UNIQUE KEY uq_chat_batch_files_object_key (object_key),
    CONSTRAINT ck_chat_batch_files_purpose CHECK (purpose IN ('batch','batch_output')),
    CONSTRAINT ck_chat_batch_files_state CHECK (state IN ('uploading','processed','error','deleting','deleted')),
    CONSTRAINT ck_chat_batch_files_counters CHECK (size_bytes >= 0 AND upload_epoch >= 0)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS chat_batches (
    id CHAR(36) NOT NULL PRIMARY KEY,
    project_id VARCHAR(64) NOT NULL,
    user_id VARCHAR(64) NOT NULL,
    api_key_id BIGINT NULL,
    contract VARCHAR(10) NOT NULL,
    endpoint VARCHAR(64) NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'validating',
    final_target_status VARCHAR(20) NULL,
    metadata JSON NOT NULL DEFAULT ('{}'),
    idempotency_key_hash CHAR(64) NULL,
    request_fingerprint CHAR(64) NOT NULL,
    input_file_id CHAR(36) NULL,
    output_file_id CHAR(36) NULL,
    error_file_id CHAR(36) NULL,
    output_expires_after_seconds INT NULL,
    request_total INT NOT NULL DEFAULT 0,
    request_completed INT NOT NULL DEFAULT 0,
    -- Includes cancelled/expired/unknown; their native detail counters are subsets.
    request_failed INT NOT NULL DEFAULT 0,
    request_pending INT NOT NULL DEFAULT 0,
    request_queued INT NOT NULL DEFAULT 0,
    request_running INT NOT NULL DEFAULT 0,
    request_cancelled INT NOT NULL DEFAULT 0,
    request_expired INT NOT NULL DEFAULT 0,
    request_unknown INT NOT NULL DEFAULT 0,
    validation_errors JSON NOT NULL DEFAULT ('[]'),
    validation_byte_cursor BIGINT NOT NULL DEFAULT 0,
    validation_ordinal_cursor INT NOT NULL DEFAULT 0,
    lease_owner VARCHAR(190) NULL,
    lease_fence BIGINT NOT NULL DEFAULT 0,
    lease_expires_at DATETIME(6) NULL,
    finalization_epoch BIGINT NOT NULL DEFAULT 0,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    expires_at DATETIME(6) NOT NULL DEFAULT (created_at + INTERVAL 24 HOUR),
    in_progress_at DATETIME(6) NULL,
    finalizing_at DATETIME(6) NULL,
    completed_at DATETIME(6) NULL,
    failed_at DATETIME(6) NULL,
    cancelling_at DATETIME(6) NULL,
    cancelled_at DATETIME(6) NULL,
    expired_at DATETIME(6) NULL,
    UNIQUE KEY uq_chat_batches_idempotency (project_id, user_id, contract, idempotency_key_hash),
    KEY idx_chat_batches_owner_cursor (project_id, user_id, created_at, id),
    KEY idx_chat_batches_status_deadline (status, expires_at),
    KEY idx_chat_batches_input_file (input_file_id),
    KEY idx_chat_batches_output_file (output_file_id),
    KEY idx_chat_batches_error_file (error_file_id),
    CONSTRAINT fk_chat_batches_input_file FOREIGN KEY (input_file_id) REFERENCES chat_batch_files (id) ON DELETE RESTRICT,
    CONSTRAINT fk_chat_batches_output_file FOREIGN KEY (output_file_id) REFERENCES chat_batch_files (id) ON DELETE RESTRICT,
    CONSTRAINT fk_chat_batches_error_file FOREIGN KEY (error_file_id) REFERENCES chat_batch_files (id) ON DELETE RESTRICT,
    CONSTRAINT ck_chat_batches_contract CHECK (contract IN ('native','openai')),
    CONSTRAINT ck_chat_batches_native_idempotency CHECK (contract <> 'native' OR idempotency_key_hash IS NOT NULL),
    CONSTRAINT ck_chat_batches_status CHECK (
        status IN ('validating','in_progress','finalizing','completed','failed','cancelling','cancelled','expired')
    ),
    CONSTRAINT ck_chat_batches_final_target CHECK (
        final_target_status IS NULL OR final_target_status IN ('completed','failed','cancelled','expired')
    ),
    CONSTRAINT ck_chat_batches_request_counters CHECK (
        request_total >= 0 AND request_completed >= 0 AND request_failed >= 0 AND request_pending >= 0
        AND request_queued >= 0 AND request_running >= 0 AND request_cancelled >= 0
        AND request_expired >= 0 AND request_unknown >= 0
    ),
    CONSTRAINT ck_chat_batches_progress_counters CHECK (
        validation_byte_cursor >= 0 AND validation_ordinal_cursor >= 0 AND lease_fence >= 0 AND finalization_epoch >= 0
    ),
    CONSTRAINT ck_chat_batches_output_expiry CHECK (
        output_expires_after_seconds IS NULL OR output_expires_after_seconds BETWEEN 3600 AND 2592000
    )
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS chat_batch_items (
    batch_id CHAR(36) NOT NULL,
    ordinal INT NOT NULL,
    custom_id VARCHAR(64) NOT NULL,
    -- SHA-256 of the exact UTF-8 custom_id bytes, not a case-insensitive text identity.
    custom_id_hash CHAR(64) NOT NULL,
    operation VARCHAR(30) NOT NULL,
    request_ciphertext MEDIUMTEXT NOT NULL,
    result_ciphertext MEDIUMTEXT NULL,
    -- Frozen during validation; NULL while a pending item remains unvalidated.
    capability_snapshot JSON NULL,
    pricing_snapshot JSON NULL,
    required_scopes JSON NULL,
    input_asset_id CHAR(36) NULL,
    run_id CHAR(36) NULL,
    state VARCHAR(20) NOT NULL DEFAULT 'pending',
    error_code VARCHAR(100) NULL,
    error_message VARCHAR(1000) NULL,
    http_status INT NULL,
    request_id VARCHAR(190) NULL,
    -- Projection only: the existing run/hold ledger remains the settlement authority.
    settlement_status VARCHAR(20) NOT NULL DEFAULT 'pending',
    PRIMARY KEY (batch_id, ordinal),
    UNIQUE KEY uq_chat_batch_items_custom_hash (batch_id, custom_id_hash),
    UNIQUE KEY uq_chat_batch_items_run (run_id),
    KEY idx_chat_batch_items_state_ordinal (batch_id, state, ordinal),
    KEY idx_chat_batch_items_input_asset (input_asset_id),
    CONSTRAINT fk_chat_batch_items_batch FOREIGN KEY (batch_id) REFERENCES chat_batches (id) ON DELETE RESTRICT,
    CONSTRAINT fk_chat_batch_items_input_asset FOREIGN KEY (input_asset_id) REFERENCES chat_assets (id) ON DELETE RESTRICT,
    CONSTRAINT fk_chat_batch_items_run FOREIGN KEY (run_id) REFERENCES chat_runs (id) ON DELETE RESTRICT,
    CONSTRAINT ck_chat_batch_items_ordinal CHECK (ordinal >= 1),
    CONSTRAINT ck_chat_batch_items_custom_id CHECK (CHAR_LENGTH(custom_id) BETWEEN 1 AND 64),
    CONSTRAINT ck_chat_batch_items_operation CHECK (
        operation IN ('chat.completions','responses','images.generations','images.edits','audio.speech','audio.transcriptions')
    ),
    CONSTRAINT ck_chat_batch_items_state CHECK (
        state IN ('pending','queued','running','completed','failed','cancelled','expired','unknown')
    ),
    CONSTRAINT ck_chat_batch_items_http_status CHECK (http_status IS NULL OR http_status BETWEEN 100 AND 599)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS chat_batch_project_queues (
    project_id VARCHAR(64) NOT NULL PRIMARY KEY,
    -- Fair-dispatch cursor only, not a provenance pin or agent quota reference.
    last_batch_id CHAR(36) NULL,
    lease_owner VARCHAR(190) NULL,
    lease_fence BIGINT NOT NULL DEFAULT 0,
    lease_expires_at DATETIME(6) NULL,
    CONSTRAINT ck_chat_batch_project_queues_fence CHECK (lease_fence >= 0)
) ENGINE=InnoDB;
