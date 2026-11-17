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
    lookup.assert_awaited_once_with("user", "project", "a", credential_authority=None, compute_manage=manage)


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
