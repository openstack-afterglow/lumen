-- Keep existing chat models text-only; media pricing is independent of token prices.
ALTER TABLE llm_models
    ADD COLUMN model_kind VARCHAR(16) NOT NULL DEFAULT 'text',
    ADD COLUMN media_pricing JSON NULL;
