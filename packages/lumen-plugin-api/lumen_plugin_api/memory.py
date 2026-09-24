"""Memory access plugins return candidates, not an alternative source of truth."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import Field

from .contracts import ContractModel, Namespace, Plugin


class MemoryQuery(ContractModel):
    namespace: Namespace
    query: str = Field(default="", max_length=100_000)
    strategy: Literal["recency", "semantic"] = "recency"
    limit: int = Field(default=30, ge=1, le=30)
    token_budget: int = Field(default=0, ge=0)
    """``token_budget == 0`` means unbounded: rank up to ``limit`` rows with no token cap."""


class MemorySelection(ContractModel):
    candidate_ids: tuple[int, ...] = Field(max_length=30)
    provenance: str = Field(min_length=1, max_length=190)


class MemoryMutation(ContractModel):
    """Shared create/update payload; ``content``/``category`` are required for create, optional for update."""
    content: str | None = Field(default=None, min_length=1, max_length=100_000)
    category: Literal["interest", "development", "habit", "preference", "general"] | None = None
    scope: Literal["account", "project", "workspace"] = "project"
    is_active: bool | None = None
    expected_state: str | None = None


class MemoryHost(Protocol):
    async def list(self, namespace: Namespace) -> list[dict[str, Any]]: ...
    async def get(self, memory_id: int, namespace: Namespace) -> dict[str, Any]: ...
    async def hydrate(self, ids: tuple[int, ...], namespace: Namespace) -> list[dict[str, Any]]: ...
    async def create(self, mutation: MemoryMutation, namespace: Namespace) -> dict[str, Any]: ...
    async def update(self, memory_id: int, mutation: MemoryMutation, namespace: Namespace) -> dict[str, Any]: ...
    async def delete(self, memory_id: int, namespace: Namespace) -> None: ...
    async def embed_query(self, query: MemoryQuery) -> list[float]: ...


@dataclass(frozen=True, kw_only=True)
class MemoryAccess:
    namespace: Namespace
    host: MemoryHost
    index: MemoryIndex | None = None


class MemoryProvider(Plugin, Protocol):
    async def recall(self, query: MemoryQuery, access: MemoryAccess) -> MemorySelection: ...
    async def list(self, access: MemoryAccess) -> list[dict[str, Any]]: ...
    async def get(self, memory_id: int, access: MemoryAccess) -> dict[str, Any]: ...
    async def create(self, mutation: MemoryMutation, access: MemoryAccess) -> dict[str, Any]: ...
    async def update(self, memory_id: int, mutation: MemoryMutation, access: MemoryAccess) -> dict[str, Any]: ...
    async def delete(self, memory_id: int, access: MemoryAccess) -> None: ...
    def build_index(self, dsn: str, *, dimensions: int) -> MemoryIndex: ...


@dataclass(frozen=True)
class MemoryVector:
    memory_id: int
    generation: int
    user_id: str
    project_id: str | None
    workspace_id: int | None
    embedding: list[float]
    embedding_model: str
    content_hash: str


class MemoryIndex(Protocol):
    async def setup(self) -> None: ...
    async def search_ids(self, *, user_id: str, project_id: str | None, workspace_id: int | None, embedding: list[float], limit: int) -> list[int]: ...
    async def required_generations(self) -> list[int]: ...
    async def upsert(self, vector: MemoryVector) -> None: ...
    async def delete(self, *, generation: int, memory_id: int) -> None: ...
    async def delete_namespace(self, *, generation: int, user_id: str, project_id: str | None, workspace_id: int | None) -> None: ...
