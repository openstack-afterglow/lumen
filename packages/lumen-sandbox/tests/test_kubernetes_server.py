"""Regressions for Kubernetes-native on-demand stdio MCP sandbox daemon."""

import asyncio
import base64
import hashlib
import importlib.util
import json
import logging
import os
import signal
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from lumen_sandbox.envelope import canonical, sign
from lumen_sandbox.kubernetes_server import (
    McpProcess,
    McpSubprocessManager,
    ProfileDefinition,
    SandboxConfig,
    create_kubernetes_app,
    load_manifest,
)
from lumen_sandbox.preflight import PreflightError
from starlette.testclient import TestClient


def make_manifest(tmp_path: Path) -> tuple[Path, str, dict[str, ProfileDefinition]]:
    data = {
        "version": 1,
        "profiles": [
            {
                "id": "playwright",
                "command": ["/usr/bin/node", "/srv/playwright/cli.js"],
                "tools": [
                    {
                        "name": "browser_navigate",
                        "description": "Navigate to URL",
                        "input_schema": {
                            "type": "object",
                            "properties": {"url": {"type": "string"}},
                            "required": ["url"],
                        },
                        "effect": "external_mutation",
                    },
                    {
                        "name": "browser_snapshot",
                        "description": "Take accessibility snapshot",
                        "input_schema": {"type": "object", "properties": {}},
                        "effect": "read",
                    },
                ],
                "environment": {"PLAYWRIGHT_HEADLESS": "1"},
            }
        ],
    }
    raw = json.dumps(data).encode("utf-8")
    sha = hashlib.sha256(raw).hexdigest()
    path = tmp_path / "manifest.json"
    path.write_bytes(raw)
    profiles = load_manifest(path, sha)
    return path, sha, profiles


@pytest.fixture
def fake_mcp_server(tmp_path: Path):
    _, _, profiles = make_manifest(tmp_path)
    script = tmp_path / "fake_mcp_server.py"
    requests_path = tmp_path / "requests.jsonl"
    result_path = tmp_path / "result.json"
    script.write_text('''import json
import os
import sys
import signal
import subprocess
import time

advertised = json.loads(sys.argv[1])
behavior = json.loads(sys.argv[4])
with open(sys.argv[3]) as source:
    fixed_result = json.load(source)
for line in sys.stdin:
    request = json.loads(line)
    with open(sys.argv[2], "a") as log:
        log.write(json.dumps(request) + "\\n")
    method = request.get("method")
    if method is None:
        continue
    if method == "notifications/initialized":
        continue
    if method == "initialize":
        result = {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "fake-mcp", "version": "1"},
        }
    elif method == "tools/list":
        result = {"tools": [
            {"name": name, "inputSchema": {"type": "object"}} for name in advertised
        ]}
    elif method == "tools/call":
        result = fixed_result if fixed_result is not None else {"content": [{"type": "text", "text": request["params"]["name"]}]}
    else:
        raise ValueError(method)
    fault = behavior.get(method)
    delay = behavior.get(method + ":delay", 0)
    if delay:
        time.sleep(delay)
    if fault in {"ignore_term_once", "orphan_stderr_once"}:
        with open(sys.argv[2]) as log:
            calls = sum(json.loads(row).get("method") == "tools/call" for row in log)
        if calls == 1:
            if fault == "ignore_term_once":
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
            else:
                ready_file = sys.argv[2] + ".child"
                child = "import pathlib,signal,sys,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);pathlib.Path(sys.argv[1]).touch();time.sleep(10)"
                subprocess.Popen([sys.executable, "-c", child, ready_file],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
                deadline = time.monotonic() + 2
                while not os.path.exists(ready_file):
                    assert time.monotonic() < deadline
                    time.sleep(0.001)
            time.sleep(10)
    if fault in {"notification", "mixed", "slow_notifications", "notification_flood"}:
        count = 129 if fault in {"slow_notifications", "notification_flood"} else 1
        for _ in range(count):
            print(json.dumps({"jsonrpc": "2.0", "method": "private-notification-" + "x" * 100,
                              "params": {"secret": "PRIVATE_NOTIFICATION"}}), flush=True)
            if fault == "slow_notifications":
                time.sleep(0.03)
    if fault in {"server_request", "mixed"}:
        print(json.dumps({"jsonrpc": "2.0", "id": "server-request-id", "method": "private-server-method",
                          "params": {"secret": "PRIVATE_SERVER_REQUEST"}}), flush=True)
        response = json.loads(next(sys.stdin))
        with open(sys.argv[2], "a") as log:
            log.write(json.dumps(response) + "\\n")
        assert response == {"jsonrpc": "2.0", "id": "server-request-id",
                            "error": {"code": -32601, "message": "Method not found"}}
    if fault == "stderr_flood":
        sys.stderr.write("PRIVATE_STDERR" * 200_000)
        sys.stderr.flush()
    if fault == "invalid_json":
        print("PRIVATE_INVALID_JSON", flush=True)
        continue
    if fault == "nonobject":
        print("[]", flush=True)
        continue
    if fault == "oversized":
        print("x" * (8 * 1024 * 1024 + 1), flush=True)
        continue
    if fault in {"missing_result", "both_result_error", "wrong_id", "bad_version", "nonobject_result", "rpc_error"}:
        frame = {"jsonrpc": "2.0", "id": request["id"], "result": result}
        if fault == "missing_result":
            del frame["result"]
        elif fault == "both_result_error":
            frame["error"] = {"code": -1, "message": "PRIVATE_ERROR"}
        elif fault == "wrong_id":
            frame["id"] += 1
        elif fault == "bad_version":
            frame["jsonrpc"] = "1.0"
        elif fault == "nonobject_result":
            frame["result"] = None
        elif fault == "rpc_error":
            del frame["result"]
            frame["error"] = {"code": -1, "message": "PRIVATE_ERROR"}
        print(json.dumps(frame), flush=True)
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
''')

    def make_server(advertised: list[str], result=None, behavior=None):
        result_path.write_text(json.dumps(result))
        profile = replace(
            profiles["playwright"],
            command=(sys.executable, str(script), json.dumps(advertised), str(requests_path), str(result_path),
                     json.dumps(behavior or {})),
        )
        return SimpleNamespace(profile=profile, requests_path=requests_path)

    return make_server


@pytest.mark.parametrize("result", [
    {"content": [{"type": "text", "text": "browser_snapshot"}]},
    {"content": [{"type": "text", "text": "x" * 70_000}]},
    {"content": [{"type": "image", "mimeType": "image/png", "data": base64.b64encode(b"x" * (2 * 1024**2)).decode()}]},
])
def test_mcp_process_accepts_approved_tools_and_large_result_frames(tmp_path, fake_mcp_server, result):
    server = fake_mcp_server(["browser_navigate", "browser_snapshot", "browser_unapproved"], result)

    async def exercise():
        process = McpProcess(server.profile, tmp_path / "work")
        try:
            await process.start()
            assert process.proc is not None
            assert process.proc.returncode is None
            assert process.profile.tool("browser_unapproved") is None
            observed = await process.call_tool("browser_snapshot", {}, timeout=5)
            assert observed == result
            requests = [json.loads(line) for line in server.requests_path.read_text().splitlines()]
            assert [request["method"] for request in requests] == [
                "initialize", "notifications/initialized", "tools/list", "tools/call",
            ]
            assert requests[-1]["params"]["name"] == "browser_snapshot"
        finally:
            process.kill()
            if process.proc is not None:
                await asyncio.wait_for(process.proc.wait(), timeout=5)

    asyncio.run(exercise())


@pytest.mark.parametrize("advertised", [
    ["browser_navigate", "browser_unapproved"],
    ["browser_unapproved"],
    [],
])
def test_mcp_process_missing_approved_tools_fails_and_terminates(tmp_path, fake_mcp_server, advertised):
    server = fake_mcp_server(advertised)

    async def exercise():
        process = McpProcess(server.profile, tmp_path / "work")
        try:
            with pytest.raises(RuntimeError, match="missing approved tools from MCP server") as exc_info:
                await process.start()
            for name in {"browser_navigate", "browser_snapshot"} - set(advertised):
                assert name in str(exc_info.value)
            assert process.proc is not None
            # Startup itself must terminate the child, before test cleanup can do so.
            assert await asyncio.wait_for(process.proc.wait(), timeout=5) == -signal.SIGTERM
            requests = [json.loads(line) for line in server.requests_path.read_text().splitlines()]
            assert [request["method"] for request in requests] == [
                "initialize", "notifications/initialized", "tools/list",
            ]
        finally:
            process.kill()
            if process.proc is not None:
                await asyncio.wait_for(process.proc.wait(), timeout=5)

    asyncio.run(exercise())


def test_mcp_correlates_all_exchanges_and_logs_only_metadata(tmp_path, fake_mcp_server, caplog):
    caplog.set_level(logging.INFO, logger="lumen_sandbox_kubernetes")
    methods = ("initialize", "tools/list", "tools/call")
    expected = {"content": [{"type": "text", "text": "PRIVATE_RESULT"}]}
    server = fake_mcp_server(["browser_navigate", "browser_snapshot"], expected,
                             behavior=dict.fromkeys(methods, "mixed"))

    async def exercise():
        process = McpProcess(server.profile, tmp_path / "work")
        try:
            assert await process.call_tool("browser_navigate", {"url": "PRIVATE_ARGUMENT"}, timeout=5) == expected
            requests = [json.loads(line) for line in server.requests_path.read_text().splitlines()]
            answers = [row for row in requests if row.get("id") == "server-request-id"]
            assert len(answers) == 3
            assert all(row["error"]["code"] == -32601 for row in answers)
        finally:
            process.kill()
            await asyncio.wait_for(process.proc.wait(), timeout=5)
            await asyncio.wait_for(process._stderr_task, timeout=5)

    asyncio.run(exercise())
    logs = [record.getMessage() for record in caplog.records if record.name == "lumen_sandbox_kubernetes"]
    assert len(logs) == 3
    for identity, (method, message) in enumerate(zip(methods, logs, strict=True), 1):
        assert f"profile=playwright method={method} request_id={identity}" in message
        assert "notifications=1 server_requests=1 outcome=matched elapsed_ms=" in message
    assert "PRIVATE_" not in caplog.text
    assert "private-notification" not in caplog.text
    assert "private-server-method" not in caplog.text
    assert "browser_navigate" not in caplog.text


@pytest.mark.parametrize("method", ["initialize", "tools/list", "tools/call"])
@pytest.mark.parametrize("fault", [
    "wrong_id", "bad_version", "invalid_json", "nonobject", "missing_result", "both_result_error", "nonobject_result",
])
def test_mcp_invalid_response_kills_subprocess(tmp_path, fake_mcp_server, caplog, method, fault):
    caplog.set_level(logging.INFO, logger="lumen_sandbox_kubernetes")
    server = fake_mcp_server(["browser_navigate", "browser_snapshot"], behavior={method: fault})

    async def exercise():
        process = McpProcess(server.profile, tmp_path / "work")
        try:
            with pytest.raises(RuntimeError, match="MCP"):
                await process.call_tool("browser_navigate", {"secret": "PRIVATE_ARGUMENT"}, timeout=5)
            assert await asyncio.wait_for(process.proc.wait(), timeout=5) == -signal.SIGTERM
            await asyncio.wait_for(process._stderr_task, timeout=5)
        finally:
            process.kill()
            await asyncio.wait_for(process.proc.wait(), timeout=5)

    asyncio.run(exercise())
    logs = [record.getMessage() for record in caplog.records if record.name == "lumen_sandbox_kubernetes"]
    assert len(logs) == ("initialize", "tools/list", "tools/call").index(method) + 1
    assert "outcome=protocol_error" in logs[-1]
    assert "PRIVATE_" not in caplog.text


@pytest.mark.parametrize("fault,error", [
    ("slow_notifications", TimeoutError), ("notification_flood", RuntimeError), ("oversized", ValueError),
])
def test_mcp_bounds_whole_exchange_and_skipped_frames(tmp_path, fake_mcp_server, caplog, fault, error):
    caplog.set_level(logging.INFO, logger="lumen_sandbox_kubernetes")
    server = fake_mcp_server(["browser_navigate", "browser_snapshot"], behavior={"tools/call": fault})

    async def exercise():
        process = McpProcess(server.profile, tmp_path / "work")
        try:
            await process.start()
            started = time.monotonic()
            with pytest.raises(error):
                await process.call_tool("browser_navigate", {}, timeout=0.12 if fault == "slow_notifications" else 5)
            if fault == "slow_notifications":
                assert time.monotonic() - started < 1
            assert await asyncio.wait_for(process.proc.wait(), timeout=5) == -signal.SIGTERM
            await asyncio.wait_for(process._stderr_task, timeout=5)
        finally:
            process.kill()
            await asyncio.wait_for(process.proc.wait(), timeout=5)

    asyncio.run(exercise())
    logs = [record.getMessage() for record in caplog.records if record.name == "lumen_sandbox_kubernetes"]
    assert len(logs) == 3
    assert f"outcome={'timeout' if fault == 'slow_notifications' else 'protocol_error'}" in logs[-1]
    assert "PRIVATE_" not in caplog.text


def test_mcp_drains_stderr_without_blocking_stdout_or_logging_contents(tmp_path, fake_mcp_server, caplog):
    caplog.set_level(logging.INFO, logger="lumen_sandbox_kubernetes")
    server = fake_mcp_server(["browser_navigate", "browser_snapshot"], behavior={"initialize": "stderr_flood"})

    async def exercise():
        process = McpProcess(server.profile, tmp_path / "work")
        try:
            result = await process.call_tool("browser_snapshot", {}, timeout=5)
            assert result["content"][0]["text"] == "browser_snapshot"
        finally:
            process.kill()
            await asyncio.wait_for(process.proc.wait(), timeout=5)
            await asyncio.wait_for(process._stderr_task, timeout=5)

    asyncio.run(exercise())
    assert "PRIVATE_STDERR" not in caplog.text


def test_mcp_matching_error_is_not_empty_success(tmp_path, fake_mcp_server, caplog):
    caplog.set_level(logging.INFO, logger="lumen_sandbox_kubernetes")
    server = fake_mcp_server(["browser_navigate", "browser_snapshot"], behavior={"tools/call": "rpc_error"})

    async def exercise():
        process = McpProcess(server.profile, tmp_path / "work")
        try:
            result = await process.call_tool("browser_navigate", {}, timeout=5)
            assert result["isError"] is True
            assert "PRIVATE_ERROR" in result["content"][0]["text"]
        finally:
            process.kill()
            await asyncio.wait_for(process.proc.wait(), timeout=5)
            await asyncio.wait_for(process._stderr_task, timeout=5)

    asyncio.run(exercise())
    assert "PRIVATE_ERROR" not in caplog.text


@pytest.mark.parametrize("startup,init_delay,list_delay,succeeds", [
    (0.6, 0.15, 0, True),  # Scaled 60-second startup accepts initialization slower than the former 10-second bound.
    (0.12, 0.25, 0, False),
    (0.18, 0.11, 0.11, False),  # One startup deadline, not a fresh budget for tools/list.
])
def test_mcp_single_startup_deadline(tmp_path, fake_mcp_server, monkeypatch, startup, init_delay, list_delay, succeeds):
    from lumen_sandbox import kubernetes_server

    monkeypatch.setattr(kubernetes_server, "_MCP_STARTUP_TIMEOUT_SECONDS", startup)
    server = fake_mcp_server(["browser_navigate", "browser_snapshot"], behavior={
        "initialize:delay": init_delay, "tools/list:delay": list_delay,
    })

    async def exercise():
        process = McpProcess(server.profile, tmp_path / "work")
        try:
            if succeeds:
                await process.start()
                assert (await process.call_tool("browser_navigate", {}, timeout=0.1))["content"]
            else:
                with pytest.raises(TimeoutError):
                    await process.start()
                assert await asyncio.wait_for(process.proc.wait(), timeout=5) == -signal.SIGTERM
        finally:
            process.kill()
            await asyncio.wait_for(process.proc.wait(), timeout=5)
            await asyncio.wait_for(process._stderr_task, timeout=5)

    asyncio.run(exercise())


@pytest.mark.parametrize("warm", [False, True])
def test_mcp_startup_never_extends_tool_budget(tmp_path, fake_mcp_server, monkeypatch, warm):
    from lumen_sandbox import kubernetes_server

    monkeypatch.setattr(kubernetes_server, "_MCP_STARTUP_TIMEOUT_SECONDS", 0.6)
    server = fake_mcp_server(["browser_navigate", "browser_snapshot"], behavior={
        "initialize:delay": 0.15, "tools/call:delay": 0.15,
    })

    async def exercise():
        process = McpProcess(server.profile, tmp_path / "work")
        try:
            if warm:
                await process.start()
            with pytest.raises(TimeoutError):
                await process.call_tool("browser_navigate", {}, timeout=0.08 if warm else 0.24)
            assert await asyncio.wait_for(process.proc.wait(), timeout=5) == -signal.SIGTERM
        finally:
            process.kill()
            await asyncio.wait_for(process.proc.wait(), timeout=5)
            await asyncio.wait_for(process._stderr_task, timeout=5)

    asyncio.run(exercise())


def test_mcp_timed_out_process_is_fenced_before_next_call(tmp_path, fake_mcp_server):
    server = fake_mcp_server(["browser_navigate", "browser_snapshot"], behavior={"tools/call": "ignore_term_once"})

    async def exercise():
        process = McpProcess(server.profile, tmp_path / "work")
        try:
            await process.start()
            old = process.proc
            with pytest.raises(TimeoutError):
                await process.call_tool("browser_navigate", {}, timeout=0.1)
            assert old.returncode is None  # The test subprocess deliberately ignores SIGTERM.
            result = await process.call_tool("browser_snapshot", {}, timeout=5)
            assert result["content"][0]["text"] == "browser_snapshot"
            assert process.proc.pid != old.pid
            assert old.returncode == -signal.SIGKILL
        finally:
            process.kill()
            await asyncio.wait_for(process.proc.wait(), timeout=5)
            await asyncio.wait_for(process._stderr_task, timeout=5)

    asyncio.run(exercise())


def test_manifest_validation(tmp_path: Path):
    path, sha, profiles = make_manifest(tmp_path)
    assert "playwright" in profiles
    prof = profiles["playwright"]
    assert prof.command == ("/usr/bin/node", "/srv/playwright/cli.js")
    assert len(prof.tools) == 2
    assert prof.tool("browser_navigate") is not None
    assert prof.tool("browser_navigate").effect == "external_mutation"

    # Hash mismatch
    with pytest.raises(PreflightError, match="manifest sha256 mismatch"):
        load_manifest(path, "0" * 64)

    # Relative binary rejected
    bad_data = {
        "version": 1,
        "profiles": [{"id": "bad", "command": ["node", "app.js"], "tools": [{"name": "t1", "input_schema": {}}]}],
    }
    bad_raw = json.dumps(bad_data).encode("utf-8")
    bad_path = tmp_path / "bad.json"
    bad_path.write_bytes(bad_raw)
    with pytest.raises(PreflightError, match="command binary must be absolute"):
        load_manifest(bad_path, hashlib.sha256(bad_raw).hexdigest())


def test_baked_manifest_has_markitdown_with_exact_approved_effect():
    path = Path(__file__).resolve().parents[1] / "tools/manifest.json"
    sha = "3055fcb865df3696a055019da9492f2dff00e4e1c072b64c5555b7100409992c"
    profiles = load_manifest(path, sha)
    assert list(profiles) == ["playwright", "filesystem", "markitdown"]
    profile = profiles["markitdown"]
    assert profile.command == (
        "/opt/lumen-tools/markitdown/bin/python", "-I", "/opt/lumen-tools/markitdown_server.py",
    )
    assert len(profile.tools) == 1
    tool = profile.tool("convert_to_markdown")
    assert tool is not None
    assert tool.effect == "external_mutation"
    assert tool.input_schema == {
        "properties": {"uri": {"title": "Uri", "type": "string"}},
        "required": ["uri"], "type": "object", "title": "convert_to_markdownArguments",
    }


@pytest.mark.parametrize("fault", ["duplicate_profile", "cross_profile_name", "effect", "effect_type"])
def test_markitdown_manifest_rejects_ambiguous_names_and_invalid_effect(tmp_path, fault):
    source = Path(__file__).resolve().parents[1] / "tools/manifest.json"
    data = json.loads(source.read_bytes())
    profile = data["profiles"][2]
    if fault == "duplicate_profile":
        profile["id"] = "playwright"
    elif fault == "cross_profile_name":
        profile["tools"][0]["name"] = data["profiles"][0]["tools"][0]["name"]
    else:
        profile["tools"][0]["effect"] = "egress" if fault == "effect" else {}
    raw = json.dumps(data).encode()
    path = tmp_path / "manifest.json"
    path.write_bytes(raw)
    with pytest.raises(PreflightError, match="duplicate|invalid tool effect"):
        load_manifest(path, hashlib.sha256(raw).hexdigest())


@pytest.fixture
def markitdown_launcher(tmp_path, monkeypatch):
    path = Path(__file__).resolve().parents[1] / "tools/markitdown_server.py"
    spec = importlib.util.spec_from_file_location("markitdown_sandbox_launcher", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    workspace = tmp_path / "work"
    workspace.mkdir()
    monkeypatch.setattr(module, "WORKSPACE", str(workspace))
    return module, workspace


def test_markitdown_local_read_uses_open_workspace_descriptor(markitdown_launcher):
    launcher, workspace = markitdown_launcher
    path = workspace / "document with spaces.html"
    path.write_bytes(b"<h1>Local document</h1>")
    with launcher.workspace_file(path.as_uri()) as (stream, extension):
        assert stream.read() == b"<h1>Local document</h1>"
        assert extension == ".html"
    assert stream.closed


@pytest.mark.parametrize("fault", [
    "outside", "encoded_traversal", "file_symlink", "directory_symlink", "workspace_symlink",
    "directory", "fifo", "authority", "query", "fragment", "non_file",
])
def test_markitdown_local_read_cannot_escape_workspace(markitdown_launcher, tmp_path, fault):
    launcher, workspace = markitdown_launcher
    secret = tmp_path / "secret.html"
    secret.write_text("outside workspace")
    uri = (workspace / "document.html").as_uri()
    if fault == "outside":
        uri = secret.as_uri()
    elif fault == "encoded_traversal":
        uri = workspace.as_uri() + "/%2e%2e/secret.html"
    elif fault == "file_symlink":
        link = workspace / "document.html"
        link.symlink_to(secret)
    elif fault == "directory_symlink":
        (workspace / "linked").symlink_to(tmp_path, target_is_directory=True)
        uri = (workspace / "linked/secret.html").as_uri()
    elif fault == "workspace_symlink":
        workspace.rmdir()
        workspace.symlink_to(tmp_path, target_is_directory=True)
    elif fault == "directory":
        uri = workspace.as_uri()
    elif fault == "fifo":
        os.mkfifo(workspace / "document.html")
    elif fault == "authority":
        uri = "file://localhost" + str(workspace / "document.html")
    elif fault == "query":
        uri += "?path=/etc/passwd"
    elif fault == "fragment":
        uri += "#/etc/passwd"
    else:
        uri = "https://example.com"
    with pytest.raises((ValueError, OSError)), launcher.workspace_file(uri):
        pytest.fail("escaped local file must never be opened")


def auth_header(config: SandboxConfig, method: str, path: str, body: dict | None = None, fence: int = 1) -> str:
    payload = {
        "aud": "lumen-sandbox",
        "resource_id": config.resource_id,
        "run_id": config.run_id,
        "generation": config.generation,
        "method": method,
        "path": path,
        "fence": fence,
        "exp": time.time() + 10,
    }
    if body is not None:
        payload.update(
            call_id=body["call_id"],
            fence=body["fence"],
            workspace_revision=body["workspace_revision"],
            fingerprint=hashlib.sha256(canonical(body)).hexdigest(),
        )
    return sign(payload, config.key)


@pytest.fixture
def sandbox_fixture(tmp_path: Path):
    _, sha, profiles = make_manifest(tmp_path)
    state_dir = tmp_path / "state"
    workspace_dir = tmp_path / "work"
    state_dir.mkdir(parents=True)
    workspace_dir.mkdir(parents=True)
    key = b"k" * 32
    config = SandboxConfig(
        resource_id="res-123",
        run_id="run-456",
        generation=1,
        image_id="img-789",
        policy_id="pol-012",
        key=key,
        state_dir=state_dir,
        workspace_dir=workspace_dir,
        memory_bytes=1024 * 1024 * 1024,
        workspace_bytes=100 * 1024 * 1024,
        cpu_millis=2000,
        pids_limit=512,
        run_deadline=time.time() + 300,
        manifest_sha256=sha,
        profiles=profiles,
    )
    app = create_kubernetes_app(config)
    client = TestClient(app)
    return SimpleNamespace(config=config, client=client, state_dir=state_dir, workspace_dir=workspace_dir)


def test_readyz_reports_manifest_hash_and_limits(sandbox_fixture):
    f = sandbox_fixture
    resp = f.client.get("/readyz")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ready"
    assert data["manifest_sha256"] == f.config.manifest_sha256
    assert data["enforced_limits"]["pids_limit"] == 512
    assert data["enforced_limits"]["cpu_millis"] == 2000


def test_readyz_expires_after_deadline(sandbox_fixture):
    f = sandbox_fixture
    # Replace run_deadline with expired timestamp
    expired_config = SandboxConfig(
        resource_id=f.config.resource_id,
        run_id=f.config.run_id,
        generation=f.config.generation,
        image_id=f.config.image_id,
        policy_id=f.config.policy_id,
        key=f.config.key,
        state_dir=f.config.state_dir,
        workspace_dir=f.config.workspace_dir,
        memory_bytes=f.config.memory_bytes,
        workspace_bytes=f.config.workspace_bytes,
        cpu_millis=f.config.cpu_millis,
        pids_limit=f.config.pids_limit,
        run_deadline=time.time() - 1.0,
        manifest_sha256=f.config.manifest_sha256,
        profiles=f.config.profiles,
    )
    expired_client = TestClient(create_kubernetes_app(expired_config))
    resp = expired_client.get("/readyz")
    assert resp.status_code == 503
    assert resp.json()["status"] == "expired"


def test_mcp_execution_requires_valid_capability(sandbox_fixture):
    f = sandbox_fixture
    body = {
        "run_id": f.config.run_id,
        "call_id": "call-1",
        "fence": 1,
        "profile": "playwright",
        "tool": "browser_navigate",
        "arguments": {"url": "https://example.com"},
        "timeout_seconds": 30,
        "workspace_revision": 0,
    }

    # Missing auth
    resp = f.client.post("/v1/mcp/executions", json=body)
    assert resp.status_code == 403

    # Wrong key
    bad_header = sign(
        {
            "aud": "lumen-sandbox",
            "resource_id": f.config.resource_id,
            "run_id": f.config.run_id,
            "generation": f.config.generation,
            "method": "POST",
            "path": "/v1/mcp/executions",
            "fence": 1,
            "exp": time.time() + 10,
        },
        b"wrong-key" * 4,
    )
    resp = f.client.post("/v1/mcp/executions", json=body, headers={"Authorization": bad_header})
    assert resp.status_code == 403

    # Unapproved tool rejected
    bad_body = dict(body, tool="browser_unapproved")
    good_header_bad_tool = auth_header(f.config, "POST", "/v1/mcp/executions", bad_body)
    resp = f.client.post("/v1/mcp/executions", json=bad_body, headers={"Authorization": good_header_bad_tool})
    assert resp.status_code == 400
    assert resp.json()["error"] == "unapproved_tool"


def test_advertised_extra_rejected_by_http_without_dispatch(sandbox_fixture, fake_mcp_server, monkeypatch):
    f = sandbox_fixture
    server = fake_mcp_server(["browser_navigate", "browser_snapshot", "browser_unapproved"])
    f.config.profiles["playwright"] = server.profile
    dispatch = AsyncMock(side_effect=AssertionError("unapproved tool dispatched"))
    monkeypatch.setattr(McpSubprocessManager, "call", dispatch)
    body = {
        "run_id": f.config.run_id,
        "call_id": "call-extra",
        "fence": 1,
        "profile": "playwright",
        "tool": "browser_unapproved",
        "arguments": {},
        "timeout_seconds": 30,
        "workspace_revision": 0,
    }

    async def exercise():
        process = McpProcess(server.profile, f.workspace_dir)
        try:
            await process.start()
            assert process.proc is not None
            assert process.proc.returncode is None
            header = auth_header(f.config, "POST", "/v1/mcp/executions", body)
            resp = f.client.post("/v1/mcp/executions", json=body, headers={"Authorization": header})
            assert resp.status_code == 400
            assert resp.json() == {"error": "unapproved_tool"}
            dispatch.assert_not_called()
            requests = [json.loads(line) for line in server.requests_path.read_text().splitlines()]
            assert [request["method"] for request in requests] == [
                "initialize", "notifications/initialized", "tools/list",
            ]
        finally:
            process.kill()
            if process.proc is not None:
                await asyncio.wait_for(process.proc.wait(), timeout=5)

    asyncio.run(exercise())


def test_mcp_execution_replay_and_polling(sandbox_fixture, monkeypatch):
    f = sandbox_fixture
    body = {
        "run_id": f.config.run_id,
        "call_id": "call-2",
        "fence": 1,
        "profile": "playwright",
        "tool": "browser_navigate",
        "arguments": {"url": "https://example.com"},
        "timeout_seconds": 30,
        "workspace_revision": 0,
    }

    header = auth_header(f.config, "POST", "/v1/mcp/executions", body)

    # Mock subprocess call
    mock_result = {"content": [{"type": "text", "text": "navigated to example.com"}]}

    async def fake_call(_self, profile_id, tool_name, args, timeout):
        return mock_result

    from lumen_sandbox import kubernetes_server

    monkeypatch.setattr(kubernetes_server.McpSubprocessManager, "call", fake_call)

    # First execution -> 202 Accepted
    resp = f.client.post("/v1/mcp/executions", json=body, headers={"Authorization": header})
    assert resp.status_code == 202
    data = resp.json()
    eid = data["id"]
    assert data["call_id"] == "call-2"

    # Poll status
    get_hdr = auth_header(f.config, "GET", f"/v1/executions/{eid}")
    time.sleep(0.05)  # wait for task to complete in-memory
    get_resp = f.client.get(f"/v1/executions/{eid}", headers={"Authorization": get_hdr})
    assert get_resp.status_code == 200
    status_data = get_resp.json()
    assert status_data["state"] == "succeeded"
    assert status_data["mcp_result"] == mock_result

    # Replay with same call_id and fingerprint -> 200 OK with stored result
    replay_resp = f.client.post("/v1/mcp/executions", json=body, headers={"Authorization": header})
    assert replay_resp.status_code == 200
    assert replay_resp.json()["id"] == eid
    assert replay_resp.json()["mcp_result"] == mock_result


def test_mcp_restart_with_inherited_stderr_keeps_http_journal_terminal(sandbox_fixture, fake_mcp_server, monkeypatch):
    f = sandbox_fixture
    server = fake_mcp_server(["browser_navigate", "browser_snapshot"], behavior={"tools/call": "orphan_stderr_once"})
    f.config.profiles["playwright"] = server.profile
    processes = []
    original_start = McpProcess.start

    async def track_start(process):
        await original_start(process)
        processes.append((process.proc, process._stderr_task))

    monkeypatch.setattr(McpProcess, "start", track_start)

    def execute(client, call_id, timeout, revision):
        body = {"run_id": f.config.run_id, "call_id": call_id, "fence": 1,
                "profile": "playwright", "tool": "browser_navigate", "arguments": {},
                "timeout_seconds": timeout, "workspace_revision": revision}
        response = client.post("/v1/mcp/executions", json=body, headers={
            "Authorization": auth_header(f.config, "POST", "/v1/mcp/executions", body),
        })
        assert response.status_code == 202
        path = "/v1/executions/" + response.json()["id"]
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            result = client.get(path, headers={"Authorization": auth_header(f.config, "GET", path)})
            assert result.status_code == 200
            if result.json()["state"] not in {"reserved", "running"}:
                return result.json()
            time.sleep(0.01)
        pytest.fail("MCP execution journal remained nonterminal")

    try:
        with TestClient(create_kubernetes_app(f.config)) as client:
            assert execute(client, "orphan-first", 1, 0)["state"] == "timeout"
            old, drain = processes[0]
            deadline = time.monotonic() + 1
            while old.returncode is None and time.monotonic() < deadline:
                time.sleep(0.01)
            assert old.returncode == -signal.SIGTERM
            assert not drain.done()  # A SIGTERM-ignoring descendant still holds the inherited stderr pipe.
            assert execute(client, "fresh-second", 5, 1)["state"] == "succeeded"
            assert len(processes) == 2 and processes[1][0].pid != old.pid
            assert execute(client, "fresh-third", 5, 2)["state"] == "succeeded"
            assert len(processes) == 2
    finally:
        for process, _ in processes:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):  # macOS reports EPERM for an all-zombie group.
                pass


def test_workspace_revision_survives_tools_and_daemon_resume(sandbox_fixture, fake_mcp_server):
    f = sandbox_fixture
    server = fake_mcp_server(["browser_navigate", "browser_snapshot"])
    f.config.profiles["playwright"] = server.profile

    def revision(client, fence=1):
        response = client.get("/v1/workspace", headers={
            "Authorization": auth_header(f.config, "GET", "/v1/workspace", fence=fence),
        })
        assert response.status_code == 200
        return response.json()["workspace_revision"]

    def execute(client, call_id, tool, fence=1):
        body = {"run_id": f.config.run_id, "call_id": call_id, "fence": fence,
                "profile": "playwright", "tool": tool, "arguments": {}, "timeout_seconds": 10,
                "workspace_revision": revision(client, fence)}
        response = client.post("/v1/mcp/executions", json=body, headers={
            "Authorization": auth_header(f.config, "POST", "/v1/mcp/executions", body),
        })
        assert response.status_code == 202
        path = "/v1/executions/" + response.json()["id"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            response = client.get(path, headers={"Authorization": auth_header(f.config, "GET", path, fence=fence)})
            assert response.status_code == 200
            if response.json()["state"] == "succeeded":
                return
            time.sleep(0.01)
        pytest.fail("stdio tool execution did not complete")

    with TestClient(create_kubernetes_app(f.config)) as client:
        assert client.get("/v1/workspace").status_code == 403
        wrong_run = replace(f.config, run_id="other-run")
        assert client.get("/v1/workspace", headers={
            "Authorization": auth_header(wrong_run, "GET", "/v1/workspace"),
        }).status_code == 403
        assert revision(client) == 0
        execute(client, "navigate", "browser_navigate")
        assert revision(client) == 1
        execute(client, "snapshot", "browser_snapshot")
        assert revision(client) == 2

    with TestClient(create_kubernetes_app(f.config)) as resumed:
        assert revision(resumed, fence=2) == 2
        execute(resumed, "resumed-snapshot", "browser_snapshot", fence=2)
        assert revision(resumed, fence=2) == 3
        assert resumed.get("/v1/workspace", headers={
            "Authorization": auth_header(f.config, "GET", "/v1/workspace", fence=1),
        }).status_code == 409
    calls = [json.loads(line) for line in server.requests_path.read_text().splitlines()]
    assert [call["params"]["name"] for call in calls if call["method"] == "tools/call"] == [
        "browser_navigate", "browser_snapshot", "browser_snapshot",
    ]


@pytest.mark.parametrize("layout", ["unified", "gvisor"])
@pytest.mark.parametrize(("control", "replacement"), [
    ("memory", None),
    ("memory", "max"),
    ("memory", str(1024**3 + 1)),
    ("pids", "129"),
    ("pids", "invalid"),
    ("status", None),
    ("status", "CapEff:\t1\nNoNewPrivs:\t1\n"),
    ("status", "CapEff:\t0\nNoNewPrivs:\t0\n"),
    ("token", "ambient-token"),
    ("nproc", "unavailable"),
])
def test_pod_preflight_rejects_unbounded_isolation(tmp_path, monkeypatch, layout, control, replacement):
    from lumen_sandbox import kubernetes_server

    paths = {
        "status": "proc/self/status",
        "memory": "sys/fs/cgroup/memory.max" if layout == "unified" else "sys/fs/cgroup/memory/memory.limit_in_bytes",
        "pids": "sys/fs/cgroup/pids.max" if layout == "unified" else "sys/fs/cgroup/pids/pids.max",
        "token": "var/run/secrets/kubernetes.io/serviceaccount/token",
    }
    for name, value in (("status", "CapEff:\t0\nNoNewPrivs:\t1\n"),
                        ("memory", str(1024**3)), ("pids", "128" if layout == "unified" else "max")):
        target = tmp_path / paths[name]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(value)
    monkeypatch.setattr(kubernetes_server, "Path", lambda *parts: tmp_path / str(Path(*parts)).lstrip("/"))
    monkeypatch.setattr(kubernetes_server.platform, "system", lambda: "Linux")
    monkeypatch.setattr(kubernetes_server.os, "geteuid", lambda: 4300)
    monkeypatch.setattr(kubernetes_server.os, "getegid", lambda: 4300)
    infinity = kubernetes_server.resource.RLIM_INFINITY
    monkeypatch.setattr(kubernetes_server.resource, "getrlimit", lambda _: (infinity, infinity))
    monkeypatch.setattr(kubernetes_server.resource, "setrlimit", lambda *_: None)
    limits = {"memory_bytes": 1024**3, "workspace_bytes": 128 * 1024**2, "pids_limit": 128}
    kubernetes_server.verify_pod_preflight(**limits)
    if control == "nproc":
        def unavailable(*_):
            raise OSError("kernel process limit unavailable")
        monkeypatch.setattr(kubernetes_server.resource, "setrlimit", unavailable)
    else:
        target = tmp_path / paths[control]
        if replacement is None:
            target.unlink()
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(replacement)
    with pytest.raises(PreflightError):
        kubernetes_server.verify_pod_preflight(**limits)
