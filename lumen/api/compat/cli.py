"""Coding-CLI model catalog — GET /v1/cli/models.

Installers group the active text routes by provider and store the exact
``lumen/<provider_id>/<model_id>`` route token, which compatibility endpoints
resolve to that one provider/model. ``/v1/models`` keeps its public-ID contract.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from lumen.auth import require_api_key_scopes
from lumen.services.providers import errors, routing

from .openai import openai_error_dict

router = APIRouter()
_NO_STORE = {"Cache-Control": "no-store"}


class CliModelItem(BaseModel):
    id: str
    api_model_name: str
    display_name: str
    provider: str
    provider_name: str
    provider_type: str
    protocols: list[Literal["messages", "responses"]]
    usable: bool
    disabled_reason: (
        Literal[
            "subscription_protocol_unsupported",
            "provider_credentials_missing",
            "pricing_unavailable",
            "text_unavailable",
            "tools_disabled",
        ]
        | None
    )
    capabilities: dict
    input_price_per_million: str | None
    output_price_per_million: str | None


class CliModelListResponse(BaseModel):
    models: list[CliModelItem]


@router.get(
    "/cli/models",
    response_model=CliModelListResponse,
    openapi_extra={"security": [{"APIKeyBearer": []}, {"XApiKey": []}]},
)
async def list_cli_models(token_info: dict = Depends(require_api_key_scopes("models:read"))):
    del token_info
    try:
        models = await routing.list_cli_models()
    except errors.ChatStorageUnavailable:
        # An empty 200 would read as "no models"; installers must not rewrite config from it.
        return JSONResponse(
            status_code=503,
            content=openai_error_dict("model catalog unavailable", type_="api_error"),
            headers=_NO_STORE,
        )
    body = CliModelListResponse(models=models).model_dump(mode="json")
    return JSONResponse(content=body, headers=_NO_STORE)
