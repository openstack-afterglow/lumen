"""Explicit upstream account graphs for integration fixtures, never application defaults."""
from __future__ import annotations

from lumen.crypto import encrypt_llm_provider_key
from lumen.models.chat_db import LlmProvider, LlmProviderAccount, LlmProviderCredential


def configured_provider(**fields) -> LlmProvider:
    """Build a real endpoint/account/credential graph with a non-production key."""
    mode = fields.get("auth_mode", "api_key")
    credential = LlmProviderCredential(
        name="fixture-credential", auth_mode=mode, generation=0, is_active=True,
        priority=0, weight=1, subscription_status="disconnected",
        encrypted_api_key=encrypt_llm_provider_key("fixture-provider-key") if mode == "api_key" else None,
    )
    account = LlmProviderAccount(name="fixture-account", is_active=True, priority=0, weight=1,
                                 credentials=[credential])
    return LlmProvider(selection_policy="priority", accounts=[account], **fields)
