"""Native action/capability policy regressions; synthetic identity, no provider I/O."""
import os
import time
from importlib.metadata import entry_points
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import Depends, FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from lumen import auth, config, policy
from lumen.api.policy import router
from lumen.service_authority import READER_CAPABILITIES, SCOPE_CAPABILITIES, capabilities
from lumen.services import api_key_store


def test_policy_generator_entrypoints_and_packaged_sample_match_registered_defaults():
    import yaml

    providers = entry_points(group="oslo.policy.policies")
    assert next(item for item in providers if item.name == "lumen").load() is policy.list_rules
    enforcers = entry_points(group="oslo.policy.enforcer")
    assert next(item for item in enforcers if item.name == "lumen").load() is policy.get_enforcer
    sample = yaml.safe_load((Path(policy.__file__).with_name("policy.yaml.sample")).read_text())
    assert sample == {rule.name: rule.check_str for rule in policy.list_rules()}


def actor(roles=(), **extra):
    return {"user_id": "owner", "project_id": "project", "roles": list(roles),
            "auth_type": "keystone", "is_system_admin": False, **extra}


@pytest.fixture
def operator_policy(tmp_path, monkeypatch):
    path = tmp_path / "policy.yaml"
    directory = tmp_path / "policy.d"
    directory.mkdir()
    monkeypatch.setattr(config, "get_settings", lambda: SimpleNamespace(
        oslo_policy_policy_file=str(path), oslo_policy_policy_dirs=[str(directory)]))
    policy._enforcer.cache_clear()
    tick = time.time() + 1

    def replace(content, destination=None):
        nonlocal tick
        target = destination or path
        target.write_text(content)
        tick += 2
        os.utime(target, (tick, tick))
        os.utime(directory, (tick, tick))
    yield replace, directory
    policy._enforcer.cache_clear()


@pytest.mark.parametrize("role", ["member", "manager", "admin", "reader"])
@pytest.mark.parametrize("action", sorted(SCOPE_CAPABILITIES))
def test_registered_rules_use_standard_project_roles(operator_policy, role, action):
    principal = actor([role])
    assert policy.authorize(action, principal) is (role != "reader" or action.endswith(":read"))
    assert principal["roles"] == [role]
    assert not policy.authorize("unregistered:action", principal)


@pytest.mark.parametrize("roles", [
    [], ["project_admin"], ["project_owner"], ["drover_admin"],
    ["lumen_reader"], ["lumen_user"], ["lumen_editor"], ["lumen_admin"],
    *[[label] for label in sorted({label for labels in SCOPE_CAPABILITIES.values() for label in labels})],
])
@pytest.mark.parametrize("action", sorted(SCOPE_CAPABILITIES))
def test_legacy_only_roles_have_no_default_project_entitlement(operator_policy, roles, action):
    assert not policy.authorize(action, actor(roles))


@pytest.mark.parametrize("roles", [[], ["reader"], ["lumen_admin"], ["lumen-chat_user"], ["custom_operator"]])
def test_operator_allow_cannot_broaden_native_project_role_ceiling(operator_policy, roles):
    replace, _ = operator_policy
    replace('"compat:completions:write": "@"\n"models:read": "@"')
    assert not policy.authorize("compat:completions:write", actor(roles))
    assert policy.authorize("models:read", actor(roles)) is (roles == ["reader"])


@pytest.mark.parametrize("content", [
    '"compat:completions:write": [',
    'false', '[]', '"compat:completions:write": null',
    'null',
    '"compat:completions:write": "rule:missing"',
    '"compat:completions:write": "rule:compat:completions:write"',
    '"compat:completions:write": "not rule:missing"',
    '"compat:completions:write": "not rule:cycle", "cycle": "not rule:compat:completions:write"',
    '"compat:completions:write": "role:member and not (rule:missing or role:reader)"',
    '"": "@"', '"compat:completions:write": false', '"compat:completions:write": []',
])
def test_malformed_override_never_uses_prior_allow(operator_policy, content):
    replace, _ = operator_policy
    principal = actor(["member"])
    assert policy.authorize("compat:completions:write", principal)
    replace(content)
    for _ in range(2):
        with pytest.raises(HTTPException) as error:
            policy.authorize("compat:completions:write", principal)
        assert error.value.status_code == 503
    replace('"compat:completions:write": "@"')
    assert policy.authorize("compat:completions:write", principal)


@pytest.mark.parametrize("expression", ["role:member or", "not bogus", "not bogus or role:member", "role:member or not bogus"])
@pytest.mark.parametrize("in_directory", [False, True])
def test_native_expression_errors_are_503_even_under_negation_and_recover(operator_policy, expression, in_directory):
    from oslo_policy import _parser

    replace, directory = operator_policy
    original = _parser.LOG
    destination = directory / "10-override.yaml" if in_directory else None
    replace(f'"compat:completions:write": "{expression}"', destination)
    with pytest.raises(HTTPException) as error:
        policy.authorize("compat:completions:write", actor(["member"]))
    assert error.value.status_code == 503
    assert _parser.LOG is original
    replace('"compat:completions:write": "@"', destination)
    assert policy.authorize("compat:completions:write", actor(["member"]))
    assert _parser.LOG is original


def test_absent_configured_policy_directory_fails_closed(operator_policy, monkeypatch, tmp_path):
    from lumen import config as config_module

    _, directory = operator_policy
    missing = tmp_path / "absent-policy.d"
    assert not missing.exists()
    base = config_module.get_settings()
    monkeypatch.setattr(config_module, "get_settings", lambda: SimpleNamespace(
        oslo_policy_policy_file=base.oslo_policy_policy_file,
        oslo_policy_policy_dirs=[*base.oslo_policy_policy_dirs, str(missing)]))
    policy._enforcer.cache_clear()
    try:
        with pytest.raises(HTTPException) as error:
            policy.authorize("models:read", actor(["member"]))
        assert error.value.status_code == 503
    finally:
        policy._enforcer.cache_clear()


async def test_yaml_reload_changes_real_route_and_current_capability_projection(operator_policy, monkeypatch):
    replace, _ = operator_policy
    principal = actor(["member"])
    monkeypatch.setattr(auth, "validate_token", Mock(return_value={**principal, "auth_token": "original"}))
    authority = AsyncMock(side_effect=lambda *_args: {
        "roles": principal["roles"], "is_system_admin": principal["is_system_admin"]})
    monkeypatch.setattr(auth, "resolve_project_authority", authority)
    application = FastAPI()
    application.include_router(router, prefix="/v1")

    @application.post("/effect")
    async def effect(current=Depends(auth.require_scopes("compat:completions:write"))):
        return {"roles": current["roles"]}

    headers = {"X-Auth-Token": "original", "X-Project-Id": "project"}
    async with AsyncClient(transport=ASGITransport(application), base_url="http://native") as client:
        assert (await client.get("/v1/policy/capabilities")).status_code == 401
        before = (await client.get("/v1/policy/capabilities", headers=headers)).json()
        assert before["service"] == "lumen"
        assert "compat:completions:write" in before["actions"]
        assert (await client.post("/effect", headers=headers)).status_code == 200
        replace('"compat:completions:write": "!"')
        assert (await client.post("/effect", headers=headers)).status_code == 403
        narrowed = (await client.get("/v1/policy/capabilities", headers=headers)).json()
        assert "compat:completions:write" not in narrowed["actions"]
        replace('"compat:completions:write": "role:member"')
        allowed = await client.post("/effect", headers=headers)
        assert allowed.status_code == 200 and allowed.json()["roles"] == ["member"]
        granted = (await client.get("/v1/policy/capabilities", headers=headers)).json()
        assert "compat:completions:write" in granted["actions"]
        assert "lumen-chat_user" in granted["capabilities"]
        assert granted["actions"] == sorted(granted["actions"])
        assert granted["capabilities"] == sorted(granted["capabilities"])
        replace('\n'.join(f'"{name}": "!"' for name in SCOPE_CAPABILITIES))
        denied = (await client.get("/v1/policy/capabilities", headers=headers)).json()
        assert denied == {"service": "lumen", "capabilities": [], "actions": []}
        assert (await client.post("/effect", headers=headers)).status_code == 403
        assert set(denied) == {"service", "capabilities", "actions"}
    assert authority.await_count >= 6


async def test_policy_directory_override_reloads_without_restarting(operator_policy):
    replace, directory = operator_policy
    principal = actor(["member"])
    assert policy.authorize("native:tools:execute", principal)
    override = directory / "10-tools.yaml"
    replace('"native:tools:execute": "!"', override)
    assert not policy.authorize("native:tools:execute", principal)
    replace('"native:tools:execute": "@"', override)
    assert policy.authorize("native:tools:execute", principal)


async def test_key_projection_and_action_share_scope_ceiling_even_with_allow_all(operator_policy):
    from lumen.api.policy import policy_capabilities

    replace, _ = operator_policy
    replace('\n'.join(f'"{name}": "@"' for name in [*SCOPE_CAPABILITIES, "system:admin"]))
    principal = actor([], auth_type="api_key", scopes=("models:read",), service_system_admin=True)
    response = await policy_capabilities(principal)
    assert response == {"service": "lumen", "capabilities": ["lumen-inventory_reader"], "actions": ["models:read"]}
    assert policy.authorize("models:read", principal)
    assert not policy.authorize("native:tools:execute", principal)
    assert not policy.authorize("system:admin", principal)
    for roles in (["lumen_admin"], ["admin"], ["manager"], ["member"], ["reader"]):
        assert not policy.authorize("system:admin", actor(roles))
    assert policy.authorize("system:admin", actor([], is_system_admin=True))
    assert not policy.authorize("system:admin", actor(["admin"], is_system_admin=True, credential_restricted=True))
    assert not policy.authorize("models:read", actor(["admin"], project_id=""))
    principal["scopes"] = ()
    assert await policy_capabilities(principal) == {"service": "lumen", "capabilities": [], "actions": []}


async def test_same_policy_revalidates_worker_and_key_issuance(operator_policy, monkeypatch):
    replace, _ = operator_policy
    lookup = AsyncMock(return_value={"roles": ["member"], "is_system_admin": False})
    monkeypatch.setattr(auth, "resolve_project_authority", lookup)
    row, _raw = await api_key_store.prepare_key_record("owner", "project", "member only", ["native:tools:execute"])
    assert row.scopes == ["native:tools:execute"]
    await api_key_store.authorize_api_key_in_transaction(None, api_key_id=None,
        user_id="owner", project_id="project", required_scopes=("native:tools:execute",))
    replace('"native:tools:execute": "!"\n"native:keys:write": "!"')
    with pytest.raises(api_key_store.ApiKeyForbidden):
        await api_key_store.authorize_api_key_in_transaction(None, api_key_id=None,
            user_id="owner", project_id="project", required_scopes=("native:tools:execute",))
    with pytest.raises(api_key_store.ApiKeyForbidden):
        await api_key_store.prepare_key_record("owner", "project", "denied", ["native:tools:execute"])
    replace('"native:tools:execute": "role:custom_operator"\n"native:keys:write": "role:custom_operator"')
    lookup.return_value = {"roles": ["member", "custom_operator"], "is_system_admin": False}
    await api_key_store.authorize_api_key_in_transaction(None, api_key_id=None,
        user_id="owner", project_id="project", required_scopes=("native:tools:execute",))
    row, _raw = await api_key_store.prepare_key_record("owner", "project", "operator restriction", ["native:tools:execute"])
    assert row.scopes == ["native:tools:execute"]
    assert {"lumen-tools_user", "lumen-keys_editor"} <= capabilities(actor(["member", "custom_operator"]))
    lookup.return_value = {"roles": ["member"], "is_system_admin": False}
    with pytest.raises(api_key_store.ApiKeyForbidden):
        await api_key_store.prepare_key_record("owner", "project", "missing operator role", ["native:tools:execute"])
    assert not policy.authorize("native:tools:execute", actor(["custom_operator"]))


async def test_allow_rule_cannot_bypass_key_foreign_target_or_owner_checks(operator_policy, monkeypatch):
    from starlette.requests import Request

    replace, _ = operator_policy
    replace('"models:read": "@"\n"system:admin": "@"')
    monkeypatch.setattr(api_key_store, "verify_key", AsyncMock(return_value={
        "user_id": "owner", "project_id": "project", "api_key_id": 7,
        "roles": ["admin"], "scopes": ("models:read",), "service_system_admin": True}))
    for header in (b"x-project-id", b"x-target-project-id"):
        request = Request({"type": "http", "headers": [(b"x-api-key", b"sk-afgl-synthetic"), (header, b"foreign")]})
        with pytest.raises(HTTPException) as error:
            await auth.get_principal(request)
        assert error.value.status_code == 403
    row = SimpleNamespace(owner_user_id="someone-else", owner_project_id="project", is_active=True,
        revoked_at=None, expires_at=None, scopes=["models:read"])
    session = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: row)))
    with pytest.raises(api_key_store.ApiKeyForbidden):
        await api_key_store._active_key_scopes(session, api_key_id=7, user_id="owner", project_id="project")


async def test_device_approval_requires_real_issuance_policy_before_state_change(operator_policy, monkeypatch):
    from lumen.api import claude_gateway

    replace, _ = operator_policy
    principal = actor(["reader"])
    application = FastAPI()
    application.include_router(claude_gateway.internal_router, prefix="/device")
    application.dependency_overrides[auth.require_token] = lambda: principal
    approval = AsyncMock(return_value={"status": "approved"})
    monkeypatch.setattr(claude_gateway.gateway, "authorize_user_code", approval)
    monkeypatch.setattr(claude_gateway.gateway, "enforce_rate_limit", AsyncMock())
    async with AsyncClient(transport=ASGITransport(application), base_url="http://native") as client:
        body = {"user_code": "ABCD-EFGH", "action": "approve"}
        assert (await client.post("/device/authorize", json=body)).status_code == 403
        approval.assert_not_awaited()
        replace('"native:keys:write": "@"\n"models:read": "@"\n"compat:completions:write": "@"')
        assert (await client.post("/device/authorize", json=body)).status_code == 403
        approval.assert_not_awaited()
        principal["roles"] = ["member"]
        assert (await client.post("/device/authorize", json=body)).status_code == 200
        approval.assert_awaited_once_with(user_code="ABCD-EFGH", approve=True,
            owner_user_id="owner", owner_project_id="project")


@pytest.mark.parametrize("names", [("member", "MEMBER"), ("custom_operator", "CUSTOM_OPERATOR")])
async def test_real_engine_casefold_matching_cannot_accept_ambiguous_native_binding(operator_policy, monkeypatch, names):
    replace, _ = operator_policy
    replace(f'"compat:completions:write": "role:{names[0]}"')
    # Demonstrate the engine's matching semantics, then exercise the native
    # binding boundary that must prevent this from becoming caller authority.
    assert policy.authorize("compat:completions:write", actor(["manager", names[1]]))
    rows = [{"id": str(index), "name": name, "domain_id": None} for index, name in enumerate(names)]
    ks = SimpleNamespace(
        users=SimpleNamespace(get=lambda value: SimpleNamespace(id=value, domain_id="domain", enabled=True)),
        projects=SimpleNamespace(get=lambda value: SimpleNamespace(id=value, domain_id="domain", enabled=True)),
        domains=SimpleNamespace(get=lambda value: SimpleNamespace(id=value, enabled=True)),
        get=lambda _path: (None, {"roles": rows, "links": {}, "truncated": False}),
    )
    monkeypatch.setattr(auth, "_get_admin_ks_client", lambda: ks)
    with pytest.raises(HTTPException) as error:
        await auth.resolve_project_authority("owner", "project")
    assert error.value.status_code == 503


async def test_key_authentication_preserves_policy_503_and_corrected_file_recovery(operator_policy, monkeypatch):
    replace, _ = operator_policy
    row = SimpleNamespace(key_hash="digest", is_active=True, revoked_at=None, expires_at=None,
        owner_user_id="owner", owner_project_id="project", id=7, scopes=["models:read"], credential_kind="api_key")

    class Session:
        commit = AsyncMock()
        rollback = AsyncMock()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, _statement):
            return SimpleNamespace(scalar_one_or_none=lambda: row)

    monkeypatch.setattr(api_key_store, "_hash_key", lambda _raw: "digest")
    monkeypatch.setattr(api_key_store, "is_db_available", lambda: True)
    monkeypatch.setattr(api_key_store, "get_session_factory", lambda: Session)
    monkeypatch.setattr(api_key_store, "_current_owner_authority", AsyncMock(return_value=actor(["member"])))
    replace('"models:read": null')
    with pytest.raises(HTTPException) as error:
        await api_key_store.verify_key("sk-afgl-synthetic")
    assert error.value.status_code == 503
    replace('"models:read": "role:member"')
    info = await api_key_store.verify_key("sk-afgl-synthetic")
    assert info["scopes"] == ("models:read",)


def test_obsolete_personal_owner_claim_cannot_influence_standard_policy(operator_policy):
    replace, _ = operator_policy
    for roles in ([], ["reader"], ["lumen_admin"]):
        assert not policy.authorize("native:images:write", actor(roles, personal_project_owner=True))
    principal = actor(["member"], personal_project_owner=True)
    assert all(policy.authorize(action, principal) for action in SCOPE_CAPABILITIES)
    assert not policy.authorize("system:admin", principal)
    assert policy.authorize("native:images:write", {**principal, "credential_restricted": True})
    key = {**principal, "auth_type": "api_key", "scopes": ("models:read",)}
    assert policy.authorize("models:read", key)
    assert not policy.authorize("native:images:write", key)
    replace('"native:images:write": "!"\n"system:admin": "@"')
    assert not policy.authorize("native:images:write", principal)
    assert not policy.authorize("system:admin", principal)
    replace('"native:images:write": "personal_project_owner:True"')
    assert not policy.authorize("native:images:write", principal)


async def test_reader_projection_keeps_read_actions_without_editor_capability_labels(operator_policy):
    from lumen.api.policy import policy_capabilities

    principal = actor(["reader"])
    response = await policy_capabilities(principal)
    assert response["actions"] == sorted(action for action in SCOPE_CAPABILITIES if action.endswith(":read"))
    assert response["capabilities"] == sorted(READER_CAPABILITIES)
    assert "native:keys:read" in response["actions"]
    assert "native:keys:write" not in response["actions"]
    assert "lumen-keys_editor" not in response["capabilities"]


@pytest.mark.parametrize("issued_roles", [("member",), ("member", "manager")])
async def test_operator_narrowed_key_scopes_keep_original_standard_role_ceiling(
    operator_policy, monkeypatch, issued_roles,
):
    from lumen.api import api_keys

    replace, _ = operator_policy
    replace('"native:tools:execute": "role:manager"')
    catalog = [{"id": f"{role}-id", "name": role, "domain_id": None} for role in ("member", "manager")]
    assignments = [{"role": {"id": role["id"]}, "user": {"id": "owner"},
                    "scope": {"project": {"id": "project"}}} for role in catalog]

    def get(path):
        if path == "/roles":
            key, rows = "roles", catalog
        elif path == "/role_inferences":
            key, rows = "role_inferences", []
        else:
            assert path == "/role_assignments?user.id=owner&scope.project.id=project&effective=True"
            key, rows = "role_assignments", assignments
        return None, {key: rows, "links": {}, "truncated": False}

    monkeypatch.setattr(auth, "_get_admin_ks_client", lambda: SimpleNamespace(
        get=get,
        users=SimpleNamespace(get=lambda identity: SimpleNamespace(id=identity, domain_id="domain", enabled=True)),
        projects=SimpleNamespace(get=lambda identity: SimpleNamespace(id=identity, domain_id="domain", enabled=True)),
        domains=SimpleNamespace(get=lambda identity: SimpleNamespace(id=identity, enabled=True)),
    ))
    monkeypatch.setattr(api_key_store, "owner_issuance_policy", AsyncMock(return_value={
        "monthly_credit_limit": None, "weekly_credit_limit": None,
    }))
    caller = actor(issued_roles, credential_restricted=True, source_project_id="project",
                   source_system_scope="", issued_role_ids=[f"{role}-id" for role in issued_roles])
    response = await api_keys.get_api_key_issuance_policy(caller)
    assert ("native:tools:execute" in response["allowed_scopes"]) is ("manager" in issued_roles)
    assert "models:read" in response["allowed_scopes"]
    row = SimpleNamespace(owner_user_id="owner", owner_project_id="project", is_active=True,
                          revoked_at=None, expires_at=None, scopes=["models:read", "native:tools:execute"])
    session = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: row)))
    scopes = await api_key_store.current_api_key_scopes(
        session, api_key_id=7, user_id="owner", project_id="project",
        credential_authority=auth.credential_authority_snapshot(caller))
    assert scopes == (frozenset(row.scopes) if "manager" in issued_roles else frozenset({"models:read"}))
    assert row.scopes == ["models:read", "native:tools:execute"]
