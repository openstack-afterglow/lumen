-- Prompt-cache pricing on the per-model price record and an Anthropic
-- organization-report shaped token ledger. Cache rates are per token and
-- NULL means the category bills at 0 until an administrator sets a rate.
-- chat_usage_logs.prompt_tokens keeps meaning total input, cache included.
ALTER TABLE llm_models
    ADD COLUMN IF NOT EXISTS cache_read_price NUMERIC(20, 10) NULL AFTER output_price,
    ADD COLUMN IF NOT EXISTS cache_write_price NUMERIC(20, 10) NULL AFTER cache_read_price,
    ADD COLUMN IF NOT EXISTS cache_write_1h_price NUMERIC(20, 10) NULL AFTER cache_write_price;

ALTER TABLE chat_usage_logs
    ADD COLUMN IF NOT EXISTS cache_read_input_tokens INT NOT NULL DEFAULT 0 AFTER completion_tokens,
    ADD COLUMN IF NOT EXISTS cache_creation_5m_input_tokens INT NOT NULL DEFAULT 0 AFTER cache_read_input_tokens,
    ADD COLUMN IF NOT EXISTS cache_creation_1h_input_tokens INT NOT NULL DEFAULT 0 AFTER cache_creation_5m_input_tokens;
