from __future__ import annotations

from lumen.scripts.migrate import MIGRATIONS, _statements, load_manifest


def test_api_key_scope_and_run_provenance_migration_is_immutable_and_complete():
    migration = next(item for item in load_manifest() if item.logical_id == "002-api-key-scopes-and-run-source")
    statements = _statements(MIGRATIONS / migration.relative_path)

    assert "ALTER TABLE chat_api_keys ADD COLUMN scopes JSON NULL" in statements
    assert "UPDATE chat_api_keys\nSET scopes = JSON_ARRAY('models:read', 'compat:completions:write')" in statements
    assert "ALTER TABLE chat_api_keys MODIFY COLUMN scopes JSON NOT NULL" in statements
    assert any("ADD COLUMN source VARCHAR(10) NULL" in statement for statement in statements)
    assert any("ADD COLUMN api_key_id BIGINT NULL" in statement for statement in statements)
    assert "UPDATE chat_runs SET source = 'web' WHERE source IS NULL" in statements
    assert "ALTER TABLE chat_runs MODIFY COLUMN source VARCHAR(10) NOT NULL" in statements
    assert "CREATE INDEX idx_chat_usage_api_key_id ON chat_usage_logs (api_key_id, id)" in statements


def test_provider_environment_credential_migration_is_registered():
    migration = next(item for item in load_manifest() if item.logical_id == "003-provider-api-key-env")
    statements = _statements(MIGRATIONS / migration.relative_path)

    assert statements == ["ALTER TABLE llm_providers ADD COLUMN api_key_env VARCHAR(128) NULL"]


def test_api_key_monthly_credit_limits_migration_is_registered():
    migration = next(item for item in load_manifest() if item.logical_id == "004-api-key-monthly-credit-limits")
    statements = _statements(MIGRATIONS / migration.relative_path)

    assert any("ADD COLUMN owner_monthly_credit_limit NUMERIC(18, 8) NULL" in statement for statement in statements)
    assert any("ADD COLUMN admin_monthly_credit_limit NUMERIC(18, 8) NULL" in statement for statement in statements)
    assert any("chk_chat_api_keys_owner_monthly_credit_limit" in statement for statement in statements)
    assert any("chk_chat_api_keys_admin_monthly_credit_limit" in statement for statement in statements)
    assert any(
        "CREATE INDEX idx_chat_usage_api_key_created ON chat_usage_logs (api_key_id, created_at)" in statement
        for statement in statements
    )


def test_context_lifecycle_migration_adds_wire_provenance_and_checkpoint_reuse_fields():
    migration = next(item for item in load_manifest() if item.logical_id == "005-chat-context-lifecycle")
    statements = _statements(MIGRATIONS / migration.relative_path)

    assert any("ADD COLUMN run_kind VARCHAR(20) NOT NULL DEFAULT 'completion'" in statement for statement in statements)
    assert any("ADD COLUMN title_source VARCHAR(20) NOT NULL DEFAULT 'legacy'" in statement for statement in statements)
    assert any("ADD COLUMN title_status VARCHAR(20) NOT NULL DEFAULT 'idle'" in statement for statement in statements)
    assert any("ADD COLUMN title_revision BIGINT NOT NULL DEFAULT 0" in statement for statement in statements)
    checkpoint = statements[2]
    for column in (
        "temp_thread_id CHAR(36) NULL",
        "source_message_ids JSON NULL",
        "source_message_count INT NOT NULL DEFAULT 0",
        "previous_checkpoint_id CHAR(36) NULL",
        "context_metadata JSON NULL",
    ):
        assert f"ADD COLUMN {column}" in checkpoint
    assert any("ON DELETE CASCADE" in statement and "chat_temp_threads" in statement for statement in statements)
    assert any(
        "ON DELETE SET NULL" in statement and "chat_context_checkpoints" in statement for statement in statements
    )
    assert any("idx_chat_context_checkpoints_temp_created" in statement for statement in statements)


def test_usage_run_association_migration_allows_multiple_summary_segments():
    migration = next(item for item in load_manifest() if item.logical_id == "006-chat-usage-run-association")
    statements = _statements(MIGRATIONS / migration.relative_path)

    assert statements == [
        "ALTER TABLE chat_usage_logs\n    DROP INDEX run_id",
        "CREATE INDEX idx_chat_usage_run_id\n    ON chat_usage_logs (run_id)",
    ]


def test_asset_project_bucket_migration_is_registered_and_additive():
    migration = next(item for item in load_manifest() if item.logical_id == "007-chat-asset-project-buckets")
    statements = _statements(MIGRATIONS / migration.relative_path)

    assert statements == ["ALTER TABLE chat_assets\n    ADD COLUMN bucket_name VARCHAR(63) NULL AFTER object_key"]
def test_provider_subscription_migration_is_registered_and_additive():
    migration = next(item for item in load_manifest() if item.logical_id == "008-provider-subscriptions")
    statements = _statements(MIGRATIONS / migration.relative_path)

    provider_alter = statements[0]
    for column in (
        "auth_mode VARCHAR(32) NOT NULL DEFAULT 'api_key'",
        "encrypted_subscription_tokens TEXT NULL",
        "subscription_status VARCHAR(24) NOT NULL DEFAULT 'disconnected'",
        "subscription_expires_at DATETIME NULL",
        "subscription_generation BIGINT NOT NULL DEFAULT 0",
    ):
        assert f"ADD COLUMN {column}" in provider_alter
    auth_attempts = statements[1]
    assert "CREATE TABLE llm_provider_auth_attempts" in auth_attempts
    assert "PRIMARY KEY (provider_id)" in auth_attempts
    assert "UNIQUE (id)" in auth_attempts
    assert "FOREIGN KEY (provider_id) REFERENCES llm_providers(id) ON DELETE CASCADE" in auth_attempts


def test_weekly_quota_migration_is_registered():
    migration = next(
        item
        for item in load_manifest()
        if item.logical_id == "009-weekly-quotas-and-key-limits"
    )
    statements = _statements(MIGRATIONS / migration.relative_path)

    assert any(
        "ADD COLUMN max_quota_weekly NUMERIC(18, 8) NOT NULL DEFAULT 0"
        in statement
        for statement in statements
    )
    assert any(
        "ADD COLUMN owner_weekly_credit_limit NUMERIC(18, 8) NULL"
        in statement
        for statement in statements
    )
    assert any(
        "chk_chat_api_keys_owner_weekly_credit_limit" in statement
        for statement in statements
    )
