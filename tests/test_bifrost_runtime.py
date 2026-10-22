"""Private child lifecycle tests; real subprocesses and authenticated loopback HTTP."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest

from lumen.services import bifrost_runtime as runtime


@pytest.fixture
def child(tmp_path, monkeypatch):
    def configure(mode="normal", startup=3.0, shutdown=0.3):
        state = tmp_path / (mode + ".json")
        script = tmp_path / (mode + "-bifrost")
        script.write_text(f'''#!{sys.executable}
import http.server, json, os, signal, sys, time
from pathlib import Path
mode = {mode!r}
state = Path({str(state)!r})
state.write_text(json.dumps({{"pid": os.getpid(), "env": dict(os.environ)}}))
if mode == "hang":
    time.sleep(30)
    sys.exit(1)
if mode == "ignore_term":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
if mode in ("malformed_flood", "oversized_flood"):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    sys.stdout.buffer.write(b"not-json\\n" if mode == "malformed_flood" else b"x" * 65536)
    sys.stdout.buffer.flush()
    while True:
        sys.stdout.buffer.write(b"x" * 65536)
        sys.stdout.buffer.flush()
class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self):
        if self.headers.get("Authorization") != "Bearer " + os.environ["LUMEN_BIFROST_TOKEN"]:
            self.send_response(401); self.end_headers(); return
        self.send_response(200)
        self.send_header("X-Lumen-Bifrost-Contract", {runtime.CONTRACT!r})
        self.end_headers()
        self.wfile.write(json.dumps({{
            "contract": {runtime.CONTRACT!r},
            "version": "wrong" if mode == "bad_probe" else {runtime.BIFROST_VERSION!r},
            "supported_protocols": ["chat", "responses", "messages", "count_tokens", "embeddings"],
        }}).encode())
server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
print(json.dumps({{"ready": True,
    "address": ("0.0.0.0" if mode == "public" else "127.0.0.1") + ":" + str(server.server_port),
    "contract": "wrong" if mode == "bad_contract" else {runtime.CONTRACT!r},
    "version": {runtime.BIFROST_VERSION!r}}}), flush=True)
server.serve_forever()
''')
        script.chmod(0o700)
        monkeypatch.setattr(runtime, "get_settings", lambda: SimpleNamespace(
            bifrost_executable=str(script), bifrost_startup_seconds=startup,
            bifrost_shutdown_seconds=shutdown,
        ))
        return state
    return configure


@pytest.mark.asyncio
async def test_children_use_distinct_private_ports_tokens_and_minimal_environment(child, monkeypatch):
    state = child()
    for name in ("DATABASE_URL", "OPENAI_API_KEY", "OS_PASSWORD", "AWS_SECRET_ACCESS_KEY", "HOME"):
        monkeypatch.setenv(name, "NEVER-INHERIT")
    # A service proxy must not intercept the authenticated local readiness proof.
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    first, second = runtime._ManagedRuntime(), runtime._ManagedRuntime()
    try:
        (address_a, token_a), (address_b, token_b) = await asyncio.gather(first.get(), second.get())
        assert address_a != address_b
        assert address_a.startswith("http://127.0.0.1:")
        assert token_a != token_b
        env = json.loads(state.read_text())["env"]
        assert not any(value == "NEVER-INHERIT" for value in env.values())
        assert env["LUMEN_BIFROST_PARENT_PID"] == str(os.getpid())
        assert "HTTP_PROXY" not in env
        async with httpx.AsyncClient(trust_env=False) as client:
            assert (await client.get(address_a + "/ready")).status_code == 401
            assert (await client.get(address_a + "/ready", headers={
                "Authorization": "Bearer " + token_a,
            })).status_code == 200
        assert await first.get() == (address_a, token_a)
    finally:
        await asyncio.gather(first.close(), second.close())
    assert first.process.returncode is not None and second.process.returncode is not None
    with pytest.raises(runtime.BifrostRuntimeError):
        await first.get()


@pytest.mark.asyncio
async def test_crashed_child_is_not_replaced_or_replayed(child):
    child()
    managed = runtime._ManagedRuntime()
    try:
        await managed.get()
        original = managed.process
        original.kill()
        await original.wait()
        for _ in range(2):
            with pytest.raises(runtime.BifrostRuntimeError):
                await managed.get()
        assert managed.process is original
    finally:
        await managed.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["public", "bad_contract", "bad_probe", "hang"])
async def test_failed_startup_is_bounded_reaped_and_never_retried(child, mode):
    child(mode, startup=0.5 if mode == "hang" else 3.0)
    managed = runtime._ManagedRuntime()
    with pytest.raises(runtime.BifrostRuntimeError, match="contract proof failed"):
        await managed.get()
    assert managed.process is not None and managed.process.returncode is not None
    original = managed.process
    with pytest.raises(runtime.BifrostRuntimeError):
        await managed.get()
    assert managed.process is original
    await managed.close()


@pytest.mark.asyncio
async def test_shutdown_kills_uncooperative_child_after_grace_period(child):
    child("ignore_term", shutdown=0.05)
    managed = runtime._ManagedRuntime()
    await managed.get()
    await asyncio.wait_for(managed.close(), timeout=2)
    assert managed.process.returncode == -9


@pytest.mark.asyncio
async def test_cancelled_startup_reaps_child(child):
    state = child("hang")
    managed = runtime._ManagedRuntime()
    task = asyncio.create_task(managed.get())
    for _ in range(200):
        if state.exists():
            break
        await asyncio.sleep(0.01)
    assert state.exists()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert managed.failed and managed.process.returncode is not None
    await managed.close()


@pytest.mark.asyncio
async def test_concurrent_getters_share_one_process_and_new_lifecycle_restarts_only_after_close(child, monkeypatch):
    child()
    monkeypatch.setattr(runtime, "_runtime", None)
    try:
        first, second = await asyncio.gather(runtime.get_bifrost_runtime(), runtime.get_bifrost_runtime())
        assert first == second
        original = runtime._runtime.process
        await runtime.close_bifrost_runtime()
        with pytest.raises(runtime.BifrostRuntimeError):
            await runtime.get_bifrost_runtime()
        replacement = await runtime.start_bifrost_runtime()
        assert runtime._runtime.process is not original
        assert replacement[1] != first[1]
    finally:
        await runtime.close_bifrost_runtime()


@pytest.mark.asyncio
async def test_missing_binary_is_sanitized_and_fail_closed(monkeypatch):
    monkeypatch.setattr(runtime, "get_settings", lambda: SimpleNamespace(
        bifrost_executable="/nonexistent/private-BIFROST", bifrost_shutdown_seconds=0.1,
    ))
    managed = runtime._ManagedRuntime()
    with pytest.raises(runtime.BifrostRuntimeError, match="executable unavailable") as error:
        await managed.get()
    assert "/nonexistent" not in str(error.value)
    assert managed.process is None and managed.failed
    with pytest.raises(runtime.BifrostRuntimeError):
        await managed.get()
    await managed.close()


@pytest.mark.asyncio
async def test_api_stops_provider_child_after_service_cleanup(monkeypatch):
    from lumen import main

    events = []

    @asynccontextmanager
    async def service(app):
        events.append("start")
        try:
            yield
        finally:
            events.append("streams-and-service-drained")

    async def close():
        events.append("child-close")

    monkeypatch.setattr(main, "_service_lifespan", service)
    monkeypatch.setattr(runtime, "close_bifrost_runtime", close)
    async with main.lifespan(main.app):
        events.append("request")
    assert events == ["start", "request", "streams-and-service-drained", "child-close"]


@pytest.mark.asyncio
async def test_worker_stops_provider_child_after_run_and_auxiliary_cleanup(monkeypatch):
    from lumen import worker

    events = []

    async def serve():
        events.extend(["run-drained", "auxiliary-drained", "plugins-closed"])

    async def close():
        events.append("child-close")

    monkeypatch.setattr(worker, "_serve", serve)
    monkeypatch.setattr(runtime, "close_bifrost_runtime", close)
    await worker.serve()
    assert events == ["run-drained", "auxiliary-drained", "plugins-closed", "child-close"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["malformed_flood", "oversized_flood"])
async def test_failed_readiness_drains_flooding_stdout_before_bounded_reap(child, mode):
    child(mode, shutdown=0.05)
    managed = runtime._ManagedRuntime()
    try:
        with pytest.raises(runtime.BifrostRuntimeError, match="contract proof failed"):
            await asyncio.wait_for(managed.get(), timeout=3.0)
        assert managed.failed
        assert managed.process.returncode == -9
        assert managed.stdout_task.done()
    finally:
        await asyncio.wait_for(managed.close(), timeout=3.0)
