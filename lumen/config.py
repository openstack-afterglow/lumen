"""Lumen configuration loaded from environment or ``lumen.conf``."""

from __future__ import annotations

import os
import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from urllib.parse import urlsplit

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def is_development_loopback_http_url(value: str) -> bool:
    """Return whether a URL is a valid development-only HTTP loopback endpoint."""
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError:
        return False
    return (
        os.environ.get("AFTERGLOW_ENV", "development").strip().lower() == "development"
        and parsed.scheme.lower() == "http"
        and bool(parsed.netloc)
        and (parsed.hostname or "").lower() in _LOOPBACK_HOSTS
        and not parsed.username
        and not parsed.password
        and not parsed.query
        and not parsed.fragment
    )

def _config_candidates() -> list[Path]:
    configured = os.environ.get("LUMEN_CONFIG_FILE", "").strip()
    candidates = [Path(configured)] if configured else []
    candidates.extend(
        [
            Path("/etc/lumen/lumen.conf"),
            Path("lumen.conf"),
            Path("/etc/afterglow/afterglow.conf"),
            Path("afterglow.conf"),
        ]
    )
    return candidates


@lru_cache
def load_raw_toml() -> dict:
    for path in _config_candidates():
        if path.is_file() and path.stat().st_size > 0:
            with path.open("rb") as handle:
                return tomllib.load(handle)
    return {}


def _load_toml() -> dict[str, Any]:
    data = load_raw_toml()
    if not data:
        return {}
    keystone = data.get("keystone", {})
    if not keystone:
        openstack = data.get("openstack", {})
        keystone = {
            "keystone_auth_url": openstack.get("auth_url", ""),
            "keystone_admin_username": openstack.get("username", ""),
            "keystone_admin_password": openstack.get("password", ""),
            "keystone_admin_project": openstack.get("project_name", ""),
            "keystone_domain": openstack.get("user_domain_name", "Default"),
            "keystone_region_name": openstack.get("region_name", "RegionOne"),
            "keystone_interface": openstack.get("interface", "internal"),
        }
    database = data.get("database", {})
    cache = data.get("cache", {})
    lumen = data.get("lumen", {})
    chat = data.get("chat", {})
    mcp = data.get("mcp", {})
    merged: dict[str, Any] = {}
    for section in (keystone, database, cache, lumen, chat, mcp):
        for k, v in section.items():
            merged[k] = v
    return merged


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    # Infrastructure & Auth
    keystone_auth_url: str = "http://localhost:5000/v3"
    keystone_admin_username: str = "admin"
    keystone_admin_password: str = "secret"
    keystone_admin_project: str = "admin"
    keystone_domain: str = "Default"
    keystone_region_name: str = "RegionOne"
    keystone_interface: str = "internal"
    frontend_base_url: str = ""
    public_api_base: str = ""
    cors_origins: str = ""

    database_url: str = ""
    database_pool_size: int = 20
    database_max_overflow: int = 10
    database_connect_timeout: int = 10
    database_pool_timeout: int = 30
    database_unhealthy_seconds: int = 15
    database_auto_create_tables: bool = True

    redis_url: str = "redis://localhost:6379/8"
    redis_db_index: int = 8

    lumen_encryption_key: str = ""
    k3s_kubeconfig_encryption_key: str = ""

    # Service flags
    service_chat_enabled: bool = True

    # Chat Settings
    chat_execution_protocol_version: int = 1
    chat_default_model: str = ""
    chat_credit_per_usd: float = 1000.0
    chat_default_monthly_quota: float = 100000.0
    chat_stream_enabled: bool = True
    chat_api_hosts: str = ""
    chat_reasoning_effort: str = "auto"
    chat_mcp_oauth_callback_url: str = ""
    chat_checkpointer_postgres_url: str = ""
    chat_run_event_retention_hours: int = 24
    chat_checkpoint_retention_days: int = 7
    chat_semantic_memory_enabled: bool = False
    chat_memory_pgvector_url: str = ""
    chat_memory_embedding_model: str = ""
    chat_memory_embedding_dimensions: int = 0
    chat_memory_candidate_limit: int = 20
    chat_memory_retrieval_token_budget: int = 1200
    chat_memory_retention_days: int = 365
    chat_asset_s3_endpoint: str = ""
    chat_asset_s3_bucket: str = ""
    chat_asset_s3_access_key: str = ""
    chat_asset_s3_secret_key: str = ""
    chat_asset_s3_server_side_encryption: str = "AES256"
    chat_asset_s3_kms_key_id: str = ""
    chat_asset_signed_url_ttl_seconds: int = 300
    chat_clamav_host: str = ""
    chat_clamav_port: int = 3310
    chat_sandbox_workspace_url: str = ""
    chat_sandbox_url: str = ""
    chat_sandbox_api_key: str = ""
    chat_sandbox_image_digest: str = ""
    chat_sandbox_policy_version: str = ""
    chat_sandbox_egress_allowlist: list[str] = []
    chat_mcp_public_url: str = ""
    mcp_public_url: str = ""
    mcp_control_plane_url: str = ""
    lumen_mcp_service_token: str = ""

    os_cacert: str = ""
    insecure: bool = False

    @field_validator("chat_execution_protocol_version")
    @classmethod
    def validate_chat_execution_protocol_version(cls, value: int) -> int:
        if value not in {1, 2}:
            raise ValueError("chat_execution_protocol_version must be 1 or 2")
        return value

    @field_validator("chat_mcp_oauth_callback_url")
    @classmethod
    def validate_chat_mcp_oauth_callback_url(cls, value: str) -> str:
        value = value.strip()
        if not value:
            return ""
        if not (value.startswith("http://") or value.startswith("https://")):
            raise ValueError("chat_mcp_oauth_callback_url must start with http:// or https://")
        return value

    @property
    def get_lumen_encryption_key(self) -> str:
        key = (self.lumen_encryption_key or self.k3s_kubeconfig_encryption_key).strip()
        if not key:
            raise ValueError("Lumen encryption key is not set (lumen_encryption_key or k3s_kubeconfig_encryption_key)")
        if len(key) != 64 or any(c not in "0123456789abcdefABCDEF" for c in key):
            raise ValueError("Lumen encryption key must be exactly 64 hex characters")
        return key

    @property
    def verify(self) -> bool | str:
        if self.insecure:
            return False
        return self.os_cacert or True

    @property
    def cors_origin_list(self) -> list[str]:
        if not self.cors_origins:
            return []
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    for key, value in _load_toml().items():
        if not os.environ.get(key.upper()):
            os.environ[key.upper()] = str(value)
    return Settings()
