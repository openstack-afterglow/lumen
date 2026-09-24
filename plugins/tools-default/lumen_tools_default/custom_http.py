"""Custom administrator/user-configured HTTP tool projection and execution.

Every network call goes through the host's ``PublicHttpAccess`` capability, which
enforces SSRF policy, DNS pinning, and a bounded response size. This module only
projects the stored definition into a provider-safe schema and formats the bounded
response; it never opens a socket itself.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

from lumen_plugin_api.contracts import ExecutionContext
from lumen_plugin_api.hosts import PublicHttpAccess
from lumen_plugin_api.tools import ToolDefinition, ToolExecutionResult, ToolTextPart

CUSTOM_HTTP_EXPORT_KEY = "custom_http"
_MAX_RESPONSE_CHARS = 4_000


class InvalidCustomTool(ValueError):
    """The stored custom tool definition cannot be safely projected."""


def _provider_safe_name(identifier: int, name: object) -> str:
    raw_name = str(name)
    normalized = re.sub(r"[^A-Za-z0-9_]+", "_", raw_name).strip("_") or "tool"
    digest = hashlib.sha256(raw_name.encode("utf-8")).hexdigest()[:12]
    return f"custom__{identifier}__{normalized[:96]}_{digest}"[:128]


@dataclass(frozen=True)
class CustomHttpTool:
    identifier: int
    name: str
    description: str
    url: str
    method: str
    params_schema: dict[str, Any] | None
    timeout_seconds: int
    effect: str


def function_schema(tool: CustomHttpTool) -> dict[str, Any]:
    """Project one custom HTTP tool into its exact provider function schema."""
    return {
        "name": _provider_safe_name(tool.identifier, tool.name),
        "description": tool.description or tool.name or "Custom HTTP tool",
        "parameters": {
            **(tool.params_schema or {"type": "object", "properties": {}}),
            "additionalProperties": False,
        },
    }


def build_definition(tool: CustomHttpTool) -> ToolDefinition:
    projection = function_schema(tool)
    try:
        return ToolDefinition(
            name=projection["name"],
            description=str(projection["description"]),
            input_schema=projection["parameters"],
            effect=tool.effect if tool.effect in {"read", "workspace_write", "process", "external_mutation"} else "external_mutation",
            source="custom_http",
            activity_category="커스텀 도구",
        )
    except Exception as exc:
        raise InvalidCustomTool(f"custom tool {tool.identifier} has an invalid definition") from exc


async def execute(tool: CustomHttpTool, arguments: dict[str, Any], context: ExecutionContext, http: PublicHttpAccess) -> ToolExecutionResult:
    safe_args = arguments if isinstance(arguments, dict) else {}
    try:
        async with http.client(timeout_seconds=tool.timeout_seconds, max_response_bytes=64 * 1024) as client:
            request_kwargs = {"json": safe_args} if tool.method == "POST" else {"params": safe_args}
            async with client.stream(tool.method, tool.url, **request_kwargs) as response:
                encoding = response.headers.get("content-encoding", "identity").strip().lower()
                if encoding not in ("", "identity"):
                    return ToolExecutionResult(status="failed", model_content="압축된 툴 응답은 허용되지 않습니다.", error_code="custom_http_compressed_response")
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(content) + len(chunk) > 64 * 1024:
                        return ToolExecutionResult(status="failed", model_content="툴 응답이 허용 크기를 초과했습니다.", error_code="custom_http_response_too_large")
                    content.extend(chunk)
                text = f"[{response.status_code}] " + content.decode(response.encoding or "utf-8", errors="replace")[:_MAX_RESPONSE_CHARS]
                return ToolExecutionResult(status="completed", model_content=text[:8_192], display=[ToolTextPart(text=text[:8_192])])
    except Exception as exc:
        message = "허용되지 않은 URL 입니다(내부/사설 주소 차단)." if _is_ssrf_error(exc) else "툴 호출 중 오류가 발생했습니다."
        return ToolExecutionResult(status="failed", model_content=message, error_code="custom_http_call_failed")


def _is_ssrf_error(exc: Exception) -> bool:
    # Host implementations raise a ValueError subclass for policy violations; the
    # plugin never imports the concrete core exception type.
    return isinstance(exc, ValueError)
