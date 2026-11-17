#!/usr/bin/env python3
"""Snapshot the GitHub MCP Registry into ``lumen/services/mcp_registry_catalog.json``.

Run from the repository root with the service environment:

    uv run --package lumen python scripts/sync_mcp_registry.py

Default refresh is incremental: it GETs the registry, retains earlier credential-free observations for unchanged
destinations, and probes only new or previously unreachable literal remotes. ``--reprobe-all`` probes every literal
remote; ``--registry-only`` sends no remote request and marks unobserved destinations unreachable.

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

import argparse
import concurrent.futures
import http.client
import json
import sys
import time
import urllib.error
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
    coverage_counts,
    probe_targets,
)

REGISTRY_URL = "https://api.mcp.github.com/v0/servers"
HEADER_REGISTRY_URL = "https://registry.modelcontextprotocol.io/v0.1/servers"
OUTPUT = Path(__file__).resolve().parents[1] / "lumen" / "services" / "mcp_registry_catalog.json"
PAGE_SIZE = 50
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


def _get_json(url: str, *, timeout: float, attempts: int = 4) -> Any:
    """GET one fixed registry document, retrying only transient network failures."""
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                return json.load(response)
        except (TimeoutError, urllib.error.URLError, ConnectionError, http.client.HTTPException):
            if attempt + 1 == attempts:
                raise
            time.sleep(2**attempt)


def fetch_registry() -> tuple[int, list[dict[str, Any]]]:
    items: list[dict[str, Any]] = []
    total: int | None = None
    cursor: str | None = None
    seen_cursors: set[str] = set()
    while True:
        query: dict[str, Any] = {"limit": PAGE_SIZE}
        if cursor:
            query["cursor"] = cursor
        url = REGISTRY_URL + "?" + urllib.parse.urlencode(query)
        page = _get_json(url, timeout=90)
        items.extend(page["servers"])
        metadata = page.get("metadata") or {}
        total = int(metadata.get("total", total or 0))
        cursor = metadata.get("next_cursor") or metadata.get("nextCursor")
        if not cursor:
            break
        if cursor in seen_cursors:
            raise SystemExit("registry repeated a pagination cursor")
        seen_cursors.add(cursor)
    if len(items) != total:
        raise SystemExit(f"registry returned {len(items)} servers, metadata says {total}")
    return total, items


def enrich_header_metadata(items: list[dict[str, Any]]) -> dict[str, int]:
    """Recover names/requirement flags omitted by GitHub, using exact upstream server and remote identities.

    Only fixed official-registry GET URLs are requested. A missing or changed upstream declaration leaves
    unknown metadata intact; no header value, default, example or credential is copied.
    """
    selected = [
        item["server"] for item in items
        if any(remote.get("headers") for remote in item["server"].get("remotes", []))
    ]

    def fetch(server: dict[str, Any]) -> dict[str, Any] | None:
        name = urllib.parse.quote(server["name"], safe="")
        url = f"{HEADER_REGISTRY_URL}/{name}/versions/latest"
        try:
            document = _get_json(url, timeout=20, attempts=2)
            upstream = document.get("server") or {}
            if upstream.get("name") != server["name"]:
                return None
            if upstream.get("repository", {}).get("url") != server.get("repository", {}).get("url"):
                return None
            return upstream
        except (OSError, ValueError):
            return None

    recovered = 0
    unavailable = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=PROBE_WORKERS) as pool:
        for server, upstream in zip(selected, pool.map(fetch, selected), strict=True):
            if upstream is None:
                unavailable += 1
                continue
            for remote in server.get("remotes", []):
                matches = [
                    candidate for candidate in upstream.get("remotes", [])
                    if candidate.get("url") == remote.get("url")
                    and candidate.get("type") == remote.get("transport_type")
                ]
                if len(matches) != 1:
                    continue
                for header in remote.get("headers", []):
                    headers = [
                        candidate for candidate in matches[0].get("headers", [])
                        if candidate.get("description", "") == header.get("description", "")
                    ]
                    if len(headers) != 1:
                        continue
                    declared = headers[0]
                    header["name"] = declared.get("name")
                    header["is_required"] = declared.get("isRequired", False)
                    header["is_secret"] = declared.get("isSecret", False)
                    recovered += 1
    return {"servers_requested": len(selected), "servers_unavailable": unavailable, "headers_recovered": recovered}


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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry-only", action="store_true", help="GET registry metadata only; never probe remotes")
    parser.add_argument("--reprobe-all", action="store_true", help="Probe every literal remote instead of only new/unverified ones")
    parser.add_argument("--output", type=Path, default=OUTPUT, help="Destination of the generated snapshot")
    args = parser.parse_args()
    total, items = fetch_registry()
    header_metadata = enrich_header_metadata(items)
    targets = probe_targets(items)
    previous = json.loads(OUTPUT.read_text(encoding="utf-8")) if OUTPUT.exists() else {}
    observed: dict[tuple[str, str], str] = {}
    for key, value in (previous.get("probe_observations") or {}).items():
        url, _, transport = key.rpartition("|")
        if value in (ACCESS_OPEN, ACCESS_OAUTH, ACCESS_CREDENTIALS):
            observed[(url, transport)] = value
    for candidate in previous.get("entries", []):
        if (
            candidate.get("access") in (ACCESS_OPEN, ACCESS_OAUTH, ACCESS_CREDENTIALS)
            and not candidate.get("header_hints")
            and not candidate.get("variables")
            and candidate.get("url")
        ):
            observed.setdefault((candidate["url"], candidate["transport"]), candidate["access"])
    results: dict[str, str] = {}
    pending: list[str] = []
    for url, transport in targets.items():
        retained = None if args.reprobe_all else observed.get((url, transport))
        if retained is not None:
            results[url] = retained
        elif args.registry_only:
            results[url] = ACCESS_UNREACHABLE
        else:
            pending.append(url)
    if pending:
        with concurrent.futures.ThreadPoolExecutor(max_workers=PROBE_WORKERS) as pool:
            results.update(zip(pending, pool.map(lambda u: probe(u, targets[u]), pending), strict=True))
    entries = build_catalog(items, results)
    counts: dict[str, int] = {}
    transports: dict[str | None, int] = {}
    for entry in entries:
        counts[entry["access"]] = counts.get(entry["access"], 0) + 1
        transports[entry["transport"]] = transports.get(entry["transport"], 0) + 1
    document = {
        "source_url": REGISTRY_URL,
        "registry_total": total,
        "coverage": coverage_counts(entries, total),
        "auth_observation": "registry_only_retained_previous" if args.registry_only else "incremental_credential_free_initialize_probe",
        "probe_observations": {f"{url}|{targets[url]}": result for url, result in sorted(results.items())},
        "header_metadata_source_url": HEADER_REGISTRY_URL,
        "header_metadata": header_metadata,
        "entries": entries,
    }
    args.output.write_text(json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"registry_total={total} probed={len(pending)} retained={len(targets) - len(pending)} entries={len(entries)}")
    for access in sorted(counts):
        print(f"  access {access}={counts[access]}")
    for transport in sorted(transports, key=lambda value: value or ""):
        print(f"  transport {transport}={transports[transport]}")
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
