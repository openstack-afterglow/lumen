-- Retain project provenance and NULL as an origin-project-only fallback.
ALTER TABLE chat_message_graphs
    ADD COLUMN IF NOT EXISTS domain_id VARCHAR(64),
    ADD INDEX IF NOT EXISTS idx_chat_message_graphs_domain (domain_id, user_id);
ALTER TABLE chat_conversations
    ADD COLUMN IF NOT EXISTS domain_id VARCHAR(64),
    ADD INDEX IF NOT EXISTS idx_chat_conversations_domain (domain_id, user_id, updated_at);
ALTER TABLE chat_agents
    ADD COLUMN IF NOT EXISTS domain_id VARCHAR(64),
    ADD INDEX IF NOT EXISTS idx_chat_agents_domain (domain_id, owner_user_id, updated_at);
ALTER TABLE chat_custom_tools
    ADD COLUMN IF NOT EXISTS domain_id VARCHAR(64),
    ADD COLUMN IF NOT EXISTS owner_domain_id VARCHAR(64),
    ADD COLUMN IF NOT EXISTS legacy_domain_group CHAR(64),
    ADD INDEX IF NOT EXISTS idx_chat_tool_user_domain (domain_id, owner_user_id, updated_at),
    ADD INDEX IF NOT EXISTS idx_chat_tool_domain (owner_domain_id, scope);
SET @tool_domain_owner_check = IF(EXISTS (
    SELECT 1 FROM information_schema.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'chat_custom_tools'
      AND CONSTRAINT_NAME = 'ck_chat_tool_domain_owner'
), 'DO 0', 'ALTER TABLE chat_custom_tools ADD CONSTRAINT ck_chat_tool_domain_owner CHECK (scope <> ''domain'' OR owner_domain_id IS NOT NULL)');
PREPARE tool_domain_owner_check FROM @tool_domain_owner_check;
EXECUTE tool_domain_owner_check;
DEALLOCATE PREPARE tool_domain_owner_check;
ALTER TABLE chat_mcp_servers
    ADD COLUMN IF NOT EXISTS legacy_domain_group CHAR(64);
ALTER TABLE chat_mcp_activations
    ADD COLUMN IF NOT EXISTS owner_domain_id VARCHAR(64),
    ADD UNIQUE INDEX IF NOT EXISTS uq_chat_mcp_activation_domain (mcp_server_id, owner_user_id, owner_domain_id),
    ADD INDEX IF NOT EXISTS idx_chat_mcp_activation_domain (owner_domain_id, owner_user_id);
ALTER TABLE chat_mcp_oauth_connections
    ADD COLUMN IF NOT EXISTS owner_domain_id VARCHAR(64),
    ADD UNIQUE INDEX IF NOT EXISTS uq_chat_mcp_oauth_domain (mcp_server_id, owner_user_id, owner_domain_id),
    ADD INDEX IF NOT EXISTS idx_chat_mcp_oauth_domain (owner_domain_id, owner_user_id);
ALTER TABLE chat_mcp_oauth_requests
    ADD COLUMN IF NOT EXISTS owner_domain_id VARCHAR(64);

CREATE TABLE IF NOT EXISTS chat_extension_archives (
    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    kind VARCHAR(10) NOT NULL,
    entry_type VARCHAR(20) NOT NULL DEFAULT 'extension',
    original_id BIGINT NOT NULL,
    canonical_id BIGINT NOT NULL,
    owner_user_id VARCHAR(64),
    domain_id VARCHAR(64) NOT NULL,
    name VARCHAR(100) NOT NULL,
    encrypted_snapshot MEDIUMTEXT NOT NULL,
    archived_at DATETIME NOT NULL,
    restored_at DATETIME,
    KEY idx_chat_extension_archive_original (kind, original_id, entry_type),
    KEY idx_chat_extension_archive_owner (domain_id, owner_user_id, restored_at)
) ENGINE=InnoDB;
