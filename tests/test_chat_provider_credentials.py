from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from lumen.services.providers import credentials, pricing
from lumen.services.providers.errors import ProviderValidationError


def test_database_provider_key_takes_precedence_over_environment(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "environment-key")
    monkeypatch.setattr(credentials, "decrypt_llm_provider_key", lambda encrypted: f"decrypted:{encrypted}")

    provider = SimpleNamespace(encrypted_api_key="ciphertext", api_key_env="OPENAI_API_KEY")

    assert credentials.resolve_api_key(provider) == "decrypted:ciphertext"
    assert credentials.api_key_source(provider) == "database"


def test_environment_provider_key_is_used_when_database_key_is_absent(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "environment-key")

    provider = SimpleNamespace(encrypted_api_key=None, api_key_env="OPENAI_API_KEY")

    assert credentials.resolve_api_key(provider) == "environment-key"
    assert credentials.api_key_source(provider) == "environment"


def test_empty_or_invalid_environment_key_configuration_has_no_credential(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    assert credentials.resolve_api_key(SimpleNamespace(encrypted_api_key=None, api_key_env="OPENAI_API_KEY")) is None
    assert credentials.resolve_api_key(SimpleNamespace(encrypted_api_key=None, api_key_env="not-valid")) is None
    assert credentials.api_key_source(SimpleNamespace(encrypted_api_key=None, api_key_env="OPENAI_API_KEY")) is None
    with pytest.raises(ProviderValidationError, match="환경 변수 이름"):
        credentials.normalize_api_key_env("not-valid")


def test_public_provider_projection_reports_missing_and_environment_credentials(monkeypatch):
    provider = SimpleNamespace(
        id=1,
        name="openai",
        provider_type="openai",
        api_base=None,
        encrypted_api_key=None,
        api_key_env="OPENAI_API_KEY",
        is_active=True,
        margin_multiplier=Decimal("1"),
        models_dev_provider_id=None,
        created_at=None,
        updated_at=None,
    )
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    missing = pricing._provider_public(provider)
    assert missing["has_api_key"] is False
    assert missing["api_key_source"] is None

    monkeypatch.setenv("OPENAI_API_KEY", "environment-key")
    configured = pricing._provider_public(provider)
    assert configured["has_api_key"] is True
    assert configured["api_key_source"] == "environment"


def test_lumen_setting_names_cannot_be_provider_credential_sources(monkeypatch):
    monkeypatch.setenv("LUMEN_ENCRYPTION_KEY", "must-not-be-exposed")

    assert (
        credentials.resolve_api_key(SimpleNamespace(encrypted_api_key=None, api_key_env="LUMEN_ENCRYPTION_KEY")) is None
    )
    with pytest.raises(ProviderValidationError, match="Lumen 설정"):
        credentials.normalize_api_key_env("LUMEN_ENCRYPTION_KEY")


def test_subscription_provider_never_uses_api_key_or_environment_fallback(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-be-used")
    provider = SimpleNamespace(
        auth_mode="chatgpt_device",
        encrypted_api_key="legacy-ciphertext",
        api_key_env="OPENAI_API_KEY",
    )

    assert credentials.api_key_source(provider) is None
    assert credentials.resolve_api_key(provider) is None


@pytest.mark.parametrize(
    ("auth_mode", "model_name", "expected"),
    (
        ("chatgpt_device", "gpt-5.2-codex", "chatgpt/gpt-5.2-codex"),
        ("chatgpt_device", "chatgpt/gpt-5.2-codex", "chatgpt/gpt-5.2-codex"),
        ("anthropic_subscription", "claude-opus-4-1", "anthropic-subscription/claude-opus-4-1"),
        (
            "anthropic_subscription",
            "anthropic-subscription/claude-opus-4-1",
            "anthropic-subscription/claude-opus-4-1",
        ),
        ("api_key", "anthropic/claude-opus-4-1", "anthropic/claude-opus-4-1"),
    ),
)
def test_subscription_model_names_are_canonical(auth_mode, model_name, expected):
    assert credentials.canonical_subscription_model_name(model_name, auth_mode) == expected


@pytest.mark.parametrize(
    ("auth_mode", "model_name"),
    (
        ("chatgpt_device", "anthropic/claude-opus-4-1"),
        ("chatgpt_device", "chatgpt/chatgpt/gpt-5"),
        ("anthropic_subscription", "chatgpt/gpt-5"),
        ("api_key", "chatgpt/gpt-5"),
        ("api_key", "anthropic-subscription/claude-opus-4-1"),
    ),
)
def test_foreign_or_double_subscription_model_prefixes_are_rejected(auth_mode, model_name):
    with pytest.raises(ProviderValidationError):
        credentials.canonical_subscription_model_name(model_name, auth_mode)


def test_litellm_model_name_strips_exactly_one_subscription_namespace():
    assert credentials.litellm_model_name("chatgpt/gpt-5.2-codex") == "gpt-5.2-codex"
    assert credentials.litellm_model_name("anthropic-subscription/claude-opus-4-1") == "claude-opus-4-1"
    assert credentials.litellm_model_name("anthropic/claude-opus-4-1") == "anthropic/claude-opus-4-1"
    assert credentials.litellm_model_name("chatgpt/chatgpt/gpt-5") == "chatgpt/chatgpt/gpt-5"


def test_subscription_public_projection_distinguishes_api_key_and_subscription_status(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "environment-key")
    base = {
        "id": 1,
        "name": "provider",
        "provider_type": "openai",
        "api_base": None,
        "encrypted_api_key": None,
        "api_key_env": "OPENAI_API_KEY",
        "encrypted_subscription_tokens": None,
        "subscription_status": "disconnected",
        "subscription_expires_at": None,
        "subscription_generation": 0,
        "is_active": True,
        "margin_multiplier": Decimal("1"),
        "models_dev_provider_id": None,
        "created_at": None,
        "updated_at": None,
    }
    api_key = pricing._provider_public(SimpleNamespace(auth_mode="api_key", **base))
    chatgpt = pricing._provider_public(
        SimpleNamespace(
            **{
                **base,
                "provider_type": "chatgpt",
                "api_key_env": None,
                "auth_mode": "chatgpt_device",
                "encrypted_subscription_tokens": "ciphertext",
                "subscription_status": "configured",
                "subscription_expires_at": datetime.now(UTC) - timedelta(hours=1),
            }
        )
    )
    claude_expired = pricing._provider_public(
        SimpleNamespace(
            **{
                **base,
                "provider_type": "anthropic",
                "api_key_env": None,
                "auth_mode": "anthropic_subscription",
                "encrypted_subscription_tokens": "ciphertext",
                "subscription_status": "configured",
                "subscription_expires_at": datetime.now(UTC) - timedelta(hours=1),
            }
        )
    )

    assert api_key["has_credentials"] is True
    assert api_key["has_api_key"] is True
    assert chatgpt["has_credentials"] is True
    assert chatgpt["has_api_key"] is False
    assert claude_expired["has_credentials"] is False
    assert claude_expired["auth_status"] == "configured"
