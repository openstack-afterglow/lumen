"""Real MariaDB contract for dashboard usage views: the caller's own rows by default, the pool for managers."""
from __future__ import annotations

import os
import uuid
from decimal import Decimal
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import delete

from lumen.api import domain_pools
from lumen.api import usage as usage_api
from lumen.db import close_db, get_session_factory, init_db
from lumen.models.chat_db import ChatUsageLog

pytestmark = pytest.mark.integration


@pytest.fixture
async def ledger(monkeypatch):
    init_db(os.environ["DATABASE_URL"], pool_size=4, max_overflow=0)
    factory = get_session_factory()
    nonce = uuid.uuid4().hex[:12]
    me, other, domain = f"usage-me-{nonce}", f"usage-other-{nonce}", f"usage-dom-{nonce}"

    def row(name, user_id, credits, **fields):
        return ChatUsageLog(event_id=f"{name}-{nonce}", project_id="usage-project", user_id=user_id,
                            model_name="usage-model", provider="fixture", prompt_tokens=1, completion_tokens=1,
                            credited_cost=Decimal(credits), source=fields.pop("source", "web"),
                            run_id=str(uuid.uuid4()), pricing_status="priced", **fields)

    rows = {
        # Before migration 037: no payer columns at all.
        "legacy": row("legacy", me, "1"),
        "personal_key": row("key", me, "2", source="api", api_key_id=77, billing_scope="user", payer_user_id=me),
        "my_domain": row("mine", me, "4", billing_scope="domain", payer_domain_id=domain, domain_id=domain),
        "other_domain": row("theirs", other, "8", billing_scope="domain", payer_domain_id=domain, domain_id=domain),
        "other_legacy": row("other-legacy", other, "16"),
    }
    async with factory() as session, session.begin():
        session.add_all(rows.values())
    ids = {name: item.id for name, item in rows.items()}
    authority = {"domain_id": domain, "is_member": True, "can_manage": True}
    monkeypatch.setattr(domain_pools.mcp_domain_authority, "resolve_domain_authority", AsyncMock(return_value=authority))
    app = FastAPI()
    app.include_router(usage_api.router, prefix="/v1")
    principal = {"auth_type": "keystone", "credential_class": "keystone", "user_id": me,
                 "project_id": "usage-project", "domain_id": domain}
    # Keystone service-role checks are not under test; the payer views are.
    app.dependency_overrides[usage_api._dashboard] = lambda: principal
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            yield client, ids, authority, other
    finally:
        async with factory() as session, session.begin():
            await session.execute(delete(ChatUsageLog).where(ChatUsageLog.event_id.like(f"%-{nonce}")))
        await close_db()


async def test_default_view_is_callers_own_usage_across_payers(ledger):
    client, ids, _authority, _other = ledger
    records = (await client.get("/v1/usage/records")).json()["records"]
    assert {item["id"] for item in records} == {ids["legacy"], ids["personal_key"], ids["my_domain"]}
    scopes = {item["id"]: item["billing_scope"] for item in records}
    assert scopes == {ids["legacy"]: "user", ids["personal_key"]: "user", ids["my_domain"]: "domain"}
    assert all(item["run_id"] for item in records)

    summary = (await client.get("/v1/usage")).json()
    assert summary["view"] == "self"
    assert Decimal(str(summary["total_credited_cost"])) == 7
    assert {item["billing_scope"]: item["credited_cost"] for item in summary["by_payer"]} == {"user": 3.0, "domain": 4.0}

    series = (await client.get("/v1/usage/timeseries?range=all")).json()["series"]
    assert sum(item["credited_cost"] for item in series) == 7
    assert sum(item["credited_cost_by_payer"]["domain"] for item in series) == 4

    keys = (await client.get("/v1/usage/keys")).json()["keys"]
    assert [(key["api_key_id"], key["billing_scope"]) for key in keys] == [(77, "user")]


async def test_domain_scope_is_manager_only_and_masks_other_members(ledger):
    client, ids, authority, _other = ledger
    response = await client.get("/v1/usage/records?scope=domain")
    assert response.status_code == 200, response.text
    records = {item["id"]: item for item in response.json()["records"]}
    assert set(records) == {ids["my_domain"], ids["other_domain"]}
    assert records[ids["my_domain"]]["run_id"] is not None
    assert records[ids["other_domain"]]["run_id"] is None
    assert all(item["billing_scope"] == "domain" for item in records.values())
    assert (await client.get("/v1/usage?scope=domain")).json()["view"] == "domain"

    authority["can_manage"] = False
    for path in ("/v1/usage", "/v1/usage/timeseries", "/v1/usage/keys", "/v1/usage/records"):
        denied = await client.get(f"{path}?scope=domain")
        assert denied.status_code == 403 and denied.json()["detail"] == "domain_manager_required"
    # Members keep their own default view.
    assert (await client.get("/v1/usage/records")).status_code == 200
