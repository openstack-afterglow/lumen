-- API key owner monthly credit limit and administrator ceiling.
-- Deploy with API running; schema changes are additive.

ALTER TABLE chat_api_keys
    ADD COLUMN owner_monthly_credit_limit NUMERIC(18, 8) NULL,
    ADD COLUMN admin_monthly_credit_limit NUMERIC(18, 8) NULL,
    ADD CONSTRAINT chk_chat_api_keys_owner_monthly_credit_limit CHECK (owner_monthly_credit_limit IS NULL OR owner_monthly_credit_limit > 0),
    ADD CONSTRAINT chk_chat_api_keys_admin_monthly_credit_limit CHECK (admin_monthly_credit_limit IS NULL OR admin_monthly_credit_limit > 0);

CREATE INDEX idx_chat_usage_api_key_created ON chat_usage_logs (api_key_id, created_at);
