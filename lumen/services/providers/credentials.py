"""Provider credential resolution from encrypted storage or a configured environment variable."""

from __future__ import annotations

import os
import re
from typing import Literal, Protocol, TypedDict

from lumen.config import Settings
from lumen.crypto import decrypt_llm_provider_key

from .errors import ProviderValidationError

_ENV_NAME = re.compile(r"[A-Z_][A-Z0-9_]{0,127}")

SubscriptionAuthMode = Literal["chatgpt_device", "anthropic_subscription"]
_SUBSCRIPTION_PREFIX_BY_MODE: dict[SubscriptionAuthMode, str] = {
    "chatgpt_device": "chatgpt",
    "anthropic_subscription": "anthropic-subscription",
}
_SUBSCRIPTION_PREFIXES = frozenset(_SUBSCRIPTION_PREFIX_BY_MODE.values())


class ProviderAuthRef(TypedDict):
    """Secret-free pointer resolved only at the request-local transport boundary."""

    provider_id: int
    generation: int
    auth_mode: SubscriptionAuthMode


class _ProviderCredential(Protocol):
    encrypted_api_key: str | None
    api_key_env: str | None
    auth_mode: str


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


def canonical_subscription_model_name(model_name: str, auth_mode: str) -> str:
    """Canonicalize subscription model IDs and reject cross-provider prefixes."""
    normalized = str(model_name or "").strip()
    if not normalized:
        raise ProviderValidationError("model_name 은 필수입니다")
    if auth_mode == "api_key":
        prefix = normalized.split("/", 1)[0]
        if prefix in _SUBSCRIPTION_PREFIXES:
            raise ProviderValidationError("구독 전용 model_name 은 구독 프로바이더에서만 사용할 수 있습니다")
        return normalized
    expected_prefix = _SUBSCRIPTION_PREFIX_BY_MODE.get(auth_mode)
    if expected_prefix is None:
        raise ProviderValidationError("지원하지 않는 provider auth_mode 입니다")
    parts = normalized.split("/")
    if len(parts) == 1:
        return f"{expected_prefix}/{normalized}"
    if len(parts) == 2 and parts[0] == expected_prefix and parts[1]:
        return normalized
    raise ProviderValidationError(f"model_name 은 bare 이름 또는 {expected_prefix}/ 접두부만 허용합니다")


def litellm_model_name(model_name: str) -> str:
    """Strip exactly one Lumen subscription namespace before provider transport."""
    normalized = str(model_name or "").strip()
    prefix, separator, bare = normalized.partition("/")
    if separator and prefix in _SUBSCRIPTION_PREFIXES and bare and "/" not in bare:
        return bare
    return normalized


def api_key_source(provider: _ProviderCredential) -> str | None:
    """Return the effective credential source without exposing or decrypting the key."""
    if getattr(provider, "auth_mode", "api_key") != "api_key":
        return None
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
