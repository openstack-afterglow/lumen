"""LLM 프로바이더의 모델 목록 discovery.

우선순위:
1. 라이브 API — OpenAI 호환 `{api_base}/models` (또는 provider_type 기본 base)를 호출해 모델 id 수집.
2. 실패/미지원 시 litellm 정적 레지스트리(models_by_provider)로 fallback.

프로바이더 api_base 는 관리자(require_admin)가 등록한 신뢰 값이므로 SSRF 가드를 적용하지 않는다
(ollama localhost 등 내부 엔드포인트를 정상 허용해야 함 — 사용자 커스텀 툴과 신뢰 경계가 다르다).
복호화 api_key 는 discovery 호출에만 쓰고 응답/로그에 노출하지 않는다.
"""

from __future__ import annotations

import logging

from lumen.services import provider_store

logger = logging.getLogger(__name__)

# provider_type → OpenAI 호환 /models 기본 base (api_base 미지정 시 사용)
_DEFAULT_BASES: dict[str, str] = {
    "openai": "https://api.openai.com/v1",
    "groq": "https://api.groq.com/openai/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "mistral": "https://api.mistral.ai/v1",
    "together_ai": "https://api.together.xyz/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "xai": "https://api.x.ai/v1",
    "perplexity": "https://api.perplexity.ai",
    "ollama": "http://localhost:11434/v1",
}
# OpenAI 호환 /models 엔드포인트를 노출하는 provider_type
_OPENAI_COMPATIBLE = set(_DEFAULT_BASES) | {"azure"}


async def _fetch_openai_compatible(provider_type: str, api_base: str | None, api_key: str | None) -> list[str]:
    base = api_base or _DEFAULT_BASES.get(provider_type)
    if not base:
        return []
    url = base.rstrip("/") + "/models"
    try:
        import httpx

        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as cx:
            resp = await cx.get(url, headers=headers)
        if resp.status_code != 200:
            logger.info("모델 목록 조회 비정상 status=%s ptype=%s", resp.status_code, provider_type)
            return []
        data = resp.json()
        items = data.get("data") if isinstance(data, dict) else data
        ids: list[str] = []
        for it in items or []:
            mid = it.get("id") if isinstance(it, dict) else it
            if mid:
                ids.append(str(mid))
        return sorted(set(ids))
    except Exception:
        logger.warning("라이브 모델 목록 조회 실패 ptype=%s", provider_type, exc_info=True)
        return []


def _litellm_static(provider_type: str) -> list[str]:
    try:
        import litellm

        mbp = getattr(litellm, "models_by_provider", None) or {}
        return sorted({str(m) for m in mbp.get(provider_type, [])})
    except Exception:
        logger.warning("litellm 정적 모델 목록 실패 ptype=%s", provider_type, exc_info=True)
        return []


async def discover_models(provider_id: int) -> dict:
    """프로바이더의 사용 가능 모델 목록. {"models": [...], "source": "api"|"litellm"|"none"}."""
    prov = await provider_store.get_provider_for_discovery(provider_id)
    if prov is None:
        raise provider_store.ProviderNotFoundError(f"프로바이더 {provider_id} 를 찾을 수 없습니다")

    ptype = prov["provider_type"]
    if ptype in _OPENAI_COMPATIBLE:
        live = await _fetch_openai_compatible(ptype, prov["api_base"], prov["api_key"])
        if live:
            return {"models": live, "source": "api"}

    static = _litellm_static(ptype)
    return {"models": static, "source": "litellm" if static else "none"}
