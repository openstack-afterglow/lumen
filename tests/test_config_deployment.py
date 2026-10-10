"""Unit test for Lumen deployment config loading and environment overrides."""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from lumen.config import Settings, _config_candidates, _load_toml, get_settings, load_raw_toml


def test_lumen_toml_config_loading(monkeypatch, tmp_path):
    """Test loading Kolla-rendered-equivalent TOML configuration."""
    config_file = tmp_path / "lumen.conf"
    toml_content = """# Lumen TOML Configuration File
[keystone]
keystone_auth_url = "http://10.0.0.10:5000/v3"
keystone_admin_username = "lumen"
keystone_admin_password = "test-keystone-password"
keystone_admin_project = "lumen-service"
keystone_domain = "Default"
keystone_region_name = "RegionOne"
keystone_interface = "internal"

[database]
database_url = "mysql+aiomysql://lumen:test-db-pass@mariadb:3306/lumen"
database_pool_size = 20
database_max_overflow = 10

[cache]
redis_url = "redis://:test-redis-pass@valkey:6379/8"

[lumen]
lumen_encryption_key = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"

[chat]
chat_checkpointer_postgres_url = "postgresql://checkpointer:pass@postgres:5432/checkpointer_db"
chat_memory_pgvector_url = "postgresql://pgvector:pass@postgres:5432/pgvector_db"
chat_asset_s3_endpoint = "https://s3.example.com"
chat_asset_s3_bucket = "lumen-assets"
chat_asset_s3_access_key = "s3-access-key"
chat_asset_s3_secret_key = "s3-secret-key"
chat_asset_s3_region = "default"
chat_asset_s3_server_side_encryption = "none"
chat_asset_s3_kms_key_id = ""
chat_sandbox_api_key = "sandbox-api-key"
chat_api_hosts = "api.lumen.example.com"
chat_default_model = "gpt-4.1-mini"
chat_compat_run_timeout_seconds = 600
claude_gateway_base_url = "https://api.lumen.example.com/v1/claude-gateway"
claude_gateway_model = "claude-sonnet-4-6"
claude_gateway_provider = "anthropic"
"""
    config_file.write_text(toml_content, encoding="utf-8")

    monkeypatch.setenv("LUMEN_CONFIG_FILE", str(config_file))
    load_raw_toml.cache_clear()
    get_settings.cache_clear()

    raw = _load_toml()
    for field in raw:
        monkeypatch.delenv(field.upper(), raising=False)
    get_settings.cache_clear()

    assert raw["keystone_auth_url"] == "http://10.0.0.10:5000/v3"
    assert raw["keystone_admin_username"] == "lumen"
    assert raw["database_url"] == "mysql+aiomysql://lumen:test-db-pass@mariadb:3306/lumen"
    assert raw["redis_url"] == "redis://:test-redis-pass@valkey:6379/8"
    assert raw["lumen_encryption_key"] == "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    assert raw["chat_checkpointer_postgres_url"] == "postgresql://checkpointer:pass@postgres:5432/checkpointer_db"
    assert raw["chat_memory_pgvector_url"] == "postgresql://pgvector:pass@postgres:5432/pgvector_db"
    assert raw["chat_asset_s3_region"] == "default"
    assert raw["chat_asset_s3_server_side_encryption"] == "none"
    assert raw["chat_sandbox_api_key"] == "sandbox-api-key"
    assert raw["chat_api_hosts"] == "api.lumen.example.com"
    assert raw["chat_default_model"] == "gpt-4.1-mini"
    assert raw["chat_compat_run_timeout_seconds"] == 600
    assert raw["claude_gateway_base_url"] == "https://api.lumen.example.com/v1/claude-gateway"
    assert raw["claude_gateway_model"] == "claude-sonnet-4-6"
    assert raw["claude_gateway_provider"] == "anthropic"

    settings = get_settings()
    assert settings.keystone_auth_url == "http://10.0.0.10:5000/v3"
    assert settings.keystone_admin_username == "lumen"
    assert settings.database_url == "mysql+aiomysql://lumen:test-db-pass@mariadb:3306/lumen"
    assert settings.redis_url == "redis://:test-redis-pass@valkey:6379/8"
    assert settings.lumen_encryption_key == "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    assert settings.chat_checkpointer_postgres_url == "postgresql://checkpointer:pass@postgres:5432/checkpointer_db"
    assert settings.chat_memory_pgvector_url == "postgresql://pgvector:pass@postgres:5432/pgvector_db"
    assert settings.chat_asset_s3_region == "default"
    assert settings.chat_asset_s3_server_side_encryption == "none"
    assert settings.chat_sandbox_api_key == "sandbox-api-key"
    assert settings.chat_api_hosts == "api.lumen.example.com"
    assert settings.chat_default_model == "gpt-4.1-mini"
    assert settings.chat_compat_run_timeout_seconds == 600
    assert settings.claude_gateway_base_url == "https://api.lumen.example.com/v1/claude-gateway"
    assert settings.claude_gateway_model == "claude-sonnet-4-6"
    assert settings.claude_gateway_provider == "anthropic"


def test_lumen_environment_variable_overrides(monkeypatch, tmp_path):
    """Test environment variable overrides hitting exact Settings fields."""
    config_file = tmp_path / "lumen.conf"
    config_file.write_text('[lumen]\nservice_chat_enabled = true\n')
    monkeypatch.setenv("LUMEN_CONFIG_FILE", str(config_file))
    monkeypatch.setenv("KEYSTONE_AUTH_URL", "http://env-keystone:5000/v3")
    monkeypatch.setenv("KEYSTONE_ADMIN_USERNAME", "env-user")
    monkeypatch.setenv("KEYSTONE_ADMIN_PASSWORD", "env-pass")
    monkeypatch.setenv("KEYSTONE_ADMIN_PROJECT", "env-project")
    monkeypatch.setenv("DATABASE_URL", "mysql+aiomysql://env:env@env-db:3306/env")
    monkeypatch.setenv("REDIS_URL", "redis://:env-redis@env-valkey:6379/8")
    monkeypatch.setenv("LUMEN_ENCRYPTION_KEY", "fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210")
    monkeypatch.setenv("CHAT_CHECKPOINTER_POSTGRES_URL", "postgresql://env:env@env-pg:5432/env_cp")
    monkeypatch.setenv("CHAT_MEMORY_PGVECTOR_URL", "postgresql://env:env@env-pg:5432/env_pv")
    monkeypatch.setenv("CHAT_ASSET_S3_ACCESS_KEY", "env-s3-ak")
    monkeypatch.setenv("CHAT_ASSET_S3_SECRET_KEY", "env-s3-sk")
    monkeypatch.setenv("CHAT_ASSET_S3_REGION", "ceph-region")
    monkeypatch.setenv("CHAT_ASSET_S3_SERVER_SIDE_ENCRYPTION", "aws:kms")
    monkeypatch.setenv("CHAT_ASSET_S3_KMS_KEY_ID", "asset-key")
    monkeypatch.setenv("CHAT_SANDBOX_API_KEY", "env-sandbox-key")
    monkeypatch.setenv("CHAT_API_HOSTS", "env.lumen.example.com")
    monkeypatch.setenv("MCP_CONTROL_PLANE_URL", "https://afterglow.internal")
    monkeypatch.setenv("LUMEN_MCP_SERVICE_TOKEN", "lumen-bridge-secret")

    monkeypatch.setenv("CLAUDE_GATEWAY_BASE_URL", "https://env.lumen.example.com/v1/claude-gateway")
    monkeypatch.setenv("CLAUDE_GATEWAY_MODEL", "claude-opus-4-6")
    monkeypatch.setenv("CLAUDE_GATEWAY_PROVIDER", "anthropic")
    load_raw_toml.cache_clear()
    get_settings.cache_clear()

    settings = get_settings()
    assert settings.keystone_auth_url == "http://env-keystone:5000/v3"
    assert settings.keystone_admin_username == "env-user"
    assert settings.keystone_admin_password == "env-pass"
    assert settings.keystone_admin_project == "env-project"
    assert settings.database_url == "mysql+aiomysql://env:env@env-db:3306/env"
    assert settings.redis_url == "redis://:env-redis@env-valkey:6379/8"
    assert settings.lumen_encryption_key == "fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210"
    assert settings.chat_checkpointer_postgres_url == "postgresql://env:env@env-pg:5432/env_cp"
    assert settings.chat_memory_pgvector_url == "postgresql://env:env@env-pg:5432/env_pv"
    assert settings.chat_asset_s3_access_key == "env-s3-ak"
    assert settings.chat_asset_s3_secret_key == "env-s3-sk"
    assert settings.chat_asset_s3_region == "ceph-region"
    assert settings.chat_asset_s3_server_side_encryption == "aws:kms"
    assert settings.chat_asset_s3_kms_key_id == "asset-key"
    assert settings.chat_sandbox_api_key == "env-sandbox-key"
    assert settings.chat_api_hosts == "env.lumen.example.com"
    assert settings.mcp_control_plane_url == "https://afterglow.internal"
    assert settings.lumen_mcp_service_token == "lumen-bridge-secret"
    assert settings.claude_gateway_base_url == "https://env.lumen.example.com/v1/claude-gateway"
    assert settings.claude_gateway_model == "claude-opus-4-6"
    assert settings.claude_gateway_provider == "anthropic"


def test_lumen_empty_environment_value_falls_back_to_toml(monkeypatch):
    monkeypatch.setattr("lumen.config._load_toml", lambda: {"keystone_auth_url": "https://keystone.example.test/v3"})

    with patch.dict(os.environ, {"KEYSTONE_AUTH_URL": ""}, clear=True):
        get_settings.cache_clear()
        try:
            assert get_settings().keystone_auth_url == "https://keystone.example.test/v3"
        finally:
            get_settings.cache_clear()


def test_claude_gateway_base_url_rejects_wrong_path_and_insecure_public_origin():
    with pytest.raises(ValueError, match="origin plus /v1/claude-gateway"):
        Settings(claude_gateway_base_url="https://lumen.example/v1")
    with pytest.raises(ValueError, match="HTTPS"):
        Settings(claude_gateway_base_url="http://lumen.example/v1/claude-gateway")
    assert (
        Settings(claude_gateway_base_url="http://127.0.0.1:8012/v1/claude-gateway").claude_gateway_base_url
        == "http://127.0.0.1:8012/v1/claude-gateway"
    )


def test_lumen_maps_afterglow_openstack_section(monkeypatch):
    monkeypatch.setattr(
        "lumen.config.load_raw_toml",
        lambda: {
            "openstack": {
                "auth_url": "https://keystone.example.test/v3",
                "username": "lumen",
                "project_name": "lumen-service",
                "region_name": "RegionTwo",
                "insecure": True,
                "cacert": "/etc/ssl/certs/keystone-ca.pem",
            }
        },
    )

    settings = _load_toml()

    assert settings["keystone_auth_url"] == "https://keystone.example.test/v3"
    assert settings["keystone_admin_username"] == "lumen"
    assert settings["keystone_admin_project"] == "lumen-service"
    assert settings["keystone_region_name"] == "RegionTwo"
    assert settings["insecure"] is True
    assert settings["os_cacert"] == "/etc/ssl/certs/keystone-ca.pem"


def test_lumen_config_candidates_only_lumen_paths(monkeypatch):
    monkeypatch.delenv("LUMEN_CONFIG_FILE", raising=False)
    candidates = _config_candidates()
    assert candidates == [Path("/etc/lumen/lumen.conf"), Path("lumen.conf")]

    monkeypatch.setenv("LUMEN_CONFIG_FILE", "/custom/lumen.conf")
    candidates_custom = _config_candidates()
    assert candidates_custom == [Path("/custom/lumen.conf")]


@pytest.mark.parametrize("content", [None, "", "# empty deployment\n", "[lumen\n"])
def test_explicit_config_never_falls_back(monkeypatch, tmp_path, content):
    config = tmp_path / "explicit.conf"
    if content is not None:
        config.write_text(content)
    monkeypatch.setenv("LUMEN_CONFIG_FILE", str(config))
    load_raw_toml.cache_clear()
    try:
        with pytest.raises((OSError, ValueError)):
            load_raw_toml()
    finally:
        load_raw_toml.cache_clear()


def test_get_lumen_encryption_key_strict_validation():
    s_valid = Settings(lumen_encryption_key="a" * 64)
    assert s_valid.get_lumen_encryption_key == "a" * 64

    s_empty = Settings(lumen_encryption_key="")
    with pytest.raises(ValueError, match=r"Lumen encryption key is not set \(lumen_encryption_key\)"):
        _ = s_empty.get_lumen_encryption_key

    s_short = Settings(lumen_encryption_key="a" * 63)
    with pytest.raises(ValueError, match=r"Lumen encryption key must be exactly 64 hex characters"):
        _ = s_short.get_lumen_encryption_key

    s_non_hex = Settings(lumen_encryption_key="z" * 64)
    with pytest.raises(ValueError, match=r"Lumen encryption key must be exactly 64 hex characters"):
        _ = s_non_hex.get_lumen_encryption_key


def test_chat_compat_run_timeout_seconds_bounds_validation():
    """Test chat_compat_run_timeout_seconds validator rejects outside 1..3600."""
    assert Settings(chat_compat_run_timeout_seconds=1).chat_compat_run_timeout_seconds == 1
    assert Settings(chat_compat_run_timeout_seconds=3600).chat_compat_run_timeout_seconds == 3600
    assert Settings(chat_compat_run_timeout_seconds=300).chat_compat_run_timeout_seconds == 300

    with pytest.raises(ValueError, match="must be between 1 and 3600"):
        Settings(chat_compat_run_timeout_seconds=0)

    with pytest.raises(ValueError, match="must be between 1 and 3600"):
        Settings(chat_compat_run_timeout_seconds=-10)

    with pytest.raises(ValueError, match="must be between 1 and 3600"):
        Settings(chat_compat_run_timeout_seconds=3601)


def test_fixed_worker_classes_preserve_online_default_and_isolate_batch():
    with patch.dict(os.environ, {}, clear=True):
        settings = Settings()
        assert settings.runtime_config.enabled is False
        assert settings.worker_workload_classes == ["online_text", "online_media"]
        assert Settings(worker_workload_classes=["batch"]).worker_workload_classes == ["batch"]
        assert Settings(worker_workload_classes=["online_media"]).worker_workload_classes == ["online_media"]
        for classes in ([], ["realtime"], ["online_text", "online_text"], ["batch", "online_text"]):
            with pytest.raises(ValueError):
                Settings(worker_workload_classes=classes)


def test_batch_policy_defaults_match_bounded_validation_and_dispatch_contract():
    with patch.dict(os.environ, {}, clear=True):
        settings = Settings()
        assert settings.batch_enabled is False
        assert (settings.batch_dispatch_window, settings.batch_project_dispatch_window) == (8, 32)
        assert (settings.batch_native_max_items, settings.batch_native_max_bytes) == (1000, 10 * 1024 * 1024)
        assert (settings.batch_jsonl_max_rows, settings.batch_jsonl_max_bytes, settings.batch_jsonl_max_line_bytes) == (
            50000, 200000000, 4 * 1024 * 1024,
        )
        assert (settings.batch_validation_chunk_rows, settings.batch_validation_chunk_bytes) == (100, 1024 * 1024)
        assert settings.batch_upload_slots == 4
        assert (settings.batch_result_ttl_days, settings.batch_input_ttl_days) == (7, 30)
        assert settings.batch_cancel_grace_seconds == 600
        assert settings.api_max_websocket_connections == 64
        assert settings.api_max_body_bytes > settings.batch_jsonl_max_bytes
        assert Settings(batch_enabled=True, worker_workload_classes=["batch"]).batch_enabled is True


@pytest.mark.parametrize("policy", [
    {"api_max_active_requests": 0}, {"api_max_sse_connections": 0},
    {"api_max_websocket_connections": 0}, {"api_max_body_bytes": 0},
    {"api_max_websocket_connections": float("inf")},
    {"batch_native_max_items": 1001}, {"batch_native_max_bytes": 10 * 1024 * 1024 + 1},
    {"batch_jsonl_max_rows": 50001}, {"batch_jsonl_max_bytes": 200000001},
    {"batch_jsonl_max_line_bytes": 4 * 1024 * 1024 + 1},
    {"batch_validation_chunk_rows": 101}, {"batch_validation_chunk_bytes": 1024 * 1024 + 1},
    {"batch_dispatch_window": 0}, {"batch_project_dispatch_window": 0},
    {"batch_dispatch_window": 33}, {"batch_upload_slots": 0},
    {"batch_result_ttl_days": 31}, {"batch_input_ttl_days": 0},
    {"batch_cancel_grace_seconds": 601},
    {"batch_jsonl_max_bytes": 1},
    {"batch_enabled": True, "api_max_body_bytes": 199999999},
])
def test_online_and_batch_settings_reject_invalid_finite_bounds(policy):
    with patch.dict(os.environ, {}, clear=True), pytest.raises(ValueError):
        Settings(**policy)


def test_runtime_and_batch_environment_fields_use_public_settings_names():
    with patch.dict(os.environ, {
        "WORKER_WORKLOAD_CLASSES": '["batch"]', "BATCH_ENABLED": "true",
        "BATCH_DISPATCH_WINDOW": "4", "BATCH_PROJECT_DISPATCH_WINDOW": "16",
        "API_MAX_WEBSOCKET_CONNECTIONS": "32", "RUNTIME_CONFIG": '{"enabled":false}',
    }, clear=True):
        settings = Settings()
        assert settings.worker_workload_classes == ["batch"]
        assert settings.batch_enabled is True
        assert settings.runtime_config.enabled is False
        assert (settings.batch_dispatch_window, settings.batch_project_dispatch_window) == (4, 16)
        assert settings.api_max_websocket_connections == 32


def test_shipped_toml_example_preserves_disabled_runtime_and_settings_sections(monkeypatch):
    import tomllib

    example = Path(__file__).resolve().parents[1] / "lumen.conf.example"
    with example.open("rb") as handle:
        parsed = tomllib.load(handle)
    monkeypatch.setattr("lumen.config.load_raw_toml", lambda: parsed)
    with patch.dict(os.environ, {}, clear=True):
        settings = Settings(**_load_toml())
    assert settings.worker_workload_classes == ["online_text", "online_media"]
    assert settings.batch_jsonl_max_line_bytes == 4 * 1024 * 1024
    assert settings.runtime_config.enabled is False
    assert settings.runtime_config.pools == ()
    assert settings.runtime_config.guest_profiles == ()
    assert settings.runtime_config.renewal.renew_interval_seconds == 1200
    assert settings.runtime_config.renewal.overlap_seconds == 120
