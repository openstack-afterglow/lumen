"""Remote, fail-closed adapter for Afterglow's MCP control plane.

Only opaque delegated-grant snapshots cross the Lumen boundary.  The Afterglow
bridge revalidates owner, grant, credential epoch, and selection generation on
every list, preview, and execution request; Lumen never receives a personal
MCP token or a user's Keystone credential.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

from lumen.config import get_settings

REGISTRY_VERSION = "v1"
_TIMEOUT = httpx.Timeout(15.0, connect=5.0)
_EMPTY_FINGERPRINT = "0" * 64


class McpLumenAuthorityError(RuntimeError):
    """The remote control plane cannot validate the selected grant."""


class McpInvocationError(RuntimeError):
    """The remote control plane rejected or could not complete an invocation."""


@dataclass(frozen=True)
class LumenSnapshot:
    user_id: str
    project_id: str
    grant_id: str
    credential_epoch: int
    selection_generation: int


@dataclass
class Principal:
    user_id: str
    project_id: str
    snapshot: LumenSnapshot
    entries: tuple[RegistryEntry, ...]
    service_fingerprint: str


@dataclass(frozen=True)
class RegistryEntry:
    name: str
    description: str
    effect: str
    schema: dict[str, Any]

    def input_schema(self) -> dict[str, Any]:
        return self.schema


@dataclass(frozen=True)
class ConsumerCloudContext:
    principal: Principal


@dataclass(frozen=True)
class ClaimResult:
    invocation_id: str = ""
    state: str = "failed"
    error: str | None = None
    result: dict[str, Any] | None = None


class _ParsedArguments(dict[str, Any]):
    def model_dump(self, mode: str = "json") -> dict[str, Any]:
        del mode
        return dict(self)


def snapshot_payload(snapshot: LumenSnapshot | None) -> dict[str, Any] | None:
    if snapshot is None:
        return None
    return {
        "grant_id": snapshot.grant_id,
        "user_id": snapshot.user_id,
        "project_id": snapshot.project_id,
        "credential_epoch": snapshot.credential_epoch,
        "selection_generation": snapshot.selection_generation,
    }


def frozen_snapshot(value: object) -> LumenSnapshot | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {
        "grant_id",
        "user_id",
        "project_id",
        "credential_epoch",
        "selection_generation",
    }:
        raise McpLumenAuthorityError("Lumen delegated MCP snapshot is invalid")
    grant_id = value["grant_id"]
    user_id = value["user_id"]
    project_id = value["project_id"]
    credential_epoch = value["credential_epoch"]
    selection_generation = value["selection_generation"]
    if (
        not all(isinstance(item, str) and item for item in (grant_id, user_id, project_id))
        or type(credential_epoch) is not int
        or credential_epoch < 1
        or type(selection_generation) is not int
        or selection_generation < 0
    ):
        raise McpLumenAuthorityError("Lumen delegated MCP snapshot is invalid")
    return LumenSnapshot(
        grant_id=grant_id,
        user_id=user_id,
        project_id=project_id,
        credential_epoch=credential_epoch,
        selection_generation=selection_generation,
    )


def _bridge_base_url() -> str:
    configured = get_settings().mcp_control_plane_url.strip().rstrip("/")
    token = get_settings().lumen_mcp_service_token
    parsed = urlsplit(configured)
    if (
        not configured
        or not token
        or parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise McpLumenAuthorityError("Lumen MCP control plane is not configured")
    return f"{configured}/api/v1/mcp/lumen"


async def _bridge_post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    settings = get_settings()
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, verify=settings.verify) as client:
            response = await client.post(
                f"{_bridge_base_url()}{path}",
                json=payload,
                headers={"Authorization": f"Bearer {settings.lumen_mcp_service_token}"},
            )
    except (httpx.HTTPError, ValueError) as exc:
        raise McpLumenAuthorityError("Lumen MCP control plane is unavailable") from exc
    if response.status_code in {401, 403, 404, 409, 422}:
        raise McpLumenAuthorityError("Lumen MCP authority rejected the request")
    if response.status_code >= 500:
        raise McpLumenAuthorityError("Lumen MCP control plane is unavailable")
    try:
        response.raise_for_status()
        data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise McpLumenAuthorityError("Lumen MCP control plane returned an invalid response") from exc
    if not isinstance(data, dict):
        raise McpLumenAuthorityError("Lumen MCP control plane returned an invalid response")
    return data


def _entry(value: object) -> RegistryEntry:
    if not isinstance(value, dict) or set(value) != {"name", "description", "input_schema", "effect"}:
        raise McpLumenAuthorityError("Lumen MCP registry response is invalid")
    name = value["name"]
    description = value["description"]
    schema = value["input_schema"]
    effect = value["effect"]
    if (
        not isinstance(name, str)
        or not name
        or len(name) > 128
        or not isinstance(description, str)
        or not isinstance(schema, dict)
        or effect not in {"read", "external_mutation"}
    ):
        raise McpLumenAuthorityError("Lumen MCP registry response is invalid")
    return RegistryEntry(name=name, description=description, effect=effect, schema=schema)


async def _resolve(snapshot: LumenSnapshot) -> Principal:
    response = await _bridge_post("/snapshot", {"user_id": snapshot.user_id, "project_id": snapshot.project_id})
    current = frozen_snapshot(response.get("snapshot"))
    if current != snapshot:
        raise McpLumenAuthorityError("Lumen delegated MCP selection changed")
    entries_value = response.get("entries")
    fingerprint = response.get("service_fingerprint")
    if not isinstance(entries_value, list) or not isinstance(fingerprint, str) or len(fingerprint) != 64:
        raise McpLumenAuthorityError("Lumen MCP registry response is invalid")
    return Principal(
        user_id=snapshot.user_id,
        project_id=snapshot.project_id,
        snapshot=snapshot,
        entries=tuple(_entry(value) for value in entries_value),
        service_fingerprint=fingerprint,
    )


async def selected_lumen_snapshot(*, user_id: str, project_id: str) -> LumenSnapshot | None:
    if not user_id or not project_id:
        return None
    response = await _bridge_post("/snapshot", {"user_id": user_id, "project_id": project_id})
    return frozen_snapshot(response.get("snapshot"))


async def resolve_lumen_principal(snapshot: LumenSnapshot) -> Principal:
    return await _resolve(snapshot)


def enabled_entries(principal: Principal) -> tuple[RegistryEntry, ...]:
    return principal.entries


def enabled_service_fingerprint(principal: Principal | None = None) -> str:
    """Return the authoritative remote catalog fingerprint for this binding."""
    return principal.service_fingerprint if principal is not None else _EMPTY_FINGERPRINT


def parse_entry_arguments(entry: RegistryEntry, arguments: dict[str, Any]) -> _ParsedArguments:
    if not isinstance(arguments, dict):
        raise McpInvocationError("MCP tool arguments are invalid")
    return _ParsedArguments(arguments)


async def dispatch(context: ConsumerCloudContext, entry: RegistryEntry, arguments: _ParsedArguments) -> dict[str, Any]:
    if entry.effect != "read":
        raise McpInvocationError("MCP mutation dispatch must use the durable ledger")
    try:
        return await _bridge_post(
            "/execute",
            {"snapshot": snapshot_payload(context.principal.snapshot), "name": entry.name, "arguments": arguments.model_dump()},
        )
    except McpLumenAuthorityError as exc:
        raise McpInvocationError("Cloud control-plane call was denied or is unavailable") from exc


def output_payload(entry: RegistryEntry, result: Any) -> dict[str, Any]:
    del entry
    if not isinstance(result, dict):
        raise McpInvocationError("MCP tool result is invalid")
    return result


async def record_read_invocation(*args: Any, **kwargs: Any) -> None:
    # The bridge records the invocation atomically with its remote execution.
    del args, kwargs


async def claim_mutation(principal: Principal, *, entry: RegistryEntry, arguments: dict[str, Any], idempotency_key: str, **_: Any) -> ClaimResult:
    try:
        result = await _bridge_post(
            "/execute",
            {
                "snapshot": snapshot_payload(principal.snapshot),
                "name": entry.name,
                "arguments": arguments,
                "idempotency_key": idempotency_key,
            },
        )
    except McpLumenAuthorityError as exc:
        raise McpInvocationError("Cloud control-plane call was denied or is unavailable") from exc
    # The bridge owns preview, authorization, dispatch, and completion under one
    # durable idempotency key.  Return replay so the caller never dispatches twice.
    return ClaimResult(state="replay", result=result)


async def fail_pre_dispatch(*args: Any, **kwargs: Any) -> None:
    del args, kwargs


async def authorize_mutation_dispatch(*args: Any, **kwargs: Any) -> None:
    del args, kwargs


async def complete_mutation(*args: Any, **kwargs: Any) -> None:
    del args, kwargs


async def build_mutation_preview(context: ConsumerCloudContext, entry: RegistryEntry, arguments: _ParsedArguments) -> dict[str, Any]:
    try:
        return await _bridge_post(
            "/preview",
            {"snapshot": snapshot_payload(context.principal.snapshot), "name": entry.name, "arguments": arguments.model_dump()},
        )
    except McpLumenAuthorityError as exc:
        raise McpInvocationError("Cloud mutation preview is unavailable") from exc
