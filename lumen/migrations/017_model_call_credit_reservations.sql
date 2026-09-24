-- Durable credit bounds per model-call segment. Zero-priced calls still get a unique fence.
-- Existing run reserved_credits and project quota credits_reserved carry the counters.
CREATE TABLE IF NOT EXISTS chat_model_call_reservations (
    run_id CHAR(36) NOT NULL,
    segment_id VARCHAR(190) NOT NULL,
    bound_credits DECIMAL(18,8) NOT NULL,
    actual_credits DECIMAL(18,8) NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'reserved',
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    settled_at DATETIME(6) NULL,
    PRIMARY KEY (run_id, segment_id),
    CONSTRAINT fk_model_call_reservation_run FOREIGN KEY (run_id) REFERENCES chat_runs (id) ON DELETE RESTRICT,
    CONSTRAINT ck_model_call_bound CHECK (bound_credits >= 0),
    CONSTRAINT ck_model_call_actual CHECK (actual_credits IS NULL OR actual_credits >= 0)
) ENGINE=InnoDB;
