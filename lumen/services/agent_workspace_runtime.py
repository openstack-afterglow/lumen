"""HTTPS-only control-plane boundary for remote code workspaces.

The API/worker never checks a repository out or executes model code locally.  This
module is the only planned client for the operator-provided workspace runtime.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

from lumen.services.code_workspace_store import CodeWorkspaceValidationError, normalize_repository_url
from lumen.services.sandbox_runtime import SandboxPolicy, configured_policy
from lumen.services.ssrf import SafeAsyncTransport

_MAX_RESPONSE_BYTES = 128 * 1024
_TIMEOUT_SECONDS = 30


class WorkspaceRuntimeUnavailable(RuntimeError):
    """The configured runtime is incomplete, unreachable, or returned invalid identity."""


@dataclass(frozen=True)
class WorkspaceRuntimePolicy:
    base_url: str
    sandbox: SandboxPolicy


def configured_workspace_policy(settings) -> WorkspaceRuntimePolicy | None:
    """Return a complete non-secret workspace runtime policy or ``None``.

    The workspace endpoint shares the sandbox bearer key, immutable image digest,
    policy version, and egress allowlist.  It must remain a separate HTTPS origin.
    """
    sandbox = configured_policy(settings)
    base_url = str(getattr(settings, "chat_sandbox_workspace_url", "") or "").strip().rstrip("/")
    if sandbox is None or not base_url:
        return None
    parsed = urlsplit(base_url)
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
    ):
        return None
    return WorkspaceRuntimePolicy(base_url=base_url, sandbox=sandbox)


def _endpoint(policy: WorkspaceRuntimePolicy, suffix: str) -> str:
    if not suffix.startswith("/"):
        raise ValueError("workspace runtime suffix must be absolute")
    return f"{policy.base_url}{suffix}"


async def _request(
    policy: WorkspaceRuntimePolicy, method: str, suffix: str, payload: dict[str, Any] | None
) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(
            transport=SafeAsyncTransport(max_response_bytes=_MAX_RESPONSE_BYTES),
            headers={"Authorization": f"Bearer {policy.sandbox.api_key}", "Accept-Encoding": "identity"},
            timeout=httpx.Timeout(_TIMEOUT_SECONDS),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = await client.request(method, _endpoint(policy, suffix), json=payload)
            response.raise_for_status()
            body = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise WorkspaceRuntimeUnavailable("workspace runtime request failed") from exc
    if not isinstance(body, dict):
        raise WorkspaceRuntimeUnavailable("workspace runtime response is invalid")
    if (
        body.get("image_digest") != policy.sandbox.image_digest
        or body.get("policy_version") != policy.sandbox.policy_version
    ):
        raise WorkspaceRuntimeUnavailable("workspace runtime identity mismatch")
    return body


def _source_payload(source: dict[str, Any], *, allowed_domains: tuple[str, ...]) -> dict[str, Any]:
    kind = source.get("kind")
    if kind == "empty":
        return {"kind": "empty"}
    if kind != "git":
        raise WorkspaceRuntimeUnavailable("workspace source kind is invalid")
    url = source.get("url")
    revision = source.get("source_revision")
    credential_version = source.get("credential_version")
    try:
        normalized_url, _host = (
            normalize_repository_url(url, allowed_domains=allowed_domains) if isinstance(url, str) else (None, None)
        )
    except CodeWorkspaceValidationError as exc:
        raise WorkspaceRuntimeUnavailable("git workspace source is invalid") from exc
    if (
        normalized_url is None
        or not isinstance(revision, str)
        or not revision
        or not isinstance(credential_version, int)
        or credential_version < 1
    ):
        raise WorkspaceRuntimeUnavailable("git workspace source is invalid")
    return {
        "kind": "git",
        "url": normalized_url,
        "source_revision": revision,
        "credential_version": credential_version,
    }


async def provision(
    policy: WorkspaceRuntimePolicy,
    *,
    workspace_id: str,
    source: dict[str, Any],
    operation_credential: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Provision one runtime workspace and verify its immutable runtime identity."""
    payload: dict[str, Any] = {
        "workspace_id": workspace_id,
        "source": _source_payload(source, allowed_domains=policy.sandbox.egress_allowlist),
        "image_digest": policy.sandbox.image_digest,
        "policy_version": policy.sandbox.policy_version,
        "egress_allowlist": list(policy.sandbox.egress_allowlist),
    }
    if operation_credential is not None:
        # It is intentionally request-only.  Callers must never persist this dict.
        payload["operation_credential"] = operation_credential
    body = await _request(policy, "POST", "/v1/workspaces", payload)
    required = ("workspace_id", "state_revision", "capabilities")
    if any(key not in body for key in required) or not isinstance(body["workspace_id"], str):
        raise WorkspaceRuntimeUnavailable("workspace provisioning response is invalid")
    if (
        body["workspace_id"] != workspace_id
        or not isinstance(body["state_revision"], int)
        or not isinstance(body["capabilities"], dict)
    ):
        raise WorkspaceRuntimeUnavailable("workspace provisioning identity is invalid")
    return body


async def delete(policy: WorkspaceRuntimePolicy, runtime_workspace_id: str) -> None:
    if not isinstance(runtime_workspace_id, str) or not runtime_workspace_id:
        raise WorkspaceRuntimeUnavailable("runtime workspace id is invalid")
    await _request(policy, "DELETE", f"/v1/workspaces/{runtime_workspace_id}", None)
