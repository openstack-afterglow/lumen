"""외부 API 디스커버리 — GET /v1.

인증 없이(호스트 게이트만) 어떤 포맷·엔드포인트로 연결하는지 안내한다. 모델 목록은 시크릿이 아니지만
키 소유자만 보도록 /v1/models(API 키 필요)로 분리한다.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/compat")
async def discovery(request: Request) -> dict:
    base = str(request.base_url).rstrip("/")
    return {
        "service": "Afterglow Chat API",
        "description": "OpenAI / Anthropic 호환 채팅 API. 발급한 API 키로 외부 SDK에서 연결하세요.",
        "formats": ["openai", "anthropic"],
        "authentication": {
            "scheme": "Bearer",
            "headers": ["Authorization: Bearer sk-afgl-...", "x-api-key: sk-afgl-..."],
            "issue": "웹 대시보드 → 채팅 → 설정 → API 키 에서 발급",
        },
        "endpoints": {
            "openai": {
                "chat_completions": f"{base}/v1/chat/completions",
                "models": f"{base}/v1/models",
                "sdk_base_url": f"{base}/v1",
            },
            "anthropic": {
                "messages": f"{base}/v1/messages",
                "sdk_base_url": base,
            },
        },
        "models_endpoint": f"{base}/v1/models  (API 키 필요 — 사용 가능한 모델 목록)",
        "features": [
            "stream: true 스트리밍 지원",
            "tools(function calling) pass-through — 모델이 tool_calls 반환, 클라이언트가 실행",
            "이미지 입력(vision) — vision 지원 모델",
        ],
        "notes": [
            "사용량은 발급 사용자의 지갑·월 쿼터에서 차감됩니다.",
            "사용량은 웹과 분리된 API 통계(키별)로 집계됩니다.",
        ],
    }
