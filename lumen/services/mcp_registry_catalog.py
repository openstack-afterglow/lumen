"""GitHub MCP Registry entries as connections of a template.

The snapshot ``lumen/services/mcp_registry_catalog.json`` is produced by ``scripts/sync_mcp_registry.py``. An
entry is a template: installing it creates a connection, and installing it again with another label creates a
second connection with its own credentials. Rows are written by :mod:`lumen.services.mcp_connections`, so every
rule there applies: https-only, public addresses, encrypted admin headers, and no config bump on re-install.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from lumen.services import mcp_connections
from lumen.services.mcp_registry_model import (
    ACCESS_CONFIGURE,
    ACCESS_CREDENTIALS,
    ACCESS_MANAGED,
    ACCESS_OAUTH,
    ACCESS_OPEN,
    ACCESS_UNAVAILABLE,
    ACCESS_UNREACHABLE,
    AUTH_MODES,
    BULK_ACCESS,
    McpRegistryError,
    McpRegistryNotFound,
    coverage_counts,
    normalized_url,
    safe_catalog_url,
)

CATALOG_PATH = Path(__file__).with_name("mcp_registry_catalog.json")
_CONCRETE_AUTH = {ACCESS_OPEN: "none", ACCESS_OAUTH: "oauth", ACCESS_CREDENTIALS: "admin"}


@lru_cache(maxsize=1)
def _load() -> tuple[dict[str, Any], ...]:
    data = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise McpRegistryError("MCP registry catalog is malformed")
    return tuple(entries)


def entries() -> list[dict[str, Any]]:
    return [dict(item) for item in _load()]


def coverage() -> dict[str, Any]:
    """Return complete registry coverage without exposing source payloads or credentials."""
    document = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    return {
        "source_url": document.get("source_url", "https://api.mcp.github.com/v0/servers"),
        "auth_observation": document.get("auth_observation"),
        "header_metadata_source_url": document.get("header_metadata_source_url"),
        "header_metadata": document.get("header_metadata", {}),
        **coverage_counts(list(_load()), int(document["registry_total"])),
    }


def entry(entry_id: str) -> dict[str, Any]:
    for candidate in _load():
        if candidate["id"] == entry_id:
            return dict(candidate)
    raise McpRegistryNotFound(f"알 수 없는 MCP 레지스트리 항목입니다: {entry_id}")


def _fill_url(candidate: dict[str, Any], variables: dict[str, str] | None) -> str:
    required = list(candidate["variables"])
    provided = dict(variables or {})
    unexpected = sorted(set(provided) - set(required))
    if unexpected:
        raise McpRegistryError(f"사용하지 않는 URL 변수입니다: {', '.join(unexpected)}")
    missing = [name for name in required if not str(provided.get(name, "")).strip()]
    if missing:
        raise McpRegistryError(f"URL 변수가 필요합니다: {', '.join(missing)}")
    url = candidate["url"]
    for name in required:
        value = str(provided[name]).strip()
        if any(ch.isspace() or ch in "{}/?#@:%&=+\\" or ord(ch) < 32 for ch in value):
            raise McpRegistryError(f"URL 변수 {name} 에 사용할 수 없는 문자가 있습니다")
        url = url.replace("{" + name + "}", value)
    if not safe_catalog_url(url, templates=False):
        raise McpRegistryError("공개 HTTPS URL만 사용할 수 있으며 인증 정보가 포함된 URL은 허용하지 않습니다")
    return url


async def connections_for(entry_id: str) -> list[dict[str, Any]]:
    """Every connection created from one template."""
    rows = await mcp_connections.list_connections()
    return [row for row in rows if row.get("template_key") == entry_id]


async def catalog() -> list[dict[str, Any]]:
    """Every catalogued template with its connections. A template with variables has no connection until one is made."""
    rows = await mcp_connections.list_connections()
    result: list[dict[str, Any]] = []
    for candidate in _load():
        linked = [row for row in rows if row.get("template_key") == candidate["id"]]
        result.append(
            {
                **candidate,
                "connections": [row["id"] for row in linked],
                "installed": bool(linked),
            }
        )
    return result


async def install(
    entry_id: str,
    *,
    variables: dict[str, str] | None = None,
    auth_mode: str | None = None,
    label: str | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Create one connection of a template, or return the matching existing one unchanged.

    ``auth_mode`` is required for ``configure`` entries and defaults to the probe-derived mode for every other entry.
    An entry that declares header hints also accepts ``admin`` with ``headers``, so an administrator can supply an
    optional key. ``label`` distinguishes several connections of one template.
    """
    candidate = entry(entry_id)
    access = candidate["access"]
    if access == ACCESS_MANAGED:
        raise McpRegistryError(f"이 항목은 Lumen 샌드박스가 실행하는 관리형 도구({candidate['managed_profile']})라 연결을 만들지 않습니다")
    if access in (ACCESS_UNREACHABLE, ACCESS_UNAVAILABLE):
        raise McpRegistryError(f"이 서버는 설치할 수 없습니다: {candidate.get('unavailable_reason') or access}")
    if access == ACCESS_CONFIGURE:
        if auth_mode not in AUTH_MODES:
            raise McpRegistryError("URL 템플릿 서버는 auth_mode(none|oauth|admin) 를 지정해야 합니다")
        mode = auth_mode
    else:
        derived = _CONCRETE_AUTH[access]
        if auth_mode is not None and auth_mode != derived and not (auth_mode == "admin" and candidate["header_hints"]):
            raise McpRegistryError(f"이 서버의 auth_mode 는 {derived} 입니다")
        mode = auth_mode or derived
    url = _fill_url(candidate, variables)
    return await mcp_connections.create_connection(
        name=candidate["name"],
        url=url,
        transport=candidate["transport"],
        auth_mode=mode,
        headers=headers,
        template_key=entry_id,
        default_label=candidate["name"],
        label=label,
        implicit_selection=False,
    )


async def install_all() -> dict[str, Any]:
    """Publish every credential-free template; publication never selects it for an owner."""
    return await _install_many(featured_only=False)


async def install_featured() -> dict[str, Any]:
    """Publish curated usable templates without supplying credentials or owner consent."""
    return await _install_many(featured_only=True)


async def _install_many(*, featured_only: bool) -> dict[str, Any]:
    """Reuse the single-install idempotency boundary and report configuration requirements."""
    created: list[str] = []
    existing: list[str] = []
    skipped: list[dict[str, str]] = []
    failed: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    for candidate in _load():
        if featured_only and not candidate.get("featured"):
            continue
        identity = {"id": candidate["id"], "name": candidate["registry_name"]}
        if candidate["access"] not in BULK_ACCESS:
            skipped.append({**identity, "reason": candidate.get("unavailable_reason") or candidate["access"]})
            continue
        # Template installs match existing rows only by (template, label): one endpoint must not become two rows.
        endpoint = normalized_url(candidate["url"])
        if endpoint in seen_urls:
            skipped.append({**identity, "reason": "duplicate_endpoint"})
            continue
        seen_urls.add(endpoint)
        try:
            result = await install(candidate["id"])
        except ValueError as exc:
            failed.append({**identity, "reason": str(exc)})
            continue
        (created if result["created"] else existing).append(candidate["id"])
    return {
        "created": created,
        "existing": existing,
        "skipped": skipped,
        "failed": failed,
        "counts": {
            "created": len(created),
            "existing": len(existing),
            "skipped": len(skipped),
            "failed": len(failed),
        },
    }
