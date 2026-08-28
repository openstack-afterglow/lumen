"""Lumen FastAPI service application entrypoint."""
# Router imports intentionally follow application construction to avoid import-time cycles.
# ruff: noqa: E402

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from pydantic import BaseModel

from lumen.cache import close_cache
from lumen.config import get_settings
from lumen.db import close_db, init_db

logger = logging.getLogger(__name__)


class LinkDescription(BaseModel):
    rel: str
    href: str


class VersionDescription(BaseModel):
    id: str = "v1.0"
    status: str = "CURRENT"
    min_version: str = "1.0"
    version: str = "1.0"
    links: list[LinkDescription]


class RootDiscoveryResponse(BaseModel):
    versions: list[VersionDescription]


class VersionDiscoveryResponse(BaseModel):
    version: VersionDescription


class HealthResponse(BaseModel):
    status: str = "ok"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    if settings.database_url:
        init_db(
            settings.database_url,
            pool_size=settings.database_pool_size,
            max_overflow=settings.database_max_overflow,
            connect_timeout=settings.database_connect_timeout,
            pool_timeout=settings.database_pool_timeout,
            unhealthy_seconds=settings.database_unhealthy_seconds,
        )

    if settings.chat_checkpointer_postgres_url:
        from lumen.services.checkpointer import chat_checkpointer

        ok = await chat_checkpointer.start(settings.chat_checkpointer_postgres_url)
        if not ok:
            raise RuntimeError("configured chat checkpointer failed to initialize")

    if settings.chat_semantic_memory_enabled:
        from lumen.services.semantic_memory import setup_semantic_memory

        await setup_semantic_memory()

    yield

    try:
        from lumen.services.checkpointer import chat_checkpointer

        await chat_checkpointer.close()
    except Exception:
        pass
    await close_cache()
    await close_db()


app = FastAPI(
    title="Lumen",
    description="Lumen durable agent, LLM, and chat service API",
    version="1.0.0",
    lifespan=lifespan,
)

settings = get_settings()
origins = settings.cors_origin_list
if origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


@app.get("/", tags=["Discovery"], response_model=RootDiscoveryResponse)
async def root_discovery(request: Request):
    base_url = str(request.base_url).rstrip("/")
    return {
        "versions": [
            {
                "id": "v1.0",
                "status": "CURRENT",
                "min_version": "1.0",
                "version": "1.0",
                "links": [{"rel": "self", "href": f"{base_url}/v1/"}],
            }
        ]
    }


@app.get("/v1/", tags=["Discovery"], response_model=VersionDiscoveryResponse)
async def v1_discovery(request: Request):
    base_url = str(request.base_url).rstrip("/")
    return {
        "version": {
            "id": "v1.0",
            "status": "CURRENT",
            "min_version": "1.0",
            "version": "1.0",
            "links": [{"rel": "self", "href": f"{base_url}/v1/"}],
        }
    }


@app.get("/v1/health", tags=["Health"], response_model=HealthResponse)
async def health():
    return {"status": "ok"}


from lumen.api import (
    chat_admin_router,
    chat_agents_router,
    chat_api_keys_router,
    chat_assets_router,
    chat_code_workspaces_router,
    chat_completions_router,
    chat_conversations_router,
    chat_extensions_admin_router,
    chat_extensions_user_router,
    chat_mcp_oauth_router,
    chat_memory_router,
    chat_stats_router,
    chat_usage_router,
    chat_workspaces_router,
)
from lumen.api.compat import anthropic as compat_anthropic
from lumen.api.compat import discovery as compat_discovery
from lumen.api.compat import openai as compat_openai
from lumen.auth import require_chat_api_host

# Chat routers already declare their complete route paths. Mount once at `/v1`
# so the SDK and Afterglow BFF preserve the public contract exactly.
for router, tag in (
    (chat_conversations_router, "Chat Conversations"),
    (chat_completions_router, "Chat Completions"),
    (chat_agents_router, "Chat Agents"),
    (chat_workspaces_router, "Chat Workspaces"),
    (chat_code_workspaces_router, "Chat Code Workspaces"),
    (chat_memory_router, "Chat Memory"),
    (chat_assets_router, "Chat Assets"),
    (chat_api_keys_router, "Chat API Keys"),
    (chat_mcp_oauth_router, "Chat MCP OAuth"),
    (chat_extensions_admin_router, "Chat Extensions Admin"),
    (chat_extensions_user_router, "Chat Extensions"),
    (chat_usage_router, "Chat Usage"),
    (chat_stats_router, "Chat Stats"),
    (chat_admin_router, "Chat Administration"),
):
    app.include_router(router, prefix="/v1", tags=[tag])

# The external AI-compatible API is direct-to-Lumen and host-gated. Browser
# `/api/v1/chat/*` traffic remains an Afterglow proxy concern.
for router, tag in (
    (compat_discovery.router, "AI Compat Discovery"),
    (compat_openai.router, "OpenAI Compat"),
    (compat_anthropic.router, "Anthropic Compat"),
):
    app.include_router(router, prefix="/v1", tags=[tag], dependencies=[Depends(require_chat_api_host)])


def custom_openapi() -> dict:
    """Document API-key compatibility schemes without changing native route semantics."""
    if app.openapi_schema:
        return app.openapi_schema
    from lumen.models.chat_contracts import _CHAT_RUN_EVENT

    schema = get_openapi(title=app.title, version=app.version, description=app.description, routes=app.routes)
    schema["x-contract-version"] = "1.0.0"
    schema["x-profiles"] = {
        "openai_stateless": "OpenAI-compatible stateless completion (/v1/chat/completions)",
        "anthropic_stateless": "Anthropic-compatible stateless completion (/v1/messages)",
        "lumen_native": "Lumen native durable runs (/v1/conversations/{conversation_id}/completions, /v1/temp-completions, /v1/runs/...)",
    }

    security_schemes = schema.setdefault("components", {}).setdefault("securitySchemes", {})
    for scheme_name, description in (
        ("KeystoneToken", "Project-scoped Keystone token in X-Auth-Token."),
        ("KeystoneBearer", "Project-scoped Keystone token in Authorization: Bearer."),
    ):
        if scheme_name in security_schemes:
            security_schemes[scheme_name]["description"] = description
    security_schemes["APIKeyBearer"] = {
        "type": "http",
        "scheme": "bearer",
        "description": "Bearer API key (Authorization: Bearer <key>)",
    }
    security_schemes["XApiKey"] = {
        "type": "apiKey",
        "in": "header",
        "name": "x-api-key",
        "description": "API key header (x-api-key: <key>)",
    }
    api_key_security = [{"APIKeyBearer": []}, {"XApiKey": []}]
    for path, method in (("/v1/chat/completions", "post"), ("/v1/messages", "post"), ("/v1/models", "get")):
        if path in schema.get("paths", {}) and method in schema["paths"][path]:
            schema["paths"][path][method]["security"] = api_key_security

    route_scopes: dict[tuple[str, str], list[str]] = {
        ("/v1/chat/completions", "post"): ["compat:completions:write"],
        ("/v1/messages", "post"): ["compat:completions:write"],
        ("/v1/models", "get"): ["models:read"],
        ("/v1/chat/models", "get"): ["models:read"],
        ("/v1/capabilities", "get"): ["models:read"],
        ("/v1/conversations", "post"): ["native:conversations:write"],
        ("/v1/conversations", "get"): ["native:conversations:read"],
        ("/v1/conversations/search", "get"): ["native:conversations:read"],
        ("/v1/conversations/{conversation_id}", "get"): ["native:conversations:read"],
        ("/v1/conversations/{conversation_id}", "delete"): ["native:conversations:write"],
        ("/v1/conversations/{conversation_id}/workspace", "patch"): ["native:conversations:write"],
        ("/v1/conversations/{conversation_id}/messages", "get"): ["native:conversations:read"],
        ("/v1/conversations/{conversation_id}/active-leaf", "patch"): ["native:conversations:write"],
        ("/v1/conversations/{conversation_id}/fork", "post"): ["native:conversations:write"],
        ("/v1/conversations/{conversation_id}/completions", "post"): [
            "native:conversations:write",
            "native:runs:write",
        ],
        ("/v1/temp-completions", "post"): ["native:runs:write"],
        ("/v1/conversations/{conversation_id}/messages/{message_id}/regenerate", "post"): [
            "native:conversations:write",
            "native:runs:write",
        ],
        ("/v1/conversations/{conversation_id}/runs/{run_id}/retry", "post"): [
            "native:conversations:write",
            "native:runs:write",
        ],
        ("/v1/runs/{run_id}", "get"): ["native:runs:read"],
        ("/v1/runs/{run_id}/events", "get"): ["native:runs:read"],
        ("/v1/conversations/{conversation_id}/runs", "get"): ["native:runs:read"],
        ("/v1/runs", "get"): ["native:runs:read"],
        ("/v1/temp-threads/{temp_thread_id}", "get"): ["native:runs:read"],
        ("/v1/runs/{run_id}/cancel", "post"): ["native:runs:write"],
        ("/v1/runs/{run_id}/approvals/{call_id}", "post"): ["native:runs:write"],
        ("/v1/runs/{run_id}/interactions/{interaction_id}", "post"): ["native:runs:write"],
        ("/v1/usage", "get"): ["usage:read"],
        ("/v1/usage/timeseries", "get"): ["usage:read"],
        ("/v1/usage/keys", "get"): ["usage:read"],
        ("/v1/usage/records", "get"): ["usage:read"],
    }
    admission_posts = {
        ("/v1/conversations/{conversation_id}/completions", "post"),
        ("/v1/conversations/{conversation_id}/messages/{message_id}/regenerate", "post"),
        ("/v1/conversations/{conversation_id}/runs/{run_id}/retry", "post"),
        ("/v1/temp-completions", "post"),
    }
    conditional_scopes = {
        "features.memory=true": ["native:memory:read", "native:memory:write"],
        "tool policy, web search/fetch, or advisor enabled": ["native:tools:execute"],
        "skill or selected custom/MCP extension": ["native:extensions:read"],
        "selected custom/MCP extension": ["native:tools:execute"],
        "agent_id selected": ["native:agents:use"],
    }
    for (path, method), scopes in route_scopes.items():
        if path in schema.get("paths", {}) and method in schema["paths"][path]:
            op = schema["paths"][path][method]
            op["x-required-api-key-scopes"] = scopes
            if (path, method) in admission_posts:
                op["x-conditional-api-key-scopes"] = conditional_scopes
    sse_routes = [
        ("/v1/chat/completions", "post", "OpenAI SSE stream (data: JSON \\n\\n data: [DONE])"),
        ("/v1/messages", "post", "Anthropic SSE stream (event: <type> \\n data: JSON)"),
        ("/v1/runs/{run_id}/events", "get", "Native ChatRunEvent journal stream"),
    ]
    for path, method, stream_desc in sse_routes:
        if path in schema.get("paths", {}) and method in schema["paths"][path]:
            responses = schema["paths"][path][method].setdefault("responses", {})
            resp_200 = responses.setdefault("200", {"description": "Successful Response"})
            content = resp_200.setdefault("content", {})
            if path == "/v1/runs/{run_id}/events":
                content["text/event-stream"] = {
                    "schema": {"type": "string", "format": "event-stream"},
                    "x-event-data-schema": {"$ref": "#/components/schemas/ChatRunEvent"},
                    "description": stream_desc,
                }
            else:
                content.setdefault(
                    "text/event-stream",
                    {
                        "schema": {"type": "string", "format": "event-stream"},
                        "description": stream_desc,
                    },
                )

    event_schema = _CHAT_RUN_EVENT.json_schema(ref_template="#/components/schemas/{model}")
    defs = event_schema.pop("$defs", {})
    components_schemas = schema.setdefault("components", {}).setdefault("schemas", {})
    for def_name, def_schema in defs.items():
        components_schemas[def_name] = def_schema
    components_schemas["ChatRunEvent"] = event_schema

    def _fix_refs(obj: Any) -> None:
        if isinstance(obj, dict):
            for k, v in list(obj.items()):
                if k == "$ref" and isinstance(v, str) and v.startswith("#/$defs/"):
                    obj[k] = v.replace("#/$defs/", "#/components/schemas/")
                else:
                    _fix_refs(v)
        elif isinstance(obj, list):
            for item in obj:
                _fix_refs(item)

    _fix_refs(schema)
    app.openapi_schema = schema
    return schema


app.openapi = custom_openapi


def run() -> None:
    import uvicorn

    uvicorn.run("lumen.main:app", host="0.0.0.0", port=8012, reload=False)


if __name__ == "__main__":
    run()
