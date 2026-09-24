"""Semantic-memory availability, backed by the selected memory plugin's index."""

from __future__ import annotations

from lumen_plugin_api.memory import MemoryIndex

from lumen.plugins import memory_host

_ready = False


class SemanticMemoryUnavailable(RuntimeError):
    pass


def configured_memory_index() -> MemoryIndex:
    index = memory_host.configured_index()
    if index is None:
        raise SemanticMemoryUnavailable("semantic memory is disabled or its configuration is incomplete")
    return index


async def setup_semantic_memory() -> None:
    """Mark retrieval available only after the dedicated index schema is ready."""
    global _ready
    index = configured_memory_index()
    await index.setup()
    _ready = True


def semantic_memory_available() -> bool:
    return _ready
