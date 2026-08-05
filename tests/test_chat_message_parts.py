import pytest

from lumen.services.message_parts import MessagePartsError, deserialize_parts, serialize_parts, text_projection


def test_canonical_parts_encrypt_roundtrip_and_project_text():
    encrypted = serialize_parts(
        [
            {"type": "text", "text": "answer"},
            {"type": "citation", "citation_id": "source-1", "url": "https://example.test/source"},
            {"type": "reasoning", "text": "visible", "visibility": "user"},
        ]
    )

    parts = deserialize_parts(encrypted)

    assert encrypted.startswith("v3:")
    assert [part.type for part in parts] == ["text", "citation", "reasoning"]
    assert text_projection(parts) == "answer\nvisible"


def test_canonical_parts_fail_closed_for_missing_or_malformed_ciphertext():
    with pytest.raises(MessagePartsError, match="missing"):
        deserialize_parts(None)
    with pytest.raises(MessagePartsError, match="invalid"):
        deserialize_parts("v3:not-a-valid-ciphertext")
