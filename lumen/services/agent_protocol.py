"""Strict serializable contracts for the v2 browser-hosted agent executor.

Bindings and credentials deliberately remain outside these models.  Only definitions and
redacted, durable execution results may cross the model, journal, and UI boundaries.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from lumen.models.chat_contracts import FilePart, TextPart

MAX_TOOL_SCHEMA_BYTES = 32 * 1024
MAX_TOOL_ARGUMENT_BYTES = 256 * 1024
MAX_TOOL_MODEL_CONTENT_CHARS = 8_192
MAX_TOOL_DISPLAY_PARTS = 32
_TOOL_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,127}$")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, protected_namespaces=())


class CodePart(_StrictModel):
    type: Literal["code"] = "code"
    code: str = Field(min_length=1, max_length=65_536)
    language: str | None = Field(default=None, max_length=64)


ToolDisplayPart = TextPart | CodePart | FilePart
_TOOL_DISPLAY_PARTS = TypeAdapter(list[ToolDisplayPart])


class ToolDefinition(_StrictModel):
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(min_length=1, max_length=4_000)
    input_schema: dict[str, Any]
    effect: Literal["read", "workspace_write", "process", "external_mutation"]
    parallel_safe: bool = False
    source: Literal["builtin", "managed", "custom_http", "mcp", "workspace", "agent"]
    activity_category: str = Field(default="기본 도구", min_length=1, max_length=100)

    @field_validator("name")
    @classmethod
    def require_provider_safe_name(cls, value: str) -> str:
        if not _TOOL_NAME_RE.fullmatch(value):
            raise ValueError("tool name must match the provider-safe function-name grammar")
        return value

    @field_validator("input_schema")
    @classmethod
    def validate_schema(cls, value: dict[str, Any]) -> dict[str, Any]:
        validate_tool_schema(value)
        return value


@dataclass(frozen=True)
class ToolBinding:
    """Server-only executable binding; it must never enter a model or journal payload."""

    definition: ToolDefinition
    execute: Callable[[dict[str, Any], object], Awaitable[ToolExecutionResult]]
    classify_effect: (
        Callable[[dict[str, Any]], Literal["read", "workspace_write", "process", "external_mutation"]] | None
    ) = None
    preview: Callable[[dict[str, Any], object], Awaitable[dict[str, Any]]] | None = None
    config_fingerprint: str | None = None
    destination_origin: str | None = None
    load_policy: Literal["preloaded", "on_demand"] | None = None


class ToolArtifactRef(_StrictModel):
    asset_id: str = Field(min_length=1, max_length=36)
    kind: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=255)
    media_type: str = Field(min_length=1, max_length=127)
    size_bytes: int = Field(ge=0, le=5 * 1024 * 1024)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ToolExecutionResult(_StrictModel):
    status: Literal["completed", "failed", "denied", "canceled"]
    model_content: str = Field(default="", max_length=MAX_TOOL_MODEL_CONTENT_CHARS)
    display: list[ToolDisplayPart] = Field(default_factory=list, max_length=MAX_TOOL_DISPLAY_PARTS)
    artifacts: list[ToolArtifactRef] = Field(default_factory=list, max_length=20)
    usage_components: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = Field(default=None, max_length=100)
    retryable: bool = False

    @field_validator("display")
    @classmethod
    def validate_display(cls, value: list[ToolDisplayPart]) -> list[ToolDisplayPart]:
        return _TOOL_DISPLAY_PARTS.validate_python(value)


class ToolPolicy(_StrictModel):
    approval_mode: Literal["never", "mutations", "always"] = "mutations"
    enabled_tool_ids: list[int] = Field(default_factory=list, max_length=100)
    enabled_mcp_ids: list[int] = Field(default_factory=list, max_length=100)
    workspace_write_mode: Literal["ask", "auto_edit"] = "ask"


class AgentExecutionPolicy(_StrictModel):
    allowed_modes: set[Literal["chat", "plan", "code"]]
    can_delegate_read: bool = False
    can_delegate_write: bool = False
    max_model_turns: int = Field(ge=1, le=16)
    max_tool_calls: int = Field(ge=1, le=64)
    max_children: int = Field(ge=0, le=4)
    max_parallel_children: int = Field(ge=0, le=4)
    max_child_depth: int = Field(ge=0, le=1)


def validate_tool_schema(schema: object) -> None:
    """Reject unsafe/oversized schemas before jsonschema compiles one."""
    if not isinstance(schema, dict):
        raise ValueError("tool input_schema must be an object")
    encoded = str(schema).encode("utf-8")
    if len(encoded) > MAX_TOOL_SCHEMA_BYTES:
        raise ValueError("tool input_schema exceeds the byte limit")
    _validate_schema_shape(schema)
    try:
        Draft202012Validator.check_schema(schema)
    except Exception as exc:  # jsonschema raises several implementation-specific subclasses.
        raise ValueError("tool input_schema is invalid") from exc


def validate_tool_arguments(schema: dict[str, Any], arguments: object) -> dict[str, Any]:
    """Validate one model call before any network or sandbox I/O."""
    if not isinstance(arguments, dict):
        raise ValueError("tool arguments must be an object")
    encoded = str(arguments).encode("utf-8")
    if len(encoded) > MAX_TOOL_ARGUMENT_BYTES:
        raise ValueError("tool arguments exceed the byte limit")
    validate_tool_schema(schema)
    errors = list(Draft202012Validator(schema).iter_errors(arguments))
    if errors:
        raise ValueError("tool arguments do not match the frozen schema")
    return arguments


def _validate_schema_shape(value: object, *, depth: int = 0) -> None:
    if depth > 16:
        raise ValueError("tool input_schema exceeds the nesting limit")
    if isinstance(value, dict):
        if len(value) > 100:
            raise ValueError("tool input_schema has too many properties")
        if value.get("type") == "object" and value.get("additionalProperties") is not False:
            raise ValueError("tool object schemas must set additionalProperties to false")
        for key, child in value.items():
            if not isinstance(key, str) or len(key) > 128:
                raise ValueError("tool input_schema has an invalid key")
            _validate_schema_shape(child, depth=depth + 1)
    elif isinstance(value, list):
        if len(value) > 100:
            raise ValueError("tool input_schema has too many values")
        for child in value:
            _validate_schema_shape(child, depth=depth + 1)
    elif not isinstance(value, (str, int, float, bool, type(None))):
        raise ValueError("tool input_schema contains an unsupported value")
