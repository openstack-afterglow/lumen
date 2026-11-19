"""Domain settings authority is independent of project and credential labels."""

from unittest.mock import AsyncMock

import pytest

from lumen.services import extensions_store as es
from lumen.services import mcp_settings
from lumen.services.mcp_registry_model import McpRegistryError


def principal(**changes):
    return {"user_id": "user", "project_id": "project", "auth_type": "keystone", **changes}


@pytest.mark.parametrize(("member", "manager", "manage", "allowed"), [
    (False, False, False, False), (False, True, False, False),
    (True, False, False, True), (True, False, True, False),
    (True, True, False, True), (True, True, True, True),
])
async def test_exact_domain_authority_matrix(monkeypatch, member, manager, manage, allowed):
    lookup = AsyncMock(return_value={"domain_id": "a", "is_member": member, "can_manage": manager})
    monkeypatch.setattr(mcp_settings, "resolve_domain_authority", lookup)
    if allowed:
        assert (await mcp_settings.authority(principal(), "a", manage=manage))["domain_id"] == "a"
    else:
        with pytest.raises(es.ExtensionForbidden):
            await mcp_settings.authority(principal(), "a", manage=manage)
    lookup.assert_awaited_once_with(
        "user", "project", "a", credential_authority=None, compute_manage=manage, unrestricted_human=True,
    )


@pytest.mark.parametrize("kind", ["api_key", "restricted"])
async def test_only_unrestricted_human_can_manage_even_if_resolver_claims_manager(monkeypatch, kind):
    lookup = AsyncMock(return_value={"domain_id": "a", "is_member": True, "can_manage": True})
    monkeypatch.setattr(mcp_settings, "resolve_domain_authority", lookup)
    actor = principal(auth_type="api_key") if kind == "api_key" else principal(
        credential_restricted=True, source_project_id="project", source_system_scope="", issued_role_ids=("r",),
    )
    with pytest.raises(es.ExtensionForbidden):
        await mcp_settings.authority(actor, "a", manage=True)
    assert (await mcp_settings.authority(actor, "a"))["is_member"] is True
    if kind == "restricted":
        assert lookup.await_args.kwargs["credential_authority"] == {
            "credential_restricted": True, "user_id": "user", "source_project_id": "project",
            "source_system_scope": "", "issued_role_ids": ["r"],
        }


def test_credential_header_names_are_bound_to_the_catalog_declaration():
    entry = {"header_hints": [{"name": "X-Api-Key", "secret": True}, {"description": "unnamed", "secret": True}]}
    mcp_settings._check_header_names(entry, {"x-api-key": "value"})
    with pytest.raises(McpRegistryError):
        mcp_settings._check_header_names(entry, {"Authorization": "Bearer value"})
    mcp_settings._check_header_names({"header_hints": [{"description": "unnamed", "secret": True}]}, {"Anything": "v"})


async def test_settings_view_survives_a_domain_authority_outage(monkeypatch):
    from fastapi import HTTPException

    lookup = AsyncMock(side_effect=HTTPException(503, "Current Keystone domain authority unavailable"))
    monkeypatch.setattr(mcp_settings, "resolve_domain_authority", lookup)
    monkeypatch.setattr(es, "list_for_user", AsyncMock(return_value=[]))
    monkeypatch.setattr(mcp_settings, "managed_profiles", lambda: {})

    result = await mcp_settings.view(principal(), limit=1)

    assert result["organization_available"] is False
    assert result["domain_id"] is None and result["can_manage_domain"] is False
    assert len(result["entries"]) == 1 and result["total"] > 1


class _Session:
    def __init__(self, row):
        self.row = row

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, model, item_id):
        return self.row


async def test_oauth_precheck_fails_closed_when_storage_is_unavailable(monkeypatch):
    def unavailable():
        raise es.ChatStorageUnavailable("breaker open")

    monkeypatch.setattr(es, "_require_db", unavailable)
    with pytest.raises(es.ChatStorageUnavailable):
        await mcp_settings.check_oauth_management(principal(), 7)


@pytest.mark.parametrize("actor", [
    principal(auth_type="api_key"),
    principal(credential_restricted=True, source_project_id="project", source_system_scope="", issued_role_ids=("r",)),
])
async def test_api_key_or_reduced_manager_cannot_pass_the_oauth_precheck(monkeypatch, actor):
    from types import SimpleNamespace

    row = SimpleNamespace(scope="domain", owner_domain_id="a")
    monkeypatch.setattr(es, "_require_db", lambda: (lambda: _Session(row)))
    lookup = AsyncMock(return_value={"domain_id": "a", "is_member": True, "can_manage": True})
    monkeypatch.setattr(mcp_settings, "resolve_domain_authority", lookup)
    with pytest.raises(es.ExtensionForbidden):
        await mcp_settings.check_oauth_management(actor, 7)
    # An unrestricted human manager passes the same check.
    await mcp_settings.check_oauth_management(principal(), 7)


async def test_store_domain_access_requires_a_human_actor_to_manage(monkeypatch):
    lookup = AsyncMock(return_value={"domain_id": "a", "is_member": True, "can_manage": True})
    monkeypatch.setattr("lumen.services.mcp_domain_authority.resolve_domain_authority", lookup)
    with pytest.raises(es.ExtensionForbidden):
        await es._domain_access("u", "p", "a", manage=True)
    lookup.assert_not_awaited()
    await es._domain_access("u", "p", "a")
    assert lookup.await_args.kwargs["unrestricted_human"] is False
    with es.actor_scope(human=True):
        await es._domain_access("u", "p", "a", manage=True)
    assert lookup.await_args.kwargs["unrestricted_human"] is True


async def test_unactivated_or_disabled_rows_are_not_connected_states(monkeypatch):
    base = {"auth_mode": "none", "template_key": "t1", "auth_connected": True}
    rows = [
        {**base, "id": 1, "scope": "domain", "is_active": False},
        {**base, "id": 2, "scope": "user", "is_active": False},
        {**base, "id": 3, "scope": "domain", "is_active": True},
    ]
    monkeypatch.setattr(mcp_settings, "resolve_domain_authority", AsyncMock(
        return_value={"domain_id": "a", "is_member": True, "can_manage": False}))
    monkeypatch.setattr(es, "list_for_user", AsyncMock(return_value=rows))
    monkeypatch.setattr(mcp_settings, "managed_profiles", lambda: {})
    monkeypatch.setattr(mcp_settings.catalog, "entries", lambda: [{"id": "t1", "name": "T", "access": "open"}])
    entry = (await mcp_settings.view(principal()))["entries"][0]
    assert [row["state"] for row in entry["connections"]] == ["not_connected", "not_connected", "org_connected"]
