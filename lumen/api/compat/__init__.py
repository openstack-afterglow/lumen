"""외부 OpenAI/Anthropic 호환 API (/v1) — API 키 인증, stateless.

⚠️ 최상위 /v1 마운트는 "모든 라우터 /api/v1 단독" 규정의 명시적 예외다(외부 SDK 표준 경로 호환).
"""

_ROUTERS = {
    "openai_compat_router": ".openai",
    "anthropic_compat_router": ".anthropic",
    "ai_discovery_router": ".discovery",
}


def __getattr__(name: str):
    if name in _ROUTERS:
        import importlib

        mod = importlib.import_module(_ROUTERS[name], __package__)
        return mod.router
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = list(_ROUTERS)
