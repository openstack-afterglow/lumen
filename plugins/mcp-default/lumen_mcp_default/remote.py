"""Remote Streamable HTTP MCP, scoped to a host-provided public-network transport."""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import mimetypes
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import httpx
from lumen_plugin_api.contracts import (
    ExecutionContext,
    Namespace,
    PluginError,
    PluginHost,
    PluginIdentity,
    PluginManifest,
)
from lumen_plugin_api.mcp import McpGeneratedFile, McpSelection, McpSnapshot, McpToolOutput
from lumen_plugin_api.tools import GeneratedToolFile, ToolBinding, ToolDefinition, ToolExecutionResult

from .oauth import RemoteOAuth

_TIMEOUT = 15
_MAX_RESPONSE = 1024 * 1024
_MAX_TEXT = 6000
_MAX_FILES = 20
_MAX_FILE_BYTES = 5 * 1024 * 1024
_SAFE_STEM = re.compile(r"[^A-Za-z0-9._-]+")




def _block(block: object, key: str) -> object:
    return block.get(key) if isinstance(block, dict) else getattr(block, key, None)


def result_to_output(result: Any, *, tool_name: str) -> McpToolOutput:
    if isinstance(result, tuple):
        result = result[0]
    content = getattr(result, "content", result)
    if isinstance(content, str):
        return McpToolOutput(content[:_MAX_TEXT], ())
    if not isinstance(content, (list, tuple)):
        return McpToolOutput(str(content)[:_MAX_TEXT], ())
    texts: list[str] = []
    files: list[McpGeneratedFile] = []
    total = 0
    for block in content:
        if isinstance(block, str):
            texts.append(block)
            continue
        text = _block(block, "text")
        if isinstance(text, str):
            texts.append(text)
        encoded = _block(block, "base64")
        if _block(block, "type") not in {"image", "file"} or not isinstance(encoded, str):
            continue
        if len(files) >= _MAX_FILES or len(encoded) > (_MAX_FILE_BYTES * 4 // 3 + 8):
            raise ValueError("MCP tool returned too many or oversized embedded files")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("MCP tool returned invalid embedded file data") from exc
        total += len(data)
        if not data or total > _MAX_FILE_BYTES:
            raise ValueError("MCP tool returned oversized embedded file data")
        media = _block(block, "mime_type")
        media_type = media.strip().lower() if isinstance(media, str) else "application/octet-stream"
        supplied = _block(block, "name") or _block(block, "filename")
        extension = mimetypes.guess_extension(media_type, strict=False) or ".bin"
        stem = _SAFE_STEM.sub("-", tool_name).strip(".-") or "mcp-output"
        name = supplied.strip()[:255] if isinstance(supplied, str) and supplied.strip() else f"{stem[:240-len(extension)]}-{len(files)+1}{extension}"
        files.append(McpGeneratedFile(name, media_type, data))
    return McpToolOutput("\n".join(texts)[:_MAX_TEXT], tuple(files))


def connection(server: dict[str, Any], http_factory) -> dict[str, Any]:
    transport = (server.get("transport") or "http").lower()
    if transport not in {"http", "streamable_http", "streamable-http"}:
        raise ValueError("streamable HTTP MCP transport is required")
    url = server.get("url") or ""
    parsed = urlsplit(url)
    if parsed.scheme.lower() != "https" or not parsed.netloc or parsed.username or parsed.password or parsed.fragment:
        raise ValueError("MCP URL must use HTTPS")
    headers = server.get("headers") or {}
    if not isinstance(headers, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in headers.items()):
        raise ValueError("MCP headers must be string pairs")
    return {"transport": "streamable_http", "url": url, "headers": headers, "timeout": _TIMEOUT, "httpx_client_factory": http_factory}


def _closed_schema(value: Any) -> Any:
    """Narrow open MCP object schemas to the public tool contract."""
    if isinstance(value, dict):
        result = {key: _closed_schema(child) for key, child in value.items()}
        if value.get("type") == "object":
            result["additionalProperties"] = False
        return result
    if isinstance(value, list):
        return [_closed_schema(child) for child in value]
    return value


def _schema(tool: Any) -> dict[str, Any]:
    schema = getattr(tool, "args_schema", None)
    if isinstance(schema, dict):
        return _closed_schema(schema)
    method = getattr(schema, "model_json_schema", None)
    return _closed_schema(method() if callable(method) else {"type": "object", "properties": {}})



def _provider_tool_name(server_id: int, remote_name: str) -> str:
    """Keep already-safe names stable; suffix normalized names to avoid collisions."""
    if not isinstance(remote_name, str) or not remote_name or len(remote_name) > 512:
        raise PluginError("plugin_incompatible", "remote MCP tool name is invalid")
    prefix = f"mcp_{server_id}_"
    original = prefix + remote_name
    if len(original) <= 128 and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", original):
        return original
    stem = re.sub(r"[^A-Za-z0-9_]", "_", remote_name).strip("_") or "tool"
    digest = hashlib.sha256(remote_name.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}{stem[:128 - len(prefix) - len(digest) - 1]}_{digest}"

class RemoteTransport:
    """No client or credentials survive an invocation; host pins every socket."""
    def __init__(self, host: PluginHost):
        if host.public_http is None:
            raise PluginError("plugin_unavailable", "Public HTTP capability is required")
        self._http = host.public_http

    def _http_factory(self, headers=None, timeout=None, auth=None) -> httpx.AsyncClient:
        client = self._http.client(timeout_seconds=_TIMEOUT, max_response_bytes=_MAX_RESPONSE)
        client.headers.update(headers or {})
        client.headers["Accept-Encoding"] = "identity"
        if timeout is not None:
            client.timeout = timeout
        if auth is not None:
            client.auth = auth
        return client

    def _client(self, server: dict[str, Any]):
        from langchain_mcp_adapters.client import MultiServerMCPClient
        configured = connection(server, self._http_factory)
        self._http.validate_url(configured["url"])
        return MultiServerMCPClient({"remote": configured}, handle_tool_errors=False)

    async def list_tools(self, server: dict[str, Any]) -> list[dict[str, Any]]:
        async def run():
            tools = await self._client(server).get_tools(server_name="remote")
            return [{"name": t.name, "description": t.description or t.name, "input_schema": _schema(t)} for t in tools[:40]]
        return await asyncio.wait_for(run(), timeout=_TIMEOUT)

    async def call_tool(self, server: dict[str, Any], name: str, arguments: dict[str, Any]) -> McpToolOutput:
        if not isinstance(arguments, dict):
            raise ValueError("MCP tool arguments must be an object")
        async def run():
            tools = await self._client(server).get_tools(server_name="remote")
            tool = next((candidate for candidate in tools if candidate.name == name), None)
            if tool is None:
                raise ValueError("MCP tool is unavailable")
            return result_to_output(await tool.ainvoke(arguments), tool_name=name)
        return await asyncio.wait_for(run(), timeout=_TIMEOUT)


def _fingerprint(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


class RemoteMcp:
    manifest = PluginManifest(id="remote-mcp", kind="mcp", version="0.1.0", required_capabilities=("extensions", "public_http", "mcp_connections"))

    def __init__(self) -> None:
        self._extensions = None
        self._connections = None
        self.transport: RemoteTransport | None = None
        self._oauth: RemoteOAuth | None = None

    async def start(self, host: PluginHost) -> None:
        if host.extensions is None or host.mcp_connections is None:
            raise PluginError("plugin_unavailable", "Scoped extension and connection capabilities are required")
        transport = RemoteTransport(host)
        oauth = RemoteOAuth(host)
        self._extensions = host.extensions
        self._connections = host.mcp_connections
        self.transport = transport
        self._oauth = oauth

    async def close(self) -> None:
        self._extensions = None
        self._connections = None
        self.transport = None
        self._oauth = None

    def _ready(self) -> tuple[RemoteTransport, RemoteOAuth]:
        if self.transport is None or self._oauth is None:
            raise PluginError("plugin_unavailable", "Remote MCP plugin has not started")
        return self.transport, self._oauth

    async def list_tools(self, server: dict[str, Any]) -> list[dict[str, Any]]:
        transport, _ = self._ready()
        return await transport.list_tools(server)

    async def call_tool(self, server: dict[str, Any], name: str, arguments: dict[str, Any]) -> McpToolOutput:
        transport, _ = self._ready()
        return await transport.call_tool(server, name, arguments)

    async def detect(self, server_id: int, namespace: Namespace) -> dict[str, Any]:
        return await self._ready()[1].detect(server_id, namespace)

    async def begin_connect(self, server_id: int, namespace: Namespace, *, initiator_nonce: str, return_origin: str | None = None) -> dict[str, str]:
        return await self._ready()[1].begin_connect(server_id, namespace, initiator_nonce=initiator_nonce, return_origin=return_origin)

    async def complete_callback(self, *, state: str, code: str | None, error: str | None, iss: str | None, initiator_nonce: str | None) -> tuple[int, str | None]:
        return await self._ready()[1].complete_callback(state=state, code=code, error=error, iss=iss, initiator_nonce=initiator_nonce)

    async def connection_status(self, server_id: int, namespace: Namespace) -> dict[str, Any]:
        return await self._ready()[1].connection_status(server_id, namespace)

    async def disconnect(self, server_id: int, namespace: Namespace) -> None:
        await self._ready()[1].disconnect(server_id, namespace)

    async def refresh(self, connection_id: str) -> dict[str, Any] | None:
        return await self._ready()[1].refresh(connection_id)

    async def headers_for_user(self, namespace: Namespace) -> dict[int, dict[str, str]]:
        return await self._ready()[1].headers_for_user(namespace)

    async def _server(self, server_id: int, namespace: Namespace) -> tuple[dict[str, Any], int | None]:
        transport, _ = self._ready()
        server = await self._extensions.resolve("mcp", server_id, namespace)
        if not server.get("is_active") or server.get("id") != server_id:
            raise PluginError("plugin_authority_revoked")
        connection(server, transport._http_factory)
        transport._http.validate_url(server["url"])
        mode = server.get("auth_mode", "none")
        if mode not in {"none", "admin", "oauth"}:
            raise PluginError("plugin_authority_revoked")
        if mode == "oauth":
            credential = await self._connections.connection(server_id, namespace)
            if credential is None or credential.status != "active" or credential.config_version != server.get("config_version"):
                raise PluginError("plugin_authority_revoked")
            if credential.expires_at is not None:
                expiry = credential.expires_at
                if expiry.tzinfo is None:
                    expiry = expiry.replace(tzinfo=UTC)
                if expiry <= datetime.now(UTC) + timedelta(seconds=60):
                    raise PluginError("plugin_authority_revoked", "OAuth connection requires refresh")
            token = credential.token.get("access_token")
            if not isinstance(token, str) or not token:
                raise PluginError("plugin_authority_revoked")
            server = {**server, "headers": {"Authorization": f"Bearer {token}"}}
            return server, credential.credential_epoch
        if mode == "none" and server.get("headers"):
            raise PluginError("plugin_authority_revoked")
        if mode == "admin" and not isinstance(server.get("headers", {}), dict):
            raise PluginError("plugin_authority_revoked")
        return server, None

    async def describe(self, selection: McpSelection) -> tuple[McpSnapshot, ...]:
        if selection.authority != "remote" or selection.delegated_snapshot is not None:
            raise PluginError("plugin_authority_revoked")
        snapshots = []
        for server_id in dict.fromkeys(selection.server_ids):
            server, epoch = await self._server(server_id, selection.namespace)
            tools = await self.list_tools(server)
            definitions = []
            remote_tool_names: dict[str, str] = {}
            for item in tools:
                effect = (server.get("effect_overrides") or {}).get(item["name"], "read")
                if effect not in {"read", "external_mutation"}:
                    raise PluginError("plugin_configuration_changed")
                name = _provider_tool_name(server_id, item["name"])
                if name in remote_tool_names:
                    raise PluginError("plugin_incompatible", "remote MCP tool names collide")
                remote_tool_names[name] = item["name"]
                definitions.append(ToolDefinition(name=name, description=item["description"], input_schema=item["input_schema"], effect=effect, source="mcp"))
            fingerprint = _fingerprint({k: v for k, v in server.items() if k != "headers"})
            identity = PluginIdentity(plugin_id=self.manifest.id, version=self.manifest.version, config_fingerprint=fingerprint)
            snapshots.append(McpSnapshot(authority="remote", namespace=selection.namespace, identity=identity, server_id=server_id, config_version=server["config_version"], credential_epoch=epoch, fingerprint=fingerprint, tools=tuple(definitions), remote_tool_names=remote_tool_names))
        return tuple(snapshots)

    async def revalidate(self, snapshot: McpSnapshot) -> None:
        if snapshot.authority != "remote" or snapshot.server_id is None or snapshot.identity.plugin_id != self.manifest.id:
            raise PluginError("plugin_authority_revoked")
        server, epoch = await self._server(snapshot.server_id, snapshot.namespace)
        fingerprint = _fingerprint({k: v for k, v in server.items() if k != "headers"})
        if server["config_version"] != snapshot.config_version or epoch != snapshot.credential_epoch or fingerprint != snapshot.fingerprint:
            raise PluginError("plugin_configuration_changed")

    async def bind(self, snapshot: McpSnapshot, context: ExecutionContext) -> tuple[ToolBinding, ...]:
        if (context.user_id, context.project_id) != (snapshot.namespace.user_id, snapshot.namespace.project_id):
            raise PluginError("plugin_authority_revoked")
        await self.revalidate(snapshot)
        server, _ = await self._server(snapshot.server_id, snapshot.namespace)
        url = urlsplit(server["url"])
        destination_origin = f"{url.scheme}://{url.netloc}"
        bindings = []
        if snapshot.remote_tool_names and set(snapshot.remote_tool_names) != {definition.name for definition in snapshot.tools}:
            raise PluginError("plugin_configuration_changed", "remote MCP snapshot tool names changed")
        for definition in snapshot.tools:
            prefix = f"mcp_{snapshot.server_id}_"
            if not definition.name.startswith(prefix):
                raise PluginError("plugin_configuration_changed")
            method = snapshot.remote_tool_names.get(definition.name, definition.name[len(prefix):])
            if _provider_tool_name(snapshot.server_id, method) != definition.name:
                raise PluginError("plugin_configuration_changed", "remote MCP tool name changed")
            async def execute(args: dict[str, Any], ctx: ExecutionContext, *, method=method):
                if (ctx.user_id, ctx.project_id) != (snapshot.namespace.user_id, snapshot.namespace.project_id):
                    raise PluginError("plugin_authority_revoked")
                await self.revalidate(snapshot)
                server, _ = await self._server(snapshot.server_id, snapshot.namespace)
                output = await self.call_tool(server, method, args)
                return ToolExecutionResult(status="completed", model_content=output.text, generated_files=[GeneratedToolFile(name=f.name, media_type=f.media_type, data=f.data) for f in output.files])
            bindings.append(ToolBinding(definition=definition, execute=execute, config_fingerprint=snapshot.fingerprint, destination_origin=destination_origin))
        return tuple(bindings)


def create_remote_plugin() -> RemoteMcp:
    return RemoteMcp()
