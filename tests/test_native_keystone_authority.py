"""Actual installed Keystone SDK HTTP against the isolated directory fixture."""
import threading
from types import SimpleNamespace

import httpx
import pytest
from fastapi import Depends, FastAPI, HTTPException
from system.fake_keystone import (
    CONTROL_TOKEN,
    DIRECTORY_ID,
    DIRECTORY_PASSWORD,
    DIRECTORY_PROJECT_ID,
    DIRECTORY_TOKEN,
    OWNER_ID,
    OWNER_TOKEN,
    PROJECT_ID,
    KeystoneServer,
)

from lumen import auth
from lumen.service_authority import SERVICE_CAPABILITIES, capabilities
from lumen.services import api_key_store


@pytest.fixture
def native_directory(monkeypatch):
    server = KeystoneServer(("127.0.0.1", 0))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
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
        yield SimpleNamespace(client=client, configure=configure)
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


async def test_native_sdk_current_graph_does_not_reexpand_retained_parent(native_directory):
    before = await auth.resolve_project_authority(OWNER_ID, PROJECT_ID)
    assert capabilities(before["roles"]) == SERVICE_CAPABILITIES
    assert before["is_system_admin"] is False
    native_directory.configure({"remove_edges": [{"prior": "lumen_user", "implied": "lumen-chat_user"}]})
    after = await auth.resolve_project_authority(OWNER_ID, PROJECT_ID)
    assert "lumen_admin" in after["roles"] and "lumen_user" in after["roles"]
    assert "lumen-chat_user" not in capabilities(after["roles"])
    assert "lumen-images_user" in capabilities(after["roles"])
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
    native_directory.configure({"remove_edges": [{"prior": "lumen_editor", "implied": "lumen-keys_editor"}]})
    await api_key_store.authorize_api_key_in_transaction(Session(), **kwargs)
    with pytest.raises(api_key_store.ApiKeyForbidden):
        await api_key_store.prepare_key_record(OWNER_ID, PROJECT_ID, "cannot reissue", [scope])
    native_directory.configure({"remove_edges": [{"prior": "lumen_user", "implied": leaf}]})
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
    info = await auth.require_token(
        x_auth_token=DIRECTORY_TOKEN, bearer=None,
        x_project_id=DIRECTORY_PROJECT_ID, x_target_project_id=PROJECT_ID,
    )
    assert info["is_system_admin"] is True
    assert info["project_id"] == PROJECT_ID
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


async def test_native_sdk_non_admin_cannot_select_foreign_target(native_directory):
    with pytest.raises(HTTPException) as exc:
        await auth.require_token(
            x_auth_token=OWNER_TOKEN, bearer=None,
            x_project_id=PROJECT_ID, x_target_project_id=DIRECTORY_PROJECT_ID,
        )
    assert exc.value.status_code == 403


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
        denied = await client.get("/authority", headers={"X-Auth-Token": OWNER_TOKEN,
            "X-Project-Id": PROJECT_ID})
        assert denied.status_code == 403
