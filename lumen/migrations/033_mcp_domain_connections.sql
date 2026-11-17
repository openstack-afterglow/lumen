-- 032 is reserved by the independent Drover sandbox change.
ALTER TABLE chat_mcp_servers
    ADD COLUMN owner_domain_id VARCHAR(64) NULL,
    ADD COLUMN connection_scope_key VARCHAR(200) NOT NULL DEFAULT 'global',
    ADD COLUMN implicit_selection BOOLEAN NOT NULL DEFAULT TRUE;

UPDATE chat_mcp_servers
SET connection_scope_key = CONCAT('user:', owner_user_id, ':', owner_project_id)
WHERE scope = 'user';

ALTER TABLE chat_mcp_servers
    DROP INDEX uq_chat_mcp_template_label,
    ADD UNIQUE KEY uq_chat_mcp_scoped_template_label (connection_scope_key, template_key, connection_label),
    ADD INDEX idx_chat_mcp_domain (owner_domain_id, scope);
