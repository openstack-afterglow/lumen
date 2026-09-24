"""Versioned metadata and authenticated values shared by all plugin kinds."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

PluginKind = Literal["database", "memory", "tools", "skills", "mcp"]
PluginErrorCode = Literal[
    "plugin_unavailable", "plugin_incompatible", "plugin_configuration_changed", "plugin_authority_revoked"
]


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, protected_namespaces=())


class PluginError(RuntimeError):
    """Safe failure; credentials and implementation exceptions must not be exposed."""
    def __init__(self, code: PluginErrorCode, message: str = "Plugin operation unavailable") -> None:
        super().__init__(message)
        self.code = code


class PluginIdentity(ContractModel):
    plugin_id: str = Field(min_length=1, max_length=190)
    version: str = Field(min_length=1, max_length=64)
    api_version: Literal[1] = 1
    config_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class PluginExport(ContractModel):
    key: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]{0,127}$")
    kind: Literal["tool", "skill"]
    name: str = Field(min_length=1, max_length=190)
    configuration_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}, "additionalProperties": False})
    user_configurable: bool = False


class PluginManifest(ContractModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,189}$")
    kind: PluginKind
    version: str = Field(min_length=1, max_length=64)
    api_version: Literal[1] = 1
    configuration_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}, "additionalProperties": False})
    required_capabilities: tuple[str, ...] = ()
    exports: tuple[PluginExport, ...] = ()

    @field_validator("exports")
    @classmethod
    def unique_exports(cls, value: tuple[PluginExport, ...]) -> tuple[PluginExport, ...]:
        if len({(entry.kind, entry.key) for entry in value}) != len(value):
            raise ValueError("duplicate plugin export")
        return value


class Namespace(ContractModel):
    user_id: str = Field(min_length=1, max_length=64)
    project_id: str | None = Field(default=None, min_length=1, max_length=64)
    workspace_id: int | None = Field(default=None, ge=1)
    include_account: bool = False


class Cancellation(Protocol):
    async def is_cancelled(self) -> bool: ...


@dataclass(frozen=True, kw_only=True)
class ExecutionContext:
    """Host-created, never parsed from model arguments; carries no credentials."""
    user_id: str
    project_id: str
    run_id: str | None = None
    root_run_id: str | None = None
    parent_run_id: str | None = None
    call_id: str | None = None
    lease_fence: int = 0
    policy_fingerprint: str | None = None
    identity: PluginIdentity | None = None
    deadline: datetime | None = None
    cancellation: Cancellation | None = None


class SecretReference(ContractModel):
    """Opaque reference; only host capability implementations resolve credentials."""
    reference: str = Field(min_length=1, max_length=190)


class Plugin(Protocol):
    manifest: PluginManifest

    async def start(self, host: PluginHost) -> None: ...
    async def close(self) -> None: ...


@dataclass(frozen=True, kw_only=True)
class PluginHost:
    """Capability-scoped host view. Only requested domain operations are supplied."""
    configuration: dict[str, Any]
    conversations: ConversationAccess | None = None
    memory: MemoryHost | None = None
    extensions: ExtensionAccess | None = None
    advisor: AdvisorAccess | None = None
    workspace: WorkspaceAccess | None = None
    artifacts: ArtifactAccess | None = None
    public_http: PublicHttpAccess | None = None
    mcp_connections: McpConnectionStore | None = None
    mcp_authority: McpAuthorityAccess | None = None


# Type-only imports keep SQLAlchemy and optional transports out of this module.
if TYPE_CHECKING:
    from .hosts import (
        AdvisorAccess,
        ArtifactAccess,
        ConversationAccess,
        ExtensionAccess,
        PublicHttpAccess,
        WorkspaceAccess,
    )
    from .mcp import McpAuthorityAccess, McpConnectionStore
    from .memory import MemoryHost
