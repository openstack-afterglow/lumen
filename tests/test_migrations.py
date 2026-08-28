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
    assert any("CREATE INDEX idx_chat_usage_api_key_created ON chat_usage_logs (api_key_id, created_at)" in statement for statement in statements)
