"""Root supervisor: renewable identity, private readiness/drain, bounded child lifetime."""
from __future__ import annotations

import asyncio
import hmac
import json
import os
import secrets
import signal
import ssl
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from lumen.services.infrastructure.guest_bootstrap import (
    _atomic_file,
    _bootstrap_file,
    _identity_file,
    _sync_dir,
    activate_version,
    controller_request,
    identity_version,
    load_identity,
    service_account,
    write_identity,
)

_SHUTDOWN_MARGIN = timedelta(seconds=60)
_PRE_CUTOFF_MARGIN = timedelta(seconds=60)


def dependency_ready(body: bytes) -> bool:
    try:
        state = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return False
    return (isinstance(state, dict) and state.get("status") == "ok"
            and state.get("database") is True and state.get("plugins") is True
            and state.get("checkpointer") in (None, True))


def verified_load(body: bytes, *, now: datetime | None = None) -> dict | None:
    """Counters are valid even when dependencies are down; absent/stale is not zero."""
    try:
        state = json.loads(body)
        observed = datetime.fromisoformat(state["observed_at"])
        age = ((now or datetime.now(UTC)) - observed).total_seconds()
    except (ValueError, TypeError, KeyError, UnicodeDecodeError):
        return None
    if not 0 <= age <= 10:
        return None
    names = ("active_requests", "active_sse", "active_ws", "ttft_samples")
    if ("p95_ttft_ms" not in state
            or any(type(state.get(name)) is not int or state[name] < 0 for name in names)):
        return None
    p95 = state.get("p95_ttft_ms")
    if (state["active_sse"] > state["active_requests"]
            or (p95 is not None and (type(p95) is not int or p95 < 0))
            or (state["ttft_samples"] == 0) != (p95 is None)):
        return None
    return {name: state[name] for name in (*names, "p95_ttft_ms", "observed_at")}


def api_load(body: bytes) -> tuple[int, int | None] | None:
    load = verified_load(body)
    return (load["active_requests"], load["p95_ttft_ms"]) if load is not None else None


class DrainState:
    """Persist before forwarding; retries of equal fence repair lost acknowledgements."""

    def __init__(self, path: Path, identity: dict):
        self.path = path
        self.identity = {k: identity[k] for k in ("resource_id", "generation")}
        self.fence: int | None = None
        self.acknowledged = False
        if path.exists():
            data = json.loads(_bootstrap_file(path))
            if (any(data.get(k) != v for k, v in self.identity.items())
                    or type(data.get("fence")) is not int or data["fence"] < 0):
                raise RuntimeError("persisted guest drain invalid")
            self.fence = data["fence"]

    def accept(self, body: dict) -> None:
        if (not isinstance(body, dict) or set(body) != {"resource_id", "generation", "fence"}
                or any(body.get(k) != v for k, v in self.identity.items())
                or type(body.get("generation")) is not int
                or type(body.get("fence")) is not int or body["fence"] < 0):
            raise ValueError("invalid drain identity")
        if self.fence is not None and body["fence"] < self.fence:
            raise ValueError("stale drain fence")
        if self.fence != body["fence"]:
            _atomic_file(self.path, json.dumps(body).encode())
            self.fence = body["fence"]
            self.acknowledged = False

    async def forward(self, service_port: int, token: str) -> None:
        if self.fence is None:
            return
        async with httpx.AsyncClient(trust_env=False, timeout=3) as client:
            response = await client.post(f"http://127.0.0.1:{service_port}/v1/internal/drain",
                                         headers={"X-Lumen-Drain-Token": token}, json={"fence": self.fence})
        data = response.json()
        self.acknowledged = (response.status_code == 200 and data.get("draining") is True
                             and data.get("drain_fence") == self.fence and data.get("drain_acknowledged") is True)
        if not self.acknowledged:
            raise RuntimeError("guest drain not acknowledged")


class RenewalAgent:
    def __init__(self, directory: Path, identity: dict, controller_url: str):
        self.directory = directory
        self.identity = {k: identity[k] for k in ("role", "resource_id", "generation")}
        self.controller_url = controller_url
        self.state_file = directory / ".renewal" / "request.json"

    def renew(self) -> Path:
        import uuid
        if self.state_file.exists():
            state = json.loads(_bootstrap_file(self.state_file))
        else:
            key = ec.generate_private_key(ec.SECP256R1())
            csr = x509.CertificateSigningRequestBuilder().subject_name(x509.Name([])).sign(key, hashes.SHA256())
            state = {"renewal_request_id": str(uuid.uuid4()),
                     "key_pem": key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                  serialization.NoEncryption()).decode("ascii"),
                     "csr_pem": csr.public_bytes(serialization.Encoding.PEM).decode("ascii")}
            _atomic_file(self.state_file, json.dumps(state).encode())
        request_id = str(uuid.UUID(state["renewal_request_id"]))
        version = self.directory / request_id
        # Only a completely written, verified pending identity can recover activation.
        try:
            load_identity(version, **self.identity)
            pending_valid = True
        except (OSError, RuntimeError, ValueError, x509.ExtensionNotFound):
            pending_valid = False
        if pending_valid:
            try:
                self.activate(version, request_id)
                return version
            except RuntimeError:
                # An expired pending cannot activate. Renew with the persisted CSR;
                # an already-activated id is NOT success unless activate replay succeeds.
                pass
        data = controller_request(self.controller_url, "/v1/runtime/identity/renew", self.directory,
                                  body={"renewal_request_id": request_id, "csr_pem": state["csr_pem"]})
        ca_pem = data["ca_pem"].encode("ascii")
        if ca_pem != _identity_file(identity_version(self.directory) / "ca.pem"):
            raise RuntimeError("guest renewal CA mismatch")
        version = write_identity(self.directory, self.identity, key_pem=state["key_pem"].encode("ascii"),
                                 ca_pem=ca_pem, cert_pem=data["certificate_pem"].encode("ascii"), version=request_id)
        self.activate(version, request_id)
        return version

    def activate(self, version: Path, request_id: str) -> None:
        response = controller_request(self.controller_url, "/v1/runtime/identity/activate", version,
                                      body={"renewal_request_id": request_id})
        cert = x509.load_pem_x509_certificate(_identity_file(version / "cert.pem"))
        if (response.get("activated") is not True or type(response.get("not_after")) is not int
                or response["not_after"] != int(cert.not_valid_after_utc.timestamp())):
            raise RuntimeError("guest activation response invalid")

    def complete(self) -> None:
        self.state_file.unlink(missing_ok=True)
        _sync_dir(self.state_file.parent)


class GuestRuntime:
    def __init__(self, directory: Path, guest_config: dict, *, drain_path: Path,
                 service_port: int | None = None):
        self.directory = directory
        self.identity = load_identity(directory, role=guest_config["role"])
        self.service_port = service_port
        self.operator_pins = frozenset(guest_config["operator_probe_fingerprints"])
        self.interval = guest_config["renewal"]["renew_interval_seconds"]
        self.renewal = RenewalAgent(directory, self.identity, os.environ["LUMEN_CONTROLLER_URL"])
        self.drain = DrainState(drain_path, self.identity)
        self.token = secrets.token_urlsafe(32)
        self.context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        self.context.minimum_version = ssl.TLSVersion.TLSv1_2
        self.context.verify_mode = ssl.CERT_REQUIRED
        self.reload(identity_version(directory))

    def reload(self, version: Path) -> None:
        load_identity(version, **{k: self.identity[k] for k in ("role", "resource_id", "generation")})
        cert = x509.load_pem_x509_certificate(_identity_file(version / "cert.pem"))
        ca = x509.load_pem_x509_certificate(_identity_file(version / "ca.pem"))
        cutoff = min(cert.not_valid_after_utc, ca.not_valid_after_utc) - _SHUTDOWN_MARGIN
        if datetime.now(UTC) >= cutoff:
            raise RuntimeError("trusted identity has insufficient remaining lifetime")
        # Existing TLS connections finish on the old session; new handshakes see this chain.
        self.context.load_cert_chain(str(version / "cert.pem"), str(version / "key.pem"))
        self.context.load_verify_locations(cafile=str(version / "ca.pem"))
        self.cutoff = cutoff

    async def rotate(self) -> None:
        version = await asyncio.to_thread(self.renewal.renew)
        activate_version(self.directory, version)
        self.reload(version)
        self.renewal.complete()
        if self.service_port is not None and self.drain.fence is not None:
            await self.drain.forward(self.service_port, self.token)

    async def admission_stop(self, process: subprocess.Popen) -> None:
        if self.service_port is not None:
            self.drain.accept({**self.drain.identity, "fence": self.drain.fence or 0})
            await self.drain.forward(self.service_port, self.token)
        else:
            os.killpg(process.pid, signal.SIGTERM)

    async def probe(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        status, body = 503, {"error": "guest_unavailable"}
        try:
            tls = writer.get_extra_info("ssl_object")
            peer = tls.getpeercert(binary_form=True) if tls else None
            pin = x509.load_der_x509_certificate(peer).fingerprint(hashes.SHA256()).hex() if peer else ""
            if not any(hmac.compare_digest(pin, allowed) for allowed in self.operator_pins):
                status, body = 403, {"error": "operator_required"}
            elif datetime.now(UTC) < self.cutoff:
                headers = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=3)
                lines = headers.decode("ascii").split("\r\n")
                verb, path, protocol = lines[0].split(" ")
                if len(headers) > 4096 or protocol not in {"HTTP/1.0", "HTTP/1.1"}:
                    raise ValueError("invalid request")
                fields = {}
                for line in lines[1:]:
                    if not line:
                        continue
                    name, value = line.split(":", 1)
                    name = name.lower()
                    if name in fields or name == "transfer-encoding":
                        raise ValueError("invalid request headers")
                    fields[name] = value.strip()
                if verb == "POST" and path == "/v1/drain":
                    length = int(fields.get("content-length", "0"))
                    if not 0 < length <= 4096:
                        raise ValueError("invalid request size")
                    payload = json.loads(await asyncio.wait_for(reader.readexactly(length), timeout=3))
                    try:
                        self.drain.accept(payload)
                    except ValueError:
                        status, body = 409, {"error": "invalid_drain_fence"}
                    else:
                        await self.drain.forward(self.service_port, self.token)
                        status, body = 200, {"draining": True, "drain_fence": self.drain.fence,
                                             "drain_acknowledged": self.drain.acknowledged}
                elif verb == "GET" and path == "/v1/ready":
                    async with httpx.AsyncClient(trust_env=False, timeout=3) as client:
                        response = await client.get(f"http://127.0.0.1:{self.service_port}/v1/ready?include_load=1")
                    if response.status_code not in {200, 503} or len(response.content) > 8192:
                        raise ValueError("invalid local readiness")
                    load = verified_load(response.content)
                    if load is None:
                        raise ValueError("invalid local counters")
                    local = response.json()
                    local_fence = local.get("drain_fence")
                    acknowledged = (self.drain.fence is not None and local.get("draining") is True
                                    and type(local_fence) is int and local_fence == self.drain.fence
                                    and local.get("drain_acknowledged") is True)
                    self.drain.acknowledged = acknowledged
                    status, body = 200, {"ready": dependency_ready(response.content) and self.drain.fence is None,
                                         "draining": self.drain.fence is not None, "drain_fence": self.drain.fence,
                                         "drain_acknowledged": acknowledged, "load": load}
                else:
                    status, body = 404, {"error": "not_found"}
        except (OSError, TimeoutError, ValueError, TypeError, RuntimeError, asyncio.IncompleteReadError,
                asyncio.LimitOverrunError, httpx.HTTPError):
            status, body = 503, {"error": "guest_unavailable"}
        finally:
            if status == 200 and datetime.now(UTC) >= self.cutoff:
                status, body = 503, {"error": "guest_unavailable"}
            encoded = json.dumps(body).encode()
            writer.write(f"HTTP/1.1 {status} Response\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: {len(encoded)}\r\n\r\n".encode() + encoded)
            try:
                await writer.drain()
            except (OSError, ssl.SSLError):
                pass
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, ssl.SSLError):
                pass


async def _run(command: list[str], identity_dir: Path, *, guest_config: dict,
               readiness_port: int | None = None, service_port: int | None = None,
               drain_path: Path = Path("/var/lib/lumen/drain/state.json")) -> int:
    runtime = GuestRuntime(identity_dir, guest_config, service_port=service_port, drain_path=drain_path)
    server = None
    if readiness_port is not None:
        server = await asyncio.start_server(runtime.probe, host="0.0.0.0", port=readiness_port,
                                            ssl=runtime.context, limit=4096)
    process = None
    renewal_task = None
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stopping.set)
    try:
        account = service_account()
        environment = {**os.environ, "LUMEN_INTERNAL_DRAIN_TOKEN": runtime.token,
                       "HOME": account.pw_dir, "USER": account.pw_name, "LOGNAME": account.pw_name}
        environment.pop("LUMEN_INTERNAL_DRAIN_FENCE", None)
        # Reapply drain before the child can accept its first request after restart.
        if runtime.drain.fence is not None:
            environment["LUMEN_INTERNAL_DRAIN_FENCE"] = str(runtime.drain.fence)
        process = subprocess.Popen(command, start_new_session=True, user=account.pw_uid,
                                   group=account.pw_gid, extra_groups=[], env=environment)
        next_renew = loop.time() if runtime.renewal.state_file.exists() else loop.time() + runtime.interval
        admission_stopped = False
        while process.poll() is None and not stopping.is_set() and datetime.now(UTC) < runtime.cutoff:
            if renewal_task is not None and renewal_task.done():
                try:
                    await renewal_task
                    next_renew = loop.time() + runtime.interval
                except Exception:
                    # Safe code only: exceptions may contain secret response data.
                    next_renew = loop.time() + 20
                renewal_task = None
            if renewal_task is None and loop.time() >= next_renew:
                renewal_task = asyncio.create_task(runtime.rotate())
            if not admission_stopped and datetime.now(UTC) >= runtime.cutoff - _PRE_CUTOFF_MARGIN:
                try:
                    await runtime.admission_stop(process)
                    admission_stopped = True
                except (OSError, RuntimeError, ValueError, httpx.HTTPError):
                    pass
            if service_port is not None and runtime.drain.fence is not None and not runtime.drain.acknowledged:
                try:
                    await runtime.drain.forward(service_port, runtime.token)
                except (OSError, RuntimeError, ValueError, httpx.HTTPError):
                    pass
            try:
                await asyncio.wait_for(stopping.wait(), timeout=1)
            except TimeoutError:
                pass
    finally:
        # A to_thread renew cannot be interrupted. Do not wait for it before enforcing cutoff.
        if renewal_task is not None:
            renewal_task.cancel()
        if server is not None:
            server.close()
            await server.wait_closed()
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            if process.poll() is None:
                try:
                    await asyncio.wait_for(asyncio.to_thread(process.wait), timeout=10)
                except TimeoutError:
                    pass
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            if process.poll() is None:
                await asyncio.to_thread(process.wait)
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(signum)
    return process.returncode if process is not None else 1


def run(command: list[str], identity_dir: Path, config_file: Path, *, guest_config: dict) -> int:
    options = json.loads(_bootstrap_file(config_file))
    if (not isinstance(options, dict) or set(options) != {"readiness_port", "service_port"}
            or any(type(value) is not int or not 1 <= value <= 65535 for value in options.values())
            or options["readiness_port"] == options["service_port"]):
        raise RuntimeError("invalid API guest port configuration")
    return asyncio.run(_run(command, identity_dir, guest_config=guest_config, **options))


def run_worker(command: list[str], identity_dir: Path, *, guest_config: dict) -> int:
    return asyncio.run(_run(command, identity_dir, guest_config=guest_config))
