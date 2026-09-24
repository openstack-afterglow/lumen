"""Managed Nova API guest: private mTLS readiness and bounded public process lifetime.

The public API stays on its operator-configured HTTP port behind Octavia. This
independent HTTPS listener exposes only dependency readiness to the controller;
its certificate is the per-generation bootstrap identity, never the public TLS
certificate. Run via guest_bootstrap --role api, not as a detached daemon.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import ssl
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from cryptography import x509

from lumen.services.infrastructure.guest_bootstrap import _private_file

_SHUTDOWN_MARGIN = timedelta(seconds=60)


def dependency_ready(body: bytes) -> bool:
    try:
        state = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return False
    return (isinstance(state, dict) and state.get("status") == "ok"
            and state.get("database") is True and state.get("plugins") is True
            and state.get("checkpointer") in (None, True))


def api_load(body: bytes) -> tuple[int, int | None] | None:
    """Validate counters from the local API process before trusting scaling input."""
    try:
        state = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(state, dict) or not dependency_ready(body):
        return None
    active, sse = state.get("active_requests"), state.get("active_sse")
    p95, count = state.get("p95_ttft_ms"), state.get("ttft_samples")
    if (type(active) is not int or type(sse) is not int or type(count) is not int
            or active < 0 or sse < 0 or sse > active or count < 0
            or (p95 is not None and (type(p95) is not int or p95 < 0))
            or (count == 0) != (p95 is None)):
        return None
    return active, p95

async def _probe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, *,
                 service_port: int, cutoff: datetime) -> None:
    try:
        request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=3)
        if len(request) > 2048 or not request.startswith(b"GET /v1/ready HTTP/1.") or datetime.now(UTC) >= cutoff:
            return
        async with httpx.AsyncClient(trust_env=False, timeout=3) as client:
            response = await client.get(f"http://127.0.0.1:{service_port}/v1/ready?include_load=1")
        if (response.status_code != 200 or len(response.content) > 4096
                or api_load(response.content) is None or datetime.now(UTC) >= cutoff):
            return
        body = response.content
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "
                     + str(len(body)).encode("ascii") + b"\r\n\r\n" + body)
        await writer.drain()
    except (TimeoutError, OSError, ValueError, asyncio.IncompleteReadError, httpx.HTTPError):
        pass
    finally:
        writer.close()
        await writer.wait_closed()


async def _run(command: list[str], identity_dir: Path, *,
               readiness_port: int | None = None, service_port: int | None = None) -> int:
    certificate = x509.load_pem_x509_certificate(_private_file(identity_dir / "cert.pem"))
    ca = x509.load_pem_x509_certificate(_private_file(identity_dir / "ca.pem"))
    cutoff = min(certificate.not_valid_after_utc, ca.not_valid_after_utc) - _SHUTDOWN_MARGIN
    if datetime.now(UTC) >= cutoff:
        raise RuntimeError("trusted identity has insufficient remaining lifetime")
    server = None
    if readiness_port is not None and service_port is not None:
        context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        context.load_cert_chain(str(identity_dir / "cert.pem"), str(identity_dir / "key.pem"))
        context.load_verify_locations(cafile=str(identity_dir / "ca.pem"))
        context.verify_mode = ssl.CERT_REQUIRED
        server = await asyncio.start_server(
            lambda reader, writer: _probe(reader, writer, service_port=service_port, cutoff=cutoff),
            host="0.0.0.0", port=readiness_port, ssl=context, limit=2048,
        )
    process = None
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stopping.set)
    try:
        process = subprocess.Popen(command, start_new_session=True)
        while process.poll() is None and not stopping.is_set() and datetime.now(UTC) < cutoff:
            try:
                await asyncio.wait_for(stopping.wait(), timeout=1)
            except TimeoutError:
                pass
    finally:
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
    return process.returncode if process is not None else 1


def run(command: list[str], identity_dir: Path, config_file: Path) -> int:
    options = json.loads(_private_file(config_file))
    if (not isinstance(options, dict) or set(options) != {"readiness_port", "service_port"}
            or any(type(value) is not int or not 1 <= value <= 65535 for value in options.values())
            or options["readiness_port"] == options["service_port"]):
        raise RuntimeError("invalid API guest port configuration")
    return asyncio.run(_run(command, identity_dir, **options))

def run_worker(command: list[str], identity_dir: Path) -> int:
    """Stop managed worker and any subprocesses before their mTLS identity expires."""
    return asyncio.run(_run(command, identity_dir))
