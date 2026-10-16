-- Kubernetes sandbox scope is separate from OpenStack credentials/projects.
-- Existing Nova/Zun rows and writers retain their original values. New Kubernetes
-- pools must not be activated until every controller/worker uses this release.
ALTER TABLE chat_runtime_pools
    MODIFY COLUMN cloud_profile_id VARCHAR(190) NULL,
    MODIFY COLUMN project_id VARCHAR(64) NULL,
    MODIFY COLUMN region_name VARCHAR(190) NULL,
    ADD COLUMN IF NOT EXISTS cluster_profile_id VARCHAR(64) NULL,
    ADD COLUMN IF NOT EXISTS namespace VARCHAR(63) NULL;

-- MariaDB CHECK replacement; drop existing constraint before adding the wider domain.
SET @k8s_ddl = IF(EXISTS (
    SELECT 1 FROM information_schema.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'chat_runtime_pools'
      AND CONSTRAINT_NAME = 'ck_runtime_pool_backend'
), 'ALTER TABLE chat_runtime_pools DROP CONSTRAINT ck_runtime_pool_backend', 'DO 0');
PREPARE k8s_stmt FROM @k8s_ddl;
EXECUTE k8s_stmt;
DEALLOCATE PREPARE k8s_stmt;

SET @k8s_ddl = IF(EXISTS (
    SELECT 1 FROM information_schema.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'chat_runtime_pools'
      AND CONSTRAINT_NAME = 'ck_runtime_pool_backend'
), 'DO 0', 'ALTER TABLE chat_runtime_pools ADD CONSTRAINT ck_runtime_pool_backend CHECK (backend IN (''nova'',''zun'',''kubernetes''))');
PREPARE k8s_stmt FROM @k8s_ddl;
EXECUTE k8s_stmt;
DEALLOCATE PREPARE k8s_stmt;

ALTER TABLE chat_runtime_resources
    MODIFY COLUMN cloud_profile_id VARCHAR(190) NULL,
    MODIFY COLUMN cloud_project_id VARCHAR(64) NULL,
    ADD COLUMN IF NOT EXISTS cluster_profile_id VARCHAR(64) NULL,
    ADD COLUMN IF NOT EXISTS namespace VARCHAR(63) NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_runtime_resource_cluster_provider
    ON chat_runtime_resources (cluster_profile_id, namespace, provider_id);
