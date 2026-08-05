"""Forward/reverse conversion helpers for the A1-A4 encrypted message-parts window."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from lumen.models.chat_contracts import ChatParts, validate_chat_parts
from lumen.services.message_parts import deserialize_parts, serialize_parts
from lumen.crypto import decrypt_chat_content


class BackfillPartsError(ValueError):
    pass


@dataclass(frozen=True)
class LegacyMessageProjection:
    content: str | None
    tool_calls: list[dict[str, Any]] | None
    citations: list[dict[str, Any]] | None
    reasoning: str | None


def _legacy_json(ciphertext: str | None, label: str) -> Any:
    if not ciphertext:
        return None
    try:
        return json.loads(decrypt_chat_content(ciphertext))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise BackfillPartsError(f"legacy {label} is malformed") from exc


def legacy_to_parts(
    *, content: str | None, tool_calls: str | None, citations: str | None, reasoning: str | None
) -> ChatParts:
    """Build canonical assistant history from encrypted legacy projections without data loss."""
    raw_parts: list[dict[str, Any]] = []
    if content:
        raw_parts.append({"type": "text", "text": decrypt_chat_content(content)})
    if reasoning:
        raw_parts.append({"type": "reasoning", "text": decrypt_chat_content(reasoning), "visibility": "user"})
    legacy_calls = _legacy_json(tool_calls, "tool_calls")
    if legacy_calls:
        if not isinstance(legacy_calls, list):
            raise BackfillPartsError("legacy tool_calls must be a list")
        for index, call in enumerate(legacy_calls):
            if not isinstance(call, dict):
                raise BackfillPartsError("legacy tool call must be an object")
            raw_parts.append(
                {
                    "type": "tool_call",
                    "call_id": str(call.get("id") or f"legacy-{index}"),
                    "name": str(call.get("name") or "legacy_tool"),
                    "arguments": _legacy_tool_arguments(call),
                    "status": "completed",
                }
            )
    legacy_citations = _legacy_json(citations, "citations")
    if legacy_citations:
        if not isinstance(legacy_citations, list):
            raise BackfillPartsError("legacy citations must be a list")
        for index, citation in enumerate(legacy_citations):
            if not isinstance(citation, dict) or not isinstance(citation.get("url"), str):
                raise BackfillPartsError("legacy citation is malformed")
            raw_parts.append(
                {
                    "type": "citation",
                    "citation_id": str(citation.get("id") or f"legacy-citation-{index}"),
                    "url": citation["url"],
                    "title": citation.get("title"),
                    "snippet": citation.get("snippet"),
                }
            )
    if not raw_parts:
        raise BackfillPartsError("legacy message has no canonical content")
    return validate_chat_parts(raw_parts)


def forward_encrypt_parts(**legacy: str | None) -> str:
    return serialize_parts(legacy_to_parts(**legacy))


def _legacy_tool_arguments(call: dict[str, Any]) -> dict[str, Any]:
    value = call.get("args", call.get("arguments", {}))
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise BackfillPartsError("legacy tool arguments are malformed") from exc
        if isinstance(parsed, dict):
            return parsed
    raise BackfillPartsError("legacy tool arguments must be an object")


def reverse_legacy_projection(parts_ciphertext: str) -> LegacyMessageProjection:
    """Restore every retained legacy projection before an A4a-to-A3 binary rollback."""
    parts = deserialize_parts(parts_ciphertext)
    text_parts = [part.text for part in parts if part.type == "text"]
    reasoning_parts = [part.text for part in parts if part.type == "reasoning"]
    tool_calls = [
        {
            "id": part.call_id,
            "name": part.name,
            "args": json.dumps(part.arguments, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        }
        for part in parts
        if part.type == "tool_call"
    ]
    citations = [
        {
            "id": part.citation_id,
            "url": part.url,
            **({"title": part.title} if part.title is not None else {}),
            **({"snippet": part.snippet} if part.snippet is not None else {}),
        }
        for part in parts
        if part.type == "citation"
    ]
    return LegacyMessageProjection(
        content="\n".join(text_parts) if text_parts else None,
        tool_calls=tool_calls or None,
        citations=citations or None,
        reasoning="\n".join(reasoning_parts) if reasoning_parts else None,
    )
