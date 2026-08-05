"""Lumen provider-key encryption and domain-separation tests.

Lumen owns the ``llm_provider_key`` and ``chat_content`` crypto domains. These
tests intentionally do not depend on Drover's legacy kubeconfig crypto domain.
"""

from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.crypto

_VALID_KEY_HEX = "a" * 64  # 32바이트 (64 hex characters)
_OTHER_KEY_HEX = "b" * 64


@pytest.fixture
def valid_key(monkeypatch):
    monkeypatch.setattr(
        "lumen.crypto.get_settings",
        lambda: SimpleNamespace(get_lumen_encryption_key=_VALID_KEY_HEX),
    )


class TestLlmProviderKeyCrypto:
    def test_roundtrip(self, valid_key):
        from lumen.crypto import decrypt_llm_provider_key, encrypt_llm_provider_key

        plaintext = "sk-provider-secret-abcdef0123456789"
        ciphertext = encrypt_llm_provider_key(plaintext)
        assert ciphertext != plaintext
        assert decrypt_llm_provider_key(ciphertext) == plaintext

    def test_decrypts_afterglow_chat_content_ciphertext(self, valid_key):
        from afterglow_crypto import aesgcm

        from lumen.crypto import decrypt_chat_content

        source_ciphertext = aesgcm.encrypt(bytes.fromhex(_VALID_KEY_HEX), b"chat_content", "migrated message")

        assert decrypt_chat_content(source_ciphertext) == "migrated message"

    def test_ciphertext_uses_v3_prefix(self, valid_key):
        from lumen.crypto import encrypt_llm_provider_key

        ciphertext = encrypt_llm_provider_key("sk-abc")
        assert ciphertext.startswith("v3:")

    def test_nonce_randomized(self, valid_key):
        from lumen.crypto import encrypt_llm_provider_key

        # 동일 평문이라도 nonce 랜덤화로 매번 다른 ciphertext.
        ct1 = encrypt_llm_provider_key("sk-abc")
        ct2 = encrypt_llm_provider_key("sk-abc")
        assert ct1 != ct2

    def test_domain_separation_chat_content_cannot_decrypt_provider_key(self, valid_key):
        """A provider-key ciphertext cannot decrypt through Lumen chat-content crypto."""
        from lumen.crypto import decrypt_chat_content, encrypt_llm_provider_key

        ciphertext = encrypt_llm_provider_key("sk-provider-secret")
        with pytest.raises(Exception):
            decrypt_chat_content(ciphertext)

    def test_domain_separation_reverse(self, valid_key):
        """A chat-content ciphertext cannot decrypt through Lumen provider-key crypto."""
        from lumen.crypto import decrypt_llm_provider_key, encrypt_chat_content

        ciphertext = encrypt_chat_content("chat message")
        with pytest.raises(Exception):
            decrypt_llm_provider_key(ciphertext)

    def test_decrypt_fails_with_different_master_key(self, valid_key, monkeypatch):
        from lumen.crypto import decrypt_llm_provider_key, encrypt_llm_provider_key

        ciphertext = encrypt_llm_provider_key("sk-provider-secret")
        monkeypatch.setattr(
            "lumen.crypto.get_settings",
            lambda: SimpleNamespace(get_lumen_encryption_key=_OTHER_KEY_HEX),
        )
        with pytest.raises(Exception):
            decrypt_llm_provider_key(ciphertext)
