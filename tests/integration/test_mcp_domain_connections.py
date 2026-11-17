"""Real SQL domain MCP settings, owner activation and shared OAuth boundaries.

Run only against a disposable migrated test database. Synthetic credentials never leave it.
"""
from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from lumen_plugin_api.contracts import Namespace
from sqlalchemy import delete, select

from lumen.db import close_db, get_session_factory, init_db
from lumen.models.chat_contracts import ChatFeatureOptions
from lumen.models.chat_db import ChatMcpActivation, ChatMcpOAuthConnection, ChatMcpOAuthRequest, ChatMcpServer
from lumen.services import chat_admission, mcp_oauth, mcp_settings
from lumen.services import extensions_store as es
from lumen.services import mcp_registry_catalog as catalog

pytestmark = pytest.mark.integration


@pytest.fixture
async def domain_db(monkeypatch):
    init_db(os.environ["DATABASE_URL"], pool_size=3, max_overflow=0)
    factory = get_session_factory()
    assert factory is not None
    suffix = uuid.uuid4().hex
    domain_a, domain_b = f"a-{suffix}", f"b-{suffix}"
    manager = {"user_id": f"manager-{suffix}", "project_id": f"pa-{suffix}", "auth_type": "keystone"}
    manager_b = {"user_id": f"manager-b-{suffix}", "project_id": f"pb-{suffix}", "auth_type": "keystone"}
    member = {"user_id": f"member-{suffix}", "project_id": f"pa-{suffix}", "auth_type": "keystone"}
    foreign = {"user_id": f"foreign-{suffix}", "project_id": f"pb-{suffix}", "auth_type": "keystone"}
    ids = []

    async def resolve(user_id, project_id, domain_id=None, *, credential_authority=None):
        target = domain_a if project_id == manager["project_id"] else domain_b
        if domain_id is not None and domain_id != target:
            raise HTTPException(403, "wrong domain")
        return {"domain_id": target, "is_member": True,
                "can_manage": user_id in {manager["user_id"], manager_b["user_id"]} and credential_authority is None}

    async def access(user_id, project_id, domain_id=None, *, manage=False):
        result = await resolve(user_id, project_id, domain_id)
        if manage and not result["can_manage"]:
            raise es.ExtensionForbidden("manager required")
        return result

    monkeypatch.setattr(es, "_domain_access", access)
    monkeypatch.setattr(mcp_settings, "resolve_domain_authority", resolve)
    monkeypatch.setattr(es.ssrf, "validate_url", lambda url: url)
    try:
        yield {"factory": factory, "manager": manager, "manager_b": manager_b, "member": member, "foreign": foreign,
               "a": domain_a, "b": domain_b, "ids": ids, "suffix": suffix}
    finally:
        async with factory() as session, session.begin():
            for model in (ChatMcpOAuthRequest, ChatMcpOAuthConnection, ChatMcpActivation):
                await session.execute(delete(model).where(model.mcp_server_id.in_(ids)))
            await session.execute(delete(ChatMcpServer).where(ChatMcpServer.id.in_(ids)))
        await close_db()


def template(access):
    return next(item for item in catalog.entries() if item["access"] == access and not item["variables"])


async def create_domain(db, access="open"):
    candidate = template(access)
    kwargs = {"headers": {"Authorization": "synthetic-static-secret"}} if access == "credentials" else {}
    result = await mcp_settings.create(db["manager"], candidate["id"], domain_id=db["a"],
                                       label=db["suffix"], **kwargs)
    db["ids"].append(result["server"]["id"])
    return candidate, result


def owner(principal):
    return {"user_id": principal["user_id"], "project_id": principal["project_id"]}


async def selected(db, server_id, *, explicit=True):
    options = ChatFeatureOptions(tool_policy={"enabled_mcp_ids": [server_id]}) if explicit else ChatFeatureOptions()
    return await chat_admission._resolve_extension_selection(None, options, **owner(db["member"]))


async def test_scope_uniqueness_and_domain_idempotence(domain_db):
    db = domain_db
    candidate, first = await create_domain(db)
    duplicate = await mcp_settings.create(db["manager"], candidate["id"], domain_id=db["a"], label=db["suffix"])
    assert first["created"] and not duplicate["created"]
    assert duplicate["server"]["id"] == first["server"]["id"]
    personal = await mcp_settings.create(db["manager"], candidate["id"], label=db["suffix"])
    db["ids"].append(personal["server"]["id"])
    global_row = await es.create("mcp", {"name": candidate["name"], "url": candidate["url"],
        "transport": candidate["transport"], "auth_mode": "none", "template_key": candidate["id"],
        "connection_label": db["suffix"]}, scope="global")
    db["ids"].append(global_row["id"])
    assert personal["created"] and len({first["server"]["id"], personal["server"]["id"], global_row["id"]}) == 3
    async with db["factory"]() as session:
        row = await session.get(ChatMcpServer, first["server"]["id"])
        assert (row.owner_user_id, row.owner_project_id, row.owner_domain_id) == (
            db["manager"]["user_id"], db["manager"]["project_id"], db["a"])
        assert row.connection_scope_key == f"domain:{db['a']}" and row.implicit_selection is False
    other_domain = await mcp_settings.create(db["manager_b"], candidate["id"], domain_id=db["b"], label=db["suffix"])
    db["ids"].append(other_domain["server"]["id"])
    assert other_domain["created"] and other_domain["server"]["id"] != first["server"]["id"]


async def test_member_read_redacts_and_cannot_mutate_or_use_other_domain(domain_db):
    db = domain_db
    _, created = await create_domain(db, "credentials")
    server_id = created["server"]["id"]
    rows = await mcp_settings.domain_connections(db["member"], db["a"])
    row = next(row for row in rows if row["id"] == server_id)
    assert row["has_headers"] and "synthetic-static-secret" not in str(row)
    assert "synthetic-static-secret" not in str(await mcp_settings.view(db["member"]))
    with pytest.raises(es.ExtensionForbidden):
        await mcp_settings.update(db["member"], server_id, {"name": "unauthorized"}, domain_id=db["a"])
    with pytest.raises(es.ExtensionForbidden):
        await mcp_settings.delete(db["member"], server_id, domain_id=db["a"])
    with pytest.raises(HTTPException) as error:
        await mcp_settings.domain_connections(db["foreign"], db["a"])
    assert error.value.status_code == 403
    with pytest.raises(HTTPException) as error:
        await mcp_settings.update(db["foreign"], server_id, {"name": "wrong"}, domain_id=db["a"])
    assert error.value.status_code == 403
    with pytest.raises(es.ExtensionForbidden):
        await mcp_settings.create(db["member"], template("open")["id"], domain_id=db["a"], label=db["suffix"])
    assert server_id not in {r["id"] for r in await es.list_for_user("mcp", **owner(db["foreign"]))}
    with pytest.raises(HTTPException) as error:
        await es.set_mcp_activation(server_id, **owner(db["foreign"]), is_active=True, config_version=1)
    assert error.value.status_code == 403
    with pytest.raises(HTTPException) as error:
        await chat_admission._resolve_extension_selection(None,
            ChatFeatureOptions(tool_policy={"enabled_mcp_ids": [server_id]}), **owner(db["foreign"]))
    assert error.value.status_code == 404
    with pytest.raises(es.ExtensionForbidden):
        await es.update("mcp", server_id, {"name": "legacy"}, requester_user_id=db["manager"]["user_id"],
                        requester_project_id=db["manager"]["project_id"])
    updated = await es.update("mcp", server_id, {"name": "reviewed"},
        requester_user_id=db["manager"]["user_id"], requester_project_id=db["manager"]["project_id"], domain_id=db["a"])
    assert updated["name"] == "reviewed"


async def test_activation_freezes_explicit_selection_and_invalidates_on_changes(domain_db):
    db = domain_db
    _, created = await create_domain(db)
    server_id = created["server"]["id"]
    with pytest.raises(es.ExtensionValidationError):
        await es.set_mcp_activation(server_id, **owner(db["member"]), is_active=True, config_version=2)
    approved = await es.set_mcp_activation(server_id, **owner(db["member"]), is_active=True, config_version=1)
    assert approved["is_active"] and approved["implicit_selection"] is False
    assert server_id not in [item["id"] for item in (await selected(db, server_id, explicit=False))["mcp"]]
    snapshot = await selected(db, server_id)
    assert [item["id"] for item in snapshot["mcp"]] == [server_id]
    assert (await es.validated_frozen_selection(snapshot, **owner(db["member"])))[1] == (server_id,)
    await es.set_mcp_activation(server_id, **owner(db["member"]), is_active=False, config_version=1)
    assert (await es.validated_frozen_selection(snapshot, **owner(db["member"])))[1] == ()
    await es.set_mcp_activation(server_id, **owner(db["member"]), is_active=True, config_version=1)
    assert (await es.validated_frozen_selection(snapshot, **owner(db["member"])))[1] == ()
    newer = await selected(db, server_id)
    await mcp_settings.update(db["manager"], server_id, {"name": "changed"}, domain_id=db["a"])
    assert (await es.validated_frozen_selection(newer, **owner(db["member"])))[1] == ()
    with pytest.raises(es.ExtensionValidationError):
        await es.set_mcp_activation(server_id, **owner(db["member"]), is_active=True, config_version=1)
    await es.set_mcp_activation(server_id, **owner(db["member"]), is_active=True, config_version=2)
    assert (await selected(db, server_id))["mcp"][0]["id"] == server_id


async def test_shared_oauth_canonical_owner_and_revocation_invalidate_frozen_run(domain_db):
    db = domain_db
    _, created = await create_domain(db, "oauth")
    server_id = created["server"]["id"]
    store = mcp_oauth.SqlMcpConnectionStore()
    manager, member, foreign = (Namespace(**owner(db[k])) for k in ("manager", "member", "foreign"))
    state_hash = uuid.uuid4().hex
    with pytest.raises(es.ExtensionForbidden):
        await store.create_state(state_hash=uuid.uuid4().hex, server_id=server_id, namespace=member,
                                 payload={}, expires_at=datetime.now(UTC) + timedelta(minutes=5))
    await store.create_state(state_hash=state_hash, server_id=server_id, namespace=manager,
                             payload={"synthetic": True}, expires_at=datetime.now(UTC) + timedelta(minutes=5))
    async with db["factory"]() as session, session.begin():
        request = (await session.execute(select(ChatMcpOAuthRequest).where(
            ChatMcpOAuthRequest.state_hash == state_hash))).scalar_one()
        request.status = "processing"
    assert await store.complete_state(state_hash, token={"access_token": "synthetic-oauth-secret", "token_type": "Bearer"},
                                      expires_at=None) == server_id
    async with db["factory"]() as session:
        connection = (await session.execute(select(ChatMcpOAuthConnection).where(
            ChatMcpOAuthConnection.mcp_server_id == server_id))).scalar_one()
        assert (connection.owner_user_id, connection.owner_project_id) == (db["a"], db["a"])
        server = await session.get(ChatMcpServer, server_id)
        assert (server.owner_user_id, server.owner_project_id) == (manager.user_id, manager.project_id)
    assert "synthetic-oauth-secret" not in str(await mcp_settings.domain_connections(db["member"], db["a"]))
    assert (await store.connection(server_id, member)).user_id == db["a"]
    with pytest.raises(HTTPException) as error:
        await store.connection(server_id, foreign)
    assert error.value.status_code == 403
    with pytest.raises(es.ExtensionForbidden):
        await store.disconnect(server_id, member)
    assert await store.connections(member) == ()
    approved = await es.set_mcp_activation(server_id, **owner(db["member"]), is_active=True, config_version=1)
    assert approved["is_active"]
    assert [row.server_id for row in await store.connections(member)] == [server_id]
    snapshot = await selected(db, server_id)
    assert snapshot["mcp"][0]["credential_version"] == 1
    assert (await es.validated_frozen_selection(snapshot, **owner(db["member"])))[1] == (server_id,)
    await store.disconnect(server_id, manager)
    assert await store.connections(member) == ()
    assert (await es.validated_frozen_selection(snapshot, **owner(db["member"])))[1] == ()
    async with db["factory"]() as session:
        connection = (await session.execute(select(ChatMcpOAuthConnection).where(
            ChatMcpOAuthConnection.mcp_server_id == server_id))).scalar_one()
        assert connection.status == "revoked" and connection.credential_version == 2
    assert await store.connections(foreign) == ()
