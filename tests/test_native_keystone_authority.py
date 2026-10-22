"""Actual installed Keystone SDK HTTP against the isolated directory fixture."""
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, call
from uuid import uuid4

import httpx
import pytest
from fastapi import Depends, FastAPI, HTTPException, Request
from system.fake_keystone import (
    CONTROL_TOKEN,
    DIRECTORY_ID,
    DIRECTORY_PASSWORD,
    DIRECTORY_PROJECT_ID,
    DIRECTORY_TOKEN,
    OWNER_ID,
    OWNER_TOKEN,
    OWNER_UNSCOPED_TOKEN,
    PROJECT_ID,
    PROJECTLESS_ADMIN_ID,
    PROJECTLESS_SYSTEM_TOKEN,
    PROJECTLESS_UNSCOPED_TOKEN,
    SYSTEM_GROUP_ID,
    SYSTEM_PARENT,
    KeystoneServer,
    role_id,
)

from lumen import auth
from lumen.service_authority import SERVICE_CAPABILITIES, capabilities
from lumen.services import api_key_store

_TOOL_BODY = {"name": "synthetic-tool", "url": "https://tool.example/invoke"}
_ADMIN_REQUESTS = [
    ("GET", "/v1/admin/providers", None, "provider_list", 200),
    ("POST", "/v1/admin/providers", {"name": "synthetic-provider"}, "provider_create", 201),
    ("PATCH", "/v1/admin/providers/1", {"name": "renamed-provider"}, "provider_update", 200),
    ("DELETE", "/v1/admin/providers/1", None, "provider_delete", 204),
    ("GET", "/v1/admin/custom-tools", None, "extension_list_global", 200),
    ("POST", "/v1/admin/custom-tools", _TOOL_BODY, "extension_create", 201),
    ("PATCH", "/v1/admin/custom-tools/1", _TOOL_BODY, "extension_update", 200),
    ("DELETE", "/v1/admin/custom-tools/1", None, "extension_delete", 204),
]
_SCOPED_REQUESTS = [
    ("GET", "/v1/custom-tools", None, "extension_list_user", 200),
    ("POST", "/v1/custom-tools", _TOOL_BODY, "extension_create", 201),
    ("PATCH", "/v1/custom-tools/1", _TOOL_BODY, "extension_update", 200),
    ("DELETE", "/v1/custom-tools/1", None, "extension_delete", 204),
]
_AUTH_REQUESTS = _ADMIN_REQUESTS + _SCOPED_REQUESTS
_ADMIN_SOURCES = [
    pytest.param((DIRECTORY_TOKEN, DIRECTORY_ID, DIRECTORY_PROJECT_ID, ""), id="PROJECT"),
    pytest.param((PROJECTLESS_SYSTEM_TOKEN, PROJECTLESS_ADMIN_ID, "", "all"), id="SYSTEM"),
    pytest.param((PROJECTLESS_UNSCOPED_TOKEN, PROJECTLESS_ADMIN_ID, "", ""), id="UNSCOPED"),
]
_PROJECTLESS_TOKENS = [
    pytest.param(PROJECTLESS_SYSTEM_TOKEN, id="SYSTEM"),
    pytest.param(PROJECTLESS_UNSCOPED_TOKEN, id="UNSCOPED"),
]


def _headers(token=DIRECTORY_TOKEN, project_id=DIRECTORY_PROJECT_ID, *, target_project_id=None):
    headers = {"X-Auth-Token": token}
    if project_id is not None:
        headers["X-Project-Id"] = project_id
    if target_project_id is not None:
        headers["X-Target-Project-Id"] = target_project_id
    return headers


def _admin_headers(logical_project_id):
    return _headers(target_project_id=logical_project_id if logical_project_id != DIRECTORY_PROJECT_ID else None)


@pytest.fixture
def native_directory(monkeypatch):
    server = KeystoneServer(("127.0.0.1", 0))
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    settings = SimpleNamespace(keystone_auth_url=url + "/v3", keystone_admin_username=DIRECTORY_ID,
        keystone_admin_password=DIRECTORY_PASSWORD, keystone_admin_project=DIRECTORY_PROJECT_ID,
        keystone_domain="Default", keystone_region_name="RegionOne", keystone_interface="public", verify=True)
    monkeypatch.setattr(auth, "get_settings", lambda: settings)
    with httpx.Client(base_url=url, trust_env=False) as client:
        def configure(payload):
            response = client.post("/_control/configure", json=payload,
                                   headers={"X-Test-Control-Token": CONTROL_TOKEN})
            assert response.status_code == 200, response.text
        yield SimpleNamespace(client=client, configure=configure, state=server.state)
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


@pytest.fixture(params=_ADMIN_SOURCES)
def native_admin_source(request):
    """Projectless callers have no project grants; the directory reader keeps its home."""
    token, user_id, connection_project_id, system_scope = request.param
    return SimpleNamespace(
        token=token, user_id=user_id, connection_project_id=connection_project_id, system_scope=system_scope,
        headers=_headers(token, None, target_project_id=PROJECT_ID),
    )


@pytest.fixture
async def native_routes(native_directory, monkeypatch):
    """Production routers and auth dependencies; only SQL persistence is mocked."""
    from lumen.api import api_keys, completions, extensions, models

    application = FastAPI()
    application.include_router(models.router, prefix="/v1")
    application.include_router(extensions.admin_router, prefix="/v1")
    application.include_router(extensions.user_router, prefix="/v1")
    application.include_router(api_keys.router, prefix="/v1")
    application.include_router(completions.router, prefix="/v1")
    assert application.dependency_overrides == {}

    @application.get("/native/require-token")
    async def token_probe(info: dict = Depends(auth.require_token)):
        return info

    @application.get("/native/principal")
    async def principal_probe(info: dict = Depends(auth.get_principal)):
        return info
    provider = {
        "id": 1, "name": "synthetic-provider", "provider_type": "openai", "api_provider": "openai",
        "api_base": None, "has_api_key": False, "is_active": True, "margin_multiplier": 1.0,
        "created_at": None, "updated_at": None,
    }
    tool = {"id": 1, **_TOOL_BODY}
    storage = {
        "provider_list": AsyncMock(return_value=[provider]),
        "provider_create": AsyncMock(return_value=provider),
        "provider_update": AsyncMock(return_value=provider),
        "provider_delete": AsyncMock(return_value=None),
        "extension_list_global": AsyncMock(return_value=[tool]),
        "extension_list_user": AsyncMock(return_value=[tool]),
        "extension_create": AsyncMock(return_value=tool),
        "extension_update": AsyncMock(return_value=tool),
        "extension_delete": AsyncMock(return_value=None),
    }
    for name, attribute in (("provider_list", "list_providers"), ("provider_create", "create_provider"),
                            ("provider_update", "update_provider"), ("provider_delete", "delete_provider")):
        monkeypatch.setattr(models.repository, attribute, storage[name])
    for name, attribute in (("extension_list_global", "list_global"), ("extension_list_user", "list_for_user"),
                            ("extension_create", "create"), ("extension_update", "update"), ("extension_delete", "delete")):
        monkeypatch.setattr(extensions.es, attribute, storage[name])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(application), base_url="http://native") as client:
        yield SimpleNamespace(client=client, storage=storage)


async def test_native_sdk_grade_alone_survives_zero_service_edges_without_fabricated_roles(native_directory):
    native_directory.configure({"owner_roles": ["lumen_admin"], "remove_edges": [
        {"prior": "lumen_admin", "implied": "lumen_editor"},
        {"prior": "lumen_admin", "implied": "lumen-resources_admin"},
    ]})
    current = await auth.resolve_project_authority(OWNER_ID, PROJECT_ID)
    assert current == {"roles": ["lumen_admin"], "is_system_admin": False}
    assert capabilities({"user_id": OWNER_ID, "project_id": PROJECT_ID, **current}) == SERVICE_CAPABILITIES
    assert auth._is_system_admin(OWNER_ID) is False
    assert auth._is_system_admin(DIRECTORY_ID) is True


@pytest.mark.parametrize("patch", [{"owner_roles": []}, {"owner_enabled": False}, {"project_enabled": False}])
async def test_native_sdk_removed_or_disabled_owner_denied(native_directory, patch):
    native_directory.configure(patch)
    with pytest.raises(HTTPException) as exc:
        await auth.resolve_project_authority(OWNER_ID, PROJECT_ID)
    assert exc.value.status_code == 403


@pytest.mark.parametrize(("user_id", "project_id"), [("deleted-owner", PROJECT_ID), (OWNER_ID, "deleted-project")])
async def test_native_sdk_deleted_owner_or_project_is_permanent_denial(native_directory, user_id, project_id):
    with pytest.raises(HTTPException) as exc:
        await auth.resolve_project_authority(user_id, project_id)
    assert exc.value.status_code == 403


async def test_native_sdk_directory_failure_stays_unavailable(native_directory):
    native_directory.configure({"unavailable": True})
    with pytest.raises(HTTPException) as exc:
        await auth.resolve_project_authority(OWNER_ID, PROJECT_ID)
    assert exc.value.status_code == 503


@pytest.mark.parametrize(("leaf", "scope"), [
    ("lumen-chat_user", "compat:completions:write"),
    ("lumen-images_user", "native:images:write"),
    ("lumen-audio_user", "native:audio:write"),
    ("lumen-tools_user", "native:tools:execute"),
])
@pytest.mark.parametrize("key", [False, True])
async def test_native_sdk_live_owner_fences_new_io_for_web_and_existing_key(native_directory, key, leaf, scope):
    row, _raw = await api_key_store.prepare_key_record(OWNER_ID, PROJECT_ID, "HTTP owner", [scope])
    row.id = 7
    class Session:
        async def execute(self, statement):
            return SimpleNamespace(scalar_one_or_none=lambda: row)
    kwargs = dict(api_key_id=7 if key else None, user_id=OWNER_ID, project_id=PROJECT_ID,
                  required_scopes=(scope,))
    await api_key_store.authorize_api_key_in_transaction(Session(), **kwargs)
    # Issuer key-management authority need not be retained by an existing key.
    native_directory.configure({"owner_roles": ["member", leaf]})
    await api_key_store.authorize_api_key_in_transaction(Session(), **kwargs)
    with pytest.raises(api_key_store.ApiKeyForbidden):
        await api_key_store.prepare_key_record(OWNER_ID, PROJECT_ID, "cannot reissue", [scope])
    native_directory.configure({"owner_roles": ["member"]})
    # The directory service remains privileged; it cannot replace owner authority.
    assert auth._is_system_admin(DIRECTORY_ID) is True
    with pytest.raises(api_key_store.ApiKeyForbidden):
        await api_key_store.authorize_api_key_in_transaction(Session(), **kwargs)


def test_native_sdk_token_auth_preserves_fixed_project_and_rejects_role_headers(native_directory):
    principal = auth.validate_token(OWNER_TOKEN)
    assert principal["user_id"] == OWNER_ID and principal["project_id"] == PROJECT_ID
    assert principal["is_system_admin"] is False
    response = native_directory.client.get("/v3/roles", headers={"X-Roles": "admin", "X-Auth-Token": "invented"})
    assert response.status_code == 401
    response = native_directory.client.post("/_control/configure", json={"owner_roles": ["admin"]})
    assert response.status_code == 403


async def test_native_sdk_system_admin_target_keeps_original_connection_scope(native_directory):
    assert await auth.resolve_project_authority(DIRECTORY_ID, PROJECT_ID) == {
        "roles": [], "is_system_admin": True,
    }
    info = await auth.require_token(
        x_auth_token=DIRECTORY_TOKEN, bearer=None,
        x_project_id=DIRECTORY_PROJECT_ID, x_target_project_id=PROJECT_ID,
    )
    assert info["is_system_admin"] is True
    assert info["project_id"] == PROJECT_ID
    assert info["roles"] == []
    assert info["connection_project_id"] == DIRECTORY_PROJECT_ID
    connection = auth.get_os_conn(token_info=info)
    conn = await anext(connection)
    try:
        # Authenticate the installed SDK over HTTP, not a constructor-kwargs echo.
        assert conn.current_project_id == DIRECTORY_PROJECT_ID
        assert conn.session.get_user_id() == DIRECTORY_ID
        # Generic token auth exposes its original credential through this
        # public cache identity, unlike directory password authentication.
        assert conn.session.auth.get_cache_id_elements()["token"] == DIRECTORY_TOKEN
    finally:
        await connection.aclose()


async def test_native_sdk_admin_source_scope_matrix(native_directory, native_admin_source, monkeypatch):
    source = native_admin_source
    # openstack.connect defaults to loading both environment and clouds.yaml.
    # Neither may silently project-scope an originally projectless credential,
    # or invent system scope for an originally UNSCOPED credential.
    monkeypatch.setenv("OS_PROJECT_ID", PROJECT_ID)
    monkeypatch.setenv("OS_PROJECT_NAME", "hostile-ambient-project")
    monkeypatch.setenv("OS_CLOUD", "hostile-ambient-cloud")
    monkeypatch.setenv("OS_SYSTEM_SCOPE", "all")
    if not source.connection_project_id:
        assignments = native_directory.client.get(
            "/v3/role_assignments", params={"user.id": source.user_id},
            headers={"X-Auth-Token": DIRECTORY_TOKEN},
        )
        assert assignments.status_code == 200
        assert assignments.json()["role_assignments"] == [{
            "user": {"id": PROJECTLESS_ADMIN_ID}, "role": {"id": "role-admin"},
            "scope": {"system": {"all": True}},
        }]
        assert source.user_id != DIRECTORY_ID
        directory_home = native_directory.client.get(
            "/v3/role_assignments", params={"user.id": DIRECTORY_ID, "scope.project.id": DIRECTORY_PROJECT_ID},
            headers={"X-Auth-Token": DIRECTORY_TOKEN},
        )
        assert directory_home.status_code == 200
        assert {row["role"]["id"] for row in directory_home.json()["role_assignments"]} == {
            "role-admin", "role-member",
        }
    info = await auth.require_token(
        x_auth_token=source.token, bearer=None, x_project_id=None, x_target_project_id=PROJECT_ID,
    )
    assert info["is_system_admin"] is True
    assert info["user_id"] == source.user_id
    assert info["roles"] == []
    assert info["project_id"] == PROJECT_ID
    assert info["connection_project_id"] == source.connection_project_id
    assert info["connection_system_scope"] == source.system_scope
    assert info["token"] == source.token
    principal = await auth.get_principal(Request({
        "type": "http", "headers": [(name.lower().encode(), value.encode()) for name, value in source.headers.items()],
    }))
    assert principal["is_system_admin"] is True
    assert principal["roles"] == []
    assert principal["user_id"] == source.user_id
    assert principal["project_id"] == PROJECT_ID
    assert principal["connection_project_id"] == source.connection_project_id
    assert principal["connection_system_scope"] == source.system_scope
    assert principal["token"] == source.token
    connection = auth.get_os_conn(token_info=info)
    conn = await anext(connection)
    try:
        assert (conn.current_project_id or "") == source.connection_project_id
        assert conn.session.get_user_id() == source.user_id
        assert conn.session.auth.get_access(conn.session).system_scoped is bool(source.system_scope)
        assert conn.session.auth.get_cache_id_elements()["token"] == source.token
    finally:
        await connection.aclose()


def test_native_sdk_selector_free_exchange_preserves_original_scope(native_directory, native_admin_source):
    source = native_admin_source
    response = native_directory.client.post("/v3/auth/tokens", json={
        "auth": {"identity": {"methods": ["token"], "token": {"id": source.token}}},
    })
    assert response.status_code == 201, response.text
    assert response.headers["X-Subject-Token"] == source.token
    document = response.json()["token"]
    assert document["user"]["id"] == source.user_id
    if source.connection_project_id:
        assert document["project"]["id"] == source.connection_project_id
    else:
        assert "project" not in document
    if source.system_scope:
        assert document["system"] == {"all": True}
    else:
        assert "system" not in document

async def test_native_sdk_non_admin_cannot_select_foreign_target(native_directory):
    with pytest.raises(HTTPException) as exc:
        await auth.require_token(
            x_auth_token=OWNER_TOKEN, bearer=None,
            x_project_id=PROJECT_ID, x_target_project_id=DIRECTORY_PROJECT_ID,
        )
    assert exc.value.status_code == 403


@pytest.mark.parametrize("logical_project_id", [DIRECTORY_PROJECT_ID, PROJECT_ID], ids=["home", "target"])
async def test_native_http_system_admin_without_target_roles_crud(native_directory, native_routes, logical_project_id):
    authority = await auth.resolve_project_authority(DIRECTORY_ID, PROJECT_ID)
    assert authority == {"roles": [], "is_system_admin": True}
    headers = _admin_headers(logical_project_id)
    for method, path, payload, _store, status in _AUTH_REQUESTS:
        response = await native_routes.client.request(method, path, json=payload, headers=headers)
        assert response.status_code == status, (method, path, response.text)
    storage = native_routes.storage
    storage["provider_list"].assert_awaited_once_with()
    assert storage["provider_create"].await_args.kwargs["name"] == "synthetic-provider"
    storage["provider_delete"].assert_awaited_once_with(1)
    storage["extension_list_global"].assert_awaited_once_with("tool")
    storage["extension_list_user"].assert_awaited_once_with(
        "tool", user_id=DIRECTORY_ID, project_id=logical_project_id,
    )
    assert storage["extension_create"].await_args_list == [
        call("tool", _TOOL_BODY, scope="global"),
        call("tool", _TOOL_BODY, scope="user", owner_user_id=DIRECTORY_ID, owner_project_id=logical_project_id),
    ]
    assert storage["extension_delete"].await_args_list == [
        call("tool", 1, admin=True),
        call("tool", 1, requester_user_id=DIRECTORY_ID, requester_project_id=logical_project_id),
    ]


async def test_native_http_admin_source_scope_matrix_crud(native_directory, native_routes, native_admin_source):
    source = native_admin_source
    assert "X-Project-Id" not in source.headers
    assert source.headers["X-Target-Project-Id"] == PROJECT_ID
    assert await auth.resolve_project_authority(source.user_id, PROJECT_ID) == {
        "roles": [], "is_system_admin": True,
    }
    for method, path, payload, store, status in _AUTH_REQUESTS:
        before = native_routes.storage[store].await_count
        response = await native_routes.client.request(method, path, json=payload, headers=source.headers)
        assert response.status_code == status, (method, path, response.text)
        assert native_routes.storage[store].await_count == before + 1
    native_routes.storage["extension_list_user"].assert_awaited_once_with(
        "tool", user_id=source.user_id, project_id=PROJECT_ID,
    )
    assert native_routes.storage["extension_create"].await_args_list == [
        call("tool", _TOOL_BODY, scope="global"),
        call("tool", _TOOL_BODY, scope="user", owner_user_id=source.user_id, owner_project_id=PROJECT_ID),
    ]
    assert native_routes.storage["extension_delete"].await_args_list == [
        call("tool", 1, admin=True),
        call("tool", 1, requester_user_id=source.user_id, requester_project_id=PROJECT_ID),
    ]


@pytest.mark.parametrize(("patch", "status"), [
    pytest.param({"directory_system_admin": False}, 403, id="revoked-current-admin"),
    pytest.param({"authority_unavailable": True}, 503, id="metadata-only-outage"),
    pytest.param({"project_enabled": False}, 403, id="disabled-target"),
    pytest.param({"project_exists": False}, 403, id="deleted-target"),
])
async def test_native_http_admin_source_scope_matrix_denied(
    native_directory, native_routes, native_admin_source, patch, status,
):
    source = native_admin_source
    native_directory.configure(patch)
    if patch.get("directory_system_admin") is False:
        assert auth._is_system_admin(source.user_id) is False
        # The password directory reader retains native home-project admin;
        # revoking requester authority must not also revoke metadata access.
        directory_home = await auth.resolve_project_authority(DIRECTORY_ID, DIRECTORY_PROJECT_ID)
        assert directory_home == {"roles": ["admin", "member"], "is_system_admin": False}
        assert native_directory.client.get("/v3/roles", headers={"X-Auth-Token": DIRECTORY_TOKEN}).status_code == 200
    # Valid issued snapshots cannot stand in for current directory authority.
    snapshot = native_directory.client.get(
        "/v3/auth/tokens", headers={"X-Auth-Token": DIRECTORY_TOKEN, "X-Subject-Token": source.token},
    )
    assert snapshot.status_code == 200
    document = snapshot.json()["token"]
    assert document["user"]["id"] == source.user_id
    assert {role["name"] for role in document["roles"]} == {"admin", "member"}
    assert document.get("project", {}).get("id", "") == source.connection_project_id
    assert document.get("system", {}).get("all", False) is bool(source.system_scope)
    for method, path, payload, _store, _status in _AUTH_REQUESTS:
        response = await native_routes.client.request(method, path, json=payload, headers=source.headers)
        assert response.status_code == status, (method, path, response.text)
    for mock in native_routes.storage.values():
        mock.assert_not_awaited()


@pytest.mark.parametrize("token", _PROJECTLESS_TOKENS)
async def test_native_http_projectless_admin_requires_explicit_target(native_directory, native_routes, token):
    assert auth._is_system_admin(PROJECTLESS_ADMIN_ID) is True
    for method, path, payload, _store, _status in _AUTH_REQUESTS:
        response = await native_routes.client.request(method, path, json=payload, headers=_headers(token, None))
        assert response.status_code == 401, (method, path, response.text)
    for mock in native_routes.storage.values():
        mock.assert_not_awaited()


@pytest.mark.parametrize("target", [None, PROJECT_ID], ids=["missing-target", "explicit-target"])
async def test_native_http_projectless_nonadmin_denied(native_directory, native_routes, target):
    native_directory.configure({"owner_roles": []})
    assert auth._is_system_admin(OWNER_ID) is False
    headers = _headers(OWNER_UNSCOPED_TOKEN, None, target_project_id=target)
    for method, path, payload, _store, _status in _AUTH_REQUESTS:
        response = await native_routes.client.request(method, path, json=payload, headers=headers)
        assert response.status_code == (403 if target else 401), (method, path, response.text)
    for mock in native_routes.storage.values():
        mock.assert_not_awaited()


@pytest.mark.parametrize("roles", [["project_member", "manager"], ["project_member", "project_admin"]],
                         ids=["native-project-manager", "project-admin"])
async def test_native_http_unentitled_project_roles_denied(native_directory, native_routes, roles):
    native_directory.configure({"owner_roles": roles})
    current = await auth.resolve_project_authority(OWNER_ID, PROJECT_ID)
    assert current["is_system_admin"] is False
    assert capabilities({"user_id": OWNER_ID, "project_id": PROJECT_ID, "roles": current["roles"]}) == frozenset()
    headers = {**_headers(OWNER_TOKEN, PROJECT_ID), "X-Roles": "lumen_admin", "X-Is-System-Admin": "true"}
    for method, path, payload, _store, _status in _AUTH_REQUESTS:
        response = await native_routes.client.request(method, path, json=payload, headers=headers)
        assert response.status_code == 403, (method, path, response.text)
    for mock in native_routes.storage.values():
        mock.assert_not_awaited()


async def test_native_http_entitled_user_scoped_crud_still_works(native_directory, native_routes):
    # Ordinary project membership plus current leaves, not a native admin label.
    current = await auth.resolve_project_authority(OWNER_ID, PROJECT_ID)
    assert current["is_system_admin"] is False
    assert capabilities({"user_id": OWNER_ID, "project_id": PROJECT_ID, "roles": current["roles"]}) == SERVICE_CAPABILITIES
    headers = _headers(OWNER_TOKEN, PROJECT_ID)
    for method, path, payload, _store, status in _SCOPED_REQUESTS:
        response = await native_routes.client.request(method, path, json=payload, headers=headers)
        assert response.status_code == status, (method, path, response.text)
    storage = native_routes.storage
    storage["extension_list_user"].assert_awaited_once_with("tool", user_id=OWNER_ID, project_id=PROJECT_ID)
    storage["extension_create"].assert_awaited_once_with(
        "tool", _TOOL_BODY, scope="user", owner_user_id=OWNER_ID, owner_project_id=PROJECT_ID,
    )
    storage["extension_delete"].assert_awaited_once_with(
        "tool", 1, requester_user_id=OWNER_ID, requester_project_id=PROJECT_ID,
    )
    for method, path, payload, store, _status in _ADMIN_REQUESTS:
        before = storage[store].await_count
        response = await native_routes.client.request(method, path, json=payload, headers=headers)
        assert response.status_code == 403, (method, path, response.text)
        assert storage[store].await_count == before


@pytest.mark.parametrize("target_roles", [[], ["project_member", "lumen_admin"]], ids=["no-target-roles", "retained-target-roles"])
async def test_native_http_revoked_system_assignment_rejects_foreign_target(native_directory, native_routes, target_roles):
    native_directory.configure({"directory_target_roles": target_roles})
    headers = _headers(target_project_id=PROJECT_ID)
    assert (await native_routes.client.get("/v1/admin/providers", headers=headers)).status_code == 200
    for mock in native_routes.storage.values():
        mock.reset_mock()
    native_directory.configure({"directory_system_admin": False})
    # Native token exchange and privileged directory reads still work. Only the
    # caller's current system assignment disappeared; admin in the token did not.
    snapshot = auth.validate_token(DIRECTORY_TOKEN, project_id=DIRECTORY_PROJECT_ID)
    assert snapshot["roles"] == ["admin", "member"]
    assert snapshot["is_system_admin"] is False
    assert snapshot["connection_project_id"] == DIRECTORY_PROJECT_ID
    home = await auth.resolve_project_authority(DIRECTORY_ID, DIRECTORY_PROJECT_ID)
    assert home == {"roles": ["admin", "member"], "is_system_admin": False}
    assert native_directory.client.get("/v3/roles", headers={"X-Auth-Token": DIRECTORY_TOKEN}).status_code == 200
    for method, path, payload, _store, _status in _AUTH_REQUESTS:
        response = await native_routes.client.request(method, path, json=payload, headers=headers)
        assert response.status_code == 403, (method, path, response.text)
    for mock in native_routes.storage.values():
        mock.assert_not_awaited()
    for method, path, payload, _store, _status in _ADMIN_REQUESTS:
        home_response = await native_routes.client.request(method, path, json=payload, headers=_headers())
        assert home_response.status_code == 403, (method, path, home_response.text)
    # A native home-project admin retains project actions, never system APIs
    # or a foreign target after raw system assignment revocation.
    for method, path, payload, _store, status in _SCOPED_REQUESTS:
        home_response = await native_routes.client.request(method, path, json=payload, headers=_headers())
        assert home_response.status_code == status, (method, path, home_response.text)


@pytest.mark.parametrize("path", ["/v1/admin/providers", "/v1/custom-tools"], ids=["require-token", "get-principal"])
async def test_native_http_final_authority_rejects_mid_request_system_revocation(
    native_directory, native_routes, native_admin_source, path,
):
    native_directory.configure({
        "directory_target_roles": ["project_member", "lumen_admin"],
        "revoke_system_admin_after_checks": 1,
    })
    # The first system check observes admin, then Keystone revokes it before
    # the final project-authority read. PROJECT retains target leaves; the
    # projectless requester has none. Neither may retain foreign target access.
    response = await native_routes.client.get(path, headers=native_admin_source.headers)
    assert response.status_code == 403, response.text
    assert auth._is_system_admin(native_admin_source.user_id) is False
    if native_admin_source.connection_project_id:
        assert "Target project override" in response.json()["detail"]
        target = await auth.resolve_project_authority(native_admin_source.user_id, PROJECT_ID)
        assert target["is_system_admin"] is False
        assert capabilities({"user_id": OWNER_ID, "project_id": PROJECT_ID, "roles": target["roles"]}) == SERVICE_CAPABILITIES
    else:
        assert "Current project membership required" in response.json()["detail"]
        with pytest.raises(HTTPException) as exc:
            await auth.resolve_project_authority(native_admin_source.user_id, PROJECT_ID)
        assert exc.value.status_code == 403
    directory_home = await auth.resolve_project_authority(DIRECTORY_ID, DIRECTORY_PROJECT_ID)
    assert directory_home == {"roles": ["admin", "member"], "is_system_admin": False}
    for mock in native_routes.storage.values():
        mock.assert_not_awaited()


@pytest.mark.parametrize("logical_project_id", [DIRECTORY_PROJECT_ID, PROJECT_ID], ids=["home", "target"])
async def test_native_http_authority_outage_fails_closed_with_valid_admin_token(native_directory, native_routes, logical_project_id):
    native_directory.configure({"authority_unavailable": True})
    token_response = native_directory.client.get(
        "/v3/auth/tokens", headers={"X-Auth-Token": DIRECTORY_TOKEN, "X-Subject-Token": DIRECTORY_TOKEN},
    )
    assert token_response.status_code == 200
    assert {role["name"] for role in token_response.json()["token"]["roles"]} == {"admin", "member"}
    with pytest.raises(HTTPException) as exc:
        auth._is_system_admin(DIRECTORY_ID)
    assert exc.value.status_code == 503
    for method, path, payload, _store, _status in _AUTH_REQUESTS:
        response = await native_routes.client.request(method, path, json=payload, headers=_admin_headers(logical_project_id))
        assert response.status_code == 503, (method, path, response.text)
    for mock in native_routes.storage.values():
        mock.assert_not_awaited()


@pytest.mark.parametrize("patch", [{"project_enabled": False}, {"project_exists": False}], ids=["disabled", "deleted"])
async def test_native_http_system_admin_disabled_or_deleted_target_denied(native_directory, native_routes, patch):
    native_directory.configure(patch)
    assert auth._is_system_admin(DIRECTORY_ID) is True
    for method, path, payload, _store, _status in _AUTH_REQUESTS:
        response = await native_routes.client.request(method, path, json=payload, headers=_headers(target_project_id=PROJECT_ID))
        assert response.status_code == 403, (method, path, response.text)
    for mock in native_routes.storage.values():
        mock.assert_not_awaited()
    # Deleting/disabling the logical target never invalidates the original home token.
    response = await native_routes.client.get("/v1/admin/providers", headers=_headers())
    assert response.status_code == 200
    native_routes.storage["provider_list"].assert_awaited_once_with()


@pytest.mark.parametrize("scope", [
    "compat:completions:write", "native:images:write", "native:audio:write", "native:tools:execute",
])
@pytest.mark.parametrize(("patch", "error"), [
    pytest.param({"directory_system_admin": False}, api_key_store.ApiKeyForbidden, id="revoked"),
    pytest.param({"authority_unavailable": True}, api_key_store.ApiKeyAuthorityUnavailable, id="outage"),
    pytest.param({"project_enabled": False}, api_key_store.ApiKeyForbidden, id="disabled-target"),
    pytest.param({"project_exists": False}, api_key_store.ApiKeyForbidden, id="deleted-target"),
])
async def test_native_sdk_zero_target_role_admin_pre_io_authority_fenced(native_directory, scope, patch, error):
    assert await auth.resolve_project_authority(DIRECTORY_ID, PROJECT_ID) == {
        "roles": [], "is_system_admin": True,
    }
    session = SimpleNamespace(execute=AsyncMock(side_effect=AssertionError("Web execution must not load an API-key row")))
    provider_io = AsyncMock()

    async def start_io():
        await api_key_store.authorize_api_key_in_transaction(
            session, api_key_id=None, user_id=DIRECTORY_ID, project_id=PROJECT_ID, required_scopes=(scope,),
        )
        await provider_io()

    # Exercise the existing durable web pre-I/O gate against installed Keystone
    # HTTP, with no target project grants, key row, or service-account substitution.
    await start_io()
    provider_io.assert_awaited_once_with()
    provider_io.reset_mock()
    native_directory.configure(patch)
    snapshot = native_directory.client.get(
        "/v3/auth/tokens", headers={"X-Auth-Token": DIRECTORY_TOKEN, "X-Subject-Token": DIRECTORY_TOKEN},
    )
    assert snapshot.status_code == 200
    assert {role["name"] for role in snapshot.json()["token"]["roles"]} == {"admin", "member"}
    assert snapshot.json()["token"]["project"]["id"] == DIRECTORY_PROJECT_ID
    with pytest.raises(error):
        await start_io()
    provider_io.assert_not_awaited()
    session.execute.assert_not_awaited()


_SYSTEM_MODES = ["direct", "group", "inferred", "group-inferred"]
_SYSTEM_REVOCATIONS = [
    pytest.param("direct", "grant", id="direct-grant"),
    pytest.param("group", "grant", id="group-grant"),
    pytest.param("group", "group", id="group-membership"),
    pytest.param("inferred", "edge", id="direct-inference-edge"),
    pytest.param("group-inferred", "edge", id="group-inference-edge"),
]


def _issued_roles(*names):
    return [{"id": role_id(name), "name": name} for name in names]


def _caller_exchanges(native_directory):
    return [query for method, path, query in native_directory.state.requests
            if method == "POST" and path == "/v3/auth/tokens" and query.get("methods") == ["token"]]


@pytest.mark.parametrize("mode", _SYSTEM_MODES)
async def test_native_sdk_effective_system_empty_raw_grants_authorize(native_directory, mode):
    native_directory.configure({"system_admin_mode": mode})
    ks = auth._get_admin_ks_client()
    # This is the installed python-keystoneclient manager, not fixture internals.
    assert ks.role_assignments.list(user=PROJECTLESS_ADMIN_ID, system="all", effective=True) == []
    assert ks.role_assignments.list(group=SYSTEM_GROUP_ID, system="all", effective=True) == []
    direct = ks.role_assignments.list(user=PROJECTLESS_ADMIN_ID, system="all")
    groups = ks.role_assignments.list(group=SYSTEM_GROUP_ID, system="all")
    rows = groups if mode in {"group", "group-inferred"} else direct
    assert len(rows) == 1
    assert (direct == []) is (mode in {"group", "group-inferred"})
    expected_role = SYSTEM_PARENT if mode in {"inferred", "group-inferred"} else "admin"
    assert rows[0].to_dict()["role"] == {"id": role_id(expected_role)}
    assert rows[0].to_dict()["scope"] == {"system": {"all": True}}
    assert {group.id for group in ks.groups.list(user=PROJECTLESS_ADMIN_ID)} == {SYSTEM_GROUP_ID}
    assert ks.role_assignments.list(user=PROJECTLESS_ADMIN_ID, project=PROJECT_ID, effective=True) == []
    assert await auth.resolve_project_authority(PROJECTLESS_ADMIN_ID, PROJECT_ID) == {
        "roles": [], "is_system_admin": True,
    }
    # Issued admin/member names do not establish native target membership.
    assert native_directory.client.get("/v3/users/" + PROJECTLESS_ADMIN_ID,
        headers={"X-Auth-Token": DIRECTORY_TOKEN}).json()["user"]["domain_id"] == "default"


@pytest.mark.parametrize("mode", _SYSTEM_MODES)
async def test_native_http_raw_system_zero_membership_full_crud_and_keys(
    native_directory, native_routes, native_key_storage, mode,
):
    native_directory.configure({"system_admin_mode": mode})
    headers = _headers(PROJECTLESS_SYSTEM_TOKEN, None, target_project_id=PROJECT_ID)
    for method, path, payload, store, status in _AUTH_REQUESTS:
        before = native_routes.storage[store].await_count
        response = await native_routes.client.request(method, path, json=payload, headers=headers)
        assert response.status_code == status, (method, path, response.text)
        assert native_routes.storage[store].await_count == before + 1
    policy = await native_routes.client.get("/v1/api-keys/issuance-policy", headers=headers)
    assert policy.status_code == 200, policy.text
    assert set(policy.json()["allowed_scopes"]) == api_key_store.API_KEY_SCOPES
    response = await native_routes.client.post("/v1/api-keys", headers=headers,
        json={"name": "zero-membership", "scopes": ["native:images:write"]})
    assert response.status_code == 201, response.text
    assert response.json()["key"].startswith("sk-afgl-")
    assert response.json()["scopes"] == ["native:images:write"]
    assert len(native_key_storage.rows) == 1
    row = native_key_storage.rows[0]
    assert (row.owner_user_id, row.owner_project_id) == (PROJECTLESS_ADMIN_ID, PROJECT_ID)
    assert row.key_hash == api_key_store._hash_key(response.json()["key"])


@pytest.fixture
def native_key_storage(monkeypatch):
    """Replace SQL persistence only; issuance and every authority gate stay real."""
    rows = []

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def begin(self):
            return self

        def add(self, row):
            row.id = len(rows) + 1
            rows.append(row)

        async def flush(self):
            pass

    monkeypatch.setattr(api_key_store, "_require_db", lambda: Session)
    quota = AsyncMock(return_value=SimpleNamespace(monthly=None, weekly=None))
    monkeypatch.setattr(api_key_store, "_get_system_quota", quota)
    return SimpleNamespace(rows=rows, quota=quota)


@pytest.mark.parametrize(("mode", "revocation"), _SYSTEM_REVOCATIONS)
@pytest.mark.parametrize("path", ["/v1/admin/providers", "/v1/custom-tools", "/v1/api-keys/issuance-policy"])
async def test_native_http_final_raw_system_revocation_zero_target_membership(
    native_directory, native_routes, native_key_storage, mode, revocation, path,
):
    native_directory.configure({"system_admin_mode": mode, "system_revocation": revocation,
                                "revoke_system_admin_after_checks": 1})
    headers = _headers(PROJECTLESS_SYSTEM_TOKEN, None, target_project_id=PROJECT_ID)
    response = await native_routes.client.get(path, headers=headers)
    assert response.status_code == 403, response.text
    assert auth._is_system_admin(PROJECTLESS_ADMIN_ID) is False
    for store in native_routes.storage.values():
        store.assert_not_awaited()
    assert native_key_storage.rows == []
    native_key_storage.quota.assert_not_awaited()
    assert _caller_exchanges(native_directory) == []


@pytest.mark.parametrize(("mode", "revocation"), _SYSTEM_REVOCATIONS)
@pytest.mark.parametrize("key", [False, True], ids=["web", "existing-key"])
async def test_native_sdk_raw_system_pre_io_revocation_zero_target_membership(
    native_directory, mode, revocation, key,
):
    native_directory.configure({"system_admin_mode": mode})
    scope = "compat:completions:write"
    row, _raw = await api_key_store.prepare_key_record(PROJECTLESS_ADMIN_ID, PROJECT_ID, "live owner", [scope])
    row.id = 71
    session = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: row)))
    io = AsyncMock()

    async def start_io():
        await api_key_store.authorize_api_key_in_transaction(session, api_key_id=row.id if key else None,
            user_id=PROJECTLESS_ADMIN_ID, project_id=PROJECT_ID, required_scopes=(scope,))
        await io()

    await start_io()
    io.assert_awaited_once_with()
    io.reset_mock()
    session.execute.reset_mock()
    patch = {"directory_system_admin": False} if revocation == "grant" else (
        {"system_group_members": []} if revocation == "group" else {
            "remove_edges": [{"prior": SYSTEM_PARENT, "implied": "admin"}]})
    native_directory.configure(patch)
    snapshot = native_directory.client.get("/v3/auth/tokens",
        headers={"X-Auth-Token": DIRECTORY_TOKEN, "X-Subject-Token": PROJECTLESS_SYSTEM_TOKEN})
    assert snapshot.status_code == 200
    assert {role["name"] for role in snapshot.json()["token"]["roles"]} == {"admin", "member"}
    with pytest.raises(api_key_store.ApiKeyForbidden):
        await start_io()
    io.assert_not_awaited()
    session.execute.assert_not_awaited()


_RESTRICTED_ORIGINALS = [
    pytest.param({"OS-TRUST:trust": {"id": "synthetic-trust", "trustor_user_id": OWNER_ID,
                                   "trustee_user_id": DIRECTORY_ID, "impersonation": True}}, id="trust"),
    pytest.param({"methods": ["application_credential"], "application_credential": {
        "id": "synthetic-appcred", "unrestricted": True}}, id="appcred-method"),
    pytest.param({"application_credential": {"id": "synthetic-appcred"}}, id="appcred-marker"),
    pytest.param({"methods": ["oauth1"], "OS-OAUTH1": {"access_token_id": "synthetic-oauth"}}, id="oauth1"),
    pytest.param({"OS-OAUTH1:access_token": {"id": "synthetic-oauth"}}, id="oauth1-access-token"),
    pytest.param({"methods": ["oauth2"], "OS-OAUTH2": {"access_token_id": "synthetic-oauth"}}, id="oauth2"),
    pytest.param({"OS-OAUTH2:access_token": {"id": "synthetic-oauth"}}, id="oauth2-access-token"),
    pytest.param({"oauth1": {"id": "synthetic-oauth"}}, id="oauth1-marker"),
    pytest.param({"oauth2": {"id": "synthetic-oauth"}}, id="oauth2-marker"),
    pytest.param({"oauth2_credential": {"id": "synthetic-oauth"}}, id="oauth2-credential-marker"),
    pytest.param({"methods": []}, id="missing-human-method"),
    pytest.param({"methods": ["unrecognized-auth-method"]}, id="unknown-method"),
]
_NATIVE_DEPENDENCIES = ["/native/require-token", "/native/principal"]


@pytest.mark.parametrize("metadata", _RESTRICTED_ORIGINALS)
@pytest.mark.parametrize("path", _NATIVE_DEPENDENCIES)
async def test_native_http_original_restricted_ids_never_inherit_owner_system_or_parents(
    native_directory, native_routes, metadata, path,
):
    names = ("member", "lumen-inventory_reader", "lumen-assets_editor", "lumen_user")
    native_directory.configure({
        "system_admin_mode": "group",
        "system_group_members": [DIRECTORY_ID, PROJECTLESS_ADMIN_ID, OWNER_ID],
        "token_overrides": {OWNER_TOKEN: {**metadata, "roles": _issued_roles(*names)}},
    })
    assert auth._is_system_admin(OWNER_ID) is True
    # Same-project selection is not a reissue that can erase Trust/appcred/OAuth.
    for selector in (None, PROJECT_ID):
        response = await native_routes.client.get(path, headers=_headers(OWNER_TOKEN, selector))
        assert response.status_code == 200, response.text
        info = response.json()
        assert info["credential_restricted"] is True
        assert info["is_system_admin"] is False
        assert info["roles"] == sorted(names)
        assert "lumen-chat_user" not in info["roles"]
        assert info["issued_role_ids"] == sorted(role_id(name) for name in names)
        assert info["token"] == OWNER_TOKEN
        assert info["connection_project_id"] == info["source_project_id"] == PROJECT_ID
    assert _caller_exchanges(native_directory) == []
    assert any(method == "GET" and endpoint == "/v3/auth/tokens" and "nocatalog" in query
               for method, endpoint, query in native_directory.state.requests)
    # Both ways of requesting another project are forbidden before exchange.
    for headers in (_headers(OWNER_TOKEN, DIRECTORY_PROJECT_ID),
                    _headers(OWNER_TOKEN, PROJECT_ID, target_project_id=DIRECTORY_PROJECT_ID)):
        response = await native_routes.client.get(path, headers=headers)
        assert response.status_code == 403, response.text
    assert _caller_exchanges(native_directory) == []


@pytest.mark.parametrize("metadata", _RESTRICTED_ORIGINALS)
async def test_native_http_restricted_original_crud_and_key_scope_boundaries(
    native_directory, native_routes, native_key_storage, metadata,
):
    names = ("member", "lumen-inventory_reader", "lumen-assets_editor", "lumen-keys_editor")
    native_directory.configure({"token_overrides": {OWNER_TOKEN: {**metadata, "roles": _issued_roles(*names)}}})
    headers = _headers(OWNER_TOKEN, PROJECT_ID)
    for method, path, payload, store, status in _SCOPED_REQUESTS:
        before = native_routes.storage[store].await_count
        response = await native_routes.client.request(method, path, json=payload, headers=headers)
        expected = 403 if method == "DELETE" else status
        assert response.status_code == expected, (method, path, response.text)
        assert native_routes.storage[store].await_count == before + (expected != 403)
    for method, path, payload, store, _status in _ADMIN_REQUESTS:
        before = native_routes.storage[store].await_count
        response = await native_routes.client.request(method, path, json=payload, headers=headers)
        assert response.status_code == 403, (method, path, response.text)
        assert native_routes.storage[store].await_count == before
    policy = await native_routes.client.get("/v1/api-keys/issuance-policy", headers=headers)
    assert policy.status_code == 200, policy.text
    from lumen.service_authority import allowed_scopes
    assert set(policy.json()["allowed_scopes"]) == allowed_scopes({"user_id": OWNER_ID, "project_id": PROJECT_ID, "roles": names}) & api_key_store.API_KEY_SCOPES
    # Narrow issued leaves cannot borrow the owner's unrestricted grade.
    response = await native_routes.client.post("/v1/api-keys", headers=headers,
        json={"name": "cannot-launder-chat", "scopes": ["compat:completions:write"]})
    assert response.status_code == 403, response.text
    assert native_key_storage.rows == []
    response = await native_routes.client.post("/v1/api-keys", headers=headers,
        json={"name": "inventory-only", "scopes": ["models:read"]})
    assert response.status_code == 201, response.text
    assert response.json()["scopes"] == ["models:read"]
    assert len(native_key_storage.rows) == 1
    assert _caller_exchanges(native_directory) == []


@pytest.mark.parametrize("metadata", [
    pytest.param({"bind": {"kerberos": "synthetic@EXAMPLE"}}, id="bind"),
    pytest.param({"access_rules": [{"service": "compute", "method": "GET", "path": "/servers"}]}, id="access-rules"),
    pytest.param({"application_credential": {"id": "synthetic-appcred", "access_rules": [
        {"service": "compute", "method": "GET", "path": "/servers"}]}}, id="appcred-access-rules"),
])
async def test_native_http_original_bind_and_access_rules_rejected_before_storage(
    native_directory, native_routes, native_key_storage, metadata,
):
    native_directory.configure({"token_overrides": {OWNER_TOKEN: metadata}})
    headers = _headers(OWNER_TOKEN, PROJECT_ID)
    for path in _NATIVE_DEPENDENCIES:
        response = await native_routes.client.get(path, headers=headers)
        assert response.status_code == 403, response.text
    for method, path, payload, _store, _status in _AUTH_REQUESTS:
        response = await native_routes.client.request(method, path, json=payload, headers=headers)
        assert response.status_code == 403, (method, path, response.text)
    for method, path, body in [("GET", "/v1/api-keys/issuance-policy", None),
                               ("POST", "/v1/api-keys", {"scopes": ["models:read"]})]:
        response = await native_routes.client.request(method, path, json=body, headers=headers)
        assert response.status_code == 403, response.text
    for store in native_routes.storage.values():
        store.assert_not_awaited()
    assert native_key_storage.rows == []
    native_key_storage.quota.assert_not_awaited()
    assert _caller_exchanges(native_directory) == []


@pytest.mark.parametrize("method", ["password", "token", "totp", "mapped", "external", "oidc", "saml2"])
@pytest.mark.parametrize("path", _NATIVE_DEPENDENCIES)
async def test_native_http_human_original_methods_keep_live_authority(native_directory, native_routes, method, path):
    native_directory.configure({"token_overrides": {DIRECTORY_TOKEN: {"methods": [method]}}})
    response = await native_routes.client.get(path, headers=_headers(target_project_id=PROJECT_ID))
    assert response.status_code == 200, response.text
    assert response.json()["credential_restricted"] is False
    assert response.json()["is_system_admin"] is True
    assert response.json()["roles"] == []
    assert _caller_exchanges(native_directory) == []


@pytest.mark.parametrize("path", _NATIVE_DEPENDENCIES)
async def test_native_http_human_explicit_rescope_keeps_original_metadata(native_directory, native_routes, path):
    response = await native_routes.client.get(path, headers=_headers(OWNER_UNSCOPED_TOKEN, PROJECT_ID))
    assert response.status_code == 200, response.text
    assert response.json()["source_project_id"] == ""
    assert response.json()["connection_project_id"] == PROJECT_ID
    assert response.json()["token"] == OWNER_TOKEN
    assert response.json()["credential_restricted"] is False
    assert _caller_exchanges(native_directory) == [{"methods": ["token"], "scope": {"project": {"id": PROJECT_ID}}}]


def test_native_sdk_original_token_metadata_survives_directory_mutations(native_directory):
    metadata = {"methods": ["application_credential"], "application_credential": {"id": "synthetic-appcred"},
                "roles": _issued_roles("member", "lumen-inventory_reader"), "audit_ids": ["original-audit"]}
    native_directory.configure({"token_overrides": {OWNER_TOKEN: metadata}})
    ks = auth._get_admin_ks_client()
    before = ks.tokens.get_token_data(OWNER_TOKEN, include_catalog=False)
    native_directory.configure({"owner_roles": [], "remove_edges": [{"prior": "lumen_reader", "implied": "lumen-inventory_reader"}]})
    after = ks.tokens.get_token_data(OWNER_TOKEN, include_catalog=False)
    assert before == after
    assert "catalog" not in after["token"]
    for key, value in metadata.items():
        assert after["token"][key] == value
    assert after["token"]["issued_at"] and after["token"]["expires_at"]


@pytest.mark.parametrize(("patch", "status"), [
    pytest.param({"domains": {}}, 403, id="deleted-domain"),
    pytest.param({"domains": {"default": {"id": "default", "enabled": False}}}, 403, id="disabled-domain"),
    pytest.param({"domains": {"default": {"id": "default"}}}, 503, id="missing-domain-enabled"),
    pytest.param({"domains": {"default": {"id": "default", "enabled": "true"}}}, 503, id="string-domain-enabled"),
    pytest.param({"domains": {"default": {"id": "default", "enabled": 1}}}, 503, id="integer-domain-enabled"),
    pytest.param({"domains": {"default": {"id": "wrong-domain", "enabled": True}}}, 503, id="domain-id-mismatch"),
    pytest.param({"user_overrides": {"id": "wrong-user"}}, 503, id="user-id-mismatch"),
    pytest.param({"project_overrides": {"id": "wrong-project"}}, 503, id="project-id-mismatch"),
    pytest.param({"user_overrides": {"enabled": None}}, 503, id="missing-user-enabled"),
    pytest.param({"project_overrides": {"enabled": "true"}}, 503, id="string-project-enabled"),
    pytest.param({"user_domain_id": ""}, 503, id="missing-user-domain"),
    pytest.param({"project_domain_id": ""}, 503, id="missing-project-domain"),
])
async def test_native_http_native_domain_identity_and_boolean_validation(
    native_directory, native_routes, native_admin_source, patch, status,
):
    native_directory.configure(patch)
    for path in _NATIVE_DEPENDENCIES:
        response = await native_routes.client.get(path, headers=native_admin_source.headers)
        assert response.status_code == status, (path, response.text)
    for store in native_routes.storage.values():
        store.assert_not_awaited()


@pytest.mark.parametrize("disabled", [None, "user-domain", "project-domain"])
async def test_native_sdk_distinct_user_and_project_domains_both_required(native_directory, disabled):
    domains = {name: {"id": name, "name": name, "enabled": name != disabled}
               for name in ("user-domain", "project-domain")}
    native_directory.configure({"user_domain_id": "user-domain", "project_domain_id": "project-domain", "domains": domains})
    if disabled:
        with pytest.raises(HTTPException) as exc:
            await auth.resolve_project_authority(PROJECTLESS_ADMIN_ID, PROJECT_ID)
        assert exc.value.status_code == 403
    else:
        assert await auth.resolve_project_authority(PROJECTLESS_ADMIN_ID, PROJECT_ID) == {
            "roles": [], "is_system_admin": True,
        }
        endpoints = {path for method, path, _query in native_directory.state.requests if method == "GET"}
        assert {"/v3/domains/user-domain", "/v3/domains/project-domain"} <= endpoints


@pytest.mark.parametrize(("token", "patch", "status"), [
    pytest.param("unknown-original-token", {}, 401, id="invalid-original"),
    pytest.param(OWNER_TOKEN, {"unavailable": True}, 503, id="original-validation-outage"),
    pytest.param(OWNER_TOKEN, {"authority_unavailable": True}, 503, id="current-authority-outage"),
    pytest.param(OWNER_TOKEN, {"token_overrides": {OWNER_TOKEN: {"methods": "token"}}}, 503, id="malformed-methods"),
    pytest.param(OWNER_TOKEN, {"token_overrides": {OWNER_TOKEN: {"methods": [None]}}}, 503, id="malformed-method-entry"),
    pytest.param(OWNER_TOKEN, {"token_overrides": {OWNER_TOKEN: {"application_credential": "not-an-object"}}}, 503, id="malformed-appcred"),
    pytest.param(OWNER_TOKEN, {"token_overrides": {OWNER_TOKEN: {"roles": [{"name": "lumen_admin"}]}}}, 503, id="missing-original-role-id"),
    pytest.param(OWNER_TOKEN, {"token_overrides": {OWNER_TOKEN: {"roles": None}}}, 503, id="malformed-original-roles"),
    pytest.param(OWNER_TOKEN, {"token_overrides": {OWNER_TOKEN: {"user": {"id": ""}}}}, 503, id="missing-original-user-id"),
    pytest.param(OWNER_TOKEN, {"token_overrides": {OWNER_TOKEN: {"system": {"all": True}}}}, 503, id="conflicting-original-scopes"),
    pytest.param(PROJECTLESS_SYSTEM_TOKEN, {"token_overrides": {PROJECTLESS_SYSTEM_TOKEN: {"system": {"all": 1}}}}, 503, id="nonboolean-original-system"),
])
@pytest.mark.parametrize("path", _NATIVE_DEPENDENCIES)
async def test_native_http_invalid_unavailable_and_malformed_originals_fail_closed(
    native_directory, native_routes, token, patch, status, path,
):
    native_directory.configure(patch)
    response = await native_routes.client.get(path, headers=_headers(token, None, target_project_id=PROJECT_ID))
    assert response.status_code == status, response.text
    for store in native_routes.storage.values():
        store.assert_not_awaited()
    assert _caller_exchanges(native_directory) == []


@pytest.mark.parametrize("patch", [
    pytest.param({"owner_roles": []}, id="membership-revoked"),
    pytest.param({"owner_enabled": False}, id="owner-disabled"),
    pytest.param({"project_enabled": False}, id="project-disabled"),
    pytest.param({"remove_edges": [{"prior": "lumen_editor", "implied": "lumen-assets_editor"}]}, id="issued-leaf-edge-revoked"),
])
async def test_native_http_restricted_original_requires_fresh_membership_and_issued_leaf(
    native_directory, native_routes, patch,
):
    native_directory.configure({"system_admin_mode": "group",
        "system_group_members": [DIRECTORY_ID, PROJECTLESS_ADMIN_ID, OWNER_ID],
        "token_overrides": {OWNER_TOKEN: {"application_credential": {"id": "synthetic-appcred"},
            "roles": _issued_roles("member", "lumen-assets_editor")}}})
    headers = _headers(OWNER_TOKEN, PROJECT_ID)
    before = await native_routes.client.post("/v1/custom-tools", headers=headers, json=_TOOL_BODY)
    assert before.status_code == 201, before.text
    native_routes.storage["extension_create"].reset_mock()
    native_directory.configure(patch)
    # Current user-level system admin cannot bypass reduced credential membership.
    assert auth._is_system_admin(OWNER_ID) is True
    response = await native_routes.client.post("/v1/custom-tools", headers=headers, json=_TOOL_BODY)
    assert response.status_code == 403, response.text
    native_routes.storage["extension_create"].assert_not_awaited()
    assert _caller_exchanges(native_directory) == []


@pytest.mark.parametrize(("mode", "revocation"), _SYSTEM_REVOCATIONS)
@pytest.mark.parametrize("operation", ["policy", "create"])
async def test_native_http_key_issue_pre_storage_raw_authority_revocation(
    native_directory, native_routes, native_key_storage, mode, revocation, operation,
):
    native_directory.configure({"system_admin_mode": mode, "system_revocation": revocation,
        "revoke_system_admin_after_checks": 2})
    # Validation and the final auth dependency observe authority. Its removal
    # must still fence the independent key-issuance read, before any SQL access.
    headers = _headers(PROJECTLESS_SYSTEM_TOKEN, None, target_project_id=PROJECT_ID)
    if operation == "policy":
        response = await native_routes.client.get("/v1/api-keys/issuance-policy", headers=headers)
    else:
        response = await native_routes.client.post("/v1/api-keys", headers=headers,
            json={"name": "revoked-before-issue", "scopes": ["models:read"]})
    assert response.status_code == 403, response.text
    assert auth._is_system_admin(PROJECTLESS_ADMIN_ID) is False
    assert native_key_storage.rows == []
    native_key_storage.quota.assert_not_awaited()
    assert _caller_exchanges(native_directory) == []


@pytest.mark.parametrize(("roles", "scopes"), [
    pytest.param(("member", "lumen-inventory_reader"), ["models:read"], id="no-keys-editor"),
    pytest.param(("member", "lumen-keys_editor"), ["models:read"], id="no-requested-leaf"),
    pytest.param(("member", "lumen-keys_editor", "lumen-inventory_reader"),
                 ["models:read", "compat:completions:write"], id="every-requested-scope-required"),
])
async def test_native_http_key_issue_requires_original_keys_editor_and_all_requested_leaves(
    native_directory, native_routes, native_key_storage, roles, scopes,
):
    native_directory.configure({"token_overrides": {OWNER_TOKEN: {
        "application_credential": {"id": "synthetic-appcred"}, "roles": _issued_roles(*roles)}}})
    headers = _headers(OWNER_TOKEN, PROJECT_ID)
    response = await native_routes.client.post("/v1/api-keys", headers=headers,
        json={"name": "cannot-increase-original-authority", "scopes": scopes})
    assert response.status_code == 403, response.text
    if "lumen-keys_editor" not in capabilities({"user_id": OWNER_ID, "project_id": PROJECT_ID, "roles": roles}):
        policy = await native_routes.client.get("/v1/api-keys/issuance-policy", headers=headers)
        assert policy.status_code == 403, policy.text
    assert native_key_storage.rows == []
    native_key_storage.quota.assert_not_awaited()
    assert _caller_exchanges(native_directory) == []


@pytest.mark.parametrize("domain_id", ["owner-home-domain", "target-native-domain"])
@pytest.mark.parametrize("key", [False, True], ids=["web-owner", "existing-key"])
async def test_native_sdk_owner_domain_revocation_fences_new_io(native_directory, domain_id, key):
    domains = {name: {"id": name, "enabled": True}
               for name in ("owner-home-domain", "target-native-domain")}
    native_directory.configure({"user_domain_id": "owner-home-domain",
                                "project_domain_id": "target-native-domain", "domains": domains})
    scope = "compat:completions:write"
    row, _raw = await api_key_store.prepare_key_record(OWNER_ID, PROJECT_ID, "domain-bound", [scope])
    row.id = 72
    session = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: row)))
    provider_io = AsyncMock()

    async def start_io():
        await api_key_store.authorize_api_key_in_transaction(
            session, api_key_id=row.id if key else None, user_id=OWNER_ID,
            project_id=PROJECT_ID, required_scopes=(scope,),
        )
        await provider_io()

    await start_io()
    provider_io.assert_awaited_once_with()
    provider_io.reset_mock()
    session.execute.reset_mock()
    domains[domain_id]["enabled"] = False
    native_directory.configure({"domains": domains})
    snapshot = native_directory.client.get("/v3/auth/tokens", headers={
        "X-Auth-Token": DIRECTORY_TOKEN, "X-Subject-Token": OWNER_TOKEN,
    })
    assert snapshot.status_code == 200
    with pytest.raises(api_key_store.ApiKeyForbidden):
        await start_io()
    session.execute.assert_not_awaited()
    provider_io.assert_not_awaited()


_AUTHORITY_COLLECTIONS = [
    pytest.param("/v3/roles", {}, "roles", id="role-catalog"),
    pytest.param("/v3/role_inferences", {}, "role_inferences", id="inference-graph"),
    pytest.param("/v3/role_assignments", {"user.id": PROJECTLESS_ADMIN_ID, "scope.system": "all"},
                 "role_assignments", id="raw-user-system-grants"),
    pytest.param("/v3/role_assignments", {"group.id": SYSTEM_GROUP_ID, "scope.system": "all"},
                 "role_assignments", id="raw-group-system-grants"),
    pytest.param("/v3/users/" + PROJECTLESS_ADMIN_ID + "/groups", {}, "groups", id="current-group-memberships"),
    pytest.param("/v3/role_assignments", {"user.id": PROJECTLESS_ADMIN_ID, "scope.project.id": PROJECT_ID},
                 "role_assignments", id="effective-target-memberships"),
]


@pytest.mark.parametrize(("endpoint", "query", "collection"), _AUTHORITY_COLLECTIONS)
@pytest.mark.parametrize("fault", [
    "missing-collection", "null-collection", "object-collection", "nonobject-row",
    "nonobject-envelope", "malformed-links", "next-page", "malformed-next-link", "truncated-flag",
])
async def test_native_http_incomplete_or_malformed_identity_collection_is_503(
    native_directory, native_routes, native_key_storage, endpoint, query, collection, fault,
):
    native_directory.configure({"system_admin_mode": "group"})
    # Start with a genuine HTTP envelope including valid admin-bearing rows.
    # Faults must remain visible through the installed SDK rather than being
    # replaced by a prebuilt manager list, iterator, or exception mock.
    response = native_directory.client.get(endpoint, params=query, headers={"X-Auth-Token": DIRECTORY_TOKEN})
    assert response.status_code == 200, response.text
    envelope = response.json()
    assert isinstance(envelope[collection], list)
    if fault == "missing-collection":
        envelope.pop(collection)
    elif fault == "null-collection":
        envelope[collection] = None
    elif fault == "object-collection":
        envelope[collection] = {"not": "a-list"}
    elif fault == "nonobject-row":
        envelope[collection] = ["not-an-identity-row"]
    elif fault == "nonobject-envelope":
        envelope = []
    elif fault == "malformed-links":
        envelope["links"] = ["not-a-links-object"]
    elif fault == "truncated-flag":
        envelope["truncated"] = True
        envelope["links"] = {"next": None, "previous": None}
    elif fault == "next-page":
        # Preserve all first-page rows: even a valid admin found on this page
        # cannot make an explicitly incomplete collection authoritative.
        envelope["links"] = {"next": endpoint + "?marker=unread-page", "previous": None}
    else:
        envelope["links"] = {"next": 17, "previous": None}
    native_directory.configure({"collection_overrides": [{"path": endpoint, "query": query, "payload": envelope}]})
    headers = _headers(PROJECTLESS_SYSTEM_TOKEN, None, target_project_id=PROJECT_ID)
    for path in _NATIVE_DEPENDENCIES + ["/v1/admin/providers", "/v1/custom-tools", "/v1/api-keys/issuance-policy"]:
        response = await native_routes.client.get(path, headers=headers)
        assert response.status_code == 503, (collection, fault, path, response.text)
    response = await native_routes.client.post("/v1/api-keys", headers=headers,
        json={"name": "partial-metadata-must-not-issue", "scopes": ["models:read"]})
    assert response.status_code == 503, response.text
    snapshot = native_directory.client.get("/v3/auth/tokens",
        headers={"X-Auth-Token": DIRECTORY_TOKEN, "X-Subject-Token": PROJECTLESS_SYSTEM_TOKEN})
    assert snapshot.status_code == 200
    for store in native_routes.storage.values():
        store.assert_not_awaited()
    assert native_key_storage.rows == []
    native_key_storage.quota.assert_not_awaited()
    assert _caller_exchanges(native_directory) == []


@pytest.mark.parametrize("metadata", _RESTRICTED_ORIGINALS)
async def test_native_sdk_exchange_can_erase_original_restrictions_but_native_auth_never_uses_it(
    native_directory, native_routes, metadata,
):
    from keystoneauth1 import session as ks_session
    from keystoneauth1.identity import v3

    names = ("member", "lumen-inventory_reader")
    native_directory.configure({"token_overrides": {OWNER_TOKEN: {
        **metadata, "roles": _issued_roles(*names)}}})
    directory = auth._get_admin_ks_client()
    original = directory.tokens.get_token_data(OWNER_TOKEN, include_catalog=False)["token"]
    for key, value in metadata.items():
        assert original[key] == value
    assert original["roles"] == _issued_roles(*names)
    assert _caller_exchanges(native_directory) == []

    # Positive control: authenticate the installed v3.Token plugin explicitly.
    # The fixture deliberately models a reissue whose response loses original
    # delegation markers and broadens role IDs. Merely constructing the plugin
    # would not demonstrate this security-sensitive boundary.
    plugin = v3.Token(auth_url=auth.get_settings().keystone_auth_url, token=OWNER_TOKEN, project_id=PROJECT_ID)
    session = ks_session.Session(auth=plugin, verify=True)
    try:
        exchanged = plugin.get_access(session)
    finally:
        session.session.close()
    assert exchanged.user_id == OWNER_ID
    assert exchanged.project_id == PROJECT_ID
    assert set(exchanged.role_ids) > {role_id(name) for name in names}
    assert exchanged.trust_scoped is False
    assert exchanged.application_credential_id is None
    # AccessInfo retains the exact SDK response here; inspect it rather than
    # asking the fixture's original-token inspection endpoint for a reissue.
    exchanged_document = exchanged._data["token"]
    assert exchanged_document["methods"] == ["token"]
    for key in metadata:
        if key != "methods":
            assert key not in exchanged_document
    exchanges = _caller_exchanges(native_directory)
    assert exchanges == [{"methods": ["token"], "scope": {"project": {"id": PROJECT_ID}}}]

    # Original inspection remains narrow. Both production dependencies must
    # use it, not repeat the demonstrably unsafe exchange performed above.
    assert directory.tokens.get_token_data(OWNER_TOKEN, include_catalog=False)["token"] == original
    for path in _NATIVE_DEPENDENCIES:
        response = await native_routes.client.get(path, headers=_headers(OWNER_TOKEN, PROJECT_ID))
        assert response.status_code == 200, response.text
        assert response.json()["credential_restricted"] is True
        assert response.json()["is_system_admin"] is False
        assert response.json()["roles"] == sorted(names)
    assert _caller_exchanges(native_directory) == exchanges


async def _admit_native_ceiling_run(native_routes, monkeypatch):
    """Real native planning/admission; replace only unrelated stores and routing."""
    from lumen.api import completions
    from lumen.models.chat_runs import ChatRun
    from lumen.services import chat_admission, mcp_adapter
    from lumen.services.durable_runs import admission

    class Session:
        def __init__(self):
            self.added = []
            self.execute = AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: None))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def begin(self):
            return self

        def add(self, row):
            self.added.append(row)

        async def flush(self):
            pass

    session = Session()
    resolved = {"model_name": "synthetic-text", "model_id": 1, "provider_name": "synthetic",
                "provider_id": 1, "config_version_hash": "a" * 64, "margin_multiplier": "1",
                "input_price_per_token": "0.000001", "output_price_per_token": "0.000002",
                "capabilities": {"context_limit": 16000, "max_output_tokens": 4096}}
    monkeypatch.setattr(chat_admission, "_resolve_model", AsyncMock(return_value=resolved))
    monkeypatch.setattr(chat_admission, "_resolve_extension_selection", AsyncMock(return_value={"tools": [], "mcp": []}))
    monkeypatch.setattr(chat_admission, "_resolve_feature_routes", AsyncMock(return_value={}))
    monkeypatch.setattr(chat_admission, "_load_skill_snapshot", AsyncMock(return_value=([], [])))
    monkeypatch.setattr(chat_admission, "resolve_summary_route", AsyncMock(return_value=None))
    monkeypatch.setattr(chat_admission, "_preview_tool_schemas", AsyncMock(return_value=[]))
    monkeypatch.setattr(completions.credit, "precheck", AsyncMock())
    monkeypatch.setattr(completions, "_admission_execution_protocol_version", lambda: 1)
    monkeypatch.setattr(admission, "existing_run_for_intent", AsyncMock(return_value=None))
    monkeypatch.setattr(admission, "_factory", lambda: lambda: session)
    monkeypatch.setattr(admission, "_lock_run_configurations", AsyncMock())
    monkeypatch.setattr(admission, "_bind_user_assets", AsyncMock())
    monkeypatch.setattr(admission, "append_event", AsyncMock())
    monkeypatch.setattr(admission, "wake_run", AsyncMock())
    monkeypatch.setattr(mcp_adapter, "selected_lumen_snapshot", AsyncMock(return_value=None))
    monkeypatch.setattr(admission.worker_routing, "resolve_worker_route", AsyncMock(return_value=SimpleNamespace(
        workload_class="chat", worker_pool_id="synthetic-pool")))
    response = await native_routes.client.post("/v1/temp-completions",
        headers={**_headers(OWNER_TOKEN, PROJECT_ID), "Idempotency-Key": str(uuid4())},
        json={"model_id": "synthetic-text", "parts": [{"type": "text", "text": "synthetic request"}],
              "features": {"memory": False, "tool_policy": {"mode": "none"}}})
    assert response.status_code == 202, response.text
    run, = [row for row in session.added if isinstance(row, ChatRun)]
    assert response.json()["run_id"] == run.id
    return run, session


@pytest.mark.parametrize("metadata", [_RESTRICTED_ORIGINALS[index] for index in (0, 1, 3, 5)])
@pytest.mark.parametrize(("surface", "leaf"), [
    ("generation", "lumen-chat_user"), ("tool", "lumen-tools_user"),
    ("auxiliary", "lumen-chat_user"),
])
@pytest.mark.parametrize("revocation", ["grant", "inference"])
async def test_native_http_reduced_admin_ceiling_persists_and_fences_fresh_io(
    native_directory, native_routes, monkeypatch, metadata, surface, leaf, revocation,
):
    from lumen.services import inference_authority
    from lumen.services.durable_runs import execution
    from lumen.services.durable_runs.errors import DurableRunInputError

    names = ("member", "lumen-chat_user", *({"tool": ("lumen-tools_user",),
        "auxiliary": ("lumen-history_reader",)}.get(surface, ())))
    native_directory.configure({"system_admin_mode": "group",
        "system_group_members": [DIRECTORY_ID, PROJECTLESS_ADMIN_ID, OWNER_ID],
        "owner_roles": ["member", "lumen_user", "lumen_reader"],
        "token_overrides": {OWNER_TOKEN: {**metadata, "roles": _issued_roles(*names)}}})
    assert auth._is_system_admin(OWNER_ID) is True
    run, session = await _admit_native_ceiling_run(native_routes, monkeypatch)
    # Round-trip the JSON-column snapshot, not a live auth dependency or bearer.
    run.capability_snapshot = json.loads(json.dumps(run.capability_snapshot))
    ceiling = run.capability_snapshot["credential_authority"]
    assert ceiling == {"credential_restricted": True, "user_id": OWNER_ID,
        "source_project_id": PROJECT_ID, "source_system_scope": "",
        "issued_role_ids": sorted(role_id(name) for name in names)}
    assert OWNER_TOKEN not in json.dumps(run.capability_snapshot)
    assert "token" not in ceiling and "roles" not in ceiling and "is_system_admin" not in ceiling
    with pytest.raises(api_key_store.ApiKeyForbidden):
        await api_key_store.authorize_api_key_in_transaction(session, api_key_id=None,
            user_id=OWNER_ID, project_id=PROJECT_ID, required_scopes=("native:images:write",),
            credential_authority=ceiling)
    session.execute = AsyncMock(return_value=SimpleNamespace(scalar_one=lambda: run))
    monkeypatch.setattr(execution, "_factory", lambda: lambda: session)
    monkeypatch.setattr(execution, "_require_owned_running_lease", lambda *_args: None)
    monkeypatch.setattr(execution._DurableExecutionHooks, "_ensure_provider_turn", AsyncMock())
    segment = SimpleNamespace(status="prepared")
    monkeypatch.setattr(execution, "prepare_segment", AsyncMock(return_value=segment))
    started = []
    monkeypatch.setattr(execution, "begin_segment_io", lambda value: started.append(value))
    provider = AsyncMock()
    hooks = execution._DurableExecutionHooks(run_id=run.id, owner="synthetic-worker")

    async def start_io():
        if surface == "auxiliary":
            await inference_authority.authorize_run_generation(session, run)
        else:
            await hooks._start(segment_id="synthetic:1", ordinal=1,
                endpoint="tool" if surface == "tool" else "chat_completions", turn_ordinal=1)
        await provider()

    await start_io()
    provider.assert_awaited_once_with()
    assert len(started) == (0 if surface == "auxiliary" else 1)
    provider.reset_mock()
    started.clear()
    session.execute.reset_mock()
    native_directory.configure({"owner_roles": ["member"]} if revocation == "grant" else {
        "remove_edges": [{"prior": "lumen_user", "implied": leaf}]})
    # Unrestricted owner re-resolution still succeeds: only the original ceiling
    # plus fresh project grants can deny this credential while admin is retained.
    assert auth._is_system_admin(OWNER_ID) is True
    assert (await auth.resolve_project_authority(OWNER_ID, PROJECT_ID))["is_system_admin"] is True
    error = api_key_store.ApiKeyForbidden if surface == "auxiliary" else DurableRunInputError
    with pytest.raises(error):
        await start_io()
    provider.assert_not_awaited()
    assert started == [] and segment.status == "prepared"
    if surface == "auxiliary":
        session.execute.assert_not_awaited()
    assert _caller_exchanges(native_directory) == []


@pytest.mark.parametrize("metadata", [_RESTRICTED_ORIGINALS[index] for index in (0, 1, 3, 5)])
async def test_native_http_restricted_mcp_oauth_start_cannot_create_owner_only_callback(
    native_directory, native_routes, monkeypatch, metadata,
):
    from contextlib import asynccontextmanager

    from lumen.services import mcp_oauth

    native_directory.configure({"system_admin_mode": "group",
        "system_group_members": [DIRECTORY_ID, PROJECTLESS_ADMIN_ID, OWNER_ID],
        "token_overrides": {OWNER_TOKEN: {**metadata,
            "roles": _issued_roles("member", "lumen-mcp_editor")}}})
    assert auth._is_system_admin(OWNER_ID) is True
    begin = AsyncMock(return_value={"authorization_url": "https://synthetic.example/authorize?state=human-state"})
    monkeypatch.setattr(mcp_oauth, "begin", begin)
    monkeypatch.setattr(mcp_oauth, "callback_cookie_secure", lambda: False)
    headers = _headers(OWNER_TOKEN, PROJECT_ID)
    response = await native_routes.client.post("/v1/mcp-servers/7/oauth/start", headers=headers)
    assert response.status_code == 403, response.text
    assert "unrestricted human" in response.json()["detail"]
    assert "set-cookie" not in response.headers
    begin.assert_not_awaited()

    native_directory.configure({"remove_edges": [{"prior": "lumen_editor", "implied": "lumen-mcp_editor"}]})
    assert auth._is_system_admin(OWNER_ID) is True
    assert (await auth.resolve_project_authority(OWNER_ID, PROJECT_ID))["is_system_admin"] is True
    response = await native_routes.client.post("/v1/mcp-servers/7/oauth/start", headers=headers)
    assert response.status_code == 403, response.text
    assert "set-cookie" not in response.headers
    begin.assert_not_awaited()

    # A rejected start creates no state. The real callback-state gate must not
    # borrow the still-admin owner's authority or reach code exchange afterward.
    directory = AsyncMock(side_effect=AssertionError("callback borrowed unrestricted owner authority"))
    monkeypatch.setattr(mcp_oauth, "resolve_project_authority", directory)
    missing = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(
        scalars=lambda: SimpleNamespace(first=lambda: None))))

    @asynccontextmanager
    async def no_pending_state():
        yield missing

    monkeypatch.setattr(mcp_oauth, "_session", no_pending_state)
    with pytest.raises(mcp_oauth.McpOAuthError, match="expired or was already used"):
        await mcp_oauth.SqlMcpConnectionStore().consume_state("no-state-from-rejected-delegated-start")
    directory.assert_not_awaited()
    begin.assert_not_awaited()

    # Unrestricted humans retain interactive OAuth approval without target roles,
    # even when the selected project's mcp-editor implication was removed.
    response = await native_routes.client.post("/v1/mcp-servers/7/oauth/start",
        headers=_headers(DIRECTORY_TOKEN, None, target_project_id=PROJECT_ID))
    assert response.status_code == 200, response.text
    assert mcp_oauth.INITIATOR_COOKIE in response.headers["set-cookie"]
    begin.assert_awaited_once()
    assert begin.await_args.kwargs["user_id"] == DIRECTORY_ID
    assert begin.await_args.kwargs["project_id"] == PROJECT_ID


async def test_native_http_reduced_grade_alone_can_issue_its_local_actions_without_baseline(
    native_directory, native_routes, native_key_storage,
):
    native_directory.configure({"owner_roles": ["lumen_editor"], "remove_edges": [
        {"prior": "lumen_editor", "implied": "lumen_user"},
        *({"prior": "lumen_editor", "implied": name} for name in (
            "lumen-assets_editor", "lumen-agents_editor", "lumen-mcp_editor", "lumen-keys_editor", "lumen-history_editor")),
    ], "token_overrides": {OWNER_TOKEN: {
        "application_credential": {"id": "synthetic-appcred"}, "roles": _issued_roles("lumen_editor"),
    }}})
    authority = auth._resolve_project_authority(OWNER_ID, PROJECT_ID, issued_role_ids=(role_id("lumen_editor"),))
    assert authority == {"roles": ["lumen_editor"], "is_system_admin": False}
    response = await native_routes.client.post("/v1/api-keys", headers=_headers(OWNER_TOKEN, PROJECT_ID),
        json={"name": "grade-only-issued-ceiling", "scopes": ["compat:completions:write"]})
    assert response.status_code == 201, response.text
    assert response.json()["scopes"] == ["compat:completions:write"]
    assert len(native_key_storage.rows) == 1
    assert _caller_exchanges(native_directory) == []


def _configure_personal_directory(directory):
    directory.configure({"owner_roles": ["member"], "project_domain_id": "personal",
        "domains": {"default": {"id": "default", "name": "Accounts", "enabled": True},
                    "personal": {"id": "personal", "name": "Personal", "enabled": True}},
        "project_overrides": {"afterglow_personal_user_id": OWNER_ID}})


async def test_native_personal_member_has_all_project_actions_without_native_service_roles(native_directory, native_routes):
    from lumen.policy import authorize
    from lumen.service_authority import SCOPE_CAPABILITIES

    native_directory.configure({"owner_roles": ["member"]})
    ordinary = await auth.resolve_project_authority(OWNER_ID, PROJECT_ID)
    assert not capabilities({"user_id": OWNER_ID, "project_id": PROJECT_ID, **ordinary})
    _configure_personal_directory(native_directory)
    current = await auth.resolve_project_authority(OWNER_ID, PROJECT_ID)
    assert current["personal_project_owner"] is True
    assert set(current["roles"]) <= {"member", "reader"}
    principal = {"user_id": OWNER_ID, "project_id": PROJECT_ID, **current}
    assert capabilities(principal) == SERVICE_CAPABILITIES
    assert all(authorize(action, principal) for action in SCOPE_CAPABILITIES)
    assert not authorize("system:admin", principal)
    response = await native_routes.client.get("/native/principal", headers=_headers(OWNER_TOKEN, PROJECT_ID))
    assert response.status_code == 200 and response.json()["personal_project_owner"] is True
    assert (await native_routes.client.get("/v1/admin/providers", headers=_headers(OWNER_TOKEN, PROJECT_ID))).status_code == 403


@pytest.mark.parametrize("change", ["missing_marker", "foreign_marker", "malformed_marker", "wrong_domain", "conflicting_kind",
    "disabled_home", "disabled_domain", "disabled_user", "disabled_project", "member_removed", "foreign_raw", "group_raw", "inherited_raw", "group_effective"])
async def test_native_personal_provenance_never_borrows_member_or_foreign_authority(native_directory, change):
    _configure_personal_directory(native_directory)
    patch = {}
    if change in {"missing_marker", "foreign_marker", "malformed_marker"}:
        patch["project_overrides"] = {"afterglow_personal_user_id": {"missing_marker": None, "foreign_marker": DIRECTORY_ID, "malformed_marker": [OWNER_ID]}[change]}
    elif change in {"wrong_domain", "conflicting_kind", "disabled_home", "disabled_domain"}:
        domains = {"default": {"id": "default", "name": "Accounts", "enabled": change != "disabled_home"},
                   "personal": {"id": "personal", "name": "Other" if change == "wrong_domain" else "Personal", "enabled": change != "disabled_domain"}}
        if change == "conflicting_kind":
            domains["personal"]["afterglow_domain_kind"] = "organization"
        patch["domains"] = domains
    elif change in {"disabled_user", "disabled_project"}:
        patch["owner_enabled" if change == "disabled_user" else "project_enabled"] = False
    elif change == "member_removed":
        patch["owner_roles"] = []
    else:
        row = {"user": {"id": OWNER_ID}, "role": {"id": role_id("member")}, "scope": {"project": {"id": PROJECT_ID}}}
        if change == "foreign_raw":
            row["user"]["id"] = DIRECTORY_ID
        elif change in {"group_raw", "group_effective"}:
            row["links"] = {"membership": "https://identity/groups/foreign/users/owner"}
        else:
            row["scope"]["OS-INHERIT:inherited_to"] = "projects"
        query = {"effective": "True"} if change == "group_effective" else {"scope.project.id": PROJECT_ID}
        patch["collection_overrides"] = [{"path": "/v3/role_assignments", "query": query,
            "payload": {"role_assignments": [row], "links": {"next": None}}}]
    native_directory.configure(patch)
    try:
        current = await auth.resolve_project_authority(OWNER_ID, PROJECT_ID)
    except HTTPException as error:
        assert error.status_code == 403
    else:
        assert not current.get("personal_project_owner")
        assert not capabilities({"user_id": OWNER_ID, "project_id": PROJECT_ID, **current})


async def test_native_personal_key_and_worker_recheck_owner_marker_and_reduced_ceiling(native_directory):
    _configure_personal_directory(native_directory)
    scope = "native:images:write"
    row, _ = await api_key_store.prepare_key_record(OWNER_ID, PROJECT_ID, "personal bounded", [scope])
    assert row.scopes == [scope]
    reduced = {"credential_restricted": True, "user_id": OWNER_ID, "source_project_id": PROJECT_ID,
        "source_system_scope": "", "issued_role_ids": [role_id("member")]}
    with pytest.raises(api_key_store.ApiKeyForbidden):
        await api_key_store.prepare_key_record(OWNER_ID, PROJECT_ID, "reduced", [scope], credential_authority=reduced)
    await api_key_store.authorize_api_key_in_transaction(None, api_key_id=None, user_id=OWNER_ID,
        project_id=PROJECT_ID, required_scopes=(scope,))
    native_directory.configure({"project_overrides": {"afterglow_personal_user_id": DIRECTORY_ID}})
    with pytest.raises(api_key_store.ApiKeyForbidden):
        await api_key_store.authorize_api_key_in_transaction(None, api_key_id=None, user_id=OWNER_ID,
            project_id=PROJECT_ID, required_scopes=(scope,))


async def test_native_personal_fresh_refresh_does_not_retain_owner_flag_or_use_logical_target(native_directory):
    _configure_personal_directory(native_directory)
    principal = auth._keystone_principal(auth.validate_token(OWNER_TOKEN), OWNER_TOKEN)
    await auth._refresh_token_authority(principal)
    assert principal["personal_project_owner"] is True
    principal["source_project_id"] = "another-original-project"
    await auth._refresh_token_authority(principal)
    assert not principal.get("personal_project_owner")
    principal["source_project_id"] = PROJECT_ID
    native_directory.configure({"project_overrides": {}})
    await auth._refresh_token_authority(principal)
    assert not principal.get("personal_project_owner")


async def test_native_personal_http_issuance_needs_fresh_unrestricted_owner(native_directory, native_routes, native_key_storage):
    _configure_personal_directory(native_directory)
    response = await native_routes.client.post("/v1/api-keys", headers=_headers(OWNER_TOKEN, PROJECT_ID),
        json={"name": "personal native", "scopes": ["native:images:write"]})
    assert response.status_code == 201, response.text
    assert response.json()["scopes"] == ["native:images:write"]
    assert len(native_key_storage.rows) == 1
    native_directory.configure({"token_overrides": {OWNER_TOKEN: {
        "application_credential": {"id": "reduced"}, "roles": _issued_roles("member"),
    }}})
    response = await native_routes.client.post("/v1/api-keys", headers=_headers(OWNER_TOKEN, PROJECT_ID),
        json={"name": "cannot borrow personal", "scopes": ["native:images:write"]})
    assert response.status_code == 403 and len(native_key_storage.rows) == 1


@pytest.mark.parametrize("change", ["malformed_home", "missing_home", "escalating_member"])
async def test_native_personal_incomplete_home_or_escalating_member_never_establishes_owner(native_directory, change):
    _configure_personal_directory(native_directory)
    if change == "malformed_home":
        native_directory.configure({"user_overrides": {"domain_id": []}})
    elif change == "missing_home":
        native_directory.configure({"user_domain_id": "missing-home"})
    else:
        native_directory.state.graph[role_id("member")].add(role_id("admin"))
    try:
        current = await auth.resolve_project_authority(OWNER_ID, PROJECT_ID)
    except HTTPException as error:
        assert error.status_code in {403, 503}
    else:
        assert not current.get("personal_project_owner")


@pytest.mark.parametrize("kind", [None, False, "organization", ["personal"]])
async def test_native_personal_explicit_invalid_domain_kind_is_not_absence(native_directory, kind):
    _configure_personal_directory(native_directory)
    native_directory.state.domains["personal"]["afterglow_domain_kind"] = kind
    current = await auth.resolve_project_authority(OWNER_ID, PROJECT_ID)
    assert not current.get("personal_project_owner")


@pytest.mark.parametrize("admin", [False, True], ids=["native-read", "global-admin"])
async def test_direct_system_grant_authorizes_http_without_promoting_project_admin(native_directory, admin):
    app = FastAPI()
    dependency = auth.require_admin if admin else auth.require_scopes("native:conversations:read")

    @app.get("/authority")
    async def authority(principal=Depends(dependency)):
        return {"system_admin": principal["is_system_admin"], "project_id": principal["project_id"]}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        allowed = await client.get("/authority", headers={"X-Auth-Token": DIRECTORY_TOKEN,
            "X-Project-Id": DIRECTORY_PROJECT_ID})
        assert allowed.status_code == 200, allowed.text
        assert allowed.json() == {"system_admin": True, "project_id": DIRECTORY_PROJECT_ID}
        native_directory.configure({"owner_roles": ["admin", "member"]})
        project_response = await client.get("/authority", headers={"X-Auth-Token": OWNER_TOKEN,
            "X-Project-Id": PROJECT_ID})
        if admin:
            assert project_response.status_code == 403
        else:
            assert project_response.status_code == 200, project_response.text
            assert project_response.json() == {"system_admin": False, "project_id": PROJECT_ID}
