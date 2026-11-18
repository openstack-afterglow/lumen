"""Session Decisions endpoint and bounded request reader shared with compat."""
from __future__ import annotations

import json
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import ValidationError

from lumen.auth import Principal, require_scopes
from lumen.models.decision_contracts import MAX_DECISION_BODY_BYTES, DecisionRequest
from lumen.models.gemini_decision_contracts import (
    MAX_GEMINI_DECISION_BODY_BYTES,
    GeminiDecisionModalityUnsupported,
    GeminiDecisionRequest,
)
from lumen.services import decisions, gemini_decisions

_JSON_STRUCTURE = re.compile(rb'["\\{}\[\]]')
_INVALID_GEMINI = "invalid Gemini decisions request"


def _redacted_validation(errors: list[dict]) -> JSONResponse:
    # Preserve the OpenAI-shaped diagnostics without returning input or context.
    errors = [
        {
            "type": error["type"][:80],
            "loc": [part[:80] if isinstance(part, str) else part for part in error["loc"][:8]],
            "msg": error["msg"][:240],
        }
        for error in errors[:20]
    ]
    return JSONResponse(status_code=422, content={"detail": errors}, headers={"Cache-Control": "no-store"})


def _gemini_json_limits(content: bytes, duplicate_key: bool) -> None:
    if duplicate_key:
        raise HTTPException(422, _INVALID_GEMINI)
    # Ignore punctuation and escaped quotes in string payloads (including JSON-as-text).
    depth = 0
    in_string = False
    escaped = -1
    for match in _JSON_STRUCTURE.finditer(content):
        if match.start() == escaped:
            continue
        token = content[match.start()]
        if token == ord('"'):
            in_string = not in_string
        elif in_string:
            if token == ord("\\"):
                escaped = match.start() + 1
        elif token in (ord("{"), ord("[")):
            depth += 1
            if depth > 100:
                raise HTTPException(422, "decision JSON exceeds 100 levels")
        elif token in (ord("}"), ord("]")):
            depth -= 1


def _decision_json(content: bytes) -> tuple[object, bool]:
    duplicate_key = False

    def collect(pairs):
        nonlocal duplicate_key
        result = dict(pairs)
        duplicate_key |= len(result) != len(pairs)
        return result

    return json.loads(content.decode("utf-8"), object_pairs_hook=collect), duplicate_key


class BoundedDecisionRoute(APIRoute):
    def body_limit(self) -> tuple[int, str]:
        return MAX_DECISION_BODY_BYTES, "decision request body exceeds 16 MiB"

    async def prepare(self, request: Request) -> None:
        pass

    def validation_error(self, exc: RequestValidationError) -> JSONResponse:
        return _redacted_validation(exc.errors())

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def bounded(request: Request):
            length = request.headers.get("content-length", "")
            limit, message = self.body_limit()
            if length.isdecimal() and (len(length) > 10 or int(length) > limit):
                raise HTTPException(413, message)
            content = bytearray()
            async for chunk in request.stream():
                if len(content) + len(chunk) > limit:
                    raise HTTPException(413, message)
                content.extend(chunk)
            request._body = bytes(content)
            del content
            await self.prepare(request)
            try:
                return await handler(request)
            except RequestValidationError as exc:
                return self.validation_error(exc)
        return bounded


class ProviderDecisionRoute(BoundedDecisionRoute):
    def body_limit(self) -> tuple[int, str]:
        return MAX_GEMINI_DECISION_BODY_BYTES, "decision request body exceeds 20 MiB"


class GeminiDecisionRoute(ProviderDecisionRoute):
    async def prepare(self, request: Request) -> None:
        content = await request.body()
        try:
            request._json, duplicate_key = _decision_json(content)
        except (ValueError, RecursionError):
            raise HTTPException(422, _INVALID_GEMINI) from None
        _gemini_json_limits(content, duplicate_key)

    def validation_error(self, exc: RequestValidationError) -> JSONResponse:
        unsupported = any(
            isinstance(error.get("ctx", {}).get("error"), GeminiDecisionModalityUnsupported)
            for error in exc.errors()
        )
        return JSONResponse(
            status_code=422, headers={"Cache-Control": "no-store"},
            content={"detail": "decision_input_modality_unsupported" if unsupported else _INVALID_GEMINI},
        )


router = APIRouter(route_class=ProviderDecisionRoute)


def _openai_request(raw: object) -> DecisionRequest:
    try:
        return DecisionRequest.model_validate(raw)
    except ValidationError as exc:
        errors = exc.errors(include_url=False, include_context=False, include_input=False)
        for error in errors:
            error["loc"] = ("body", *error["loc"])
        raise RequestValidationError(errors) from None


@router.post("/chat/decisions", openapi_extra={"requestBody": {
    "required": True,
    "content": {"application/json": {"schema": {"oneOf": [
        {"$ref": "#/components/schemas/DecisionRequest"},
        {"$ref": "#/components/schemas/GeminiDecisionRequest"},
    ]}}},
}})
async def create_decision(request: Request,
                          principal: Principal = Depends(require_scopes("native:runs:write"))):
    content = await request.body()
    if not content:
        raise RequestValidationError([{"type": "missing", "loc": ("body",), "msg": "Field required"}])
    media_type = request.headers.get("content-type", "application/json").partition(";")[0].strip().lower()
    if media_type != "application/json" and not (media_type.startswith("application/") and media_type.endswith("+json")):
        _openai_request(content)
    try:
        raw, duplicate_key = _decision_json(content)
    except json.JSONDecodeError as exc:
        raise RequestValidationError([{
            "type": "json_invalid", "loc": ("body", exc.pos), "msg": "JSON decode error",
        }]) from None
    except (UnicodeError, ValueError, RecursionError):
        raise HTTPException(400, "There was an error parsing the body") from None

    # A questions array is impossible in Gemini. Preserve fail-fast validation
    # for legacy array requests, even when the registry is unavailable.
    body = None
    if not isinstance(raw, dict) or isinstance(raw.get("questions"), list):
        if len(content) > MAX_DECISION_BODY_BYTES:
            raise HTTPException(413, "decision request body exceeds 16 MiB")
        body = _openai_request(raw)
    model = raw.get("model")
    if not isinstance(model, str) or not 1 <= len(model) <= 190 or any("\ud800" <= c <= "\udfff" for c in model):
        _openai_request(raw)
        raise HTTPException(422, "invalid decision model")
    caller = dict(user_id=principal["user_id"], project_id=principal["project_id"],
                  api_key_id=principal.get("api_key_id"), required_scopes=("native:runs:write",),
                  source="api" if principal.get("api_key_id") is not None else "web")
    try:
        route = await decisions.resolve(model)
        if route.get("provider_type") == "gemini":
            _gemini_json_limits(content, duplicate_key)
            try:
                gemini = GeminiDecisionRequest.model_validate(raw, strict=True)
            except ValidationError as exc:
                if any(isinstance(error.get("ctx", {}).get("error"), GeminiDecisionModalityUnsupported)
                       for error in exc.errors(include_input=False, include_url=False)):
                    raise HTTPException(422, "decision_input_modality_unsupported") from None
                raise HTTPException(422, _INVALID_GEMINI) from None
            return await gemini_decisions.decide(route=route, body=gemini, **caller)
        if len(content) > MAX_DECISION_BODY_BYTES:
            raise HTTPException(413, "decision request body exceeds 16 MiB")
        return await decisions.decide(route=route, body=body or _openai_request(raw), **caller)
    except decisions.DecisionError as exc:
        raise HTTPException(exc.status_code, exc.message) from exc
