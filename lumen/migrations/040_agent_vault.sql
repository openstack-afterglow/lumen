-- Vault IDs above 2^40 are allocated for new notes; imported chat memories retain their vector IDs.
CREATE TABLE IF NOT EXISTS memory_vault_notes (
    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    user_id VARCHAR(64) NOT NULL,
    domain_id VARCHAR(64) NULL,
    project_key VARCHAR(80) NULL,
    scope_key CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    kind VARCHAR(24) NOT NULL,
    role VARCHAR(24) NULL,
    slug VARCHAR(128) NOT NULL,
    title VARCHAR(200) NOT NULL,
    body_ciphertext MEDIUMTEXT NOT NULL,
    body_hash CHAR(64) NOT NULL,
    metadata JSON NOT NULL,
    status VARCHAR(20) NOT NULL,
    revision INT NOT NULL,
    supersedes_id BIGINT NULL,
    project_id VARCHAR(64) NULL,
    workspace_id BIGINT NULL,
    category VARCHAR(32) NULL,
    confidence DECIMAL(5,4) NULL,
    expires_at DATETIME(6) NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    UNIQUE KEY uq_memory_vault_note_scope (scope_key),
    KEY idx_memory_vault_notes_scope (user_id, domain_id, status, kind, updated_at),
    CONSTRAINT fk_memory_vault_supersedes FOREIGN KEY (supersedes_id) REFERENCES memory_vault_notes(id),
    CONSTRAINT ck_memory_vault_project_domain CHECK (project_key IS NULL OR domain_id IS NOT NULL),
    CONSTRAINT ck_memory_vault_workspace_owner CHECK (workspace_id IS NULL OR (project_id IS NOT NULL AND domain_id IS NOT NULL))
) ENGINE=InnoDB AUTO_INCREMENT=1099511627776 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS memory_vault_revisions (
    note_id BIGINT NOT NULL,
    revision INT NOT NULL,
    body_ciphertext MEDIUMTEXT NOT NULL,
    body_hash CHAR(64) NOT NULL,
    title VARCHAR(200) NOT NULL,
    metadata JSON NOT NULL,
    status VARCHAR(20) NOT NULL,
    change_kind VARCHAR(20) NOT NULL,
    created_at DATETIME(6) NOT NULL,
    PRIMARY KEY (note_id, revision),
    CONSTRAINT fk_memory_vault_revision_note FOREIGN KEY (note_id) REFERENCES memory_vault_notes(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS memory_vault_projects (
    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    user_id VARCHAR(64) NOT NULL,
    domain_id VARCHAR(64) NOT NULL,
    project_key VARCHAR(80) NOT NULL,
    title VARCHAR(200) NOT NULL,
    metadata JSON NOT NULL,
    status VARCHAR(20) NOT NULL,
    created_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    UNIQUE KEY uq_memory_vault_project (user_id, domain_id, project_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS memory_vault_reviews (
    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    user_id VARCHAR(64) NOT NULL,
    domain_id VARCHAR(64) NULL,
    project_key VARCHAR(80) NULL,
    note_id BIGINT NOT NULL,
    status VARCHAR(20) NOT NULL,
    expected_hash CHAR(64) NOT NULL,
    assessment_ciphertext MEDIUMTEXT NULL,
    ready_at DATETIME(6) NULL,
    resolved_at DATETIME(6) NULL,
    created_at DATETIME(6) NOT NULL,
    KEY idx_memory_vault_review_scope (user_id, domain_id, project_key, status, ready_at),
    CONSTRAINT fk_memory_vault_review_note FOREIGN KEY (note_id) REFERENCES memory_vault_notes(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS memory_vault_feedback (
    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    review_id BIGINT NOT NULL,
    source_hash CHAR(64) NOT NULL,
    trigger_kind VARCHAR(32) NOT NULL,
    body_ciphertext MEDIUMTEXT NOT NULL,
    created_at DATETIME(6) NOT NULL,
    UNIQUE KEY uq_memory_vault_feedback (review_id, source_hash),
    CONSTRAINT fk_memory_vault_feedback_review FOREIGN KEY (review_id) REFERENCES memory_vault_reviews(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS memory_vault_tasks (
    scope_key CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL PRIMARY KEY,
    user_id VARCHAR(64) NOT NULL,
    domain_id VARCHAR(64) NULL,
    project_key VARCHAR(80) NULL,
    task_id VARCHAR(128) NOT NULL,
    parent_scope_key CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NULL,
    resume_scope_key CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NULL,
    status VARCHAR(20) NOT NULL,
    week_key VARCHAR(12) NOT NULL,
    timezone VARCHAR(64) NOT NULL,
    started_at DATETIME(6) NOT NULL,
    finished_at DATETIME(6) NULL,
    KEY idx_memory_vault_task_scope (user_id, domain_id, project_key, status),
    FOREIGN KEY (parent_scope_key) REFERENCES memory_vault_tasks(scope_key),
    FOREIGN KEY (resume_scope_key) REFERENCES memory_vault_tasks(scope_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS memory_vault_activity (
    scope_key CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL PRIMARY KEY,
    user_id VARCHAR(64) NOT NULL,
    domain_id VARCHAR(64) NULL,
    project_key VARCHAR(80) NULL,
    role VARCHAR(24) NULL,
    read_count BIGINT NOT NULL DEFAULT 0,
    write_count BIGINT NOT NULL DEFAULT 0,
    updated_at DATETIME(6) NOT NULL,
    KEY idx_memory_vault_activity_scope (user_id, domain_id, project_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS memory_vault_weekly_exports (
    scope_key CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL PRIMARY KEY,
    user_id VARCHAR(64) NOT NULL,
    domain_id VARCHAR(64) NULL,
    project_key VARCHAR(80) NULL,
    week_key VARCHAR(12) NOT NULL,
    content_hash CHAR(64) NOT NULL,
    exported_at DATETIME(6) NOT NULL,
    KEY idx_memory_vault_week_scope (user_id, domain_id, project_key, week_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

ALTER TABLE chat_memories
    ADD COLUMN IF NOT EXISTS vault_migration_status VARCHAR(16) NULL,
    ADD COLUMN IF NOT EXISTS vault_migration_retry_at DATETIME(6) NULL,
    ADD INDEX IF NOT EXISTS idx_chat_memories_vault_pending (vault_migration_status, vault_migration_retry_at, id);

ALTER TABLE chat_memory_outbox
    MODIFY COLUMN memory_id BIGINT NULL,
    ADD COLUMN IF NOT EXISTS vault_note_id BIGINT NULL,
    ADD CONSTRAINT fk_chat_memory_outbox_vault FOREIGN KEY IF NOT EXISTS (vault_note_id) REFERENCES memory_vault_notes(id);
