"""Provider credential resolution from encrypted storage or a configured environment variable."""

from __future__ import annotations

import os
import re
from typing import Literal, NotRequired, Protocol, TypedDict

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
    account_id: int
    credential_id: int
    auth_mode: Literal["api_key", "chatgpt_device", "anthropic_subscription"]
    model_id: NotRequired[int]
    required_capabilities: NotRequired[list[str]]


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


def transport_model_name(model_name: str) -> str:
    """Strip exactly one Lumen subscription namespace before provider transport."""
    normalized = str(model_name or "").strip()
    prefix, separator, bare = normalized.partition("/")
    if separator and prefix in _SUBSCRIPTION_PREFIXES and bare and "/" not in bare:
        return bare
    return normalized


def api_model_name(model_name: str, provider_type: str) -> str:
    """Project a stable external API ID without changing the stored route key."""
    normalized = str(model_name or "").strip()
    if provider_type == "perplexity":
        while normalized.startswith("perplexity/perplexity/"):
            normalized = normalized[len("perplexity/") :]
        prefix, separator, remainder = normalized.partition("/")
        if prefix == "perplexity" and separator and "/" in remainder:
            return remainder
        if "/" not in normalized:
            return f"perplexity/{normalized}" if normalized else normalized
        return normalized
    if provider_type == "gemini" or normalized.startswith("gemini/gemini-"):
        while normalized.startswith("gemini/"):
            rest = normalized[len("gemini/") :]
            if not rest:
                break
            normalized = rest
        return normalized
    return normalized


def short_model_name(model_name: str, provider_type: str | None = None) -> str:
    """Return a human-friendly shortened model name without redundant provider prefixes."""
    name = api_model_name(model_name, provider_type or "")
    if provider_type == "perplexity":
        while name.startswith("perplexity/"):
            rest = name[len("perplexity/") :]
            if not rest:
                break
            name = rest
    elif provider_type == "gemini" or name.startswith("gemini/"):
        while name.startswith("gemini/"):
            rest = name[len("gemini/") :]
            if not rest:
                break
            name = rest
    return name


def perplexity_route_model_name(model_name: str) -> str:
    """Encode one canonical Perplexity API ID as a provider transport route."""
    canonical = api_model_name(model_name, "perplexity")
    if (
        not canonical
        or any(not segment for segment in canonical.split("/"))
        or any(character.isspace() for character in canonical)
    ):
        raise ProviderValidationError("Perplexity model_name 형식이 올바르지 않습니다")
    routed = f"perplexity/{canonical}"
    if len(routed) > 190:
        raise ProviderValidationError("Perplexity model_name 은 190자 이하여야 합니다")
    return routed


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


def validate_auth_ref(ref: ProviderAuthRef) -> None:
    from .errors import ProviderConfigurationChangedError
    if (not isinstance(ref, dict) or any(type(ref.get(k)) is not int or ref[k] <= 0
            for k in ("provider_id", "account_id", "credential_id"))
            or type(ref.get("generation")) is not int or ref["generation"] < 0
            or ref.get("auth_mode") not in {"api_key", "chatgpt_device", "anthropic_subscription"}):
        raise ProviderConfigurationChangedError("legacy or invalid credential reference requires readmission")


def api_key_route_configured(route: dict) -> bool:
    """Structural readiness only; fresh resolution still checks current authority."""
    auth = route.get("provider_auth")
    if auth is None:
        key = route.get("api_key")
        return isinstance(key, str) and bool(key.strip())
    from .errors import ProviderConfigurationChangedError
    try:
        validate_auth_ref(auth)
    except ProviderConfigurationChangedError:
        return False
    return auth["auth_mode"] == "api_key"


async def resolve_inference_credential(ref: ProviderAuthRef) -> dict:
    """Resolve the exact live credential, never a pool replacement or persisted secret."""
    validate_auth_ref(ref)
    if ref.get("auth_mode") != "api_key":
        from .subscriptions import resolve_subscription_credential
        return await resolve_subscription_credential(ref)
    from lumen.services.durable_runs.budgets import retry_deadlocks
    return await retry_deadlocks(lambda: _resolve_api_credential(ref))


async def _resolve_api_credential(ref: ProviderAuthRef) -> dict:
    import hashlib

    from .accounts import _eligible, anonymous_allowed, lock_credential
    from .errors import ProviderNotFoundError, ProviderSubscriptionError
    from .routing import _require_db
    async with _require_db()() as session, session.begin():
        try:
            provider, account, credential = await lock_credential(
                session, ref["provider_id"], account_id=ref["account_id"], credential_id=ref["credential_id"])
        except ProviderNotFoundError:
            raise ProviderSubscriptionError("subscription_auth_required", 502) from None
        if (not provider.is_active or not account.is_active or not credential.is_active
                or provider.auth_mode != ref["auth_mode"]
                or credential.generation != ref.get("generation") or credential.auth_mode != ref.get("auth_mode")
                or not _eligible(account, ref.get("model_id"), ref.get("required_capabilities", ()))
                or not _eligible(credential, ref.get("model_id"), ref.get("required_capabilities", ()))
                or credential.subscription_status == "reauth_required"):
            raise ProviderSubscriptionError("subscription_auth_required", 502)
        key = resolve_api_key(credential)
        if not key and not anonymous_allowed(provider):
            raise ProviderSubscriptionError("subscription_auth_required", 502)
        return {"api_key": key, "_fingerprint": hashlib.sha256((key or "").encode()).hexdigest()}


async def mark_inference_credential_rejected(ref: ProviderAuthRef, fingerprint: str) -> None:
    """An old response cannot poison a rotated key or another account's grant."""
    validate_auth_ref(ref)
    if ref.get("auth_mode") != "api_key":
        from .subscriptions import _mark_subscription_credential_rejected
        await _mark_subscription_credential_rejected(ref, fingerprint)
        return
    import hashlib
    import hmac

    from .accounts import lock_credential
    from .errors import ProviderNotFoundError
    from .routing import _require_db
    async with _require_db()() as session, session.begin():
        try:
            _, _, credential = await lock_credential(
                session, ref["provider_id"], account_id=ref.get("account_id"), credential_id=ref.get("credential_id"))
        except ProviderNotFoundError:
            return
        if credential.generation != ref.get("generation") or credential.auth_mode != "api_key":
            return
        key = resolve_api_key(credential)
        if key and hmac.compare_digest(hashlib.sha256(key.encode()).hexdigest(), fingerprint):
            credential.subscription_status = "reauth_required"


def execution_identity(route: dict) -> dict:
    """Copy only secret-free selected execution provenance into durable snapshots."""
    from copy import deepcopy
    fields = ("provider_account_id", "provider_credential_id", "provider_credential_generation",
              "provider_backend", "provider_backend_version", "provider_auth", "provider_quota_group")
    return {key: deepcopy(route[key]) for key in fields if key in route}


async def resolve_route_api_key(route: dict) -> str:
    ref = route.get("provider_auth")
    if ref is not None:
        if ref.get("auth_mode") != "api_key":
            raise ProviderValidationError("API-key credential required")
        material = await resolve_inference_credential(ref)
        key = material.get("api_key")
    else:
        key = route.get("api_key")
    if not isinstance(key, str) or not key.strip():
        raise ProviderValidationError("provider API key unavailable")
    return key
