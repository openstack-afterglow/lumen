"""Synthetic native Keystone contract; no production identity or token is used."""

from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import HTTPException
from keystoneclient.exceptions import NotFound

from lumen import auth
from lumen.services.mcp_domain_authority import resolve_domain_authority


def assignment(role="member-id", *, project=None, domain=None, system=False, group=None, inherited=False):
    scope = {"system": {"all": True}} if system else {
        "domain" if domain else "project": {"id": domain or project or "project-a"},
    }
    if inherited:
        scope["OS-INHERIT:inherited_to"] = "projects"
    return {"role": {"id": role}, "group" if group else "user": {"id": group or "user"}, "scope": scope}


class Directory:
    def __init__(self):
        self.user = SimpleNamespace(id="user", domain_id="home", enabled=True)
        self.domain_rows = {name: SimpleNamespace(id=name, enabled=True) for name in ("home", "a", "b")}
        self.project_rows = {
            "project-a": SimpleNamespace(id="project-a", domain_id="a", name="Work", enabled=True, parent_id=None),
            "project-b": SimpleNamespace(id="project-b", domain_id="b", name="Work", enabled=True, parent_id=None),
            "default-a": SimpleNamespace(id="default-a", domain_id="a", name="Default", enabled=True, parent_id=None),
            "default-b": SimpleNamespace(id="default-b", domain_id="b", name="Default", enabled=True, parent_id=None),
        }
        self.roles = [{"id": f"{name}-id", "name": name, "domain_id": None}
                      for name in ("admin", "manager", "member", "reader")]
        self.inferences = []
        self.assignments = [assignment()]
        self.effective = {"project-a": [assignment()], "project-b": [assignment(project="project-b")]}
        self.group_rows = {}
        self.groups_for_user = []
        self.overrides = {}
        self.paths = []
        self.users = SimpleNamespace(get=lambda uid: self.exact({"user": self.user}, uid))
        self.projects = SimpleNamespace(get=lambda pid: self.exact(self.project_rows, pid))
        self.domains = SimpleNamespace(get=lambda did: self.exact(self.domain_rows, did))
        self.groups = SimpleNamespace(get=lambda gid: self.exact(self.group_rows, gid))

    @staticmethod
    def exact(rows, key):
        if key not in rows:
            raise NotFound("Synthetic missing identity")
        return rows[key]

    def grant_manager(self, *, domain="a", group=None, role="manager-id", inherited=False):
        self.assignments.append(assignment(role, project=f"default-{domain}", group=group, inherited=inherited))
        self.effective[f"default-{domain}"] = [assignment("manager-id", project=f"default-{domain}")]

    def add_group(self, gid="group", *, projection=False):
        self.groups_for_user.append({"id": gid})
        self.group_rows[gid] = SimpleNamespace(
            id=gid, name="afterglow-default-managers-a" if projection else "Team",
            description="afterglow:default-manager-reader:v1:a" if projection else "",
        )

    def get(self, path):
        self.paths.append(path)
        parsed = urlsplit(path)
        query = {key: values[0] for key, values in parse_qs(parsed.query).items()}
        if parsed.path == "/roles":
            key, rows = "roles", self.roles
        elif parsed.path == "/role_inferences":
            key, rows = "role_inferences", self.inferences
        elif parsed.path == "/users/user/groups":
            key, rows = "groups", self.groups_for_user
        elif parsed.path == "/projects":
            key = "projects"
            rows = [vars(project) for project in self.project_rows.values()
                    if project.domain_id == query["domain_id"]]
        elif parsed.path == "/role_assignments":
            key = "role_assignments"
            if "effective" in query:
                rows = self.effective.get(query["scope.project.id"], [])
            else:
                kind = "group" if "group.id" in query else "user"
                subject = query[f"{kind}.id"]
                rows = [row for row in self.assignments if row.get(kind, {}).get("id") == subject]
                if "scope.system" in query:
                    rows = [row for row in rows if "system" in row["scope"]]
        else:
            raise AssertionError(f"Unexpected native request: {path}")
        return None, {key: rows, "links": {}, **self.overrides.get(parsed.path, {})}


@pytest.fixture
def directory(monkeypatch):
    native = Directory()
    monkeypatch.setattr(auth, "_get_admin_ks_client", lambda: native)
    return native


def reduced(*roles, project="project-a"):
    return {"credential_restricted": True, "user_id": "user", "source_project_id": project,
            "source_system_scope": "", "issued_role_ids": list(roles)}


@pytest.mark.parametrize("role", ["admin", "manager", "member", "reader"])
async def test_arbitrary_project_role_is_domain_member_not_domain_manager(directory, role):
    directory.assignments = [assignment(f"{role}-id")]
    directory.effective["project-a"] = [assignment(f"{role}-id")]
    assert await resolve_domain_authority("user", "project-a") == {
        "domain_id": "a", "is_member": True, "can_manage": False,
    }


@pytest.mark.parametrize("group", [None, "group"])
async def test_canonical_default_project_manager_manages_exact_domain(directory, group):
    if group:
        directory.add_group(group)
    directory.grant_manager(group=group)
    assert await resolve_domain_authority("user", "project-a", "a") == {
        "domain_id": "a", "is_member": True, "can_manage": True,
    }
    assert (await resolve_domain_authority("user", "project-b"))["can_manage"] is False


async def test_default_project_admin_with_native_manager_implication_manages(directory):
    directory.inferences = [{"prior_role": {"id": "admin-id"}, "implies": [{"id": "manager-id"}]}]
    directory.grant_manager(role="admin-id")
    assert (await resolve_domain_authority("user", "project-a"))["can_manage"] is True


async def test_default_manager_uses_native_custom_role_ids(directory):
    directory.roles.append({"id": "team-lead-native-id", "name": "team-lead", "domain_id": None})
    directory.inferences = [{"prior_role": {"id": "team-lead-native-id"}, "implies": [{"id": "manager-id"}]}]
    directory.grant_manager(role="team-lead-native-id")
    assert (await resolve_domain_authority("user", "project-a"))["can_manage"] is True


@pytest.mark.parametrize("source", ["domain", "ancestor"])
async def test_native_inheritance_to_default_project_is_valid_manager_source(directory, source):
    if source == "domain":
        directory.assignments.append(assignment("manager-id", domain="a", inherited=True))
    else:
        directory.project_rows["default-a"].parent_id = "project-a"
        directory.assignments.append(assignment("manager-id", inherited=True))
    directory.effective["default-a"] = [assignment("manager-id", project="default-a")]
    assert (await resolve_domain_authority("user", "project-a"))["can_manage"] is True


async def test_noninherited_domain_manager_is_exact_domain_only(directory):
    directory.assignments.append(assignment("manager-id", domain="a"))
    assert (await resolve_domain_authority("user", "project-a"))["can_manage"] is True
    assert (await resolve_domain_authority("user", "project-b"))["can_manage"] is False


async def test_projection_derived_reader_never_creates_manager_authority(directory):
    directory.add_group(projection=True)
    directory.assignments.append(assignment("reader-id", domain="a", inherited=True, group="group"))
    directory.effective["default-a"] = [assignment("reader-id", project="default-a")]
    assert (await resolve_domain_authority("user", "project-a"))["can_manage"] is False


async def test_projection_only_manager_cannot_self_prove_authority(directory):
    directory.add_group(projection=True)
    directory.assignments.append(assignment("manager-id", domain="a", inherited=True, group="group"))
    projected = assignment("manager-id", project="default-a")
    projected["links"] = {"membership": "/v3/groups/group/users/user"}
    directory.effective["default-a"] = [projected]
    assert (await resolve_domain_authority("user", "project-a"))["can_manage"] is False


async def test_foreign_explicit_domain_rejects_even_its_default_manager(directory):
    directory.grant_manager(domain="b")
    with pytest.raises(HTTPException) as error:
        await resolve_domain_authority("user", "project-a", "b")
    assert error.value.status_code == 403


@pytest.mark.parametrize(("name", "marker"), [
    ("Accounts", None), ("Account", None), ("Personal", None), ("Other", "account"), ("Other", "personal"),
])
async def test_reserved_domain_never_uses_default_project_manager(directory, name, marker):
    directory.domain_rows["a"].name = name
    directory.domain_rows["a"].afterglow_domain_kind = marker
    directory.grant_manager()
    assert (await resolve_domain_authority("user", "project-a"))["can_manage"] is False


async def test_revoked_manager_with_stale_projection_remains_member(directory):
    directory.add_group(projection=True)
    directory.grant_manager()
    projected = assignment("manager-id", project="default-a")
    projected["links"] = {"membership": "/v3/groups/group/users/user"}
    directory.effective["default-a"].append(projected)
    assert (await resolve_domain_authority("user", "project-a"))["can_manage"] is True
    directory.assignments = [assignment()]
    directory.effective["default-a"] = [projected]
    assert await resolve_domain_authority("user", "project-a") == {
        "domain_id": "a", "is_member": True, "can_manage": False,
    }


@pytest.mark.parametrize("group", [None, "group"])
async def test_fresh_human_system_admin_can_select_validated_foreign_domain(directory, group):
    directory.effective["project-a"] = []
    if group:
        directory.add_group(group)
    directory.assignments = [assignment("admin-id", system=True, group=group)]
    assert await resolve_domain_authority("user", "project-a", "b") == {
        "domain_id": "b", "is_member": True, "can_manage": True,
    }
    directory.domain_rows["b"].enabled = False
    with pytest.raises(HTTPException) as error:
        await resolve_domain_authority("user", "project-a", "b")
    assert error.value.status_code == 403


@pytest.mark.parametrize("manager_kind", ["default", "domain", "system"])
async def test_reduced_credential_never_borrows_owner_management_authority(directory, manager_kind):
    if manager_kind == "default":
        directory.grant_manager()
    elif manager_kind == "domain":
        directory.assignments.append(assignment("manager-id", domain="a"))
    else:
        directory.assignments.append(assignment("admin-id", system=True))
    assert await resolve_domain_authority("user", "project-a", credential_authority=reduced("member-id")) == {
        "domain_id": "a", "is_member": True, "can_manage": False,
    }


async def test_reduced_membership_revocation_denies_despite_new_owner_roles(directory):
    directory.effective["project-a"] = [assignment("admin-id")]
    with pytest.raises(HTTPException) as error:
        await resolve_domain_authority("user", "project-a", credential_authority=reduced("member-id"))
    assert error.value.status_code == 403


async def test_reduced_identity_cannot_change_original_project(directory):
    with pytest.raises(HTTPException) as error:
        await resolve_domain_authority("user", "project-b", credential_authority=reduced("member-id"))
    assert error.value.status_code == 403


async def test_manager_revocation_is_observed_on_next_lookup(directory):
    directory.grant_manager()
    assert (await resolve_domain_authority("user", "project-a"))["can_manage"] is True
    directory.assignments = [assignment()]
    directory.effective["default-a"] = []
    assert (await resolve_domain_authority("user", "project-a"))["can_manage"] is False
    directory.effective["project-a"] = []
    with pytest.raises(HTTPException) as error:
        await resolve_domain_authority("user", "project-a")
    assert error.value.status_code == 403


async def test_group_membership_revocation_removes_default_management(directory):
    directory.add_group()
    directory.grant_manager(group="group")
    assert (await resolve_domain_authority("user", "project-a"))["can_manage"] is True
    directory.groups_for_user = []
    directory.effective["default-a"] = []
    assert (await resolve_domain_authority("user", "project-a"))["can_manage"] is False


@pytest.mark.parametrize("resource", ["user", "project", "home", "domain"])
async def test_disabled_native_identity_denies(directory, resource):
    row = {"user": directory.user, "project": directory.project_rows["project-a"],
           "home": directory.domain_rows["home"], "domain": directory.domain_rows["a"]}[resource]
    row.enabled = False
    with pytest.raises(HTTPException) as error:
        await resolve_domain_authority("user", "project-a")
    assert error.value.status_code == 403


async def test_project_domain_comes_from_native_project_not_user_home(directory):
    assert (await resolve_domain_authority("user", "project-a"))["domain_id"] == "a"
    assert directory.user.domain_id == "home"


@pytest.mark.parametrize("override", [{"links": {"next": "next-page"}}, {"truncated": True}])
@pytest.mark.parametrize("path", ["/projects", "/role_assignments", "/users/user/groups", "/roles"])
async def test_partial_native_collections_fail_closed(directory, path, override):
    directory.overrides[path] = override
    with pytest.raises(HTTPException) as error:
        await resolve_domain_authority("user", "project-a")
    assert error.value.status_code == 503


async def test_unknown_native_role_id_cannot_grant_management(directory):
    directory.grant_manager(role="unknown-id")
    with pytest.raises(HTTPException) as error:
        await resolve_domain_authority("user", "project-a")
    assert error.value.status_code == 503


async def test_effective_and_raw_default_manager_sources_must_agree(directory):
    directory.grant_manager()
    directory.effective["default-a"] = []
    with pytest.raises(HTTPException) as error:
        await resolve_domain_authority("user", "project-a")
    assert error.value.status_code == 503


async def test_absent_default_project_does_not_invent_domain_manager(directory):
    del directory.project_rows["default-a"]
    assert (await resolve_domain_authority("user", "project-a"))["can_manage"] is False


async def test_duplicate_case_alias_default_project_is_unavailable(directory):
    directory.project_rows["alias"] = SimpleNamespace(
        id="alias", domain_id="a", name=" default ", enabled=True, parent_id=None,
    )
    with pytest.raises(HTTPException) as error:
        await resolve_domain_authority("user", "project-a")
    assert error.value.status_code == 503


async def test_default_project_domain_mismatch_is_not_management(directory):
    directory.project_rows["default-a"].domain_id = "b"
    directory.grant_manager()
    assert (await resolve_domain_authority("user", "project-a"))["can_manage"] is False


async def test_deleted_target_domain_denies_system_admin(directory):
    directory.assignments.append(assignment("admin-id", system=True))
    del directory.domain_rows["b"]
    with pytest.raises(HTTPException) as error:
        await resolve_domain_authority("user", "project-a", "b")
    assert error.value.status_code == 403
