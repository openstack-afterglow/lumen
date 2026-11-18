"""Domain history is shared by one user; runs, replay, and assets are not."""

from __future__ import annotations

import os
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from lumen_plugin_api.contracts import ExecutionContext

from lumen import auth
from lumen.api import conversations as history_api
from lumen.api.completions import _active_context_compaction_run_id, list_conversation_runs
from lumen.db import close_db, get_session_factory, init_db
from lumen.main import app
from lumen.models.chat_assets import ChatAsset, ChatRunAsset
from lumen.models.chat_db import ChatApiKey, ChatConversation, ChatMessage, ChatMessageGraph
from lumen.models.chat_runs import ChatRun
from lumen.plugins.tools_host import ToolsConversationAccess
from lumen.scripts.migrate import migrate
from lumen.services import api_key_store, assets, context_store, workspace_store
from lumen.services import conversation_store as history
from lumen.services.durable_runs import admission, queries
from lumen.services.durable_runs.errors import DurableRunConflict, DurableRunInputError, DurableRunNotFound

pytestmark = pytest.mark.integration


@pytest.fixture
async def scopes():
    nonce = uuid4().hex
    origin = {"user_id": f"user-{nonce}", "project_id": f"origin-{nonce}", "domain_id": f"domain-{nonce}"}
    current = {**origin, "project_id": f"current-{nonce}"}
    init_db(os.environ["DATABASE_URL"], pool_size=2, max_overflow=0)
    try:
        yield origin, current
    finally:
        await close_db()


def _principal(scope: dict) -> dict:
    return {**scope, "auth_type": "keystone", "credential_class": "keystone"}


async def test_same_user_domain_history_fork_cursor_and_legacy_origin(scopes):
    origin, current = scopes
    legacy_owner = {**origin, "domain_id": None}
    foreign_domain = {**current, "domain_id": "foreign-domain"}
    other_user = {**current, "user_id": "other-user"}
    principal = _principal(current)
    source = await history.create_conversation(title="Domain example", model_name=None, **origin)
    legacy = await history.create_conversation(title="Legacy example", model_name=None, **legacy_owner)
    foreign = await history.create_conversation(title="Foreign example", model_name=None, **foreign_domain)
    other = await history.create_conversation(title="Other example", model_name=None, **other_user)
    source_id = source["id"]

    visible = await history_api.list_conversations(token_info=principal)
    assert [item["id"] for item in visible] == [source_id]
    found = await history_api.search_conversations(q="example", limit=20, token_info=principal)
    assert [item["id"] for item in found] == [source_id]
    loaded_legacy = await history.get_conversation(legacy["id"], **origin)
    assert loaded_legacy["id"] == legacy["id"]
    assert loaded_legacy["title"] == legacy["title"]
    assert loaded_legacy["project_id"] == origin["project_id"]
    for denied in (other_user, foreign_domain, {**origin, "domain_id": None}):
        with pytest.raises(history.ConversationForbidden):
            await history.get_conversation(source_id, **denied)
    for credential in ({"credential_class": "personal_key"}, {"credential_restricted": True}):
        with pytest.raises(HTTPException) as denied:
            await history_api.get_conversation(source_id, token_info={**_principal(origin), **credential})
        assert denied.value.status_code == 403
    with pytest.raises(history.ConversationForbidden):
        await history.get_conversation(legacy["id"], **current)
    legacy_message = await history.add_message(legacy["id"], role="user", content="legacy question", set_leaf=True)
    legacy_fork = await history.fork_conversation(legacy["id"], message_id=legacy_message["id"], **origin)
    loaded_fork = await history.get_conversation(legacy_fork["id"], **legacy_owner)
    assert loaded_fork["id"] == legacy_fork["id"]
    assert loaded_fork["project_id"] == origin["project_id"]
    assert loaded_fork["active_leaf_id"] == legacy_message["id"]
    with pytest.raises(history.ConversationForbidden):
        await history.get_conversation(legacy_fork["id"], **current)

    question = await history.add_message(source_id, role="user", content="shared question", set_leaf=True)
    answer = await history.add_message(
        source_id, role="assistant", content="shared answer", parent_id=question["id"], set_leaf=True,
    )
    sibling = await history.add_message(
        source_id, role="assistant", content="source sibling", parent_id=question["id"],
    )
    fork = await history_api.fork_conversation(
        source_id, history_api.ForkRequest(message_id=answer["id"]), token_info=principal,
    )
    fork_id = fork["id"]
    assert fork["project_id"] == current["project_id"]
    assert fork["parent_conversation_id"] == source_id
    assert fork["forked_from_message_id"] == answer["id"]
    factory = get_session_factory()
    assert factory is not None
    async with factory() as session:
        original = await session.get(ChatConversation, source_id)
        fork_row = await session.get(ChatConversation, fork_id)
        graph = await session.get(ChatMessageGraph, original.graph_id)
        graph_id = graph.id
        assert fork_row.graph_id == graph_id
        assert fork_row.domain_id == graph.domain_id == origin["domain_id"]
        assert graph.project_id == original.project_id == origin["project_id"]
    pending, _ = await migrate(os.environ["DATABASE_URL"], apply=False)
    assert pending == []

    page = await history_api.list_messages(fork_id, anchor="latest", cursor=None, limit=1, token_info=principal)
    previous = await history_api.list_messages(
        fork_id, anchor=None, cursor=page["before_cursor"], limit=1, token_info=_principal(origin),
    )
    assert [item["id"] for item in page["messages"]] == [answer["id"]]
    assert [item["id"] for item in previous["messages"]] == [question["id"]]
    with pytest.raises(history.ConversationNotFound):
        await history.path_ending_at(fork_id, message_id=sibling["id"], **current)
    with pytest.raises(history.ConversationForbidden):
        await history.fork_conversation(fork_id, message_id=answer["id"], **foreign_domain)
    source_context = await context_store.load_context_source(conversation_id=source_id, temp_thread_id=None, **current)
    assert source_context["message_ids"] == [str(question["id"]), str(answer["id"])]

    workspace = await workspace_store.create_workspace(owner_user_id=current["user_id"], name="History workspace")
    assert (await history.set_workspace(source_id, workspace_id=workspace["id"], **current))["workspace_id"] == workspace["id"]
    with pytest.raises(history.ConversationForbidden):
        await history.set_workspace(source_id, workspace_id=None, **foreign_domain)
    assert (await history.update_title(source_id, title="New title", **current))["title"] == "New title"
    switched = await history.set_active_leaf(fork_id, message_id=question["id"], **origin)
    assert switched["history_revision"] > page["history_revision"]
    with pytest.raises(HTTPException) as stale:
        await history_api.list_messages(
            fork_id, anchor=None, cursor=page["before_cursor"], limit=1, token_info=principal,
        )
    assert stale.value.status_code == 409

    await history.delete_conversation(source_id, **current)
    assert [message["id"] for message in await history.list_messages(fork_id, **origin)] == [question["id"]]
    await history.delete_conversation(fork_id, **origin)
    async with factory() as session:
        assert await session.get(ChatMessageGraph, graph_id) is None
        assert await session.get(ChatMessage, answer["id"]) is None
    for conversation, owner in (
        (legacy_fork, legacy_owner), (legacy, legacy_owner), (foreign, foreign_domain), (other, other_user),
    ):
        await history.delete_conversation(conversation["id"], **owner)
    await workspace_store.delete_workspace(workspace["id"], user_id=current["user_id"])


async def test_history_http_resolves_current_project_domain_without_principal_override(scopes, monkeypatch):
    origin, current = scopes
    user_id = origin["user_id"]
    home_domain = origin["domain_id"]
    foreign = {"user_id": user_id, "project_id": str(uuid4()), "domain_id": str(uuid4())}
    projects = {scope["project_id"]: scope["domain_id"] for scope in (origin, current, foreign)}
    roles = [{"id": "member-id", "name": "member", "domain_id": None}]

    def token_data(token, *, include_catalog):
        assert include_catalog is False
        return {"token": {
            "methods": ["password"],
            "user": {"id": user_id, "name": "fixture", "domain": {"id": home_domain}},
            "project": {"id": token, "name": "fixture", "domain": {"id": projects[token]}},
            "roles": roles,
            "issued_at": "2026-01-01T00:00:00Z",
            "expires_at": "2099-01-01T00:00:00Z",
        }}

    def get(path):
        if path == "/roles":
            return None, {"roles": roles}
        if path == "/role_inferences":
            return None, {"role_inferences": []}
        query = parse_qs(urlsplit(path).query)
        project_id, = query["scope.project.id"]
        assert project_id in projects and query["user.id"] == [user_id]
        return None, {"role_assignments": [{
            "scope": {"project": {"id": project_id}}, "user": {"id": user_id}, "role": {"id": "member-id"},
        }]}

    monkeypatch.setattr(auth, "_get_admin_ks_client", lambda: SimpleNamespace(
        get=get,
        tokens=SimpleNamespace(get_token_data=token_data),
        users=SimpleNamespace(get=lambda value: SimpleNamespace(id=value, enabled=True, domain_id=home_domain)),
        projects=SimpleNamespace(get=lambda value: SimpleNamespace(id=value, enabled=True, domain_id=projects[value])),
        domains=SimpleNamespace(get=lambda value: SimpleNamespace(id=value, enabled=True)),
    ))

    conversation = await history.create_conversation(title="Domain proof", model_name=None, **origin)
    conversation_id = conversation["id"]
    key_ids = []
    factory = get_session_factory()
    assert factory is not None
    try:
        async with factory() as db, db.begin():
            credentials = []
            for kind, ownership in (("personal", {"owner_user_id": user_id}), ("service", {"owner_domain_id": home_domain})):
                row, raw = api_key_store._new_key(
                    "History acceptance", ("models:read", "native:conversations:read"), expires_at=None,
                    key_kind=kind, credential_kind="api_key", created_by_user_id=user_id, **ownership,
                )
                db.add(row)
                await db.flush()
                key_ids.append(row.id)
                credentials.append(raw)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            for scope in (origin, current, foreign):
                headers = {"X-Auth-Token": scope["project_id"], "X-Project-Id": scope["project_id"]}
                listed = await client.get("/v1/conversations", headers=headers)
                assert listed.status_code == 200, listed.text
                assert [item["id"] for item in listed.json()] == ([] if scope is foreign else [conversation_id])
                detail = await client.get(f"/v1/conversations/{conversation_id}", headers=headers)
                assert detail.status_code == (403 if scope is foreign else 200), detail.text
            for raw in credentials:
                headers = {"X-API-Key": raw, "X-Project-Id": origin["project_id"]}
                assert (await client.get("/v1/models", headers=headers)).status_code == 200
                assert (await client.get("/v1/conversations", headers=headers)).status_code == 403
                assert (await client.get(f"/v1/conversations/{conversation_id}", headers=headers)).status_code == 403
    finally:
        await history.delete_conversation(conversation_id, **origin)
        async with factory() as db, db.begin():
            for key_id in key_ids:
                await db.delete(await db.get(ChatApiKey, key_id))


@pytest.mark.usefixtures("current_project_authority")
async def test_cross_project_run_attachment_and_replay_keep_selected_project(scopes, monkeypatch):
    origin, current = scopes
    origin_run_scope = {key: origin[key] for key in ("user_id", "project_id")}
    current_run_scope = {key: current[key] for key in ("user_id", "project_id")}
    source = await history.create_conversation(title=None, model_name=None, **origin)
    source_id = source["id"]
    factory = get_session_factory()
    assert factory is not None
    origin_asset_id, current_asset_id = str(uuid4()), str(uuid4())
    async with factory() as session, session.begin():
        for asset_id, owner in ((origin_asset_id, origin_run_scope), (current_asset_id, current_run_scope)):
            session.add(ChatAsset(
                id=asset_id, **owner, object_key=f"scope-test/{asset_id}", original_name="image.png",
                mime_type="image/png", size_bytes=16, sha256="a" * 64, status="clean",
                media_metadata={"width": 1, "height": 1},
            ))

    async def no_provider_lock(*_args, **_kwargs):
        return None

    async def no_wake(_run_id):
        return None

    monkeypatch.setattr(admission, "_lock_run_configurations", no_provider_lock)
    monkeypatch.setattr(admission, "wake_run", no_wake)
    snapshot = {
        "provider_id": None, "model_id": None, "provider_name": "fixture", "model_name": "fixture",
        "config_version_hash": "a" * 64, "history_domain_id": current["domain_id"],
    }

    async def admit(asset_id):
        input_parts = [{"type": "image", "asset_id": asset_id}]
        return await admission.create_persistent_run(
            **current, client_request_id=str(uuid4()), intent={"asset": asset_id}, conversation_id=source_id,
            model_name="fixture", agent_id=None, user_content="", user_parts=input_parts,
            request_payload={"features": {}, "input_parts": input_parts}, capability_snapshot=snapshot, pricing_snapshot={},
        )

    with pytest.raises(DurableRunInputError, match="input asset is not owned by this project"):
        await admit(origin_asset_id)
    assert (await history.get_active_path(source_id, **current))["active_leaf_id"] is None
    with pytest.raises(assets.AssetError, match="asset forbidden"):
        await assets.signed_download_url(asset_id=origin_asset_id, **current_run_scope)
    run = await admit(current_asset_id)
    run_id = run.run_id
    async with factory() as session:
        persisted = await session.get(ChatRun, run_id)
        user_message_id = persisted.user_message_id
        assert persisted.project_id == current["project_id"]
        assert await session.get(ChatRunAsset, (run_id, current_asset_id, "input")) is not None

    active = await list_conversation_runs(source_id, active=True, token_info=_principal(current))
    assert [item.run_id for item in active] == [run_id]
    assert await list_conversation_runs(source_id, active=True, token_info=_principal(origin)) == []
    with pytest.raises(history.ConversationRunActive):
        await history.delete_conversation(source_id, **origin)
    with pytest.raises(DurableRunConflict, match="conversation_run_active"):
        await admit(current_asset_id)
    with pytest.raises(DurableRunNotFound):
        await queries.owned_events(run_id=run_id, after_seq=0, **origin_run_scope)
    assert await history.list_messages_for_run(run_id, **origin_run_scope) == []
    assert [item["id"] for item in await history.list_messages_for_run(run_id, **current_run_scope)] == [user_message_id]
    assert (await assets.get_asset(asset_id=current_asset_id, **current_run_scope))["id"] == current_asset_id
    with pytest.raises(assets.AssetError, match="asset forbidden"):
        await assets.get_asset(asset_id=current_asset_id, **origin_run_scope)
    assert (await history.list_message_page(source_id, **origin))["messages"][0]["execution"] is None
    assert (await history.list_message_page(source_id, **current))["messages"][0]["execution"]["run_id"] == run_id
    tool = ToolsConversationAccess()
    assert (await tool.read(source_id, ExecutionContext(run_id=run_id, **current_run_scope)))["message_count"] == 1
    with pytest.raises(history.ConversationForbidden):
        await tool.read(source_id, ExecutionContext(run_id=run_id, **origin_run_scope))

    async with factory() as session, session.begin():
        persisted = await session.get(ChatRun, run_id)
        persisted.run_kind = "compaction"
    assert await _active_context_compaction_run_id(conversation_id=source_id, **current_run_scope) == run_id
    assert await _active_context_compaction_run_id(conversation_id=source_id, **origin_run_scope) is None
    async with factory() as session, session.begin():
        persisted = await session.get(ChatRun, run_id)
        persisted.status = "failed"

    async def replay(scope):
        return await admission.create_run(
            **scope, client_request_id=str(uuid4()), intent={"source_run": run_id}, conversation_id=source_id,
            temp_thread_id=None, model_name="fixture", agent_id=None, user_message_id=user_message_id,
            request_payload={"features": {}, "input_parts": [{"type": "image", "asset_id": current_asset_id}]},
            capability_snapshot=snapshot, pricing_snapshot={}, expected_parent_id=user_message_id,
        )

    with pytest.raises(DurableRunInputError, match="input asset is not owned by this project"):
        await replay(origin)
    replayed = await replay(current)
    async with factory() as session, session.begin():
        retry_run = await session.get(ChatRun, replayed.run_id)
        assert retry_run.project_id == current["project_id"]
        assert await session.get(ChatRunAsset, (retry_run.id, current_asset_id, "input")) is not None
        await session.delete(retry_run)
    await history.delete_conversation(source_id, **current)
    async with factory() as session, session.begin():
        await session.delete(await session.get(ChatRun, run_id))
        await session.flush()
        for asset_id in (origin_asset_id, current_asset_id):
            await session.delete(await session.get(ChatAsset, asset_id))
