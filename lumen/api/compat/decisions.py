"""API-key authenticated OpenAI and Gemini Decisions wire endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from lumen.api.compat.openai import openai_error_response
from lumen.api.decisions import (
    GEMINI_DECISION_BODY_SCHEMA,
    BoundedDecisionRoute,
    ProviderDecisionRoute,
    parse_gemini_request,
)
from lumen.auth import Principal, require_api_key_scopes
from lumen.models.decision_contracts import DecisionRequest
from lumen.services import decisions, gemini_decisions

router = APIRouter(route_class=BoundedDecisionRoute)
google_router = APIRouter(route_class=ProviderDecisionRoute)


@router.post("/decisions", openapi_extra={"security": [{"APIKeyBearer": []}, {"XApiKey": []}]})
async def create_decision(body: DecisionRequest,
                          principal: Principal = Depends(require_api_key_scopes("decisions:write"))):
    try:
        route = await decisions.resolve(body.model, openai_compat=True)
        result = await decisions.decide(
            route=route, body=body, user_id=principal["user_id"], project_id=principal["project_id"],
            api_key_id=principal.get("api_key_id"), required_scopes=("decisions:write",), source="api",
            payer=principal.get("payer"),
        )
    except decisions.DecisionError as exc:
        return openai_error_response(
            exc.status_code, exc.message,
            code="domain_credit_insufficient" if exc.message == "domain_credit_insufficient" else "decision_error",
        )
    return {key: result[key] for key in ("model", "answers", "usage")}


@google_router.post("/v1beta/decisions", openapi_extra={"requestBody": {
    "required": True,
    "content": {"application/json": {"schema": GEMINI_DECISION_BODY_SCHEMA}},
}})
async def create_gemini_decision(
    request: Request,
    principal: Principal = Depends(require_api_key_scopes("decisions:write")),
):
    body = parse_gemini_request(request, await request.body())
    try:
        model = body.model.removeprefix("models/") if body.model is not None else None
        route = (await decisions.resolve(model, model_row_id=body.model_row_id, provider_type="gemini")
                 if body.model_row_id is not None else await decisions.resolve(model, provider_type="gemini"))
        return await gemini_decisions.decide(
            route=route, body=body, user_id=principal["user_id"], project_id=principal["project_id"],
            api_key_id=principal.get("api_key_id"), required_scopes=("decisions:write",),
            payer=principal.get("payer"),
        )
    except decisions.DecisionError as exc:
        raise HTTPException(exc.status_code, exc.message) from exc
