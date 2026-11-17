"""Secret-free, explicit run-scoped Kubernetes tool authority.

Admission freezes exact names and immutable execution identities. Every worker and
controller dispatch revalidates that authority; configured pools never imply selection.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

from lumen.config import get_settings
from lumen.services.infrastructure.tool_manifest import ToolManifestError, load_tool_manifest

if TYPE_CHECKING:
    from lumen.services.infrastructure.config import RuntimeConfig

_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,127}")
_FIELDS = {"name", "pool", "profile", "image", "pool_digest", "manifest_sha256"}


class SandboxSelectionError(ValueError):
    """Bounded, non-secret admission or dispatch refusal."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def freeze_selection(names: list[str], *, config: RuntimeConfig | None = None) -> list[dict[str, str]]:
    if (not isinstance(names, list) or len(names) > 100
            or any(not isinstance(name, str) or not _NAME.fullmatch(name) for name in names)
            or len(set(names)) != len(names)):
        raise SandboxSelectionError("sandbox_selection_invalid")
    if not names:
        return []
    config = config if config is not None else get_settings().runtime_config
    if not config.enabled:
        raise SandboxSelectionError("sandbox_tool_unavailable")
    matches: dict[str, dict[str, str]] = {}
    wanted = set(names)
    try:
        for pool in config.pools:
            if not (pool.enabled and pool.role == "sandbox" and pool.backend == "kubernetes"):
                continue
            for profile in load_tool_manifest(pool.profile.tool_manifest_file, pool.profile.tool_manifest_sha256):
                for definition in profile.tools:
                    if definition.name not in wanted:
                        continue
                    if definition.name in matches:
                        raise SandboxSelectionError("sandbox_selection_ambiguous")
                    matches[definition.name] = {
                        "name": definition.name, "pool": pool.name, "profile": profile.id,
                        "image": pool.image, "pool_digest": pool.digest(),
                        "manifest_sha256": pool.profile.tool_manifest_sha256,
                    }
    except ToolManifestError as exc:
        raise SandboxSelectionError("sandbox_tool_unavailable") from exc
    if wanted != matches.keys():
        raise SandboxSelectionError("sandbox_tool_unavailable")
    if len({item["pool"] for item in matches.values()}) != 1:
        raise SandboxSelectionError("sandbox_selection_multiple_pools")
    return [matches[name] for name in names]


def validate_selection(snapshot: object, *, config: RuntimeConfig | None = None) -> list[dict[str, str]]:
    if (not isinstance(snapshot, list)
            or any(not isinstance(item, dict) or item.keys() != _FIELDS
                   or any(not isinstance(value, str) or not value for value in item.values())
                   for item in snapshot)):
        raise SandboxSelectionError("sandbox_selection_invalid")
    current = freeze_selection([item["name"] for item in snapshot], config=config)
    if snapshot != current:
        raise SandboxSelectionError("sandbox_configuration_changed")
    return current


def selected_pool(snapshot: object, *, config: RuntimeConfig | None = None) -> str | None:
    selected = validate_selection(snapshot, config=config)
    return selected[0]["pool"] if selected else None
