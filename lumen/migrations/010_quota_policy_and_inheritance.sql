-- Mutable monthly default and nullable per-user overrides.
-- Existing wallet values remain explicit overrides; new/reset wallets inherit.

ALTER TABLE user_wallets
    MODIFY COLUMN max_quota_monthly NUMERIC(18, 8) NULL DEFAULT NULL,
    MODIFY COLUMN max_quota_weekly NUMERIC(18, 8) NULL DEFAULT NULL;

CREATE TABLE chat_quota_policies (
    id INTEGER NOT NULL,
    default_monthly_credit_limit NUMERIC(18, 8) NOT NULL,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT chk_chat_quota_policies_singleton CHECK (id = 1),
    CONSTRAINT chk_chat_quota_policies_default_monthly CHECK (default_monthly_credit_limit >= 0)
);
