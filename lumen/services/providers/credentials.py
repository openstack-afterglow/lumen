"""Provider credential resolution from encrypted storage or a configured environment variable."""

from __future__ import annotations

import os
import re
from typing import Protocol

from lumen.config import Settings
from lumen.crypto import decrypt_llm_provider_key

from .errors import ProviderValidationError

_ENV_NAME = re.compile(r"[A-Z_][A-Z0-9_]{0,127}")


class _ProviderCredential(Protocol):
    encrypted_api_key: str | None
    api_key_env: str | None


def normalize_api_key_env(value: str | None) -> str | None:
    """Accept only portable environment-variable names; empty clears the fallback."""
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    if not _ENV_NAME.fullmatch(normalized):
        raise ProviderValidationError("api_key_env는 대문자 영문자·숫자·밑줄로 된 환경 변수 이름이어야 합니다")
    if normalized.lower() in Settings.model_fields:
        raise ProviderValidationError("api_key_env는 Lumen 설정에 사용하는 환경 변수 이름일 수 없습니다")
    return normalized


def api_key_source(provider: _ProviderCredential) -> str | None:
    """Return the effective credential source without exposing or decrypting the key."""
    if provider.encrypted_api_key:
        return "database"
    env_name = getattr(provider, "api_key_env", None)
    if not env_name or not _ENV_NAME.fullmatch(env_name) or env_name.lower() in Settings.model_fields:
        return None
    return "environment" if os.environ.get(env_name, "").strip() else None


def resolve_api_key(provider: _ProviderCredential) -> str | None:
    """Use the encrypted database credential first, then its configured environment fallback."""
    source = api_key_source(provider)
    if source == "database":
        return decrypt_llm_provider_key(provider.encrypted_api_key)
    if source == "environment":
        return os.environ.get(provider.api_key_env or "", "").strip() or None
    return None
