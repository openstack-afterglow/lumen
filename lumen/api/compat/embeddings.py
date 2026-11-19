"""OpenAI and Google Gemini-compatible text embedding APIs."""

from __future__ import annotations

import base64
import struct
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field, StrictStr, ValidationError

from lumen.api.compat.google import GoogleModelRoute
from lumen.api.compat.openai import openai_error_response
from lumen.auth import Principal, require_api_key_scopes
from lumen.services import embeddings

router = APIRouter()
google_router = APIRouter(route_class=GoogleModelRoute)


class OpenAIEmbeddingRequest(BaseModel):
    model: StrictStr = Field(min_length=1, max_length=190)
    input: StrictStr | list[StrictStr]
    dimensions: int | None = Field(default=None, gt=0, le=65536)
    encoding_format: Literal["float", "base64"] = "float"
    user: str | None = Field(default=None, max_length=256)
    provider: str | None = None


class GeminiPart(BaseModel):
    text: StrictStr


class GeminiContent(BaseModel):
    parts: list[GeminiPart] = Field(min_length=1, max_length=1)
    role: str | None = None


class GeminiEmbedRequest(BaseModel):
    model: StrictStr | None = None
    content: GeminiContent
    taskType: Literal["RETRIEVAL_QUERY", "RETRIEVAL_DOCUMENT", "SEMANTIC_SIMILARITY", "CLASSIFICATION",
                      "CLUSTERING", "QUESTION_ANSWERING", "FACT_VERIFICATION", "CODE_RETRIEVAL_QUERY"] | None = None
    title: str | None = None
    outputDimensionality: int | None = Field(default=None, gt=0, le=65536)


def _validate_inputs(inputs: list[str], maximum: int) -> None:
    if not 1 <= len(inputs) <= maximum or any(not isinstance(text, str) or not text.strip() for text in inputs):
        raise HTTPException(status_code=400, detail=f"input must contain 1 to {maximum} non-empty strings")


def _openai_vectors(vectors: list[dict], encoding_format: str) -> list[dict]:
    if encoding_format == "float":
        return [{"object": "embedding", **item} for item in vectors]
    return [{"object": "embedding", "index": item["index"],
             "embedding": base64.b64encode(struct.pack(f"<{len(item['embedding'])}f", *item["embedding"])).decode("ascii")}
            for item in vectors]


async def _run(model: str, inputs: list[str], *, principal: Principal, dimensions: int | None,
               max_inputs: int, provider: str | None = None, provider_type: str | None = None,
               task_type: str | None = None, title: str | None = None) -> dict:
    _validate_inputs(inputs, max_inputs)
    try:
        route = await embeddings.resolve(model.removeprefix("models/"), provider=provider, provider_type=provider_type)
        return await embeddings.embed(
            route=route, inputs=inputs, user_id=principal["user_id"], project_id=principal["project_id"],
            api_key_id=principal.get("api_key_id"), required_scopes=("embeddings:write",),
            payer=principal.get("payer"),
            dimensions=dimensions, task_type=task_type, title=title,
        )
    except embeddings.EmbeddingError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


@router.post("/embeddings", openapi_extra={"security": [{"APIKeyBearer": []}, {"XApiKey": []}]})
async def openai_embeddings(body: OpenAIEmbeddingRequest,
        principal: Principal = Depends(require_api_key_scopes("embeddings:write"))):
    try:
        inputs = [body.input] if isinstance(body.input, str) else body.input
        if not isinstance(inputs, list) or not all(isinstance(item, str) for item in inputs):
            raise HTTPException(status_code=400, detail="input must be a string or an array of strings")
        result = await _run(body.model, inputs, principal=principal, dimensions=body.dimensions,
                            max_inputs=2048, provider=body.provider)
    except HTTPException as exc:
        return openai_error_response(
            exc.status_code, str(exc.detail),
            code="domain_credit_insufficient" if exc.detail == "domain_credit_insufficient" else "embedding_error",
        )
    return {"object": "list", "data": _openai_vectors(result["embeddings"], body.encoding_format),
            "model": result["model"], "usage": {"prompt_tokens": result["usage"]["prompt_tokens"],
            "total_tokens": result["usage"]["total_tokens"]}}


@google_router.post("/v1beta/models/{model:path}:embedContent")
async def gemini_embed(model: str, body: GeminiEmbedRequest,
        x_lumen_provider: str | None = Header(default=None, alias="X-Lumen-Provider", min_length=1, max_length=40,
                                              pattern=r"^[a-z0-9][a-z0-9_-]*$"),
        principal: Principal = Depends(require_api_key_scopes("embeddings:write"))):
    result = await _run(model, [body.content.parts[0].text], principal=principal,
                        dimensions=body.outputDimensionality, max_inputs=1, provider_type="gemini",
                        provider=x_lumen_provider,
                        task_type=body.taskType if "taskType" in body.model_fields_set else None, title=body.title)
    return {"embedding": {"values": result["embeddings"][0]["embedding"]}}


@google_router.post("/v1beta/models/{model:path}:batchEmbedContents")
async def gemini_batch_embed(model: str, request: Request,
        x_lumen_provider: str | None = Header(default=None, alias="X-Lumen-Provider", min_length=1, max_length=40,
                                              pattern=r"^[a-z0-9][a-z0-9_-]*$"),
        principal: Principal = Depends(require_api_key_scopes("embeddings:write"))):
    try:
        payload: Any = await request.json()
        requests = [GeminiEmbedRequest.model_validate(row) for row in payload["requests"]]
        if not requests or len(requests) > 100:
            raise ValueError("requests must contain 1 to 100 items")
        if any(row.model and row.model.removeprefix("models/") != model.removeprefix("models/") for row in requests):
            raise ValueError("batch models must match the URL model")
        dimensions = {row.outputDimensionality for row in requests}
        task_types = {row.taskType for row in requests}
        titles = {row.title for row in requests}
        if len(dimensions) != 1 or len(task_types) != 1 or len(titles) != 1:
            raise ValueError("batch embedding options must match across requests")
        inputs = [row.content.parts[0].text for row in requests]
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        raise HTTPException(status_code=400, detail="invalid Gemini embedding request") from exc
    result = await _run(model, inputs, principal=principal, dimensions=next(iter(dimensions)), max_inputs=100,
                        provider_type="gemini", provider=x_lumen_provider,
                        task_type=next(iter(task_types)), title=next(iter(titles)))
    return {"embeddings": [{"values": item["embedding"]} for item in result["embeddings"]]}
