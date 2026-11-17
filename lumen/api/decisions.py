"""Session Decisions endpoint and bounded request reader shared with compat."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from lumen.auth import Principal, require_scopes
from lumen.models.decision_contracts import MAX_DECISION_BODY_BYTES, DecisionRequest
from lumen.services import decisions


class BoundedDecisionRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def bounded(request: Request):
            length = request.headers.get("content-length", "")
            if length.isdecimal() and int(length) > MAX_DECISION_BODY_BYTES:
                raise HTTPException(413, "decision request body exceeds 16 MiB")
            content = bytearray()
            async for chunk in request.stream():
                if len(content) + len(chunk) > MAX_DECISION_BODY_BYTES:
                    raise HTTPException(413, "decision request body exceeds 16 MiB")
                content.extend(chunk)
            request._body = bytes(content)
            try:
                return await handler(request)
            except RequestValidationError as exc:
                # Do not echo request content or amplify one invalid item into an
                # unbounded error response. Tag values can also occur in messages.
                errors = [
                    {
                        "type": error["type"][:80],
                        "loc": [part[:80] if isinstance(part, str) else part for part in error["loc"][:8]],
                        "msg": error["msg"][:240],
                    }
                    for error in exc.errors()[:20]
                ]
                return JSONResponse(status_code=422, content={"detail": errors}, headers={"Cache-Control": "no-store"})
        return bounded


router = APIRouter(route_class=BoundedDecisionRoute)


@router.post("/chat/decisions")
async def create_decision(body: DecisionRequest,
                          principal: Principal = Depends(require_scopes("native:runs:write"))):
    try:
        route = await decisions.resolve(body.model)
        return await decisions.decide(
            route=route, body=body, user_id=principal["user_id"], project_id=principal["project_id"],
            api_key_id=principal.get("api_key_id"), required_scopes=("native:runs:write",),
            source="api" if principal.get("api_key_id") is not None else "web",
        )
    except decisions.DecisionError as exc:
        raise HTTPException(exc.status_code, exc.message) from exc
