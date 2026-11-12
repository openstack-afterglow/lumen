"""Real MariaDB owner consent, config revisions, OAuth and frozen execution selection."""
from __future__ import annotations

import os
import uuid

import pytest
from lumen_plugin_api.contracts import Namespace
from sqlalchemy import select

from lumen.db import close_db, get_session_factory, init_db
from lumen.models.chat_db import ChatMcpActivation, ChatMcpOAuthConnection, ChatMcpServer
from lumen.services import extensions_store as es
from lumen.services import mcp_oauth

pytestmark = pytest.mark.integration


@pytest.fixture
async def activation_db():
    init_db(os.environ["DATABASE_URL"], pool_size=1, max_overflow=0)
    factory = get_session_factory()
    assert factory is not None
    ids = []
    try:
        yield factory, ids
    finally:
        async with factory() as session, session.begin():
            connections = (await session.execute(select(ChatMcpOAuthConnection).where(
                ChatMcpOAuthConnection.mcp_server_id.in_(ids)
            ))).scalars().all()
            for row in connections:
                await session.delete(row)
            for server_id in ids:
                row = await session.get(ChatMcpServer, server_id)
                if row is not None:
                    await session.delete(row)
        await close_db()


async def _server(factory, ids, **fields):
    async with factory() as session, session.begin():
        row = ChatMcpServer(name=f"activation-{uuid.uuid4().hex}", url="https://mcp.example/mcp", **fields)
        session.add(row)
        await session.flush()
        ids.append(row.id)
        return row.id


async def _visible(owner, **kwargs):
    return {item["id"]: item for item in await es.list_for_user("mcp", **owner, **kwargs)}


@pytest.mark.parametrize("auth_mode", ["none", "admin"])
async def test_global_publication_needs_owner_consent_and_epoch_never_resurrects(activation_db, auth_mode):
    factory, ids = activation_db
    owner = {"user_id": uuid.uuid4().hex, "project_id": uuid.uuid4().hex}
    secret_fields = {"encrypted_headers": es.encrypt_llm_provider_key('{"X-Api-Key":"admin-secret"}')} if auth_mode == "admin" else {}
    server_id = await _server(factory, ids, scope="global", auth_mode=auth_mode, **secret_fields)
    other = {**owner, "project_id": uuid.uuid4().hex}
    catalogue = (await _visible(owner))[server_id]
    assert "admin-secret" not in str(catalogue)
    assert catalogue["server_is_active"] and not catalogue["is_active"]
    assert not catalogue["activation_is_active"]
    assert server_id not in await _visible(owner, active_only=True)
    first = await es.set_mcp_activation(server_id, **owner, is_active=True, config_version=1)
    assert first["is_active"] and first["activation_generation"] == 1
    execution = await _visible(owner, active_only=True, reveal_secrets=True)
    assert server_id in execution
    assert execution[server_id]["headers"] == ({"X-Api-Key": "admin-secret"} if auth_mode == "admin" else {})
    assert server_id not in await _visible(other, active_only=True)
    snapshot = {"tools": [], "mcp": [{"id": server_id,
        "config_fingerprint": es.selection_fingerprint(first), "credential_version": 0}]}
    assert (await es.validated_frozen_selection(snapshot, **owner))[1] == (server_id,)
    assert (await es.set_mcp_activation(server_id, **owner, is_active=True, config_version=1))["activation_generation"] == 1
    disabled = await es.set_mcp_activation(server_id, **owner, is_active=False, config_version=1)
    assert disabled["activation_generation"] == 2
    assert server_id not in await _visible(owner, active_only=True)
    resumed = await es.set_mcp_activation(server_id, **owner, is_active=True, config_version=1)
    assert resumed["activation_generation"] == 3
    assert (await es.validated_frozen_selection(snapshot, **owner))[1] == ()
    async with factory() as session:
        server = await session.get(ChatMcpServer, server_id)
        assert server.config_version == 1 and server.is_active
        approvals = (await session.execute(select(ChatMcpActivation).where(
            ChatMcpActivation.mcp_server_id == server_id
        ))).scalars().all()
        assert len(approvals) == 1 and approvals[0].owner_project_id == owner["project_id"]
    await es.update("mcp", server_id, {"name": "new reviewed config"}, admin=True)
    assert server_id not in await _visible(owner, active_only=True)
    assert (await _visible(owner))[server_id]["config_version"] == 2
    with pytest.raises(es.ExtensionValidationError, match="changed"):
        await es.set_mcp_activation(server_id, **owner, is_active=True, config_version=1)
    assert (await es.set_mcp_activation(server_id, **owner, is_active=True, config_version=2))["is_active"]


async def test_activation_errors_preserve_direct_crud_and_owner_scope(activation_db):
    factory, ids = activation_db
    owner = {"user_id": uuid.uuid4().hex, "project_id": uuid.uuid4().hex}
    private_id = await _server(factory, ids, scope="user", owner_user_id=owner["user_id"], owner_project_id=owner["project_id"])
    unavailable = await _server(factory, ids, scope="global", is_active=False)
    assert private_id in await _visible(owner, active_only=True)
    assert private_id not in await _visible({**owner, "user_id": "not-owner"})
    for context in [owner, {**owner, "user_id": "not-owner"}]:
        with pytest.raises(es.ExtensionForbidden):
            await es.set_mcp_activation(private_id, **context, is_active=True, config_version=1)
    with pytest.raises(es.ExtensionValidationError, match="unavailable"):
        await es.set_mcp_activation(unavailable, **owner, is_active=True, config_version=1)
    with pytest.raises(es.ExtensionNotFound):
        await es.set_mcp_activation(-1, **owner, is_active=True, config_version=1)
    with pytest.raises(es.ExtensionForbidden):
        await es.set_mcp_activation(unavailable, user_id=owner["user_id"], project_id="", is_active=True, config_version=1)
    with pytest.raises(es.ExtensionForbidden):
        await es.update("mcp", private_id, {"is_active": False}, requester_user_id="other", requester_project_id=owner["project_id"])
    await es.update("mcp", private_id, {"is_active": False}, requester_user_id=owner["user_id"], requester_project_id=owner["project_id"])
    assert private_id not in await _visible(owner, active_only=True)
    assert unavailable in await _visible(owner)


async def test_oauth_requires_both_current_tokens_and_activation_without_public_secrets(activation_db):
    factory, ids = activation_db
    owner = {"user_id": uuid.uuid4().hex, "project_id": uuid.uuid4().hex}
    server_id = await _server(factory, ids, scope="global", auth_mode="oauth")
    approved = await es.set_mcp_activation(server_id, **owner, is_active=True, config_version=1)
    assert approved["activation_is_active"] and not approved["is_active"]
    assert server_id not in await _visible(owner, active_only=True)
    connection_id = str(uuid.uuid4())
    async with factory() as session, session.begin():
        session.add(ChatMcpOAuthConnection(id=connection_id, mcp_server_id=server_id,
            owner_user_id=owner["user_id"], owner_project_id=owner["project_id"],
            encrypted_tokens=mcp_oauth._encrypt({"access_token": "private-token", "token_type": "Bearer"}),
            server_config_version=1))
    current = (await _visible(owner, active_only=True))[server_id]
    assert current["is_active"] and "private-token" not in str(current)
    namespace = Namespace(**owner)
    store = mcp_oauth.SqlMcpConnectionStore()
    assert [c.server_id for c in await store.connections(namespace)] == [server_id]
    snapshot = {"tools": [], "mcp": [{"id": server_id,
        "config_fingerprint": es.selection_fingerprint(current), "credential_version": 1}]}
    assert (await es.validated_frozen_selection(snapshot, **owner))[1] == (server_id,)
    await es.set_mcp_activation(server_id, **owner, is_active=False, config_version=1)
    assert await store.connections(namespace) == ()
    with pytest.raises(mcp_oauth.McpOAuthError, match="activation"):
        async with store.locked_refresh(connection_id):
            pytest.fail("disabled approval must not refresh or reveal tokens")
    await es.set_mcp_activation(server_id, **owner, is_active=True, config_version=1)
    async with factory() as session, session.begin():
        connection = await session.get(ChatMcpOAuthConnection, connection_id)
        connection.server_config_version = 2
    assert server_id not in await _visible(owner, active_only=True)
    assert await store.connections(namespace) == ()
    assert (await es.validated_frozen_selection(snapshot, **owner))[1] == ()
