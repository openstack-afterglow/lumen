"""Declarative skills cannot confer executable authority."""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol
from uuid import UUID

from pydantic import Field

from .contracts import ContractModel, Namespace, Plugin, PluginIdentity

if TYPE_CHECKING:
    from .hosts import ExtensionAccess


class SkillRef(ContractModel):
    database_id: int | None = Field(default=None, ge=1)
    binding_id: str | None = None

    def model_post_init(self, context: Any) -> None:
        if (self.database_id is None) == (self.binding_id is None):
            raise ValueError("skill ref needs exactly one database or plugin binding identity")
        if self.binding_id is not None:
            try:
                canonical = str(UUID(self.binding_id))
            except ValueError as exc:
                raise ValueError("skill ref binding_id must be a canonical UUID") from exc
            if canonical != self.binding_id:
                raise ValueError("skill ref binding_id must be a canonical UUID")


SkillRefs = tuple[SkillRef, ...]


class SkillSnapshot(ContractModel):
    reference: SkillRef
    identity: PluginIdentity
    version: str = Field(min_length=1, max_length=190)
    content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    instruction: str = Field(min_length=1, max_length=100_000)
    name: str = ""
    resources: tuple[str, ...] = Field(default=(), max_length=20)


@dataclass(frozen=True, kw_only=True)
class SkillAccess:
    namespace: Namespace
    extensions: ExtensionAccess


class SkillProvider(Plugin, Protocol):
    async def resolve(self, refs: SkillRefs, access: SkillAccess) -> tuple[SkillSnapshot, ...]: ...
    async def revalidate(self, snapshot: SkillSnapshot, access: SkillAccess) -> None: ...
