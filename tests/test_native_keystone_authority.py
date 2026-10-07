"""Actual installed Keystone SDK HTTP against the isolated directory fixture."""
import threading
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from system.fake_keystone import (
    CONTROL_TOKEN,
    DIRECTORY_ID,
    DIRECTORY_PASSWORD,
    DIRECTORY_PROJECT_ID,
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
        keystone_domain="Default", verify=True)
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


@pytest.mark.parametrize("key", [False, True])
async def test_native_sdk_live_owner_fences_new_io_for_web_and_existing_key(native_directory, key):
    row, _raw = await api_key_store.prepare_key_record(OWNER_ID, PROJECT_ID, "HTTP owner", ["compat:completions:write"])
    row.id = 7
    class Session:
        async def execute(self, statement):
            return SimpleNamespace(scalar_one_or_none=lambda: row)
    kwargs = dict(api_key_id=7 if key else None, user_id=OWNER_ID, project_id=PROJECT_ID,
                  required_scopes=("compat:completions:write",))
    await api_key_store.authorize_api_key_in_transaction(Session(), **kwargs)
    # Issuer key-management authority need not be retained by an existing key.
    native_directory.configure({"remove_edges": [{"prior": "lumen_editor", "implied": "lumen-keys_editor"}]})
    await api_key_store.authorize_api_key_in_transaction(Session(), **kwargs)
    with pytest.raises(api_key_store.ApiKeyForbidden):
        await api_key_store.prepare_key_record(OWNER_ID, PROJECT_ID, "cannot reissue", ["compat:completions:write"])
    native_directory.configure({"remove_edges": [{"prior": "lumen_user", "implied": "lumen-chat_user"}]})
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
