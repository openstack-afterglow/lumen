"""API-key authenticated OpenAI Decisions wire endpoint."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from lumen.api.compat.openai import openai_error_response
from lumen.api.decisions import BoundedDecisionRoute
from lumen.auth import Principal, require_api_key_scopes
from lumen.models.decision_contracts import DecisionRequest
from lumen.services import decisions

router = APIRouter(route_class=BoundedDecisionRoute)


@router.post("/decisions", openapi_extra={"security": [{"APIKeyBearer": []}, {"XApiKey": []}]})
async def create_decision(body: DecisionRequest,
                          principal: Principal = Depends(require_api_key_scopes("compat:completions:write"))):
    try:
        route = await decisions.resolve(body.model)
        result = await decisions.decide(
            route=route, body=body, user_id=principal["user_id"], project_id=principal["project_id"],
            api_key_id=principal.get("api_key_id"), required_scopes=("compat:completions:write",), source="api",
        )
    except decisions.DecisionError as exc:
        return openai_error_response(exc.status_code, exc.message, code="decision_error")
    return {key: result[key] for key in ("model", "answers", "usage")}
