"""Gemini Decisions wire contract: https://aistudio.google.com/docs/decisions-api."""
from __future__ import annotations

import re
import unicodedata
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import AfterValidator, Field, StrictStr, TypeAdapter, model_validator

from lumen.models.decision_contracts import WireModel

MAX_GEMINI_DECISION_BODY_BYTES = 20 * 1024 * 1024
CONTEXT_TOKENS = 262_144
MAX_QUESTIONS = 1_000
MAX_CRITERIA = 255
UNSUPPORTED_MODALITIES = frozenset({"audio", "video", "document"})
QUESTION_KEY = re.compile(r"[A-Za-z0-9_-]{1,64}")
_BASE64 = re.compile(r"[A-Za-z0-9+/]+={0,2}")
_SURROGATE = re.compile(r"[\ud800-\udfff]")
_UNSAFE_URI_CHAR = re.compile(r"[\x00-\x20\x7f\\]")
_YOUTUBE_DOMAINS = ("youtube.com", "youtu.be", "youtube-nocookie.com")


def _unicode_text(value: str) -> str:
    if _SURROGATE.search(value):
        raise ValueError("text must be valid Unicode")
    return value


Text = Annotated[StrictStr, AfterValidator(_unicode_text)]
NonemptyText = Annotated[Text, Field(min_length=1)]
ModelName = Annotated[NonemptyText, Field(max_length=190)]
Resolution = Literal["low", "medium", "high", "ultra_high"]
ImageMimeType = Literal[
    "image/png", "image/jpeg", "image/webp", "image/heic", "image/heif", "image/gif", "image/bmp", "image/tiff",
]


class GeminiDecisionModalityUnsupported(ValueError):
    pass


class TextContent(WireModel):
    type: Literal["text"]
    text: Text


class ImageContent(WireModel):
    type: Literal["image"]
    mime_type: ImageMimeType
    data: NonemptyText | None = None
    uri: NonemptyText | None = None
    resolution: Resolution | None = None

    @model_validator(mode="after")
    def one_image_source(self):
        if any(getattr(self, field) is None for field in self.model_fields_set):
            raise ValueError("image fields cannot be null")
        if (self.data is None) == (self.uri is None):
            raise ValueError("image requires data or uri, but not both")
        if self.data is not None:
            # Check standard base64 without allocating decoded pixels. The upstream
            # validates the file format; the resolver never fetches caller URIs.
            if len(self.data) % 4 or _BASE64.fullmatch(self.data) is None:
                raise ValueError("image data must be base64")
        else:
            if _UNSAFE_URI_CHAR.search(self.uri):
                raise ValueError("image uri must be an HTTPS URL")
            parsed = urlsplit(self.uri)
            host = (parsed.hostname or "").rstrip(".")
            if (parsed.scheme != "https" or not host or parsed.username is not None or parsed.password is not None
                    or any(host == domain or host.endswith(f".{domain}") for domain in _YOUTUBE_DOMAINS)):
                raise ValueError("image uri must be an HTTPS URL outside YouTube")
            # Accessing port also validates a supplied numeric port and its range.
            _ = parsed.port
        return self


Content = Annotated[TextContent | ImageContent, Field(discriminator="type")]
SharedInput = NonemptyText | Content | Annotated[list[Content], Field(min_length=1, fail_fast=True)]
Instructions = NonemptyText | TextContent | Annotated[list[TextContent], Field(min_length=1, fail_fast=True)]


def _blocks(value):
    return value if isinstance(value, list) else (value,)


def reject_unsupported_modalities(value: object) -> None:
    # Scan only typed input positions. A JSON-as-text string stays opaque.
    if not isinstance(value, dict):
        return
    inputs = [value.get("input")]
    questions = value.get("questions")
    if isinstance(questions, dict) and len(questions) <= MAX_QUESTIONS:
        inputs.extend(q.get("instructions") for q in questions.values() if isinstance(q, dict))
    for payload in inputs:
        for part in _blocks(payload):
            kind = part.get("type") if isinstance(part, dict) else None
            if isinstance(kind, str) and kind in UNSUPPORTED_MODALITIES:
                raise GeminiDecisionModalityUnsupported("decision_input_modality_unsupported")


def _has_text(value: Instructions) -> bool:
    return bool(value) if isinstance(value, str) else any(block.text for block in _blocks(value))


class Question(WireModel):
    instructions: Instructions

    @model_validator(mode="after")
    def nonempty_instructions(self):
        if not _has_text(self.instructions):
            raise ValueError("instructions must not be empty")
        return self


class NoulCriteria(WireModel):
    true_description: Text | None = None
    false_description: Text | None = None

    @model_validator(mode="after")
    def nonnull_descriptions(self):
        if any(getattr(self, field) is None for field in self.model_fields_set):
            raise ValueError("descriptions must be strings")
        return self


class NoulQuestion(Question):
    type: Literal["noul"]
    criteria: NoulCriteria | None = None

    @model_validator(mode="after")
    def nonnull_criteria(self):
        if self.criteria is None and "criteria" in self.model_fields_set:
            raise ValueError("criteria must be an object")
        return self


def _choice_key(value: str) -> str:
    if value.startswith(" ") or value.endswith(" ") or any(
        (ch.isspace() and ch != " ") or unicodedata.category(ch) == "Cc" for ch in value
    ):
        raise ValueError("invalid choice option key")
    # 128 Unicode code points imply at most 512 UTF-8 bytes. Surrogates are invalid.
    return _unicode_text(value)


ChoiceKey = Annotated[StrictStr, Field(min_length=1, max_length=128), AfterValidator(_choice_key)]


class ChoiceQuestion(Question):
    type: Literal["choice"]
    criteria: Annotated[dict[ChoiceKey, Text], Field(min_length=2, max_length=MAX_CRITERIA)]

    @model_validator(mode="before")
    @classmethod
    def bound_criteria_before_validating_options(cls, value):
        criteria = value.get("criteria") if isinstance(value, dict) else None
        if not isinstance(criteria, dict) or not 2 <= len(criteria) <= MAX_CRITERIA:
            raise ValueError("choice requires 2 to 255 options")
        return value


class ScoreQuestion(Question):
    type: Literal["score"]
    criteria: Annotated[list[NonemptyText], Field(min_length=2, max_length=MAX_CRITERIA, fail_fast=True)]


QuestionType = Annotated[NoulQuestion | ChoiceQuestion | ScoreQuestion, Field(discriminator="type")]
_QUESTION = TypeAdapter(QuestionType)


class GeminiDecisionRequest(WireModel):
    model: ModelName
    input: SharedInput
    questions: Annotated[dict[str, QuestionType], Field(min_length=1, max_length=MAX_QUESTIONS)]

    @model_validator(mode="before")
    @classmethod
    def validate_questions_one_at_a_time(cls, value):
        reject_unsupported_modalities(value)
        questions = value.get("questions") if isinstance(value, dict) else None
        if not isinstance(questions, dict) or not 1 <= len(questions) <= MAX_QUESTIONS:
            raise ValueError("questions must be an object with 1 to 1000 entries")
        parsed = {}
        for key, question in questions.items():
            if not isinstance(key, str) or QUESTION_KEY.fullmatch(key) is None:
                raise ValueError("invalid question key")
            parsed[key] = _QUESTION.validate_python(question, strict=True)
        return {**value, "questions": parsed}

    @model_validator(mode="after")
    def nonempty_input(self):
        if not isinstance(self.input, str) and not any(
            isinstance(part, ImageContent) or part.text for part in _blocks(self.input)
        ):
            raise ValueError("input must not be empty")
        return self

    @property
    def has_image(self) -> bool:
        return any(isinstance(part, ImageContent) for part in _blocks(self.input))
