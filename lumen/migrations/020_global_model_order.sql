-- Global model display order shared by administrator and public discovery.
ALTER TABLE llm_models
    ADD COLUMN IF NOT EXISTS sort_order BIGINT NULL AFTER display_name;

UPDATE llm_models SET sort_order = id WHERE sort_order IS NULL;

ALTER TABLE llm_models
    MODIFY COLUMN sort_order BIGINT NOT NULL;

CREATE INDEX IF NOT EXISTS idx_llm_models_sort_order ON llm_models (sort_order, id);
