"""Owner-scoped child run listing and admin agent-runtime inventory/quota routes.

Users see safe child/resource states and failure reason codes; OpenStack identifiers,
credentials and bootstrap material stay in the operator-only inventory.
"""

from __future__ import annotations

import base64
import json
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from lumen.auth import Principal, require_admin, require_scopes
from lumen.config import get_settings
from lumen.crypto import decrypt_chat_content
from lumen.db import get_session_factory, is_db_available
from lumen.models.chat_infrastructure import (
    ChatDelegationCall,
    ChatProjectAgentQuota,
    ChatRuntimePool,
    ChatRuntimeResource,
)
from lumen.models.chat_runs import ChatRun
from lumen.services.run_store import NONTERMINAL

router = APIRouter()
admin_router = APIRouter(dependencies=[Depends(require_admin)])

_PAGE_LIMIT = 100


def _factory():
    factory = get_session_factory()
    if factory is None or not is_db_available():
        raise HTTPException(status_code=503, detail="chat storage is unavailable")
    return factory


def _encode_cursor(created_at, run_id: str) -> str:
    return base64.urlsafe_b64encode(json.dumps([created_at.isoformat(), run_id]).encode()).decode().rstrip("=")


def _decode_cursor(value: str) -> tuple[str, str]:
    try:
        padded = value + "=" * (-len(value) % 4)
        created_at, run_id = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
        return str(created_at), str(run_id)
    except Exception as exc:
        raise HTTPException(status_code=422, detail="cursor is invalid") from exc


def _child_summary(run: ChatRun, call: ChatDelegationCall | None) -> dict[str, Any]:
    result: dict[str, Any] | None = None
    if call is not None and call.result_payload:
        try:
            payload = json.loads(decrypt_chat_content(call.result_payload))
            result = {
                "status": payload.get("status"),
                "error_code": payload.get("error_code"),
                "summary": payload.get("summary", ""),
                "artifacts": payload.get("artifacts", []),
            }
        except (TypeError, ValueError):
            result = None
    return {
        "run_id": run.id,
        "parent_run_id": run.parent_run_id,
        "root_run_id": run.root_run_id,
        "delegation_call_id": run.delegation_call_id,
        "wait_group_id": call.group_id if call is not None else None,
        "ordinal": call.ordinal if call is not None else None,
        "agent_id": run.agent_id,
        "depth": run.depth,
        "status": run.status,
        "terminal": run.status not in NONTERMINAL,
        "execution_mode": run.execution_mode,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "events_url": f"/v1/runs/{run.id}/events",
        "cancel_url": f"/v1/runs/{run.id}/cancel",
        "result": result,
    }


@router.get("/runs/{run_id}/children")
async def list_children(
    run_id: str,
    cursor: str | None = Query(default=None, max_length=512),
    limit: int = Query(default=50, ge=1, le=_PAGE_LIMIT),
    principal: Principal = Depends(require_scopes("native:runs:read")),
):
    """Cursor-paginated children of an owned run, in delegation (creation) order."""
    factory = _factory()
    async with factory() as session:
        parent = (
            await session.execute(
                select(ChatRun).where(
                    ChatRun.id == run_id,
                    ChatRun.project_id == principal["project_id"],
                    ChatRun.user_id == principal["user_id"],
                )
            )
        ).scalar_one_or_none()
        if parent is None:
            raise HTTPException(status_code=404, detail="chat run was not found")
        query = (
            select(ChatRun, ChatDelegationCall)
            .outerjoin(ChatDelegationCall, ChatDelegationCall.child_run_id == ChatRun.id)
            .where(ChatRun.parent_run_id == run_id)
            .order_by(ChatRun.created_at, ChatRun.id)
            .limit(limit + 1)
        )
        if cursor:
            created_at, last_id = _decode_cursor(cursor)
            query = query.where(
                (ChatRun.created_at > created_at) | ((ChatRun.created_at == created_at) & (ChatRun.id > last_id))
            )
        rows = list((await session.execute(query)).all())
    page = rows[:limit]
    next_cursor = _encode_cursor(page[-1][0].created_at, page[-1][0].id) if len(rows) > limit and page else None
    return {
        "parent_run_id": run_id,
        "children": [_child_summary(run, call) for run, call in page],
        "next_cursor": next_cursor,
    }


class ProjectAgentQuotaBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_active_children: int = Field(ge=0, le=1_000)
    max_active_sandboxes: int = Field(ge=0, le=1_000)
    max_sandbox_seconds: int = Field(ge=0, le=86_400 * 30)
    max_credit_reservation: Decimal = Field(ge=0, max_digits=18, decimal_places=8)


def _quota_view(row: ChatProjectAgentQuota) -> dict[str, Any]:
    return {
        "project_id": row.project_id,
        "max_active_children": row.max_active_children,
        "max_active_sandboxes": row.max_active_sandboxes,
        "max_sandbox_seconds": row.max_sandbox_seconds,
        "max_credit_reservation": format(Decimal(str(row.max_credit_reservation)), "f"),
        "active_children": row.active_children,
        "active_sandboxes": row.active_sandboxes,
        "sandbox_seconds_reserved": row.sandbox_seconds_reserved,
        "credits_reserved": format(Decimal(str(row.credits_reserved)), "f"),
    }


@admin_router.get("/admin/agent-project-quotas/{project_id}")
async def get_project_agent_quota(project_id: str = Path(min_length=1, max_length=64)):
    factory = _factory()
    async with factory() as session:
        row = await session.get(ChatProjectAgentQuota, project_id)
    if row is None:
        defaults = get_settings().runtime_config.project_quota_defaults
        return {
            "project_id": project_id,
            "configured": False,
            **defaults.model_dump(mode="json"),
            "active_children": 0,
            "active_sandboxes": 0,
            "sandbox_seconds_reserved": 0,
            "credits_reserved": "0",
        }
    return {"configured": True, **_quota_view(row)}


@admin_router.put("/admin/agent-project-quotas/{project_id}")
async def set_project_agent_quota(body: ProjectAgentQuotaBody, project_id: str = Path(min_length=1, max_length=64)):
    """Finite caps only; lowering a cap never releases live reservations, it only blocks new ones."""
    factory = _factory()
    async with factory() as session, session.begin():
        row = (
            await session.execute(
                select(ChatProjectAgentQuota).where(ChatProjectAgentQuota.project_id == project_id).with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            row = ChatProjectAgentQuota(project_id=project_id)
            session.add(row)
        row.max_active_children = body.max_active_children
        row.max_active_sandboxes = body.max_active_sandboxes
        row.max_sandbox_seconds = body.max_sandbox_seconds
        row.max_credit_reservation = body.max_credit_reservation
        await session.flush()
        return {"configured": True, **_quota_view(row)}


def _pool_view(row: ChatRuntimePool) -> dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "role": row.role,
        "backend": row.backend,
        "enabled": row.enabled,
        "cloud_profile_id": row.cloud_profile_id,
        "region_name": row.region_name,
        "image_ref": row.image_ref,
        "profile_digest": row.profile_digest,
        "min_replicas": row.min_replicas,
        "max_replicas": row.max_replicas,
        "slots_per_worker": row.slots_per_worker,
        "target_wait_seconds": row.target_wait_seconds,
        "desired_revision": row.desired_revision,
        "reconcile_lease_owner": row.reconcile_lease_owner,
        "reconcile_fence": row.reconcile_fence,
        "service_time_estimate_ms": row.service_time_estimate_ms,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _resource_view(row: ChatRuntimeResource) -> dict[str, Any]:
    # Provider IDs and addresses are operator inventory; bootstrap hashes/certs never leave the DB.
    return {
        "id": row.id,
        "pool_id": row.pool_id,
        "generation": row.generation,
        "role": row.role,
        "backend": row.backend,
        "run_id": row.run_id,
        "logical_project_id": row.logical_project_id,
        "desired_state": row.desired_state,
        "observed_state": row.observed_state,
        "provider_id": row.provider_id,
        "address": row.address,
        "port": row.port,
        "image_ref": row.image_ref,
        "policy_digest": row.policy_digest,
        "heartbeat_at": row.heartbeat_at.isoformat() if row.heartbeat_at else None,
        "ready_at": row.ready_at.isoformat() if row.ready_at else None,
        "deadline_at": row.deadline_at.isoformat() if row.deadline_at else None,
        "active_slots": row.active_slots,
        "failure_code": row.failure_code,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "deleted_at": row.deleted_at.isoformat() if row.deleted_at else None,
    }


@admin_router.get("/admin/runtime-pools")
async def list_runtime_pools():
    runtime = get_settings().runtime_config
    factory = _factory()
    async with factory() as session:
        rows = list((await session.execute(select(ChatRuntimePool).order_by(ChatRuntimePool.name))).scalars())
    return {"runtime": runtime.public_view(), "pools": [_pool_view(row) for row in rows]}


@admin_router.get("/admin/runtime-resources")
async def list_runtime_resources(
    pool_id: str | None = Query(default=None, max_length=36),
    state: str | None = Query(default=None, max_length=20),
    limit: int = Query(default=100, ge=1, le=500),
):
    factory = _factory()
    async with factory() as session:
        query = select(ChatRuntimeResource).order_by(ChatRuntimeResource.created_at.desc()).limit(limit)
        if pool_id:
            query = query.where(ChatRuntimeResource.pool_id == pool_id)
        if state:
            query = query.where(ChatRuntimeResource.observed_state == state)
        rows = list((await session.execute(query)).scalars())
    return {"resources": [_resource_view(row) for row in rows]}
