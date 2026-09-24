"""Linux-only durable API and workload isolation regressions."""

import asyncio
import hashlib
import json
import os
import platform
import subprocess
import tempfile
import time
from dataclasses import replace
from pathlib import Path

import pytest
from lumen_sandbox.envelope import canonical, sign, verify
from lumen_sandbox.isolation import MAX_ARTIFACT, Workload
from lumen_sandbox.journal import Conflict, Journal
from lumen_sandbox.preflight import (
    PreflightError,
    verify_bootstrap_file,
    verify_cgroup_delegation,
    verify_firewall,
    verify_workload_network,
)
from lumen_sandbox.server import Config, create_app, scrub_bootstrap_file
from starlette.testclient import TestClient

pytestmark = pytest.mark.skipif(platform.system() != "Linux", reason="sandbox execution and contract tests require Linux namespaces/cgroups")


class FakeWorkload:
    cancel_requested = False

    def __init__(self):
        self.started = asyncio.Event()

    async def execute(self, eid, language, source, timeout):
        self.started.set()
        for _ in range(100):
            if self.cancel_requested:
                return "cancelled", {"stdout": "", "stderr": "", "artifacts": []}, []
            await asyncio.sleep(0.01)
        return "succeeded", {"stdout": source, "stderr": "", "artifacts": []}, []

    def kill(self):
        self.cancel_requested = True


def fixture_config(tmp_path):
    return Config("resource-one", "run-one", 7, "sha256:image", "policy-one", b"k" * 32,
                  tmp_path, 65534, 65534, 256 * 1024 * 1024, 16 * 1024 * 1024, 50000, 32,
                  Path("/sys/fs/cgroup"), time.time() + 600)


def auth(config, method, path, body=None, fence=1):
    payload = {"aud": "lumen-sandbox", "resource_id": config.resource_id, "run_id": config.run_id,
               "generation": config.generation, "method": method, "path": path, "fence": fence,
               "exp": time.time() + 8}
    if body is not None:
        payload.update(call_id=body["call_id"], fence=body["fence"], workspace_revision=body["workspace_revision"],
                       fingerprint=hashlib.sha256(canonical(body)).hexdigest())
    return {"Authorization": sign(payload, config.key)}


def test_envelope_roundtrip_and_expiry():
    payload = {"exp": 110.0, "aud": "lumen-sandbox", "run_id": "run-one"}
    header = sign(payload, b"k" * 32)
    assert verify(header, b"k" * 32, now=100) == payload
    with pytest.raises(ValueError):
        verify(header, b"z" * 32, now=100)
    with pytest.raises(ValueError):
        verify(header, b"k" * 32, now=110)
    with pytest.raises(ValueError):
        verify(header, b"k" * 32, now=90)

def test_bootstrap_token_scrub_unlinks_and_truncates(tmp_path):
    token_file = tmp_path / "one-shot"
    token_file.write_bytes(b"private-bootstrap-token")
    token_file.chmod(0o600)
    fd = os.open(token_file, os.O_RDONLY)
    try:
        scrub_bootstrap_file(token_file)
        assert not token_file.exists()
        assert os.read(fd, 128) == b""
    finally:
        os.close(fd)

def test_bootstrap_requires_root_owned_file_without_symlink(tmp_path):
    if os.geteuid() != 0:
        pytest.skip("bootstrap ownership verification requires Linux root")
    token = tmp_path / "token"
    token.write_bytes(b"one-shot")
    token.chmod(0o600)
    assert verify_bootstrap_file(token) == b"one-shot"
    link = tmp_path / "link"
    link.symlink_to(token)
    with pytest.raises(OSError):
        verify_bootstrap_file(link)

def test_firewall_rejects_extra_or_inverted_egress_rules(monkeypatch):
    ruleset = {"nftables": [
        {"chain": {"family": "inet", "table": "lumen_sandbox", "name": "output", "type": "filter",
                   "hook": "output", "policy": "drop"}},
        {"rule": {"expr": [
            {"match": {"op": "in", "left": {"ct": {"key": "state"}}, "right": ["established", "related"]}},
            {"accept": None},
        ]}},
    ]}
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, json.dumps(ruleset)))
    verify_firewall()  # nftables 1.1.3 serializes ct state as a list, not {"set": ...}.
    ruleset["nftables"].append({"rule": {"expr": [{"jump": {"target": "allow_all"}}]}})
    with pytest.raises(PreflightError, match="only established"):
        verify_firewall()
    ruleset["nftables"].pop()
    ruleset["nftables"][1]["rule"]["expr"][0]["match"]["op"] = "!="
    with pytest.raises(PreflightError, match="only established"):
        verify_firewall()


def request(call="call-one", fence=1, revision=0, source="hi"):
    return {"run_id": "run-one", "call_id": call, "fence": fence, "language": "python", "source": source,
            "timeout_seconds": 2, "workspace_revision": revision}


def terminal(client, config, eid, fence=1):
    path = "/v1/executions/" + eid
    for _ in range(200):
        result = client.get(path, headers=auth(config, "GET", path, fence=fence)).json()
        if result["state"] not in ("reserved", "running"):
            return result
        time.sleep(0.01)
    pytest.fail("execution did not terminate")


def test_replay_conflict_fencing_and_readiness(tmp_path):
    config = fixture_config(tmp_path)
    with TestClient(create_app(config, FakeWorkload())) as client:
        ready = client.get("/readyz").json()
        assert ready["generation"] == 7 and ready["limits"]["source_bytes"] == 65536
        assert "key" not in json.dumps(ready).lower() and "token" not in json.dumps(ready).lower()
        first = request()
        response = client.post("/v1/executions", json=first, headers=auth(config, "POST", "/v1/executions", first))
        assert response.status_code == 202
        eid = response.json()["id"]
        assert terminal(client, config, eid)["stdout"] == "hi"
        replay = client.post("/v1/executions", json=first, headers=auth(config, "POST", "/v1/executions", first))
        assert replay.status_code == 200 and replay.json()["id"] == eid and replay.json()["state"] == "succeeded"
        changed = request(source="not hi")
        assert client.post("/v1/executions", json=changed, headers=auth(config, "POST", "/v1/executions", changed)).status_code == 409
        newer = request("call-two", fence=2, revision=1)
        assert client.post("/v1/executions", json=newer, headers=auth(config, "POST", "/v1/executions", newer)).status_code == 202
        assert client.post("/v1/executions", json=first, headers=auth(config, "POST", "/v1/executions", first)).status_code == 409
        assert client.get("/v1/executions/" + eid, headers=auth(config, "GET", "/v1/executions/" + eid, fence=1)).status_code == 409


def test_cancel_idempotent(tmp_path):
    config = fixture_config(tmp_path)
    with TestClient(create_app(config, FakeWorkload())) as client:
        body = request()
        eid = client.post("/v1/executions", json=body, headers=auth(config, "POST", "/v1/executions", body)).json()["id"]
        path = "/v1/executions/" + eid
        response = client.delete(path, headers=auth(config, "DELETE", path))
        assert response.status_code == 200 and response.json()["state"] == "cancelled"
        assert client.delete(path, headers=auth(config, "DELETE", path)).json()["state"] == "cancelled"


def test_artifact_endpoint_reads_snapshot_not_workspace(tmp_path):
    config = fixture_config(tmp_path)
    aid = "frozen-artifact"
    journal = Journal(tmp_path, "run-one", "resource-one", 7)
    execution, _ = journal.reserve("artifact-call", "digest", 1, 0)
    journal.finish(execution["id"], "succeeded", {"artifacts": [{"id": aid}]}, time.time(), [(aid, "data.txt", 2)])
    frozen = tmp_path / "snapshots" / execution["id"]
    frozen.mkdir(parents=True)
    (frozen / aid).write_bytes(b"ok")
    with TestClient(create_app(config, FakeWorkload())) as client:
        path = "/v1/artifacts/" + aid
        assert client.get(path, headers=auth(config, "GET", path)).content == b"ok"
        (frozen / aid).unlink()
        (frozen / aid).symlink_to("/etc/passwd")
        assert client.get(path, headers=auth(config, "GET", path)).status_code == 409

def test_deadline_rejects_new_calls_but_preserves_replay(tmp_path):
    config = fixture_config(tmp_path)
    body = request()
    with TestClient(create_app(config, FakeWorkload())) as client:
        eid = client.post("/v1/executions", json=body,
                          headers=auth(config, "POST", "/v1/executions", body)).json()["id"]
        terminal(client, config, eid)
    expired = replace(config, run_deadline=time.time() - 1)
    with TestClient(create_app(expired, FakeWorkload())) as client:
        assert client.get("/readyz").status_code == 503
        assert client.post("/v1/executions", json=body,
                           headers=auth(expired, "POST", "/v1/executions", body)).json()["id"] == eid
        next_body = request("new", revision=1)
        response = client.post("/v1/executions", json=next_body,
                               headers=auth(expired, "POST", "/v1/executions", next_body))
        assert response.status_code == 409 and response.json()["error"] == "run_deadline_expired"

def test_journal_restart_indeterminate(tmp_path):
    first = Journal(tmp_path, "run-one", "resource-one", 7)
    status, created = first.reserve("call", "digest", 4, 0)
    assert created
    first.start(status["id"], time.time())
    first.db.close()
    second = Journal(tmp_path, "run-one", "resource-one", 7)
    assert second.reserve("call", "digest", 4, 0)[0]["state"] == "indeterminate"
    with pytest.raises(Conflict, match="indeterminate"):
        second.reserve("other", "different", 5, 0)
    with pytest.raises(Conflict, match="stale fence"):
        second.reserve("other", "different", 3, 0)


def test_journal_rejects_other_resource_generation(tmp_path):
    journal = Journal(tmp_path, "run-one", "resource-one", 7)
    journal.db.close()
    with pytest.raises(ValueError, match="another resource"):
        Journal(tmp_path, "run-one", "resource-one", 8)

def test_higher_fence_survives_active_conflict(tmp_path):
    journal = Journal(tmp_path, "run-one", "resource-one", 7)
    journal.reserve("first", "digest", 1, 0)
    with pytest.raises(Conflict, match="active"):
        journal.reserve("second", "digest-two", 2, 0)
    assert journal.highest_fence() == 2
    with pytest.raises(Conflict, match="stale fence"):
        journal.reserve("first", "digest", 1, 0)

def test_snapshot_rejects_symlink_and_bounds(tmp_path):
    workload = object.__new__(Workload)
    workload.workspace = tmp_path / "workspace"
    workload.snapshots = tmp_path / "snapshots"
    workload.workspace.mkdir()
    workload.snapshots.mkdir()
    (workload.workspace / "link").symlink_to("/etc/passwd")
    with pytest.raises(ValueError, match="symlink"):
        workload.snapshot("symlink")
    (workload.workspace / "link").unlink()
    (workload.workspace / "big").write_bytes(b"a" * (MAX_ARTIFACT + 1))
    with pytest.raises(ValueError, match="size"):
        workload.snapshot("big")
    (workload.workspace / "big").unlink()
    for n in range(21):
        (workload.workspace / str(n)).write_bytes(b"x")
    with pytest.raises(ValueError, match="count"):
        workload.snapshot("many")


def test_snapshot_copies_regular_files(tmp_path):
    workload = object.__new__(Workload)
    workload.workspace = tmp_path / "workspace"
    workload.snapshots = tmp_path / "snapshots"
    workload.workspace.mkdir()
    workload.snapshots.mkdir()
    (workload.workspace / "result.txt").write_bytes(b"frozen data")
    [(aid, filename, size)] = workload.snapshot("execution-one")
    assert (filename, size) == ("result.txt", 11)
    (workload.workspace / "result.txt").write_bytes(b"changed")
    assert (workload.snapshots / "execution-one" / aid).read_bytes() == b"frozen data"

def test_undelegated_cgroup_fails_before_mount(tmp_path):
    if os.geteuid() != 0:
        pytest.skip("workload creation requires Linux root")
    group = tmp_path / "cgroup"
    group.mkdir()
    (group / "cgroup.controllers").write_text("cpu memory pids")
    (group / "cgroup.subtree_control").write_text("")
    workspace = Path(tempfile.mkdtemp(prefix="lumen-undelegated-test-"))
    workspace.rmdir()
    try:
        with pytest.raises(PreflightError, match="not delegated"):
            verify_cgroup_delegation(group)
        with pytest.raises(PreflightError, match="not delegated"):
            Workload(root=tmp_path, workspace_root=workspace, uid=65534, gid=65534,
                     memory_bytes=256 * 1024 * 1024, workspace_bytes=16 * 1024 * 1024,
                     cpu_quota=50000, pids_limit=32, cgroup_parent=group)
        assert not workspace.exists()
        assert not any(path.name.startswith("lumen-") for path in group.iterdir())
    finally:
        if workspace.is_mount():
            subprocess.run(["/usr/bin/umount", str(workspace)], check=True)
        if workspace.exists():
            workspace.rmdir()


@pytest.fixture
def real_workload(tmp_path):
    if os.geteuid() != 0 or not Path("/usr/bin/bwrap").exists() or not Path("/sys/fs/cgroup/cgroup.controllers").exists():
        pytest.skip("requires privileged Linux namespace/bubblewrap and writable cgroup v2 host")
    group = Path("/sys/fs/cgroup")
    if not os.access(group, os.W_OK):
        pytest.skip("requires delegated writable cgroup v2")
    if not {"cpu", "memory", "pids"}.issubset(set((group / "cgroup.subtree_control").read_text().split())):
        pytest.skip("requires cpu/memory/pids delegation to child cgroups")
    workspace = Path(tempfile.mkdtemp(prefix="lumen-sandbox-test-"))
    try:
        workload = Workload(root=tmp_path, workspace_root=workspace, uid=65534, gid=65534,
                            memory_bytes=256 * 1024 * 1024, workspace_bytes=16 * 1024 * 1024,
                            cpu_quota=50000, pids_limit=32, cgroup_parent=group)
    except (OSError, PermissionError):
        workspace.rmdir()
        pytest.skip("requires CAP_SYS_ADMIN for bounded tmpfs and delegated cgroup")
    try:
        yield workload
    finally:
        workload.close()
        subprocess.run(["/usr/bin/umount", str(workspace)], check=True)
        workspace.rmdir()


def test_real_languages_and_artifact_freeze(real_workload):
    async def exercise():
        for eid, language, source in (("py", "python", "print('python-ok')"),
                                      ("js", "javascript", "console.log('node-ok')"),
                                      ("sh", "shell", "echo shell-ok")):
            state, detail, _ = await real_workload.execute(eid, language, source, 5)
            assert state == "succeeded" and detail["stdout"].strip().endswith("-ok")
        state, _, artifacts = await real_workload.execute("artifact", "python",
            "open('result.txt', 'w').write('snapshot')", 5)
        assert state == "succeeded"
        aid, name, size = next(artifact for artifact in artifacts if artifact[1] == "result.txt")
        assert (name, size) == ("result.txt", 8)
        (real_workload.workspace / "result.txt").write_text("changed")
        assert (real_workload.snapshots / "artifact" / aid).read_text() == "snapshot"
    asyncio.run(exercise())

def test_real_workload_has_no_external_network(real_workload):
    asyncio.run(verify_workload_network(real_workload, 8013))


def test_real_output_and_timeout_kill(real_workload):
    async def exercise():
        result = await real_workload.execute("output", "python", "print('x' * 70000)", 10)
        assert result[0] == "output_limit_exceeded" and len(result[1]["stdout"].encode()) == 65536
        result = await real_workload.execute("timeout", "python", "import time; time.sleep(10)", 0.1)
        assert result[0] == "timed_out"
    asyncio.run(exercise())

def test_real_group_cancel(real_workload):
    async def exercise():
        task = asyncio.create_task(real_workload.execute("cancel", "shell", "echo ready > started; sleep 30 & wait", 20))
        started = real_workload.workspace / "started"
        for _ in range(200):
            if started.exists() or task.done():
                break
            await asyncio.sleep(0.01)
        try:
            assert started.is_file(), "workload did not start before cancellation"
            assert started.read_text().strip() == "ready", "workload did not start before cancellation"
            assert real_workload.proc is not None
            real_workload.kill()
            result = await task
            assert result[0] == "cancelled"
        finally:
            if not task.done():
                real_workload.kill()
                await task
        assert "populated 0" in (real_workload.group / "cgroup.events").read_text()
    asyncio.run(exercise())

def test_real_workspace_disk_ceiling(real_workload):
    source = (
        "import errno\n"
        "try:\n"
        "    with open('flood', 'wb', buffering=0) as out:\n"
        "        while True: out.write(b'x' * 65536)\n"
        "except OSError as exc:\n"
        "    assert exc.errno == errno.ENOSPC\n"
        "    print('disk-capped')\n"
    )
    state, detail, _ = asyncio.run(real_workload.execute("disk", "python", source, 10, capture_artifacts=False))
    assert state == "succeeded" and detail["stdout"].strip() == "disk-capped"
    assert (real_workload.workspace / "flood").stat().st_size <= real_workload.workspace_bytes


def test_real_fork_pids_ceiling(real_workload):
    source = (
        "import os, time\n"
        "while True:\n"
        "    try:\n"
        "        if os.fork() == 0: time.sleep(30); os._exit(0)\n"
        "    except OSError:\n"
        "        print('pids-capped', flush=True)\n"
        "        break\n"
    )
    asyncio.run(real_workload.execute("pids", "python", source, 5, capture_artifacts=False))
    events = dict(line.split() for line in (real_workload.group / "pids.events").read_text().splitlines())
    assert int(events["max"]) > 0


def test_real_memory_ceiling(real_workload):
    source = (
        "blocks = []\n"
        "for _ in range(320):\n"
        "    block = bytearray(1024 * 1024)\n"
        "    for page in range(0, len(block), 4096): block[page] = 1\n"
        "    blocks.append(block)\n"
    )
    asyncio.run(real_workload.execute("memory", "python", source, 10, capture_artifacts=False))
    events = dict(line.split() for line in (real_workload.group / "memory.events").read_text().splitlines())
    assert int(events["oom_kill"]) > 0
