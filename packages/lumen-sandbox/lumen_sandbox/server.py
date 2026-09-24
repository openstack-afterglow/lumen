"""Single-run mTLS sandbox service and controller bootstrap."""

import asyncio
import base64
import hashlib
import json
import os
import ssl
import time
import urllib.request
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .envelope import canonical
from .envelope import verify as verify_envelope
from .isolation import LIMIT, Workload
from .journal import Conflict, Journal
from .preflight import (
    PreflightError,
    activate_runtime_firewall,
    verify,
    verify_bootstrap_file,
    verify_private_directory,
    verify_workload_network,
)


@dataclass(frozen=True)
class Config:
    resource_id: str
    run_id: str
    generation: int
    image_id: str
    policy_id: str
    key: bytes
    state_dir: Path
    workload_uid: int
    workload_gid: int
    memory_bytes: int
    workspace_bytes: int
    cpu_quota: int
    pids_limit: int
    cgroup_parent: Path
    run_deadline: float


def authorize(request: Request, config: Config, *, fingerprint: str | None = None) -> dict:
    try:
        payload = verify_envelope(request.headers["authorization"], config.key, now=time.time())
        if (payload.get("aud") != "lumen-sandbox" or payload.get("resource_id") != config.resource_id
                or payload.get("run_id") != config.run_id or payload.get("generation") != config.generation
                or payload.get("method") != request.method or payload.get("path") != request.url.path
                or type(payload.get("fence")) is not int or payload["fence"] < 0):
            raise ValueError("capability scope/expiry invalid")
        if fingerprint is not None and payload.get("fingerprint") != fingerprint:
            raise ValueError("request digest mismatch")
        return payload
    except (KeyError, ValueError, TypeError, UnicodeError, json.JSONDecodeError):
        raise PermissionError("invalid dispatch capability") from None


def create_app(config: Config, workload: Workload) -> Starlette:
    journal = Journal(config.state_dir, config.run_id, config.resource_id, config.generation)
    active: dict[str, asyncio.Task] = {}

    def checked(request: Request, *, fingerprint: str | None = None) -> dict:
        return authorize(request, config, fingerprint=fingerprint)

    async def ready(request: Request) -> Response:
        return JSONResponse({"resource_id": config.resource_id, "run_id": config.run_id, "generation": config.generation,
            "image_id": config.image_id, "policy_id": config.policy_id, "languages": ["python", "javascript", "shell"],
            "limits": {"source_bytes": 65536, "stdout_bytes": LIMIT, "stderr_bytes": LIMIT, "timeout_seconds": 300,
                       "artifact_files": 20, "artifact_bytes": 5 * 1024 * 1024, "memory_bytes": config.memory_bytes,
                       "workspace_bytes": config.workspace_bytes, "pids": config.pids_limit}},
            status_code=200 if time.time() < config.run_deadline else 503)

    async def submit(request: Request) -> Response:
        try:
            raw = bytearray()
            async for chunk in request.stream():
                if len(raw) + len(chunk) > 524288:
                    return JSONResponse({"error": "request_too_large"}, status_code=413)
                raw.extend(chunk)
            body = json.loads(raw)
            if not isinstance(body, dict) or set(body) != {"run_id", "call_id", "fence", "language", "source", "timeout_seconds", "workspace_revision"}:
                raise ValueError("invalid request fields")
            run_id, call_id = body["run_id"], body["call_id"]
            if (run_id != config.run_id or not isinstance(call_id, str) or not 0 < len(call_id) <= 128
                    or type(body["fence"]) is not int or body["fence"] < 0
                    or type(body["workspace_revision"]) is not int or body["workspace_revision"] < 0
                    or body["language"] not in ("python", "javascript", "shell") or not isinstance(body["source"], str)
                    or len(body["source"].encode("utf-8")) > 65536 or type(body["timeout_seconds"]) not in (int, float)
                    or not 0 < body["timeout_seconds"] <= 300):
                raise ValueError("invalid execution arguments")
            fingerprint = hashlib.sha256(canonical(body)).hexdigest()
            claim = checked(request, fingerprint=fingerprint)
            if claim.get("call_id") != call_id or claim["fence"] != body["fence"] or claim.get("workspace_revision") != body["workspace_revision"]:
                raise PermissionError("capability identity mismatch")
            result, created = journal.reserve(call_id, fingerprint, body["fence"], body["workspace_revision"],
                                             allow_create=time.time() < config.run_deadline)
            if created:
                async def perform() -> None:
                    journal.start(result["id"], time.time())
                    try:
                        if workload.cancel_requested or time.time() >= config.run_deadline:
                            state, detail, artifacts = "cancelled", {"artifacts": []}, []
                        else:
                            timeout = min(body["timeout_seconds"], max(0.001, config.run_deadline - time.time()))
                            state, detail, artifacts = await workload.execute(result["id"], body["language"], body["source"], timeout)
                    except Exception as exc:
                        state, detail, artifacts = "failed", {"error": type(exc).__name__, "artifacts": []}, []
                    journal.finish(result["id"], state, detail, time.time(), artifacts)
                    active.pop(result["id"], None)
                    workload.cancel_requested = False
                active[result["id"]] = asyncio.create_task(perform())
            return JSONResponse(result, status_code=202 if created else 200)
        except (ValueError, TypeError, UnicodeError, json.JSONDecodeError):
            return JSONResponse({"error": "invalid_request"}, status_code=400)
        except PermissionError:
            return JSONResponse({"error": "unauthorized"}, status_code=403)
        except Conflict as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)

    async def get_execution(request: Request) -> Response:
        try:
            claim = checked(request)
            result = journal.get(request.path_params["eid"])
            if result is None:
                return JSONResponse({"error": "not_found"}, status_code=404)
            if claim["fence"] < journal.highest_fence():
                return JSONResponse({"error": "stale_fence"}, status_code=409)
            return JSONResponse(result)
        except PermissionError:
            return JSONResponse({"error": "unauthorized"}, status_code=403)

    async def cancel(request: Request) -> Response:
        try:
            claim = checked(request)
            eid = request.path_params["eid"]
            result = journal.get(eid)
            if result is None:
                return JSONResponse({"error": "not_found"}, status_code=404)
            if claim["fence"] < journal.highest_fence():
                return JSONResponse({"error": "stale_fence"}, status_code=409)
            if eid in active:
                workload.kill()
                await active[eid]
            return JSONResponse(journal.get(eid))
        except PermissionError:
            return JSONResponse({"error": "unauthorized"}, status_code=403)

    async def artifact(request: Request) -> Response:
        try:
            claim = checked(request)
            if claim["fence"] < journal.highest_fence():
                return JSONResponse({"error": "stale_fence"}, status_code=409)
            row = journal.artifact(request.path_params["aid"])
            if row is None:
                return JSONResponse({"error": "not_found"}, status_code=404)
            eid, _, size = row
            import stat
            path = config.state_dir / "snapshots" / eid / request.path_params["aid"]
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_size != size or size > 5 * 1024 * 1024:
                    return JSONResponse({"error": "unsafe_artifact"}, status_code=409)
                data = os.read(fd, size + 1)
                if len(data) != size:
                    return JSONResponse({"error": "unsafe_artifact"}, status_code=409)
                return Response(data, media_type="application/octet-stream", headers={"Content-Disposition": "attachment"})
            finally:
                os.close(fd)
        except PermissionError:
            return JSONResponse({"error": "unauthorized"}, status_code=403)
        except (OSError, ValueError):
            return JSONResponse({"error": "unsafe_artifact"}, status_code=409)

    @asynccontextmanager
    async def lifespan(_app: Starlette):
        async def expire():
            await asyncio.sleep(max(0, config.run_deadline - time.time()))
            if active:
                workload.kill()
        timer = asyncio.create_task(expire())
        try:
            yield
        finally:
            timer.cancel()
            if active:
                workload.kill()
                await asyncio.gather(*active.values())

    return Starlette(routes=[Route("/readyz", ready), Route("/v1/executions", submit, methods=["POST"]),
        Route("/v1/executions/{eid}", get_execution, methods=["GET"]), Route("/v1/executions/{eid}", cancel, methods=["DELETE"]),
        Route("/v1/artifacts/{aid}", artifact, methods=["GET"])], lifespan=lifespan)


def scrub_bootstrap_file(path: Path) -> None:
    fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    with os.fdopen(fd, "r+b", buffering=0) as file:
        length = file.seek(0, os.SEEK_END)
        file.seek(0)
        file.write(b"\x00" * length)
        file.truncate(0)
        os.fsync(file.fileno())
    path.unlink()


def bootstrap(path: Path, controller_url: str, ca_file: str, state: Path) -> tuple[dict, Path, Path, Path]:
    if not controller_url.startswith("https://") or not ca_file:
        raise PreflightError("bootstrap requires pinned HTTPS controller CA")
    while not path.exists():
        time.sleep(0.2)
    token = bytearray(verify_bootstrap_file(path))
    key = ec.generate_private_key(ec.SECP256R1())
    csr = x509.CertificateSigningRequestBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "lumen-sandbox")])).sign(key, hashes.SHA256())
    try:
        data = canonical({"token": token.decode("ascii"), "csr_pem": csr.public_bytes(serialization.Encoding.PEM).decode()})
        request = urllib.request.Request(controller_url.rstrip("/") + "/v1/sandbox/bootstrap", data=data, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(request, context=ssl.create_default_context(cafile=ca_file), timeout=10) as response:
            config = json.loads(response.read(16384))
    finally:
        for n in range(len(token)):
            token[n] = 0
        scrub_bootstrap_file(path)
    verify_private_directory(state)
    key_path, cert_path, ca_path = (state / name for name in ("server.key", "server.crt", "clients.ca"))
    for dest, contents in ((key_path, key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())),
                            (cert_path, config["certificate_pem"].encode()), (ca_path, config["client_ca_pem"].encode())):
        temporary = dest.with_suffix(dest.suffix + ".new")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, contents)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temporary, dest)
    return config, key_path, cert_path, ca_path


def main() -> None:
    import platform
    if platform.system() != "Linux":
        raise PreflightError("Linux only")
    state = Path(os.environ["SANDBOX_STATE_DIR"])
    cgroup = Path(os.environ["SANDBOX_CGROUP_PARENT"])
    uid, gid = int(os.environ["SANDBOX_WORKLOAD_UID"]), int(os.environ["SANDBOX_WORKLOAD_GID"])
    memory, workspace = int(os.environ["SANDBOX_MEMORY_BYTES"]), int(os.environ["SANDBOX_WORKSPACE_BYTES"])
    verify(uid=uid, gid=gid, memory_bytes=memory, workspace_bytes=workspace, cgroup_parent=cgroup,
           node_major=int(os.environ["SANDBOX_NODE_MAJOR"]), node_version=os.environ["SANDBOX_NODE_VERSION"], runtime_firewall=False)
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    verify_private_directory(state)
    verify_private_directory(Path(os.environ["SANDBOX_BOOTSTRAP_FILE"]).parent)
    response, key, cert, ca = bootstrap(Path(os.environ["SANDBOX_BOOTSTRAP_FILE"]), os.environ["SANDBOX_CONTROLLER_URL"], os.environ["SANDBOX_CONTROLLER_CA"], state)
    # Atomically replace the boot-only outbound exception before listening.
    activate_runtime_firewall()
    config = Config(resource_id=response["resource_id"], run_id=response["run_id"], generation=int(response["generation"]),
        image_id=response["image_id"], policy_id=response["policy_id"], key=base64.urlsafe_b64decode(response["dispatch_key"] + "=" * (-len(response["dispatch_key"]) % 4)), state_dir=state,
        workload_uid=uid, workload_gid=gid, memory_bytes=memory, workspace_bytes=workspace,
        cpu_quota=int(os.environ["SANDBOX_CPU_QUOTA"]), pids_limit=int(os.environ["SANDBOX_PIDS_LIMIT"]), cgroup_parent=cgroup,
        run_deadline=float(response["run_deadline"]))
    import math
    if len(config.key) < 32 or not math.isfinite(config.run_deadline):
        raise PreflightError("dispatch key or run deadline invalid")
    workload = Workload(root=state, workspace_root=Path(os.environ["SANDBOX_WORKSPACE_DIR"]), uid=uid, gid=gid,
        memory_bytes=memory, workspace_bytes=workspace, cpu_quota=config.cpu_quota,
        pids_limit=config.pids_limit, cgroup_parent=cgroup)
    try:
        asyncio.run(verify_workload_network(workload, int(os.environ.get("SANDBOX_PORT", "8013"))))
        uvicorn.run(create_app(config, workload), host=os.environ.get("SANDBOX_HOST", "0.0.0.0"),
            port=int(os.environ.get("SANDBOX_PORT", "8013")), ssl_keyfile=str(key), ssl_certfile=str(cert),
            ssl_ca_certs=str(ca), ssl_cert_reqs=ssl.CERT_REQUIRED, proxy_headers=False)
    finally:
        workload.close()


if __name__ == "__main__":
    main()
