"""Per-run ``run_code`` binding backed by the run's managed sandbox resource.

The worker never holds the operator key: every dispatch asks the controller for a
capability bound to the live lease fence, run, resource generation and request
fingerprint, then calls the sandbox over the identity-pinned internal transport.
Artifacts are pulled over mTLS and ingested through the existing asset path before
the tool result references them; failed ingest is an explicit failure.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import logging
import math
import mimetypes
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from lumen_plugin_api.tools import (
    MAX_TOOL_MODEL_CONTENT_CHARS,
    GeneratedToolFile,
    ToolBinding,
    ToolDefinition,
    ToolExecutionResult,
    ToolTextPart,
)
from sqlalchemy import select

from lumen.config import get_settings
from lumen.models.chat_infrastructure import ChatAgentReservation, ChatRuntimeResource
from lumen.models.chat_runs import ChatRun
from lumen.services.infrastructure import dispatch as capability_client
from lumen.services.infrastructure.transport import InternalTransport, InternalTransportError
from lumen.services.tools import ToolContext

logger = logging.getLogger(__name__)

RUN_CODE_TOOL_NAME = "run_code"
_MAX_SOURCE_BYTES = 64 * 1024
_MAX_TIMEOUT_SECONDS = 300
_POLL_INTERVAL_SECONDS = 0.5
_MAX_ARTIFACTS = 20
_MAX_ARTIFACT_BYTES = 5 * 1024 * 1024
_MCP_MODEL_PREVIEW_BYTES = 8 * 1024
_MCP_DISPLAY_PREVIEW_BYTES = 16 * 1024
_MCP_TRUNCATION_MARKER = "\n[Truncated; see displayed result or generated files.]"


@dataclass(frozen=True)
class SandboxTarget:
    resource_id: str
    generation: int
    address: str
    port: int
    certificate_fingerprint: str
    deadline_at: datetime | None
    backend: str = "nova"


class SandboxDispatchError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def run_code_definition() -> ToolDefinition:
    return ToolDefinition(
        name=RUN_CODE_TOOL_NAME,
        description=(
            "Execute source code in this run's isolated sandbox. Python 3.12, a pinned Node LTS and POSIX sh "
            "are available; there is no network access. Files written under the workspace are returned as artifacts."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "language": {"type": "string", "enum": ["python", "javascript", "shell"]},
                "source": {"type": "string", "minLength": 1, "maxLength": _MAX_SOURCE_BYTES},
                "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": _MAX_TIMEOUT_SECONDS},
            },
            "required": ["language", "source"],
            "additionalProperties": False,
        },
        effect="process",
        parallel_safe=False,
        source="workspace",
        activity_category="코드 실행",
    )


async def assigned_sandbox(run_id: str) -> SandboxTarget | None:
    """Return the run's ready sandbox generation from controller observations only."""
    from lumen.db import get_session_factory

    factory = get_session_factory()
    if factory is None:
        return None
    async with factory() as session:
        run = (await session.execute(select(ChatRun).where(ChatRun.id == run_id))).scalar_one_or_none()
        if run is None or run.assigned_resource_id is None:
            return None
        resource = await session.get(ChatRuntimeResource, run.assigned_resource_id)
    if (
        resource is None
        or resource.run_id != run_id
        or resource.observed_state != "ready"
        or not resource.address
        or resource.port is None
        or not resource.certificate_fingerprint
    ):
        return None
    return SandboxTarget(
        resource_id=resource.id,
        generation=int(resource.generation),
        address=resource.address,
        port=int(resource.port),
        certificate_fingerprint=resource.certificate_fingerprint,
        deadline_at=resource.deadline_at,
        backend=str(resource.backend),
    )


def _deadline(target: SandboxTarget, seconds: int) -> datetime:
    now = datetime.now(UTC)
    deadline = now + timedelta(seconds=seconds + 30)
    hard = target.deadline_at.replace(tzinfo=UTC) if target.deadline_at and target.deadline_at.tzinfo is None else target.deadline_at
    return min(deadline, hard) if hard is not None else deadline


async def _authorized_request(
    transport: InternalTransport,
    target: SandboxTarget,
    *,
    run_id: str,
    lease_owner: str,
    method: str,
    path: str,
    deadline: datetime,
    body: dict[str, Any] | None = None,
    call_id: str | None = None,
    workspace_revision: int | None = None,
) -> bytes:
    grant = await capability_client.request_capability(
        get_settings().runtime_config,
        transport,
        run_id=run_id,
        lease_owner=lease_owner,
        resource_id=target.resource_id,
        generation=target.generation,
        method=method,
        path=path,
        call_id=call_id,
        workspace_revision=workspace_revision,
        body=body,
    )
    return await transport.request(
        address=grant.address,
        port=grant.port,
        role="sandbox",
        resource_id=target.resource_id,
        generation=target.generation,
        certificate_fingerprints=frozenset({grant.certificate_fingerprint}),
        method=method,
        path=path,
        deadline=deadline,
        capability=grant.capability,
        body=body,
    )


def _wire_call_id(call_id: str) -> str:
    return call_id if len(call_id) <= 128 else "sandbox-" + hashlib.sha256(call_id.encode()).hexdigest()


async def _workspace_revision(
    transport: InternalTransport, target: SandboxTarget, *,
    run_id: str, lease_owner: str, deadline: datetime,
) -> int:
    """The sandbox journal, not an ephemeral per-tool closure, owns the revision."""
    try:
        state = json.loads(await _authorized_request(
            transport, target, run_id=run_id, lease_owner=lease_owner,
            method="GET", path="/v1/workspace", deadline=deadline,
        ))
        revision = state["workspace_revision"]
        if type(revision) is not int or revision < 0:
            raise ValueError("invalid workspace revision")
        return revision
    except InternalTransportError as exc:
        raise SandboxDispatchError("resource_unavailable", "sandbox revision is unavailable") from exc
    except (KeyError, TypeError, ValueError) as exc:
        raise SandboxDispatchError("sandbox_response_invalid", "sandbox revision is invalid") from exc


async def execute_in_sandbox(
    *,
    run_id: str,
    lease_owner: str,
    call_id: str,
    fence: int,
    language: str,
    source: str,
    timeout_seconds: int,
) -> dict[str, Any]:
    """Submit one fenced execution, poll to a terminal state and return the bounded result."""
    target = await assigned_sandbox(run_id)
    if target is None:
        raise SandboxDispatchError("resource_unavailable", "the run has no ready sandbox")
    settings = get_settings()
    try:
        transport = InternalTransport(settings.runtime_config, worker_client=True)
    except InternalTransportError as exc:
        raise SandboxDispatchError("resource_unavailable", "sandbox transport is unavailable") from exc
    deadline = _deadline(target, timeout_seconds)
    call_id = _wire_call_id(call_id)
    workspace_revision = await _workspace_revision(
        transport, target, run_id=run_id, lease_owner=lease_owner, deadline=deadline,
    )
    body = {
        "run_id": run_id,
        "call_id": call_id,
        "fence": fence,
        "language": language,
        "source": source,
        "timeout_seconds": timeout_seconds,
        "workspace_revision": workspace_revision,
    }
    try:
        accepted = json.loads(
            await _authorized_request(
                transport,
                target,
                run_id=run_id,
                lease_owner=lease_owner,
                method="POST",
                path="/v1/executions",
                deadline=deadline,
                body=body,
                call_id=call_id,
                workspace_revision=workspace_revision,
            )
        )
        execution_id = str(accepted["id"])
        while True:
            status = json.loads(
                await _authorized_request(
                    transport,
                    target,
                    run_id=run_id,
                    lease_owner=lease_owner,
                    method="GET",
                    path=f"/v1/executions/{execution_id}",
                    deadline=deadline,
                )
            )
            if status.get("state") in {"succeeded", "failed", "timed_out", "cancelled", "output_limit_exceeded", "indeterminate"}:
                break
            if datetime.now(UTC) >= deadline:
                raise SandboxDispatchError("resource_deadline_exceeded", "sandbox execution exceeded its deadline")
            await asyncio.sleep(_POLL_INTERVAL_SECONDS)
        artifacts: list[GeneratedToolFile] = []
        for ref in (status.get("artifacts") or [])[:_MAX_ARTIFACTS]:
            artifact_id = str(ref.get("id") or "")
            data = await _authorized_request(
                transport,
                target,
                run_id=run_id,
                lease_owner=lease_owner,
                method="GET",
                path=f"/v1/artifacts/{artifact_id}",
                deadline=deadline,
            )
            if len(data) > _MAX_ARTIFACT_BYTES:
                raise SandboxDispatchError("artifact_too_large", "sandbox artifact exceeds 5 MiB")
            artifacts.append(
                GeneratedToolFile(
                    data=data,
                    name=str(ref.get("name") or f"artifact-{artifact_id}"),
                    media_type=str(ref.get("media_type") or "application/octet-stream"),
                )
            )
    except InternalTransportError as exc:
        raise SandboxDispatchError(getattr(exc, "code", "resource_unavailable"), str(exc)) from exc
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SandboxDispatchError("sandbox_response_invalid", "sandbox returned an invalid response") from exc
    return {"status": status, "artifacts": artifacts}

async def ensure_sandbox_for_run(
    run_id: str,
    *,
    lease_owner: str,
    fence: int,
    pool_name: str,
    wait_timeout: float = 30.0,
) -> SandboxTarget:
    """Allocate only under a live worker lease, in quota -> root -> run lock order."""
    from lumen.db import get_session_factory
    from lumen.services.durable_runs.budgets import BudgetExceeded, LockOrder, lock_project_quota, reserve
    from lumen.services.infrastructure.store import ResourceUnavailable, assign_sandbox

    factory = get_session_factory()
    if factory is None:
        raise SandboxDispatchError("database_unavailable", "database is not configured")
    settings = get_settings()
    pool = settings.runtime_config.pool(pool_name)
    if not settings.runtime_config.enabled or not pool.enabled or pool.backend != "kubernetes" or pool.role != "sandbox":
        raise SandboxDispatchError("sandbox_pool_unavailable", "sandbox pool is not enabled")
    order = LockOrder()
    async with factory() as session, session.begin():
        identity = (await session.execute(
            select(ChatRun.project_id, ChatRun.root_run_id).where(ChatRun.id == run_id)
        )).one_or_none()
        if identity is None:
            raise SandboxDispatchError("run_missing", "run was not found")
        quota = await lock_project_quota(session, identity.project_id, order=order)
        order.take("root")
        root_id = identity.root_run_id or run_id
        root = (await session.execute(
            select(ChatRun).where(ChatRun.id == root_id).with_for_update()
            .execution_options(populate_existing=True)
        )).scalar_one_or_none()
        if root_id == run_id:
            run = root
        else:
            order.take("child")
            run = (await session.execute(
                select(ChatRun).where(ChatRun.id == run_id).with_for_update()
                .execution_options(populate_existing=True)
            )).scalar_one_or_none()
        now = datetime.now(UTC)
        if (run is None or root is None or run.project_id != identity.project_id
                or (run.root_run_id or run.id) != root_id
                or run.status != "running" or run.cancel_requested_at is not None
                or root.cancel_requested_at is not None or run.lease_owner != lease_owner
                or run.lease_fence != fence or run.lease_expires_at is None
                or run.lease_expires_at.replace(tzinfo=UTC) <= now
                or any(value is not None and value.replace(tzinfo=UTC) <= now
                       for value in (run.deadline_at, root.deadline_at))):
            raise SandboxDispatchError("run_lease_invalid", "run lease is no longer active")
        deadline = min(
            run.deadline_at.replace(tzinfo=UTC) if run.deadline_at is not None else now + timedelta(seconds=1800),
            now + timedelta(seconds=pool.max_lifetime_seconds),
            root.deadline_at.replace(tzinfo=UTC) if root.deadline_at is not None else now + timedelta(seconds=1800),
        )
        if run.assigned_resource_id is None:
            order.take("ledger")
            holds = {row.kind: row for row in (await session.execute(
                select(ChatAgentReservation).where(
                    ChatAgentReservation.run_id == run.id,
                    ChatAgentReservation.kind.in_(("sandbox_slot", "sandbox_seconds")),
                ).with_for_update().execution_options(populate_existing=True)
            )).scalars()}
            if holds:
                if set(holds) != {"sandbox_slot", "sandbox_seconds"} or any(row.status != "reserved" for row in holds.values()):
                    raise SandboxDispatchError("sandbox_reservation_invalid", "sandbox reservation is no longer active")
                deadline = min(deadline, now + timedelta(seconds=int(holds["sandbox_seconds"].amount)))
            else:
                if root.sandbox_seconds_ceiling is not None:
                    available = root.sandbox_seconds_ceiling - root.sandbox_seconds_reserved
                    if available <= 0:
                        raise SandboxDispatchError("child_budget_exhausted", "sandbox time ceiling is exhausted")
                    deadline = min(deadline, now + timedelta(seconds=available))
                try:
                    await reserve(session, quota=quota, root=root, run_id=run.id,
                                  kind="sandbox_slot", amount=1, order=order)
                    await reserve(session, quota=quota, root=root, run_id=run.id,
                                  kind="sandbox_seconds", amount=math.ceil((deadline - now).total_seconds()),
                                  order=order, allow_unbudgeted_chat=True)
                except BudgetExceeded as exc:
                    raise SandboxDispatchError(exc.code, "sandbox quota is unavailable") from exc
        try:
            resource = await assign_sandbox(
                session, run=run, config=settings.runtime_config,
                deadline_at=deadline, order=order, pool_name=pool_name,
            )
        except ResourceUnavailable as exc:
            raise SandboxDispatchError(exc.code, "sandbox resource is unavailable") from exc
        if resource.policy_digest != pool.digest() or resource.image_ref != pool.image:
            raise SandboxDispatchError("sandbox_configuration_changed", "sandbox execution identity changed")
    start_time = asyncio.get_running_loop().time()
    while asyncio.get_running_loop().time() - start_time < wait_timeout:
        target = await assigned_sandbox(run_id)
        if target is not None:
            return target
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)
    raise SandboxDispatchError("sandbox_timeout", "timed out waiting for sandbox readiness")


async def execute_mcp_in_sandbox(
    *,
    run_id: str,
    lease_owner: str,
    call_id: str,
    fence: int,
    profile: str,
    tool: str,
    arguments: dict[str, Any],
    timeout_seconds: int,
    pool_name: str,
) -> dict[str, Any]:
    """Submit one MCP execution to the Kubernetes sandbox daemon and await result."""
    target = await ensure_sandbox_for_run(run_id, lease_owner=lease_owner, fence=fence, pool_name=pool_name)
    if target.backend != "kubernetes":
        raise SandboxDispatchError("sandbox_backend_mismatch", "MCP tool requires a Kubernetes sandbox")
    settings = get_settings()
    try:
        transport = InternalTransport(settings.runtime_config, worker_client=True)
    except InternalTransportError as exc:
        raise SandboxDispatchError("resource_unavailable", "sandbox transport is unavailable") from exc
    deadline = _deadline(target, timeout_seconds)
    call_id = _wire_call_id(call_id)
    workspace_revision = await _workspace_revision(
        transport, target, run_id=run_id, lease_owner=lease_owner, deadline=deadline,
    )
    body = {
        "run_id": run_id,
        "call_id": call_id,
        "fence": fence,
        "profile": profile,
        "tool": tool,
        "arguments": arguments,
        "timeout_seconds": timeout_seconds,
        "workspace_revision": workspace_revision,
    }
    try:
        accepted = json.loads(
            await _authorized_request(
                transport,
                target,
                run_id=run_id,
                lease_owner=lease_owner,
                method="POST",
                path="/v1/mcp/executions",
                deadline=deadline,
                body=body,
                call_id=call_id,
                workspace_revision=workspace_revision,
            )
        )
        eid = str(accepted["id"])
        status = accepted
        poll_deadline = deadline + timedelta(seconds=10)
        while str(status.get("state")) not in {"succeeded", "failed", "cancelled", "timeout", "oom", "indeterminate"}:
            await asyncio.sleep(_POLL_INTERVAL_SECONDS)
            status = json.loads(
                await _authorized_request(
                    transport,
                    target,
                    run_id=run_id,
                    lease_owner=lease_owner,
                    method="GET",
                    path=f"/v1/executions/{eid}",
                    deadline=poll_deadline,
                )
            )
    except InternalTransportError as exc:
        raise SandboxDispatchError(getattr(exc, "code", "resource_unavailable"), str(exc)) from exc
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SandboxDispatchError("sandbox_response_invalid", "sandbox returned an invalid response") from exc
    return status


def run_code_binding(ctx: ToolContext, *, lease_owner: str, fence: int) -> ToolBinding:
    """Server-only binding; dispatch happens through the canonical approval/durable segment path."""
    definition = run_code_definition()

    async def execute(arguments: dict[str, Any], context: object) -> ToolExecutionResult:
        if not isinstance(context, ToolContext) or context.run_id != ctx.run_id or context.run_id is None:
            return ToolExecutionResult(status="failed", model_content="Sandbox execution context is invalid.", error_code="invalid_tool_context")
        call_id = context.tool_call_id or str(uuid4())
        try:
            outcome = await execute_in_sandbox(
                run_id=context.run_id,
                lease_owner=lease_owner,
                call_id=call_id,
                fence=fence,
                language=str(arguments["language"]),
                source=str(arguments["source"]),
                timeout_seconds=int(arguments.get("timeout_seconds") or 60),
            )
        except SandboxDispatchError as exc:
            return ToolExecutionResult(status="failed", model_content=f"Sandbox execution failed ({exc.code}).", error_code=exc.code)
        status = outcome["status"]
        state = str(status.get("state"))
        if state == "indeterminate":
            return ToolExecutionResult(
                status="failed",
                model_content="The sandbox lost this execution's result; it was not rerun.",
                error_code="sandbox_result_unknown",
            )
        stdout = str(status.get("stdout") or "")[:8_000]
        stderr = str(status.get("stderr") or "")[:2_000]
        exit_code = status.get("exit_code")
        summary = json.dumps(
            {"state": state, "exit_code": exit_code, "stdout": stdout, "stderr": stderr, "duration_seconds": status.get("duration_seconds")},
            ensure_ascii=False,
        )[:8_192]
        display = [ToolTextPart(text=stdout or f"exit {exit_code}")]
        completed = state == "succeeded" and exit_code == 0
        return ToolExecutionResult(
            status="completed" if completed else "failed",
            model_content=summary,
            display=display,
            generated_files=outcome["artifacts"],
            usage_components={
                "components": [
                    {
                        "kind": "sandbox_seconds",
                        "price_key": "sandbox_per_second",
                        "quantity": str(status.get("duration_seconds") or 0),
                        "unit": "second",
                        "source": "sandbox",
                    }
                ]
            },
            error_code=None if completed else (status.get("error_code") or f"sandbox_{state}"),
        )

    return ToolBinding(definition=definition, execute=execute)

def _kubernetes_binding_fingerprint(pool_name: str, profile_id: str) -> str:
    config = get_settings().runtime_config
    pool = config.pool(pool_name)
    cluster = next(item for item in config.kubernetes_clusters if item.id == pool.profile.cluster_profile_id)
    identity = {
        "enabled": config.enabled, "deployment_id": config.deployment_id, "controller_url": config.controller_url,
        "pool_digest": pool.digest(), "profile_id": profile_id,
        "cluster": cluster.model_dump(mode="json", exclude={"bearer_token", "ca_file", "qualification_file"}),
    }
    return hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _mcp_preview(text: str, max_bytes: int, *, max_chars: int | None = None) -> str:
    """Bound JSON-escaped UTF-8, not characters, inside the 64 KiB tool checkpoint."""
    def fits(value: str) -> bool:
        return ((max_chars is None or len(value) <= max_chars)
                and len(json.dumps(value, ensure_ascii=False).encode()) <= max_bytes)

    if fits(text):
        return text
    low, high = 0, min(len(text), max_bytes)
    while low < high:
        middle = (low + high + 1) // 2
        if fits(text[:middle] + _MCP_TRUNCATION_MARKER):
            low = middle
        else:
            high = middle - 1
    return text[:low] + _MCP_TRUNCATION_MARKER


def _project_mcp_result(status: dict[str, Any]) -> ToolExecutionResult:
    """Preserve bounded text and embedded media through the existing asset pipeline."""
    state = str(status.get("state"))
    mcp_result = status.get("mcp_result")
    if state != "succeeded" and mcp_result is None:
        return ToolExecutionResult(
            status="failed", model_content=f"Sandbox MCP tool execution failed ({state}).",
            error_code=status.get("error_code") or f"mcp_{state}",
        )
    try:
        if not isinstance(mcp_result, dict) or not isinstance(mcp_result.get("content"), list):
            raise ValueError("invalid MCP result")
        text_parts: list[str] = []
        files: list[GeneratedToolFile] = []
        for part in mcp_result["content"]:
            if not isinstance(part, dict):
                raise ValueError("invalid MCP content")
            kind = part.get("type")
            if kind == "text":
                if not isinstance(part.get("text"), str):
                    raise ValueError("invalid MCP text")
                text_parts.append(part["text"])
                continue
            resource = part.get("resource") if kind == "resource" else part
            if not isinstance(resource, dict) or kind not in {"image", "audio", "resource"}:
                raise ValueError("unsupported MCP content")
            media_type = resource.get("mimeType", "application/octet-stream")
            if not isinstance(media_type, str) or not 0 < len(media_type) <= 127:
                raise ValueError("invalid MCP media type")
            if kind == "resource" and isinstance(resource.get("text"), str):
                data = resource["text"].encode()
                text_parts.append(resource["text"])
                if media_type == "application/octet-stream":
                    media_type = "text/plain"
            else:
                encoded = resource.get("blob") if kind == "resource" else resource.get("data")
                if not isinstance(encoded, str) or len(encoded) > ((_MAX_ARTIFACT_BYTES + 2) // 3) * 4:
                    raise ValueError("MCP media exceeds artifact bounds")
                data = base64.b64decode(encoded, validate=True)
            if len(data) > _MAX_ARTIFACT_BYTES or len(files) >= _MAX_ARTIFACTS:
                raise ValueError("MCP media exceeds artifact bounds")
            extension = mimetypes.guess_extension(media_type, strict=False) or ".bin"
            files.append(GeneratedToolFile(data=data, name=f"tool-result-{len(files) + 1}{extension}", media_type=media_type))
        structured = mcp_result.get("structuredContent")
        if structured is not None:
            text_parts.append(json.dumps(structured, ensure_ascii=False, allow_nan=False))
        text = "\n".join(text_parts)
        display_text = _mcp_preview(text, _MCP_DISPLAY_PREVIEW_BYTES)
        if display_text != text:
            if len(files) >= _MAX_ARTIFACTS or len(text.encode()) > _MAX_ARTIFACT_BYTES:
                raise ValueError("MCP text exceeds artifact bounds")
            files.append(GeneratedToolFile(data=text.encode(), name="tool-result.txt", media_type="text/plain"))
        completed = state == "succeeded" and not mcp_result.get("isError", False)
        summary = text or ("Tool execution completed." if completed else "Tool execution failed.")
        if files:
            summary = "Generated files: " + ", ".join(item.name for item in files) + "\n" + summary
        summary = _mcp_preview(summary, _MCP_MODEL_PREVIEW_BYTES, max_chars=MAX_TOOL_MODEL_CONTENT_CHARS)
        return ToolExecutionResult(
            status="completed" if completed else "failed", model_content=summary,
            display=[ToolTextPart(text=display_text)] if display_text else [], generated_files=files,
            error_code=None if completed else (status.get("error_code") or "mcp_tool_error"),
        )
    except (ValueError, TypeError, binascii.Error):
        return ToolExecutionResult(
            status="failed", model_content="Sandbox MCP returned invalid, unsupported or oversized content.",
            error_code="sandbox_result_invalid",
        )


def kubernetes_mcp_binding(
    ctx: ToolContext,
    *,
    lease_owner: str,
    fence: int,
    profile_id: str,
    tool_def: ToolDefinition,
    pool_name: str,
) -> ToolBinding:
    fingerprint = _kubernetes_binding_fingerprint(pool_name, profile_id)

    async def execute(arguments: dict[str, Any], context: object) -> ToolExecutionResult:
        if (not isinstance(context, ToolContext) or context.run_id != ctx.run_id or context.run_id is None
                or context.project_id != ctx.project_id or context.user_id != ctx.user_id):
            return ToolExecutionResult(
                status="failed",
                model_content="Sandbox execution context is invalid.",
                error_code="invalid_tool_context",
            )
        call_id = context.tool_call_id or str(uuid4())
        try:
            if _kubernetes_binding_fingerprint(pool_name, profile_id) != fingerprint:
                raise SandboxDispatchError("sandbox_configuration_changed", "sandbox execution identity changed")
            status = await execute_mcp_in_sandbox(
                run_id=context.run_id,
                lease_owner=lease_owner,
                call_id=call_id,
                fence=fence,
                profile=profile_id,
                tool=tool_def.name,
                arguments=arguments,
                timeout_seconds=int(arguments.get("timeout_seconds") or 60),
                pool_name=pool_name,
            )
        except SandboxDispatchError as exc:
            return ToolExecutionResult(
                status="failed",
                model_content=f"Sandbox MCP tool execution failed ({exc.code}).",
                error_code=exc.code,
            )
        state = str(status.get("state"))
        if state == "indeterminate":
            return ToolExecutionResult(
                status="failed",
                model_content="The sandbox lost this tool execution's result; it was not rerun.",
                error_code="sandbox_result_unknown",
            )
        return _project_mcp_result(status)

    return ToolBinding(
        definition=tool_def,
        execute=execute,
        config_fingerprint=fingerprint,
    )
