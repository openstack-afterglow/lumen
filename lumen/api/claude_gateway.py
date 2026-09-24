"""Claude Code gateway discovery, device auth, and Anthropic-native inference."""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from typing import Any, Literal
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from lumen.api.compat.anthropic import (
    AnthropicCountTokensRequest,
    AnthropicMessagesRequest,
    anthropic_error,
    anthropic_error_response,
    anthropic_protocol_headers,
)
from lumen.api.compat.streaming import events_with_ping
from lumen.auth import Principal, require_api_key_scopes, require_token
from lumen.services import claude_gateway as gateway
from lumen.services import completion_api as core

public_router = APIRouter()
internal_router = APIRouter()


class GatewayAuthorizeBody(BaseModel):
    user_code: str = Field(min_length=8, max_length=16)
    action: Literal["approve", "deny"] = "approve"


def _oauth_error(exc: gateway.GatewayError) -> JSONResponse:
    payload: dict[str, Any] = {"error": exc.code}
    if exc.description:
        payload["error_description"] = exc.description
    return JSONResponse(payload, status_code=exc.status_code, headers={"Cache-Control": "no-store"})


async def _fields(request: Request) -> dict[str, str]:
    raw = await request.body()
    if len(raw) > 8192:
        raise gateway.GatewayError("invalid_request", description="Request body is too large")
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type == "application/json":
        try:
            parsed = json.loads(raw or b"{}")
        except (TypeError, ValueError) as exc:
            raise gateway.GatewayError("invalid_request", description="Request JSON is invalid") from exc
        if (
            not isinstance(parsed, dict)
            or any(not isinstance(key, str) for key in parsed)
            or any(not isinstance(value, str) for value in parsed.values())
        ):
            raise gateway.GatewayError("invalid_request")
        return parsed
    if content_type not in {"", "application/x-www-form-urlencoded"}:
        raise gateway.GatewayError("invalid_request", description="Unsupported content type")
    try:
        values = parse_qs(raw.decode("utf-8"), keep_blank_values=True, strict_parsing=True)
    except (UnicodeDecodeError, ValueError) as exc:
        raise gateway.GatewayError("invalid_request", description="Request form is invalid") from exc
    if any(len(items) != 1 for items in values.values()):
        raise gateway.GatewayError("invalid_request", description="Duplicate form fields are not allowed")
    return {key: items[0] for key, items in values.items()}


def _remote_subject(request: Request) -> str:
    return request.client.host if request.client is not None else "unknown"


def _gateway_principal(principal: Principal) -> Principal:
    if principal.get("credential_kind") != "claude_gateway":
        raise HTTPException(status_code=401, detail="Claude gateway credential required")
    return principal


async def require_gateway_write(
    principal: Principal = Depends(require_api_key_scopes("compat:completions:write")),
) -> Principal:
    return _gateway_principal(principal)


async def require_gateway_read(
    principal: Principal = Depends(require_api_key_scopes("models:read")),
) -> Principal:
    return _gateway_principal(principal)


@public_router.get("/.well-known/oauth-authorization-server")
async def oauth_metadata():
    try:
        base = gateway.configured_base_url()
        return {
            "issuer": base,
            "device_authorization_endpoint": f"{base}/oauth/device/code",
            "token_endpoint": f"{base}/oauth/token",
            "grant_types_supported": [gateway.DEVICE_GRANT_TYPE],
            "token_endpoint_auth_methods_supported": ["none"],
            "scopes_supported": list(gateway.GATEWAY_SCOPES),
        }
    except gateway.GatewayError as exc:
        return _oauth_error(exc)


@public_router.post("/oauth/device/code")
async def oauth_device_code(request: Request):
    try:
        fields = await _fields(request)
        client_id = fields.get("client_id", "")
        await gateway.enforce_rate_limit("device-admission", f"{_remote_subject(request)}:{client_id}", limit=20)
        payload = await gateway.create_device_grant(client_id=client_id, scope=fields.get("scope"))
        return JSONResponse(payload, headers={"Cache-Control": "no-store"})
    except gateway.GatewayError as exc:
        return _oauth_error(exc)


@public_router.post("/oauth/token")
async def oauth_token(request: Request):
    try:
        fields = await _fields(request)
        payload = await gateway.exchange_device_code(
            device_code=fields.get("device_code", ""),
            grant_type=fields.get("grant_type", ""),
            client_id=fields.get("client_id", ""),
        )
        return JSONResponse(payload, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})
    except gateway.GatewayError as exc:
        return _oauth_error(exc)


@internal_router.post("/authorize")
async def authorize_device(
    body: GatewayAuthorizeBody,
    token_info: dict = Depends(require_token),
):
    try:
        await gateway.enforce_rate_limit(
            "device-approval", f"{token_info['project_id']}:{token_info['user_id']}", limit=10
        )
        return await gateway.authorize_user_code(
            user_code=body.user_code,
            approve=body.action == "approve",
            owner_user_id=token_info["user_id"],
            owner_project_id=token_info["project_id"],
        )
    except gateway.GatewayError as exc:
        return _oauth_error(exc)


async def _resolved_gateway_model() -> tuple[str, dict]:
    model, provider = gateway.configured_route()
    return model, await core.resolve_api(model, provider=provider)


@public_router.get("/v1/managed-settings")
async def managed_settings(request: Request, _: Principal = Depends(require_gateway_read)):
    del _
    try:
        model, resolved = await _resolved_gateway_model()
        payload = {
            "model": model,
            "availableModels": [model],
            "env": {"ANTHROPIC_BASE_URL": gateway.configured_base_url()},
            "gateway": {"provider": resolved["provider_name"]},
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        etag = f'"{hashlib.sha256(encoded).hexdigest()}"'
        headers = {"ETag": etag, "Cache-Control": "private, max-age=300"}
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=headers)
        return JSONResponse(payload, headers=headers)
    except core.CompletionError as exc:
        return anthropic_error_response(exc.status_code, exc.message)
    except gateway.GatewayError as exc:
        return _oauth_error(exc)


@public_router.get("/v1/models")
async def models(_: Principal = Depends(require_gateway_read)):
    del _
    try:
        model, resolved = await _resolved_gateway_model()
        return {
            "data": [
                {
                    "id": model,
                    "display_name": resolved.get("display_name") or model,
                    "description": "Lumen-managed Claude Code gateway model",
                }
            ]
        }
    except core.CompletionError as exc:
        return anthropic_error_response(exc.status_code, exc.message)
    except gateway.GatewayError as exc:
        return _oauth_error(exc)


@public_router.head("/api/hello", status_code=204)
async def hello():
    return Response(status_code=204)


@public_router.post("/v1/messages")
async def messages(
    body: AnthropicMessagesRequest,
    request: Request,
    token_info: Principal = Depends(require_gateway_write),
):
    try:
        _model, resolved = await _resolved_gateway_model()
        await core.precheck(token_info["user_id"], token_info["project_id"], api_key_id=token_info.get("api_key_id"))
        options = body.model_dump(
            exclude={"model", "provider", "messages", "max_tokens", "stream"},
            exclude_none=True,
        )
        protocol_headers = anthropic_protocol_headers(request)
        if protocol_headers:
            options["anthropic_headers"] = protocol_headers
        result = await core.complete_anthropic(
            user_id=token_info["user_id"],
            project_id=token_info["project_id"],
            api_key_id=token_info.get("api_key_id"),
            resolved=resolved,
            messages=body.messages,
            max_tokens=body.max_tokens,
            options=options,
            stream=body.stream,
        )
    except core.CompletionError as exc:
        return anthropic_error_response(exc.status_code, exc.message)
    except gateway.GatewayError as exc:
        return _oauth_error(exc)

    if not body.stream:
        return JSONResponse(result)

    async def stream() -> AsyncIterator[str]:
        try:
            async for item in events_with_ping(result, ping_seconds=15):
                if item is None:
                    yield 'event: ping\ndata: {"type":"ping"}\n\n'
                else:
                    event_type = item.get("type", "message")
                    payload = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
                    yield f"event: {event_type}\ndata: {payload}\n\n"
        except Exception:
            payload = json.dumps(anthropic_error(502, "upstream model error"), separators=(",", ":"))
            yield f"event: error\ndata: {payload}\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


@public_router.post("/v1/messages/count_tokens")
async def count_tokens(
    body: AnthropicCountTokensRequest,
    request: Request,
    token_info: Principal = Depends(require_gateway_write),
):
    del token_info
    try:
        _model, resolved = await _resolved_gateway_model()
        payload = body.model_dump(exclude={"model", "provider"}, exclude_none=True)
        return await core.count_anthropic_tokens(
            resolved=resolved,
            payload=payload,
            anthropic_headers=anthropic_protocol_headers(request),
        )
    except core.CompletionError as exc:
        return anthropic_error_response(exc.status_code, exc.message)
    except gateway.GatewayError as exc:
        return _oauth_error(exc)
