"""Shared execution-protocol rollout contract for API, workers, and discovery."""

from __future__ import annotations

SUPPORTED_EXECUTION_PROTOCOL_VERSIONS = frozenset({1, 2})


def is_supported(version: object) -> bool:
    return isinstance(version, int) and version in SUPPORTED_EXECUTION_PROTOCOL_VERSIONS


def v2_runtime_ready(configured_version: object) -> bool:
    """True only after both configuration and worker support explicitly include v2."""
    return configured_version == 2 and 2 in SUPPORTED_EXECUTION_PROTOCOL_VERSIONS
