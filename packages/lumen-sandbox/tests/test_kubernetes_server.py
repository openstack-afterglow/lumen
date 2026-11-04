"""Regressions for Kubernetes-native on-demand stdio MCP sandbox daemon."""

import asyncio
import base64
import hashlib
import json
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
import sys

advertised = json.loads(sys.argv[1])
with open(sys.argv[3]) as source:
    fixed_result = json.load(source)
for line in sys.stdin:
    request = json.loads(line)
    with open(sys.argv[2], "a") as log:
        log.write(json.dumps(request) + "\\n")
    method = request["method"]
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
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
''')

    def make_server(advertised: list[str], result=None):
        result_path.write_text(json.dumps(result))
        profile = replace(
            profiles["playwright"],
            command=(sys.executable, str(script), json.dumps(advertised), str(requests_path), str(result_path)),
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
