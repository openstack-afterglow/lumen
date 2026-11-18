"""Agent Vault acceptance against migrated MariaDB with synthetic caller authority."""

import asyncio
import json
import os
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from lumen.auth import get_principal
from lumen.crypto import encrypt_chat_content
from lumen.db import close_db, get_session_factory, init_db
from lumen.main import app
from lumen.models.chat_db import ChatMemory, ChatMemoryOwnerLock, ChatWorkspace
from lumen.models.chat_jobs import ChatMemoryOutbox
from lumen.models.vault import VaultNote, VaultRevision
from lumen.plugins import memory_host
from lumen.services import graph, memory_store, provider_client, vault_migration, vault_store, vault_tools
from lumen.services.tool_runtime import bindings, contracts, dispatch
from lumen.services.tools import ToolContext

pytestmark = pytest.mark.integration


@pytest.fixture
async def owner():
    init_db(os.environ["DATABASE_URL"], pool_size=4, max_overflow=0)
    token = uuid4().hex
    scope = vault_store.VaultScope(f"vault-user-{token}", f"vault-domain-{token}", f"vault-project-{token}")
    try:
        yield scope
    finally:
        async with get_session_factory()() as session, session.begin():
            owned = select(VaultNote.id).where(VaultNote.user_id.in_((scope.user_id, f"other-{scope.user_id}")))
            await session.execute(delete(ChatMemoryOutbox).where(ChatMemoryOutbox.vault_note_id.in_(owned)))
            await session.execute(delete(VaultRevision).where(VaultRevision.note_id.in_(owned)))
            await session.execute(delete(VaultNote).where(VaultNote.id.in_(owned)))
            await session.execute(delete(ChatMemoryOwnerLock).where(ChatMemoryOwnerLock.user_id == scope.user_id))
            await session.execute(delete(ChatMemory).where(ChatMemory.user_id == scope.user_id))
            await session.execute(delete(ChatWorkspace).where(
                ChatWorkspace.owner_user_id.in_((scope.user_id, f"other-{scope.user_id}")),
            ))
        await close_db()


@pytest.fixture
async def dashboard(owner):
    identity = {"auth_type": "keystone", "credential_class": "keystone", "source": "web", "roles": ["member"],
                "scopes": (), "api_key_id": None, "is_system_admin": False, "user_id": owner.user_id,
                "ingress_verified": True,
                "domain_id": owner.domain_id, "project_id": owner.project_id}
    app.dependency_overrides[get_principal] = lambda: identity
    try:
        async with AsyncClient(transport=ASGITransport(app), base_url="http://test/v1/vault/") as client:
            yield client, identity
    finally:
        app.dependency_overrides.pop(get_principal, None)


async def test_note_rest_hash_revisions_scope_and_markdown(dashboard, owner, monkeypatch):
    client, identity = dashboard
    created = await client.post("notes", json={"kind": "user_note", "slug": "preference", "title": "Editor", "content": "Use tabs"})
    assert created.status_code == 201
    note = created.json()
    async with get_session_factory()() as session:
        stored = await session.get(VaultNote, note["id"])
        assert stored.content != note["content"] and stored.body_hash == note["hash"]
        assert stored.domain_id == owner.domain_id
    url = f"notes/{note['id']}"
    requests = [client.patch(url, json={"content": value, "expectedHash": note["hash"]}) for value in ("Use spaces", "Use four spaces")]
    winner, conflict = sorted(await asyncio.gather(*requests), key=lambda response: response.status_code)
    assert winner.is_success and conflict.status_code == 409 and conflict.json()["detail"] == "hash_mismatch"
    assert winner.json()["hash"] != note["hash"]
    assert (await client.get(url + "/revisions")).json()[1]["content"] == "Use tabs"
    identity["project_id"] += "-next"
    assert (await client.get(url)).json()["id"] == note["id"]
    result = await client.get("search", params={"kind": "user_note", "query": "SPACE"})
    assert [row["id"] for row in result.json()] == [note["id"]]
    result = await client.get("document")
    assert result.json()["filename"] == "memory.md"
    assert winner.json()["content"] in result.json()["content"]

    def no_decrypt(_value):
        raise AssertionError("unauthorized ciphertext was decrypted")

    monkeypatch.setattr(vault_store, "decrypt_chat_content", no_decrypt)
    identity["domain_id"] = "other-domain"
    assert (await client.get(url)).status_code == 404
    assert (await client.get(url + "/revisions")).status_code == 404
    assert (await client.get("search", params={"query": "Space"})).json() == []
    identity["domain_id"], identity["user_id"] = owner.domain_id, "other-user"
    assert (await client.get(url)).status_code == 404
    assert (await client.get("notes")).json() == []


@pytest.mark.parametrize("credential_class", ["personal_key", "service_key", "claude_gateway", "restricted"])
async def test_no_api_keys_or_reduced_sessions(dashboard, credential_class):
    client, identity = dashboard
    if credential_class == "restricted":
        identity["credential_restricted"] = True
    else:
        identity.update(credential_class=credential_class, auth_type="api_key", api_key_id=77)
    assert (await client.get("notes")).status_code == 403
    assert (await client.get("../memories")).status_code == 403


async def test_immutable_notes_and_singletons(dashboard):
    client, _identity = dashboard
    for kind in ("long_term", "log", "handoff", "worktree", "role_memory"):
        payload = {"kind": kind, "slug": "same", "title": "Review", "content": "Keep evidence", "mode": "append"}
        note = (await client.post("notes", json=payload)).json()
        denied = await client.patch(f"notes/{note['id']}", json={"content": "Overwrite", "expectedHash": note["hash"]})
        assert denied.status_code == 409 and denied.json()["detail"] == "append_only"
        assert (await client.post("notes", json=payload)).json()["detail"] == "append_only"
    assert (await client.get("short-term")).json() == {"exists": False}
    current = (await client.put("short-term", json={"content": "State"})).json()
    assert (await client.get("short-term")).json()["hash"] == current["hash"]
    assert (await client.put("short-term", json={"content": "Stale"})).json()["detail"] == "hash_mismatch"
    for path in ("profile", "coding-standards"):
        assert (await client.put(path, json={"content": "Personal documentation"})).is_success
        assert (await client.get(path)).json()["scope"] == "global"
    fake_secret = "sk-" + "A" * 24
    payload = {"kind": "knowledge", "slug": "secret-shape", "title": "Review", "content": "Body",
               "metadata": {"example": fake_secret}}
    result = await client.post("notes", json=payload)
    assert result.status_code == 422 and result.json() == {"detail": "secret_detected"}
    assert (await client.get("search", params={"query": "secret-shape"})).json() == []


async def test_legacy_import_and_recall_use_verified_domain(owner, dashboard, monkeypatch):
    client, identity = dashboard
    factory = get_session_factory()
    async with factory() as session, session.begin():
        workspace = ChatWorkspace(owner_user_id=owner.user_id, name="My workspace")
        foreign_workspace = ChatWorkspace(owner_user_id=f"other-{owner.user_id}", name="Foreign workspace")
        session.add_all([workspace, foreign_workspace])
        await session.flush()
        workspace_id, foreign_workspace_id = workspace.id, foreign_workspace.id
        account = ChatMemory(user_id=owner.user_id, scope="account", content=encrypt_chat_content("Account note"))
        project = ChatMemory(user_id=owner.user_id, project_id=owner.project_id, scope="project",
                             content=encrypt_chat_content("Domain note"))
        blocked = ChatMemory(user_id=owner.user_id, project_id="unresolved", scope="workspace",
                             workspace_id=workspace_id, content=encrypt_chat_content("Never hydrate"))
        private = ChatMemory(user_id=owner.user_id, project_id=owner.project_id, scope="workspace",
                             workspace_id=workspace_id, content=encrypt_chat_content("Workspace note"))
        session.add_all([account, project, blocked, private])
        await session.flush()
        ids = account.id, project.id, blocked.id, private.id
    original_decrypt = vault_migration.decrypt_chat_content

    def decrypt(value):
        plaintext = original_decrypt(value)
        assert plaintext != "Never hydrate"
        return plaintext

    def native(project):
        if project != owner.project_id:
            raise vault_migration.UnresolvedProject
        return owner.domain_id

    monkeypatch.setattr(vault_migration, "decrypt_chat_content", decrypt)
    monkeypatch.setattr(vault_migration, "_native_domain", native)
    assert await vault_migration.migrate_batch() == 4
    assert await vault_migration.migrate_batch() == 0
    async with factory() as session:
        blocked = await session.get(ChatMemory, ids[2])
        assert blocked.vault_migration_status is None and blocked.vault_migration_retry_at is not None
        assert await session.get(VaultNote, ids[2]) is None
    same_domain = replace(owner, project_id="other-project")
    assert {note["id"] for note in await vault_store.list_notes(same_domain)} == set(ids[:2])
    different_domain = replace(owner, domain_id="other-domain")
    assert {note["id"] for note in await vault_store.list_notes(different_domain)} == {ids[0]}
    same = await memory_store.hydrate_namespace_ids(ids=list(ids), user_id=owner.user_id,
                                                   project_id=same_domain.project_id, domain_id=owner.domain_id,
                                                   workspace_id=None)
    assert [note["content"] for note in same] == ["Account note", "Domain note"]
    foreign = await memory_store.hydrate_namespace_ids(ids=list(ids), user_id=owner.user_id,
                                                      project_id="other-project", domain_id="other-domain",
                                                      workspace_id=None)
    assert [note["id"] for note in foreign] == [ids[0]]
    private_id = ids[3]
    identity["project_id"] = same_domain.project_id
    params = {"workspace_id": workspace_id}
    default = (await client.get("notes")).json()
    assert {note["id"] for note in default} == set(ids[:2])
    for imported in default:
        route = f"notes/{imported['id']}"
        assert (await client.get(route)).json()["content"] == imported["content"]
        assert (await client.patch(route, json={"content": imported["content"], "expectedHash": imported["hash"]})).is_success
    listed = (await client.get("notes", params=params)).json()
    assert len(listed) == 1 and (listed[0]["id"], listed[0]["workspace_id"]) == (private_id, workspace_id)
    note_route = f"notes/{private_id}"
    assert (await client.get(note_route)).status_code == 404
    assert (await client.get(note_route, params=params)).json()["content"] == "Workspace note"
    assert (await client.get("search", params={**params, "query": "Workspace note"})).json()[0]["id"] == private_id
    updated = await client.patch(note_route, params=params, json={"content": "Private receipt", "expectedHash": listed[0]["hash"]})
    assert updated.is_success
    assert len((await client.get(note_route + "/revisions", params=params)).json()) == 2
    denied = await client.get("notes", params={"workspace_id": foreign_workspace_id})
    assert denied.status_code == 404 and denied.json()["detail"] == "workspace_not_found"
    payload = {"kind": "user_note", "slug": "manual", "title": "Manual", "content": "Body"}
    assert (await client.post("notes", json={**payload, "workspace_id": foreign_workspace_id})).status_code == 404
    assert (await client.post("notes", json={**payload, **params})).json()["workspace_id"] == workspace_id
    recalled = await memory_store.hydrate_namespace_ids(
        ids=[private_id], user_id=owner.user_id, project_id=same_domain.project_id,
        domain_id=owner.domain_id, workspace_id=workspace_id,
    )
    assert [row["content"] for row in recalled] == ["Private receipt"]
    assert not await memory_store.hydrate_namespace_ids(
        ids=[private_id], user_id=owner.user_id, project_id=same_domain.project_id,
        domain_id=owner.domain_id, workspace_id=foreign_workspace_id,
    )

    queried = set()

    async def search_ids(*, user_id, project_id, workspace_id, **_kwargs):
        assert user_id == owner.user_id
        queried.add((project_id, workspace_id))
        # Intentionally misroute candidates; the SQL source still controls disclosure.
        return [private_id, ids[2], ids[1], ids[0]]

    async def embedding(_query):
        return [0.5]

    monkeypatch.setattr(memory_host, "configured_index", lambda: SimpleNamespace(search_ids=search_ids))
    monkeypatch.setattr(memory_host, "embed_maintenance", embedding)
    for workspace, content in ((None, ["Domain note", "Account note"]),
                               (workspace_id, ["Private receipt", "Domain note", "Account note"])):
        assert await memory_host.recall_for_run(
            user_id=owner.user_id, project_id=same_domain.project_id, domain_id=owner.domain_id,
            workspace_id=workspace, include_account=True, strategy="semantic", query="note",
            limit=10, token_budget=200,
        ) == content
    assert queried == {(owner.domain_id, None), (None, None), (owner.domain_id, workspace_id)}
    identity["domain_id"] = different_domain.domain_id
    assert (await client.get(note_route, params=params)).status_code == 404


async def test_automatic_ops_and_dashboard_tool_dispatch(owner, monkeypatch):
    async with get_session_factory()() as session, session.begin():
        applied = await memory_store.apply_automatic_ops_in_transaction(
            session, ops=[{"op": "add", "category": "preference", "content": "Keeps receipts"},
                          {"op": "add", "category": "general", "content": "sk-" + "A" * 24}],
            user_id=owner.user_id, project_id=owner.project_id, domain_id=owner.domain_id,
        )
    assert applied == {"add": 1, "update": 0, "delete": 0}

    def current(user_id, project_id):
        assert (user_id, project_id) == (owner.user_id, owner.project_id)
        return {"roles": ["member"], "is_system_admin": False, "domain_id": owner.domain_id}

    monkeypatch.setattr(vault_store.auth, "_resolve_project_authority", current)
    run = SimpleNamespace(source="web", api_key_id=None, run_scope="persistent", parent_run_id=None,
                          capability_snapshot={"vault_domain_id": owner.domain_id, "vault_ingress_verified": True})
    ctx = ToolContext(run_id="synthetic-run", tool_call_id="tool-1", user_id=owner.user_id,
                      project_id=owner.project_id, vault_domain_id=vault_store.frozen_run_domain(run),
                      execution_hooks=object(), binding_session=contracts.ToolBindingSession(),
                      selected_tool_ids=(), selected_mcp_ids=())
    result = await dispatch.context_execute_result("memory_search", {"query": "receipts"}, ctx)
    assert result.status == "completed" and "Keeps receipts" in result.content
    result = await dispatch.context_execute_result("short_memory_write", {"content": "Save state"}, ctx)
    assert result.status == "completed" and json.loads(result.content)["content"] == "Save state"
    available = await bindings.v2_tool_bindings(ctx)
    assert available["agent_handoff"].definition.effect == "external_mutation"
    denied = await available["memory_search"].execute({"query": "receipts"}, replace(ctx, user_id="other-user"))
    assert denied.error_code == "vault_dashboard_required"
    run.api_key_id = 17
    assert vault_tools.bindings(replace(ctx, vault_domain_id=vault_store.frozen_run_domain(run))) == {}


async def test_dashboard_graph_tool_result_reaches_next_model_turn(owner, monkeypatch):
    async def authority(user_id, project_id):
        assert (user_id, project_id) == (owner.user_id, owner.project_id)
        return {"roles": ["member"], "is_system_admin": False, "domain_id": owner.domain_id}

    monkeypatch.setattr(vault_store.auth, "resolve_project_authority", authority)
    await vault_store.put_slot(
        owner, kind="short_term", slug="memory", title="Working state", content="Tracked in Vault",
        note_scope="domain", expected_hash=None,
    )
    requests = []

    async def stream(messages, **kwargs):
        requests.append(messages)
        if len(requests) == 1:
            assert any(tool["function"]["name"] == "short_memory_read" for tool in kwargs["tools"])
            delta = {"tool_calls": [{"index": 0, "id": "vault-read-1", "type": "function",
                                     "function": {"name": "short_memory_read", "arguments": "{}"}}]}
            reason = "tool_calls"
        else:
            assert any(message["role"] == "tool" and "Tracked in Vault" in message["content"] for message in messages)
            delta = {"content": "State recalled."}
            reason = "stop"

        async def chunks():
            yield provider_client.ProviderObject(choices=[{"delta": delta, "finish_reason": reason}])

        return chunks()

    monkeypatch.setattr(provider_client, "acompletion_stream", stream)
    events = [event async for event in graph.stream(
        model="synthetic", messages=[{"role": "user", "content": "Read my working state."}],
        user_id=owner.user_id, project_id=owner.project_id, run_id="dashboard-vault-graph",
        vault_domain_id=owner.domain_id, execution_hooks=object(), selected_tool_ids=(), selected_mcp_ids=(),
    )]
    assert len(requests) == 2
    assert any(event.get("text") == "State recalled." for event in events)
