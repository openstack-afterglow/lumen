"""Synthetic HTTP authorization tests using the real FastAPI scope dependencies.

Only identity and storage/provider boundaries are replaced. Role checks themselves
are never overridden, and no production Keystone or provider calls are made.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from test_policy import operator_policy as operator_policy

from lumen import auth
from lumen.api import agents, assets, code_workspaces, conversations, extensions, memory, plugins, workspaces
from lumen.api.compat import files
from lumen.services import agent_store, conversation_store, extensions_store, mcp_oauth

_IDENTIFIER = "00000000-0000-0000-0000-000000000001"



@pytest.fixture
def management_app():
    app = FastAPI()
    for router in (agents.router, assets.router, code_workspaces.router, conversations.router,
                   extensions.user_router, memory.router, plugins.router, workspaces.router,
                   extensions.admin_router, plugins.admin_router):
        app.include_router(router, prefix="/v1")
    app.include_router(files.router, prefix="/compat")
    principal = {
        "auth_type": "keystone", "user_id": "owner", "project_id": "project",
        "roles": ["member"], "is_system_admin": False, "scopes": (),
    }
    app.dependency_overrides[auth.get_principal] = lambda: principal
    app.dependency_overrides[auth.require_token] = lambda: principal
    return app, principal


_CONFIG_WRITES = [
    ("POST", "/v1/assets", None),
    ("POST", "/v1/custom-tools", {"name": "tool", "url": "https://example.test/tool"}),
    ("PATCH", "/v1/custom-tools/1", {"name": "updated"}),
    ("POST", "/v1/skills", {"name": "skill", "instructions": "private"}),
    ("PATCH", "/v1/skills/1", {"instructions": "updated"}),
    ("POST", "/v1/plugin-bindings", {"kind": "tool", "plugin_id": "test", "export_key": "tool", "name": "tool"}),
    ("PATCH", f"/v1/plugin-bindings/{_IDENTIFIER}", {"name": "updated"}),
    ("POST", "/v1/agents", {"name": "agent"}),
    ("PATCH", "/v1/agents/1", {"name": "updated"}),
    ("POST", "/v1/agents/1/clone", None),
    ("POST", "/v1/mcp-servers", {"name": "mcp", "url": "https://example.test/mcp"}),
    ("PATCH", "/v1/mcp-servers/1", {"name": "updated"}),
    ("POST", "/v1/mcp-servers/1/oauth/start", None),
    ("POST", "/v1/git-credentials", {"host": "example.test", "token": "secret"}),
    ("PUT", "/v1/git-credentials/1", {"token": "updated"}),
    ("POST", "/v1/code-workspaces", {"name": "workspace", "source_kind": "empty"}),
    ("POST", "/v1/memories", {"content": "history", "scope": "project"}),
    ("PATCH", "/v1/memories/1", {"content": "updated"}),
]


@pytest.mark.parametrize("method,path,payload", _CONFIG_WRITES)
@pytest.mark.parametrize("roles", [["reader"], []])
async def test_readers_and_nonmembers_cannot_manage_configuration(management_app, method, path, payload, roles):
    app, principal = management_app
    principal["roles"] = roles
    async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
        response = await client.request(method, path, json=payload)
    assert response.status_code == 403


_CONFIG_DELETES = [
    f"/v1/assets/{_IDENTIFIER}", "/v1/custom-tools/1", "/v1/skills/1",
    f"/v1/plugin-bindings/{_IDENTIFIER}", "/v1/agents/1", "/v1/mcp-servers/1",
    "/v1/mcp-servers/1/oauth", "/v1/git-credentials/1", f"/v1/code-workspaces/{_IDENTIFIER}",
]


@pytest.mark.parametrize("path", _CONFIG_DELETES)
@pytest.mark.parametrize("roles", [["reader"], []])
async def test_readers_and_nonmembers_cannot_delete_configuration(management_app, path, roles):
    app, principal = management_app
    principal["roles"] = roles
    async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
        response = await client.delete(path)
    assert response.status_code == 403


@pytest.mark.parametrize("role", ["member", "manager", "admin"])
@pytest.mark.parametrize("family", ["custom-tools", "skills", "mcp-servers"])
async def test_project_writers_manage_only_their_own_extensions(management_app, monkeypatch, role, family):
    app, principal = management_app
    principal["roles"] = [role]
    create = AsyncMock(return_value={"id": 1, "name": "owned"})
    update = AsyncMock(return_value={"id": 1, "name": "updated"})
    delete = AsyncMock()
    monkeypatch.setattr(extensions_store, "create", create)
    monkeypatch.setattr(extensions_store, "update", update)
    monkeypatch.setattr(extensions_store, "delete", delete)
    monkeypatch.setattr(mcp_oauth, "detect", AsyncMock(return_value={"auth_mode": "none"}))
    async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
        assert (await client.post(f"/v1/{family}", json={"name": "owned"})).status_code == 201
        assert (await client.patch(f"/v1/{family}/1", json={"name": "updated"})).status_code == 200
        response = await client.delete(f"/v1/{family}/1")
    assert create.call_args.kwargs == {"scope": "user", "owner_user_id": "owner", "owner_project_id": "project"}
    assert update.call_args.kwargs == {"requester_user_id": "owner", "requester_project_id": "project"}
    assert response.status_code == 204
    assert delete.call_args.kwargs == {"requester_user_id": "owner", "requester_project_id": "project"}
    principal["roles"] = ["reader"]
    async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
        assert (await client.post(f"/v1/{family}", json={"name": "blocked"})).status_code == 403


async def test_member_creates_clones_updates_and_deletes_owned_agent(management_app, monkeypatch):
    app, principal = management_app
    principal["roles"] = ["member"]
    create = AsyncMock(return_value={"id": 1})
    update = AsyncMock(return_value={"id": 1})
    clone = AsyncMock(return_value={"id": 2})
    delete = AsyncMock()
    for name, operation in (("create_agent", create), ("update_agent", update), ("clone_agent", clone), ("delete_agent", delete)):
        monkeypatch.setattr(agent_store, name, operation)
    async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
        assert (await client.post("/v1/agents", json={"name": "agent"})).status_code == 201
        assert (await client.patch("/v1/agents/1", json={"name": "updated"})).status_code == 200
        assert (await client.post("/v1/agents/1/clone")).status_code == 201
        principal["roles"] = ["reader"]
        assert (await client.delete("/v1/agents/1")).status_code == 403
        principal["roles"] = ["member"]
        assert (await client.delete("/v1/agents/1")).status_code == 204
    assert create.call_args.kwargs["owner_user_id"] == "owner"
    assert create.call_args.kwargs["project_id"] == "project"
    for operation in (update, clone, delete):
        assert operation.call_args.kwargs["user_id"] == "owner"
        assert operation.call_args.kwargs["project_id"] == "project"


def _conversation():
    return {"id": "conversation", "project_id": "project", "user_id": "owner", "title": None,
            "title_source": "explicit", "title_status": "idle", "title_revision": 0,
            "model_name": None, "created_at": None, "updated_at": None}


async def test_reader_reads_history_and_member_creates_forks_and_deletes(management_app, monkeypatch):
    app, principal = management_app
    create = AsyncMock(return_value=_conversation())
    fork = AsyncMock(return_value=_conversation())
    delete = AsyncMock()
    get = AsyncMock(return_value=_conversation())
    for name, operation in (("create_conversation", create), ("fork_conversation", fork),
                            ("delete_conversation", delete), ("get_conversation", get)):
        monkeypatch.setattr(conversation_store, name, operation)
    async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
        principal["roles"] = ["reader"]
        assert (await client.get("/v1/conversations/conversation")).status_code == 200
        assert (await client.post("/v1/conversations", json={})).status_code == 403
        principal["roles"] = ["member"]
        assert (await client.post("/v1/conversations", json={})).status_code == 201
        assert (await client.post("/v1/conversations/conversation/fork", json={"message_id": 1})).status_code == 201
        assert (await client.get("/v1/conversations/conversation")).status_code == 200
        principal["roles"] = ["reader"]
        assert (await client.delete("/v1/conversations/conversation")).status_code == 403
        delete.assert_not_called()
        principal["roles"] = ["member"]
        assert (await client.delete("/v1/conversations/conversation")).status_code == 204
    assert fork.call_args.kwargs["user_id"] == delete.call_args.kwargs["user_id"] == "owner"
    assert fork.call_args.kwargs["project_id"] == delete.call_args.kwargs["project_id"] == "project"


@pytest.mark.parametrize("user_id,project_id", [("other", "project"), ("owner", "other-project")])
async def test_admin_does_not_bypass_real_conversation_ownership(management_app, monkeypatch, user_id, project_id):
    app, principal = management_app
    principal.update(roles=["admin"], user_id=user_id, project_id=project_id)
    row = SimpleNamespace(user_id="owner", project_id="project")

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, *args, **kwargs):
            return row

    monkeypatch.setattr(conversation_store, "_require_db", lambda: Session)
    async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
        response = await client.get("/v1/conversations/conversation")
    assert response.status_code == 403


@pytest.mark.parametrize("path", ["/v1/admin/mcp-servers", "/v1/admin/custom-tools", "/v1/admin/skills",
                                  "/v1/admin/mcp-bundles", "/v1/admin/plugins", "/v1/admin/plugin-bindings"])
async def test_project_admin_is_not_global_system_admin(management_app, path):
    app, principal = management_app
    principal["roles"] = ["admin"]
    async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
        assert (await client.get(path)).status_code == 403


@pytest.mark.parametrize("role,scopes", [("reader", ("compat:files:write", "compat:files:delete")),
                                       ("member", ("compat:files:write",)),
                                       ("admin", ("compat:files:write",))])
async def test_compat_file_delete_requires_project_writer_and_explicit_delete_scope(management_app, role, scopes):
    app, principal = management_app
    principal.update(auth_type="api_key", roles=[role], scopes=scopes)
    async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
        assert (await client.delete("/compat/files/file-" + "0" * 32)).status_code == 403


async def test_api_key_scope_attenuates_project_member(management_app, monkeypatch):
    app, principal = management_app
    principal.update(auth_type="api_key", roles=["member"], scopes=("native:extensions:read",))
    create = AsyncMock(return_value={"id": 1})
    monkeypatch.setattr(extensions_store, "create", create)
    async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
        assert (await client.post("/v1/skills", json={"name": "skill"})).status_code == 403
        principal["scopes"] = ("native:extensions:write",)
        assert (await client.post("/v1/skills", json={"name": "skill"})).status_code == 201
    assert create.await_count == 1


@pytest.mark.parametrize("roles", [["reader"], []])
async def test_oauth_callback_rechecks_persisted_owner_before_exchange(monkeypatch, roles):
    row = SimpleNamespace(status="pending", expires_at=datetime.now(UTC) + timedelta(minutes=5),
                          owner_user_id="owner", owner_project_id="project", encrypted_payload="encrypted")
    result = SimpleNamespace(scalars=lambda: SimpleNamespace(first=lambda: row))
    session = SimpleNamespace(execute=AsyncMock(return_value=result))

    @asynccontextmanager
    async def connection():
        yield session

    authority = AsyncMock(return_value={"roles": roles, "is_system_admin": False})
    monkeypatch.setattr(mcp_oauth, "_session", connection)
    monkeypatch.setattr(mcp_oauth, "resolve_project_authority", authority)
    monkeypatch.setattr(mcp_oauth, "_decrypt", lambda _: pytest.fail("revoked owner state disclosed to OAuth exchange"))
    with pytest.raises(mcp_oauth.McpOAuthError, match="management authority"):
        await mcp_oauth.SqlMcpConnectionStore().consume_state("state-hash")
    authority.assert_awaited_once_with("owner", "project")
    assert row.status == "pending"


@pytest.mark.parametrize("family", ["agents", "skills", "plugin-bindings"])
async def test_inventory_and_private_config_have_distinct_authority(management_app, monkeypatch, family):
    app, principal = management_app
    principal["roles"] = ["reader"]
    private = "private instructions or credentials"

    async def listed(*args, include_private=True, **kwargs):
        result = {"id": 1, "name": "metadata"}
        if include_private:
            result["config" if family == "plugin-bindings" else "instructions"] = private
        return [result]

    if family == "agents":
        monkeypatch.setattr(agent_store, "list_agents", listed)
    elif family == "skills":
        monkeypatch.setattr(extensions_store, "list_for_user", listed)
    else:
        monkeypatch.setattr(plugins.bindings, "list_bindings", listed)
    async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
        inventory = await client.get(f"/v1/{family}")
        assert inventory.status_code == 200
        assert inventory.json() == [{"id": 1, "name": "metadata"}]
        assert (await client.get(f"/v1/{family}?include_private=true")).status_code == 403
        principal["roles"] = ["member"]
        details = await client.get(f"/v1/{family}?include_private=true")
        assert details.status_code == 200
        assert private in details.text
        principal.update(auth_type="api_key", scopes=("native:agents:read" if family == "agents" else "native:extensions:read",))
        assert (await client.get(f"/v1/{family}?include_private=true")).status_code == 403


async def test_private_agent_detail_requires_project_writer(management_app, monkeypatch):
    app, principal = management_app
    principal["roles"] = ["reader"]
    get = AsyncMock(return_value={"id": 1, "instructions": "private"})
    monkeypatch.setattr(agent_store, "get_agent", get)
    async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
        assert (await client.get("/v1/agents/1")).status_code == 403
        get.assert_not_called()
        principal["roles"] = ["member"]
        assert (await client.get("/v1/agents/1")).status_code == 200
    get.assert_awaited_once_with(1, user_id="owner", project_id="project")


@pytest.mark.parametrize("path,module,name,status", [
    (f"/v1/assets/{_IDENTIFIER}", assets.assets, "delete_asset", 202),
    (f"/v1/plugin-bindings/{_IDENTIFIER}", plugins.bindings, "update_binding", 204),
    ("/v1/mcp-servers/1/oauth", mcp_oauth, "disconnect", 204),
    ("/v1/git-credentials/1", code_workspaces, "revoke_git_credential", 204),
    (f"/v1/code-workspaces/{_IDENTIFIER}", code_workspaces, "request_delete_workspace", 202),
])
async def test_member_can_destroy_owned_config_without_owner_bypass(management_app, monkeypatch, path, module, name, status):
    app, principal = management_app
    principal["roles"] = ["member"]
    operation = AsyncMock(return_value={"id": _IDENTIFIER, "status": "deleting"})
    monkeypatch.setattr(module, name, operation)
    async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
        assert (await client.delete(path)).status_code == status
    kwargs = operation.call_args.kwargs
    if name == "update_binding":
        assert kwargs["namespace"].user_id == "owner"
        assert kwargs["namespace"].project_id == "project"
        assert kwargs["admin"] is False and kwargs["delete"] is True
    else:
        assert kwargs["user_id"] == "owner" and kwargs["project_id"] == "project"


async def test_memory_cleanup_requires_project_writer(management_app, monkeypatch):
    app, principal = management_app
    provider = SimpleNamespace(delete=AsyncMock())
    monkeypatch.setattr(memory, "get_plugin", lambda _: provider)
    monkeypatch.setattr(memory.memory_host, "access_for", lambda namespace: namespace)
    async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
        principal["roles"] = ["reader"]
        assert (await client.delete("/v1/memories/1")).status_code == 403
        principal["roles"] = ["member"]
        assert (await client.delete("/v1/memories/1")).status_code == 204
    args = provider.delete.call_args.args
    assert args[0] == 1 and args[1].user_id == "owner" and args[1].project_id == "project"


async def test_member_manages_owned_grouping_workspace(management_app, monkeypatch):
    app, principal = management_app
    create = AsyncMock(return_value={"id": 1})
    update = AsyncMock(return_value={"id": 1})
    delete = AsyncMock()
    monkeypatch.setattr(workspaces.ws, "create_workspace", create)
    monkeypatch.setattr(workspaces.ws, "update_workspace", update)
    monkeypatch.setattr(workspaces.ws, "delete_workspace", delete)
    async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
        principal["roles"] = ["member"]
        assert (await client.post("/v1/workspaces", json={"name": "group"})).status_code == 201
        assert (await client.patch("/v1/workspaces/1", json={"name": "updated"})).status_code == 200
        principal["roles"] = ["reader"]
        assert (await client.delete("/v1/workspaces/1")).status_code == 403
        principal["roles"] = ["member"]
        assert (await client.delete("/v1/workspaces/1")).status_code == 204
    assert create.call_args.kwargs["owner_user_id"] == "owner"
    assert update.call_args.kwargs["user_id"] == delete.call_args.kwargs["user_id"] == "owner"


def test_metadata_serializers_do_not_decrypt_private_configuration(monkeypatch):
    def unexpected(*args):
        pytest.fail("metadata serializer decrypted private configuration")

    monkeypatch.setattr(agent_store, "_dec", unexpected)
    monkeypatch.setattr(extensions_store, "decrypt_chat_content", unexpected)
    monkeypatch.setattr(plugins.bindings, "decrypt_chat_content", unexpected)
    agent = SimpleNamespace(id=1, owner_user_id="owner", name="agent", description=None, avatar=None,
                            instructions="ciphertext", model_name=None, visibility="private", cloned_from_id=None,
                            clone_count=0, created_at=None, updated_at=None)
    skill = SimpleNamespace(id=1, scope="user", name="skill", description=None,
                            instructions="ciphertext", is_active=True, created_at=None)
    binding = SimpleNamespace(id=_IDENTIFIER, kind="tool", plugin_id="plugin", export_key="tool", name="tool",
                              scope="user", owner_user_id="owner", owner_project_id="project",
                              encrypted_config="ciphertext", config_version=1, is_active=True)
    assert "instructions" not in agent_store._public(agent, owner_view=True, include_private=False)
    assert "instructions" not in extensions_store._public_skill(skill, include_private=False)
    assert "config" not in plugins.bindings._public(binding, include_private=False)


async def test_oauth_callback_accepts_current_project_member_owner(monkeypatch):
    row = SimpleNamespace(status="pending", expires_at=datetime.now(UTC) + timedelta(minutes=5),
                          owner_user_id="owner", owner_project_id="project", encrypted_payload="encrypted")
    result = SimpleNamespace(scalars=lambda: SimpleNamespace(first=lambda: row))
    session = SimpleNamespace(execute=AsyncMock(return_value=result))

    @asynccontextmanager
    async def connection():
        yield session

    authority = AsyncMock(return_value={"roles": ["member"], "is_system_admin": False})
    monkeypatch.setattr(mcp_oauth, "_session", connection)
    monkeypatch.setattr(mcp_oauth, "resolve_project_authority", authority)
    monkeypatch.setattr(mcp_oauth, "_decrypt", lambda _: {"namespace": {"user_id": "owner", "project_id": "project"}})
    payload = await mcp_oauth.SqlMcpConnectionStore().consume_state("state-hash")
    assert payload["namespace"] == {"user_id": "owner", "project_id": "project"}
    authority.assert_awaited_once_with("owner", "project")
    assert row.status == "processing"


@pytest.mark.parametrize("path", ["/v1/conversations", "/v1/conversations/search", "/v1/conversations/conversation",
                                  "/v1/conversations/conversation/messages", "/v1/workspaces", "/v1/workspaces/1",
                                  "/v1/memories", "/v1/memories/document"])
async def test_inventory_key_scope_does_not_disclose_history(management_app, path):
    app, principal = management_app
    principal.update(auth_type="api_key", roles=["reader"],
                     scopes=("models:read", "native:agents:read", "native:extensions:read"))
    async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
        assert (await client.get(path)).status_code == 403


@pytest.mark.parametrize("path", ["/v1/agents", "/v1/agents/hub", "/v1/mcp-servers", "/v1/mcp-servers/1/oauth",
                                  "/v1/custom-tools", "/v1/skills", "/v1/plugin-bindings", "/v1/code-workspaces",
                                  f"/v1/assets/{_IDENTIFIER}", f"/v1/assets/{_IDENTIFIER}/download"])
async def test_history_key_scope_does_not_grant_config_inventory(management_app, path):
    app, principal = management_app
    principal.update(auth_type="api_key", roles=["reader"],
                     scopes=("native:conversations:read", "native:memory:read"))
    async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
        assert (await client.get(path)).status_code == 403


@pytest.mark.parametrize("roles", [["member"], ["manager"], ["admin"]])
async def test_standard_project_writers_manage_configuration_without_service_roles(management_app, monkeypatch, roles):
    app, principal = management_app
    principal["roles"] = roles
    create = AsyncMock(return_value={"id": 1})
    monkeypatch.setattr(agent_store, "create_agent", create)
    async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
        assert (await client.post("/v1/agents", json={"name": "agent"})).status_code == 201
    assert create.call_args.kwargs["owner_user_id"] == "owner"
    assert create.call_args.kwargs["project_id"] == "project"
    assert principal["is_system_admin"] is False


async def test_member_oauth_start_remains_keystone_only_and_disconnect_requires_writer(management_app, monkeypatch):
    app, principal = management_app
    principal["roles"] = ["member"]
    begin = AsyncMock(return_value={"authorization_url": "https://example.test/authorize"})
    disconnect = AsyncMock()
    monkeypatch.setattr(mcp_oauth, "begin", begin)
    monkeypatch.setattr(mcp_oauth, "disconnect", disconnect)
    monkeypatch.setattr(mcp_oauth, "callback_cookie_secure", lambda: True)
    async with AsyncClient(transport=ASGITransport(app), base_url="https://test") as client:
        assert (await client.post("/v1/mcp-servers/1/oauth/start")).status_code == 200
        principal["roles"] = ["reader"]
        assert (await client.delete("/v1/mcp-servers/1/oauth")).status_code == 403
        principal.update(auth_type="api_key", roles=["member"], scopes=("native:mcp:write",))
        assert (await client.post("/v1/mcp-servers/1/oauth/start")).status_code == 403
        principal.update(auth_type="keystone", roles=["member"])
        assert (await client.delete("/v1/mcp-servers/1/oauth")).status_code == 204
    assert begin.await_count == 1
    assert begin.call_args.kwargs["user_id"] == "owner"
    assert begin.call_args.kwargs["project_id"] == "project"
    disconnect.assert_awaited_once_with(1, user_id="owner", project_id="project")


async def test_member_provisions_code_workspace_and_key_assignment_requires_agent_scope(management_app, monkeypatch):
    app, principal = management_app
    create = AsyncMock(return_value={"id": _IDENTIFIER})
    assign = AsyncMock(return_value={"id": "conversation"})
    monkeypatch.setattr(code_workspaces, "configured_workspace_policy", lambda _: "synthetic-policy")
    monkeypatch.setattr(code_workspaces, "create_workspace", create)
    monkeypatch.setattr(code_workspaces, "assign_conversation_workspace", assign)
    async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
        principal["roles"] = ["member"]
        assert (await client.post("/v1/code-workspaces", json={"name": "workspace", "source_kind": "empty"})).status_code == 202
        principal.update(auth_type="api_key", scopes=("native:conversations:write",))
        assert (await client.put("/v1/conversations/conversation/code-workspace", json={})).status_code == 403
        principal["scopes"] = ("native:conversations:write", "native:agents:use")
        assert (await client.put("/v1/conversations/conversation/code-workspace", json={"code_workspace_id": _IDENTIFIER})).status_code == 200
    assert create.call_args.kwargs["user_id"] == "owner" and create.call_args.kwargs["project_id"] == "project"
    assign.assert_awaited_once_with("conversation", workspace_id=_IDENTIFIER, user_id="owner", project_id="project")


async def test_member_plugin_create_and_update_preserve_namespace(management_app, monkeypatch):
    app, principal = management_app
    principal["roles"] = ["member"]
    create = AsyncMock(return_value={"id": _IDENTIFIER})
    update = AsyncMock(return_value={"id": _IDENTIFIER})
    monkeypatch.setattr(plugins.bindings, "create_binding", create)
    monkeypatch.setattr(plugins.bindings, "update_binding", update)
    async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
        assert (await client.post("/v1/plugin-bindings", json={"kind": "skill", "plugin_id": "test", "export_key": "skill", "name": "skill"})).status_code == 201
        assert (await client.patch(f"/v1/plugin-bindings/{_IDENTIFIER}", json={"name": "updated"})).status_code == 200
    for operation in (create, update):
        assert operation.call_args.kwargs["namespace"].user_id == "owner"
        assert operation.call_args.kwargs["namespace"].project_id == "project"
        assert operation.call_args.kwargs["admin"] is False


async def test_member_uploads_and_deletes_assets_but_reader_cannot(management_app, monkeypatch):
    app, principal = management_app
    upload = AsyncMock(return_value={"id": _IDENTIFIER, "name": "file.txt", "mime_type": "text/plain",
                                    "size_bytes": 5, "sha256": "a" * 64, "status": "ready"})
    delete = AsyncMock(return_value={"id": _IDENTIFIER, "status": "deleting"})
    monkeypatch.setattr(assets.assets, "create_uploaded_asset", upload)
    monkeypatch.setattr(assets.assets, "delete_asset", delete)
    async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
        principal["roles"] = ["member"]
        assert (await client.post("/v1/assets", files={"file": ("file.txt", b"hello", "text/plain")})).status_code == 201
        principal["roles"] = ["reader"]
        assert (await client.delete(f"/v1/assets/{_IDENTIFIER}")).status_code == 403
        assert (await client.post("/v1/assets", files={"file": ("file.txt", b"hello", "text/plain")})).status_code == 403
        principal["roles"] = ["member"]
        assert (await client.delete(f"/v1/assets/{_IDENTIFIER}")).status_code == 202
    assert upload.await_count == 1
    assert upload.call_args.kwargs["user_id"] == "owner" and upload.call_args.kwargs["project_id"] == "project"
    assert not upload.call_args.kwargs["path"].exists()
    delete.assert_awaited_once_with(asset_id=_IDENTIFIER, user_id="owner", project_id="project")


async def test_verified_system_admin_can_manage_global_extensions(management_app, monkeypatch):
    app, principal = management_app
    principal.update(is_system_admin=True, roles=["admin"])
    create = AsyncMock(return_value={"id": 1, "scope": "global"})
    monkeypatch.setattr(extensions_store, "create", create)
    async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
        response = await client.post("/v1/admin/skills", json={"name": "global", "instructions": "policy"})
    assert response.status_code == 201
    create.assert_awaited_once_with("skill", {"name": "global", "instructions": "policy"}, scope="global")


@pytest.mark.parametrize("method,payload", [("GET", None), ("PATCH", {"name": "updated"}), ("DELETE", None)])
async def test_project_admin_agent_routes_keep_real_user_project_sql_predicates(management_app, monkeypatch, method, payload):
    app, principal = management_app
    principal.update(roles=["admin"], user_id="other-user", project_id="other-project")
    statements = []

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def begin(self):
            return self

        async def execute(self, statement):
            statements.append(str(statement.compile(compile_kwargs={"literal_binds": True})))
            return SimpleNamespace(scalar_one_or_none=lambda: None)

    monkeypatch.setattr(agent_store, "_require_db", lambda: Session)
    async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
        response = await client.request(method, "/v1/agents/1", json=payload)
    assert response.status_code == 404
    assert len(statements) == 1
    assert "chat_agents.owner_user_id = 'other-user'" in statements[0]
    assert "chat_agents.project_id = 'other-project'" in statements[0]


@pytest.mark.parametrize("method,path,payload,action", [
    ("POST", "/v1/agents", {"name": "agent"}, "native:agents:write"),
    ("POST", "/v1/skills", {"name": "skill"}, "native:extensions:write"),
    ("POST", "/v1/mcp-servers/1/oauth/start", None, "native:mcp:write"),
    ("DELETE", "/v1/agents/1", None, "native:agents:delete"),
    ("DELETE", "/v1/conversations/conversation", None, "native:conversations:delete"),
    ("GET", "/v1/skills?include_private=true", None, "native:extensions:write"),
])
async def test_operator_action_deny_overrides_retained_project_member(management_app, operator_policy, method, path, payload, action):
    app, principal = management_app
    principal["roles"] = ["member"]
    replace, _ = operator_policy
    replace(f'"{action}": "!"')
    async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
        assert (await client.request(method, path, json=payload)).status_code == 403


async def test_retained_member_cannot_restore_operator_denied_callback_authority(monkeypatch, operator_policy):
    row = SimpleNamespace(status="pending", expires_at=datetime.now(UTC) + timedelta(minutes=5),
                          owner_user_id="owner", owner_project_id="project", encrypted_payload="encrypted")
    result = SimpleNamespace(scalars=lambda: SimpleNamespace(first=lambda: row))
    session = SimpleNamespace(execute=AsyncMock(return_value=result))

    @asynccontextmanager
    async def connection():
        yield session

    replace, _ = operator_policy
    replace('"native:mcp:write": "!"')
    authority = AsyncMock(return_value={"roles": ["member"], "is_system_admin": False})
    monkeypatch.setattr(mcp_oauth, "_session", connection)
    monkeypatch.setattr(mcp_oauth, "resolve_project_authority", authority)
    monkeypatch.setattr(mcp_oauth, "_decrypt", lambda _: pytest.fail("operator-denied owner state disclosed to OAuth exchange"))
    with pytest.raises(mcp_oauth.McpOAuthError, match="management authority"):
        await mcp_oauth.SqlMcpConnectionStore().consume_state("state-hash")
    authority.assert_awaited_once_with("owner", "project")
