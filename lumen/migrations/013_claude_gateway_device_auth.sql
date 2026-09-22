ALTER TABLE chat_api_keys
    ADD COLUMN expires_at DATETIME(6) NULL,
    ADD COLUMN credential_kind VARCHAR(32) NOT NULL DEFAULT 'api_key',
    ADD CONSTRAINT chk_chat_api_keys_credential_kind
        CHECK (credential_kind IN ('api_key', 'claude_gateway'));

CREATE TABLE chat_gateway_device_grants (
    id CHAR(36) NOT NULL,
    device_code_hash CHAR(64) NOT NULL,
    user_code_hash CHAR(64) NOT NULL,
    client_id_hash CHAR(64) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    created_at DATETIME(6) NOT NULL,
    expires_at DATETIME(6) NOT NULL,
    next_poll_at DATETIME(6) NOT NULL,
    poll_interval_seconds INT NOT NULL,
    owner_user_id VARCHAR(64) NULL,
    owner_project_id VARCHAR(64) NULL,
    issued_api_key_id BIGINT NULL,
    approved_at DATETIME(6) NULL,
    denied_at DATETIME(6) NULL,
    consumed_at DATETIME(6) NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_chat_gateway_device_code_hash UNIQUE (device_code_hash),
    CONSTRAINT uq_chat_gateway_user_code_hash UNIQUE (user_code_hash),
    CONSTRAINT fk_chat_gateway_issued_api_key FOREIGN KEY (issued_api_key_id)
        REFERENCES chat_api_keys (id) ON DELETE SET NULL,
    CONSTRAINT chk_chat_gateway_device_status
        CHECK (status IN ('pending', 'approved', 'denied', 'consumed')),
    CONSTRAINT chk_chat_gateway_device_poll_interval
        CHECK (poll_interval_seconds BETWEEN 1 AND 60),
    INDEX idx_chat_gateway_device_expiry (status, expires_at),
    INDEX idx_chat_gateway_device_owner (owner_project_id, owner_user_id, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
