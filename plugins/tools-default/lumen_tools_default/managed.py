"""Managed search, fetch, and advisor tool definitions.

``managed_web_fetch`` executes entirely in this plugin through ``PublicHttpAccess``:
redirect chasing, domain policy, and HTML/PDF text extraction are all plugin-owned
formatting concerns bounded by the host's SSRF-safe primitive.

``managed_web_search`` and ``managed_advisor`` require an authorized, budgeted
provider call (route selection, API credentials, usage billing). Because
``AdvisorAccess.invoke`` returns a terminal, already-formatted ``ToolExecutionResult``,
their citation/usage formatting is necessarily host-side; this module only supplies
the schema, the frozen per-run use-count gate, and the request envelope.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from io import BytesIO
from typing import Any
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from lumen_plugin_api.contracts import ExecutionContext
from lumen_plugin_api.hosts import AdvisorAccess, PublicHttpAccess
from lumen_plugin_api.tools import ToolExecutionResult, ToolTextPart
from pypdf import PdfReader

MANAGED_WEB_SEARCH_KEY = "managed_web_search"
MANAGED_WEB_FETCH_KEY = "managed_web_fetch"
MANAGED_ADVISOR_KEY = "managed_advisor"

_MAX_REDIRECTS = 3
_MAX_RESPONSE_BYTES = 5 * 1024 * 1024
_MAX_MODEL_TEXT_CHARS = 50_000
_MAX_PDF_PAGES = 50
_MAX_RESULT_BYTES = 48 * 1024


def managed_schema(name: str, description: str, property_name: str) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": {property_name: {"type": "string"}},
                "required": [property_name],
                "additionalProperties": False,
            },
        },
    }


def _use_allowed(configuration: dict[str, Any]) -> bool:
    maximum = configuration.get("max_uses")
    current = configuration.get("current_use_count", 0)
    if not isinstance(maximum, int) or maximum < 1 or not isinstance(current, int):
        return False
    return current < maximum


# ---------------------------------------------------------------------------
# managed_web_fetch — real, host-mediated execution.
# ---------------------------------------------------------------------------


class ManagedFetchError(ValueError):
    """The managed fetch could not produce a safe, supported document."""


def _matches_domain(hostname: str, domain: str) -> bool:
    return hostname == domain or hostname.endswith(f".{domain}")


def _hostname_of(url: str) -> str | None:
    parsed = urlsplit(url)
    if not parsed.hostname:
        return None
    try:
        return parsed.hostname.encode("idna").decode("ascii").lower().rstrip(".")
    except UnicodeError:
        return None


def _domain_allowed(url: str, *, allowed_domains: tuple[str, ...], blocked_domains: tuple[str, ...]) -> bool:
    hostname = _hostname_of(url)
    if hostname is None:
        return False
    if any(_matches_domain(hostname, domain) for domain in blocked_domains):
        return False
    if allowed_domains and not any(_matches_domain(hostname, domain) for domain in allowed_domains):
        return False
    return True


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


async def execute_managed_fetch(
    arguments: dict[str, Any], configuration: dict[str, Any], context: ExecutionContext, http: PublicHttpAccess
) -> ToolExecutionResult:
    url = arguments.get("url") if isinstance(arguments, dict) else None
    if not isinstance(url, str) or not (url := url.strip()) or len(url) > 2_048:
        return ToolExecutionResult(status="failed", model_content="가져올 URL이 올바르지 않습니다.", error_code="invalid_fetch_url")
    if urlsplit(url).scheme.lower() != "https":
        return ToolExecutionResult(status="failed", model_content="관리형 가져오기는 HTTPS만 허용합니다.", error_code="fetch_requires_https")
    if not _use_allowed(configuration):
        return ToolExecutionResult(status="failed", model_content="이 실행의 웹 가져오기 사용 한도에 도달했습니다.", error_code="managed_tool_limit_reached")
    allowed_domains = tuple(configuration.get("allowed_domains") or ())
    blocked_domains = tuple(configuration.get("blocked_domains") or ())
    current_url = url
    try:
        for redirect_count in range(_MAX_REDIRECTS + 1):
            if urlsplit(current_url).scheme.lower() != "https":
                raise ManagedFetchError("managed fetch requires HTTPS")
            if not _domain_allowed(current_url, allowed_domains=allowed_domains, blocked_domains=blocked_domains):
                raise ManagedFetchError("managed fetch URL is not allowed")
            status, headers, body = await http.request("GET", current_url)
            if 300 <= status < 400:
                location = headers.get("location") or headers.get("Location")
                if not location:
                    raise ManagedFetchError("redirect response has no location")
                if redirect_count == _MAX_REDIRECTS:
                    raise ManagedFetchError("managed fetch exceeded redirect limit")
                current_url = urljoin(current_url, location)
                continue
            if not 200 <= status < 300:
                raise ManagedFetchError(f"managed fetch returned HTTP {status}")
            if len(body) > _MAX_RESPONSE_BYTES:
                raise ManagedFetchError("managed fetch response exceeds byte limit")
            content_type = (headers.get("content-type") or headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            title, text = _extract_document(content_type, body)
            payload = json.dumps(
                {
                    "url": current_url[:2_048],
                    "title": (title or "")[:512] or None,
                    "content_type": content_type,
                    "text": text[: _MAX_RESULT_BYTES - 4_096],
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            return ToolExecutionResult(
                status="completed",
                model_content=payload[:8_192],
                display=[ToolTextPart(text=payload[:8_192])],
                usage_components={
                    "components": [
                        {"kind": "web_fetch_requests", "price_key": "web_fetch_request_per_unit", "quantity": "1", "unit": "request", "source": "fetch"},
                        {"kind": "web_fetch_context", "price_key": "web_fetch_context_per_unit", "quantity": "1", "unit": "context", "source": "fetch"},
                    ]
                },
            )
    except Exception:
        return ToolExecutionResult(status="failed", model_content="웹 페이지를 안전하게 가져오지 못했습니다.", error_code="managed_fetch_failed")


# ---------------------------------------------------------------------------
# managed_web_search / managed_advisor — schema + gated envelope; the terminal
# ToolExecutionResult is necessarily formatted by the host's AdvisorAccess.
# ---------------------------------------------------------------------------


async def execute_managed_search(
    arguments: dict[str, Any], configuration: dict[str, Any], context: ExecutionContext, advisor: AdvisorAccess
) -> ToolExecutionResult:
    query = arguments.get("query") if isinstance(arguments, dict) else None
    if not isinstance(query, str) or not (query := query.strip()) or len(query) > 10_000:
        return ToolExecutionResult(status="failed", model_content="검색어가 올바르지 않습니다.", error_code="invalid_search_query")
    if not _use_allowed(configuration):
        return ToolExecutionResult(status="failed", model_content="이 실행의 웹 검색 사용 한도에 도달했습니다.", error_code="managed_tool_limit_reached")
    return await advisor.invoke(
        {
            "kind": "search",
            "query": query,
            "route": configuration.get("route"),
            "context_size": configuration.get("context_size"),
            "allowed_domains": configuration.get("allowed_domains"),
            "blocked_domains": configuration.get("blocked_domains"),
            "country": configuration.get("country"),
        },
        context,
    )


async def execute_managed_advisor(
    arguments: dict[str, Any], configuration: dict[str, Any], context: ExecutionContext, advisor: AdvisorAccess
) -> ToolExecutionResult:
    goal = arguments.get("goal") if isinstance(arguments, dict) else None
    if not isinstance(goal, str) or not (goal := goal.strip()) or len(goal) > 10_000:
        return ToolExecutionResult(status="failed", model_content="Advisor goal is invalid.", error_code="invalid_advisor_goal")
    if not _use_allowed(configuration):
        return ToolExecutionResult(status="failed", model_content="Advisor use limit reached.", error_code="managed_tool_limit_reached")
    return await advisor.invoke(
        {
            "kind": "advisor",
            "goal": goal,
            "route": configuration.get("route"),
            "visible_messages": configuration.get("visible_messages") or [],
        },
        context,
    )


@dataclass(frozen=True)
class ManagedToolMeta:
    key: str
    description: str
    property_name: str


MANAGED_TOOLS: tuple[ManagedToolMeta, ...] = (
    ManagedToolMeta(MANAGED_WEB_SEARCH_KEY, "Search the public web through the selected provider.", "query"),
    ManagedToolMeta(MANAGED_WEB_FETCH_KEY, "Fetch a permitted public HTTPS document.", "url"),
    ManagedToolMeta(MANAGED_ADVISOR_KEY, "Ask the selected advisor for private analysis.", "goal"),
)
