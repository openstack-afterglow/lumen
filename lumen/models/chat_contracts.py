"""Canonical, provider-neutral chat wire and persistence contracts.

Only these typed parts cross API, graph, storage, and worker boundaries. Provider-native
payloads, credentials, signed URLs, and hidden reasoning are deliberately excluded.
"""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

MAX_REQUEST_BYTES = 2 * 1024 * 1024
MAX_PROVIDER_OUTPUT_BYTES = 4 * 1024 * 1024
MAX_TEXT_PART_CHARS = 100_000
MAX_PARTS_PER_MESSAGE = 32
MAX_PARTS_PER_RUN = 256
MAX_TOOL_RESULT_DEPTH = 4
MAX_TEMP_VISIBLE_MESSAGES = 50


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, protected_namespaces=())


class TextPart(_StrictModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False, protected_namespaces=())

    type: Literal["text"]
    text: str = Field(min_length=1, max_length=MAX_TEXT_PART_CHARS)


class ImagePart(_StrictModel):
    type: Literal["image"]
    asset_id: str = Field(min_length=1, max_length=36)
    mime_type: str = Field(min_length=1, max_length=127)
    name: str = Field(min_length=1, max_length=255)
    width: int = Field(ge=1)
    height: int = Field(ge=1)
    alt_text: str | None = Field(default=None, max_length=2_000)


class AudioPart(_StrictModel):
    type: Literal["audio"]
    asset_id: str = Field(min_length=1, max_length=36)
    mime_type: str = Field(min_length=1, max_length=127)
    name: str = Field(min_length=1, max_length=255)
    duration_ms: int = Field(ge=0)
    transcript: str | None = Field(default=None, max_length=MAX_TEXT_PART_CHARS)


class VideoPart(_StrictModel):
    type: Literal["video"]
    asset_id: str = Field(min_length=1, max_length=36)
    mime_type: str = Field(min_length=1, max_length=127)
    name: str = Field(min_length=1, max_length=255)
    duration_ms: int = Field(ge=0)
    width: int = Field(ge=1)
    height: int = Field(ge=1)


class DocumentPart(_StrictModel):
    type: Literal["document"]
    asset_id: str = Field(min_length=1, max_length=36)
    mime_type: str = Field(min_length=1, max_length=127)
    name: str = Field(min_length=1, max_length=255)
    page_count: int = Field(ge=1)


class FilePart(_StrictModel):
    type: Literal["file"]
    asset_id: str = Field(min_length=1, max_length=36)
    mime_type: str = Field(min_length=1, max_length=127)
    name: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(ge=0)


class ToolCallPart(_StrictModel):
    type: Literal["tool_call"]
    call_id: str = Field(min_length=1, max_length=190)
    name: str = Field(min_length=1, max_length=190)
    arguments: dict[str, Any]
    status: Literal["pending", "running", "completed", "failed", "denied"]


class ToolResultPart(_StrictModel):
    type: Literal["tool_result"]
    call_id: str = Field(min_length=1, max_length=190)
    name: str = Field(min_length=1, max_length=190)
    content: list[ChatPart] = Field(min_length=1, max_length=MAX_PARTS_PER_MESSAGE)
    is_error: bool


class CitationPart(_StrictModel):
    type: Literal["citation"]
    citation_id: str = Field(min_length=1, max_length=190)
    url: str | None = Field(default=None, max_length=4_096)
    source_kind: Literal["web", "document"] = "web"
    document_index: int | None = Field(default=None, ge=0)
    title: str | None = Field(default=None, max_length=1_000)
    snippet: str | None = Field(default=None, max_length=10_000)
    start_index: int | None = Field(default=None, ge=0)
    end_index: int | None = Field(default=None, ge=0)

    @field_validator("url")
    @classmethod
    def validate_http_url(cls, value: str | None) -> str | None:
        if value is None:
            return value
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("citation url must be an absolute http(s) URL")
        return value

    @model_validator(mode="after")
    def validate_inline_range(self) -> CitationPart:
        if (self.start_index is None) != (self.end_index is None):
            raise ValueError("citation inline range requires both start_index and end_index")
        if self.start_index is not None and self.end_index is not None and self.start_index > self.end_index:
            raise ValueError("citation start_index must not exceed end_index")
        if self.source_kind == "web" and self.url is None:
            raise ValueError("web citation requires an absolute URL")
        if self.source_kind == "document" and self.document_index is None:
            raise ValueError("document citation requires document_index")
        if self.url is None and self.document_index is None:
            raise ValueError("citation requires a URL or document_index")
        return self


class StructuredPart(_StrictModel):
    type: Literal["structured"]
    schema_name: str = Field(min_length=1, max_length=190)
    schema_version: str = Field(min_length=1, max_length=64)
    value: Any
    valid: bool
    validation_error: str | None = Field(default=None, max_length=2_000)


class ReasoningPart(_StrictModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False, protected_namespaces=())

    type: Literal["reasoning"]
    text: str = Field(min_length=1, max_length=MAX_TEXT_PART_CHARS)
    visibility: Literal["user"]


ChatPart = Annotated[
    TextPart
    | ImagePart
    | AudioPart
    | VideoPart
    | DocumentPart
    | FilePart
    | ToolCallPart
    | ToolResultPart
    | CitationPart
    | StructuredPart
    | ReasoningPart,
    Field(discriminator="type"),
]
ToolResultPart.model_rebuild()
ChatParts = list[ChatPart]
_CHAT_PARTS = TypeAdapter(ChatParts)


class UserTextInputPart(TextPart):
    pass


class UserAssetInputPart(_StrictModel):
    type: Literal["image", "audio", "video", "document"]
    asset_id: str = Field(min_length=1, max_length=36)


UserInputPart = Annotated[UserTextInputPart | UserAssetInputPart, Field(discriminator="type")]
UserInputParts = list[UserInputPart]
_USER_INPUT_PARTS = TypeAdapter(UserInputParts)


def validate_chat_parts(parts: object, *, max_parts: int = MAX_PARTS_PER_MESSAGE) -> ChatParts:
    """Validate canonical provider/server parts and enforce bounded tool-result recursion."""
    parsed = _CHAT_PARTS.validate_python(parts)
    if len(parsed) > max_parts:
        raise ValueError(f"message has more than {max_parts} parts")

    def visit(items: ChatParts, depth: int) -> None:
        if depth > MAX_TOOL_RESULT_DEPTH:
            raise ValueError(f"tool_result nesting exceeds {MAX_TOOL_RESULT_DEPTH}")
        for part in items:
            if isinstance(part, ToolResultPart):
                visit(part.content, depth + 1)

    visit(parsed, 0)
    return parsed


def validate_user_input_parts(parts: object) -> UserInputParts:
    parsed = _USER_INPUT_PARTS.validate_python(parts)
    if not parsed:
        raise ValueError("at least one input part is required")
    if len(parsed) > MAX_PARTS_PER_MESSAGE:
        raise ValueError(f"message has more than {MAX_PARTS_PER_MESSAGE} parts")
    return parsed


class UnsupportedInputPartError(ValueError):
    """The canonical wire part is valid but its execution capability is unavailable."""


def text_from_user_input_parts(parts: object) -> str:
    """Materialize a text-only provider request without silently dropping owned assets."""
    parsed = validate_user_input_parts(parts)
    unsupported = [part.type for part in parsed if not isinstance(part, UserTextInputPart)]
    if unsupported:
        raise UnsupportedInputPartError(
            f"input capabilities are unavailable for: {', '.join(sorted(set(unsupported)))}"
        )
    return "\n".join(part.text for part in parsed)


def text_projection_from_user_input_parts(parts: object) -> str:
    """Return the durable plaintext projection without discarding canonical asset refs."""
    parsed = validate_user_input_parts(parts)
    return "\n".join(part.text for part in parsed if isinstance(part, UserTextInputPart))


_DOMAIN_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_LOCATION_KEYS = {"country", "region", "city", "timezone"}


def _normalize_domain(value: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("domain must be a non-empty hostname")
    if any(character in value for character in "/:@?#[\\]") or "*" in value:
        raise ValueError("domain must be a hostname without URL syntax or wildcards")
    try:
        domain = value.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ValueError("domain is not valid IDNA") from exc
    labels = domain.split(".")
    if len(domain) > 253 or len(labels) < 2 or any(not _DOMAIN_LABEL_RE.fullmatch(label) for label in labels):
        raise ValueError("domain is not valid")
    return domain


def _domain_sets_overlap(left: str, right: str) -> bool:
    return left == right or left.endswith(f".{right}") or right.endswith(f".{left}")


def _normalize_domains(values: list[str]) -> list[str]:
    normalized = [_normalize_domain(value) for value in values]
    if len(normalized) != len(set(normalized)):
        raise ValueError("domain list contains duplicates")
    return normalized


def _validate_location(value: dict[str, str] | None) -> dict[str, str] | None:
    if value is None:
        return None
    if not value or set(value) - _LOCATION_KEYS:
        raise ValueError("approximate_location has unsupported fields")
    normalized: dict[str, str] = {}
    for key, item in value.items():
        if (
            not isinstance(item, str)
            or not item.strip()
            or len(item) > 128
            or any(character in item for character in "\r\n")
        ):
            raise ValueError("approximate_location values must be bounded single-line strings")
        normalized[key] = item.strip()
    return normalized


class WebSearchOptions(_StrictModel):
    enabled: bool = False
    context_size: Literal["low", "medium", "high"] = "medium"
    allowed_domains: list[str] = Field(default_factory=list, max_length=10)
    blocked_domains: list[str] = Field(default_factory=list, max_length=10)
    approximate_location: dict[str, str] | None = None
    max_uses: int = Field(default=1, ge=1, le=5)
    provider_id: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_search_scope(self) -> WebSearchOptions:
        if self.enabled and self.provider_id is None:
            raise ValueError("web_search requires provider_id when enabled")
        self.allowed_domains = _normalize_domains(self.allowed_domains)
        self.blocked_domains = _normalize_domains(self.blocked_domains)
        if any(
            _domain_sets_overlap(allowed, blocked)
            for allowed in self.allowed_domains
            for blocked in self.blocked_domains
        ):
            raise ValueError("allowed_domains and blocked_domains must not overlap")
        self.approximate_location = _validate_location(self.approximate_location)
        return self


class WebFetchOptions(_StrictModel):
    enabled: bool = False
    allowed_domains: list[str] = Field(default_factory=list, max_length=10)
    blocked_domains: list[str] = Field(default_factory=list, max_length=10)
    max_uses: int = Field(default=1, ge=1, le=5)

    @model_validator(mode="after")
    def validate_fetch_scope(self) -> WebFetchOptions:
        self.allowed_domains = _normalize_domains(self.allowed_domains)
        self.blocked_domains = _normalize_domains(self.blocked_domains)
        if any(
            _domain_sets_overlap(allowed, blocked)
            for allowed in self.allowed_domains
            for blocked in self.blocked_domains
        ):
            raise ValueError("allowed_domains and blocked_domains must not overlap")
        return self


class AdvisorOptions(_StrictModel):
    enabled: bool = False
    max_uses: int = Field(default=1, ge=1, le=3)
    model_id: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def require_model_when_enabled(self) -> AdvisorOptions:
        if self.enabled and self.model_id is None:
            raise ValueError("advisor requires model_id when enabled")
        return self


_STRUCTURED_SCHEMA_KEYS = {
    "type",
    "properties",
    "required",
    "additionalProperties",
    "items",
    "enum",
    "const",
    "minLength",
    "maxLength",
    "minItems",
    "maxItems",
    "minimum",
    "maximum",
    "description",
}
_MAX_STRUCTURED_SCHEMA_DEPTH = 64
_MAX_STRUCTURED_SCHEMA_PROPERTIES = 200


def _validate_structured_schema(value: object, *, depth: int = 0) -> None:
    if not isinstance(value, dict):
        raise ValueError("json_schema schema must be an object")
    if depth > _MAX_STRUCTURED_SCHEMA_DEPTH:
        raise ValueError("json_schema exceeds the maximum nesting depth")
    unknown = set(value) - _STRUCTURED_SCHEMA_KEYS
    if unknown:
        raise ValueError(f"json_schema contains unsupported keywords: {', '.join(sorted(unknown))}")
    if "type" in value and value["type"] not in {"object", "array", "string", "number", "integer", "boolean", "null"}:
        raise ValueError("json_schema type is invalid")
    properties = value.get("properties")
    if properties is not None:
        if not isinstance(properties, dict) or len(properties) > _MAX_STRUCTURED_SCHEMA_PROPERTIES:
            raise ValueError("json_schema properties are invalid")
        for name, child in properties.items():
            if not isinstance(name, str) or not name:
                raise ValueError("json_schema property names must be non-empty strings")
            _validate_structured_schema(child, depth=depth + 1)
    required = value.get("required")
    if required is not None and (
        not isinstance(required, list)
        or any(not isinstance(name, str) for name in required)
        or len(set(required)) != len(required)
        or (isinstance(properties, dict) and not set(required) <= set(properties))
    ):
        raise ValueError("json_schema required is invalid")
    if "additionalProperties" in value and not isinstance(value["additionalProperties"], bool):
        raise ValueError("json_schema additionalProperties must be boolean")
    if "items" in value:
        _validate_structured_schema(value["items"], depth=depth + 1)
    if "enum" in value and (
        not isinstance(value["enum"], list)
        or not value["enum"]
        or any(isinstance(item, (dict, list)) for item in value["enum"])
    ):
        raise ValueError("json_schema enum is invalid")
    if "const" in value and isinstance(value["const"], (dict, list)):
        raise ValueError("json_schema const must be scalar")
    for key in ("minLength", "maxLength", "minItems", "maxItems"):
        if key in value and (not isinstance(value[key], int) or isinstance(value[key], bool) or value[key] < 0):
            raise ValueError(f"json_schema {key} must be a non-negative integer")
    for key in ("minimum", "maximum"):
        if key in value and (not isinstance(value[key], (int, float)) or isinstance(value[key], bool)):
            raise ValueError(f"json_schema {key} must be numeric")
    if "description" in value and (not isinstance(value["description"], str) or len(value["description"]) > 2_000):
        raise ValueError("json_schema description is invalid")


class ResponseFormat(_StrictModel):
    kind: Literal["text", "json_object", "json_schema"] = "text"
    name: str | None = Field(default=None, min_length=1, max_length=190)
    version: str | None = Field(default=None, min_length=1, max_length=64)
    schema_: dict[str, Any] | None = Field(default=None, alias="schema")

    @model_validator(mode="after")
    def validate_schema_format(self) -> ResponseFormat:
        if self.kind == "json_schema" and (not self.name or not self.version or self.schema_ is None):
            raise ValueError("json_schema requires name, version, and schema")
        if self.kind != "json_schema" and any(value is not None for value in (self.name, self.version, self.schema_)):
            raise ValueError("schema metadata is only valid for json_schema")
        if self.kind == "json_schema":
            _validate_structured_schema(self.schema_)
        return self


class ToolPolicy(_StrictModel):
    mode: Literal["agent_default", "none"] = "agent_default"
    approval_mode: Literal["required_for_mutations", "always"] = "required_for_mutations"
    enabled_tool_ids: list[int] | None = Field(default=None, max_length=100)
    enabled_mcp_ids: list[int] | None = Field(default=None, max_length=100)
    workspace_write_mode: Literal["ask", "auto_edit"] = "ask"

    @field_validator("enabled_tool_ids", "enabled_mcp_ids")
    @classmethod
    def validate_enabled_ids(cls, value: list[int] | None) -> list[int] | None:
        if value is None:
            return None
        if len(set(value)) != len(value) or any(item < 1 for item in value):
            raise ValueError("enabled extension IDs must be unique positive ids")
        return value


class ImageOutputOptions(_StrictModel):
    count: int = Field(default=1, ge=1, le=4)
    aspect_ratio: Literal["1:1", "16:9", "9:16"] = "1:1"
    quality: Literal["standard", "high"] = "standard"


class AudioOutputOptions(_StrictModel):
    voice: str = Field(min_length=1, max_length=190)
    format: Literal["mp3", "wav", "opus"]


class VideoOutputOptions(_StrictModel):
    duration_seconds: int = Field(ge=1, le=60)
    aspect_ratio: Literal["16:9", "9:16"] = "16:9"


class ChatFeatureOptions(_StrictModel):
    web_search: WebSearchOptions = Field(default_factory=WebSearchOptions)
    web_fetch: WebFetchOptions = Field(default_factory=WebFetchOptions)
    advisor: AdvisorOptions = Field(default_factory=AdvisorOptions)
    memory: bool = True
    response_format: ResponseFormat = Field(default_factory=ResponseFormat)
    tool_policy: ToolPolicy = Field(default_factory=ToolPolicy)
    output_modalities: list[Literal["text", "image", "audio", "video"]] = Field(
        default_factory=lambda: ["text"], min_length=1, max_length=4
    )
    image_output: ImageOutputOptions | None = None
    audio_output: AudioOutputOptions | None = None
    video_output: VideoOutputOptions | None = None

    @model_validator(mode="after")
    def validate_output_options(self) -> ChatFeatureOptions:
        requested = set(self.output_modalities)
        if len(requested) != len(self.output_modalities):
            raise ValueError("output_modalities must not contain duplicates")
        if ("image" in requested) != (self.image_output is not None):
            raise ValueError("image_output must be supplied exactly when image output is requested")
        if ("audio" in requested) != (self.audio_output is not None):
            raise ValueError("audio_output must be supplied exactly when audio output is requested")
        if ("video" in requested) != (self.video_output is not None):
            raise ValueError("video_output must be supplied exactly when video output is requested")
        return self


ReasoningEffort = Literal["auto", "none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"]


class _ReasoningEffortRequest(_StrictModel):
    reasoning_effort: ReasoningEffort = "auto"
    client_timezone: str | None = Field(default=None, max_length=64)

    @field_validator("client_timezone")
    @classmethod
    def validate_client_timezone(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("client_timezone must be a valid IANA timezone") from exc
        return value


class CompletionRequest(_ReasoningEffortRequest):
    """Canonical client intent; routing, pricing, and tool resolution are server snapshots."""

    parts: UserInputParts = Field(min_length=1, max_length=MAX_PARTS_PER_MESSAGE)
    model_id: str = Field(min_length=1, max_length=190)
    features: ChatFeatureOptions = Field(default_factory=ChatFeatureOptions)
    agent_id: str | None = Field(default=None, max_length=36)
    execution_mode: Literal["chat", "plan", "code"] = "chat"
    code_workspace_id: str | None = Field(default=None, max_length=36)
    skill_ids: list[int] = Field(default_factory=list, max_length=100)

    @field_validator("skill_ids")
    @classmethod
    def validate_skill_ids(cls, value: list[int]) -> list[int]:
        if len(set(value)) != len(value) or any(item < 1 for item in value):
            raise ValueError("skill_ids must be unique positive ids")
        return value

    @model_validator(mode="after")
    def validate_execution_mode(self) -> CompletionRequest:
        if self.execution_mode == "chat" and (
            self.code_workspace_id is not None or self.features.tool_policy.workspace_write_mode != "ask"
        ):
            raise ValueError("chat mode cannot select a code workspace or auto-edit")
        if self.execution_mode != "code" and self.features.tool_policy.workspace_write_mode == "auto_edit":
            raise ValueError("auto_edit is only available in code mode")
        return self

    @field_validator("parts")
    @classmethod
    def validate_parts(cls, value: UserInputParts) -> UserInputParts:
        return validate_user_input_parts([part.model_dump() for part in value])


class RegenerateRequest(_ReasoningEffortRequest):
    model_id: str = Field(min_length=1, max_length=190)
    features: ChatFeatureOptions = Field(default_factory=ChatFeatureOptions)


class ChatRunDescriptor(_StrictModel):
    run_id: str = Field(min_length=1, max_length=36)
    conversation_id: str | None = Field(default=None, max_length=36)
    temp_thread_id: str | None = Field(default=None, max_length=36)
    status: Literal[
        "queued",
        "running",
        "awaiting_approval",
        "awaiting_input",
        "waiting_children",
        "finalizing",
        "completed",
        "failed",
        "canceled",
    ]
    events_url: str = Field(min_length=1, max_length=512)
    cancel_url: str = Field(min_length=1, max_length=512)


class ChatRunResponse(_StrictModel):
    run_id: str = Field(min_length=1, max_length=36)
    status: Literal[
        "queued",
        "running",
        "awaiting_approval",
        "awaiting_input",
        "waiting_children",
        "finalizing",
        "completed",
        "failed",
        "canceled",
    ]
    conversation_id: str | None = Field(default=None, max_length=36)
    temp_thread_id: str | None = Field(default=None, max_length=36)
    effective_features: dict[str, Any] = Field(default_factory=dict)
    public_history: list[dict[str, Any]] | None = None
    last_seq: int = Field(ge=0)
    terminal: bool


_DECIMAL_UNITS = {"token", "request", "context", "image", "second", "usd"}
_USAGE_KINDS = {
    "input_tokens",
    "output_tokens",
    "cached_input_tokens",
    "reasoning_tokens",
    "embedding_tokens",
    "web_search_requests",
    "web_search_context",
    "web_fetch_requests",
    "web_fetch_context",
    "advisor_input_tokens",
    "advisor_output_tokens",
    "image_units",
    "audio_input_seconds",
    "audio_output_seconds",
    "video_seconds",
    "sandbox_seconds",
    "provider_adjustment",
}
_USAGE_SOURCES = {"executor", "advisor", "search", "fetch", "memory", "media", "sandbox", "system"}


def _validate_decimal_string(value: str, *, allow_negative: bool = False) -> str:
    try:
        decimal = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("must be a Decimal string") from exc
    if not decimal.is_finite() or (decimal < 0 and not allow_negative):
        raise ValueError("must be a finite non-negative Decimal string")
    return format(decimal, "f")


class UsageComponent(_StrictModel):
    segment_id: str = Field(min_length=1, max_length=190)
    kind: str
    quantity: str
    unit: str
    unit_price_usd: str
    cost_usd: str
    source: str
    model_name: str = Field(min_length=1, max_length=190)
    metadata: dict[str, str | int | float | bool | None] = Field(default_factory=dict)

    @field_validator("kind")
    @classmethod
    def validate_kind(cls, value: str) -> str:
        if value not in _USAGE_KINDS:
            raise ValueError("unsupported usage kind")
        return value

    @field_validator("source")
    @classmethod
    def validate_source(cls, value: str) -> str:
        if value not in _USAGE_SOURCES:
            raise ValueError("unsupported usage source")
        return value

    @field_validator("unit")
    @classmethod
    def validate_unit(cls, value: str) -> str:
        if value not in _DECIMAL_UNITS:
            raise ValueError("unsupported usage unit")
        return value

    @field_validator("quantity", "unit_price_usd")
    @classmethod
    def validate_nonnegative_decimal(cls, value: str) -> str:
        return _validate_decimal_string(value)

    @field_validator("cost_usd")
    @classmethod
    def validate_cost_decimal(cls, value: str, info: Any) -> str:
        kind = info.data.get("kind")
        return _validate_decimal_string(value, allow_negative=kind == "provider_adjustment")


class RunStartedPayload(_StrictModel):
    conversation_id: str | None = None
    temp_thread_id: str | None = None
    model_name: str = Field(min_length=1, max_length=190)
    effective_features: dict[str, Any]


class RunStageChangedPayload(_StrictModel):
    stage: Literal[
        "queued",
        "model_request",
        "model_response",
        "tool_execution",
        "response_writing",
        "awaiting_input",
        "finalizing",
    ]
    tool_name: str | None = Field(default=None, min_length=1, max_length=190)

    @model_validator(mode="after")
    def validate_tool_stage(self) -> RunStageChangedPayload:
        if self.stage == "tool_execution" and self.tool_name is None:
            raise ValueError("tool_execution stage requires tool_name")
        if self.stage != "tool_execution" and self.tool_name is not None:
            raise ValueError("only tool_execution stage may include tool_name")
        return self


class RunWarningPayload(_StrictModel):
    code: str = Field(min_length=1, max_length=100)
    safe_message: str = Field(min_length=1, max_length=2_000)


class MessageCreatedPayload(_StrictModel):
    message_id: str = Field(min_length=1, max_length=190)
    role: Literal["assistant", "tool"]
    parent_id: str | None = None


class PartDeltaPayload(_StrictModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False, protected_namespaces=())

    message_id: str = Field(min_length=1, max_length=190)
    part_index: int = Field(ge=0)
    part_type: Literal["text", "reasoning"]
    delta: str = Field(min_length=1, max_length=MAX_TEXT_PART_CHARS)


class PartCompletedPayload(_StrictModel):
    message_id: str = Field(min_length=1, max_length=190)
    part_index: int = Field(ge=0)
    part: ChatPart


class ToolCallStartedPayload(_StrictModel):
    call_id: str = Field(min_length=1, max_length=190)
    name: str = Field(min_length=1, max_length=190)
    arguments: dict[str, Any]
    source: Literal["builtin", "managed", "custom_http", "mcp", "workspace", "agent"] = "builtin"
    category: str = Field(default="기본 도구", min_length=1, max_length=100)


class ToolCallCompletedPayload(_StrictModel):
    call_id: str = Field(min_length=1, max_length=190)
    name: str = Field(min_length=1, max_length=190)
    content: ChatParts = Field(min_length=1, max_length=MAX_PARTS_PER_MESSAGE)
    status: Literal["completed", "failed"]
    error_code: str | None = Field(default=None, max_length=100)
    source: Literal["builtin", "managed", "custom_http", "mcp", "workspace", "agent"] = "builtin"
    category: str = Field(default="기본 도구", min_length=1, max_length=100)


class ToolApprovalRequiredPayload(_StrictModel):
    call_id: str = Field(min_length=1, max_length=190)
    name: str = Field(min_length=1, max_length=190)
    source: Literal["builtin", "managed", "custom_http", "mcp", "workspace", "agent"]
    effect: Literal["read", "workspace_write", "process", "external_mutation"]
    destination: str | None = Field(default=None, max_length=255)
    redacted_arguments: dict[str, Any] = Field(max_length=256)
    preview: ChatParts = Field(max_length=MAX_PARTS_PER_MESSAGE)
    expected_state_revision: int | None = Field(default=None, ge=0)
    writer_fence: int | None = Field(default=None, ge=0)
    expires_at: datetime


class ToolApprovalResolvedPayload(_StrictModel):
    call_id: str = Field(min_length=1, max_length=190)
    decision: Literal["approve", "deny"]
    decided_by_user_id: str | None = Field(max_length=64)
    decided_at: datetime


class RunInteractionResponsePayload(_StrictModel):
    option_ids: list[str] = Field(max_length=5)
    text: str | None = Field(default=None, max_length=4_000)

    @field_validator("option_ids")
    @classmethod
    def validate_option_ids(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value) or any(not option_id for option_id in value):
            raise ValueError("interaction option ids must be unique non-empty strings")
        return value


class RunInteractionResolvedPayload(_StrictModel):
    interaction_id: str = Field(min_length=1, max_length=36)
    status: Literal["answered", "timeout", "canceled"]
    response: RunInteractionResponsePayload | None = None

    @model_validator(mode="after")
    def validate_response_for_status(self) -> RunInteractionResolvedPayload:
        if (self.status == "answered") != (self.response is not None):
            raise ValueError("answered interactions require a response and terminal non-answers omit it")
        return self


class UsageUpdatedPayload(_StrictModel):
    components: list[UsageComponent] = Field(max_length=MAX_PARTS_PER_RUN)
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    raw_cost: str
    credited_cost: str

    @field_validator("raw_cost", "credited_cost")
    @classmethod
    def validate_usage_total(cls, value: str) -> str:
        return _validate_decimal_string(value)


class RunCompletedPayload(_StrictModel):
    status: Literal["completed"]
    message_id: str | None = Field(default=None, max_length=190)


class RunFailedPayload(_StrictModel):
    status: Literal["failed"]
    message_id: str | None = Field(default=None, max_length=190)
    error_code: str = Field(min_length=1, max_length=100)
    safe_message: str = Field(min_length=1, max_length=2_000)


class RunCanceledPayload(_StrictModel):
    status: Literal["canceled"]
    message_id: str | None = Field(default=None, max_length=190)
    error_code: str = Field(min_length=1, max_length=100)
    safe_message: str = Field(min_length=1, max_length=2_000)


class _RunEvent(_StrictModel):
    event_id: str = Field(min_length=3, max_length=256)
    run_id: str = Field(min_length=1, max_length=36)
    seq: int = Field(ge=1)
    created_at: datetime

    @model_validator(mode="after")
    def validate_event_id(self) -> _RunEvent:
        if self.event_id != f"{self.run_id}:{self.seq}":
            raise ValueError("event_id must equal <run_id>:<seq>")
        return self


class RunStartedEvent(_RunEvent):
    type: Literal["run.started"]
    payload: RunStartedPayload


class RunStageChangedEvent(_RunEvent):
    type: Literal["run.stage.changed"]
    payload: RunStageChangedPayload


class RunWarningEvent(_RunEvent):
    type: Literal["run.warning"]
    payload: RunWarningPayload


class MessageCreatedEvent(_RunEvent):
    type: Literal["message.created"]
    payload: MessageCreatedPayload


class PartDeltaEvent(_RunEvent):
    type: Literal["part.delta"]
    payload: PartDeltaPayload


class PartCompletedEvent(_RunEvent):
    type: Literal["part.completed"]
    payload: PartCompletedPayload


class ToolCallStartedEvent(_RunEvent):
    type: Literal["tool.call.started"]
    payload: ToolCallStartedPayload


class ToolCallCompletedEvent(_RunEvent):
    type: Literal["tool.call.completed"]
    payload: ToolCallCompletedPayload


class ToolApprovalRequiredEvent(_RunEvent):
    type: Literal["tool.approval_required"]
    payload: ToolApprovalRequiredPayload


class ToolApprovalResolvedEvent(_RunEvent):
    type: Literal["tool.approval_resolved"]
    payload: ToolApprovalResolvedPayload


class RunInteractionResolvedEvent(_RunEvent):
    type: Literal["interaction.resolved"]
    payload: RunInteractionResolvedPayload


class UsageUpdatedEvent(_RunEvent):
    type: Literal["usage.updated"]
    payload: UsageUpdatedPayload


class RunCompletedEvent(_RunEvent):
    type: Literal["run.completed"]
    payload: RunCompletedPayload


class RunFailedEvent(_RunEvent):
    type: Literal["run.failed"]
    payload: RunFailedPayload


class RunCanceledEvent(_RunEvent):
    type: Literal["run.canceled"]
    payload: RunCanceledPayload


ChatRunEvent = Annotated[
    RunStartedEvent
    | RunStageChangedEvent
    | RunWarningEvent
    | MessageCreatedEvent
    | PartDeltaEvent
    | PartCompletedEvent
    | ToolCallStartedEvent
    | ToolCallCompletedEvent
    | ToolApprovalRequiredEvent
    | ToolApprovalResolvedEvent
    | RunInteractionResolvedEvent
    | UsageUpdatedEvent
    | RunCompletedEvent
    | RunFailedEvent
    | RunCanceledEvent,
    Field(discriminator="type"),
]
_CHAT_RUN_EVENT = TypeAdapter(ChatRunEvent)


def validate_chat_run_event(event: object) -> ChatRunEvent:
    return _CHAT_RUN_EVENT.validate_python(event)


class UsageRecord(_StrictModel):
    id: int
    created_at: datetime
    model_name: str
    provider: str | None = None
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    credited_cost: str
    source: Literal["web", "api"]
    api_key_id: int | None = None
    conversation_id: str | None = None
    run_id: str | None = None
    pricing_status: str


class UsageRecordPage(_StrictModel):
    records: list[UsageRecord]
    next_before_id: int | None = None


class TempCompletionRequest(BaseModel):
    """Canonical first-turn temporary completion request."""

    model_config = ConfigDict(protected_namespaces=())

    parts: list[UserInputPart] = Field(min_length=1, max_length=32)
    model_id: str = Field(min_length=1, max_length=190)
    features: ChatFeatureOptions = Field(default_factory=ChatFeatureOptions)
    reasoning_effort: ReasoningEffort = "auto"
    execution_mode: str = Field(default="chat", pattern="^(chat|plan|code)$")
    code_workspace_id: str | None = Field(default=None, max_length=36)
    skill_ids: list[int] = Field(default_factory=list, max_length=100)
    temp_thread_id: str | None = Field(default=None, max_length=36)

    @field_validator("skill_ids")
    @classmethod
    def validate_skill_ids(cls, value: list[int]) -> list[int]:
        if len(set(value)) != len(value) or any(item < 1 for item in value):
            raise ValueError("skill_ids must be unique positive ids")
        return value

    @model_validator(mode="after")
    def validate_execution_mode(self) -> TempCompletionRequest:
        if self.execution_mode == "chat" and (
            self.code_workspace_id is not None or self.features.tool_policy.workspace_write_mode != "ask"
        ):
            raise ValueError("chat mode cannot select a code workspace or auto-edit")
        if self.execution_mode != "code" and self.features.tool_policy.workspace_write_mode == "auto_edit":
            raise ValueError("auto_edit is only available in code mode")
        return self


class ToolApprovalDecisionRequest(BaseModel):
    decision: Literal["approve", "deny"]


class RunInteractionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    option_ids: list[str] = Field(default_factory=list, max_length=5)
    text: str | None = Field(default=None, max_length=4_000)

    @field_validator("option_ids")
    @classmethod
    def validate_option_ids(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value) or any(not option_id for option_id in value):
            raise ValueError("option_ids must be unique non-empty strings")
        return value


class RunInteractionResponseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    response: RunInteractionResponse
