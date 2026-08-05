"""빌트인 AI 채팅 콘텐츠 암호화 단위 테스트 (DB 불요).

- lumen.crypto.encrypt/decrypt_chat_content: v3 round-trip, prefix, plaintext passthrough, and domain separation
- conversation_store 의 _enc/_dec/_enc_json/_dec_json 헬퍼 왕복(메시지/제목/툴기록 암호화 경계)
"""

from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.crypto

_VALID_KEY_HEX = "a" * 64


@pytest.fixture
def valid_key(monkeypatch):
    monkeypatch.setattr(
        "lumen.crypto.get_settings",
        lambda: SimpleNamespace(get_lumen_encryption_key=_VALID_KEY_HEX),
    )


class TestChatContentCrypto:
    def test_roundtrip(self, valid_key):
        from lumen.crypto import decrypt_chat_content, encrypt_chat_content

        plaintext = "안녕하세요, 이 대화 내용은 비밀입니다. secret-42"
        ct = encrypt_chat_content(plaintext)
        assert ct != plaintext
        assert ct.startswith("v3:")
        assert decrypt_chat_content(ct) == plaintext

    def test_plaintext_passthrough(self, valid_key):
        """암호화 도입 이전 저장된 평문 행(prefix 없음)은 그대로 반환(base64/AES 시도 금지)."""
        from lumen.crypto import decrypt_chat_content

        assert decrypt_chat_content("예전에 저장된 평문 메시지") == "예전에 저장된 평문 메시지"

    def test_cross_domain_not_decryptable(self, valid_key):
        """chat_content 암호문을 llm_provider_key 로 복호화하면 실패(도메인 분리)."""
        from lumen.crypto import decrypt_llm_provider_key, encrypt_chat_content

        ct = encrypt_chat_content("secret")
        with pytest.raises(Exception):
            decrypt_llm_provider_key(ct)


class TestConversationStoreHelpers:
    def test_content_enc_dec(self, valid_key):
        from lumen.services import conversation_store as cs

        assert cs._dec(cs._enc("hello world")) == "hello world"
        # None/빈문자열은 그대로(암호화하지 않음)
        assert cs._enc(None) is None
        assert cs._enc("") == ""
        assert cs._dec(None) is None
        # 암호문은 실제로 평문과 달라야 함
        assert cs._enc("hello") != "hello"
        assert cs._enc("hello").startswith("v3:")

    def test_tool_calls_enc_dec(self, valid_key):
        from lumen.services import conversation_store as cs

        tool_calls = [{"id": "call_1", "name": "list_instances", "args": "{}"}]
        enc = cs._enc_json(tool_calls)
        assert enc is not None
        assert enc.startswith("v3:")
        assert "list_instances" not in enc  # 평문 노출 없음
        assert cs._dec_json(enc) == tool_calls

    def test_tool_calls_none(self, valid_key):
        from lumen.services import conversation_store as cs

        assert cs._enc_json(None) is None
        assert cs._dec_json(None) is None
