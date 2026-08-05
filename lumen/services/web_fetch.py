"""Managed web-fetch boundary for chat capabilities.

Fetched pages are untrusted tool input. This module only returns bounded extracted
text and source metadata; callers must keep it out of privileged instructions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from io import BytesIO
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup
from pypdf import PdfReader

from lumen.services.ssrf import SafeAsyncTransport, SsrfBlocked

_MAX_REDIRECTS = 3
_MAX_RESPONSE_BYTES = 5 * 1024 * 1024
_MAX_MODEL_TEXT_CHARS = 50_000
_MAX_PDF_PAGES = 50


class ManagedFetchError(ValueError):
    """The managed fetch could not produce a safe, supported document."""


@dataclass(frozen=True)
class FetchedDocument:
    url: str
    title: str | None
    text: str
    content_type: str


def _normalized_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()[:_MAX_MODEL_TEXT_CHARS]


def _extract_html(payload: bytes) -> tuple[str | None, str]:
    soup = BeautifulSoup(payload.decode("utf-8", errors="replace"), "html.parser")
    for element in soup(["script", "style", "nav"]):
        element.decompose()
    title = soup.title.get_text(" ", strip=True) if soup.title else None
    return title or None, _normalized_text(soup.get_text(" ", strip=True))


def _extract_pdf(payload: bytes) -> tuple[str | None, str]:
    try:
        reader = PdfReader(BytesIO(payload))
        text = "\n".join((page.extract_text() or "") for page in reader.pages[:_MAX_PDF_PAGES])
        metadata = reader.metadata
    except Exception as exc:
        raise ManagedFetchError("PDF content could not be extracted") from exc
    title = getattr(metadata, "title", None) if metadata is not None else None
    return title if isinstance(title, str) and title.strip() else None, _normalized_text(text)


def _extract_document(content_type: str, payload: bytes) -> tuple[str | None, str]:
    if content_type == "application/pdf":
        return _extract_pdf(payload)
    if content_type == "text/html":
        return _extract_html(payload)
    if content_type.startswith("text/"):
        return None, _normalized_text(payload.decode("utf-8", errors="replace"))
    raise ManagedFetchError("unsupported fetched content type")


def _require_https(url: str) -> None:
    if urlsplit(url).scheme.lower() != "https":
        raise ManagedFetchError("managed fetch requires HTTPS")


async def fetch_document(
    url: str,
    *,
    allowed_domains: tuple[str, ...] = (),
    blocked_domains: tuple[str, ...] = (),
    client: httpx.AsyncClient | None = None,
) -> FetchedDocument:
    """Fetch a public HTTPS document with per-hop policy, redirect, and byte bounds."""
    _require_https(url)
    owns_client = client is None
    active_client = client or httpx.AsyncClient(
        transport=SafeAsyncTransport(
            allowed_domains=allowed_domains,
            blocked_domains=blocked_domains,
            max_response_bytes=_MAX_RESPONSE_BYTES,
        ),
        follow_redirects=False,
        trust_env=False,
        timeout=httpx.Timeout(15.0),
    )
    current_url = url
    try:
        for redirect_count in range(_MAX_REDIRECTS + 1):
            _require_https(current_url)
            request = active_client.build_request("GET", current_url)
            request.headers["accept-encoding"] = "identity"
            try:
                response = await active_client.send(request, stream=True)
            except SsrfBlocked as exc:
                raise ManagedFetchError("managed fetch URL is not allowed") from exc
            try:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise ManagedFetchError("redirect response has no location")
                    if redirect_count == _MAX_REDIRECTS:
                        raise ManagedFetchError("managed fetch exceeded redirect limit")
                    current_url = urljoin(str(response.url), location)
                    continue
                if not 200 <= response.status_code < 300:
                    raise ManagedFetchError(f"managed fetch returned HTTP {response.status_code}")
                content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if (
                    content_type != "application/pdf"
                    and content_type != "text/html"
                    and not content_type.startswith("text/")
                ):
                    raise ManagedFetchError("unsupported fetched content type")
                payload = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(payload) + len(chunk) > _MAX_RESPONSE_BYTES:
                        raise ManagedFetchError("managed fetch response exceeds byte limit")
                    payload.extend(chunk)
                title, text = _extract_document(content_type, bytes(payload))
                return FetchedDocument(url=str(response.url), title=title, text=text, content_type=content_type)
            finally:
                await response.aclose()
    finally:
        if owns_client:
            await active_client.aclose()
    raise ManagedFetchError("managed fetch exceeded redirect limit")
