CREATE TABLE IF NOT EXISTS chat_plugin_bindings (
    id CHAR(36) NOT NULL PRIMARY KEY,
    kind VARCHAR(10) NOT NULL,
    plugin_id VARCHAR(190) NOT NULL,
    export_key VARCHAR(128) NOT NULL,
    name VARCHAR(190) NOT NULL,
    scope VARCHAR(10) NOT NULL,
    owner_user_id VARCHAR(64) NULL,
    owner_project_id VARCHAR(64) NULL,
    encrypted_config MEDIUMTEXT NOT NULL,
    config_version BIGINT NOT NULL DEFAULT 1,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    CONSTRAINT ck_plugin_binding_kind CHECK (kind IN ('tool', 'skill')),
    CONSTRAINT ck_plugin_binding_owner CHECK (
        (scope = 'global' AND owner_user_id IS NULL AND owner_project_id IS NULL) OR
        (scope = 'user' AND owner_user_id IS NOT NULL AND owner_project_id IS NOT NULL)
    ),
    INDEX idx_plugin_binding_catalog (scope, owner_user_id, owner_project_id, is_active)
) ENGINE=InnoDB;

ALTER TABLE chat_agents
    ADD COLUMN IF NOT EXISTS plugin_tool_ids JSON NULL,
    ADD COLUMN IF NOT EXISTS plugin_skill_ids JSON NULL;
