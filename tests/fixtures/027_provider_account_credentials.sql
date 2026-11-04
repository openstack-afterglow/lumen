-- Coordinated cutover: drain legacy workers and nonterminal runs before applying.
-- Legacy route secrets are deliberately not executable after this migration.
ALTER TABLE llm_providers
    ADD COLUMN IF NOT EXISTS selection_policy VARCHAR(16) NOT NULL DEFAULT 'priority',
    ADD COLUMN IF NOT EXISTS selected_credential_id BIGINT NULL;

CREATE TABLE IF NOT EXISTS llm_provider_accounts (
    id BIGINT NOT NULL AUTO_INCREMENT,
    provider_id BIGINT NOT NULL,
    name VARCHAR(100) NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    priority INT NOT NULL DEFAULT 0,
    weight INT NOT NULL DEFAULT 1,
    allowed_model_ids JSON NULL,
    allowed_capabilities JSON NULL,
    quota_group VARCHAR(100) NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_provider_account_name (provider_id, name),
    CONSTRAINT fk_provider_account_endpoint FOREIGN KEY (provider_id) REFERENCES llm_providers(id) ON DELETE CASCADE,
    CONSTRAINT chk_provider_account_weight CHECK (weight > 0 AND priority >= 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS llm_provider_credentials (
    id BIGINT NOT NULL AUTO_INCREMENT,
    account_id BIGINT NOT NULL,
    name VARCHAR(100) NOT NULL,
    auth_mode VARCHAR(32) NOT NULL,
    encrypted_api_key TEXT NULL,
    api_key_env VARCHAR(128) NULL,
    encrypted_subscription_tokens TEXT NULL,
    subscription_status VARCHAR(24) NOT NULL DEFAULT 'disconnected',
    subscription_expires_at DATETIME NULL,
    expires_at DATETIME NULL,
    cooldown_until DATETIME NULL,
    generation BIGINT NOT NULL DEFAULT 0,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    priority INT NOT NULL DEFAULT 0,
    weight INT NOT NULL DEFAULT 1,
    allowed_model_ids JSON NULL,
    allowed_capabilities JSON NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_provider_credential_name (account_id, name),
    CONSTRAINT fk_provider_credential_account FOREIGN KEY (account_id) REFERENCES llm_provider_accounts(id) ON DELETE CASCADE,
    CONSTRAINT chk_provider_credential_weight CHECK (weight > 0 AND priority >= 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

INSERT INTO llm_provider_accounts (provider_id, name)
SELECT p.id, 'default' FROM llm_providers p
WHERE NOT EXISTS (SELECT 1 FROM llm_provider_accounts a WHERE a.provider_id = p.id);

INSERT INTO llm_provider_credentials (account_id, name, auth_mode, encrypted_api_key, api_key_env,
    encrypted_subscription_tokens, subscription_status, subscription_expires_at, generation)
SELECT a.id, 'default', p.auth_mode, p.encrypted_api_key, p.api_key_env,
    p.encrypted_subscription_tokens, p.subscription_status, p.subscription_expires_at, p.subscription_generation
FROM llm_providers p JOIN llm_provider_accounts a ON a.provider_id = p.id AND a.name = 'default'
WHERE NOT EXISTS (SELECT 1 FROM llm_provider_credentials c WHERE c.account_id = a.id);

ALTER TABLE llm_provider_auth_attempts ADD COLUMN IF NOT EXISTS credential_id BIGINT NULL;
UPDATE llm_provider_auth_attempts t
JOIN llm_provider_accounts a ON a.provider_id = t.provider_id AND a.name = 'default'
JOIN llm_provider_credentials c ON c.account_id = a.id AND c.name = 'default'
SET t.credential_id = c.id WHERE t.credential_id IS NULL;
ALTER TABLE llm_provider_auth_attempts MODIFY COLUMN credential_id BIGINT NOT NULL;
SET @credential_attempt_pk = (SELECT COUNT(*) FROM information_schema.KEY_COLUMN_USAGE
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'llm_provider_auth_attempts'
    AND CONSTRAINT_NAME = 'PRIMARY' AND COLUMN_NAME = 'credential_id');
SET @credential_attempt_ddl = IF(@credential_attempt_pk = 0,
    'ALTER TABLE llm_provider_auth_attempts ADD INDEX IF NOT EXISTS idx_auth_attempt_provider (provider_id), DROP PRIMARY KEY, ADD PRIMARY KEY (credential_id)',
    'SELECT 1');
PREPARE credential_attempt_statement FROM @credential_attempt_ddl;
EXECUTE credential_attempt_statement;
DEALLOCATE PREPARE credential_attempt_statement;
SET @credential_attempt_fk = (SELECT COUNT(*) FROM information_schema.KEY_COLUMN_USAGE
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'llm_provider_auth_attempts'
    AND COLUMN_NAME = 'credential_id' AND REFERENCED_TABLE_NAME = 'llm_provider_credentials'
    AND REFERENCED_COLUMN_NAME = 'id');
SET @credential_attempt_fk_ddl = IF(@credential_attempt_fk = 0,
    'ALTER TABLE llm_provider_auth_attempts ADD CONSTRAINT fk_auth_attempt_credential FOREIGN KEY (credential_id) REFERENCES llm_provider_credentials(id) ON DELETE CASCADE',
    'SELECT 1');
PREPARE credential_attempt_fk_statement FROM @credential_attempt_fk_ddl;
EXECUTE credential_attempt_fk_statement;
DEALLOCATE PREPARE credential_attempt_fk_statement;

-- Retire the original credential storage, including environment references. Billing
-- administrator credentials are intentionally untouched and remain inference-separated.
UPDATE llm_providers SET encrypted_api_key = NULL, api_key_env = NULL,
    encrypted_subscription_tokens = NULL, subscription_status = 'disconnected',
    subscription_expires_at = NULL, subscription_generation = 0;

ALTER TABLE chat_run_providers
    ADD COLUMN IF NOT EXISTS provider_account_id BIGINT NULL,
    ADD COLUMN IF NOT EXISTS provider_credential_id BIGINT NULL,
    ADD COLUMN IF NOT EXISTS provider_credential_generation BIGINT NULL,
    ADD COLUMN IF NOT EXISTS provider_backend VARCHAR(40) NULL,
    ADD COLUMN IF NOT EXISTS provider_backend_version VARCHAR(100) NULL;
ALTER TABLE chat_usage_logs
    ADD COLUMN IF NOT EXISTS provider_id BIGINT NULL,
    ADD COLUMN IF NOT EXISTS provider_account_id BIGINT NULL,
    ADD COLUMN IF NOT EXISTS provider_credential_id BIGINT NULL,
    ADD COLUMN IF NOT EXISTS provider_credential_generation BIGINT NULL,
    ADD COLUMN IF NOT EXISTS provider_backend VARCHAR(40) NULL,
    ADD COLUMN IF NOT EXISTS provider_backend_version VARCHAR(100) NULL,
    ADD INDEX IF NOT EXISTS idx_chat_usage_provider_account (provider_account_id, created_at);

-- One usage event may charge multiple upstream accounts. Keep their allocation
-- separate from the single customer credit debit and preserve deleted identities.
CREATE TABLE IF NOT EXISTS chat_provider_usage (
    event_id VARCHAR(64) NOT NULL,
    provider_account_id BIGINT NOT NULL,
    provider_credential_id BIGINT NOT NULL,
    provider_id BIGINT NOT NULL,
    provider_credential_generation BIGINT NOT NULL,
    provider_backend VARCHAR(40) NULL,
    provider_backend_version VARCHAR(100) NULL,
    prompt_tokens BIGINT NOT NULL DEFAULT 0,
    completion_tokens BIGINT NOT NULL DEFAULT 0,
    raw_cost NUMERIC(20, 10) NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (event_id, provider_account_id, provider_credential_id),
    INDEX idx_provider_usage_account (provider_account_id, created_at),
    CONSTRAINT chk_provider_usage_nonnegative CHECK (raw_cost >= 0 AND prompt_tokens >= 0 AND completion_tokens >= 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
