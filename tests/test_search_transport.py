"""Real loopback HTTP evidence for standalone search, not gateway hosted tools."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from lumen.services import web_search
from lumen.services.providers import search_transport


@pytest.fixture
def search_upstream():
    state = {"requests": [], "status": 200, "payload": {}, "redirect": None}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            self.respond()

        def do_POST(self):
            self.respond()

        def respond(self):
            size = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(size)
            state["requests"].append({"method": self.command, "target": self.path,
                "headers": dict(self.headers), "body": json.loads(raw) if raw else None})
            encoded = json.dumps(state["payload"]).encode()
            self.send_response(state["status"])
            if state["redirect"]:
                self.send_header("Location", state["redirect"])
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state["url"] = f"http://127.0.0.1:{server.server_port}"
    yield state
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


@pytest.mark.parametrize("provider,payload", [
    ("perplexity", {"results": [{"url": "https://allowed.example/a", "title": "source", "snippet": "evidence"}]}),
    ("tavily", {"results": [{"url": "https://allowed.example/a", "title": "source", "content": "evidence"}]}),
    ("exa_ai", {"results": [{"url": "https://allowed.example/a", "title": "source", "text": "evidence"}]}),
    ("brave", {"web": {"results": [{"url": "https://allowed.example/a", "title": "source", "description": "evidence"}]}}),
    ("firecrawl", {"data": {"web": [{"url": "https://allowed.example/a", "title": "source", "markdown": "evidence"}]}}),
    ("fastcrw", {"data": [{"url": "https://allowed.example/a", "title": "source", "markdown": "evidence"}]}),
    ("linkup", {"results": [{"url": "https://allowed.example/a", "name": "source", "content": "evidence"}]}),
    ("parallel_ai", {"results": [{"url": "https://allowed.example/a", "title": "source", "excerpts": ["evidence"]}]}),
    ("you_com", {"results": {"web": [{"url": "https://allowed.example/a", "title": "source", "snippets": ["evidence"]}]}}),
    ("serper", {"organic": [{"link": "https://allowed.example/a", "title": "source", "snippet": "evidence"}]}),
    ("searchapi", {"organic_results": [{"link": "https://allowed.example/a", "title": "source", "snippet": "evidence"}]}),
    ("searxng", {"results": [{"url": "https://allowed.example/a", "title": "source", "content": "evidence"}]}),
    ("duckduckgo", {"Heading": "source", "AbstractURL": "https://allowed.example/a", "AbstractText": "evidence"}),
    ("google_pse", {"items": [{"link": "https://allowed.example/a", "title": "source", "snippet": "evidence"}]}),
    ("dataforseo", {"tasks": [{"status_code": 20000, "result": [{"items": [
        {"type": "organic", "url": "https://allowed.example/a", "title": "source", "description": "evidence"}]}]}]}),
    ("tinyfish", {"results": [{"url": "https://allowed.example/a", "title": "source", "snippet": "evidence"}]}),
    ("apiserpent", {"results": {"organic": [{"url": "https://allowed.example/a", "title": "source", "snippet": "evidence"}]}}),
])
async def test_standalone_native_search_responses_survive_normalization(search_upstream, monkeypatch, provider, payload):
    state = search_upstream
    state["payload"] = payload
    monkeypatch.setenv("GOOGLE_PSE_ENGINE_ID", "fixture-engine")
    citations = await web_search.search_with_route("actual search", route={
        "provider_type": provider, "api_base": state["url"], "api_key": "fixture:credential"},
        context_size="medium", allowed_domains=("allowed.example",), blocked_domains=(), country="KR")
    assert [(item.url, item.title, item.snippet) for item in citations] == [
        ("https://allowed.example/a", "source", "evidence")]
    assert len(state["requests"]) == 1
    request = state["requests"][0]
    if provider == "brave":
        assert request["method"] == "GET"
        assert "site:allowed.example" in parse_qs(urlsplit(request["target"]).query)["q"][0]
        assert request["headers"].get("X-Subscription-Token") == "fixture:credential"
        assert "Authorization" not in request["headers"]
    elif provider == "perplexity":
        assert request["body"] == {"query": "actual search", "search_domain_filter": ["allowed.example"],
                                   "country": "KR", "max_tokens_per_page": 2048}
    elif provider == "dataforseo":
        assert request["body"] == [{"keyword": "actual search", "language_code": "en", "location_name": "KR",
                                    "domain": ["allowed.example"]}]
        assert request["headers"]["Authorization"].startswith("Basic ")
    elif provider == "google_pse":
        query = parse_qs(urlsplit(request["target"]).query)
        assert query["cx"] == ["fixture-engine"]
        assert query["key"] == ["fixture:credential"]
        assert "Authorization" not in request["headers"]


@pytest.mark.parametrize("status", [301, 307, 400, 401, 403, 429, 500, 503])
async def test_rejection_or_redirect_never_replays_search(search_upstream, status):
    state = search_upstream
    state["status"] = status
    state["redirect"] = state["url"] + "/credential-trap"
    state["payload"] = {"error": "fixture-key must not escape"}
    with pytest.raises(web_search.ManagedSearchError, match="provider request failed") as error:
        await web_search.search_with_route("bounded", route={"provider_type": "perplexity",
            "api_base": state["url"], "api_key": "fixture-key"}, context_size="low",
            allowed_domains=(), blocked_domains=(), country=None)
    assert len(state["requests"]) == 1
    assert "fixture-key" not in str(error.value)


async def test_provider_domain_filters_cannot_replace_local_citation_boundary(search_upstream):
    search_upstream["payload"] = {"results": [
        {"url": "https://allowed.example/ok", "snippet": "kept"},
        {"url": "https://blocked.allowed.example/no", "snippet": "blocked"},
        {"url": "https://unrelated.example/no", "snippet": "foreign"},
        {"url": "https://user:password@allowed.example/no", "snippet": "secret-bearing"},
        {"url": "javascript:alert(1)", "snippet": "unsafe"},
    ]}
    citations = await web_search.search_with_route("filter", route={"provider_type": "perplexity",
        "api_base": search_upstream["url"], "api_key": "fixture-key"}, context_size="high",
        allowed_domains=("allowed.example",), blocked_domains=("blocked.allowed.example",), country=None)
    assert [item.url for item in citations] == ["https://allowed.example/ok"]


async def test_stored_subscription_cannot_fall_back_to_api_key_search(search_upstream):
    with pytest.raises(web_search.ManagedSearchError):
        await web_search.search_with_route("no cross-auth", route={"provider_type": "perplexity",
            "api_base": search_upstream["url"], "api_key": "must-not-use",
            "provider_auth": {"auth_mode": "chatgpt_device"}}, context_size="low",
            allowed_domains=(), blocked_domains=(), country=None)
    assert search_upstream["requests"] == []


def test_unknown_provider_fails_without_default_endpoint():
    with pytest.raises(ValueError, match="unsupported managed search provider"):
        search_transport._wire("unconfigured", "query", api_base=None, api_key="secret",
                               domains=(), country=None, max_tokens=512)
