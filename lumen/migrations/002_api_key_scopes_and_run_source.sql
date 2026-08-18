-- API key least-privilege scopes and immutable durable-run provenance.
-- Deploy with API and worker stopped; this migration is not rolling-upgrade safe.

ALTER TABLE chat_api_keys ADD COLUMN scopes JSON NULL;

UPDATE chat_api_keys
SET scopes = JSON_ARRAY('models:read', 'compat:completions:write');

ALTER TABLE chat_api_keys MODIFY COLUMN scopes JSON NOT NULL;

ALTER TABLE chat_runs
    ADD COLUMN source VARCHAR(10) NULL,
    ADD COLUMN api_key_id BIGINT NULL;

UPDATE chat_runs SET source = 'web' WHERE source IS NULL;

ALTER TABLE chat_runs MODIFY COLUMN source VARCHAR(10) NOT NULL;

CREATE INDEX idx_chat_usage_api_key_id ON chat_usage_logs (api_key_id, id);
