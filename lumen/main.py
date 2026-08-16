"""Lumen FastAPI service application entrypoint."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
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


def run() -> None:
    import uvicorn

    uvicorn.run("lumen.main:app", host="0.0.0.0", port=8012, reload=False)


if __name__ == "__main__":
    run()
