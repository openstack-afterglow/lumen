"""Bounded, secret-safe provider model discovery for the admin review flow.

Live API lists are authoritative for API-key providers. A failed or incomplete live
fetch never becomes a usable static list. Subscription and unsupported providers
receive a clearly marked LiteLLM reference catalog instead. Administrator-owned
custom bases are trusted (including local endpoints), but redirects and environment
proxies are disabled so credentials cannot be forwarded to another origin.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlencode

from lumen.services.providers import errors
from lumen.services.providers import routing as provider_store
from lumen.services.providers.credentials import (
    api_model_name,
    canonical_subscription_model_name,
)

logger = logging.getLogger(__name__)

# --- 안전 한도(신뢰된 admin api_base 라도 discovery 호출 하나가 무한정 자원을 쓰지 않게 한다) ---
_REQUEST_TIMEOUT_SECONDS = 10.0
_TOTAL_TIMEOUT_SECONDS = 20.0
_MAX_PAGES = 25
_MAX_MODELS = 2000
_MAX_MODEL_ID_LEN = 190  # ModelCreateRequest.model_name 저장 한도
_MAX_RESPONSE_BYTES = 5_000_000  # decoded bytes across every page of one discovery fetch

_ANTHROPIC_DEFAULT_BASE = "https://api.anthropic.com/v1"
_ANTHROPIC_API_VERSION = "2023-06-01"
_ANTHROPIC_PAGE_LIMIT = 200

_GEMINI_DEFAULT_BASE = "https://generativelanguage.googleapis.com/v1beta"
_GEMINI_PAGE_SIZE = 200
_GEMINI_CHAT_METHODS = frozenset({"generateContent", "streamGenerateContent", "bidiGenerateContent", "generateMessage"})

# provider_type → OpenAI 호환 /models 기본 base (api_base 미지정 시 사용)
_DEFAULT_BASES: dict[str, str] = {
    "openai": "https://api.openai.com/v1",
    "groq": "https://api.groq.com/openai/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "mistral": "https://api.mistral.ai/v1",
    "together_ai": "https://api.together.xyz/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "xai": "https://api.x.ai/v1",
    "perplexity": "https://api.perplexity.ai/v1",
    "ollama": "http://localhost:11434/v1",
}
# OpenAI 호환 /models 엔드포인트를 노출하는 provider_type.
# azure 는 제외: 동일 이름이라도 실제 deployment discovery 는 api-version query 와 별도 경로가
# 필요해 일반 OpenAI 호환 /models 계약으로 라이브 조회를 대표할 수 없다(정적 참고 목록만 제공).
_OPENAI_COMPATIBLE = set(_DEFAULT_BASES)


def _models_url(provider_type: str, api_base: str | None) -> str | None:
    base = (api_base or _DEFAULT_BASES.get(provider_type) or "").rstrip("/")
    if not base:
        return None
    if provider_type != "perplexity":
        return f"{base}/models"
    lowered = base.lower()
    if lowered.endswith("/router"):
        return f"{base}/v1/models"
    if lowered.endswith("/router/v1") or lowered.endswith("/v1"):
        return f"{base}/models"
    return f"{base}/v1/models"


# ---------------------------------------------------------------------------
# 후보/결과 모델
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class DiscoveredCandidate:
    id: str
    display_name: str | None = None
    purpose: Literal["chat", "non_chat", "unknown"] = "unknown"
    generation_methods: tuple[str, ...] = ()
    input_token_limit: int | None = None
    output_token_limit: int | None = None


@dataclass(frozen=True)
class _FetchOutcome:
    status: Literal["success", "empty", "error"]
    candidates: tuple[DiscoveredCandidate, ...] = ()
    error_code: str | None = None
    error_message: str | None = None
    retryable: bool = False


@dataclass(frozen=True)
class _PageResult:
    items: list[dict]
    next_cursor: str | None
    more: bool


class _DiscoveryFailure(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False):
        self.code = code
        self.message = message
        self.retryable = retryable
        super().__init__(message)


class _MalformedPage(Exception):
    """Invalid upstream JSON shape; never publish models from earlier pages."""


def _error_outcome(code: str, message: str, retryable: bool) -> _FetchOutcome:
    return _FetchOutcome("error", (), code, message, retryable)


def _serialize_candidate(candidate: DiscoveredCandidate) -> dict[str, Any]:
    return {
        "id": candidate.id,
        "display_name": candidate.display_name,
        "purpose": candidate.purpose,
        "generation_methods": list(candidate.generation_methods),
        "input_token_limit": candidate.input_token_limit,
        "output_token_limit": candidate.output_token_limit,
    }


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _map_http_status(status_code: int) -> tuple[str, str, bool] | None:
    if status_code == 200:
        return None
    if status_code == 401:
        return ("discovery_invalid_key", "프로바이더 API 키가 유효하지 않습니다", False)
    if status_code == 403:
        return ("discovery_permission_denied", "프로바이더가 모델 목록 접근을 거부했습니다", False)
    if status_code == 429:
        return ("discovery_rate_limited", "프로바이더 요청이 제한되었습니다", True)
    if 300 <= status_code < 400:
        return ("discovery_redirect_blocked", "프로바이더가 리다이렉트를 반환했습니다", False)
    if status_code >= 500:
        return ("discovery_upstream_unavailable", "프로바이더를 사용할 수 없습니다", True)
    return ("discovery_request_failed", "프로바이더 요청이 실패했습니다", False)


async def _get(client: Any, url: str, headers: dict[str, str], budget: list[int]) -> Any:
    """Decode at most the remaining bytes, including decompressed response content."""
    async with client.stream(
        "GET", url, headers={**headers, "Accept": "application/json", "Accept-Encoding": "identity"}
    ) as response:
        status_error = _map_http_status(response.status_code)
        if status_error is not None:
            raise _DiscoveryFailure(*status_error)
        if response.headers.get("Content-Encoding", "identity").lower() != "identity":
            raise _DiscoveryFailure("discovery_encoding_unsupported", "프로바이더 응답 인코딩을 확인할 수 없습니다")
        body = bytearray()
        async for chunk in response.aiter_bytes(chunk_size=65536):
            if len(chunk) > budget[0]:
                raise _DiscoveryFailure("discovery_response_too_large", "프로바이더 응답이 허용 크기를 초과했습니다")
            budget[0] -= len(chunk)
            body.extend(chunk)
    try:
        return json.loads(body)
    except (ValueError, UnicodeDecodeError):
        raise _DiscoveryFailure("discovery_malformed_response", "프로바이더 응답을 해석할 수 없습니다") from None


# ---------------------------------------------------------------------------
# 공통 페이지네이션 엔진 — Anthropic/Gemini native 라이브 조회에 재사용
# ---------------------------------------------------------------------------
async def _paginate(
    *,
    build_url: Any,
    headers: dict[str, str],
    parse_page: Any,
    to_candidate: Any,
) -> _FetchOutcome:
    import httpx

    cursor: str | None = None
    seen_cursors: set[str] = set()
    seen_ids: set[str] = set()
    candidates: list[DiscoveredCandidate] = []
    budget = [_MAX_RESPONSE_BYTES]
    try:
        async with asyncio.timeout(_TOTAL_TIMEOUT_SECONDS):
            async with httpx.AsyncClient(
                timeout=_REQUEST_TIMEOUT_SECONDS, follow_redirects=False, trust_env=False
            ) as client:
                for page_index in range(_MAX_PAGES):
                    payload = await _get(client, build_url(cursor), headers, budget)
                    try:
                        page = parse_page(payload)
                    except _MalformedPage:
                        raise _DiscoveryFailure(
                            "discovery_malformed_response", "프로바이더 응답 형식이 올바르지 않습니다"
                        ) from None
                    for index, item in enumerate(page.items):
                        if index % 128 == 0:
                            await asyncio.sleep(0)
                        candidate = to_candidate(item)
                        if candidate is None:
                            raise _DiscoveryFailure(
                                "discovery_malformed_response", "프로바이더 모델 항목 형식이 올바르지 않습니다"
                            )
                        if len(candidate.id) > _MAX_MODEL_ID_LEN:
                            raise _DiscoveryFailure("discovery_model_limit", "모델 ID가 저장 한도를 초과했습니다")
                        if candidate.id in seen_ids:
                            continue
                        if len(candidates) >= _MAX_MODELS:
                            raise _DiscoveryFailure("discovery_model_limit", "모델 목록 조회 한도를 초과했습니다")
                        seen_ids.add(candidate.id)
                        candidates.append(candidate)
                    if not page.more:
                        return _FetchOutcome("success" if candidates else "empty", tuple(candidates))
                    if not page.next_cursor or page.next_cursor in seen_cursors:
                        raise _DiscoveryFailure("discovery_malformed_response", "페이지 커서를 확인할 수 없습니다")
                    if page_index + 1 >= _MAX_PAGES:
                        raise _DiscoveryFailure("discovery_page_limit", "모델 목록 페이지 한도를 초과했습니다")
                    seen_cursors.add(page.next_cursor)
                    cursor = page.next_cursor
    except TimeoutError:
        return _error_outcome("discovery_timeout", "모델 목록 조회 시간이 초과되었습니다", True)
    except httpx.TimeoutException:
        return _error_outcome("discovery_timeout", "프로바이더 응답이 시간 내에 도착하지 않았습니다", True)
    except httpx.HTTPError:
        return _error_outcome("discovery_upstream_unavailable", "프로바이더에 연결할 수 없습니다", True)
    except _DiscoveryFailure as exc:
        return _error_outcome(exc.code, exc.message, exc.retryable)
    raise AssertionError("unreachable pagination state")


# ---------------------------------------------------------------------------
# Anthropic(API-key) 라이브 조회 — https://platform.claude.com/docs/en/api/models/list
# ---------------------------------------------------------------------------
def _anthropic_base(api_base: str | None) -> str:
    base = (api_base or _ANTHROPIC_DEFAULT_BASE).rstrip("/") or _ANTHROPIC_DEFAULT_BASE
    return base if base.lower().endswith("/v1") else f"{base}/v1"


def _anthropic_parse_page(payload: Any) -> _PageResult:
    if not isinstance(payload, dict):
        raise _MalformedPage
    data = payload.get("data")
    if not isinstance(data, list):
        raise _MalformedPage
    items = [it for it in data if isinstance(it, dict)]
    if len(items) != len(data):
        raise _MalformedPage
    has_more = payload.get("has_more")
    last_id = payload.get("last_id")
    if not isinstance(has_more, bool) or (last_id is not None and not isinstance(last_id, str)):
        raise _MalformedPage
    if has_more and (not last_id or last_id != last_id.strip()):
        raise _MalformedPage
    return _PageResult(items=items, next_cursor=last_id if has_more else None, more=has_more)


def _anthropic_candidate(item: dict) -> DiscoveredCandidate | None:
    raw_id = item.get("id")
    if not isinstance(raw_id, str) or not raw_id or raw_id != raw_id.strip():
        return None
    display_name = item.get("display_name")
    display_name = display_name.strip() if isinstance(display_name, str) and display_name.strip() else None
    input_limit = item.get("max_input_tokens")
    output_limit = item.get("max_tokens")
    input_limit = input_limit if type(input_limit) is int and input_limit > 0 else None
    output_limit = output_limit if type(output_limit) is int and output_limit > 0 else None
    return DiscoveredCandidate(
        id=raw_id,
        display_name=display_name,
        purpose="chat",  # /v1/models 는 Messages API에서 쓰는 모델만 나열한다(공식 문서 기준)
        generation_methods=(),
        input_token_limit=input_limit,
        output_token_limit=output_limit,
    )


async def _fetch_anthropic_models(api_base: str | None, api_key: str | None) -> _FetchOutcome:
    if not api_key:
        return _error_outcome("discovery_credential_missing", "프로바이더 API 키가 설정되지 않았습니다", False)
    base = _anthropic_base(api_base)
    headers = {"x-api-key": api_key, "anthropic-version": _ANTHROPIC_API_VERSION}

    def build_url(cursor: str | None) -> str:
        query = {"limit": str(_ANTHROPIC_PAGE_LIMIT)}
        if cursor:
            query["after_id"] = cursor
        return f"{base}/models?{urlencode(query)}"

    return await _paginate(
        build_url=build_url,
        headers=headers,
        parse_page=_anthropic_parse_page,
        to_candidate=_anthropic_candidate,
    )


# ---------------------------------------------------------------------------
# Gemini(API-key) native 라이브 조회 — https://ai.google.dev/api/models
# ---------------------------------------------------------------------------
def _gemini_base(api_base: str | None) -> str:
    return (api_base or _GEMINI_DEFAULT_BASE).rstrip("/") or _GEMINI_DEFAULT_BASE


def _gemini_purpose(methods: tuple[str, ...]) -> Literal["chat", "non_chat", "unknown"]:
    normalized = set(methods)
    if normalized & _GEMINI_CHAT_METHODS:
        return "chat"
    if normalized:
        return "non_chat"
    return "unknown"


def _gemini_parse_page(payload: Any) -> _PageResult:
    if not isinstance(payload, dict):
        raise _MalformedPage
    models = payload.get("models", [])
    if not isinstance(models, list):
        raise _MalformedPage
    items = [it for it in models if isinstance(it, dict)]
    if len(items) != len(models):
        raise _MalformedPage
    next_token = payload.get("nextPageToken")
    if next_token is not None and not isinstance(next_token, str):
        raise _MalformedPage
    if isinstance(next_token, str) and next_token and next_token != next_token.strip():
        raise _MalformedPage
    return _PageResult(items=items, next_cursor=next_token or None, more=bool(next_token))


def _gemini_candidate(item: dict) -> DiscoveredCandidate | None:
    name = item.get("name")
    if not isinstance(name, str) or not name or name != name.strip():
        return None
    model_id = name
    if model_id.startswith("models/"):
        model_id = model_id[len("models/") :]
    if not model_id:
        return None
    display_name = item.get("displayName")
    display_name = display_name.strip() if isinstance(display_name, str) and display_name.strip() else None
    raw_methods = item.get("supportedGenerationMethods")
    if raw_methods is not None and (
        not isinstance(raw_methods, list) or not all(isinstance(m, str) for m in raw_methods)
    ):
        return None
    methods = tuple(raw_methods) if raw_methods is not None else ()
    input_limit = item.get("inputTokenLimit")
    output_limit = item.get("outputTokenLimit")
    input_limit = input_limit if type(input_limit) is int and input_limit > 0 else None
    output_limit = output_limit if type(output_limit) is int and output_limit > 0 else None
    return DiscoveredCandidate(
        id=model_id,
        display_name=display_name,
        purpose=_gemini_purpose(methods),
        generation_methods=methods,
        input_token_limit=input_limit,
        output_token_limit=output_limit,
    )


async def _fetch_gemini_models(api_base: str | None, api_key: str | None) -> _FetchOutcome:
    if not api_key:
        return _error_outcome("discovery_credential_missing", "프로바이더 API 키가 설정되지 않았습니다", False)
    base = _gemini_base(api_base)
    headers = {"x-goog-api-key": api_key}

    def build_url(cursor: str | None) -> str:
        query = {"pageSize": str(_GEMINI_PAGE_SIZE)}
        if cursor:
            query["pageToken"] = cursor
        return f"{base}/models?{urlencode(query)}"

    return await _paginate(
        build_url=build_url,
        headers=headers,
        parse_page=_gemini_parse_page,
        to_candidate=_gemini_candidate,
    )


# ---------------------------------------------------------------------------
# OpenAI 호환 /models — 단일 응답(페이지네이션 없음). Perplexity alias 정규화 보존.
# ---------------------------------------------------------------------------
async def _fetch_openai_compatible_models(
    provider_type: str, api_base: str | None, api_key: str | None
) -> _FetchOutcome:
    import httpx

    url = _models_url(provider_type, api_base)
    if url is None:
        return _error_outcome("discovery_unconfigured", "프로바이더 엔드포인트가 설정되지 않았습니다", False)
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        async with asyncio.timeout(_TOTAL_TIMEOUT_SECONDS):
            async with httpx.AsyncClient(
                timeout=_REQUEST_TIMEOUT_SECONDS, follow_redirects=False, trust_env=False
            ) as client:
                payload = await _get(client, url, headers, [_MAX_RESPONSE_BYTES])
            items = payload.get("data") if isinstance(payload, dict) else payload
            if not isinstance(items, list):
                raise _DiscoveryFailure("discovery_malformed_response", "프로바이더 응답 형식이 올바르지 않습니다")
            candidates: list[DiscoveredCandidate] = []
            seen: set[str] = set()
            for index, item in enumerate(items):
                if index % 128 == 0:
                    await asyncio.sleep(0)
                raw_id = item.get("id") if isinstance(item, dict) else item
                if not isinstance(raw_id, str) or not raw_id or raw_id != raw_id.strip():
                    raise _DiscoveryFailure(
                        "discovery_malformed_response", "프로바이더 모델 항목 형식이 올바르지 않습니다"
                    )
                if raw_id.startswith("preset/"):
                    continue
                projected = api_model_name(raw_id, "perplexity") if provider_type == "perplexity" else raw_id
                if len(projected) > _MAX_MODEL_ID_LEN:
                    raise _DiscoveryFailure("discovery_model_limit", "모델 ID가 저장 한도를 초과했습니다")
                if projected in seen:
                    continue
                if len(candidates) >= _MAX_MODELS:
                    raise _DiscoveryFailure("discovery_model_limit", "모델 목록 조회 한도를 초과했습니다")
                seen.add(projected)
                candidates.append(DiscoveredCandidate(id=projected))
            return _FetchOutcome("success" if candidates else "empty", tuple(candidates))
    except TimeoutError:
        return _error_outcome("discovery_timeout", "모델 목록 조회 시간이 초과되었습니다", True)
    except httpx.TimeoutException:
        return _error_outcome("discovery_timeout", "프로바이더 응답이 시간 내에 도착하지 않았습니다", True)
    except httpx.HTTPError:
        return _error_outcome("discovery_upstream_unavailable", "프로바이더에 연결할 수 없습니다", True)
    except _DiscoveryFailure as exc:
        return _error_outcome(exc.code, exc.message, exc.retryable)


# ---------------------------------------------------------------------------
# 정적(litellm) 참고 목록 — 목록 API가 미지원인 경우에만 사용
# ---------------------------------------------------------------------------
def _litellm_static(provider_type: str) -> list[str]:
    try:
        import litellm

        mbp = getattr(litellm, "models_by_provider", None) or {}
        return sorted({str(m) for m in mbp.get(provider_type, [])})
    except Exception:
        logger.warning("litellm 정적 모델 목록 실패 ptype=%s", provider_type, exc_info=True)
        return []


def _static_candidates(provider_type: str) -> tuple[DiscoveredCandidate, ...]:
    static = _litellm_static(provider_type)
    if provider_type == "perplexity":
        static = [api_model_name(model, "perplexity") for model in static if not model.startswith("preset/")]
    ids = sorted({m for m in static if m and len(m) <= _MAX_MODEL_ID_LEN})
    return tuple(DiscoveredCandidate(id=i) for i in ids)


def _wrap_ids(ids: list[str]) -> tuple[DiscoveredCandidate, ...]:
    unique = sorted({i for i in ids if i and len(i) <= _MAX_MODEL_ID_LEN})
    return tuple(DiscoveredCandidate(id=i) for i in unique)


# ---------------------------------------------------------------------------
# 응답 조립
# ---------------------------------------------------------------------------
def _response(
    provider_id: int,
    fetched_at: str,
    source: Literal["api", "litellm", "none"],
    live_status: Literal["success", "empty", "unsupported", "error"],
    complete: bool,
    error: dict[str, Any] | None,
    candidates: tuple[DiscoveredCandidate, ...],
) -> dict[str, Any]:
    return {
        "provider_id": provider_id,
        "fetched_at": fetched_at,
        "source": source,
        "live_status": live_status,
        "complete": complete,
        "error": error,
        "models": [c.id for c in candidates],
        "candidates": [_serialize_candidate(c) for c in candidates],
    }


def _subscription_response(provider_id: int, fetched_at: str, auth_mode: str) -> dict[str, Any]:
    catalog_provider = "chatgpt" if auth_mode == "chatgpt_device" else "anthropic"
    canonical_models: list[str] = []
    for candidate in _litellm_static(catalog_provider):
        try:
            canonical_models.append(canonical_subscription_model_name(candidate, auth_mode))
        except errors.ProviderValidationError:
            continue
    candidates = _wrap_ids(canonical_models)
    source: Literal["api", "litellm", "none"] = "litellm" if candidates else "none"
    return _response(provider_id, fetched_at, source, "unsupported", False, None, candidates)


def _unsupported_response(provider_id: int, fetched_at: str, provider_type: str) -> dict[str, Any]:
    candidates = _static_candidates(provider_type)
    source: Literal["api", "litellm", "none"] = "litellm" if candidates else "none"
    return _response(provider_id, fetched_at, source, "unsupported", False, None, candidates)


def _build_response(provider_id: int, fetched_at: str, outcome: _FetchOutcome) -> dict[str, Any]:
    if outcome.status == "error":
        error = {"code": outcome.error_code, "message": outcome.error_message, "retryable": outcome.retryable}
        return _response(provider_id, fetched_at, "none", "error", False, error, ())
    return _response(provider_id, fetched_at, "api", outcome.status, True, None, outcome.candidates)


async def discover_models(provider_id: int) -> dict[str, Any]:
    """Fetch authoritative live candidates or explicitly unsupported static references."""
    prov = await provider_store.get_provider_for_discovery(provider_id)
    if prov is None:
        raise errors.ProviderNotFoundError(f"프로바이더 {provider_id} 를 찾을 수 없습니다")

    fetched_at = _now_iso()
    auth_mode = prov.get("auth_mode", "api_key")
    if auth_mode in {"chatgpt_device", "anthropic_subscription"}:
        # 구독 토큰은 공식 API-key 목록 endpoint로 절대 보내지 않는다 — 정적 참고 목록만 제공.
        return _subscription_response(provider_id, fetched_at, auth_mode)

    provider_type = prov["provider_type"]
    api_base = prov["api_base"]
    api_key = prov["api_key"]

    if provider_type == "anthropic":
        outcome = await _fetch_anthropic_models(api_base, api_key)
    elif provider_type == "gemini":
        outcome = await _fetch_gemini_models(api_base, api_key)
    elif provider_type in _OPENAI_COMPATIBLE:
        outcome = await _fetch_openai_compatible_models(provider_type, api_base, api_key)
    else:
        return _unsupported_response(provider_id, fetched_at, provider_type)

    return _build_response(provider_id, fetched_at, outcome)
