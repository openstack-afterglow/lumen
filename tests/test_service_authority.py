"""Service role/credential authority contract, with isolated FastAPI auth smoke.

These tests use synthetic Keystone responses; they never contact a paid provider.
"""
from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from keystoneclient.exceptions import NotFound

from lumen import auth
from lumen.service_authority import (
    READER_CAPABILITIES,
    SCOPE_CAPABILITIES,
    SERVICE_CAPABILITIES,
    allowed_scopes,
    capabilities,
)


def principal(roles, *, key=False, scopes=(), system=False):
    return {"auth_type": "api_key" if key else "keystone", "user_id": "user", "project_id": "project",
            "api_key_id": 7 if key else None, "source": "api" if key else "web", "roles": roles,
            "scopes": tuple(scopes), "is_system_admin": system}


@pytest.mark.parametrize("roles", [[], ["member"], ["project_admin"], ["project_owner"],
                                   ["member", "lumen_custom"], ["lumen_admin"],
                                   ["member", "admin", "lumen_admin"], ["member", "manager", "lumen_admin"]])
def test_unentitled_or_nonverified_native_admin_fails_closed(roles):
    assert capabilities(roles) == frozenset()
    with pytest.raises(HTTPException) as exc:
        auth.ensure_scopes(principal(roles), "compat:completions:write")
    assert exc.value.status_code == 403


@pytest.mark.parametrize("grade", ["reader", "user", "editor", "admin"])
def test_parent_name_has_no_runtime_authority_without_current_edges(grade):
    assert capabilities(["member", f"lumen_{grade}"]) == frozenset()


def test_reader_baseline_never_authorizes_use_or_configuration():
    assert capabilities(["reader", *SERVICE_CAPABILITIES]) == READER_CAPABILITIES
    assert allowed_scopes(["reader", *SERVICE_CAPABILITIES]) == allowed_scopes(["reader", *READER_CAPABILITIES])


@pytest.mark.parametrize("scope,leaf", [(scope, leaf) for scope, leaves in SCOPE_CAPABILITIES.items() for leaf in leaves])
def test_exact_leaf_is_narrow_and_scope_mapping_is_authoritative(scope, leaf):
    roles = ["member", leaf]
    assert capabilities(roles) == frozenset({leaf})
    assert auth.ensure_scopes(principal(roles), scope)


def test_key_scopes_only_attenuate_current_role_authority():
    p = principal(["member", "lumen-chat_user"], key=True,
                  scopes=["compat:completions:write", "compat:images:write", "native:tools:execute"])
    auth.ensure_scopes(p, "compat:completions:write")
    for scope in ["compat:images:write", "native:tools:execute"]:
        with pytest.raises(HTTPException):
            auth.ensure_scopes(p, scope)
    with pytest.raises(HTTPException):
        auth.ensure_scopes(principal(["member", *SERVICE_CAPABILITIES], key=True), "compat:completions:write")


def test_service_system_authority_does_not_assert_global_key_authority():
    p = principal([], key=True, scopes=["compat:completions:write"])
    p["service_system_admin"] = True
    assert auth.ensure_scopes(p, "compat:completions:write")
    assert p["is_system_admin"] is False
    with pytest.raises(HTTPException):
        auth.require_admin(p)


def _keystone(monkeypatch, *, assignments, role_rows, rules=None, user_enabled=True, project_enabled=True):
    queries = []
    def list_assignments(**kwargs):
        queries.append(kwargs)
        return assignments if "project" in kwargs else []
    ks = SimpleNamespace(role_assignments=SimpleNamespace(list=list_assignments),
                         roles=SimpleNamespace(list=lambda **kwargs: role_rows),
                         users=SimpleNamespace(get=lambda _id: SimpleNamespace(enabled=user_enabled)),
                         projects=SimpleNamespace(get=lambda _id: SimpleNamespace(enabled=project_enabled)),
                         inference_rules=SimpleNamespace(list_inference_roles=lambda: [] if rules is None else rules))
    monkeypatch.setattr(auth, "_get_admin_ks_client", lambda: ks)
    return queries


@pytest.mark.parametrize("subject", ["user", "project"])
async def test_disabled_current_owner_or_project_denies(monkeypatch, subject):
    _keystone(monkeypatch, assignments=[], role_rows=[],
              user_enabled=subject != "user", project_enabled=subject != "project")
    with pytest.raises(HTTPException) as exc:
        await auth.resolve_project_authority("user", "project")
    assert exc.value.status_code == 403


@pytest.mark.parametrize(("resource", "status"), [("users", 403), ("projects", 403), ("roles", 503)])
async def test_deleted_identity_is_permanent_denial_but_missing_catalog_is_unavailable(monkeypatch, resource, status):
    _keystone(monkeypatch, assignments=[], role_rows=[])
    manager = getattr(auth._get_admin_ks_client(), resource)
    def missing(*args, **kwargs):
        raise NotFound("Synthetic deleted identity or missing metadata endpoint")
    monkeypatch.setattr(manager, "list" if resource == "roles" else "get", missing)
    with pytest.raises(HTTPException) as exc:
        await auth.resolve_project_authority("user", "project")
    assert exc.value.status_code == status


async def test_current_effective_lookup_uses_group_inherited_project_grants(monkeypatch):
    rows = [SimpleNamespace(role={"id": "member"}, scope={"project": {"id": "project"}}),
            SimpleNamespace(role={"id": "chat"}, scope={"project": {"id": "project"}})]
    queries = _keystone(monkeypatch, assignments=rows,
                        role_rows=[SimpleNamespace(id="member", name="member", domain_id=None),
                                   SimpleNamespace(id="chat", name="lumen-chat_user", domain_id=None)])
    current = await auth.resolve_project_authority("user", "project")
    assert set(current["roles"]) == {"member", "lumen-chat_user"}
    assert queries == [{"user": "user", "project": "project", "effective": True}]


async def test_foreign_or_domain_assignment_is_not_project_membership(monkeypatch):
    _keystone(monkeypatch, assignments=[SimpleNamespace(role={"id": "chat"}, scope={"domain": {"id": "domain"}})],
              role_rows=[])
    with pytest.raises(HTTPException) as exc:
        await auth.resolve_project_authority("user", "project")
    assert exc.value.status_code == 403


async def test_current_assignment_provider_outage_is_not_stale_token_fallback(monkeypatch):
    def unavailable():
        raise RuntimeError("synthetic Keystone outage")
    monkeypatch.setattr(auth, "_get_admin_ks_client", unavailable)
    with pytest.raises(HTTPException) as exc:
        await auth.resolve_project_authority("user", "project")
    assert exc.value.status_code == 503


@pytest.mark.parametrize("scope,expected", [({"project": {"id": "admin"}}, False),
                                           ({"domain": {"id": "default"}}, False),
                                           ({"system": {"all": True}}, True)])
def test_only_verified_direct_system_assignment_is_global_admin(monkeypatch, scope, expected):
    query = []
    def assignments(**kwargs):
        query.append(kwargs)
        return [SimpleNamespace(role={"id": "global-admin-id"}, scope=scope)]
    monkeypatch.setattr(auth, "_get_admin_ks_client", lambda: SimpleNamespace(
        role_assignments=SimpleNamespace(list=assignments),
        roles=SimpleNamespace(list=lambda: [SimpleNamespace(id="global-admin-id", name="admin", domain_id=None)]),
        inference_rules=SimpleNamespace(list_inference_roles=lambda: [])))
    assert auth._is_system_admin("user") is expected
    assert query == [{"user": "user", "system": "all"}]


def test_domain_named_admin_role_cannot_be_global_admin_role(monkeypatch):
    _keystone(monkeypatch, assignments=[],
              role_rows=[SimpleNamespace(id="domain-admin", name="admin", domain_id="domain")])
    assert auth._is_system_admin("user") is False


async def test_actual_fastapi_auth_revalidates_current_roles_and_ignores_role_headers(monkeypatch):
    application = FastAPI()
    @application.post("/generate")
    async def generate(p=Depends(auth.require_scopes("compat:completions:write"))):
        return {"user_id": p["user_id"]}
    monkeypatch.setattr(auth, "validate_token", lambda *args, **kwargs: {
        "user_id": "user", "project_id": "project", "roles": ["member", "lumen_admin"],
        "is_system_admin": False, "auth_token": "synthetic"})
    current_roles = ["member", "lumen-chat_user"]
    async def current(user_id, project_id):
        return {"roles": list(current_roles), "is_system_admin": False}
    monkeypatch.setattr(auth, "resolve_project_authority", current)
    async with AsyncClient(transport=ASGITransport(application), base_url="http://synthetic") as client:
        headers = {"X-Auth-Token": "synthetic", "X-Roles": "lumen_admin", "X-Is-System-Admin": "true"}
        assert (await client.post("/generate", headers=headers)).status_code == 200
        current_roles[:] = ["reader", "lumen-history_reader"]
        assert (await client.post("/generate", headers=headers)).status_code == 403
        current_roles[:] = []
        assert (await client.post("/generate", headers=headers)).status_code == 403


@pytest.mark.parametrize("roles", [["member", *sorted(SERVICE_CAPABILITIES)], ["admin"], ["manager"]])
async def test_actual_global_provider_routes_reject_tenant_and_domain_admin(monkeypatch, roles):
    from unittest.mock import AsyncMock

    from lumen.api import models
    application = FastAPI()
    application.include_router(models.router, prefix="/v1")
    actor = principal(roles)
    application.dependency_overrides[auth.require_token] = lambda: actor
    repository = AsyncMock(return_value=[])
    monkeypatch.setattr(models.repository, "list_providers", repository)
    async with AsyncClient(transport=ASGITransport(application), base_url="http://synthetic") as client:
        assert (await client.get("/v1/admin/providers")).status_code == 403
        repository.assert_not_awaited()
        actor["is_system_admin"] = True
        assert (await client.get("/v1/admin/providers")).status_code == 200
        repository.assert_awaited_once()


async def test_current_keystone_edges_not_preset_bundle_are_runtime_authority(monkeypatch):
    catalog = [SimpleNamespace(id=rid, name=name, domain_id=None) for rid, name in [
        ("membership", "project_member"), ("member", "member"), ("user", "lumen_user"),
        ("chat", "lumen-chat_user"), ("images", "lumen-images_user")]]
    assignments = [SimpleNamespace(role={"id": rid}, scope={"project": {"id": "project"}})
                   for rid in ("membership", "user")]
    rules = [SimpleNamespace(prior_role={"id": "membership"}, implies=[{"id": "member"}]),
             SimpleNamespace(prior_role={"id": "user"}, implies=[{"id": "chat"}, {"id": "images"}])]
    _keystone(monkeypatch, assignments=assignments, role_rows=catalog, rules=rules)
    current = await auth.resolve_project_authority("user", "project")
    actor = principal(current["roles"])
    auth.ensure_scopes(actor, "compat:images:write", "compat:completions:write")
    rules[1].implies = [{"id": "chat"}]
    current = await auth.resolve_project_authority("user", "project")
    assert "lumen_user" in current["roles"]
    auth.ensure_scopes(principal(current["roles"]), "compat:completions:write")
    with pytest.raises(HTTPException):
        auth.ensure_scopes(principal(current["roles"]), "compat:images:write")
    rules[0].implies = []
    current = await auth.resolve_project_authority("user", "project")
    with pytest.raises(HTTPException):
        auth.ensure_scopes(principal(current["roles"]), "compat:completions:write")


async def test_same_named_domain_leaf_cannot_assert_global_builtin_authority(monkeypatch):
    _keystone(monkeypatch,
              assignments=[SimpleNamespace(role={"id": rid}, scope={"project": {"id": "project"}})
                           for rid in ("member", "domain-chat")],
              role_rows=[SimpleNamespace(id="member", name="member", domain_id=None),
                         SimpleNamespace(id="domain-chat", name="lumen-chat_user", domain_id="domain")])
    current = await auth.resolve_project_authority("user", "project")
    assert current["roles"] == ["member"]
    with pytest.raises(HTTPException):
        auth.ensure_scopes(principal(current["roles"]), "compat:completions:write")


@pytest.mark.parametrize("problem", ["missing", "unknown", "cycle", "duplicate-global", "domain-collision"])
async def test_missing_or_ambiguous_current_role_graph_never_falls_back(monkeypatch, problem):
    catalog = [SimpleNamespace(id="member", name="member", domain_id=None),
               SimpleNamespace(id="chat", name="lumen-chat_user", domain_id=None)]
    rules = []
    if problem == "unknown":
        rules = [SimpleNamespace(prior_role={"id": "chat"}, implies=[{"id": "absent"}])]
    elif problem == "cycle":
        rules = [SimpleNamespace(prior_role={"id": "chat"}, implies=[{"id": "chat"}])]
    elif problem == "duplicate-global":
        catalog.append(SimpleNamespace(id="other-chat", name="lumen-chat_user", domain_id=None))
    elif problem == "domain-collision":
        catalog.append(SimpleNamespace(id="domain-chat", name="lumen-chat_user", domain_id="domain"))
    _keystone(monkeypatch, assignments=[SimpleNamespace(role={"id": "member"}, scope={"project": {"id": "project"}})],
              role_rows=catalog, rules=rules)
    if problem == "missing":
        def unavailable():
            raise RuntimeError("synthetic inference API unavailable")
        monkeypatch.setattr(auth._get_admin_ks_client().inference_rules, "list_inference_roles", unavailable)
    with pytest.raises(HTTPException) as exc:
        await auth.resolve_project_authority("user", "project")
    assert exc.value.status_code == 503


def test_system_administrator_uses_current_global_ids_and_graph(monkeypatch):
    rules = [SimpleNamespace(prior_role={"id": "system-parent"}, implies=[{"id": "admin"}])]
    ks = SimpleNamespace(
        roles=SimpleNamespace(list=lambda: [SimpleNamespace(id="admin", name="admin", domain_id=None),
                                           SimpleNamespace(id="system-parent", name="platform-owner", domain_id=None)]),
        inference_rules=SimpleNamespace(list_inference_roles=lambda: rules),
        role_assignments=SimpleNamespace(list=lambda **kwargs: [SimpleNamespace(
            role={"id": "system-parent"}, scope={"system": {"all": True}})]))
    monkeypatch.setattr(auth, "_get_admin_ks_client", lambda: ks)
    assert auth._is_system_admin("user") is True
    rules[0].implies = []
    assert auth._is_system_admin("user") is False


@pytest.mark.parametrize("label", ["admin", "manager"])
async def test_domain_native_privileged_label_is_denial_not_global_authority(monkeypatch, label):
    _keystone(monkeypatch,
              assignments=[SimpleNamespace(role={"id": rid}, scope={"project": {"id": "project"}})
                           for rid in ("member", "chat", "domain-privileged")],
              role_rows=[SimpleNamespace(id="member", name="member", domain_id=None),
                         SimpleNamespace(id="chat", name="lumen-chat_user", domain_id=None),
                         SimpleNamespace(id="domain-privileged", name=label, domain_id="domain")])
    current = await auth.resolve_project_authority("user", "project")
    assert current["is_system_admin"] is False
    with pytest.raises(HTTPException):
        auth.ensure_scopes(principal(current["roles"]), "compat:completions:write")
