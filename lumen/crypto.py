"""Lumen encryption — AES-256-GCM + HKDF sub-key domain separation."""

from __future__ import annotations

import logging

from afterglow_crypto import aesgcm

from lumen.config import get_settings

_logger = logging.getLogger(__name__)

_DOMAIN_LLM_PROVIDER_KEY = b"llm_provider_key"
_DOMAIN_CHAT_CONTENT = b"chat_content"

_V3_PREFIX = aesgcm._V3_PREFIX


def _get_key() -> bytes:
    s = get_settings()
    hex_key = s.get_lumen_encryption_key
    if not hex_key:
        raise ValueError("Lumen encryption key is not set")
    return bytes.fromhex(hex_key)


def derive_encryption_subkey(domain: bytes) -> bytes:
    return aesgcm.derive_encryption_subkey(_get_key(), domain)


def encrypt_llm_provider_key(plaintext: str) -> str:
    return aesgcm.encrypt(_get_key(), _DOMAIN_LLM_PROVIDER_KEY, plaintext)


def decrypt_llm_provider_key(ciphertext_b64: str) -> str:
    return aesgcm.decrypt(_get_key(), _DOMAIN_LLM_PROVIDER_KEY, ciphertext_b64)


def encrypt_chat_content(plaintext: str) -> str:
    return aesgcm.encrypt(_get_key(), _DOMAIN_CHAT_CONTENT, plaintext)


def decrypt_chat_content(ciphertext_b64: str) -> str:
    """Decrypt current chat ciphertext, preserving pre-encryption plaintext rows."""
    if not ciphertext_b64.startswith(_V3_PREFIX):
        return ciphertext_b64
    return aesgcm.decrypt(_get_key(), _DOMAIN_CHAT_CONTENT, ciphertext_b64)
