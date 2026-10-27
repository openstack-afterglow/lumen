"""Single-attempt standalone search transports, independent of inference gateways.

Bifrost's native hosted-search tools are not these standalone search APIs. Only
administrator-owned endpoint/key routes enter here; redirects and ambient proxy
or vendor credentials are never used. Wire mappings retain the public search
contracts previously available through the MIT-licensed SDK (see data/NOTICE).
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any
from urllib.parse import urlsplit

import httpx

from .credentials import resolve_inference_credential
from .errors import ProviderValidationError

_ENDPOINTS = {
    "perplexity": ("https://api.perplexity.ai", "/search"),
    "tavily": ("https://api.tavily.com", "/search"),
    "exa_ai": ("https://api.exa.ai", "/search"),
    "brave": ("https://api.search.brave.com/res/v1/web/search", ""),
    "firecrawl": ("https://api.firecrawl.dev/v2", "/search"),
    "fastcrw": ("https://fastcrw.com/api/v1", "/search"),
    "linkup": ("https://api.linkup.so/v1", "/search"),
    "parallel_ai": ("https://api.parallel.ai", "/v1/search"),
    "you_com": ("https://ydc-index.io", "/v1/search"),
    "serper": ("https://google.serper.dev", "/search"),
    "searchapi": ("https://www.searchapi.io/api/v1/search", ""),
    "searxng": ("", "/search"),
    "duckduckgo": ("https://api.duckduckgo.com", ""),
    "google_pse": ("https://www.googleapis.com/customsearch/v1", ""),
    "dataforseo": ("https://api.dataforseo.com/v3/serp/google/organic/live/advanced", ""),
    "tinyfish": ("https://api.search.tinyfish.ai", ""),
    "apiserpent": ("https://apiserpent.com", "/api/search/quick"),
}
_GET = frozenset({"brave", "searchapi", "searxng", "duckduckgo", "google_pse", "tinyfish", "apiserpent"})
_OPTIONAL_KEY = frozenset({"duckduckgo", "searxng", "you_com"})
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024


def _site_query(query: str, domains: tuple[str, ...], *, conjunction: bool = False) -> str:
    if not domains:
        return query
    sites = " OR ".join(f"site:{domain}" for domain in domains)
    return f"({query}) {'AND ' if conjunction else ''}({sites})"


def _wire(provider: str, query: str, *, api_base: str | None, api_key: str | None,
          domains: tuple[str, ...], country: str | None, max_tokens: int) -> tuple[str, str, dict, Any]:
    if provider not in _ENDPOINTS:
        raise ProviderValidationError("unsupported managed search provider")
    default, suffix = _ENDPOINTS[provider]
    base = (api_base or default).rstrip("/")
    parsed = urlsplit(base)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ProviderValidationError("invalid managed search endpoint")
    if provider == "parallel_ai" and suffix and not base.endswith(suffix):
        base = base.removesuffix("/v1")
    url = base if not suffix or base.endswith(suffix) else base + suffix
    if provider not in _OPTIONAL_KEY and not api_key:
        raise ProviderValidationError("managed search credential is unavailable")
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    body: Any = {"query": query}
    if provider == "perplexity":
        body["max_tokens_per_page"] = max_tokens
        if domains:
            body["search_domain_filter"] = list(domains)
        if country:
            body["country"] = country
    elif provider in {"tavily", "exa_ai", "you_com"}:
        if domains:
            body[{"tavily": "include_domains", "exa_ai": "includeDomains", "you_com": "include_domains"}[provider]] = list(domains)
        if country:
            body["userLocation" if provider == "exa_ai" else "country"] = country if provider == "exa_ai" else country.lower()
        if provider == "exa_ai":
            headers.pop("Authorization", None)
            headers["x-api-key"] = api_key
            body["contents"] = {"text": True}
        elif provider == "you_com":
            headers.pop("Authorization", None)
            if api_key:
                headers["X-API-Key"] = api_key
            elif api_base is None:
                url = "https://api.you.com/v1/agents/search"
    elif provider in {"firecrawl", "fastcrw"}:
        if country and provider == "firecrawl":
            body["country"] = country
    elif provider == "linkup":
        body = {"q": query, "depth": "standard", "outputType": "searchResults"}
        if domains:
            body["includeDomains"] = list(domains)
    elif provider == "parallel_ai":
        headers.pop("Authorization", None)
        headers["x-api-key"] = api_key
        body = {"search_queries": [query], "objective": query, "mode": "basic"}
        advanced: dict[str, Any] = {}
        if domains:
            advanced["source_policy"] = {"include_domains": list(domains)}
        if country:
            advanced["location"] = country
        if advanced:
            body["advanced_settings"] = advanced
    elif provider in {"brave", "serper", "searchapi", "apiserpent"}:
        body = {"q": _site_query(query, domains, conjunction=provider in {"brave", "searchapi"})}
        headers.pop("Authorization", None)
        if provider == "brave":
            headers["X-Subscription-Token"] = api_key
            body["include_fetch_metadata"] = "true"
        elif provider == "serper":
            headers["X-API-KEY"] = api_key
            if country:
                body["gl"] = country.lower()
        elif provider == "searchapi":
            body.update({"engine": "google", "api_key": api_key})
            if country:
                body["gl"] = country.lower()
        else:
            headers["X-API-Key"] = api_key
            body.update({"engine": "google", "country": (country or "us").lower(), "num": 10, "format": "full"})
    elif provider in {"duckduckgo", "searxng"}:
        body = {"q": query, "format": "json"}
        if country and provider == "searxng":
            body["language"] = {"us": "en", "uk": "en", "jp": "ja"}.get(country.lower(), country.lower())
    elif provider == "google_pse":
        headers.pop("Authorization", None)
        # This is a non-secret engine identifier, not an ambient provider key.
        engine = os.environ.get("GOOGLE_PSE_ENGINE_ID")
        if not engine:
            raise ProviderValidationError("Google search engine identifier is unavailable")
        body = {"q": query, "key": api_key, "cx": engine}
        if domains:
            body.update({"siteSearch": domains[0], "siteSearchFilter": "i"})
        if country:
            body["gl"] = country.upper()
    elif provider == "dataforseo":
        if not api_key or ":" not in api_key:
            raise ProviderValidationError("DataForSEO requires a stored login:password credential")
        headers["Authorization"] = "Basic " + base64.b64encode(api_key.encode()).decode("ascii")
        task: dict[str, Any] = {"keyword": query, "language_code": "en"}
        if country:
            task["location_name"] = country
        else:
            task["location_code"] = int(os.environ.get("DEFAULT_DATAFORSEO_LOCATION_CODE", "2250"))
        if domains:
            task["domain"] = list(domains)
        body = [task]
    elif provider == "tinyfish":
        headers.pop("Authorization", None)
        headers["X-API-Key"] = api_key
        body = {"query": _site_query(query, domains)}
        if country:
            body["location"] = country
    return ("GET" if provider in _GET else "POST"), url, headers, body


def _results(provider: str, payload: dict) -> list[dict]:
    if provider == "duckduckgo":
        found = []
        if payload.get("AbstractURL") and payload.get("AbstractText"):
            found.append({"url": payload["AbstractURL"], "title": payload.get("Heading"), "snippet": payload["AbstractText"]})
        pending = list(payload.get("RelatedTopics") or [])
        for topic in pending:
            if not isinstance(topic, dict):
                continue
            pending.extend(topic.get("Topics") or [])
            if topic.get("FirstURL"):
                text = topic.get("Text") or ""
                title, separator, snippet = text.partition(" - ")
                found.append({"url": topic["FirstURL"], "title": title[:50], "snippet": snippet if separator else text})
        return found
    if provider == "you_com":
        grouped = payload.get("results") or {}
        raw = [*(grouped.get("web") or []), *(grouped.get("news") or [])]
    elif provider == "brave":
        raw = (payload.get("web") or {}).get("results") or []
    elif provider == "google_pse":
        raw = payload.get("items") or []
    elif provider == "searchapi":
        raw = payload.get("organic_results") or []
    elif provider == "serper":
        raw = payload.get("organic") or []
    elif provider == "firecrawl":
        data = payload.get("data") or []
        raw = [*(data.get("web") or []), *(data.get("news") or [])] if isinstance(data, dict) else data
    elif provider == "fastcrw":
        raw = payload.get("data") or []
    elif provider == "dataforseo":
        raw = []
        for task in payload.get("tasks") or []:
            if task.get("status_code") != 20000:
                raise ProviderValidationError("managed search provider rejected task")
            for result in task.get("result") or []:
                raw.extend(item for item in result.get("items") or [] if item.get("type") == "organic")
    elif provider == "apiserpent":
        data = payload.get("results") or []
        raw = data.get("organic") or [] if isinstance(data, dict) else data
    else:
        raw = payload.get("results") or []
    if not isinstance(raw, list):
        raise ProviderValidationError("invalid managed search response")
    found = []
    for item in raw:
        if not isinstance(item, dict):
            raise ProviderValidationError("invalid managed search result")
        snippets = item.get("snippets") or item.get("excerpts") or []
        snippet = (" ... ".join(snippets) if isinstance(snippets, list) and all(isinstance(part, str) for part in snippets)
                   else None)
        found.append({
            "url": item.get("url") or item.get("link"),
            "title": item.get("title") or item.get("name"),
            "snippet": (snippet or item.get("snippet") or item.get("text") or item.get("markdown")
                        or item.get("content") or item.get("description")),
        })
    return found


async def search(route: dict, query: str, *, allowed_domains: tuple[str, ...],
                 country: str | None, max_tokens: int) -> list[dict]:
    auth = route.get("provider_auth")
    if (auth or {}).get("auth_mode", "api_key") != "api_key":
        raise ProviderValidationError("subscription providers do not support managed search")
    credential = await resolve_inference_credential(auth) if auth is not None else route
    provider = str(route.get("provider_type") or "")
    method, url, headers, body = _wire(provider, query, api_base=route.get("api_base"),
        api_key=credential.get("api_key"), domains=allowed_domains, country=country, max_tokens=max_tokens)
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=False, trust_env=False) as client:
        options = {"params": body} if method == "GET" else {"json": body}
        async with client.stream(method, url, headers=headers, **options) as response:
            from .relay_history import record_dispatch
            record_dispatch(auth, endpoint=url, model=route.get("api_model_name"), operation="search", status_code=response.status_code)
            if not 200 <= response.status_code < 300:
                raise ProviderValidationError("managed search provider request failed")
            content = bytearray()
            async for chunk in response.aiter_bytes():
                if len(content) + len(chunk) > _MAX_RESPONSE_BYTES:
                    raise ProviderValidationError("managed search response exceeds limit")
                content.extend(chunk)
    payload = json.loads(content)
    if not isinstance(payload, dict):
        raise ProviderValidationError("invalid managed search response")
    return _results(provider, payload)
