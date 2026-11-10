#!/usr/bin/env python3
"""Snapshot the GitHub MCP Registry into ``lumen/services/mcp_registry_catalog.json``.

Run from the repository root with the service environment:

    uv run --package lumen python scripts/sync_mcp_registry.py

The registry is paged through its cursor API until the advertised total is reached. Each literal https remote the
registry data cannot classify gets one credential-free probe, chosen by transport:

- streamable HTTP: an MCP ``initialize`` POST. A 200 with a protocol version is ``open``.
- SSE: a GET that asks for ``text/event-stream``. A 200 event stream is ``open``. The stream is closed as soon as
  the status and content type are read.

A 401 means the server wants authorization. It is ``oauth`` when the origin advertises OAuth protected-resource
metadata, and ``credentials`` otherwise. Nothing is authenticated and redirects are not followed. The output has no
timestamp, so an unchanged registry and endpoint answers reproduce the same file.

Registry data is third-party and untrusted. It is only parsed here, and each URL passes the SSRF policy before any
request is sent to it.
"""

from __future__ import annotations

import concurrent.futures
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import httpx

from lumen.services import ssrf
from lumen.services.mcp_registry_model import (
    ACCESS_CREDENTIALS,
    ACCESS_OAUTH,
    ACCESS_OPEN,
    ACCESS_UNREACHABLE,
    build_catalog,
    probe_targets,
)

REGISTRY_URL = "https://api.mcp.github.com/v0/servers"
OUTPUT = Path(__file__).resolve().parents[1] / "lumen" / "services" / "mcp_registry_catalog.json"
PAGE_SIZE = 100
PROTOCOL_VERSION = "2025-06-18"
PROBE_WORKERS = 16
PROBE_TIMEOUT = 15.0
PROBE_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
    "MCP-Protocol-Version": PROTOCOL_VERSION,
}
SSE_HEADERS = {"Accept": "text/event-stream"}
PROBE_BODY = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {},
        "clientInfo": {"name": "afterglow-lumen-registry-sync", "version": "1"},
    },
}


def fetch_registry() -> tuple[int, list[dict[str, Any]]]:
    items: list[dict[str, Any]] = []
    total: int | None = None
    cursor: str | None = None
    while True:
        query: dict[str, Any] = {"limit": PAGE_SIZE}
        if cursor:
            query["cursor"] = cursor
        url = REGISTRY_URL + "?" + urllib.parse.urlencode(query)
        with urllib.request.urlopen(url, timeout=60) as response:
            page = json.load(response)
        items.extend(page["servers"])
        metadata = page.get("metadata") or {}
        total = int(metadata.get("total", total or 0))
        cursor = metadata.get("next_cursor") or metadata.get("nextCursor")
        if not cursor:
            break
    if len(items) != total:
        raise SystemExit(f"registry returned {len(items)} servers, metadata says {total}")
    return total, items


def _oauth_advertised(client: httpx.Client, url: str) -> bool:
    parts = urllib.parse.urlsplit(url)
    origin = f"{parts.scheme}://{parts.netloc}"
    path = parts.path.rstrip("/")
    for candidate in (
        f"{origin}/.well-known/oauth-protected-resource{path}",
        f"{origin}/.well-known/oauth-protected-resource",
    ):
        try:
            ssrf.validate_url(candidate)
        except ssrf.SsrfBlocked:
            continue
        response = client.get(candidate)
        if response.status_code != 200:
            continue
        try:
            document = response.json()
        except ValueError:
            continue
        if isinstance(document, dict) and document.get("authorization_servers"):
            return True
    return False


def _probe_http(client: httpx.Client, url: str) -> str:
    response = client.post(url, json=PROBE_BODY, headers=PROBE_HEADERS)
    if response.status_code == 200 and '"protocolVersion"' in response.text:
        return ACCESS_OPEN
    return _unauthorized_result(client, url, response.status_code)


def _probe_sse(client: httpx.Client, url: str) -> str:
    with client.stream("GET", url, headers=SSE_HEADERS) as response:
        content_type = response.headers.get("content-type", "")
        if response.status_code == 200 and "text/event-stream" in content_type:
            return ACCESS_OPEN
        status = response.status_code
    return _unauthorized_result(client, url, status)


def _unauthorized_result(client: httpx.Client, url: str, status: int) -> str:
    if status == 401:
        return ACCESS_OAUTH if _oauth_advertised(client, url) else ACCESS_CREDENTIALS
    if status == 403:
        return ACCESS_CREDENTIALS
    return ACCESS_UNREACHABLE


def probe(url: str, transport: str) -> str:
    try:
        ssrf.validate_url(url)
    except ssrf.SsrfBlocked:
        return ACCESS_UNREACHABLE
    try:
        with httpx.Client(timeout=PROBE_TIMEOUT, follow_redirects=False) as client:
            if transport == "sse":
                return _probe_sse(client, url)
            return _probe_http(client, url)
    except httpx.HTTPError:
        return ACCESS_UNREACHABLE


def main() -> int:
    total, items = fetch_registry()
    targets = probe_targets(items)
    with concurrent.futures.ThreadPoolExecutor(max_workers=PROBE_WORKERS) as pool:
        urls = list(targets)
        results = dict(zip(urls, pool.map(lambda u: probe(u, targets[u]), urls), strict=True))
    entries = build_catalog(items, results)
    counts: dict[str, int] = {}
    transports: dict[str, int] = {}
    for entry in entries:
        counts[entry["access"]] = counts.get(entry["access"], 0) + 1
        transports[entry["transport"]] = transports.get(entry["transport"], 0) + 1
    document = {"registry_total": total, "entries": entries}
    OUTPUT.write_text(json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"registry_total={total} probed={len(targets)} entries={len(entries)}")
    for access in sorted(counts):
        print(f"  access {access}={counts[access]}")
    for transport in sorted(transports):
        print(f"  transport {transport}={transports[transport]}")
    print(f"wrote {OUTPUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
