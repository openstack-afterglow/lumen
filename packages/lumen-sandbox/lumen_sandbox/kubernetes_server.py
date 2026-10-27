"""Kubernetes-native on-demand stdio MCP tool execution daemon.

Non-root, restricted Pod security profile; manages approved stdio MCP subprocesses
per run, records reservations in SQLite journal, and enforces finite resource limits.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import platform
import re
import resource
import signal
import ssl
import stat
import time
import urllib.request
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from lumen_sandbox.envelope import canonical, verify
from lumen_sandbox.journal import Conflict, Journal
from lumen_sandbox.preflight import PreflightError

logger = logging.getLogger("lumen_sandbox_kubernetes")

_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_PROFILE_ID = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_MAX_MCP_FRAME_BYTES = 8 * 1024 * 1024
_MCP_POST_KEYS = frozenset({
    "run_id", "call_id", "fence", "profile", "tool", "arguments", "timeout_seconds", "workspace_revision",
})


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: dict[str, Any]
    effect: str


@dataclass(frozen=True)
class ProfileDefinition:
    id: str
    command: tuple[str, ...]
    tools: tuple[ToolDefinition, ...]
    environment: dict[str, str]

    def tool(self, name: str) -> ToolDefinition | None:
        for item in self.tools:
            if item.name == name:
                return item
        return None


def load_manifest(path: Path, expected_sha256: str) -> dict[str, ProfileDefinition]:
    """Load and strictly validate the operator-approved image tool manifest."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise PreflightError(f"manifest file unreadable: {path}") from exc
    digest = hashlib.sha256(raw).hexdigest()
    if digest != expected_sha256.strip().lower():
        raise PreflightError("manifest sha256 mismatch")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PreflightError("manifest invalid json") from exc
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("profiles"), list):
        raise PreflightError("manifest invalid structure")

    profiles: dict[str, ProfileDefinition] = {}
    for p in data["profiles"]:
        if not isinstance(p, dict):
            raise PreflightError("invalid profile item")
        pid = p.get("id")
        if not isinstance(pid, str) or not _PROFILE_ID.fullmatch(pid):
            raise PreflightError("invalid profile id")
        cmd = p.get("command")
        if not isinstance(cmd, list) or not cmd or not all(isinstance(c, str) and c for c in cmd):
            raise PreflightError("invalid profile command")
        if not cmd[0].startswith("/"):
            raise PreflightError("profile command binary must be absolute path")
        raw_tools = p.get("tools")
        if not isinstance(raw_tools, list) or not raw_tools:
            raise PreflightError("profile requires non-empty tools")
        parsed_tools: list[ToolDefinition] = []
        tool_names: set[str] = set()
        for t in raw_tools:
            if not isinstance(t, dict):
                raise PreflightError("invalid tool item")
            tname = t.get("name")
            if not isinstance(tname, str) or not 0 < len(tname) <= 128 or tname in tool_names:
                raise PreflightError("invalid or duplicate tool name")
            tool_names.add(tname)
            tdesc = str(t.get("description", ""))
            tschema = t.get("input_schema")
            if not isinstance(tschema, dict):
                raise PreflightError("tool input_schema must be dict")
            teffect = t.get("effect", "external_mutation")
            parsed_tools.append(ToolDefinition(tname, tdesc, tschema, teffect))
        raw_env = p.get("environment") or {}
        if not isinstance(raw_env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in raw_env.items()):
            raise PreflightError("invalid profile environment")
        profiles[pid] = ProfileDefinition(pid, tuple(cmd), tuple(parsed_tools), dict(raw_env))

    return profiles


def verify_pod_preflight(
    *,
    memory_bytes: int,
    workspace_bytes: int,
    pids_limit: int,
) -> None:
    """Verify Pod isolation, finite memory, and an inherited guest process bound.

    gVisor exposes a v1-shaped memory controller but no host Pod PID limit.
    Host cgroup v2/PID enforcement is independently pinned by qualification;
    RLIMIT_NPROC additionally bounds every child in the guest user namespace.
    """
    if platform.system() != "Linux":
        raise PreflightError("Linux required for Kubernetes sandbox daemon")
    if os.geteuid() == 0 or os.getegid() == 0:
        raise PreflightError("sandbox daemon must run as non-root UID/GID")
    if workspace_bytes <= 0 or workspace_bytes > memory_bytes // 4:
        raise PreflightError("workspace_bytes must not exceed memory_bytes // 4")
    if pids_limit <= 0:
        raise PreflightError("pids_limit must be positive")

    # Process restrictions must be observable before starting any tool subprocess.
    try:
        status = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines() if ":" in line)
        if int(status.get("CapEff", "-1").strip(), 16) != 0:
            raise PreflightError("Pod capabilities not fully dropped")
        if status.get("NoNewPrivs", "").strip() != "1":
            raise PreflightError("Pod must prohibit privilege escalation")
    except (OSError, UnicodeError, ValueError) as exc:
        raise PreflightError("Pod process isolation unavailable") from exc

    if Path("/var/run/secrets/kubernetes.io/serviceaccount/token").exists():
        raise PreflightError("Kubernetes service account token must not be mounted")
    memory_file = Path("/sys/fs/cgroup/memory.max")
    if not memory_file.is_file():
        memory_file = Path("/sys/fs/cgroup/memory/memory.limit_in_bytes")
    try:
        measured_memory = int(memory_file.read_text(encoding="ascii").strip())
        if measured_memory <= 0 or measured_memory > memory_bytes:
            raise PreflightError("cgroup memory exceeds configured bounds")
    except (OSError, UnicodeError, ValueError) as exc:
        raise PreflightError("finite cgroup memory bound unavailable") from exc

    for path in ("/sys/fs/cgroup/pids.max", "/sys/fs/cgroup/pids/pids.max"):
        pid_file = Path(path)
        if not pid_file.is_file():
            continue
        try:
            value = pid_file.read_text(encoding="ascii").strip()
            if value != "max" and not 0 < int(value) <= pids_limit:
                raise PreflightError("cgroup pids exceeds configured bounds")
        except (OSError, UnicodeError, ValueError) as exc:
            raise PreflightError("cgroup pids bound unavailable") from exc

    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NPROC)
        hard = pids_limit if hard == resource.RLIM_INFINITY else min(hard, pids_limit)
        soft = hard if soft == resource.RLIM_INFINITY else min(soft, hard)
        if soft <= 0:
            raise PreflightError("guest process budget unavailable")
        resource.setrlimit(resource.RLIMIT_NPROC, (soft, hard))
    except (OSError, ValueError) as exc:
        raise PreflightError("guest process bound unavailable") from exc


def read_bootstrap_token(path: Path) -> bytes:
    """Read one-use token from projected volume without requiring root permissions."""
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 4096 or info.st_size == 0:
            raise PreflightError("bootstrap token file invalid size")
        data = os.read(fd, 4097)
        if len(data) > 4096 or not data:
            raise PreflightError("bootstrap token invalid length")
        return data.strip()
    finally:
        os.close(fd)


def bootstrap_pod(
    token_path: Path,
    controller_url: str,
    ca_file: str,
    state_dir: Path,
) -> tuple[dict[str, Any], Path, Path, Path]:
    """Exchange one-use bootstrap token with controller HTTPS listener and store issued TLS."""
    if not controller_url.startswith("https://") or not ca_file:
        raise PreflightError("bootstrap requires pinned HTTPS controller CA")
    token_bytes = bytearray(read_bootstrap_token(token_path))
    key = ec.generate_private_key(ec.SECP256R1())
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "lumen-sandbox")]))
        .sign(key, hashes.SHA256())
    )
    try:
        data = canonical({
            "token": token_bytes.decode("ascii"),
            "csr_pem": csr.public_bytes(serialization.Encoding.PEM).decode("ascii"),
        })
        req = urllib.request.Request(
            controller_url.rstrip("/") + "/v1/sandbox/bootstrap",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        ctx = ssl.create_default_context(cafile=ca_file)
        with urllib.request.urlopen(req, context=ctx, timeout=10) as resp:
            resp_body = resp.read(16384)
            resp_data = json.loads(resp_body)
    finally:
        for i in range(len(token_bytes)):
            token_bytes[i] = 0

    tls_dir = state_dir / "tls"
    tls_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    key_path = tls_dir / "server.key"
    cert_path = tls_dir / "server.crt"
    ca_path = tls_dir / "clients.ca"

    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    key_path.write_bytes(key_pem)
    cert_path.write_text(resp_data["certificate_pem"], encoding="ascii")
    ca_path.write_text(resp_data["client_ca_pem"], encoding="ascii")

    return resp_data, key_path, cert_path, ca_path


class McpProcess:
    """Managed stdio MCP subprocess for a single profile."""

    def __init__(self, profile: ProfileDefinition, workspace_dir: Path):
        self.profile = profile
        self.workspace_dir = workspace_dir
        self.proc: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._request_id = 0

    async def start(self) -> None:
        """Perform MCP handshake, requiring approved tools and ignoring advertised extras."""
        clean_env = {
            **self.profile.environment,
            "HOME": str(self.workspace_dir),
            "TMPDIR": str(self.workspace_dir / "tmp"),
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin:/opt/node/bin"),
        }
        (self.workspace_dir / "tmp").mkdir(mode=0o700, parents=True, exist_ok=True)
        self.proc = await asyncio.create_subprocess_exec(
            *self.profile.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=_MAX_MCP_FRAME_BYTES,
            env=clean_env,
            start_new_session=True,
        )

        # 1. Initialize
        self._request_id += 1
        init_req = json.dumps({
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "lumen-sandbox", "version": "0.1.1"},
            },
        }) + "\n"
        init_resp = await self._send_and_read(init_req, timeout=10.0)
        if "error" in init_resp:
            self.kill()
            raise RuntimeError(f"MCP server initialize error: {init_resp['error']}")

        # 2. Initialized notification
        init_notif = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
        if self.proc and self.proc.stdin:
            self.proc.stdin.write(init_notif.encode("utf-8"))
            await self.proc.stdin.drain()

        # 3. Require all manifest tools; advertised extras do not grant dispatch access.
        self._request_id += 1
        list_req = json.dumps({"jsonrpc": "2.0", "id": self._request_id, "method": "tools/list", "params": {}}) + "\n"
        list_resp = await self._send_and_read(list_req, timeout=10.0)
        tools_list = list_resp.get("result", {}).get("tools", [])
        advertised = {t.get("name") for t in tools_list if isinstance(t, dict)}
        approved = {t.name for t in self.profile.tools}
        missing = approved - advertised
        if missing:
            self.kill()
            raise RuntimeError(f"missing approved tools from MCP server: {missing}")

    async def _send_and_read(self, payload: str, timeout: float) -> dict[str, Any]:
        if self.proc is None or self.proc.stdin is None or self.proc.stdout is None:
            raise RuntimeError("MCP process not running")
        self.proc.stdin.write(payload.encode("utf-8"))
        await self.proc.stdin.drain()
        line = await asyncio.wait_for(self.proc.stdout.readline(), timeout=timeout)
        if not line:
            raise RuntimeError("MCP process closed stdout unexpectedly")
        if len(line) > _MAX_MCP_FRAME_BYTES:
            raise RuntimeError("MCP response exceeds frame bound")
        return json.loads(line.decode("utf-8"))

    async def call_tool(self, tool_name: str, arguments: dict[str, Any], timeout: float) -> dict[str, Any]:
        async with self._lock:
            if self.proc is None or self.proc.returncode is not None:
                await self.start()
            self._request_id += 1
            call_req = json.dumps({
                "jsonrpc": "2.0",
                "id": self._request_id,
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": arguments},
            }) + "\n"
            try:
                resp = await self._send_and_read(call_req, timeout=timeout)
            except TimeoutError:
                self.kill()
                raise
            except Exception:
                self.kill()
                raise
            if "error" in resp:
                return {"isError": True, "content": [{"type": "text", "text": str(resp["error"])}]}
            return resp.get("result", {"content": []})

    def kill(self) -> None:
        if self.proc is not None and self.proc.returncode is None:
            try:
                pgid = os.getpgid(self.proc.pid)
                os.killpg(pgid, signal.SIGTERM)
            except ProcessLookupError:
                pass


class McpSubprocessManager:
    """Lifecycle manager for all profile subprocesses within a sandbox."""

    def __init__(self, profiles: dict[str, ProfileDefinition], workspace_dir: Path):
        self.profiles = profiles
        self.workspace_dir = workspace_dir
        self.active: dict[str, McpProcess] = {}
        self._lock = asyncio.Lock()

    async def call(self, profile_id: str, tool_name: str, arguments: dict[str, Any], timeout: float) -> dict[str, Any]:
        async with self._lock:
            proc = self.active.get(profile_id)
            if proc is None:
                profile_def = self.profiles.get(profile_id)
                if profile_def is None:
                    raise ValueError(f"unknown profile {profile_id}")
                proc = McpProcess(profile_def, self.workspace_dir)
                self.active[profile_id] = proc
        return await proc.call_tool(tool_name, arguments, timeout)

    def kill_all(self) -> None:
        for p in self.active.values():
            p.kill()
        self.active.clear()


@dataclass(frozen=True)
class SandboxConfig:
    resource_id: str
    run_id: str
    generation: int
    image_id: str
    policy_id: str
    key: bytes
    state_dir: Path
    workspace_dir: Path
    memory_bytes: int
    workspace_bytes: int
    cpu_millis: int
    pids_limit: int
    run_deadline: float
    manifest_sha256: str
    profiles: dict[str, ProfileDefinition]


def create_kubernetes_app(config: SandboxConfig) -> Starlette:
    journal = Journal(config.state_dir, config.run_id, config.resource_id, config.generation)
    subprocesses = McpSubprocessManager(config.profiles, config.workspace_dir)
    active_tasks: dict[str, asyncio.Task[None]] = {}

    def checked_authorize(request: Request, *, fingerprint: str | None = None) -> dict[str, Any]:
        auth_header = request.headers.get("authorization", "")
        try:
            payload = verify(auth_header, config.key, now=time.time())
            if (
                payload.get("aud") != "lumen-sandbox"
                or payload.get("resource_id") != config.resource_id
                or payload.get("run_id") != config.run_id
                or payload.get("generation") != config.generation
                or payload.get("method") != request.method
                or payload.get("path") != request.url.path
                or type(payload.get("fence")) is not int
                or payload["fence"] < 0
            ):
                raise ValueError("invalid capability claim")
            if fingerprint is not None and payload.get("fingerprint") != fingerprint:
                raise ValueError("request digest mismatch")
            return payload
        except (KeyError, ValueError, TypeError, UnicodeError, json.JSONDecodeError):
            raise PermissionError("invalid dispatch capability") from None

    async def readyz(request: Request) -> Response:
        now = time.time()
        status_code = 200 if now < config.run_deadline else 503
        return JSONResponse(
            {
                "resource_id": config.resource_id,
                "run_id": config.run_id,
                "generation": config.generation,
                "image_id": config.image_id,
                "policy_id": config.policy_id,
                "manifest_sha256": config.manifest_sha256,
                "enforced_limits": {
                    "memory_bytes": config.memory_bytes,
                    "workspace_bytes": config.workspace_bytes,
                    "cpu_millis": config.cpu_millis,
                    "pids_limit": config.pids_limit,
                },
                "status": "ready" if status_code == 200 else "expired",
            },
            status_code=status_code,
        )

    async def submit_mcp_execution(request: Request) -> Response:
        try:
            raw = bytearray()
            async for chunk in request.stream():
                if len(raw) + len(chunk) > 65536:
                    return JSONResponse({"error": "request_too_large"}, status_code=413)
                raw.extend(chunk)
            body = json.loads(raw)
            if not isinstance(body, dict) or set(body) != _MCP_POST_KEYS:
                return JSONResponse({"error": "invalid_request_fields"}, status_code=400)
            run_id = body["run_id"]
            call_id = body["call_id"]
            profile_id = body["profile"]
            tool_name = body["tool"]
            timeout_seconds = body["timeout_seconds"]
            fence = body["fence"]
            workspace_revision = body["workspace_revision"]
            if (
                run_id != config.run_id
                or not isinstance(call_id, str)
                or not 0 < len(call_id) <= 128
                or not isinstance(profile_id, str)
                or not _PROFILE_ID.fullmatch(profile_id)
                or profile_id not in config.profiles
                or not isinstance(tool_name, str)
                or not 0 < len(tool_name) <= 128
                or not isinstance(body["arguments"], dict)
                or type(fence) is not int
                or fence < 0
                or type(workspace_revision) is not int
                or workspace_revision < 0
                or type(timeout_seconds) not in (int, float)
                or not 0 < timeout_seconds <= 300
            ):
                return JSONResponse({"error": "invalid_execution_arguments"}, status_code=400)

            profile_def = config.profiles[profile_id]
            if profile_def.tool(tool_name) is None:
                return JSONResponse({"error": "unapproved_tool"}, status_code=400)

            fingerprint = hashlib.sha256(canonical(body)).hexdigest()
            claim = checked_authorize(request, fingerprint=fingerprint)
            if (
                claim.get("call_id") != call_id
                or claim.get("fence") != fence
                or claim.get("workspace_revision") != workspace_revision
            ):
                return JSONResponse({"error": "capability_identity_mismatch"}, status_code=403)

            result, created = journal.reserve(
                call_id, fingerprint, fence, workspace_revision, allow_create=time.time() < config.run_deadline
            )
            if created:
                eid = result["id"]

                async def run_worker() -> None:
                    journal.start(eid, time.time())
                    try:
                        timeout = min(float(timeout_seconds), max(0.001, config.run_deadline - time.time()))
                        mcp_res = await subprocesses.call(profile_id, tool_name, body["arguments"], timeout)
                        journal.finish(eid, "succeeded", {"mcp_result": mcp_res}, time.time(), [])
                    except TimeoutError:
                        journal.finish(eid, "timeout", {"error": "timeout"}, time.time(), [])
                    except Exception as exc:
                        journal.finish(eid, "failed", {"error": str(exc)}, time.time(), [])
                    finally:
                        active_tasks.pop(eid, None)

                active_tasks[eid] = asyncio.create_task(run_worker())

            return JSONResponse(result, status_code=202 if created else 200)

        except PermissionError:
            return JSONResponse({"error": "unauthorized"}, status_code=403)
        except Conflict as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        except Exception:
            return JSONResponse({"error": "internal_error"}, status_code=500)

    async def get_workspace(request: Request) -> Response:
        try:
            claim = checked_authorize(request)
            return JSONResponse({"workspace_revision": journal.workspace_revision(claim["fence"])})
        except PermissionError:
            return JSONResponse({"error": "unauthorized"}, status_code=403)
        except Conflict as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)

    async def get_execution(request: Request) -> Response:
        try:
            claim = checked_authorize(request)
            result = journal.get(request.path_params["eid"])
            if result is None:
                return JSONResponse({"error": "not_found"}, status_code=404)
            if claim["fence"] < journal.highest_fence():
                return JSONResponse({"error": "stale_fence"}, status_code=409)
            return JSONResponse(result)
        except PermissionError:
            return JSONResponse({"error": "unauthorized"}, status_code=403)

    async def cancel_execution(request: Request) -> Response:
        try:
            claim = checked_authorize(request)
            eid = request.path_params["eid"]
            result = journal.get(eid)
            if result is None:
                return JSONResponse({"error": "not_found"}, status_code=404)
            if claim["fence"] < journal.highest_fence():
                return JSONResponse({"error": "stale_fence"}, status_code=409)
            if eid in active_tasks:
                task = active_tasks[eid]
                task.cancel()
                subprocesses.kill_all()
                journal.finish(eid, "cancelled", {"error": "cancelled"}, time.time(), [])
            return JSONResponse(journal.get(eid))
        except PermissionError:
            return JSONResponse({"error": "unauthorized"}, status_code=403)

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        async def expire_timer() -> None:
            await asyncio.sleep(max(0, config.run_deadline - time.time()))
            subprocesses.kill_all()

        timer = asyncio.create_task(expire_timer())
        try:
            yield
        finally:
            timer.cancel()
            subprocesses.kill_all()
            if active_tasks:
                await asyncio.gather(*active_tasks.values(), return_exceptions=True)

    return Starlette(
        routes=[
            Route("/readyz", readyz, methods=["GET"]),
            Route("/v1/mcp/executions", submit_mcp_execution, methods=["POST"]),
            Route("/v1/workspace", get_workspace, methods=["GET"]),
            Route("/v1/executions/{eid}", get_execution, methods=["GET"]),
            Route("/v1/executions/{eid}", cancel_execution, methods=["DELETE"]),
        ],
        lifespan=lifespan,
    )


def main() -> None:
    """Entry point for lumen-sandbox-kubernetes daemon."""
    state = Path(os.environ["SANDBOX_STATE_DIR"])
    workspace = Path(os.environ["SANDBOX_WORKSPACE_DIR"])
    memory = int(os.environ["SANDBOX_MEMORY_BYTES"])
    workspace_bytes = int(os.environ["SANDBOX_WORKSPACE_BYTES"])
    cpu_millis = int(os.environ["SANDBOX_CPU_MILLIS"])
    pids = int(os.environ["SANDBOX_PIDS_LIMIT"])
    manifest_path = Path(os.environ["SANDBOX_TOOL_MANIFEST"])
    manifest_hash = os.environ["SANDBOX_TOOL_MANIFEST_SHA256"]

    verify_pod_preflight(
        memory_bytes=memory,
        workspace_bytes=workspace_bytes,
        pids_limit=pids,
    )
    profiles = load_manifest(manifest_path, manifest_hash)
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    workspace.mkdir(mode=0o700, parents=True, exist_ok=True)

    token_file = Path(os.environ["SANDBOX_BOOTSTRAP_FILE"])
    controller_url = os.environ["SANDBOX_CONTROLLER_URL"]
    controller_ca = os.environ["SANDBOX_CONTROLLER_CA"]

    response, key_path, cert_path, ca_path = bootstrap_pod(token_file, controller_url, controller_ca, state)
    dispatch_key = base64.urlsafe_b64decode(response["dispatch_key"] + "=" * (-len(response["dispatch_key"]) % 4))

    config = SandboxConfig(
        resource_id=response["resource_id"],
        run_id=response["run_id"],
        generation=int(response["generation"]),
        image_id=str(response.get("image_id", "")),
        policy_id=str(response.get("policy_id", "")),
        key=dispatch_key,
        state_dir=state,
        workspace_dir=workspace,
        memory_bytes=memory,
        workspace_bytes=workspace_bytes,
        cpu_millis=cpu_millis,
        pids_limit=pids,
        run_deadline=float(response["run_deadline"]),
        manifest_sha256=manifest_hash,
        profiles=profiles,
    )

    port = int(os.environ.get("SANDBOX_PORT", "8013"))
    host = os.environ.get("SANDBOX_HOST", "0.0.0.0")

    app = create_kubernetes_app(config)
    uvicorn.run(
        app,
        host=host,
        port=port,
        ssl_keyfile=str(key_path),
        ssl_certfile=str(cert_path),
        ssl_ca_certs=str(ca_path),
        ssl_cert_reqs=ssl.CERT_REQUIRED,
        proxy_headers=False,
    )
