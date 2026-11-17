"""Bounded OpenAI Decisions wire contracts (official reference, 2026-11-18)."""
from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr, field_validator, model_validator

MAX_DECISION_BODY_BYTES = 16 * 1024 * 1024
Text = Annotated[StrictStr, Field(max_length=10485760)]
ShortText = Annotated[StrictStr, Field(max_length=1048576)]
CallId = Annotated[StrictStr, Field(min_length=1, max_length=64)]


class WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CacheBreakpoint(WireModel):
    mode: Literal["explicit"] = "explicit"


class InputText(WireModel):
    type: Literal["input_text"] = "input_text"
    text: Text
    prompt_cache_breakpoint: CacheBreakpoint | None = None


class InputImage(WireModel):
    type: Literal["input_image"] = "input_image"
    image_url: Annotated[StrictStr, Field(pattern=r"^(data:|https?://)", max_length=1073741824)]
    detail: Literal["low", "high", "auto", "original"] | None = "auto"
    prompt_cache_breakpoint: CacheBreakpoint | None = None


class InputJSON(WireModel):
    type: Literal["input_json"] = "input_json"
    data: dict[str, Any] | list[dict[str, Any]]

    @field_validator("data")
    @classmethod
    def bounded_json(cls, data):
        pending = [(data, 0)]
        values = 0
        while pending:
            value, parent_depth = pending.pop()
            values += 1
            if values > 10000:
                raise ValueError("input_json exceeds 10000 values")
            if isinstance(value, (dict, list)):
                depth = parent_depth + 1
                if depth > 32:
                    raise ValueError("input_json exceeds 32 container levels")
                pending.extend((item, depth) for item in (value.values() if isinstance(value, dict) else value))
        try:
            encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        except (ValueError, UnicodeError, TypeError) as exc:
            raise ValueError("input_json must contain finite JSON values") from exc
        if len(encoded) > 256 * 1024:
            raise ValueError("input_json exceeds 256 KiB compact UTF-8 JSON")
        return data


InputPart = Annotated[InputText | InputImage | InputJSON, Field(discriminator="type")]
ToolPart = Annotated[InputText | InputImage, Field(discriminator="type")]


class OutputText(WireModel):
    type: Literal["output_text"] = "output_text"
    text: Text


class RefusalText(WireModel):
    type: Literal["refusal"] = "refusal"
    refusal: Text


class UserMessage(WireModel):
    type: Literal["message"] = "message"
    role: Literal["user"]
    content: Text | Annotated[list[InputPart], Field(max_length=16384)]


class InstructionMessage(WireModel):
    type: Literal["message"] = "message"
    role: Literal["developer", "system"]
    content: Text | Annotated[list[InputText], Field(max_length=16384)]


class AssistantMessage(WireModel):
    type: Literal["message"] = "message"
    role: Literal["assistant"]
    content: Text | Annotated[list[Annotated[OutputText | RefusalText, Field(discriminator="type")]], Field(max_length=16384)]
    phase: Literal["commentary", "final_answer"] | None = None


class FunctionCall(WireModel):
    type: Literal["function_call"]
    arguments: ShortText
    call_id: CallId
    name: Annotated[StrictStr, Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_.-]+$")]
    id: ShortText | None = None
    namespace: Annotated[StrictStr, Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_-]+$")] | None = None
    status: Literal["in_progress", "completed", "incomplete"] | None = None


class CustomToolCall(WireModel):
    type: Literal["custom_tool_call"]
    input: ShortText
    call_id: CallId
    name: Annotated[StrictStr, Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")]
    id: ShortText | None = None
    namespace: Annotated[StrictStr, Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")] | None = None
    status: Literal["in_progress", "completed", "incomplete"] | None = None


class ToolOutput(WireModel):
    type: Literal["function_call_output", "custom_tool_call_output"]
    call_id: CallId
    output: Text | Annotated[list[ToolPart], Field(max_length=16384)]
    id: ShortText | None = None
    status: Literal["in_progress", "completed", "incomplete"] | None = None


Message = Annotated[UserMessage | InstructionMessage | AssistantMessage, Field(discriminator="role")]
InputItem = Message | FunctionCall | CustomToolCall | ToolOutput


class QuestionText(WireModel):
    type: Literal["input_text"] = "input_text"
    text: Text


class QuestionImage(WireModel):
    type: Literal["input_image"] = "input_image"
    image_url: Annotated[StrictStr, Field(pattern=r"^(data:|https?://)", max_length=1073741824)]
    detail: Literal["low", "high", "auto", "original"] | None = "auto"


QuestionPart = Annotated[QuestionText | QuestionImage | InputJSON, Field(discriminator="type")]
Instructions = ShortText | Annotated[list[QuestionPart], Field(min_length=1, max_length=16384)]


class PredicateQuestion(WireModel):
    type: Literal["predicate"]
    name: ShortText | None = None
    instructions: Instructions

    @model_validator(mode="after")
    def name_not_null(self):
        if "name" in self.model_fields_set and self.name is None:
            raise ValueError("question name must be a string when present")
        return self


class Choice(WireModel):
    value: ShortText | StrictBool
    description: ShortText | None = None


class ChoiceQuestion(PredicateQuestion):
    type: Literal["choice"]
    choices: Annotated[list[Choice], Field(min_length=2, max_length=255)]

    @field_validator("choices")
    @classmethod
    def unique_choices(cls, choices):
        keys = [(type(item.value), item.value) for item in choices]
        if len(set(keys)) != len(keys):
            raise ValueError("choice values must be unique")
        return choices


class Level(WireModel):
    label: ShortText
    description: ShortText | None = None


class ScoreQuestion(PredicateQuestion):
    type: Literal["score"]
    levels: Annotated[list[Level], Field(min_length=2, max_length=10)]


Question = Annotated[PredicateQuestion | ChoiceQuestion | ScoreQuestion, Field(discriminator="type")]


class PromptCacheOptions(WireModel):
    mode: Literal["implicit", "explicit"] | None = None
    ttl: Literal["30m"] = "30m"


class DecisionRequest(WireModel):
    model: Annotated[StrictStr, Field(min_length=1, max_length=190)]
    input: Text | Annotated[list[InputItem], Field(max_length=131072)]
    questions: Annotated[list[Question], Field(min_length=1, max_length=400)]
    prompt_cache_key: Annotated[StrictStr, Field(max_length=64)] | None = None
    prompt_cache_options: PromptCacheOptions | None = None
    safety_identifier: Annotated[StrictStr, Field(max_length=128)] | None = None

    @model_validator(mode="after")
    def names_and_calls(self):
        names = [q.name for q in self.questions if q.name is not None]
        if len(set(names)) != len(names):
            raise ValueError("question names must be unique")
        calls = set()
        for item in self.input if isinstance(self.input, list) else []:
            if isinstance(item, (FunctionCall, CustomToolCall)):
                calls.add((item.type.removesuffix("_call"), item.call_id))
            elif isinstance(item, ToolOutput) and (item.type.removesuffix("_call_output"), item.call_id) not in calls:
                raise ValueError("tool output must follow a matching call_id")
        return self
