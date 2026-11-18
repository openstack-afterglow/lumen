"""Shared request and response schemas; workers never import HTTP routes."""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class OpenAIPromptCacheOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ttl: Literal["30m", "12h"] | None = None
    mode: Literal["implicit", "explicit"] | None = None
    comparison_response_id: str | None = None


class ResponsesPromptCacheOptions(OpenAIPromptCacheOptions):
    prewarm: bool | None = None


class OpenAIJSONSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    description: str | None = None
    schema_: dict[str, Any] = Field(alias="schema")
    strict: bool | None = None


class OpenAIJSONSchemaFormat(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["json_schema"]
    json_schema: OpenAIJSONSchema


class OpenAIPlainFormat(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["json_object", "text"]


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
    max_completion_tokens: int | None = Field(default=None, gt=0, strict=True)
    reasoning_effort: str | None = None
    response_format: OpenAIJSONSchemaFormat | OpenAIPlainFormat | None = Field(
        default=None, discriminator="type"
    )
    service_tier: str | None = None
    prompt_cache_key: str | None = None
    prompt_cache_options: OpenAIPromptCacheOptions | None = None
    prompt_cache_retention: Literal["in_memory", "in-memory", "24h"] | None = None
    tools: list[dict] | None = None
    tool_choice: Any = None
    stream_options: dict | None = None

    model_config = {"extra": "allow"}  # 미지원 OpenAI 파라미터는 무시(호환성)

    def provider_options(self) -> dict[str, Any]:
        return self.model_dump(
            include={
                "reasoning_effort", "response_format", "max_completion_tokens", "service_tier",
                "prompt_cache_key", "prompt_cache_options", "prompt_cache_retention",
            },
            exclude_none=True, by_alias=True,
        )


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
    prompt_tokens_details: dict[str, Any] = Field(default_factory=lambda: {"cached_tokens": 0})


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
    conversation: str | dict[str, Any] | None = None
    background: bool | None = None
    include: list[str] | None = None
    prompt_cache_key: str | None = None
    prompt_cache_options: ResponsesPromptCacheOptions | None = None
    prompt_cache_retention: Literal["in_memory", "in-memory", "24h"] | None = None
    client_metadata: dict[str, Any] | None = None
    instructions: str | None = None
    max_output_tokens: int | None = Field(default=None, gt=0)
    max_tool_calls: int | None = Field(default=None, gt=0, strict=True)
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
    # GPT Image 2.6 only; the transport rejects it for every other model before provider I/O.
    complexity: Literal["low", "medium", "high"] | None = None
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
    complexity: Literal["low", "medium", "high"] | None = None
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
