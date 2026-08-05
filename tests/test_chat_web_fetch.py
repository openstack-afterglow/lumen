"""Managed fetch extraction and boundary tests."""

from __future__ import annotations

import httpx
import pytest

from lumen.services.web_fetch import ManagedFetchError, fetch_document


async def test_fetch_follows_bounded_https_redirect_and_extracts_html():
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        assert request.headers["accept-encoding"] == "identity"
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "/article"}, request=request)
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            content=b"<html><head><title>Read me</title><script>alert(1)</script></head><body><nav>menu</nav><p>Hello</p></body></html>",
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
        document = await fetch_document("https://docs.example/start", client=client)

    assert requests == ["https://docs.example/start", "https://docs.example/article"]
    assert document.title == "Read me"
    assert document.text == "Read me Hello"
    assert document.content_type == "text/html"


async def test_fetch_rejects_non_https_before_request():
    with pytest.raises(ManagedFetchError, match="HTTPS"):
        await fetch_document("http://public.example/article")


async def test_fetch_rejects_unsupported_content_type():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "application/octet-stream"}, content=b"x", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ManagedFetchError, match="unsupported"):
            await fetch_document("https://docs.example/file", client=client)


async def test_fetch_enforces_byte_limit_even_with_injected_client(monkeypatch):
    from lumen.services import web_fetch

    monkeypatch.setattr(web_fetch, "_MAX_RESPONSE_BYTES", 4)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/plain"}, content=b"12345", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ManagedFetchError, match="byte limit"):
            await fetch_document("https://docs.example/large", client=client)
