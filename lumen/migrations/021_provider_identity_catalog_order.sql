-- Backfill the public qualifier without changing execution transport or legacy duplicates.
ALTER TABLE llm_providers
    ADD COLUMN IF NOT EXISTS api_provider VARCHAR(40) NULL,
    ADD COLUMN IF NOT EXISTS sort_order INT NOT NULL DEFAULT 0;

UPDATE llm_providers SET api_provider = provider_type WHERE api_provider IS NULL;

ALTER TABLE llm_providers MODIFY COLUMN api_provider VARCHAR(40) NOT NULL;

ALTER TABLE llm_models ADD COLUMN IF NOT EXISTS sort_order INT NOT NULL DEFAULT 0;

-- MariaDB CHECK additions have no IF NOT EXISTS syntax; inspect metadata on retry.
SET @catalog_ddl = IF(EXISTS (
    SELECT 1 FROM information_schema.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'llm_providers'
      AND CONSTRAINT_NAME = 'chk_llm_providers_sort_order'
), 'DO 0', 'ALTER TABLE llm_providers ADD CONSTRAINT chk_llm_providers_sort_order CHECK (sort_order >= 0)');
PREPARE catalog_stmt FROM @catalog_ddl;
EXECUTE catalog_stmt;
DEALLOCATE PREPARE catalog_stmt;

SET @catalog_ddl = IF(EXISTS (
    SELECT 1 FROM information_schema.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'llm_models'
      AND CONSTRAINT_NAME = 'chk_llm_models_sort_order'
), 'DO 0', 'ALTER TABLE llm_models ADD CONSTRAINT chk_llm_models_sort_order CHECK (sort_order >= 0)');
PREPARE catalog_stmt FROM @catalog_ddl;
EXECUTE catalog_stmt;
DEALLOCATE PREPARE catalog_stmt;
