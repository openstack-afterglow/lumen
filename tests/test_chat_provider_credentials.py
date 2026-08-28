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
