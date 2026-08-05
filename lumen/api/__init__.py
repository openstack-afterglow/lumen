"""빌트인 AI 채팅 API 라우터 — lazy import to reduce startup time.

값은 모듈 경로(str, 기본 attr "router") 또는 (모듈 경로, attr) 튜플.
"""

_ROUTERS = {
    "chat_usage_router": ".usage",
    "chat_stats_router": ".stats",
    "chat_agents_router": ".agents",
    "chat_workspaces_router": ".workspaces",
    "chat_code_workspaces_router": ".code_workspaces",
    "chat_memory_router": ".memory",
    "chat_admin_router": ".models",
    "chat_conversations_router": ".conversations",
    "chat_completions_router": ".completions",
    "chat_assets_router": ".assets",
    "chat_extensions_admin_router": (".extensions", "admin_router"),
    "chat_extensions_user_router": (".extensions", "user_router"),
    "chat_api_keys_router": ".api_keys",
    "chat_mcp_oauth_router": ".mcp_oauth",
}


def __getattr__(name: str):
    if name in _ROUTERS:
        import importlib

        spec = _ROUTERS[name]
        module, attr = spec if isinstance(spec, tuple) else (spec, "router")
        mod = importlib.import_module(module, __package__)
        return getattr(mod, attr)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = list(_ROUTERS)
