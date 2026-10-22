-- Coordinated account-runtime promotion: drain all legacy and account writers first.
-- The shared-DB compatibility repair restored provider-level secrets and the legacy
-- auth-attempt primary key without changing the applied027 ledger identity.
-- Preserve both credential stores: legacy writes may have diverged from the account
-- snapshots. Operators must reconcile that material before promotion, not replay027
-- (which clears provider-level credentials) or overwrite either store here.
-- Only legacy attempts without a credential identity inherit the original default.
UPDATE llm_provider_auth_attempts AS attempt
JOIN llm_provider_accounts AS account
    ON account.provider_id = attempt.provider_id AND BINARY account.name = BINARY 'default'
JOIN llm_provider_credentials AS credential
    ON credential.account_id = account.id AND BINARY credential.name = BINARY 'default'
SET attempt.credential_id = credential.id
WHERE attempt.credential_id IS NULL;

-- Fail explicitly even when sql_mode is non-strict: MODIFY NOT NULL alone may
-- coerce an unresolved NULL to zero. Never guess another credential or delete an
-- attempt. Both foreign keys remain intact.
SET @unresolved_attempt_ddl = IF(EXISTS (
    SELECT 1 FROM llm_provider_auth_attempts WHERE credential_id IS NULL
), 'SIGNAL SQLSTATE ''45000'' SET MESSAGE_TEXT = ''Unresolved provider auth attempt: stop writers and reconcile default credential ownership''', 'DO 0');
PREPARE unresolved_attempt_statement FROM @unresolved_attempt_ddl;
EXECUTE unresolved_attempt_statement;
DEALLOCATE PREPARE unresolved_attempt_statement;
ALTER TABLE llm_provider_auth_attempts
    ADD INDEX IF NOT EXISTS idx_auth_attempt_provider (provider_id),
    MODIFY COLUMN credential_id BIGINT NOT NULL;
SET @reconciled_attempt_pk = (SELECT COUNT(*) FROM information_schema.KEY_COLUMN_USAGE
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'llm_provider_auth_attempts'
    AND CONSTRAINT_NAME = 'PRIMARY' AND COLUMN_NAME = 'credential_id');
SET @reconciled_attempt_ddl = IF(@reconciled_attempt_pk = 0,
    'ALTER TABLE llm_provider_auth_attempts DROP PRIMARY KEY, ADD PRIMARY KEY (credential_id)',
    'DO 0');
PREPARE reconciled_attempt_statement FROM @reconciled_attempt_ddl;
EXECUTE reconciled_attempt_statement;
DEALLOCATE PREPARE reconciled_attempt_statement;
