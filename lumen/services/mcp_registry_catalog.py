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
    ACCESS_OAUTH,
    ACCESS_OPEN,
    ACCESS_UNREACHABLE,
    AUTH_MODES,
    BULK_ACCESS,
    McpRegistryError,
    McpRegistryNotFound,
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
        if any(ch.isspace() or ch in "{}/?#@" or ord(ch) < 32 for ch in value):
            raise McpRegistryError(f"URL 변수 {name} 에 사용할 수 없는 문자가 있습니다")
        url = url.replace("{" + name + "}", value)
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

    ``auth_mode`` is required for ``configure`` entries and must match the derived mode for every other entry.
    ``headers`` is accepted only with ``admin`` mode. ``label`` distinguishes several connections of one template.
    """
    candidate = entry(entry_id)
    access = candidate["access"]
    if access == ACCESS_UNREACHABLE:
        raise McpRegistryError("이 서버는 인증 없이 MCP 응답을 확인하지 못해 설치할 수 없습니다")
    if access == ACCESS_CONFIGURE:
        if auth_mode not in AUTH_MODES:
            raise McpRegistryError("URL 템플릿 서버는 auth_mode(none|oauth|admin) 를 지정해야 합니다")
        mode = auth_mode
    else:
        derived = _CONCRETE_AUTH[access]
        if auth_mode is not None and auth_mode != derived:
            raise McpRegistryError(f"이 서버의 auth_mode 는 {derived} 입니다")
        mode = derived
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
    )


async def install_all() -> dict[str, Any]:
    """Create one default connection for every entry that needs no administrator-supplied value.

    Entries needing a URL variable or a credential, and unreachable entries, are reported as skipped with their
    access state. A repeat run reports existing connections and creates nothing.
    """
    created: list[str] = []
    existing: list[str] = []
    skipped: list[dict[str, str]] = []
    failed: list[dict[str, str]] = []
    for candidate in _load():
        identity = {"id": candidate["id"], "name": candidate["registry_name"]}
        if candidate["access"] not in BULK_ACCESS:
            skipped.append({**identity, "reason": candidate["access"]})
            continue
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
