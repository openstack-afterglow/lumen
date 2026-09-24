"""Controller-issued, short-lived sandbox dispatch capabilities.

The controller alone holds the operator dispatch key; workers never see it. A
capability is minted only after one fenced DB read proves the caller's run still
owns a ready sandbox generation, so a demoted or stale worker cannot keep dispatching
into another owner's execution even if it still holds a live TCP connection.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select

from lumen.db import get_session_factory
from lumen.models.chat_infrastructure import ChatRuntimeResource, ChatWorkerRegistration
from lumen.models.chat_runs import ChatRun
from lumen.services.infrastructure.config import RuntimeConfig
from lumen.services.infrastructure.transport import (
    InternalTransport,
    InternalTransportError,
    derive_dispatch_key,
    resource_identity,
    sign_dispatch_capability,
)

_MAX_CAPABILITY_RESPONSE = 4096


class DispatchRejected(ValueError):
    """Deliberately non-diagnostic denial; never reveals run/resource existence."""


@dataclass(frozen=True)
class CapabilityGrant:
    capability: str
    address: str
    port: int
    certificate_fingerprint: str
    exp: int


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _fence_from_lease_owner(lease_owner: str) -> int:
    _, _, suffix = lease_owner.rpartition("#")
    if not suffix.isdigit():
        raise DispatchRejected("dispatch unavailable")
    return int(suffix)


async def authorize_dispatch(
    config: RuntimeConfig, *, client_identity: str, client_fingerprint: str,
    run_id: str, lease_owner: str, resource_id: str, generation: int,
    method: str, path: str, call_id: str | None = None,
    workspace_revision: int | None = None, body: dict[str, Any] | None = None,
) -> CapabilityGrant:
    """Authorize a live worker certificate, run lease and assigned sandbox together."""
    fence = _fence_from_lease_owner(lease_owner)
    factory = get_session_factory()
    if factory is None:
        raise RuntimeError("database is not configured")
    now = datetime.now(UTC)
    async with factory() as session:
        run = (await session.execute(select(ChatRun).where(ChatRun.id == run_id))).scalar_one_or_none()
        if (
            run is None or run.lease_owner != lease_owner or run.lease_fence != fence
            or run.status != "running" or run.cancel_requested_at is not None
            or run.lease_expires_at is None or _utc(run.lease_expires_at) <= now
            or run.assigned_resource_id != resource_id
        ):
            raise DispatchRejected("dispatch unavailable")
        root = await session.get(ChatRun, run.root_run_id or run.id)
        if root is None or root.cancel_requested_at is not None:
            raise DispatchRejected("dispatch unavailable")
        resource = (await session.execute(select(ChatRuntimeResource).where(
            ChatRuntimeResource.id == resource_id, ChatRuntimeResource.generation == generation,
            ChatRuntimeResource.run_id == run_id, ChatRuntimeResource.role == "sandbox",
            ChatRuntimeResource.observed_state == "ready", ChatRuntimeResource.desired_state != "deleting",
        ))).scalar_one_or_none()
        if resource is None or not resource.address or not resource.certificate_fingerprint:
            raise DispatchRejected("dispatch unavailable")
        worker = (await session.execute(select(ChatRuntimeResource).where(
            ChatRuntimeResource.role == "worker", ChatRuntimeResource.observed_state == "ready",
            ChatRuntimeResource.desired_state != "deleting",
            ChatRuntimeResource.certificate_fingerprint == client_fingerprint,
        ))).scalars().first()
        if worker is None or client_identity != resource_identity("worker", worker.id, worker.generation):
            raise DispatchRejected("dispatch unavailable")
        registration = (await session.execute(select(ChatWorkerRegistration).where(
            ChatWorkerRegistration.resource_id == worker.id,
            ChatWorkerRegistration.resource_generation == worker.generation,
            ChatWorkerRegistration.certificate_fingerprint == client_fingerprint,
            ChatWorkerRegistration.pool_id == worker.pool_id,
            ChatWorkerRegistration.worker_identity == lease_owner.rpartition("#")[0],
            # Drain forbids new claims, not capabilities for this already running lease.
            ChatWorkerRegistration.heartbeat_at >= now - timedelta(seconds=20),
        ))).scalars().first()
        if registration is None:
            raise DispatchRejected("dispatch unavailable")
    if config.dispatch_key is None:
        raise RuntimeError("controller dispatch key is not configured")
    try:
        key = derive_dispatch_key(config.dispatch_key, resource_id=resource.id, run_id=run.id, generation=generation)
        capability = sign_dispatch_capability(
            key, resource_id=resource.id, run_id=run.id, generation=generation, method=method, path=path,
            fence=fence, body=body, call_id=call_id, workspace_revision=workspace_revision,
        )
    except InternalTransportError as exc:
        raise DispatchRejected("dispatch unavailable") from exc
    return CapabilityGrant(
        capability=capability, address=resource.address, port=resource.port or config.listen_port,
        certificate_fingerprint=resource.certificate_fingerprint, exp=int(time.time()) + 15,
    )


def make_dispatch_router(config: RuntimeConfig):
    """Require a CA-verified worker client certificate on the HTTPS listener."""
    # Request must be module-global for postponed annotations to resolve.

    router = APIRouter()

    @router.post("/v1/dispatch-capabilities")
    async def issue(request: Request):
        identity = request.scope.get("lumen_client_identity")
        fingerprint = request.scope.get("lumen_client_fingerprint")
        if request.scope.get("scheme") != "https" or not identity or not fingerprint:
            raise HTTPException(status_code=403, detail="dispatch unavailable")
        try:
            if int(request.headers.get("content-length", "0")) > 65_536:
                raise HTTPException(status_code=413, detail="dispatch request too large")
        except ValueError as exc:
            raise HTTPException(status_code=403, detail="dispatch unavailable") from exc
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > 65_536:
                raise HTTPException(status_code=413, detail="dispatch request too large")
            raw.extend(chunk)
        try:
            payload = json.loads(raw)
            required = {"run_id", "lease_owner", "resource_id", "generation", "method", "path"}
            if not isinstance(payload, dict) or not required <= set(payload):
                raise DispatchRejected("dispatch unavailable")
            grant = await authorize_dispatch(
                config, client_identity=identity, client_fingerprint=fingerprint,
                run_id=payload["run_id"], lease_owner=payload["lease_owner"],
                resource_id=payload["resource_id"], generation=payload["generation"],
                method=payload["method"], path=payload["path"], call_id=payload.get("call_id"),
                workspace_revision=payload.get("workspace_revision"), body=payload.get("body"),
            )
        except (DispatchRejected, ValueError, TypeError, KeyError) as exc:
            raise HTTPException(status_code=403, detail="dispatch unavailable") from exc
        return {
            "capability": grant.capability, "address": grant.address, "port": grant.port,
            "certificate_fingerprint": grant.certificate_fingerprint, "exp": grant.exp,
        }

    return router


async def request_capability(
    config: RuntimeConfig, transport: InternalTransport, *, run_id: str, lease_owner: str,
    resource_id: str, generation: int, method: str, path: str,
    call_id: str | None = None, workspace_revision: int | None = None,
    body: dict[str, Any] | None = None,
) -> CapabilityGrant:
    """Ask the controller with the bootstrap-issued worker mTLS client identity."""
    endpoint = urlsplit(config.controller_url)
    if endpoint.scheme != "https" or not endpoint.hostname or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
        raise InternalTransportError("invalid controller URL")
    if endpoint.path not in {"", "/"}:
        raise InternalTransportError("invalid controller URL path")
    request_body = {
        "run_id": run_id, "lease_owner": lease_owner, "resource_id": resource_id,
        "generation": generation, "method": method, "path": path, "call_id": call_id,
        "workspace_revision": workspace_revision, "body": body,
    }
    encoded = json.dumps(request_body, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(encoded) > 65_536:
        raise InternalTransportError("capability request too large")
    try:
        async with (httpx.AsyncClient(verify=transport.controller_context, trust_env=False,
                                      follow_redirects=False, timeout=httpx.Timeout(10.0)) as client,
                    client.stream("POST", config.controller_url.rstrip("/") + "/v1/dispatch-capabilities",
                                  headers={"Content-Type": "application/json", "Accept-Encoding": "identity"},
                                  content=encoded) as response):
            if response.status_code != 200:
                raise InternalTransportError("dispatch capability rejected")
            raw = bytearray()
            async for chunk in response.aiter_raw():
                if len(raw) + len(chunk) > _MAX_CAPABILITY_RESPONSE:
                    raise InternalTransportError("capability response too large")
                raw.extend(chunk)
    except (httpx.HTTPError, ValueError) as exc:
        raise InternalTransportError("controller unavailable") from exc
    try:
        data = json.loads(raw)
        grant = CapabilityGrant(**data)
        if (not isinstance(grant.capability, str) or not isinstance(grant.address, str)
                or type(grant.port) is not int or not isinstance(grant.certificate_fingerprint, str)
                or type(grant.exp) is not int or grant.exp <= int(time.time())):
            raise ValueError("invalid capability grant")
        return grant
    except (TypeError, KeyError, ValueError) as exc:
        raise InternalTransportError("invalid capability grant") from exc
