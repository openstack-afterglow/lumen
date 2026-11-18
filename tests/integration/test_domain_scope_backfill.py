"""Migration 036 behavior on a disposable native MariaDB fixture."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, select, text

from lumen import auth
from lumen.api import extension_archives
from lumen.crypto import decrypt_extension_archive, encrypt_llm_provider_key
from lumen.db import close_db, get_session_factory, init_db
from lumen.models.chat_db import (
    ChatAgent,
    ChatConversation,
    ChatCustomTool,
    ChatExtensionArchive,
    ChatMcpActivation,
    ChatMcpOAuthConnection,
    ChatMcpOAuthRequest,
    ChatMcpServer,
    ChatMessageGraph,
)
from lumen.services import domain_scope_backfill as scope_backfill
from lumen.services.domain_scope import personal_mcp_scope_key

pytestmark = pytest.mark.integration


@pytest.fixture
async def db():
    init_db(os.environ["DATABASE_URL"], pool_size=3, max_overflow=0)
    factory = get_session_factory()
    assert factory is not None
    suffix = uuid.uuid4().hex
    user = f"scope-user-{suffix}"
    domain = f"scope-domain-{suffix}"
    projects = [f"scope-{part}-{suffix}" for part in ("a", "b", "missing")]
    try:
        yield factory, user, domain, projects
    finally:
        async with factory() as session, session.begin():
            server_ids = select(ChatMcpServer.id).where(ChatMcpServer.owner_user_id == user)
            for model in (ChatMcpOAuthConnection, ChatMcpOAuthRequest, ChatMcpActivation):
                await session.execute(delete(model).where(model.mcp_server_id.in_(server_ids)))
            await session.execute(text(
                "DELETE FROM chat_mcp_credentials WHERE owner_user_id = :user"
            ), {"user": user})
            await session.execute(delete(ChatExtensionArchive).where(ChatExtensionArchive.domain_id == domain))
            for model in (ChatMcpServer, ChatCustomTool, ChatAgent):
                await session.execute(delete(model).where(model.owner_user_id == user))
            for model in (ChatConversation, ChatMessageGraph):
                await session.execute(delete(model).where(model.user_id == user))
        await close_db()


async def _seed(db):
    factory, user, _, (origin, other, unresolved) = db
    now = datetime(2026, 11, 19, 8)
    async with factory() as session, session.begin():
        for project in (origin, unresolved):
            graph_id = str(uuid.uuid4())
            session.add(ChatMessageGraph(id=graph_id, user_id=user, project_id=project))
            await session.flush()
            session.add(ChatConversation(id=str(uuid.uuid4()), graph_id=graph_id, user_id=user, project_id=project))
        servers = []
        tools = []
        for offset, project in enumerate((origin, other)):
            timestamp = now + timedelta(seconds=offset)
            server = ChatMcpServer(
                scope="user", name="Synthetic connection", template_key="scope-fixture",
                connection_label=f"Workspace {offset}",
                owner_user_id=user, owner_project_id=project, project_id=project,
                connection_scope_key=personal_mcp_scope_key(user, project, None),
                url=f"https://example.com/config-{offset}", auth_mode="oauth", config_version=2 + offset,
                encrypted_oauth_client_secret=encrypt_llm_provider_key(f"synthetic-client-{offset}"),
                created_at=now, updated_at=timestamp,
            )
            tool = ChatCustomTool(
                scope="user", name=f"scope_tool_{offset}", description=f"fixture {offset}", owner_user_id=user,
                owner_project_id=project, project_id=project,
                url=("HTTPS://EXAMPLE.COM:443/tool" if offset == 0 else "https://example.com/tool"),
                created_at=now, updated_at=timestamp,
            )
            session.add_all([server, tool])
            await session.flush()
            servers.append(server)
            tools.append(tool)
            session.add_all([
                ChatMcpActivation(mcp_server_id=server.id, owner_user_id=user, owner_project_id=project,
                                  config_version=server.config_version, is_active=True, generation=2,
                                  created_at=now, updated_at=timestamp),
                ChatMcpOAuthConnection(id=str(uuid.uuid4()), mcp_server_id=server.id, owner_user_id=user,
                                       owner_project_id=project, encrypted_tokens=encrypt_llm_provider_key(
                                           f"synthetic-token-{offset}"), server_config_version=server.config_version,
                                       created_at=now, updated_at=timestamp),
            ])
        session.add(ChatMcpOAuthRequest(
            id=str(uuid.uuid4()), state_hash=uuid.uuid4().hex * 2, mcp_server_id=servers[0].id,
            owner_user_id=user, owner_project_id=origin, encrypted_payload=encrypt_llm_provider_key("synthetic-request"),
            expires_at=now + timedelta(hours=1), server_config_version=servers[0].config_version,
        ))
        await session.execute(text("""
            INSERT INTO chat_mcp_credentials (mcp_server_id, owner_user_id, owner_project_id, encrypted_values)
            VALUES (:server, :user, :project, :ciphertext)
        """), {"server": servers[0].id, "user": user, "project": origin,
                 "ciphertext": encrypt_llm_provider_key("synthetic-legacy")})
        agent = ChatAgent(owner_user_id=user, project_id=origin, name="fixture",
                          mcp_ids=[server.id for server in servers], tool_ids=[tool.id for tool in tools])
        session.add(agent)
        await session.flush()
        return agent.id, [server.id for server in servers], [tool.id for tool in tools]


async def test_domain_backfill_keeps_newest_archives_and_restores(db, monkeypatch):
    factory, user, domain, (origin, other, unresolved) = db
    agent_id, (old_server, new_server), (old_tool, new_tool) = await _seed(db)
    inspected = []

    def get_project(project_id):
        inspected.append(project_id)
        if project_id == unresolved:
            # A successful HTTP response with the wrong ID is not an exact identity proof.
            return SimpleNamespace(id=other, domain_id=domain, enabled=True)
        assert project_id in {origin, other}
        return SimpleNamespace(id=project_id, domain_id=domain, enabled=True)

    monkeypatch.setattr(auth, "_get_admin_ks_client", lambda: SimpleNamespace(projects=SimpleNamespace(get=get_project)))
    report = await scope_backfill.backfill_domain_scope()
    assert report.lock_acquired
    assert report.projects_resolved == 2 and report.projects_unresolved >= 1
    assert [project for project in inspected if project in {origin, other, unresolved}] == [origin, other, unresolved]
    assert report.archived_mcp_servers == report.archived_custom_tools == 1
    async with factory() as session:
        convs = {c.project_id: c.domain_id for c in await session.scalars(
            select(ChatConversation).where(ChatConversation.user_id == user)
        )}
        assert convs == {origin: domain, unresolved: None}
        agent = await session.get(ChatAgent, agent_id)
        assert (agent.domain_id, agent.mcp_ids, agent.tool_ids) == (domain, [new_server], [new_tool])
        winner = await session.get(ChatMcpServer, new_server)
        assert (winner.config_version, winner.owner_domain_id, winner.owner_project_id) == (3, domain, other)
        assert winner.connection_scope_key == personal_mcp_scope_key(user, other, domain)
        oauth = await session.scalar(select(ChatMcpOAuthConnection).where(ChatMcpOAuthConnection.mcp_server_id == new_server))
        assert oauth.owner_domain_id == domain and oauth.server_config_version == 3
        assert await session.get(ChatMcpServer, old_server) is None
        assert await session.get(ChatCustomTool, old_tool) is None
        archived = (await session.scalars(select(ChatExtensionArchive).where(ChatExtensionArchive.domain_id == domain))).all()
        assert {(row.kind, row.original_id, row.canonical_id) for row in archived} == {
            ("mcp", old_server, new_server), ("tool", old_tool, new_tool),
        }
        mcp_archive = next(row for row in archived if row.kind == "mcp")
        assert "synthetic-" not in mcp_archive.encrypted_snapshot
        payload = decrypt_extension_archive(mcp_archive.encrypted_snapshot)
        assert '"config_version": 2' in payload and '"oauth_requests"' in payload and '"legacy_credentials"' in payload
        mcp_archive_id = mcp_archive.id

    async def authority(principal, requested_domain, *, manage=False):
        if principal["project_id"] == unresolved or principal["domain_id"] != requested_domain:
            raise HTTPException(403, "Current project membership required")
        if manage and not principal.get("manager"):
            raise HTTPException(403, "Current manager grant required")
        return {"domain_id": requested_domain, "is_member": True, "can_manage": manage}

    monkeypatch.setattr(extension_archives.mcp_settings, "authority", authority)
    owner = {"credential_class": "keystone", "user_id": user, "project_id": other, "domain_id": domain}
    items = (await extension_archives._list(owner, None, 100, None))["items"]
    assert {item["original_id"] for item in items} == {old_server, old_tool}
    assert set(items[0]) == {"id", "kind", "original_id", "canonical_id", "name", "archived_at"}
    assert (await extension_archives._list({**owner, "user_id": "somebody-else"}, None, 100, None))["items"] == []
    assert (await extension_archives._list({**owner, "domain_id": "foreign"}, None, 100, None))["items"] == []
    assert (await extension_archives._list({**owner, "manager": True}, domain, 100, None))["items"] == []
    with pytest.raises(HTTPException, match="membership"):
        await extension_archives._list({**owner, "project_id": unresolved}, None, 100, None)
    with pytest.raises(HTTPException, match="manager grant"):
        await extension_archives._list(owner, domain, 100, None)
    with pytest.raises(HTTPException) as unauthorized:
        await extension_archives._restore({**owner, "user_id": "somebody-else"}, mcp_archive_id, None)
    assert unauthorized.value.status_code == 404
    assert await extension_archives._restore(owner, mcp_archive_id, None) == {"kind": "mcp", "id": old_server}
    tool_archive_id = next(item["id"] for item in items if item["kind"] == "tool")
    assert await extension_archives._restore(owner, tool_archive_id, None) == {"kind": "tool", "id": old_tool}
    async with factory() as session:
        restored = await session.get(ChatMcpServer, old_server)
        restored_token = await session.scalar(select(ChatMcpOAuthConnection).where(
            ChatMcpOAuthConnection.mcp_server_id == old_server,
        ))
        restored_request = await session.scalar(select(ChatMcpOAuthRequest).where(ChatMcpOAuthRequest.mcp_server_id == old_server))
        restored_tool = await session.get(ChatCustomTool, old_tool)
        assert restored.owner_project_id == origin and restored.owner_domain_id == domain
        assert restored.connection_label == "Workspace 0" and restored.url.endswith("/config-0")
        assert restored_token.owner_domain_id == domain and restored_token.server_config_version == restored.config_version
        assert restored_request.status == "expired"
        assert restored_tool.name == "scope_tool_0" and restored_tool.domain_id == domain

    inspected.clear()
    second = await scope_backfill.backfill_domain_scope()
    assert second.projects_resolved == second.archived_mcp_servers == second.archived_custom_tools == 0
    assert second.projects_unresolved == report.projects_unresolved
    assert [project for project in inspected if project in {origin, other, unresolved}] == [unresolved]


async def test_parallel_backfill_does_not_take_held_advisory_lock(db):
    factory, _, _, _ = db
    engine = factory.kw["bind"]
    async with engine.connect() as holder:
        parameters = {"name": scope_backfill._LOCK}
        assert await holder.scalar(text("SELECT GET_LOCK(:name, 0)"), parameters) == 1
        try:
            async def unexpected(project_id):
                pytest.fail(f"Keystone lookup under an exclusive lock: {project_id}")

            result = await scope_backfill.backfill_domain_scope(resolve=unexpected)
            assert not result.lock_acquired and result.projects_resolved == 0
        finally:
            assert await holder.scalar(text("SELECT RELEASE_LOCK(:name)"), parameters) == 1


async def test_published_server_archives_duplicate_approvals_and_oauth(db):
    factory, user, domain, (origin, other, _) = db
    now = datetime(2026, 11, 19, 9)
    tokens = [encrypt_llm_provider_key(f"synthetic-published-token-{suffix}") for suffix in ("old", "new")]
    async with factory() as session, session.begin():
        server = ChatMcpServer(
            scope="global", name="Published fixture", owner_user_id=user, auth_mode="oauth",
            url="https://example.com/mcp", config_version=5,
        )
        session.add(server)
        await session.flush()
        server_id = server.id
        oauth_ids = [str(uuid.uuid4()), str(uuid.uuid4())]
        for offset, project in enumerate((origin, other)):
            timestamp = now + timedelta(seconds=offset)
            session.add_all([
                ChatMcpActivation(mcp_server_id=server_id, owner_user_id=user, owner_project_id=project,
                                  is_active=bool(offset), config_version=5, generation=offset + 3,
                                  created_at=timestamp, updated_at=timestamp),
                ChatMcpOAuthConnection(id=oauth_ids[offset], mcp_server_id=server_id, owner_user_id=user,
                                       owner_project_id=project, encrypted_tokens=tokens[offset],
                                       server_config_version=5, credential_version=offset + 4,
                                       created_at=timestamp, updated_at=timestamp),
            ])

    async def resolve(project_id):
        assert project_id in {origin, other}
        return domain

    from lumen.services import extension_archive as archives

    report = await scope_backfill.backfill_domain_scope(resolve=resolve)
    assert report.projects_resolved == 2
    async with factory() as session:
        winners = {}
        for model in (ChatMcpActivation, ChatMcpOAuthConnection):
            row = (await session.scalars(select(model).where(model.mcp_server_id == server_id))).one()
            assert (row.owner_project_id, row.owner_domain_id) == (other, domain)
            winners[model] = row
        assert winners[ChatMcpOAuthConnection].encrypted_tokens == tokens[1]
        assert winners[ChatMcpActivation].is_active
        archived = (await session.scalars(select(ChatExtensionArchive).where(
            ChatExtensionArchive.original_id == server_id, ChatExtensionArchive.kind == "mcp",
        ))).all()
        assert {row.entry_type for row in archived} == {"oauth_connection", "activation"}
        for row in archived:
            blob = decrypt_extension_archive(row.encrypted_snapshot)
            assert origin in blob and now.isoformat() in blob
            if row.entry_type == "oauth_connection":
                assert tokens[0] in blob and oauth_ids[0] in blob
            else:
                assert '"generation": 3' in blob and '"config_version": 5' in blob

    for row in archived:
        assert await archives.restore_archive(archive_id=row.id, user_id=user, domain_id=domain) == {
            "kind": "mcp", "id": server_id,
        }
        with pytest.raises(archives.ArchiveNotFound):
            await archives.restore_archive(archive_id=row.id, user_id=user, domain_id=domain)
    async with factory() as session:
        activation = (await session.scalars(
            select(ChatMcpActivation).where(ChatMcpActivation.mcp_server_id == server_id)
        )).one()
        oauth = (await session.scalars(
            select(ChatMcpOAuthConnection).where(ChatMcpOAuthConnection.mcp_server_id == server_id)
        )).one()
        assert (oauth.id, oauth.encrypted_tokens, oauth.owner_project_id, oauth.created_at) == (
            oauth_ids[0], tokens[0], origin, now,
        )
        assert oauth.credential_version > winners[ChatMcpOAuthConnection].credential_version
        assert not activation.is_active and activation.owner_project_id == origin
        assert activation.generation > winners[ChatMcpActivation].generation
    displaced = await archives.list_archives(user_id=user, domain_id=domain, limit=10, before_id=None)
    assert len(displaced) == 2 and all(row["original_id"] == server_id for row in displaced)
    assert (await scope_backfill.backfill_domain_scope(resolve=resolve)).projects_resolved == 0


async def test_backfill_preserves_multi_account_groups_and_restored_rows(db):
    factory, user, domain, (origin, other, third) = db
    now = datetime(2026, 11, 19, 10)

    async def add_server(session, project, label, offset):
        row = ChatMcpServer(
            scope="user", name="Multi-account fixture", template_key="multi-fixture", connection_label=label,
            owner_user_id=user, owner_project_id=project, project_id=project,
            connection_scope_key=personal_mcp_scope_key(user, project, None),
            url="https://example.com/mcp", created_at=now, updated_at=now + timedelta(seconds=offset),
        )
        session.add(row)
        await session.flush()
        return row.id

    async def resolve(project_id):
        assert project_id in {origin, other, third}
        return domain

    async with factory() as session, session.begin():
        work_id = await add_server(session, origin, "Work", 0)
        home_id = await add_server(session, origin, "Home", 1)
        latest_work = await add_server(session, other, "Work", 2)
        unknown = await add_server(session, third, "Unmatched", 3)

    report = await scope_backfill.backfill_domain_scope(resolve=resolve)
    assert report.items_failed == 0 and report.archived_mcp_servers == 2
    async with factory() as session:
        live = (await session.scalars(select(ChatMcpServer).where(ChatMcpServer.owner_user_id == user))).all()
        assert {row.id for row in live} == {home_id, unknown}
        assert {row.owner_domain_id for row in live} == {domain}

    from lumen.services import extension_archive as archives

    archived = await archives.list_archives(user_id=user, domain_id=domain, limit=20, before_id=None)
    assert {(row["original_id"], row["canonical_id"]) for row in archived} == {
        (work_id, unknown), (latest_work, unknown),
    }
    restore_id = next(row["id"] for row in archived if row["original_id"] == work_id)
    await archives.restore_archive(archive_id=restore_id, user_id=user, domain_id=domain)
    async with factory() as session, session.begin():
        replacement = await add_server(session, origin, "New legacy label", 4)
    await scope_backfill.backfill_domain_scope(resolve=resolve)
    async with factory() as session:
        live = (await session.scalars(select(ChatMcpServer).where(ChatMcpServer.owner_user_id == user))).all()
        assert {row.id for row in live} == {home_id, work_id, replacement}
        assert {row.owner_domain_id for row in live} == {domain}
        assert {row.legacy_domain_group for row in live if row.id != work_id} - {None}
