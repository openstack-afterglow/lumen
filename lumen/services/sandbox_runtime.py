"""Hardened remote sandbox boundary for chat code and computer capabilities.

The API process never executes user/model code.  It sends a bounded request to a
separately deployed HTTPS sandbox after freezing the configured image/policy
identity.  Invalid or incomplete configuration is deliberately unavailable,
not downgraded to local execution.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

import httpx

from lumen.services.ssrf import SafeAsyncTransport

_MAX_SOURCE_BYTES = 64 * 1024
_MAX_OUTPUT_BYTES = 64 * 1024
_MAX_RUNTIME_SECONDS = 300
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_POLICY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}\Z")
_HOST_RE = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")


class SandboxUnavailable(RuntimeError):
    """Sandbox configuration or remote policy is invalid/unavailable."""


@dataclass(frozen=True)
class SandboxPolicy:
    url: str
    api_key: str
    image_digest: str
    policy_version: str
    egress_allowlist: tuple[str, ...]


@dataclass(frozen=True)
class SandboxResult:
    stdout: str
    stderr: str
    exit_code: int
    duration_seconds: float


def configured_policy(settings) -> SandboxPolicy | None:
    """Return only a complete immutable policy snapshot; never infer defaults."""
    url = str(getattr(settings, "chat_sandbox_url", "") or "").strip()
    api_key = str(getattr(settings, "chat_sandbox_api_key", "") or "").strip()
    image_digest = str(getattr(settings, "chat_sandbox_image_digest", "") or "").strip()
    policy_version = str(getattr(settings, "chat_sandbox_policy_version", "") or "").strip()
    raw_allowlist = getattr(settings, "chat_sandbox_egress_allowlist", ()) or ()
    if not all((url, api_key, image_digest, policy_version)) or not isinstance(raw_allowlist, list):
        return None
    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port not in (None, 443)
        or not _DIGEST_RE.fullmatch(image_digest)
        or not _POLICY_RE.fullmatch(policy_version)
    ):
        return None
    hosts: list[str] = []
    for host in raw_allowlist:
        if not isinstance(host, str):
            return None
        normalized = host.strip().rstrip(".").lower()
        if not _HOST_RE.fullmatch(normalized):
            return None
        hosts.append(normalized)
    return SandboxPolicy(url, api_key, image_digest, policy_version, tuple(sorted(set(hosts))))


def sandbox_available(settings) -> bool:
    return configured_policy(settings) is not None


async def execute(
    policy: SandboxPolicy,
    *,
    language: Literal["python", "javascript", "shell"],
    source: str,
    timeout_seconds: int,
) -> SandboxResult:
    """Run bounded source remotely; reject redirects and policy identity drift."""
    if not isinstance(source, str) or not source.strip() or len(source.encode("utf-8")) > _MAX_SOURCE_BYTES:
        raise SandboxUnavailable("sandbox source is invalid or too large")
    if not 1 <= timeout_seconds <= _MAX_RUNTIME_SECONDS:
        raise SandboxUnavailable("sandbox timeout is out of range")
    payload = {
        "language": language,
        "source": source,
        "timeout_seconds": timeout_seconds,
        "image_digest": policy.image_digest,
        "policy_version": policy.policy_version,
        "egress_allowlist": list(policy.egress_allowlist),
    }
    try:
        async with httpx.AsyncClient(
            transport=SafeAsyncTransport(max_response_bytes=_MAX_OUTPUT_BYTES * 2),
            headers={"Authorization": f"Bearer {policy.api_key}", "Accept-Encoding": "identity"},
            timeout=httpx.Timeout(timeout_seconds + 5),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = await client.post(policy.url, json=payload)
            response.raise_for_status()
            body = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise SandboxUnavailable("sandbox execution failed") from exc
    if not isinstance(body, dict) or body.get("policy_version") != policy.policy_version:
        raise SandboxUnavailable("sandbox policy version mismatch")
    stdout = body.get("stdout", "")
    stderr = body.get("stderr", "")
    exit_code = body.get("exit_code")
    duration = body.get("duration_seconds")
    if (
        not isinstance(stdout, str)
        or not isinstance(stderr, str)
        or len(stdout.encode("utf-8")) > _MAX_OUTPUT_BYTES
        or len(stderr.encode("utf-8")) > _MAX_OUTPUT_BYTES
        or not isinstance(exit_code, int)
        or not isinstance(duration, (int, float))
        or duration < 0
        or duration > timeout_seconds
    ):
        raise SandboxUnavailable("sandbox response is invalid")
    return SandboxResult(stdout=stdout, stderr=stderr, exit_code=exit_code, duration_seconds=float(duration))
