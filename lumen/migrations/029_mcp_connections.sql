-- One template (registry entry or bundle) may have several global connections, each with its own credentials,
-- permissions and label. The tool catalog is metadata: refreshing it never bumps config_version, so an
-- OAuth connection is not revoked by discovery.
ALTER TABLE chat_mcp_servers
    ADD COLUMN IF NOT EXISTS template_key VARCHAR(100) NULL,
    ADD COLUMN IF NOT EXISTS connection_label VARCHAR(100) NULL,
    ADD COLUMN IF NOT EXISTS tool_catalog JSON NULL,
    ADD COLUMN IF NOT EXISTS catalog_refreshed_at DATETIME NULL,
    ADD COLUMN IF NOT EXISTS catalog_error VARCHAR(300) NULL;

ALTER TABLE chat_mcp_servers
    ADD UNIQUE INDEX IF NOT EXISTS uq_chat_mcp_template_label (template_key, connection_label);
