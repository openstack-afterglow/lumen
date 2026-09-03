"""외부 API 디스커버리 — GET /v1.

인증 없이(호스트 게이트만) 어떤 포맷·엔드포인트로 연결하는지 안내한다. 모델 목록은 시크릿이 아니지만
키 소유자만 보도록 /v1/models(API 키 필요)로 분리한다.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from lumen.config import get_settings

router = APIRouter()


class CompatDiscoveryAuthentication(BaseModel):
    scheme: str = "Bearer"
    compat_headers: list[str] = Field(
        default_factory=lambda: ["Authorization: Bearer sk-afgl-...", "x-api-key: sk-afgl-..."]
    )
    compat_alternatives: list[str] = Field(default_factory=lambda: ["Authorization: Bearer", "X-API-Key"])
    native_auth: str = "Project-scoped Keystone token (Authorization: Bearer ... or X-Auth-Token) 또는 scoped API key"
    issue: str = "관리 API로 발급하거나 standalone Compose의 lumen-connection manifest를 사용하세요."
    note: str = (
        "호환 API(/v1/chat/completions, /v1/messages, /v1/models)에는 API 키가 필수이며 Keystone 토큰은 거부됩니다."
    )


class CompatDiscoveryProfile(BaseModel):
    name: str
    chat_completions: str | None = None
    messages: str | None = None
    conversations: str | None = None
    completions: str | None = None
    temp_completions: str | None = None
    runs: str | None = None
    models: str | None = None
    sdk_base_url: str
    required_scopes: list[str]
    conditional_scopes: list[str] | None = None
    streaming: str


class CompatDiscoveryEndpoints(BaseModel):
    openai: dict[str, str]
    anthropic: dict[str, str]
    native: dict[str, str]


class CompatDiscoveryLinks(BaseModel):
    openapi: str
    health: str


class CompatDiscoveryHealth(BaseModel):
    endpoint: str
    note: str = (
        "프로세스 생존 상태(process health)만 나타내며 datastore/worker 준비 상태(readiness)와 분리되어 있습니다."
    )


class CompatDiscoveryIdempotency(BaseModel):
    header: str = "Idempotency-Key"
    format: str = "UUID string"
    behavior: str = (
        "유효하지 않은 UUID는 422 Unprocessable Entity를 반환하며, 멱등 재조회 성공 시 쿼터 재검사를 우회합니다."
    )


class CompatDiscoverySSE(BaseModel):
    openai: str = "data: { ... } \\n\\n data: [DONE]"
    anthropic: str = "event: message_start \\n data: { ... } \\n\\n"
    native: str = "event: <ChatRunEvent.type> \\n data: ChatRunEvent JSON"
    error_note: str = (
        "Compat streams report post-handshake failures as in-band SSE errors; "
        "native durable failures use the terminal run.failed event."
    )


class CompatDiscoveryHostGate(BaseModel):
    gated_routes: list[str] = Field(
        default_factory=lambda: ["/v1/compat", "/v1/chat/completions", "/v1/messages", "/v1/models"]
    )
    behavior: str = "chat_api_hosts 설정을 통해 허용된 Host 헤더 요청만 통과하며 미허용 시 404로 응답합니다."


class CompatDiscoveryResponse(BaseModel):
    version: str = "1.0.0"
    contract_version: str = "1.0.0"
    service: str = "Lumen AI API"
    description: str = (
        "OpenAI / Anthropic 호환 및 Lumen 네이티브 영속 대화 API. 발급한 API 키로 외부 SDK 또는 웹에서 연결하세요."
    )
    formats: list[str] = Field(default_factory=lambda: ["openai", "anthropic", "lumen_native"])
    profiles: dict[str, CompatDiscoveryProfile]
    links: CompatDiscoveryLinks
    health: CompatDiscoveryHealth
    authentication: CompatDiscoveryAuthentication
    endpoints: CompatDiscoveryEndpoints
    models_endpoint: str
    features: list[str]
    notes: list[str]
    idempotency: CompatDiscoveryIdempotency
    sse: CompatDiscoverySSE
    host_gate: CompatDiscoveryHostGate


@router.get("/compat", response_model=CompatDiscoveryResponse)
async def discovery(request: Request) -> CompatDiscoveryResponse:
    origin = (get_settings().public_api_base or str(request.base_url)).rstrip("/")
    return CompatDiscoveryResponse(
        version="1.0.0",
        contract_version="1.0.0",
        service="Lumen AI API",
        description="OpenAI / Anthropic 호환 및 Lumen 네이티브 대화 API. model='lumen'은 백엔드 durable execution으로 동작하며 provider model ID는 stateless로 중계됩니다.",
        formats=["openai", "anthropic", "lumen_native"],
        profiles={
            "openai_lumen": CompatDiscoveryProfile(
                name="OpenAI-compatible Lumen durable completion",
                chat_completions=f"{origin}/v1/chat/completions",
                models=f"{origin}/v1/models",
                sdk_base_url=f"{origin}/v1",
                required_scopes=["compat:completions:write", "models:read"],
                streaming="text/event-stream (data: JSON)",
            ),
            "openai_stateless": CompatDiscoveryProfile(
                name="OpenAI-compatible stateless provider completion",
                chat_completions=f"{origin}/v1/chat/completions",
                models=f"{origin}/v1/models",
                sdk_base_url=f"{origin}/v1",
                required_scopes=["compat:completions:write", "models:read"],
                streaming="text/event-stream (data: JSON)",
            ),
            "anthropic_stateless": CompatDiscoveryProfile(
                name="Anthropic-compatible stateless completion",
                messages=f"{origin}/v1/messages",
                models=f"{origin}/v1/models",
                sdk_base_url=origin,
                required_scopes=["compat:completions:write", "models:read"],
                streaming="text/event-stream (event: <type>\\ndata: JSON)",
            ),
            "lumen_native": CompatDiscoveryProfile(
                name="Lumen native durable runs",
                conversations=f"{origin}/v1/conversations",
                completions=f"{origin}/v1/conversations/{{conversation_id}}/completions",
                temp_completions=f"{origin}/v1/temp-completions",
                runs=f"{origin}/v1/runs",
                sdk_base_url=origin,
                required_scopes=[
                    "native:conversations:read",
                    "native:conversations:write",
                    "native:runs:read",
                    "native:runs:write",
                    "models:read",
                ],
                conditional_scopes=[
                    "native:memory:read",
                    "native:memory:write",
                    "native:tools:execute",
                    "native:extensions:read",
                    "native:agents:use",
                ],
                streaming="text/event-stream (event: <ChatRunEvent.type>\\ndata: ChatRunEvent JSON)",
            ),
        },
        links=CompatDiscoveryLinks(
            openapi=f"{origin}/openapi.json",
            health=f"{origin}/v1/health",
        ),
        health=CompatDiscoveryHealth(
            endpoint=f"{origin}/v1/health",
            note="프로세스 생존 상태(process health)만 나타내며 datastore/worker 준비 상태(readiness)와 분리되어 있습니다.",
        ),
        authentication=CompatDiscoveryAuthentication(),
        endpoints=CompatDiscoveryEndpoints(
            openai={
                "chat_completions": f"{origin}/v1/chat/completions",
                "models": f"{origin}/v1/models",
                "sdk_base_url": f"{origin}/v1",
            },
            anthropic={
                "messages": f"{origin}/v1/messages",
                "sdk_base_url": origin,
            },
            native={
                "conversations": f"{origin}/v1/conversations",
                "temp_completions": f"{origin}/v1/temp-completions",
                "runs": f"{origin}/v1/runs",
                "sdk_base_url": origin,
            },
        ),
        models_endpoint=f"{origin}/v1/models",
        features=[
            "model='lumen': text-only durable worker streaming (caller tools/memory 비활성화)",
            "provider model ID: tools(function calling) pass-through 및 vision 지원",
            "Idempotency-Key UUID 지원 (네이티브 202 멱등 재시도 및 쿼터 재검사 예외)",
        ],
        notes=[
            "OpenAI 호환 route에서 model='lumen'은 durable worker 실행으로 처리되며, provider model ID는 stateless 중계로 동작합니다.",
            "사용량은 발급 사용자의 지갑·월 쿼터에서 차감됩니다.",
            "사용량은 웹과 분리된 API 통계(키별)로 집계됩니다.",
            "OpenAI 호환 오류는 최상위 {error: {message, type, code}} 구조를 사용합니다.",
        ],
        idempotency=CompatDiscoveryIdempotency(),
        sse=CompatDiscoverySSE(),
        host_gate=CompatDiscoveryHostGate(),
    )
