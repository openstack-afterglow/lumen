-- Content-free quota holds. Older writers ignore this additive table.
CREATE TABLE chat_stateless_call_reservations (
    event_id CHAR(36) NOT NULL PRIMARY KEY,
    user_id VARCHAR(64) NOT NULL,
    project_id VARCHAR(64) NOT NULL,
    api_key_id BIGINT NULL,
    bound_credits DECIMAL(18,8) NOT NULL,
    actual_credits DECIMAL(18,8) NULL,
    status VARCHAR(20) NOT NULL,
    created_at DATETIME(6) NOT NULL,
    settled_at DATETIME(6) NULL,
    INDEX idx_stateless_hold_owner (user_id, status),
    INDEX idx_stateless_hold_key (api_key_id, status),
    CONSTRAINT ck_stateless_bound CHECK (bound_credits >= 0),
    CONSTRAINT ck_stateless_actual CHECK (actual_credits IS NULL OR actual_credits >= 0)
) ENGINE=InnoDB;
