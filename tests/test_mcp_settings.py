"""Domain settings authority is independent of project and credential labels."""

from unittest.mock import AsyncMock

import pytest

from lumen.services import extensions_store as es
from lumen.services import mcp_settings


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
    lookup.assert_awaited_once_with("user", "project", "a", credential_authority=None)


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
