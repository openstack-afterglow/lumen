"""Remote OAuth and delegated authority remain distinct credential domains."""
from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol

from pydantic import Field

from .contracts import ContractModel, ExecutionContext, Namespace, Plugin, PluginIdentity
from .tools import ToolBinding, ToolDefinition


class McpSnapshot(ContractModel):
    authority: Literal["remote", "afterglow"]
    namespace: Namespace
    identity: PluginIdentity
    server_id: int | None = None
    config_version: int = Field(default=1, ge=1)
    credential_epoch: int | None = None
    selection_generation: int | None = None
    grant_id: str | None = None
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    tools: tuple[ToolDefinition, ...] = ()
    remote_tool_names: dict[str, str] = Field(default_factory=dict)


class McpSelection(ContractModel):
    namespace: Namespace
    authority: Literal["remote", "afterglow"]
    server_ids: tuple[int, ...] = ()
    delegated_snapshot: dict[str, Any] | None = None


@dataclass(frozen=True)
class McpGeneratedFile:
    name: str
    media_type: str
    data: bytes


@dataclass(frozen=True)
class McpToolOutput:
    text: str
    files: tuple[McpGeneratedFile, ...]


class McpProvider(Plugin, Protocol):
    async def describe(self, selection: McpSelection) -> tuple[McpSnapshot, ...]: ...
    async def bind(self, snapshot: McpSnapshot, context: ExecutionContext) -> tuple[ToolBinding, ...]: ...
    async def revalidate(self, snapshot: McpSnapshot) -> None: ...


class McpRemoteProvider(McpProvider, Protocol):
    async def list_tools(self, server: dict[str, Any]) -> list[dict[str, Any]]: ...
    async def call_tool(self, server: dict[str, Any], name: str, arguments: dict[str, Any]) -> McpToolOutput: ...


class McpConnection(ContractModel):
    id: str
    server_id: int
    user_id: str
    project_id: str
    config_version: int
    credential_epoch: int
    status: str
    token: dict[str, Any] = Field(default_factory=dict, repr=False)
    expires_at: datetime | None = None


class McpOAuthConfig(ContractModel):
    callback_url: str
    allowed_return_origins: tuple[str, ...]


class McpOAuthError(ValueError):
    """Safe OAuth failure suitable for browser callback mapping."""

    return_origin: str | None = None


class McpRefreshLease(Protocol):
    connection: McpConnection
    async def save(self, token: dict[str, Any], expires_at: datetime | None) -> None: ...
    async def revoke(self) -> None: ...


class McpConnectionStore(Protocol):
    """Operations enforce scope and config version in core-owned transactions."""
    def oauth_configuration(self) -> dict[str, Any]: ...
    async def server(self, server_id: int, namespace: Namespace) -> dict[str, Any]: ...
    async def create_state(self, *, state_hash: str, server_id: int, namespace: Namespace, payload: dict[str, Any], expires_at: datetime) -> None: ...
    async def set_auth_mode(self, server_id: int, namespace: Namespace, auth_mode: Literal["none", "oauth"]) -> None: ...
    async def consume_state(self, state_hash: str) -> dict[str, Any]: ...
    async def set_state_status(self, state_hash: str, status: str) -> None: ...
    async def complete_state(self, state_hash: str, *, token: dict[str, Any], expires_at: datetime | None) -> int: ...
    async def connection(self, server_id: int, namespace: Namespace) -> McpConnection | None: ...
    async def connections(self, namespace: Namespace) -> tuple[McpConnection, ...]: ...
    async def disconnect(self, server_id: int, namespace: Namespace) -> None: ...
    def locked_refresh(self, connection_id: str) -> AbstractAsyncContextManager[McpRefreshLease]: ...


class McpAuthorityAccess(Protocol):
    async def registry(self, namespace: Namespace) -> dict[str, Any]: ...
    async def read(self, snapshot: dict[str, Any], name: str, arguments: dict[str, Any]) -> dict[str, Any]: ...
    async def preview(self, snapshot: dict[str, Any], name: str, arguments: dict[str, Any]) -> dict[str, Any]: ...
    async def claim(self, snapshot: dict[str, Any], name: str, arguments: dict[str, Any], idempotency_key: str) -> dict[str, Any]: ...
    async def complete(self, invocation_id: str) -> dict[str, Any]: ...


class McpOAuthProvider(Protocol):
    async def detect(self, server_id: int, namespace: Namespace) -> dict[str, Any]: ...
    async def begin_connect(self, server_id: int, namespace: Namespace, *, initiator_nonce: str, return_origin: str | None = None) -> dict[str, str]: ...
    async def complete_callback(self, *, state: str, code: str | None, error: str | None, iss: str | None, initiator_nonce: str | None) -> tuple[int, str | None]: ...
    async def connection_status(self, server_id: int, namespace: Namespace) -> dict[str, Any]: ...
    async def disconnect(self, server_id: int, namespace: Namespace) -> None: ...
    async def refresh(self, connection_id: str) -> dict[str, Any] | None: ...
    async def headers_for_user(self, namespace: Namespace) -> dict[int, dict[str, str]]: ...
