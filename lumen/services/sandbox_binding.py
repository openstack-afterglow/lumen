"""Per-run ``run_code`` binding backed by the run's managed sandbox resource.

The worker never holds the operator key: every dispatch asks the controller for a
capability bound to the live lease fence, run, resource generation and request
fingerprint, then calls the sandbox over the identity-pinned internal transport.
Artifacts are pulled over mTLS and ingested through the existing asset path before
the tool result references them; failed ingest is an explicit failure.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from lumen_plugin_api.tools import GeneratedToolFile, ToolBinding, ToolDefinition, ToolExecutionResult, ToolTextPart
from sqlalchemy import select

from lumen.config import get_settings
from lumen.models.chat_infrastructure import ChatRuntimeResource
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


@dataclass(frozen=True)
class SandboxTarget:
    resource_id: str
    generation: int
    address: str
    port: int
    certificate_fingerprint: str
    deadline_at: datetime | None


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
        certificate_fingerprint=grant.certificate_fingerprint,
        method=method,
        path=path,
        deadline=deadline,
        capability=grant.capability,
        body=body,
    )


async def execute_in_sandbox(
    *,
    run_id: str,
    lease_owner: str,
    call_id: str,
    fence: int,
    workspace_revision: int,
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
        transport = InternalTransport(settings.runtime_config)
    except InternalTransportError as exc:
        raise SandboxDispatchError("resource_unavailable", "sandbox transport is unavailable") from exc
    deadline = _deadline(target, timeout_seconds)
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


def run_code_binding(ctx: ToolContext, *, lease_owner: str, fence: int) -> ToolBinding:
    """Server-only binding; dispatch happens through the canonical approval/durable segment path."""
    definition = run_code_definition()
    revision = {"value": 0}

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
                workspace_revision=revision["value"],
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
        revision["value"] = int(status.get("workspace_revision") or revision["value"]) + 1
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
