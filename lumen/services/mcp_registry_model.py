"""Pure model of the GitHub MCP Registry snapshot. No database and no network.

The registry (https://github.com/mcp, API ``https://api.mcp.github.com/v0/servers``) lists local ``packages``
(npm/pypi/oci, launched over stdio) and hosted ``remotes``. Lumen never launches local processes, because a
stdio server runs inside the worker with unbounded size and no recoverable failure boundary. Only hosted
remotes are catalogued, over two transports: ``streamable-http`` (stored as ``http``) and ``sse``.

``scripts/sync_mcp_registry.py`` builds the snapshot ``mcp_registry_catalog.json`` and probes each literal remote
once without credentials. Each entry then gets one ``access`` value that decides how it may be installed:

``open``         the probe was answered without authorization; installed as ``auth_mode=none``.
``oauth``        the probe got 401 and the origin publishes OAuth protected-resource metadata; installed as
                 ``auth_mode=oauth`` so each user authorizes individually (:mod:`lumen.services.mcp_oauth`).
``credentials``  the registry declares a header, or the probe got a bare 401/403. A static header is one secret
                 shared by everyone, so only an administrator may supply it (``auth_mode=admin``).
``configure``    the URL is a template such as ``https://{tenant}.example.com/mcp``. The administrator supplies
                 the variables and the ``auth_mode``; the probe never runs against a template.
``unreachable``  the probe got no usable answer. Listed for visibility, never installed.

Registry text is third-party data. It is never executed. Every URL is re-validated by the connection service and
``extensions_store`` (https only, public address) when a row is created, so the snapshot is not a security boundary.
"""

from __future__ import annotations

import re
from typing import Any

ACCESS_OPEN = "open"
ACCESS_OAUTH = "oauth"
ACCESS_CREDENTIALS = "credentials"
ACCESS_CONFIGURE = "configure"
ACCESS_UNREACHABLE = "unreachable"
ACCESS_STATES = (ACCESS_OPEN, ACCESS_OAUTH, ACCESS_CREDENTIALS, ACCESS_CONFIGURE, ACCESS_UNREACHABLE)
# The only outcomes a credential-free probe can establish for a literal URL.
PROBE_RESULTS = (ACCESS_OPEN, ACCESS_OAUTH, ACCESS_CREDENTIALS, ACCESS_UNREACHABLE)
# Entries an administrator can install in bulk with no value of their own to supply.
BULK_ACCESS = (ACCESS_OPEN, ACCESS_OAUTH)
AUTH_MODES = ("none", "oauth", "admin")
# Registry transport name -> Lumen transport value. Preference order picks streamable HTTP first.
REMOTE_TRANSPORTS = {"streamable-http": "http", "sse": "sse"}

_VARIABLE = re.compile(r"\{([A-Za-z0-9_-]+)\}")
_NAME_MAX = 100  # chat_mcp_servers.name is VARCHAR(100)
_DESCRIPTION_MAX = 300


class McpRegistryError(ValueError):
    """The entry cannot be installed as requested, or the input is incomplete."""


class McpRegistryNotFound(LookupError):
    """No catalogued entry has this id."""


def normalized_url(value: object) -> str:
    """Fold only case and a trailing slash, the same way duplicate installs are detected."""
    if not isinstance(value, str):
        return ""
    return value.strip().rstrip("/").lower()


def url_variables(url: str) -> list[str]:
    return list(dict.fromkeys(_VARIABLE.findall(url)))


def _supported_remote(server: dict[str, Any]) -> tuple[dict[str, Any], str] | None:
    """The preferred supported remote and its Lumen transport, or ``None`` when there is none."""
    candidates: list[tuple[int, dict[str, Any], str]] = []
    for remote in server.get("remotes") or []:
        if not isinstance(remote, dict) or not isinstance(remote.get("url"), str):
            continue
        transport = REMOTE_TRANSPORTS.get(remote.get("transport_type") or "")
        if transport is None:
            continue
        rank = 0 if transport == "http" else 1
        candidates.append((rank, remote, transport))
    if not candidates:
        return None
    _, remote, transport = min(candidates, key=lambda item: item[0])
    return remote, transport


def header_hints(remote: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "description": str(header.get("description") or "")[:_DESCRIPTION_MAX],
            "secret": bool(header.get("is_secret")),
        }
        for header in remote.get("headers") or []
        if isinstance(header, dict)
    ]


def probe_targets(items: list[dict[str, Any]]) -> dict[str, str]:
    """Literal https remotes that the registry data cannot classify, mapped to their transport."""
    targets: dict[str, str] = {}
    for item in items:
        server = item.get("server")
        supported = _supported_remote(server) if isinstance(server, dict) else None
        if supported is None:
            continue
        remote, transport = supported
        url = remote["url"].strip()
        if url_variables(url) or header_hints(remote) or not url.lower().startswith("https://"):
            continue
        targets.setdefault(url, transport)
    return dict(sorted(targets.items()))


def classify(url: str, hints: list[dict[str, Any]], probe: str | None) -> str:
    if url_variables(url):
        return ACCESS_CONFIGURE
    if hints:
        return ACCESS_CREDENTIALS
    if probe not in PROBE_RESULTS:
        raise McpRegistryError(f"probe result missing for {url}")
    return probe  # type: ignore[return-value]


def build_entry(item: dict[str, Any], probe: str | None) -> dict[str, Any] | None:
    """Project one registry list item onto a catalog entry, or ``None`` when it has no usable remote."""
    server = item.get("server")
    if not isinstance(server, dict) or not server.get("name") or not server.get("id"):
        return None
    supported = _supported_remote(server)
    if supported is None:
        return None
    remote, transport = supported
    url = remote["url"].strip()
    if not url_variables(url) and not url.lower().startswith("https://"):
        return None
    hints = header_hints(remote)
    github = item.get("x-github") if isinstance(item.get("x-github"), dict) else {}
    repository = server.get("repository") if isinstance(server.get("repository"), dict) else {}
    registry_name = str(server["name"])
    title = str(github.get("display_name") or registry_name).strip() or registry_name
    return {
        "id": str(server["id"]),
        "registry_name": registry_name,
        "name": title[:_NAME_MAX],
        "description": str(server.get("description") or "")[:_DESCRIPTION_MAX],
        "url": url,
        "transport": transport,
        "variables": url_variables(url),
        "header_hints": hints,
        "docs_url": str(repository.get("url") or f"https://github.com/mcp/{registry_name}"),
        "access": classify(url, hints, probe),
    }


def build_catalog(items: list[dict[str, Any]], probes: dict[str, str]) -> list[dict[str, Any]]:
    """Deterministic catalog: sorted by registry name, one entry per destination URL.

    ``probes`` maps each URL from :func:`probe_targets` to its probe result.
    """
    entries: dict[str, dict[str, Any]] = {}
    for item in sorted(items, key=lambda it: str((it.get("server") or {}).get("name", ""))):
        server = item.get("server") if isinstance(item.get("server"), dict) else {}
        supported = _supported_remote(server)
        probe = probes.get(supported[0]["url"].strip()) if supported is not None else None
        entry = build_entry(item, probe)
        if entry is None:
            continue
        # Two registry names can front one endpoint; keep the first in name order.
        entries.setdefault(normalized_url(entry["url"]), entry)
    return sorted(entries.values(), key=lambda entry: (entry["registry_name"], entry["id"]))
