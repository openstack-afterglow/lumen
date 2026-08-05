"""Validation boundary for provider JSON output.

The provider is untrusted: response-format schemas are narrowed at request validation,
and this module bounds and validates the returned JSON before it becomes a canonical
chat part.
"""

from __future__ import annotations

import json
from typing import Any

from jsonschema import Draft202012Validator, ValidationError

_MAX_STRUCTURED_OUTPUT_BYTES = 1 * 1024 * 1024
_MAX_STRUCTURED_OUTPUT_DEPTH = 64


class StructuredOutputError(ValueError):
    """A provider response did not satisfy the requested public JSON contract."""


def _depth(value: Any, *, current: int = 0) -> int:
    if current > _MAX_STRUCTURED_OUTPUT_DEPTH:
        raise StructuredOutputError("structured output exceeds the maximum nesting depth")
    if isinstance(value, dict):
        return max((_depth(item, current=current + 1) for item in value.values()), default=current)
    if isinstance(value, list):
        return max((_depth(item, current=current + 1) for item in value), default=current)
    return current


def parse_structured_output(text: str, response_format: dict[str, Any]) -> dict[str, Any]:
    """Turn bounded provider JSON into one canonical structured part or fail safely."""
    try:
        encoded = text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise StructuredOutputError("structured output is not UTF-8") from exc
    if len(encoded) > _MAX_STRUCTURED_OUTPUT_BYTES:
        raise StructuredOutputError("structured output exceeds 1 MiB")
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise StructuredOutputError("provider did not return valid JSON") from exc
    _depth(value)
    kind = response_format.get("kind")
    if kind == "json_object":
        if not isinstance(value, dict):
            raise StructuredOutputError("json_object response must be an object")
        return {
            "type": "structured",
            "schema_name": "json_object",
            "schema_version": "1",
            "value": value,
            "valid": True,
        }
    if kind != "json_schema":
        raise StructuredOutputError("structured output was not requested")
    schema = response_format.get("schema")
    if not isinstance(schema, dict):
        raise StructuredOutputError("json_schema request is missing its schema")
    try:
        Draft202012Validator(schema).validate(value)
    except ValidationError as exc:
        location = ".".join(str(item) for item in exc.absolute_path) or "$"
        raise StructuredOutputError(f"structured output does not match schema at {location}") from exc
    name = response_format.get("name")
    version = response_format.get("version")
    if not isinstance(name, str) or not isinstance(version, str):
        raise StructuredOutputError("json_schema request is missing its identity")
    return {
        "type": "structured",
        "schema_name": name,
        "schema_version": version,
        "value": value,
        "valid": True,
    }
