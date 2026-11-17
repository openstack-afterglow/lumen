"""Domain manager authority through the actual installed Keystone SDK against the isolated directory fixture."""
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
    PROJECT_ID,
    KeystoneServer,
)

from lumen import auth
from lumen.services.mcp_domain_authority import resolve_domain_authority


@pytest.fixture
def directory(monkeypatch):
    server = KeystoneServer(("127.0.0.1", 0))
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    settings = SimpleNamespace(
        keystone_auth_url=url + "/v3", keystone_admin_username=DIRECTORY_ID,
        keystone_admin_password=DIRECTORY_PASSWORD, keystone_admin_project=DIRECTORY_PROJECT_ID,
        keystone_domain="Default", keystone_region_name="RegionOne", keystone_interface="public", verify=True,
    )
    monkeypatch.setattr(auth, "get_settings", lambda: settings)
    with httpx.Client(base_url=url, trust_env=False) as client:
        def configure(payload):
            response = client.post("/_control/configure", json=payload, headers={"X-Test-Control-Token": CONTROL_TOKEN})
            assert response.status_code == 200, response.text
        yield configure
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


async def test_project_admin_and_member_do_not_manage_but_default_manager_does(directory):
    directory({"owner_roles": ["admin", "member"]})
    admin = await resolve_domain_authority(OWNER_ID, PROJECT_ID)
    assert admin == {"domain_id": "default", "is_member": True, "can_manage": False}

    directory({"owner_roles": ["member"], "default_managers": [OWNER_ID]})
    manager = await resolve_domain_authority(OWNER_ID, PROJECT_ID)
    assert manager == {"domain_id": "default", "is_member": True, "can_manage": True}

    directory({"default_managers": []})
    revoked = await resolve_domain_authority(OWNER_ID, PROJECT_ID)
    assert revoked["can_manage"] is False


async def test_foreign_domain_is_rejected_for_a_manager(directory):
    directory({"owner_roles": ["member"], "default_managers": [OWNER_ID],
               "domains": {"default": {"id": "default", "name": "Default", "enabled": True},
                           "other": {"id": "other", "name": "Other", "enabled": True}}})
    with pytest.raises(HTTPException) as denied:
        await resolve_domain_authority(OWNER_ID, PROJECT_ID, "other")
    assert denied.value.status_code == 403
