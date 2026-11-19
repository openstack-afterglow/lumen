"""Real SQL domain MCP settings, owner activation and shared OAuth boundaries.

Run only against a disposable migrated test database. Synthetic credentials never leave it.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from lumen_plugin_api.contracts import Namespace
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from lumen.db import close_db, get_session_factory, init_db
from lumen.models.chat_contracts import ChatFeatureOptions
from lumen.models.chat_db import (
    ChatCustomTool,
    ChatMcpActivation,
    ChatMcpOAuthConnection,
    ChatMcpOAuthRequest,
    ChatMcpServer,
)
from lumen.services import chat_admission, mcp_oauth, mcp_settings
from lumen.services import extensions_store as es
from lumen.services import mcp_registry_catalog as catalog

pytestmark = pytest.mark.integration


@pytest.fixture
async def domain_db(monkeypatch, request):
    init_db(os.environ["DATABASE_URL"], pool_size=getattr(request, "param", 3), max_overflow=0, pool_timeout=1)
    factory = get_session_factory()
    assert factory is not None
    suffix = uuid.uuid4().hex
    domain_a, domain_b = f"a-{suffix}", f"b-{suffix}"
    manager = {"user_id": f"manager-{suffix}", "project_id": f"pa-{suffix}", "auth_type": "keystone"}
    manager_b = {"user_id": f"manager-b-{suffix}", "project_id": f"pb-{suffix}", "auth_type": "keystone"}
    member = {"user_id": f"member-{suffix}", "project_id": f"pa-{suffix}", "auth_type": "keystone"}
    foreign = {"user_id": f"foreign-{suffix}", "project_id": f"pb-{suffix}", "auth_type": "keystone"}
    ids = []
    tool_ids = []
    second_project = f"pa2-{suffix}"
    project_domains = {manager["project_id"]: domain_a, second_project: domain_a, manager_b["project_id"]: domain_b}

    async def resolve(user_id, project_id, domain_id=None, *, credential_authority=None, compute_manage=True,
                      unrestricted_human=False):
        target = project_domains[project_id]
        if domain_id is not None and domain_id != target:
            raise HTTPException(403, "wrong domain")
        return {"domain_id": target, "is_member": True,
                "can_manage": user_id in {manager["user_id"], manager_b["user_id"]} and credential_authority is None
                and unrestricted_human}

    async def project_domain(project_id):
        return project_domains[project_id]

    monkeypatch.setattr("lumen.services.mcp_domain_authority.project_domain_id", project_domain)

    async def project_authority(user_id, project_id):
        return {"roles": ["member"], "is_system_admin": False}

    monkeypatch.setattr(mcp_oauth, "resolve_project_authority", project_authority)

    async def access(user_id, project_id, domain_id=None, *, manage=False, human=None):
        result = await resolve(user_id, project_id, domain_id, unrestricted_human=True)
        if manage and not result["can_manage"]:
            raise es.ExtensionForbidden("manager required")
        return result

    monkeypatch.setattr(es, "_domain_access", access)
    monkeypatch.setattr(mcp_settings, "resolve_domain_authority", resolve)
    monkeypatch.setattr(es.ssrf, "validate_url", lambda url: url)
    try:
        yield {"factory": factory, "manager": manager, "manager_b": manager_b, "member": member, "foreign": foreign,
               "a": domain_a, "b": domain_b, "ids": ids, "tool_ids": tool_ids, "suffix": suffix,
               "second_project": second_project, "project_domains": project_domains}
    finally:
        async with factory() as session, session.begin():
            for model in (ChatMcpOAuthRequest, ChatMcpOAuthConnection, ChatMcpActivation):
                await session.execute(delete(model).where(model.mcp_server_id.in_(ids)))
            await session.execute(delete(ChatMcpServer).where(ChatMcpServer.id.in_(ids)))
            await session.execute(delete(ChatCustomTool).where(ChatCustomTool.id.in_(tool_ids)))
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


async def test_activation_rejects_domain_row_without_owner_domain(domain_db, monkeypatch):
    db = domain_db
    _, created = await create_domain(db)
    server_id = created["server"]["id"]
    original_get = AsyncSession.get

    async def legacy_get(session, model, identity, **kwargs):
        row = await original_get(session, model, identity, **kwargs)
        if model is ChatMcpServer and identity == server_id:
            # 035 forbids storing this legacy state; still exercise the application guard on a corrupt read.
            set_committed_value(row, "owner_domain_id", None)
        return row

    monkeypatch.setattr(AsyncSession, "get", legacy_get)
    with pytest.raises(es.ExtensionForbidden):
        await es.set_mcp_activation(server_id, **owner(db["member"]), is_active=True, config_version=1)


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


async def test_domain_list_and_mutations_report_caller_effective_activation(domain_db):
    db = domain_db
    _, created = await create_domain(db)
    server = created["server"]
    # The manager has not activated its own organization connection: publication is not caller consent.
    assert server["is_active"] is False and server["server_is_active"] is True
    assert server["activation_is_active"] is False

    before = await mcp_settings.domain_connections(db["member"], db["a"])
    assert [(row["id"], row["is_active"]) for row in before] == [(server["id"], False)]

    await es.set_mcp_activation(
        server["id"], **owner(db["member"]), is_active=True, config_version=server["config_version"]
    )
    member_view = await mcp_settings.domain_connections(db["member"], db["a"])
    manager_view = await mcp_settings.domain_connections(db["manager"], db["a"])
    assert member_view[0]["is_active"] is True and member_view[0]["activation_is_active"] is True
    assert manager_view[0]["is_active"] is False, "another member's activation is not the manager's"

    patched = await mcp_settings.update(db["manager"], server["id"], {"is_active": False}, domain_id=db["a"])
    assert patched["is_active"] is False and patched["server_is_active"] is False
    again = await mcp_settings.domain_connections(db["member"], db["a"])
    assert again[0]["is_active"] is False, "disable propagates to members on the next read"


async def test_authority_outage_never_fails_unrelated_listing_or_implicit_runs(domain_db, monkeypatch):
    db = domain_db
    _, created = await create_domain(db)
    server = created["server"]
    calls = []

    async def broken(user_id, project_id, domain_id=None, *, manage=False):
        calls.append((user_id, manage))
        raise HTTPException(503, "Current Keystone domain authority unavailable")

    monkeypatch.setattr(es, "_domain_access", broken)
    # Implicit admission (hot path) never asks for the domain: organization rows are never implicit.
    implicit = await chat_admission._resolve_extension_selection(None, ChatFeatureOptions(), **owner(db["foreign"]))
    assert server["id"] not in {item["id"] for item in implicit["mcp"]} and calls == []
    # Explicitly selecting only non-organization ids does not ask either.
    assert await es.list_for_user("mcp", **owner(db["foreign"]), active_only=True, include_domain=(10**9,)) is not None
    assert calls == []
    # The settings list hides organization rows instead of failing with 503.
    rows = await es.list_for_user("mcp", **owner(db["foreign"]))
    assert server["id"] not in {row["id"] for row in rows} and len(calls) == 1
    # An explicit selection of the organization row fails closed (unavailable), not with a stack of Keystone errors.
    with pytest.raises(HTTPException) as unavailable:
        await chat_admission._resolve_extension_selection(
            None, ChatFeatureOptions(tool_policy={"enabled_mcp_ids": [server["id"]]}), **owner(db["member"])
        )
    assert unavailable.value.status_code == 404


async def test_legacy_route_cannot_repoint_a_catalog_connection(domain_db):
    db = domain_db
    candidate = template("open")
    personal = await mcp_settings.create(db["member"], candidate["id"], label=db["suffix"])
    db["ids"].append(personal["server"]["id"])
    with pytest.raises(es.ExtensionValidationError):
        await es.update("mcp", personal["server"]["id"], {"url": "https://attacker.invalid/mcp"},
                        requester_user_id=db["member"]["user_id"], requester_project_id=db["member"]["project_id"])
    with pytest.raises(es.ExtensionValidationError):
        await es.update("mcp", personal["server"]["id"], {"transport": "sse"},
                        requester_user_id=db["member"]["user_id"], requester_project_id=db["member"]["project_id"])
    async with db["factory"]() as session:
        assert (await session.get(ChatMcpServer, personal["server"]["id"])).url == candidate["url"]


@pytest.mark.parametrize("domain_db", [1], indirect=True)
async def test_idempotent_org_install_releases_lookup_connection_before_effective_view(domain_db):
    db = domain_db
    candidate, created = await asyncio.wait_for(create_domain(db), timeout=3)
    first = created["server"]
    again = await asyncio.wait_for(
        mcp_settings.create(db["manager"], candidate["id"], domain_id=db["a"], label=db["suffix"]),
        timeout=3,
    )
    assert again["created"] is False
    assert again["server"]["id"] == first["id"]
    assert again["server"]["is_active"] is False
    assert again["server"]["server_is_active"] is True


def verified_actor(db, name="member", *, project_id=None, domain_id=None):
    principal = db[name]
    project_id = project_id or principal["project_id"]
    return {
        **principal, "credential_class": "keystone", "project_id": project_id,
        "domain_id": domain_id or db["project_domains"][project_id],
    }


async def test_personal_catalog_reuses_domain_connection_but_not_foreign_or_legacy_owner(domain_db):
    db = domain_db
    candidate = template("open")
    home = verified_actor(db)
    same_domain = verified_actor(db, project_id=db["second_project"])
    other_domain = verified_actor(db, project_id=db["foreign"]["project_id"])
    first = await mcp_settings.create(home, candidate["id"], label=db["suffix"])
    server_id = first["server"]["id"]
    db["ids"].append(server_id)
    assert first["server"]["owner_domain_id"] == db["a"]
    existing = await mcp_settings.create(same_domain, candidate["id"], label=db["suffix"])
    assert existing["created"] is False
    assert es.selection_fingerprint(existing["server"]) == es.selection_fingerprint(first["server"])
    assert existing["server"]["owner_project_id"] == home["project_id"]
    async with db["factory"]() as session:
        row = await session.get(ChatMcpServer, server_id)
        assert row.connection_scope_key == f"user:{home['user_id']}:domain:{db['a']}"
        assert row.owner_project_id == home["project_id"]
    for principal in (other_domain, verified_actor(db, "manager"), {**home, "credential_class": "personal_key"}):
        with pytest.raises(es.ExtensionForbidden):
            await mcp_settings.scoped_connection(principal, server_id)
    await mcp_settings.delete(same_domain, server_id)


async def test_domain_custom_tool_requires_exact_manager_and_live_selected_project(domain_db):
    db = domain_db
    manager, member = verified_actor(db, "manager"), verified_actor(db, project_id=db["second_project"])
    fields = {"name": "shared_http", "url": "https://tools.example/test"}
    with mcp_settings.actor(member), pytest.raises(es.ExtensionForbidden):
        await es.create("tool", fields, scope="domain", owner_user_id=member["user_id"],
                        owner_project_id=member["project_id"], owner_domain_id=db["a"])
    with mcp_settings.actor(manager):
        tool = await es.create("tool", fields, scope="domain", owner_user_id=manager["user_id"],
                               owner_project_id=manager["project_id"], owner_domain_id=db["a"])
    db["tool_ids"].append(tool["id"])
    with mcp_settings.actor(member):
        assert [row["id"] for row in await es.list_for_user("tool", **owner(member),
                                                           selected_ids=(tool["id"],))] == [tool["id"]]
        with pytest.raises(es.ExtensionForbidden):
            await es.delete("tool", tool["id"], requester_user_id=member["user_id"],
                            requester_project_id=member["project_id"], domain_id=db["a"])
    snapshot = {"mcp": [], "tools": [{"id": tool["id"], "config_fingerprint": es.selection_fingerprint(tool)}]}
    with es.actor_scope(human=False, mode="run", owner=(member["user_id"], member["project_id"], db["a"])):
        selected, _, _ = await es.validated_frozen_selection(snapshot, **owner(member))
        assert selected == (tool["id"],)
        db["project_domains"][member["project_id"]] = db["b"]
        selected, _, _ = await es.validated_frozen_selection(snapshot, **owner(member))
        assert selected == ()


async def test_personal_custom_tool_domain_and_origin_fallback_are_disjoint(domain_db):
    db = domain_db
    home, second = verified_actor(db), verified_actor(db, project_id=db["second_project"])
    fields = {"name": "personal_http", "url": "https://tools.example/test"}
    creation = {"scope": "user", "owner_user_id": home["user_id"], "owner_project_id": home["project_id"]}
    legacy = await es.create("tool", fields, **creation)
    with mcp_settings.actor(home):
        scoped = await es.create("tool", fields, **creation)
    ids = (legacy["id"], scoped["id"])
    db["tool_ids"].extend(ids)
    for principal, expected in ((home, ids), (second, (scoped["id"],)), ({**home, "domain_id": None}, (legacy["id"],))):
        with mcp_settings.actor(principal):
            rows = await es.list_for_user("tool", **owner(principal), selected_ids=ids)
        assert {row["id"] for row in rows} == set(expected)


async def test_domain_activation_and_oauth_shadow_an_active_origin_row(domain_db):
    db = domain_db
    member = verified_actor(db)
    second = verified_actor(db, project_id=db["second_project"])
    server = await es.create("mcp", {
        "name": "oauth-shadow", "url": "https://mcp.example/oauth", "auth_mode": "oauth",
    }, scope="global")
    server_id = server["id"]
    db["ids"].append(server_id)
    async with db["factory"]() as session, session.begin():
        for principal, domain_id, active in ((member, None, True), (second, db["a"], False)):
            session.add(ChatMcpActivation(
                mcp_server_id=server_id, owner_user_id=principal["user_id"], owner_project_id=principal["project_id"],
                owner_domain_id=domain_id, is_active=active, config_version=1, generation=1,
            ))
            session.add(ChatMcpOAuthConnection(
                id=str(uuid.uuid4()), mcp_server_id=server_id, owner_user_id=principal["user_id"],
                owner_project_id=principal["project_id"], owner_domain_id=domain_id,
                encrypted_tokens=mcp_oauth._encrypt({"access_token": "synthetic-token"}) if active else mcp_oauth._encrypt({}),
                status="active" if active else "revoked", server_config_version=1, credential_version=1,
            ))
    with mcp_settings.actor(member):
        row, = await es.list_for_user("mcp", **owner(member), selected_ids=(server_id,))
        assert not row["activation_is_active"] and not row["auth_connected"]
        store = mcp_oauth.SqlMcpConnectionStore()
        assert (await store.connection(server_id, Namespace(**owner(member)))).status == "revoked"
    with mcp_settings.actor({**member, "domain_id": None}):
        row, = await es.list_for_user("mcp", **owner(member), selected_ids=(server_id,))
        assert row["activation_is_active"] and row["auth_connected"]


async def test_personal_oauth_state_fences_callback_project_domain(domain_db):
    db = domain_db
    origin = verified_actor(db)
    second = verified_actor(db, project_id=db["second_project"])
    with mcp_settings.actor(origin):
        server = await es.create("mcp", {
            "name": "personal-oauth", "url": "https://mcp.example/oauth", "auth_mode": "oauth",
        }, scope="user", owner_user_id=origin["user_id"], owner_project_id=origin["project_id"], catalog_authorized=True)
    db["ids"].append(server["id"])
    state_hash = uuid.uuid4().hex
    store = mcp_oauth.SqlMcpConnectionStore()
    with mcp_settings.actor(second):
        await store.create_state(
            state_hash=state_hash, server_id=server["id"], namespace=Namespace(**owner(second)),
            payload={"synthetic": True}, expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
    assert await store.consume_state(state_hash) == {"synthetic": True}
    # A same-domain callback writes one credential, while preserving the actual initiating project.
    assert await store.complete_state(
        state_hash, token={"access_token": "synthetic-token"}, expires_at=None,
    ) == server["id"]
    with mcp_settings.actor(origin):
        row, = await es.list_for_user("mcp", **owner(origin), active_only=True, selected_ids=(server["id"],))
        assert row["credential_version"] == 1
        connection = await store.connection(server["id"], Namespace(**owner(origin)))
        assert connection.project_id == second["project_id"]
    state_hash = uuid.uuid4().hex
    with mcp_settings.actor(second):
        await store.create_state(
            state_hash=state_hash, server_id=server["id"], namespace=Namespace(**owner(second)),
            payload={"synthetic": True}, expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
    await store.consume_state(state_hash)
    db["project_domains"][second["project_id"]] = db["b"]
    with pytest.raises(mcp_oauth.McpOAuthError, match="management authority is no longer available"):
        await store.complete_state(state_hash, token={"access_token": "synthetic-token"}, expires_at=None)
    async with db["factory"]() as session:
        persisted = (await session.execute(select(ChatMcpOAuthConnection).where(
            ChatMcpOAuthConnection.mcp_server_id == server["id"],
        ))).scalar_one()
        assert persisted.owner_domain_id == db["a"]
        assert persisted.credential_version == 1
