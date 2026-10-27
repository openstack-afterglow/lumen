"""Managed search uses only a selected route and emits safe citations."""

from __future__ import annotations

import pytest

from lumen.services.providers import routing as ps
from lumen.services.web_search import ManagedSearchError, search_selected_provider, search_with_route


async def test_search_requires_selected_active_provider(monkeypatch):
    async def no_provider(provider_id: int, **_kwargs):
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
    async def unexpected(*_args, **_kwargs):
        raise AssertionError("provider must not be called")

    monkeypatch.setattr("lumen.services.providers.search_transport.search", unexpected)
    with pytest.raises(ManagedSearchError, match="context"):
        await search_with_route(
            "query",
            route={"provider_type": "perplexity"},
            context_size="unsupported",
            allowed_domains=(),
            blocked_domains=(),
            country=None,
        )
