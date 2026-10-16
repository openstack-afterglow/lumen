"""Shared request and response schemas; workers never import HTTP routes."""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class OpenAIChatRequest(BaseModel):
    model: str = Field(..., max_length=190)
    provider: str | None = Field(
        default=None,
        min_length=1,
        max_length=40,
        pattern=r"^[a-z0-9][a-z0-9_-]*$",
    )
    messages: list[dict] = Field(..., min_length=1)
    stream: bool = False
    temperature: float | None = None
    max_tokens: int | None = None
    tools: list[dict] | None = None
    tool_choice: Any = None
    stream_options: dict | None = None

    model_config = {"extra": "allow"}  # 미지원 OpenAI 파라미터는 무시(호환성)


class OpenAIChatChoiceMessage(BaseModel):
    role: str = "assistant"
    content: str | None = None
    tool_calls: list[dict] | None = None


class OpenAIChatChoice(BaseModel):
    index: int = 0
    message: OpenAIChatChoiceMessage
    finish_reason: str = "stop"


class OpenAIUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    prompt_tokens_details: dict[str, int] = Field(default_factory=lambda: {"cached_tokens": 0})


class OpenAIChatResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: list[OpenAIChatChoice]
    usage: OpenAIUsage


class ResponsesRequest(BaseModel):
    model: str = Field(..., max_length=190)
    provider: str | None = Field(default=None, min_length=1, max_length=40, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    input: str | list[dict[str, Any]]
    stream: bool = False
    store: bool | None = None
    previous_response_id: str | None = None
    background: bool | None = None
    include: list[str] | None = None
    prompt_cache_key: str | None = None
    client_metadata: dict[str, Any] | None = None
    instructions: str | None = None
    max_output_tokens: int | None = Field(default=None, gt=0)
    metadata: dict[str, Any] | None = None
    parallel_tool_calls: bool | None = None
    reasoning: dict[str, Any] | None = None
    temperature: float | None = None
    text: dict[str, Any] | None = None
    tool_choice: Any = None
    tools: list[dict[str, Any]] | None = None
    top_p: float | None = None
    truncation: Literal["auto", "disabled"] | None = None
    user: str | None = None
    service_tier: str | None = None
    safety_identifier: str | None = None
    # Accepting this lets a caller keep its own context strategy. Lumen only
    # supplies a default when the field is absent; it never overrides one.
    context_management: list[dict[str, Any]] | None = None

    model_config = {"extra": "forbid"}


class ImageGenerationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_id: str = Field(min_length=1, max_length=190)
    provider_id: int | None = None
    prompt: str = Field(min_length=1)
    size: str = "auto"
    quality: str = "auto"
    n: int = Field(default=1, ge=1)


class ImageEditRequest(ImageGenerationRequest):
    input_asset_id: UUID


class OpenAIImageGenerationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str = Field(min_length=1, max_length=190)
    provider: str | None = None
    provider_id: int | None = None
    prompt: str = Field(min_length=1)
    n: int = Field(default=1, ge=1)
    size: str = "auto"
    quality: str = "auto"
    response_format: str = "b64_json"


class SpeechRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_id: str = Field(min_length=1, max_length=190)
    provider_id: int | str | None = None

    input: str = Field(min_length=1, max_length=4096)
    voice: str = Field(min_length=1, max_length=190)
    response_format: str = "mp3"

    @field_validator("model_id", "input", "voice")
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("audio fields must not be blank")
        return value


class TranscriptionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_id: str = Field(min_length=1, max_length=190)
    provider_id: int | str | None = None
    input_asset_id: UUID
    language: str | None = Field(default=None, max_length=16, pattern=r"^[A-Za-z-]+$")
    prompt: str | None = Field(default=None, max_length=1024)
    # Omitted/[] keeps the plain transcript contract. One entry also rejects duplicates.
    timestamp_granularities: list[Literal["segment"]] = Field(default_factory=list, max_length=1)

    @field_validator("model_id")
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("audio model must not be blank")
        return value
