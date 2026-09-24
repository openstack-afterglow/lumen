"""Managed sandbox failures remain safe, typed tool outcomes."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from lumen.services import sandbox_binding
from lumen.services.infrastructure.transport import InternalTransportError
from lumen.services.tools import ToolContext


@pytest.mark.asyncio
async def test_run_code_returns_safe_failure_when_worker_has_no_client_identity(monkeypatch):
    target = sandbox_binding.SandboxTarget(
        resource_id="resource", generation=1, address="10.0.0.12", port=8013,
        certificate_fingerprint="a" * 64, deadline_at=datetime.now(UTC) + timedelta(minutes=2),
    )

    async def assigned(_run_id):
        return target

    def unavailable(_config):
        raise InternalTransportError("private certificate path must not reach the model")

    monkeypatch.setattr(sandbox_binding, "assigned_sandbox", assigned)
    monkeypatch.setattr(sandbox_binding, "get_settings", lambda: SimpleNamespace(runtime_config=object()))
    monkeypatch.setattr(sandbox_binding, "InternalTransport", unavailable)
    context = ToolContext(project_id="project", user_id="owner", run_id="run", tool_call_id="call")
    result = await sandbox_binding.run_code_binding(context, lease_owner="worker#1", fence=1).execute(
        {"language": "python", "source": "print(1)", "timeout_seconds": 1}, context,
    )
    assert result.status == "failed"
    assert result.error_code == "resource_unavailable"
    assert "private certificate path" not in result.model_content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "exit_code", "expected"),
    [("succeeded", 0, "completed"), ("cancelled", -9, "failed"), ("output_limit_exceeded", -9, "failed")],
)
async def test_sandbox_wire_terminal_state_completes_poll_and_tool_result(monkeypatch, state, exit_code, expected):
    target = sandbox_binding.SandboxTarget(
        resource_id="resource", generation=1, address="10.0.0.12", port=8013,
        certificate_fingerprint="a" * 64, deadline_at=datetime.now(UTC) + timedelta(minutes=2),
    )
    calls = []

    async def assigned(_run_id):
        return target

    async def sandbox_request(_transport, _target, **request):
        calls.append((request["method"], request["path"]))
        if request["method"] == "POST":
            return b'{"id":"execution"}'
        return (
            f'{{"id":"execution","state":"{state}","exit_code":{exit_code},'
            '"stdout":"visible result","stderr":"","duration_seconds":0.2,"artifacts":[]}'
        ).encode()

    monkeypatch.setattr(sandbox_binding, "assigned_sandbox", assigned)
    monkeypatch.setattr(sandbox_binding, "_authorized_request", sandbox_request)
    monkeypatch.setattr(sandbox_binding, "InternalTransport", lambda _config: object())
    monkeypatch.setattr(sandbox_binding, "get_settings", lambda: SimpleNamespace(runtime_config=object()))
    context = ToolContext(project_id="project", user_id="owner", run_id="run", tool_call_id="call")
    result = await sandbox_binding.run_code_binding(context, lease_owner="worker#1", fence=1).execute(
        {"language": "python", "source": "print(1)", "timeout_seconds": 1}, context,
    )
    assert result.status == expected
    assert result.error_code is None if expected == "completed" else result.error_code == f"sandbox_{state}"
    assert "visible result" in result.model_content
    assert calls == [("POST", "/v1/executions"), ("GET", "/v1/executions/execution")]
