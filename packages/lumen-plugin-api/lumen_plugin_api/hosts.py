"""Domain capabilities implement authorization at each host operation."""
from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import TYPE_CHECKING, Any, Literal, Protocol

from .contracts import ExecutionContext, Namespace

if TYPE_CHECKING:
    import httpx

    from .tools import GeneratedToolFile, ToolArtifactRef, ToolExecutionResult


class ConversationAccess(Protocol):
    async def list(self, context: ExecutionContext, *, limit: int = 20) -> list[dict[str, Any]]: ...
    async def read(self, conversation_id: str, context: ExecutionContext) -> dict[str, Any]: ...


class ExtensionAccess(Protocol):
    async def list(self, kind: Literal["tool", "skill", "mcp"], namespace: Namespace) -> list[dict[str, Any]]: ...
    async def resolve(self, kind: Literal["tool", "skill", "mcp"], identifier: int | str, namespace: Namespace) -> dict[str, Any]: ...
    async def revalidate(self, kind: Literal["tool", "skill", "mcp"], identifier: int | str, fingerprint: str, namespace: Namespace) -> dict[str, Any]: ...


class AdvisorAccess(Protocol):
    async def invoke(self, arguments: dict[str, Any], context: ExecutionContext) -> ToolExecutionResult: ...


class WorkspaceAccess(Protocol):
    async def read_file(self, path: str, context: ExecutionContext) -> bytes: ...
    async def write_file(self, path: str, content: bytes, context: ExecutionContext, *, expected_revision: int) -> int: ...
    async def execute(self, language: str, source: str, context: ExecutionContext, *, timeout_seconds: int, expected_revision: int) -> ToolExecutionResult: ...


class ArtifactAccess(Protocol):
    async def ingest(self, file: GeneratedToolFile, context: ExecutionContext) -> ToolArtifactRef: ...


class PublicHttpAccess(Protocol):
    """Host enforces DNS pinning, SSRF, TLS, no redirects and streamed size bounds."""
    def client(self, *, timeout_seconds: float = 15, max_response_bytes: int = 65536) -> AbstractAsyncContextManager[httpx.AsyncClient]: ...

    async def request(self, method: str, url: str, *, headers: dict[str, str] | None = None, json: dict[str, Any] | None = None, data: dict[str, str] | None = None) -> tuple[int, dict[str, str], bytes]: ...

    def validate_url(self, url: str) -> None: ...


class ToolHost(Protocol):
    conversations: ConversationAccess | None
    extensions: ExtensionAccess | None
    advisor: AdvisorAccess | None
    workspace: WorkspaceAccess | None
    artifacts: ArtifactAccess | None
    public_http: PublicHttpAccess | None
