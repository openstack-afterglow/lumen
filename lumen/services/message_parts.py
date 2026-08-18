"""Single encrypted persistence boundary for canonical chat message parts."""

from __future__ import annotations

import json
from typing import Any

from lumen.crypto import decrypt_chat_content, encrypt_chat_content
from lumen.models.chat_contracts import ChatPart, ChatParts, validate_chat_parts


class MessagePartsError(ValueError):
    """Stored canonical parts were absent, malformed, or violate the wire contract."""


def serialize_parts(parts: object) -> str:
    """Validate, canonicalize, and encrypt provider/server generated parts."""
    try:
        validated = validate_chat_parts(parts)
        payload = json.dumps(
            [part.model_dump(by_alias=True, exclude_none=True) for part in validated],
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise MessagePartsError("invalid canonical chat parts") from exc
    return encrypt_chat_content(payload)


def deserialize_parts(ciphertext: str | None) -> ChatParts:
    """Decrypt and validate stored canonical parts. Never downgrade malformed rows to text."""
    if not ciphertext:
        raise MessagePartsError("canonical chat parts are missing")
    try:
        parsed: Any = json.loads(decrypt_chat_content(ciphertext))
        return validate_chat_parts(parsed)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise MessagePartsError("stored canonical chat parts are invalid") from exc


def text_projection(parts: list[ChatPart]) -> str:
    """Stable plain-text projection for title/list preview and the temporary dual-write column."""
    return "\n".join(part.text for part in parts if part.type in {"text", "reasoning"})
