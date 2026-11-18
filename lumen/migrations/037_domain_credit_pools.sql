CREATE TABLE domain_credit_pools (
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

CREATE TABLE domain_credit_ledger (
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

ALTER TABLE chat_usage_logs
    MODIFY project_id VARCHAR(64) NULL,
    MODIFY user_id VARCHAR(64) NULL,
    ADD domain_id VARCHAR(64) NULL,
    ADD billing_scope VARCHAR(10) NOT NULL DEFAULT 'user',
    ADD payer_user_id VARCHAR(64) NULL,
    ADD payer_domain_id VARCHAR(64) NULL,
    ADD INDEX idx_chat_usage_domain_created (domain_id, created_at),
    ADD INDEX idx_chat_usage_payer_domain_created (payer_domain_id, created_at),
    ADD INDEX idx_chat_usage_payer_user_created (payer_user_id, created_at);

ALTER TABLE chat_runs ADD payer_domain_id VARCHAR(64) NULL;
ALTER TABLE chat_model_call_reservations
    ADD payer_user_id VARCHAR(64) NULL,
    ADD payer_domain_id VARCHAR(64) NULL,
    ADD INDEX idx_model_call_payer_domain (payer_domain_id, status);
ALTER TABLE chat_stateless_call_reservations
    MODIFY project_id VARCHAR(64) NULL,
    MODIFY user_id VARCHAR(64) NULL,
    ADD payer_user_id VARCHAR(64) NULL,
    ADD payer_domain_id VARCHAR(64) NULL,
    ADD INDEX idx_stateless_hold_payer_domain (payer_domain_id, status);
ALTER TABLE chat_agent_reservations
    ADD payer_user_id VARCHAR(64) NULL,
    ADD payer_domain_id VARCHAR(64) NULL,
    ADD INDEX idx_agent_reservation_payer_domain (payer_domain_id, kind, status);

ALTER TABLE chat_batches ADD payer_domain_id VARCHAR(64) NULL;
