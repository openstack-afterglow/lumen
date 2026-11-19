"""Session-authenticated embedding playground API."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, StrictStr

from lumen.auth import Principal, require_scopes
from lumen.services import embeddings
from lumen.services.providers import errors, routing

router = APIRouter()


class EmbedRequest(BaseModel):
    model_id: int = Field(gt=0)
    input: list[StrictStr] = Field(min_length=1, max_length=16)
    dimensions: int | None = Field(default=None, gt=0, le=65536)


@router.post("/chat/embeddings")
async def create_embeddings(body: EmbedRequest,
        principal: Principal = Depends(require_scopes("embeddings:write"))):
    if any(not text.strip() for text in body.input):
        raise HTTPException(status_code=422, detail="input items must be non-empty")
    try:
        route = await routing.resolve_model_by_id(body.model_id, model_kind="embedding")
        if route is None:
            raise HTTPException(status_code=404, detail="embedding model not found")
        result = await embeddings.embed(
            route=route, inputs=body.input, user_id=principal["user_id"], project_id=principal["project_id"],
            api_key_id=principal.get("api_key_id"), required_scopes=("embeddings:write",),
            payer=principal.get("payer"),
            dimensions=body.dimensions,
        )
    except embeddings.EmbeddingError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    except errors.ChatStorageUnavailable as exc:
        raise HTTPException(status_code=503, detail="embedding model configuration unavailable") from exc
    vector_dimensions = len(result["embeddings"][0]["embedding"])
    return {"model_id": body.model_id, "model": result["model"], "dimensions": vector_dimensions,
            "embeddings": result["embeddings"], "usage": result["usage"],
            "credited_cost": result["credited_cost"]}
