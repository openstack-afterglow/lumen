-- Global publication is catalogue availability, never owner execution consent.
-- Intentionally no backfill: all existing global sources require explicit approval.
CREATE TABLE IF NOT EXISTS chat_mcp_activations (
    mcp_server_id BIGINT NOT NULL,
    owner_user_id VARCHAR(64) NOT NULL,
    owner_project_id VARCHAR(64) NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT FALSE,
    config_version BIGINT NOT NULL,
    generation BIGINT NOT NULL DEFAULT 1,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (mcp_server_id, owner_user_id, owner_project_id),
    INDEX idx_chat_mcp_activation_owner (owner_user_id, owner_project_id),
    CONSTRAINT fk_chat_mcp_activation_server FOREIGN KEY (mcp_server_id)
        REFERENCES chat_mcp_servers (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
