"""Own one authenticated, loopback-only Bifrost child per service process.

A failed child is never replaced by a request. Only a new service lifecycle may
start another child; durable dispatch/recovery remains entirely Python-owned.
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
from typing import Any

import httpx

from lumen.config import get_settings

CONTRACT = "single-attempt-v1"
# Kept in sync with the pinned module in runtime/bifrost/go.mod.
BIFROST_VERSION = "v1.13.2-0.20261021235121-668a660e5ac0"
_PROTOCOLS = {"chat", "responses", "messages", "count_tokens", "embeddings"}


class BifrostRuntimeError(RuntimeError):
    """Sanitized local lifecycle failure (never includes child output)."""


class _ManagedRuntime:
    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.process: asyncio.subprocess.Process | None = None
        self.stdout_task: asyncio.Task[None] | None = None
        self.address: str | None = None
        self.token = secrets.token_urlsafe(48)
        self.failed = False
        self.closed = False
        self.shutdown_seconds = 30.0

    async def get(self) -> tuple[str, str]:
        async with self.lock:
            if self.closed or self.failed:
                raise BifrostRuntimeError("Bifrost runtime unavailable")
            if self.process is not None:
                if self.process.returncode is not None:
                    self.failed = True
                    raise BifrostRuntimeError("Bifrost runtime exited; request not replayed")
                if self.address is None:
                    raise BifrostRuntimeError("Bifrost runtime unavailable")
                return self.address, self.token
            settings = get_settings()
            self.shutdown_seconds = settings.bifrost_shutdown_seconds
            executable = shutil.which(settings.bifrost_executable)
            if executable is None:
                self.failed = True
                raise BifrostRuntimeError("Bifrost executable unavailable; configure bifrost_executable")
            # Do not pass HOME, cloud/DB/provider credentials, proxy configuration,
            # or the service's full environment to the child. Custom CA paths are
            # trust material, not credentials, and preserve the existing TLS boundary.
            child_env = {
                "PATH": os.defpath, "LUMEN_BIFROST_TOKEN": self.token,
                "LUMEN_BIFROST_PARENT_PID": str(os.getpid()),
            }
            for name in ("SSL_CERT_FILE", "SSL_CERT_DIR"):
                if os.environ.get(name):
                    child_env[name] = os.environ[name]
            try:
                async with asyncio.timeout(settings.bifrost_startup_seconds):
                    self.process = await asyncio.create_subprocess_exec(
                        executable, "--listen", "127.0.0.1:0", env=child_env,
                        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL, limit=16 * 1024,
                    )
                    assert self.process.stdout is not None
                    raw = await self.process.stdout.readline()
                    proof = json.loads(raw)
                    self.address = self._ready_address(proof)
                    # Drain without logging: even a malformed child must not leak
                    # request bodies/secrets or block on a full stdout pipe.
                    self.stdout_task = asyncio.create_task(self._discard_stdout())
                    async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
                        response = await client.get(
                            self.address + "/ready", headers={"Authorization": "Bearer " + self.token},
                            timeout=settings.bifrost_startup_seconds,
                        )
                    if (response.status_code != 200
                            or response.headers.get("X-Lumen-Bifrost-Contract") != CONTRACT):
                        raise ValueError("invalid readiness response")
                    authenticated = response.json()
                    self._validate_identity(authenticated)
                    if not _PROTOCOLS.issubset(set(authenticated.get("supported_protocols", []))):
                        raise ValueError("missing protocols")
                    if self.process.returncode is not None:
                        raise ValueError("child exited during startup")
            except BaseException as exc:
                self.failed = True
                await self._stop()
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise BifrostRuntimeError("Bifrost startup or authenticated contract proof failed") from None
            return self.address, self.token

    @staticmethod
    def _validate_identity(proof: Any) -> None:
        if (not isinstance(proof, dict) or proof.get("contract") != CONTRACT
                or proof.get("version") != BIFROST_VERSION):
            raise ValueError("invalid runtime identity")

    @classmethod
    def _ready_address(cls, proof: Any) -> str:
        cls._validate_identity(proof)
        address = proof.get("address", "")
        if proof.get("ready") is not True or not isinstance(address, str):
            raise ValueError("invalid readiness announcement")
        host, separator, port = address.rpartition(":")
        if (host != "127.0.0.1" or separator != ":" or not port.isascii()
                or not port.isdecimal() or not 1 <= int(port) <= 65535):
            raise ValueError("invalid private listener")
        return "http://" + address

    async def _discard_stdout(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        while await self.process.stdout.read(16 * 1024):
            pass

    async def _stop(self) -> None:
        try:
            if self.process is None:
                return
            # Readiness parsing/readline may fail before the normal drainer was
            # installed. Always drain before waiting: a full pipe can keep an
            # asyncio Process.wait() pending even after the process is killed.
            if self.stdout_task is None and self.process.stdout is not None:
                self.stdout_task = asyncio.create_task(self._discard_stdout())
            if self.process.returncode is None:
                try:
                    self.process.terminate()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(self.process.wait(), timeout=self.shutdown_seconds)
                except TimeoutError:
                    try:
                        self.process.kill()
                    except ProcessLookupError:
                        pass
                    try:
                        await asyncio.wait_for(self.process.wait(), timeout=5.0)
                    except TimeoutError:
                        if self.process.returncode is None:
                            raise BifrostRuntimeError("Bifrost child did not exit after forced shutdown") from None
        finally:
            self.address = None
            if self.stdout_task is not None:
                # A descendant retaining stdout must not extend service cleanup
                # indefinitely either. No child output is exposed to logs.
                try:
                    await asyncio.wait_for(self.stdout_task, timeout=5.0)
                except TimeoutError:
                    pass

    async def close(self) -> None:
        async with self.lock:
            self.closed = True
            await self._stop()


_runtime: _ManagedRuntime | None = None


async def get_bifrost_runtime() -> tuple[str, str]:
    """Return the already-proven local runtime; never replay or restart failures."""
    global _runtime
    if _runtime is None:
        _runtime = _ManagedRuntime()
    return await _runtime.get()


async def start_bifrost_runtime() -> tuple[str, str]:
    """Begin a service lifecycle, including repeated in-process test lifespans."""
    global _runtime
    if _runtime is not None and _runtime.closed:
        _runtime = None
    return await get_bifrost_runtime()


async def close_bifrost_runtime() -> None:
    """Call only after active requests, durable runs and auxiliary work drain."""
    if _runtime is not None:
        await _runtime.close()
