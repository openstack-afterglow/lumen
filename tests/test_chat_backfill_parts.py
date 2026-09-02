import json

import pytest

from lumen.crypto import encrypt_chat_content
from lumen.services.backfill_parts import (
    BackfillPartsError,
    forward_encrypt_parts,
    legacy_to_parts,
    reverse_legacy_projection,
)


def test_forward_and_reverse_backfill_preserve_each_legacy_projection():
    cipher = forward_encrypt_parts(
        content=encrypt_chat_content("answer"),
        reasoning=encrypt_chat_content("visible reasoning"),
        tool_calls=encrypt_chat_content(json.dumps([{"id": "call-1", "name": "lookup", "args": '{"q":"safe"}'}])),
        citations=encrypt_chat_content(
            json.dumps([{"id": "cite-1", "url": "https://example.test", "title": "source"}])
        ),
    )

    restored = reverse_legacy_projection(cipher)

    assert restored.content == "answer"
    assert restored.reasoning == "visible reasoning"
    assert restored.tool_calls == [{"id": "call-1", "name": "lookup", "args": '{"q":"safe"}'}]
    assert restored.citations == [{"id": "cite-1", "url": "https://example.test", "title": "source"}]


def test_backfill_fails_closed_on_malformed_legacy_records():
    with pytest.raises(BackfillPartsError, match="malformed"):
        legacy_to_parts(
            content=encrypt_chat_content("answer"), tool_calls="v3:not-valid", citations=None, reasoning=None
        )
    with pytest.raises(BackfillPartsError, match="no canonical"):
        legacy_to_parts(content=None, tool_calls=None, citations=None, reasoning=None)
