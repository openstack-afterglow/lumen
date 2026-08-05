"""Unit test for Lumen deployment config loading and environment overrides."""

import os
from unittest.mock import patch

from pathlib import Path
import pytest

from lumen.config import Settings, _load_toml, get_settings, load_raw_toml


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
chat_sandbox_api_key = "sandbox-api-key"
chat_api_hosts = "api.lumen.example.com"
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
    assert raw["chat_sandbox_api_key"] == "sandbox-api-key"
    assert raw["chat_api_hosts"] == "api.lumen.example.com"

    settings = get_settings()
    assert settings.keystone_auth_url == "http://10.0.0.10:5000/v3"
    assert settings.keystone_admin_username == "lumen"
    assert settings.database_url == "mysql+aiomysql://lumen:test-db-pass@mariadb:3306/lumen"
    assert settings.redis_url == "redis://:test-redis-pass@valkey:6379/8"
    assert settings.lumen_encryption_key == "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    assert settings.chat_checkpointer_postgres_url == "postgresql://checkpointer:pass@postgres:5432/checkpointer_db"
    assert settings.chat_memory_pgvector_url == "postgresql://pgvector:pass@postgres:5432/pgvector_db"
    assert settings.chat_sandbox_api_key == "sandbox-api-key"
    assert settings.chat_api_hosts == "api.lumen.example.com"


def test_lumen_environment_variable_overrides(monkeypatch):
    """Test environment variable overrides hitting exact Settings fields."""
    monkeypatch.setenv("LUMEN_CONFIG_FILE", "/nonexistent/path/lumen.conf")
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
    monkeypatch.setenv("CHAT_SANDBOX_API_KEY", "env-sandbox-key")
    monkeypatch.setenv("CHAT_API_HOSTS", "env.lumen.example.com")
    monkeypatch.setenv("MCP_CONTROL_PLANE_URL", "https://afterglow.internal")
    monkeypatch.setenv("LUMEN_MCP_SERVICE_TOKEN", "lumen-bridge-secret")

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
    assert settings.chat_sandbox_api_key == "env-sandbox-key"
    assert settings.chat_api_hosts == "env.lumen.example.com"
    assert settings.mcp_control_plane_url == "https://afterglow.internal"
    assert settings.lumen_mcp_service_token == "lumen-bridge-secret"


def test_lumen_empty_environment_value_falls_back_to_toml(monkeypatch):
    monkeypatch.setattr("lumen.config._load_toml", lambda: {"keystone_auth_url": "https://keystone.example.test/v3"})

    with patch.dict(os.environ, {"KEYSTONE_AUTH_URL": ""}, clear=True):
        get_settings.cache_clear()
        try:
            assert get_settings().keystone_auth_url == "https://keystone.example.test/v3"
        finally:
            get_settings.cache_clear()


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
