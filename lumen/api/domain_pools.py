"""Live domain-manager views and system-admin prepaid credit controls."""
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Path
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text

from lumen.auth import Principal, get_principal, require_admin
from lumen.models.billing import DomainCreditLedger, DomainCreditPool
from lumen.services import credit, domain_credit, mcp_domain_authority
from lumen.services.durable_runs.budgets import retry_deadlocks

router = APIRouter()
DomainId = Annotated[str, Path(min_length=1, max_length=64)]
Limit = Annotated[Decimal, Field(ge=0, max_digits=18, decimal_places=8)]
PositiveCredits = Annotated[Decimal, Field(gt=0, max_digits=18, decimal_places=8)]


class ManagerPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    manager_monthly_limit_credits: Limit | None


class AdminPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    admin_ceiling_credits: Limit | None = None
    is_active: bool = True


class TopUp(BaseModel):
    model_config = ConfigDict(extra="forbid")
    amount_credits: PositiveCredits
    note: Annotated[str, Field(max_length=1000)] | None = None


async def _manager(principal: Principal) -> str:
    if (principal.get("credential_class", principal.get("auth_type")) != "keystone"
            or principal.get("credential_restricted")):
        raise HTTPException(403, "domain_manager_required")
    domain_id = principal.get("domain_id")
    if not domain_id or not principal.get("project_id"):
        raise HTTPException(403, "domain_manager_required")
    authority = await mcp_domain_authority.resolve_domain_authority(
        principal["user_id"], principal["project_id"], domain_id,
        compute_manage=True, unrestricted_human=True,
    )
    if authority["domain_id"] != domain_id or not authority["is_member"] or not authority["can_manage"]:
        raise HTTPException(403, "domain_manager_required")
    return domain_id


def _factory():
    try:
        return credit._require_db()
    except credit.ChatStorageUnavailable as exc:
        raise HTTPException(503, "domain_credit_unavailable") from exc


@router.get("/domain-pool")
async def get_domain_pool(principal: Principal = Depends(get_principal)):
    domain_id = await _manager(principal)
    async with _factory()() as session:
        pool = await session.get(DomainCreditPool, domain_id)
        return domain_credit.public(pool, domain_id)


@router.patch("/domain-pool")
async def set_domain_limit(body: ManagerPatch, principal: Principal = Depends(get_principal)):
    domain_id = await _manager(principal)

    async def transaction():
        async with _factory()() as session, session.begin():
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
            pool = await domain_credit.lock_pool(session, domain_id)
            if pool is None:
                raise HTTPException(409, "domain_credit_insufficient")
            limit = body.manager_monthly_limit_credits
            if limit is not None and pool.admin_ceiling_credits is not None and limit > pool.admin_ceiling_credits:
                raise HTTPException(409, "domain_credit_limit_exceeds_ceiling")
            pool.manager_monthly_limit_credits = limit
            await session.flush()
            return domain_credit.public(pool, domain_id)

    return await retry_deadlocks(transaction)


@router.get("/admin/domain-pools", dependencies=[Depends(require_admin)])
async def list_domain_pools():
    async with _factory()() as session:
        pools = await session.scalars(select(DomainCreditPool).order_by(DomainCreditPool.domain_id))
        return {"items": [domain_credit.public(pool, pool.domain_id) for pool in pools]}


@router.patch("/admin/domain-pools/{domain_id}", dependencies=[Depends(require_admin)])
async def configure_domain_pool(domain_id: DomainId, body: AdminPatch):
    async def transaction():
        async with _factory()() as session, session.begin():
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
            pool = await domain_credit.lock_pool(session, domain_id, create=True)
            for field, value in body.model_dump(exclude_unset=True).items():
                setattr(pool, field, value)
            await session.flush()
            return domain_credit.public(pool, domain_id)

    return await retry_deadlocks(transaction)


@router.post("/admin/domain-pools/{domain_id}/topups")
async def top_up_domain_pool(
    domain_id: DomainId, body: TopUp,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=120)],
    admin: dict = Depends(require_admin),
):
    # Namespace independently per domain; the length bound keeps the reference under 190.
    reference = f"{len(domain_id)}:{domain_id}:{idempotency_key}"

    async def transaction():
        async with _factory()() as session, session.begin():
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
            pool = await domain_credit.lock_pool(session, domain_id, create=True)
            previous = await session.scalar(select(DomainCreditLedger).where(
                DomainCreditLedger.kind == "topup", DomainCreditLedger.reference == reference,
            ))
            if previous is not None:
                if (previous.amount_credits, previous.note) != (body.amount_credits, body.note):
                    raise HTTPException(409, "idempotency_key_reused_with_different_intent")
            else:
                if pool.balance_credits + body.amount_credits >= Decimal("10000000000"):
                    raise HTTPException(409, "domain_credit_balance_overflow")
                domain_credit.append_entry(session, pool, kind="topup", amount=body.amount_credits,
                                           reference=reference, actor_user_id=admin["user_id"], note=body.note)
            await session.flush()
            return domain_credit.public(pool, domain_id)

    return await retry_deadlocks(transaction)
