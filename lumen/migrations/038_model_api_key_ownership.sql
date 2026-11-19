-- Legacy keys become personal by default. Preserve their original project as
-- history, and leave Claude Gateway's separate credential binding intact.
ALTER TABLE chat_api_keys
    MODIFY COLUMN owner_user_id VARCHAR(64) NULL,
    MODIFY COLUMN owner_project_id VARCHAR(64) NULL,
    ADD COLUMN IF NOT EXISTS key_kind VARCHAR(16) NOT NULL DEFAULT 'personal',
    ADD COLUMN IF NOT EXISTS owner_domain_id VARCHAR(64) NULL,
    ADD COLUMN IF NOT EXISTS created_by_user_id VARCHAR(64) NULL,
    ADD CONSTRAINT IF NOT EXISTS chk_chat_api_keys_owner_kind CHECK (
        (key_kind = 'personal' AND owner_user_id IS NOT NULL AND owner_domain_id IS NULL)
        OR (key_kind = 'service' AND credential_kind = 'api_key' AND owner_domain_id IS NOT NULL
            AND owner_user_id IS NULL AND owner_project_id IS NULL AND created_by_user_id IS NOT NULL)
    ),
    ADD INDEX IF NOT EXISTS idx_chat_api_keys_kind_user (key_kind, owner_user_id),
    ADD INDEX IF NOT EXISTS idx_chat_api_keys_kind_domain (key_kind, owner_domain_id);
