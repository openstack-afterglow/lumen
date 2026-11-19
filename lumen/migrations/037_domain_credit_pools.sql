-- Every statement is re-runnable: MariaDB commits each DDL on its own, so a failure part-way leaves
-- earlier statements applied and the runner replays the whole file on the next attempt.
CREATE TABLE IF NOT EXISTS domain_credit_pools (
    domain_id VARCHAR(64) NOT NULL PRIMARY KEY,
    balance_credits DECIMAL(18,8) NOT NULL DEFAULT 0,
    reserved_credits DECIMAL(18,8) NOT NULL DEFAULT 0,
    monthly_used_credits DECIMAL(18,8) NOT NULL DEFAULT 0,
    period_start DATE NOT NULL,
    manager_monthly_limit_credits DECIMAL(18,8) NULL,
    admin_ceiling_credits DECIMAL(18,8) NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at DATETIME(6) NOT NULL,
    CONSTRAINT ck_domain_pool_balance CHECK (balance_credits >= 0),
    CONSTRAINT ck_domain_pool_reserved CHECK (reserved_credits >= 0),
    CONSTRAINT ck_domain_pool_month CHECK (monthly_used_credits >= 0),
    CONSTRAINT ck_domain_pool_limit CHECK (manager_monthly_limit_credits >= 0),
    CONSTRAINT ck_domain_pool_ceiling CHECK (admin_ceiling_credits >= 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS domain_credit_ledger (
    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    domain_id VARCHAR(64) NOT NULL,
    kind VARCHAR(20) NOT NULL,
    amount_credits DECIMAL(18,8) NOT NULL,
    balance_after DECIMAL(18,8) NOT NULL,
    actor_user_id VARCHAR(64) NULL,
    reference VARCHAR(190) NOT NULL,
    note TEXT NULL,
    created_at DATETIME(6) NOT NULL,
    CONSTRAINT ck_domain_ledger_kind CHECK (kind IN ('topup','adjustment','charge','refund')),
    CONSTRAINT uq_domain_ledger_reference UNIQUE (kind, reference),
    INDEX idx_domain_ledger_domain_id (domain_id, id),
    INDEX idx_domain_ledger_actor_charge (actor_user_id, kind, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

-- One ALTER per table so each table is rebuilt at most once; re-applying MODIFY ... NULL is a no-op.
ALTER TABLE chat_usage_logs
    MODIFY COLUMN project_id VARCHAR(64) NULL,
    MODIFY COLUMN user_id VARCHAR(64) NULL,
    ADD COLUMN IF NOT EXISTS domain_id VARCHAR(64) NULL,
    ADD COLUMN IF NOT EXISTS billing_scope VARCHAR(10) NOT NULL DEFAULT 'user',
    ADD COLUMN IF NOT EXISTS payer_user_id VARCHAR(64) NULL,
    ADD COLUMN IF NOT EXISTS payer_domain_id VARCHAR(64) NULL,
    ADD INDEX IF NOT EXISTS idx_chat_usage_domain_created (domain_id, created_at),
    ADD INDEX IF NOT EXISTS idx_chat_usage_payer_domain_created (payer_domain_id, created_at),
    ADD INDEX IF NOT EXISTS idx_chat_usage_payer_user_created (payer_user_id, created_at);

ALTER TABLE chat_runs ADD COLUMN IF NOT EXISTS payer_domain_id VARCHAR(64) NULL;
ALTER TABLE chat_model_call_reservations
    ADD COLUMN IF NOT EXISTS payer_user_id VARCHAR(64) NULL,
    ADD COLUMN IF NOT EXISTS payer_domain_id VARCHAR(64) NULL,
    ADD INDEX IF NOT EXISTS idx_model_call_payer_domain (payer_domain_id, status);
ALTER TABLE chat_stateless_call_reservations
    MODIFY COLUMN project_id VARCHAR(64) NULL,
    MODIFY COLUMN user_id VARCHAR(64) NULL,
    ADD COLUMN IF NOT EXISTS payer_user_id VARCHAR(64) NULL,
    ADD COLUMN IF NOT EXISTS payer_domain_id VARCHAR(64) NULL,
    ADD INDEX IF NOT EXISTS idx_stateless_hold_payer_domain (payer_domain_id, status);
ALTER TABLE chat_agent_reservations
    ADD COLUMN IF NOT EXISTS payer_user_id VARCHAR(64) NULL,
    ADD COLUMN IF NOT EXISTS payer_domain_id VARCHAR(64) NULL,
    ADD INDEX IF NOT EXISTS idx_agent_reservation_payer_domain (payer_domain_id, kind, status);

ALTER TABLE chat_batches ADD COLUMN IF NOT EXISTS payer_domain_id VARCHAR(64) NULL;
