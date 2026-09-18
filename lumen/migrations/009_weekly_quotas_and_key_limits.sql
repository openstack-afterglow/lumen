-- Weekly quota for user wallets and owner weekly credit limit for API keys.
-- Deploy with API running; schema changes are additive.

ALTER TABLE user_wallets
    ADD COLUMN max_quota_weekly NUMERIC(18, 8) NOT NULL DEFAULT 0;

ALTER TABLE chat_api_keys
    ADD COLUMN owner_weekly_credit_limit NUMERIC(18, 8) NULL,
    ADD CONSTRAINT chk_chat_api_keys_owner_weekly_credit_limit CHECK (owner_weekly_credit_limit IS NULL OR owner_weekly_credit_limit > 0);
