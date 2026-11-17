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
    role_id,
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
    admin = await resolve_domain_authority(OWNER_ID, PROJECT_ID, unrestricted_human=True)
    assert admin == {"domain_id": "default", "is_member": True, "can_manage": False}

    directory({"owner_roles": ["member"], "default_managers": [OWNER_ID]})
    manager = await resolve_domain_authority(OWNER_ID, PROJECT_ID, unrestricted_human=True)
    assert manager == {"domain_id": "default", "is_member": True, "can_manage": True}

    directory({"default_managers": []})
    revoked = await resolve_domain_authority(OWNER_ID, PROJECT_ID, unrestricted_human=True)
    assert revoked["can_manage"] is False


async def test_foreign_domain_is_rejected_for_a_manager(directory):
    directory({"owner_roles": ["member"], "default_managers": [OWNER_ID],
               "domains": {"default": {"id": "default", "name": "Default", "enabled": True},
                           "other": {"id": "other", "name": "Other", "enabled": True}}})
    with pytest.raises(HTTPException) as denied:
        await resolve_domain_authority(OWNER_ID, PROJECT_ID, "other", unrestricted_human=True)
    assert denied.value.status_code == 403


_OTHER_DOMAINS = {
    "default": {"id": "default", "name": "Default", "enabled": True},
    "other": {"id": "other", "name": "Other", "enabled": True},
}
_RESTRICTED = {
    "credential_restricted": True, "user_id": OWNER_ID, "source_project_id": PROJECT_ID,
    "source_system_scope": "", "issued_role_ids": [role_id("member")],
}


async def test_only_an_unrestricted_human_system_admin_reaches_a_foreign_domain(directory):
    directory({"owner_roles": ["member"], "directory_system_admin": True, "system_admin_mode": "group",
               "system_group_members": [OWNER_ID], "domains": _OTHER_DOMAINS})
    human = await resolve_domain_authority(OWNER_ID, PROJECT_ID, "other", unrestricted_human=True)
    assert human == {"domain_id": "other", "is_member": True, "can_manage": True}
    # The same administrator's API key (no restricted marker) or reduced credential is not human/unrestricted.
    for kwargs in ({"unrestricted_human": False}, {"unrestricted_human": True, "credential_authority": _RESTRICTED},
                   {"unrestricted_human": False, "credential_authority": _RESTRICTED}):
        with pytest.raises(HTTPException) as denied:
            await resolve_domain_authority(OWNER_ID, PROJECT_ID, "other", **kwargs)
        assert denied.value.status_code == 403
    own = await resolve_domain_authority(OWNER_ID, PROJECT_ID, unrestricted_human=False)
    assert own["domain_id"] == "default" and own["can_manage"] is False


async def test_api_key_principal_gets_membership_only_even_for_a_manager_or_admin(directory):
    from lumen.services import extensions_store as es
    from lumen.services import mcp_settings

    directory({"owner_roles": ["member"], "default_managers": [OWNER_ID], "directory_system_admin": True,
               "system_admin_mode": "group", "system_group_members": [OWNER_ID], "domains": _OTHER_DOMAINS})
    api_key = {"user_id": OWNER_ID, "project_id": PROJECT_ID, "auth_type": "api_key"}
    session = {**api_key, "auth_type": "keystone"}
    own = await mcp_settings.authority(api_key, hint=True)
    assert own["is_member"] is True and own["can_manage"] is False
    with pytest.raises(HTTPException):
        await mcp_settings.authority(api_key, "other")
    with pytest.raises(es.ExtensionForbidden):
        await mcp_settings.authority(api_key, "default", manage=True)
    assert (await mcp_settings.authority(session, "other", manage=True))["can_manage"] is True
    # The store (legacy activation/OAuth paths) defaults to a non-human actor.
    with pytest.raises(HTTPException):
        await es._domain_access(OWNER_ID, PROJECT_ID, "other")
    with pytest.raises(es.ExtensionForbidden):
        await es._domain_access(OWNER_ID, PROJECT_ID, "default", manage=True)
    with es.actor_scope(human=True):
        assert (await es._domain_access(OWNER_ID, PROJECT_ID, "other"))["domain_id"] == "other"


async def test_member_membership_survives_broken_default_manager_metadata(directory):
    two_defaults = [{"id": name, "name": "Default", "domain_id": "default", "enabled": True} for name in ("p1", "p2")]
    directory({"owner_roles": ["member"], "collection_overrides": [{
        "path": "/v3/projects", "query": {"domain_id": "default"},
        "payload": {"projects": two_defaults, "links": {"next": None, "previous": None}},
    }]})
    member = await resolve_domain_authority(OWNER_ID, PROJECT_ID, compute_manage=False, unrestricted_human=True)
    assert member == {"domain_id": "default", "is_member": True, "can_manage": False}
    with pytest.raises(HTTPException) as broken:
        await resolve_domain_authority(OWNER_ID, PROJECT_ID, compute_manage=True, unrestricted_human=True)
    assert broken.value.status_code == 503



async def test_fake_keystone_serves_direct_system_role_lists(directory, monkeypatch):
    """``ks.roles.list(user|group=..., system='all')`` reaches /v3/system/{users,groups}/{id}/roles."""
    from keystoneclient.exceptions import NotFound

    directory({"directory_system_admin": True, "system_admin_mode": "group", "system_group_members": [OWNER_ID]})
    ks = auth._get_admin_ks_client()
    group = ks.roles.list(group=auth_group_id(), system="all")
    assert [role.id for role in group] == [role_id("admin")]
    assert ks.roles.list(user=OWNER_ID, system="all") == []
    with pytest.raises(NotFound):
        ks.roles.list(user="synthetic-unknown-user", system="all")
    with pytest.raises(NotFound):
        ks.roles.list(group="synthetic-unknown-group", system="all")
    directory({"system_admin_mode": "direct"})
    assert [role.id for role in ks.roles.list(user=DIRECTORY_ID, system="all")] == [role_id("admin")]


def auth_group_id():
    from system.fake_keystone import SYSTEM_GROUP_ID

    return SYSTEM_GROUP_ID
