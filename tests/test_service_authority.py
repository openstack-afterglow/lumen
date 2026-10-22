"""Service role/credential authority contract, with isolated FastAPI auth smoke.

These tests use synthetic Keystone responses; they never contact a paid provider.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from urllib.parse import urlencode

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


@pytest.mark.parametrize("roles", [[], ["member"], ["reader"], ["manager"], ["project_admin"],
                                   ["project_owner"], ["waygate_admin"], ["member", "lumen_custom"]])
def test_unentitled_native_roles_fail_closed(roles):
    assert capabilities(principal(roles)) == frozenset()
    with pytest.raises(HTTPException) as exc:
        auth.ensure_scopes(principal(roles), "compat:completions:write")
    assert exc.value.status_code == 403


@pytest.mark.parametrize("grade", ["reader", "user", "editor", "admin"])
def test_grade_alone_has_local_policy_authority_without_edges_or_corole(grade):
    actor = principal([f"lumen_{grade}"])
    assert "models:read" in allowed_scopes(actor)
    if grade == "reader":
        assert capabilities(actor) == READER_CAPABILITIES
        with pytest.raises(HTTPException):
            auth.ensure_scopes(actor, "compat:completions:write")
    else:
        auth.ensure_scopes(actor, "compat:completions:write")
    assert actor["roles"] == [f"lumen_{grade}"]


def test_project_admin_is_local_not_system_authority():
    actor = principal(["admin"])
    auth.ensure_scopes(actor, "compat:completions:write")
    with pytest.raises(HTTPException):
        auth.require_admin(actor)


@pytest.mark.parametrize("scope,leaf", [(scope, leaf) for scope, leaves in SCOPE_CAPABILITIES.items() for leaf in leaves])
def test_exact_leaf_is_narrow_and_scope_mapping_is_authoritative(scope, leaf):
    roles = ["member", leaf]
    assert capabilities(principal(roles)) == frozenset({leaf})
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


def _assignment(role_id, *, scope=None, user_id="user"):
    return {"role": {"id": role_id}, "user": {"id": user_id},
            "scope": {"project": {"id": "project"}} if scope is None else scope}


def _keystone(monkeypatch, *, assignments, role_rows, rules=None, user_enabled=True, project_enabled=True,
              system_assignments=(), groups=(), group_assignments=None):
    queries = []
    def get(path):
        if path == "/roles":
            key, rows = "roles", [vars(row) if isinstance(row, SimpleNamespace) else row for row in role_rows]
        elif path == "/role_inferences":
            key, rows = "role_inferences", [vars(row) if isinstance(row, SimpleNamespace) else row
                                             for row in ([] if rules is None else rules)]
        elif path == "/users/user/groups":
            key, rows = "groups", list(groups)
        else:
            key = "role_assignments"
            if path == "/role_assignments?" + urlencode({"user.id": "user", "scope.project.id": "project", "effective": True}):
                queries.append({"user": "user", "project": "project", "effective": True})
                rows = assignments
            elif path == "/role_assignments?" + urlencode({"user.id": "user", "scope.system": "all"}):
                queries.append({"user": "user", "system": "all"})
                rows = list(system_assignments)
            else:
                matching = [group_id for group_id in (group_assignments or {})
                            if path == "/role_assignments?" + urlencode({"group.id": group_id, "scope.system": "all"})]
                assert len(matching) == 1, f"Unexpected authority collection: {path}"
                group_id = matching[0]
                queries.append({"group": group_id, "system": "all"})
                rows = group_assignments[group_id]
        return None, {key: rows, "links": {}, "truncated": False}
    ks = SimpleNamespace(get=get,
                         users=SimpleNamespace(get=lambda _id: SimpleNamespace(
                             id="user", domain_id="user-domain", enabled=user_enabled)),
                         projects=SimpleNamespace(get=lambda _id: SimpleNamespace(
                             id="project", domain_id="project-domain", enabled=project_enabled)),
                         domains=SimpleNamespace(get=lambda _id: SimpleNamespace(id=_id, enabled=True)))
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
    ks = auth._get_admin_ks_client()
    def missing(*args, **kwargs):
        raise NotFound("Synthetic deleted identity or missing metadata endpoint")
    if resource == "roles":
        native_get = ks.get
        def get(path):
            return missing() if path == "/roles" else native_get(path)
        monkeypatch.setattr(ks, "get", get)
    else:
        monkeypatch.setattr(getattr(ks, resource), "get", missing)
    with pytest.raises(HTTPException) as exc:
        await auth.resolve_project_authority("user", "project")
    assert exc.value.status_code == status


async def test_current_effective_lookup_uses_group_inherited_project_grants(monkeypatch):
    # Effective group/inherited grants are resolved by Keystone to the exact user.
    rows = [_assignment("member"), _assignment("chat")]
    queries = _keystone(monkeypatch, assignments=rows,
                        role_rows=[SimpleNamespace(id="member", name="member", domain_id=None),
                                   SimpleNamespace(id="chat", name="lumen-chat_user", domain_id=None)])
    current = await auth.resolve_project_authority("user", "project")
    assert set(current["roles"]) == {"member", "lumen-chat_user"}
    assert queries == [{"user": "user", "project": "project", "effective": True}]


async def test_foreign_or_domain_assignment_is_not_project_membership(monkeypatch):
    _keystone(monkeypatch, assignments=[_assignment("chat", scope={"domain": {"id": "domain"}})],
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
def test_only_exact_raw_system_assignment_is_global_admin(monkeypatch, scope, expected):
    queries = _keystone(monkeypatch, assignments=[],
                        system_assignments=[_assignment("global-admin-id", scope=scope)],
                        role_rows=[SimpleNamespace(id="global-admin-id", name="admin", domain_id=None)])
    assert auth._is_system_admin("user") is expected
    assert queries == [{"user": "user", "system": "all"}]


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


@pytest.mark.parametrize("roles", [[], ["member", *sorted(SERVICE_CAPABILITIES)], ["admin"], ["manager"]])
async def test_actual_global_provider_routes_reject_tenant_and_domain_admin(monkeypatch, roles):

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


async def test_current_grade_authorizes_without_graph_expansion_and_revokes_with_assignment(monkeypatch):
    assignments = [_assignment("user")]
    _keystone(monkeypatch, assignments=assignments, rules=[],
              role_rows=[SimpleNamespace(id="user", name="lumen_user", domain_id=None)])
    current = await auth.resolve_project_authority("user", "project")
    assert current == {"roles": ["lumen_user"], "is_system_admin": False}
    auth.ensure_scopes(principal(current["roles"]), "compat:images:write", "compat:completions:write")
    assignments.clear()
    with pytest.raises(HTTPException) as error:
        await auth.resolve_project_authority("user", "project")
    assert error.value.status_code == 403


async def test_same_named_domain_leaf_cannot_assert_global_builtin_authority(monkeypatch):
    _keystone(monkeypatch,
              assignments=[_assignment(rid) for rid in ("member", "domain-chat")],
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
    _keystone(monkeypatch, assignments=[_assignment("member")],
              role_rows=catalog, rules=rules)
    if problem == "missing":
        ks = auth._get_admin_ks_client()
        native_get = ks.get
        def get(path):
            if path == "/role_inferences":
                raise RuntimeError("synthetic inference API unavailable")
            return native_get(path)
        monkeypatch.setattr(ks, "get", get)
    with pytest.raises(HTTPException) as exc:
        await auth.resolve_project_authority("user", "project")
    assert exc.value.status_code == 503


def test_system_administrator_uses_current_global_ids_and_graph(monkeypatch):
    rules = [SimpleNamespace(prior_role={"id": "system-parent"}, implies=[{"id": "admin"}])]
    _keystone(monkeypatch, assignments=[], rules=rules,
              role_rows=[SimpleNamespace(id="admin", name="admin", domain_id=None),
                         SimpleNamespace(id="system-parent", name="platform-owner", domain_id=None)],
              system_assignments=[_assignment("system-parent", scope={"system": {"all": True}})])
    assert auth._is_system_admin("user") is True
    rules[0].implies = []
    assert auth._is_system_admin("user") is False


@pytest.mark.parametrize("label", ["admin", "manager"])
async def test_domain_privileged_label_does_not_add_global_or_service_authority(monkeypatch, label):
    _keystone(monkeypatch,
              assignments=[_assignment(rid) for rid in ("member", "domain-privileged")],
              role_rows=[SimpleNamespace(id="member", name="member", domain_id=None),
                         SimpleNamespace(id="domain-privileged", name=label, domain_id="domain")])
    current = await auth.resolve_project_authority("user", "project")
    assert current == {"roles": ["member"], "is_system_admin": False}
    with pytest.raises(HTTPException):
        auth.ensure_scopes(principal(current["roles"]), "compat:completions:write")


@pytest.mark.parametrize("scope", sorted(SCOPE_CAPABILITIES))
def test_zero_target_roles_system_admin_is_alternative_to_every_service_leaf(scope):
    actor = principal([], system=True)
    assert capabilities(actor) == SERVICE_CAPABILITIES
    assert auth.ensure_scopes(actor, scope) is actor
    assert actor["roles"] == []
    actor["is_system_admin"] = False
    with pytest.raises(HTTPException) as error:
        auth.ensure_scopes(actor, scope)
    assert error.value.status_code == 403


@pytest.fixture
def zero_role_system_token(monkeypatch):
    # Token validation and the live directory are synthetic; native route gates
    # remain real. Logical target authority never replaces the home connection.
    validation = Mock(return_value={
        "user_id": "user", "project_id": "target", "connection_project_id": "home",
        "roles": [], "is_system_admin": True, "auth_token": "synthetic-home-token",
    })
    authority = AsyncMock(return_value={"roles": [], "is_system_admin": True})
    monkeypatch.setattr(auth, "validate_token", validation)
    monkeypatch.setattr(auth, "resolve_project_authority", authority)
    return validation, authority


@pytest.mark.parametrize("credential", [
    {"X-Auth-Token": "synthetic-home-token"},
    {"Authorization": "Bearer " + "synthetic-home-token"},
], ids=["x-auth-token", "bearer"])
async def test_native_agent_crud_zero_role_system_admin_preserves_owner_namespace(
    monkeypatch, zero_role_system_token, credential,
):
    from lumen.api import agents

    application = FastAPI()
    application.include_router(agents.router, prefix="/v1")
    create = AsyncMock(return_value={"id": 7})
    listing = AsyncMock(return_value=[])
    detail = AsyncMock(return_value={"id": 7})
    update = AsyncMock(return_value={"id": 7})
    delete = AsyncMock()
    for name, operation in (("create_agent", create), ("list_agents", listing), ("get_agent", detail),
                            ("update_agent", update), ("delete_agent", delete)):
        monkeypatch.setattr(agents.ags, name, operation)
    headers = {**credential, "X-Project-Id": "home", "X-Target-Project-Id": "target"}
    async with AsyncClient(transport=ASGITransport(application), base_url="http://synthetic") as client:
        assert (await client.post("/v1/agents", headers=headers, json={"name": "owned"})).status_code == 201
        assert (await client.get("/v1/agents?include_private=true", headers=headers)).status_code == 200
        assert (await client.get("/v1/agents/7", headers=headers)).status_code == 200
        assert (await client.patch("/v1/agents/7", headers=headers, json={"name": "renamed"})).status_code == 200
        assert (await client.delete("/v1/agents/7", headers=headers)).status_code == 204
        # Even a trusted system administrator takes the user route, not an
        # admin=True service path, when a store rejects another owner's row.
        detail.side_effect = agents.ags.AgentForbidden("not owned")
        delete.side_effect = agents.ags.AgentForbidden("not owned")
        assert (await client.get("/v1/agents/8", headers=headers)).status_code == 403
        assert (await client.delete("/v1/agents/8", headers=headers)).status_code == 403
    create.assert_awaited_once_with(owner_user_id="user", project_id="target", **agents.AgentCreate(name="owned").model_dump())
    listing.assert_awaited_once_with(user_id="user", project_id="target", include_private=True)
    assert detail.await_args_list == [
        ((7,), {"user_id": "user", "project_id": "target"}),
        ((8,), {"user_id": "user", "project_id": "target"}),
    ]
    update.assert_awaited_once_with(7, user_id="user", project_id="target", patch={"name": "renamed"})
    assert delete.await_args_list == detail.await_args_list
    validation, authority = zero_role_system_token
    assert validation.call_count == authority.await_count == 7
    validation.assert_called_with("synthetic-home-token", project_id="home", target_project_id="target")
    authority.assert_awaited_with("user", "target")


@pytest.mark.parametrize("credential", [
    {"X-Auth-Token": "synthetic-home-token"},
    {"Authorization": "Bearer " + "synthetic-home-token"},
], ids=["x-auth-token", "bearer"])
@pytest.mark.parametrize("kind,path,payload", [
    ("mcp", "mcp-servers", {"name": "remote", "url": "https://mcp.example"}),
    ("tool", "custom-tools", {"name": "remote", "url": "https://tool.example"}),
    ("skill", "skills", {"name": "review", "instructions": "Review the input"}),
])
async def test_native_global_extension_crud_accepts_zero_role_system_admin(
    monkeypatch, zero_role_system_token, credential, kind, path, payload,
):
    from lumen.api import extensions

    application = FastAPI()
    application.include_router(extensions.admin_router, prefix="/v1")
    listing = AsyncMock(return_value=[])
    create = AsyncMock(return_value={"id": 7, "scope": "global"})
    update = AsyncMock(return_value={"id": 7, "scope": "global"})
    delete = AsyncMock()
    for name, operation in (("list_global", listing), ("create", create), ("update", update), ("delete", delete)):
        monkeypatch.setattr(extensions.es, name, operation)
    headers = {**credential, "X-Project-Id": "home", "X-Target-Project-Id": "target"}
    url = f"/v1/admin/{path}"
    async with AsyncClient(transport=ASGITransport(application), base_url="http://synthetic") as client:
        assert (await client.get(url, headers=headers)).status_code == 200
        assert (await client.post(url, headers=headers, json=payload)).status_code == 201
        assert (await client.patch(f"{url}/7", headers=headers, json=payload)).status_code == 200
        assert (await client.delete(f"{url}/7", headers=headers)).status_code == 204
    listing.assert_awaited_once_with(kind)
    create.assert_awaited_once_with(kind, payload, scope="global")
    update.assert_awaited_once_with(kind, 7, payload, admin=True)
    delete.assert_awaited_once_with(kind, 7, admin=True)
    validation, authority = zero_role_system_token
    assert validation.call_count == authority.await_count == 4
    validation.assert_called_with("synthetic-home-token", project_id="home", target_project_id="target")
    authority.assert_awaited_with("user", "target")


@pytest.mark.parametrize("subject", ["users", "projects", "domains"])
@pytest.mark.parametrize("problem,status", [("foreign-id", 503), ("missing-enabled", 503),
                                           ("nonboolean-enabled", 503), ("disabled", 403),
                                           ("deleted", 403), ("unavailable", 503)])
async def test_native_identity_domain_metadata_fails_closed(monkeypatch, subject, problem, status):
    _keystone(monkeypatch, assignments=[_assignment("member")],
              role_rows=[SimpleNamespace(id="member", name="member", domain_id=None)])
    def get(identity):
        if problem == "deleted":
            raise NotFound("deleted native identity")
        if problem == "unavailable":
            raise RuntimeError("native metadata unavailable")
        fields = {"id": "foreign" if problem == "foreign-id" else identity,
                  "domain_id": "user-domain" if subject == "users" else "project-domain"}
        if problem != "missing-enabled":
            fields["enabled"] = {"disabled": False, "nonboolean-enabled": "true"}.get(problem, True)
        return SimpleNamespace(**fields)
    monkeypatch.setattr(getattr(auth._get_admin_ks_client(), subject), "get", get)
    with pytest.raises(HTTPException) as error:
        await auth.resolve_project_authority("user", "project")
    assert error.value.status_code == status


@pytest.mark.parametrize("row,status", [
    (_assignment("member", user_id="foreign"), 403),
    (_assignment("member", scope={"project": {"id": "foreign"}}), 403),
    ({"role": {"id": "member"}, "group": {"id": "group"},
      "scope": {"project": {"id": "project"}}}, 403),
    (SimpleNamespace(role={"id": "member"}), 503),
    (_assignment(""), 503),
])
async def test_project_grants_require_exact_effective_user_and_project(monkeypatch, row, status):
    _keystone(monkeypatch, assignments=[row],
              role_rows=[SimpleNamespace(id="member", name="member", domain_id=None)])
    with pytest.raises(HTTPException) as error:
        await auth.resolve_project_authority("user", "project")
    assert error.value.status_code == status


async def test_raw_group_system_grant_and_current_membership_are_both_required(monkeypatch):
    groups = [{"id": "operators"}]
    rules = [SimpleNamespace(prior_role={"id": "parent"}, implies=[{"id": "admin"}])]
    row = {"group": {"id": "operators"}, "role": {"id": "parent"}, "scope": {"system": {"all": True}}}
    queries = _keystone(monkeypatch, assignments=[], groups=groups, rules=rules,
                        group_assignments={"operators": [row]},
                        role_rows=[SimpleNamespace(id="parent", name="platform-owner", domain_id=None),
                                   SimpleNamespace(id="admin", name="admin", domain_id=None)])
    assert await auth.resolve_project_authority("user", "project") == {"roles": [], "is_system_admin": True}
    assert queries == [{"user": "user", "project": "project", "effective": True},
                       {"user": "user", "system": "all"}, {"group": "operators", "system": "all"}]
    rules[0].implies = []
    assert auth._is_system_admin("user") is False
    rules[0].implies = [{"id": "admin"}]
    groups.clear()
    assert auth._is_system_admin("user") is False


@pytest.mark.parametrize("row", [
    _assignment("admin", user_id="foreign", scope={"system": {"all": True}}),
    {"group": {"id": "foreign"}, "role": {"id": "admin"}, "scope": {"system": {"all": True}}},
    {**_assignment("admin", scope={"system": {"all": True}}), "group": {"id": "operators"}},
    _assignment("admin", scope={"system": {"all": 1}}),
    _assignment("admin", scope={"system": {"all": True}, "project": {"id": "project"}}),
])
def test_foreign_ambiguous_or_nonexact_raw_system_grants_never_elevate(monkeypatch, row):
    _keystone(monkeypatch, assignments=[], system_assignments=[row],
              role_rows=[SimpleNamespace(id="admin", name="admin", domain_id=None)])
    assert auth._is_system_admin("user") is False


@pytest.mark.parametrize("problem", ["malformed-row", "missing-role", "malformed-group", "group-outage", "raw-outage"])
def test_raw_system_or_group_metadata_failure_is_unavailable(monkeypatch, problem):
    _keystone(monkeypatch, assignments=[],
              system_assignments=[SimpleNamespace()] if problem == "malformed-row" else
              [_assignment("", scope={"system": {"all": True}})] if problem == "missing-role" else [],
              groups=[{}] if problem == "malformed-group" else [],
              role_rows=[SimpleNamespace(id="admin", name="admin", domain_id=None)])
    ks = auth._get_admin_ks_client()
    native_get = ks.get
    def get(path):
        if ((problem == "group-outage" and path == "/users/user/groups")
                or (problem == "raw-outage" and path == "/role_assignments?user.id=user&scope.system=all")):
            raise RuntimeError("current raw/group metadata unavailable")
        return native_get(path)
    monkeypatch.setattr(ks, "get", get)
    with pytest.raises(HTTPException) as error:
        auth._is_system_admin("user")
    assert error.value.status_code == 503


async def test_reduced_issued_ids_are_a_ceiling_not_an_inference_root(monkeypatch):
    catalog = [SimpleNamespace(id=rid, name=name, domain_id=None) for rid, name in
               [("member", "member"), ("parent", "lumen_user"), ("chat", "lumen-chat_user"), ("admin", "admin")]]
    rules = [SimpleNamespace(prior_role={"id": "parent"}, implies=[{"id": "chat"}])]
    queries = _keystone(monkeypatch, assignments=[_assignment("member"), _assignment("parent")],
                        role_rows=catalog, rules=rules,
                        system_assignments=[_assignment("admin", scope={"system": {"all": True}})])
    current = auth._resolve_project_authority("user", "project", issued_role_ids=("member", "parent"))
    assert current == {"roles": ["lumen_user", "member"], "is_system_admin": False}
    assert queries == [{"user": "user", "project": "project", "effective": True}]
    auth.ensure_scopes(principal(current["roles"]), "compat:completions:write")
    current = auth._resolve_project_authority("user", "project", issued_role_ids=("member", "chat"))
    assert current == {"roles": ["member"], "is_system_admin": False}
    with pytest.raises(HTTPException):
        auth.ensure_scopes(principal(current["roles"]), "compat:completions:write")


@pytest.mark.parametrize("subject", [{"group": {"id": "foreign"}},
                                    {"user": {"id": "user"}},
                                    {"group": {"id": "operators"}, "user": {"id": "user"}}])
def test_group_system_query_rejects_foreign_or_ambiguous_principal(monkeypatch, subject):
    row = {**subject, "role": {"id": "admin"}, "scope": {"system": {"all": True}}}
    queries = _keystone(monkeypatch, assignments=[], groups=[{"id": "operators"}],
                        group_assignments={"operators": [row]},
                        role_rows=[SimpleNamespace(id="admin", name="admin", domain_id=None)])
    assert auth._is_system_admin("user") is False
    assert queries == [{"user": "user", "system": "all"}, {"group": "operators", "system": "all"}]


@pytest.mark.parametrize("path,key", [
    ("/roles", "roles"), ("/role_inferences", "role_inferences"),
    ("/role_assignments?user.id=user&scope.project.id=project&effective=True", "role_assignments"),
    ("/role_assignments?user.id=user&scope.system=all", "role_assignments"),
    ("/users/user/groups", "groups"),
    ("/role_assignments?group.id=operators&scope.system=all", "role_assignments"),
])
@pytest.mark.parametrize("problem", ["next-page", "truncated", "malformed-links", "missing-rows",
                                     "nonlist-rows", "malformed-row", "nondict-envelope"])
async def test_every_authority_collection_requires_complete_native_envelope(monkeypatch, path, key, problem):
    group_admin = {"group": {"id": "operators"}, "role": {"id": "admin"}, "scope": {"system": {"all": True}}}
    _keystone(monkeypatch, assignments=[_assignment("member")], groups=[{"id": "operators"}],
              group_assignments={"operators": [group_admin]},
              role_rows=[SimpleNamespace(id="member", name="member", domain_id=None),
                         SimpleNamespace(id="admin", name="admin", domain_id=None)])
    ks = auth._get_admin_ks_client()
    native_get = ks.get
    rejected = []
    def get(request_path):
        response, payload = native_get(request_path)
        if request_path != path:
            return response, payload
        rejected.append(request_path)
        if problem == "next-page":
            payload["links"] = {"next": "/v3/next"}
        elif problem == "truncated":
            payload["truncated"] = True
        elif problem == "malformed-links":
            payload["links"] = []
        elif problem == "missing-rows":
            payload.pop(key)
        elif problem == "nonlist-rows":
            payload[key] = {}
        elif problem == "malformed-row":
            payload[key] = [SimpleNamespace(id="not-a-native-JSON-object")]
        else:
            payload = []
        return response, payload
    monkeypatch.setattr(ks, "get", get)
    with pytest.raises(HTTPException) as error:
        await auth.resolve_project_authority("user", "project")
    assert error.value.status_code == 503
    assert rejected == [path]
