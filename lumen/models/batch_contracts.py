"""Native and OpenAI batch wire contracts shared by admission and workers."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from lumen.models.api_requests import (
    ImageEditRequest,
    ImageGenerationRequest,
    OpenAIChatRequest,
    OpenAIImageGenerationRequest,
    ResponsesRequest,
    SpeechRequest,
    TranscriptionRequest,
)

BatchOperation = Literal[
    "chat.completions", "responses", "images.generations", "images.edits", "audio.speech", "audio.transcriptions"
]
BatchStatus = Literal["validating", "in_progress", "finalizing", "completed", "failed", "cancelling", "cancelled", "expired"]
BatchItemStatus = Literal["pending", "queued", "running", "completed", "failed", "cancelled", "expired", "unknown"]
BATCH_ENDPOINTS = {
    "/v1/chat/completions": "chat.completions",
    "/v1/responses": "responses",
    "/v1/images/generations": "images.generations",
}
_FORBIDDEN_TRANSPORT_FIELDS = frozenset({
    "headers", "extra_headers", "api_key", "api_base", "base_url", "api_version", "credentials", "custom_llm_provider",
})
_NATIVE_MODELS = {
    "chat.completions": OpenAIChatRequest,
    "responses": ResponsesRequest,
    "images.generations": ImageGenerationRequest,
    "images.edits": ImageEditRequest,
    "audio.speech": SpeechRequest,
    "audio.transcriptions": TranscriptionRequest,
}


class _BatchModel(BaseModel):
    # custom_id is exact UTF-8; do not silently strip or case-fold it.
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)


def validate_batch_metadata(value: dict[str, str]) -> dict[str, str]:
    if len(value) > 16:
        raise ValueError("metadata supports at most 16 pairs")
    if any(not 1 <= len(key) <= 64 or len(item) > 512 for key, item in value.items()):
        raise ValueError("metadata keys must be 1..64 characters and values at most 512")
    return value


_ITEM_SCOPES: dict[tuple[str, str], tuple[str, ...]] = {
    ("native", "chat.completions"): ("compat:completions:write",),
    ("native", "responses"): ("compat:completions:write",),
    ("native", "images.generations"): ("native:images:write",),
    ("native", "images.edits"): ("native:images:write", "native:assets:read"),
    ("native", "audio.speech"): ("native:audio:write",),
    ("native", "audio.transcriptions"): ("native:audio:write", "native:assets:read"),
    ("openai", "chat.completions"): ("compat:completions:write",),
    ("openai", "responses"): ("compat:completions:write",),
    ("openai", "images.generations"): ("compat:images:write",),
}


def batch_item_scopes(operation: str, *, contract: Literal["native", "openai"]) -> tuple[str, ...]:
    """API-key scopes an item operation needs in addition to the batch write scope."""
    return _ITEM_SCOPES[(contract, operation)]


def validate_batch_body(operation: str, body: dict, *, contract: str) -> dict:
    """Pure schema validation; authorization, pricing and asset pins belong to admission."""
    forbidden = _FORBIDDEN_TRANSPORT_FIELDS.intersection(body)
    if forbidden:
        raise ValueError(f"batch transport overrides are not supported: {', '.join(sorted(forbidden))}")
    if "stream" in body and body["stream"] is not False:
        raise ValueError("batch requests must use stream=false")
    schema = OpenAIImageGenerationRequest if contract == "openai" and operation == "images.generations" else _NATIVE_MODELS[operation]
    validated = schema.model_validate(body).model_dump(mode="json", exclude_none=True)
    if operation in {"chat.completions", "responses"}:
        if body.get("model") == "lumen":
            raise ValueError("virtual conversation model=lumen is not a batch operation")
        if body.get("context_management"):
            raise ValueError("batch compaction has no bounded cost contract")
        if any(tool.get("type") != "function" for tool in validated.get("tools") or []):
            raise ValueError("batch supports function tool relay only")
        if body.get("store") or body.get("previous_response_id") or body.get("background"):
            raise ValueError("batch responses must be stateless")
    return validated


class NativeBatchItemRequest(_BatchModel):
    custom_id: str = Field(min_length=1, max_length=64)
    operation: BatchOperation
    body: dict[str, Any]

    @model_validator(mode="after")
    def valid_body(self) -> NativeBatchItemRequest:
        self.body = validate_batch_body(self.operation, self.body, contract="native")
        return self


class NativeBatchCreateRequest(_BatchModel):
    items: list[NativeBatchItemRequest] = Field(min_length=1)
    completion_window: Literal["24h"] = "24h"
    metadata: dict[str, str] = Field(default_factory=dict)

    _metadata = field_validator("metadata")(validate_batch_metadata)

    @field_validator("items")
    @classmethod
    def unique_custom_ids(cls, items: list[NativeBatchItemRequest]) -> list[NativeBatchItemRequest]:
        ids = [item.custom_id for item in items]
        if len(set(ids)) != len(ids):
            raise ValueError("custom_id must be unique within a batch")
        return items


class OpenAIBatchRow(_BatchModel):
    custom_id: str = Field(min_length=1, max_length=64)
    method: Literal["POST"]
    url: Literal["/v1/chat/completions", "/v1/responses", "/v1/images/generations"]
    body: dict[str, Any]

    @model_validator(mode="after")
    def valid_body(self) -> OpenAIBatchRow:
        self.body = validate_batch_body(BATCH_ENDPOINTS[self.url], self.body, contract="openai")
        return self


class OutputExpiresAfter(_BatchModel):
    anchor: Literal["created_at"]
    seconds: int = Field(ge=3600, le=30 * 86400)


class OpenAIBatchCreateRequest(_BatchModel):
    input_file_id: str = Field(pattern=r"^file-[0-9a-f]{32}$")
    endpoint: Literal["/v1/chat/completions", "/v1/responses", "/v1/images/generations"]
    completion_window: Literal["24h"]
    metadata: dict[str, str] = Field(default_factory=dict)
    output_expires_after: OutputExpiresAfter | None = None

    _metadata = field_validator("metadata")(validate_batch_metadata)


class BatchRequestCounts(_BatchModel):
    total: int = Field(ge=0)
    completed: int = Field(ge=0)
    failed: int = Field(ge=0)
    pending: int = Field(default=0, ge=0)
    queued: int = Field(default=0, ge=0)
    running: int = Field(default=0, ge=0)
    cancelled: int = Field(default=0, ge=0)
    expired: int = Field(default=0, ge=0)
    unknown: int = Field(default=0, ge=0)


class NativeBatchDescriptor(_BatchModel):
    id: UUID
    status: BatchStatus
    created_at: datetime
    expires_at: datetime
    request_counts: BatchRequestCounts
    metadata: dict[str, str]
    links: dict[str, str]
    errors: list[dict[str, Any]] = Field(default_factory=list, max_length=100)


class NativeBatchItemResponse(_BatchModel):
    custom_id: str
    ordinal: int = Field(ge=1)
    operation: BatchOperation
    run_id: UUID | None
    status: BatchItemStatus
    response: dict[str, Any] | None
    error: dict[str, Any] | None
    settlement_status: str


class NativeBatchItemPage(_BatchModel):
    items: list[NativeBatchItemResponse]
    next_cursor: int | None


class NativeBatchPage(_BatchModel):
    batches: list[NativeBatchDescriptor]
    next_cursor: UUID | None


# OpenAI-compatible wire objects. Timestamps are unix seconds; IDs carry the
# public `batch_`/`file-` prefixes over the internal UUID.
OPENAI_BATCH_ID_PREFIX = "batch_"
OPENAI_FILE_ID_PREFIX = "file-"


def openai_public_id(prefix: str, internal_id: str | UUID) -> str:
    return f"{prefix}{UUID(str(internal_id)).hex}"


def openai_internal_id(prefix: str, public_id: str) -> str | None:
    """Return the internal UUID string, or None for a malformed public ID."""
    if not public_id.startswith(prefix):
        return None
    raw = public_id[len(prefix):]
    if len(raw) != 32 or any(char not in "0123456789abcdef" for char in raw):
        return None
    return str(UUID(hex=raw))


def unix_seconds(value: datetime | None) -> int | None:
    if value is None:
        return None
    if value.tzinfo is None:
        # MariaDB DATETIME columns are naive UTC.
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp())


class OpenAIBatchError(BaseModel):
    code: str
    message: str
    param: str | None = None
    line: int | None = None


class OpenAIBatchErrors(BaseModel):
    object: Literal["list"] = "list"
    data: list[OpenAIBatchError]


class OpenAIBatchRequestCounts(BaseModel):
    total: int = Field(ge=0)
    completed: int = Field(ge=0)
    failed: int = Field(ge=0)


class OpenAIBatchObject(BaseModel):
    id: str
    object: Literal["batch"] = "batch"
    endpoint: str
    errors: OpenAIBatchErrors | None
    input_file_id: str
    completion_window: Literal["24h"] = "24h"
    status: BatchStatus
    output_file_id: str | None
    error_file_id: str | None
    created_at: int
    in_progress_at: int | None
    expires_at: int | None
    finalizing_at: int | None
    completed_at: int | None
    failed_at: int | None
    expired_at: int | None
    cancelling_at: int | None
    cancelled_at: int | None
    request_counts: OpenAIBatchRequestCounts
    metadata: dict[str, str] | None


class OpenAIBatchList(BaseModel):
    object: Literal["list"] = "list"
    data: list[OpenAIBatchObject]
    first_id: str | None
    last_id: str | None
    has_more: bool


class OpenAIFileObject(BaseModel):
    id: str
    object: Literal["file"] = "file"
    bytes: int = Field(ge=0)
    created_at: int
    filename: str
    purpose: Literal["batch", "batch_output"]
    status: Literal["uploaded", "processed", "error"]
    status_details: str | None = None
    expires_at: int | None


class OpenAIFileList(BaseModel):
    object: Literal["list"] = "list"
    data: list[OpenAIFileObject]
    first_id: str | None
    last_id: str | None
    has_more: bool


class OpenAIFileDeleted(BaseModel):
    id: str
    object: Literal["file"] = "file"
    deleted: Literal[True] = True
