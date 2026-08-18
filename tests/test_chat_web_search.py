"""Managed search uses only a selected route and emits safe citations."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from lumen.services.providers import routing as ps
from lumen.services.web_search import ManagedSearchError, search_selected_provider, search_with_route


async def test_search_normalizes_results_and_drops_disallowed_urls(monkeypatch):
    captured: dict = {}

    async def fake_asearch(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            results=[
                SimpleNamespace(url="https://docs.example/guide", title="Guide", snippet="Useful"),
                SimpleNamespace(url="https://ads.example/track", title="Blocked", snippet="No"),
                SimpleNamespace(url="javascript:alert(1)", title="Unsafe", snippet="No"),
                SimpleNamespace(url="https://token@docs.example/private", title="Credential", snippet="No"),
            ]
        )

    import litellm

    monkeypatch.setattr(litellm, "asearch", fake_asearch)
    citations = await search_with_route(
        "find docs",
        route={"provider_type": "perplexity", "api_key": "secret", "api_base": "https://search.example"},
        context_size="high",
        allowed_domains=("docs.example",),
        blocked_domains=("ads.example",),
        country="KR",
    )

    assert citations == [type(citations[0])(url="https://docs.example/guide", title="Guide", snippet="Useful")]
    assert captured == {
        "query": "find docs",
        "search_provider": "perplexity",
        "search_domain_filter": ["docs.example"],
        "max_tokens_per_page": 4096,
        "country": "KR",
        "api_key": "secret",
        "api_base": "https://search.example",
        "timeout": 15,
    }


async def test_search_requires_selected_active_provider(monkeypatch):
    async def no_provider(provider_id: int):
        assert provider_id == 7
        return None

    monkeypatch.setattr(ps, "get_active_provider_route", no_provider)
    with pytest.raises(ManagedSearchError, match="selected"):
        await search_selected_provider(
            "query",
            provider_id=7,
            context_size="medium",
            allowed_domains=(),
            blocked_domains=(),
            country=None,
        )


async def test_search_rejects_unknown_context_before_provider_call(monkeypatch):
    async def unexpected(**kwargs):
        raise AssertionError("provider must not be called")

    import litellm

    monkeypatch.setattr(litellm, "asearch", unexpected)
    with pytest.raises(ManagedSearchError, match="context"):
        await search_with_route(
            "query",
            route={"provider_type": "perplexity"},
            context_size="unsupported",
            allowed_domains=(),
            blocked_domains=(),
            country=None,
        )
