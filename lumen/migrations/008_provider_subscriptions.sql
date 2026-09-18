-- Add encrypted subscription credentials and bounded provider device-auth attempts.
ALTER TABLE llm_providers
    ADD COLUMN auth_mode VARCHAR(32) NOT NULL DEFAULT 'api_key',
    ADD COLUMN encrypted_subscription_tokens TEXT NULL,
    ADD COLUMN subscription_status VARCHAR(24) NOT NULL DEFAULT 'disconnected',
    ADD COLUMN subscription_expires_at DATETIME NULL,
    ADD COLUMN subscription_generation BIGINT NOT NULL DEFAULT 0;

CREATE TABLE llm_provider_auth_attempts (
    provider_id BIGINT NOT NULL,
    id CHAR(36) NOT NULL,
    initiated_by_user_id VARCHAR(64) NOT NULL,
    initiated_by_project_id VARCHAR(64) NOT NULL,
    provider_generation BIGINT NOT NULL,
    encrypted_payload TEXT NULL,
    status VARCHAR(16) NOT NULL,
    expires_at DATETIME NOT NULL,
    next_poll_at DATETIME NOT NULL,
    created_at DATETIME NOT NULL,
    PRIMARY KEY (provider_id),
    CONSTRAINT uq_llm_provider_auth_attempts_id UNIQUE (id),
    CONSTRAINT fk_llm_provider_auth_attempts_provider
        FOREIGN KEY (provider_id) REFERENCES llm_providers(id) ON DELETE CASCADE
);
