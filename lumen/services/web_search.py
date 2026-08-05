"""Managed LiteLLM web-search adapter with canonical citation metadata."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from lumen.services import provider_store as ps

_CONTEXT_TOKEN_LIMITS = {"low": 512, "medium": 2048, "high": 4096}


class ManagedSearchError(ValueError):
    """The selected managed-search route could not return safe results."""


@dataclass(frozen=True)
class SearchCitation:
    url: str
    title: str | None
    snippet: str | None


def _matches_domain(hostname: str, domain: str) -> bool:
    return hostname == domain or hostname.endswith(f".{domain}")


def _safe_result_url(url: object, *, allowed_domains: tuple[str, ...], blocked_domains: tuple[str, ...]) -> str | None:
    if not isinstance(url, str):
        return None
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    try:
        hostname = parsed.hostname.encode("idna").decode("ascii").lower().rstrip(".")
    except UnicodeError:
        return None
    if any(_matches_domain(hostname, domain) for domain in blocked_domains):
        return None
    if allowed_domains and not any(_matches_domain(hostname, domain) for domain in allowed_domains):
        return None
    return url


async def search_with_route(
    query: str,
    *,
    route: dict,
    context_size: str,
    allowed_domains: tuple[str, ...],
    blocked_domains: tuple[str, ...],
    country: str | None,
) -> list[SearchCitation]:
    """Run only the user-selected provider route and normalize its safe citations."""
    if context_size not in _CONTEXT_TOKEN_LIMITS:
        raise ManagedSearchError("unsupported search context size")
    try:
        import litellm

        response = await litellm.asearch(
            query=query,
            search_provider=route["provider_type"],
            search_domain_filter=list(allowed_domains) or None,
            max_tokens_per_page=_CONTEXT_TOKEN_LIMITS[context_size],
            country=country,
            api_key=route.get("api_key"),
            api_base=route.get("api_base"),
            timeout=15,
        )
    except Exception as exc:
        raise ManagedSearchError("managed search provider request failed") from exc

    citations: list[SearchCitation] = []
    for result in getattr(response, "results", ()) or ():
        url = _safe_result_url(
            getattr(result, "url", None),
            allowed_domains=allowed_domains,
            blocked_domains=blocked_domains,
        )
        if url is None:
            continue
        title = getattr(result, "title", None)
        snippet = getattr(result, "snippet", None)
        citations.append(
            SearchCitation(
                url=url,
                title=title if isinstance(title, str) and title else None,
                snippet=snippet if isinstance(snippet, str) and snippet else None,
            )
        )
    return citations


async def search_selected_provider(
    query: str,
    *,
    provider_id: int,
    context_size: str,
    allowed_domains: tuple[str, ...],
    blocked_domains: tuple[str, ...],
    country: str | None,
) -> tuple[dict, list[SearchCitation]]:
    """Resolve exactly one active selected provider; never fall back to the executor."""
    route = await ps.get_active_provider_route(provider_id)
    if route is None:
        raise ManagedSearchError("selected search provider is unavailable")
    return route, await search_with_route(
        query,
        route=route,
        context_size=context_size,
        allowed_domains=allowed_domains,
        blocked_domains=blocked_domains,
        country=country,
    )
