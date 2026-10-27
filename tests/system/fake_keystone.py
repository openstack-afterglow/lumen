"""Standalone Keystone v3 boundary for the isolated system-test stack.

This is a test service, never an authentication fallback in Lumen. It exposes
real token/catalog/assignment/inference HTTP contracts to the installed SDK.
Production credentials and caller-supplied role headers are never accepted.

PROJECT, SYSTEM and UNSCOPED tokens are distinct issued snapshots; selector-free
exchange preserves scope and token ID, not current grants. The projectless admin
has no project assignments; the separate password directory reader retains its
home-project grants. Revocation removes both admins' current system grants only.
"""
from __future__ import annotations

import hmac
import json
import os
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

PROJECT_ID = "system-project"
DIRECTORY_PROJECT_ID = "synthetic-directory-project"
OWNER_ID = "system-owner"
DIRECTORY_ID = "synthetic-directory"
DIRECTORY_PASSWORD = "synthetic-directory-password"
DIRECTORY_TOKEN = "synthetic-directory-token"
PROJECTLESS_ADMIN_ID = "synthetic-projectless-admin"
PROJECTLESS_SYSTEM_TOKEN = "synthetic-projectless-system-token"
PROJECTLESS_UNSCOPED_TOKEN = "synthetic-projectless-unscoped-token"
OWNER_TOKEN = "synthetic-owner-token"
OWNER_UNSCOPED_TOKEN = "synthetic-owner-unscoped-token"
CONTROL_TOKEN = "synthetic-directory-control"
SYSTEM_GROUP_ID = "synthetic-system-admins"
SYSTEM_PARENT = "synthetic-system-admin"
READER_LEAVES = {"lumen-inventory_reader", "lumen-history_reader"}
USER_LEAVES = {"lumen-chat_user", "lumen-images_user", "lumen-audio_user", "lumen-tools_user"}
EDITOR_LEAVES = {"lumen-assets_editor", "lumen-agents_editor", "lumen-mcp_editor", "lumen-keys_editor", "lumen-history_editor"}
ADMIN_LEAVES = {"lumen-resources_admin"}
_TOKEN_SCOPES = {
    DIRECTORY_TOKEN: (DIRECTORY_ID, "project"),
    PROJECTLESS_SYSTEM_TOKEN: (PROJECTLESS_ADMIN_ID, "system"),
    PROJECTLESS_UNSCOPED_TOKEN: (PROJECTLESS_ADMIN_ID, "unscoped"),
    OWNER_TOKEN: (OWNER_ID, "project"),
    OWNER_UNSCOPED_TOKEN: (OWNER_ID, "unscoped"),
}


def role_id(name: str) -> str:
    return "role-" + name


class DirectoryState:
    def __init__(self):
        self.lock = threading.RLock()
        self.reset()

    def reset(self):
        with self.lock:
            names = (READER_LEAVES | USER_LEAVES | EDITOR_LEAVES | ADMIN_LEAVES
                     | {"admin", "manager", "member", "reader", "project_admin", "project_member", "project_reader",
                        "lumen_admin", "lumen_editor", "lumen_user", "lumen_reader", SYSTEM_PARENT})
            self.roles = {role_id(name): {"id": role_id(name), "name": name, "domain_id": None}
                          for name in sorted(names)}
            implications = {
                "project_member": {"member", "project_reader"}, "project_reader": {"reader"},
                "lumen_admin": {"lumen_editor", *ADMIN_LEAVES},
                "lumen_editor": {"lumen_user", *EDITOR_LEAVES},
                "lumen_user": {"lumen_reader", *USER_LEAVES}, "lumen_reader": READER_LEAVES,
                **{leaf: {"lumen_reader"} for leaf in USER_LEAVES | EDITOR_LEAVES | ADMIN_LEAVES},
                SYSTEM_PARENT: {"admin"},
            }
            self.graph = {rid: set() for rid in self.roles}
            for prior, children in implications.items():
                self.graph[role_id(prior)] = {role_id(child) for child in children}
            self.owner_roles = {role_id("project_member"), role_id("lumen_admin")}
            self.directory_target_roles = set()
            self.directory_system_admin = True
            self.revoke_system_admin_after_checks = None
            self.authority_unavailable = False
            self.unavailable = False
            self.owner_enabled = True
            self.project_enabled = True
            self.project_exists = True
            self.system_admin_mode = "direct"
            self.system_group_members = {DIRECTORY_ID, PROJECTLESS_ADMIN_ID}
            self.system_revocation = "grant"
            self.user_domain_id = "default"
            self.project_domain_id = "default"
            self.domains = {"default": {"id": "default", "name": "Default", "enabled": True}}
            self.user_overrides = {}
            self.project_overrides = {}
            self.token_overrides = {}
            self.collection_overrides = []
            self.requests = []
            # Issued role IDs and metadata are snapshots, not live assignments.
            now = datetime.now(UTC)
            self.issued_at = now.isoformat()
            self.expires_at = (now + timedelta(hours=1)).isoformat()
            self.issued_owner_roles = self.closure(self.owner_roles)

    def closure(self, roots: set[str]) -> set[str]:
        reached, pending = set(), list(roots)
        while pending:
            rid = pending.pop()
            if rid not in reached:
                reached.add(rid)
                pending.extend(self.graph[rid])
        return reached

    def configure(self, payload: dict):
        with self.lock:
            if set(payload) - {"owner_roles", "directory_target_roles", "directory_system_admin",
                               "revoke_system_admin_after_checks", "remove_edges", "unavailable",
                               "authority_unavailable", "owner_enabled", "project_enabled", "project_exists",
                               "system_admin_mode", "system_group_members", "system_revocation", "domains",
                               "user_domain_id", "project_domain_id", "user_overrides", "project_overrides",
                               "token_overrides", "collection_overrides"}:
                raise ValueError("Unknown synthetic directory control field")
            owner_roles = self.owner_roles
            if "owner_roles" in payload:
                names = payload["owner_roles"]
                if not isinstance(names, list) or any(not isinstance(name, str) or role_id(name) not in self.roles for name in names):
                    raise ValueError("Unknown synthetic owner role")
                owner_roles = {role_id(name) for name in names}
            directory_target_roles = self.directory_target_roles
            if "directory_target_roles" in payload:
                names = payload["directory_target_roles"]
                if not isinstance(names, list) or any(not isinstance(name, str) or role_id(name) not in self.roles for name in names):
                    raise ValueError("Unknown synthetic directory target role")
                directory_target_roles = {role_id(name) for name in names}
            directory_system_admin = payload.get("directory_system_admin", self.directory_system_admin)
            authority_unavailable = payload.get("authority_unavailable", self.authority_unavailable)
            project_exists = payload.get("project_exists", self.project_exists)
            if any(not isinstance(value, bool) for value in (directory_system_admin, authority_unavailable, project_exists)):
                raise ValueError("Synthetic directory authority/project status must be a boolean")
            revoke_after = payload.get("revoke_system_admin_after_checks", self.revoke_system_admin_after_checks)
            if revoke_after is not None and (type(revoke_after) is not int or revoke_after < 1):
                raise ValueError("System revocation check count must be a positive integer or null")
            removals = payload.get("remove_edges", [])
            if not isinstance(removals, list):
                raise ValueError("remove_edges must be a list")
            parsed = []
            for edge in removals:
                if not isinstance(edge, dict) or set(edge) != {"prior", "implied"}:
                    raise ValueError("Invalid synthetic role edge")
                if not isinstance(edge["prior"], str) or not isinstance(edge["implied"], str):
                    raise ValueError("Invalid synthetic role name")
                prior, implied = role_id(edge["prior"]), role_id(edge["implied"])
                if prior not in self.graph or implied not in self.roles:
                    raise ValueError("Unknown synthetic role edge")
                parsed.append((prior, implied))
            unavailable = payload.get("unavailable", self.unavailable)
            if not isinstance(unavailable, bool):
                raise ValueError("unavailable must be a boolean")
            owner_enabled = payload.get("owner_enabled", self.owner_enabled)
            project_enabled = payload.get("project_enabled", self.project_enabled)
            if not isinstance(owner_enabled, bool) or not isinstance(project_enabled, bool):
                raise ValueError("Synthetic owner/project enabled status must be a boolean")
            mode = payload.get("system_admin_mode", self.system_admin_mode)
            if mode not in {"direct", "group", "inferred", "group-inferred"}:
                raise ValueError("Unknown synthetic system grant mode")
            revocation = payload.get("system_revocation", self.system_revocation)
            if revocation not in {"grant", "group", "edge"}:
                raise ValueError("Unknown synthetic system revocation")
            members = payload.get("system_group_members", list(self.system_group_members))
            if not isinstance(members, list) or any(member not in {OWNER_ID, DIRECTORY_ID, PROJECTLESS_ADMIN_ID} for member in members):
                raise ValueError("Unknown synthetic system group member")
            overrides = payload.get("collection_overrides", self.collection_overrides)
            if not isinstance(overrides, list):
                raise ValueError("collection_overrides must be a list")
            for override in overrides:
                if (not isinstance(override, dict) or set(override) != {"path", "query", "payload"}
                        or not isinstance(override["path"], str) or not override["path"].startswith("/v3/")
                        or not isinstance(override["query"], dict)
                        or any(not isinstance(key, str) or not isinstance(value, str)
                               for key, value in override["query"].items())):
                    raise ValueError("Invalid synthetic collection envelope override")
            self.collection_overrides = overrides
            for field in ("domains", "user_overrides", "project_overrides", "token_overrides"):
                if field in payload and not isinstance(payload[field], dict):
                    raise ValueError(field + " must be an object")
            for field in ("user_domain_id", "project_domain_id"):
                if field in payload and not isinstance(payload[field], str):
                    raise ValueError(field + " must be a string")
            self.system_admin_mode = mode
            self.system_revocation = revocation
            self.system_group_members = set(members)
            for field in ("domains", "user_domain_id", "project_domain_id", "user_overrides", "project_overrides", "token_overrides"):
                if field in payload:
                    setattr(self, field, payload[field])
            self.owner_roles = owner_roles
            self.directory_target_roles = directory_target_roles
            self.directory_system_admin = directory_system_admin
            self.revoke_system_admin_after_checks = revoke_after
            self.authority_unavailable = authority_unavailable
            self.project_exists = project_exists
            for prior, implied in parsed:
                self.graph[prior].discard(implied)
            self.unavailable = unavailable
            self.owner_enabled = owner_enabled
            self.project_enabled = project_enabled

    def assignments(self, query: dict[str, list[str]]) -> list[dict]:
        user = query.get("user.id", [None])[0]
        group = query.get("group.id", [None])[0]
        project = query.get("scope.project.id", [None])[0]
        system = query.get("scope.system", [None])[0]
        selected_role = query.get("role.id", [None])[0]
        effective = query.get("effective", ["false"])[0].lower() in {"true", "1"}
        rows = [
            *({"user": {"id": OWNER_ID}, "role": {"id": rid}, "scope": {"project": {"id": PROJECT_ID}}}
              for rid in sorted(self.owner_roles)),
            *({"user": {"id": DIRECTORY_ID}, "role": {"id": rid}, "scope": {"project": {"id": PROJECT_ID}}}
              for rid in sorted(self.directory_target_roles)),
            {"user": {"id": DIRECTORY_ID}, "role": {"id": role_id("admin")}, "scope": {"project": {"id": DIRECTORY_PROJECT_ID}}},
            {"user": {"id": DIRECTORY_ID}, "role": {"id": role_id("member")}, "scope": {"project": {"id": DIRECTORY_PROJECT_ID}}},
        ]
        group_mode = self.system_admin_mode in {"group", "group-inferred"}
        if self.directory_system_admin and not effective:
            rid = role_id(SYSTEM_PARENT if self.system_admin_mode in {"inferred", "group-inferred"} else "admin")
            subjects = [("group", SYSTEM_GROUP_ID)] if group_mode else [
                ("user", subject) for subject in (DIRECTORY_ID, PROJECTLESS_ADMIN_ID)
            ]
            rows.extend({kind: {"id": subject}, "role": {"id": rid}, "scope": {"system": {"all": True}}}
                        for kind, subject in subjects)
        if effective:
            rows = [{**row, "role": {"id": rid}}
                    for row in rows for rid in sorted(self.closure({row["role"]["id"]}))]
        # Keystone's effective expansion intentionally excludes system grants.
        selected = [row for row in rows
                    if (user is None or row.get("user", {}).get("id") == user)
                    and (group is None or row.get("group", {}).get("id") == group)
                    and (project is None or row["scope"].get("project", {}).get("id") == project)
                    and (system is None or (system == "all" and row["scope"].get("system", {}).get("all") is True))
                    and (selected_role is None or row["role"]["id"] == selected_role)]
        # Mutate after returning this observation. Group grants count only once
        # their current membership and raw group assignment have been observed.
        observed_system = system == "all" and not effective and (
            group == SYSTEM_GROUP_ID if group_mode else user in {DIRECTORY_ID, PROJECTLESS_ADMIN_ID}
        )
        if observed_system and self.revoke_system_admin_after_checks is not None:
            self.revoke_system_admin_after_checks -= 1
            if self.revoke_system_admin_after_checks == 0:
                if self.system_revocation == "group":
                    self.system_group_members.clear()
                elif self.system_revocation == "edge":
                    self.graph[role_id(SYSTEM_PARENT)].discard(role_id("admin"))
                else:
                    self.directory_system_admin = False
                self.revoke_system_admin_after_checks = None
        return selected

    def token_document(self, subject: str, base_url: str, *, scope: str = "project", token_id: str | None = None) -> dict:
        roots = self.issued_owner_roles if subject == OWNER_ID else {role_id("admin"), role_id("member")}
        token = {
            "methods": ["password" if subject == DIRECTORY_ID else "token"],
            "issued_at": self.issued_at, "expires_at": self.expires_at,
            "audit_ids": ["synthetic-audit"],
            "user": {"id": subject, "name": subject, "domain": {"id": "default", "name": "Default"}},
            "roles": [{"id": rid, "name": self.roles[rid]["name"]} for rid in sorted(roots)],
            "catalog": [{"id": "synthetic-identity", "name": "keystone", "type": "identity",
                         "endpoints": [{"id": "identity-" + interface, "interface": interface,
                                        "region": "RegionOne", "region_id": "RegionOne", "url": base_url + "/v3"}
                                       for interface in ("public", "internal", "admin")]}],
        }
        if scope == "system":
            token["system"] = {"all": True}
        elif scope == "project":
            project_id = PROJECT_ID if subject == OWNER_ID else DIRECTORY_PROJECT_ID
            token["project"] = {"id": project_id, "name": project_id, "domain": {"id": "default", "name": "Default"}}
        if token_id is not None:
            token.update(self.token_overrides.get(token_id, {}))
        return {"token": token}


class KeystoneServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address):
        self.state = DirectoryState()
        super().__init__(address, KeystoneHandler)


class KeystoneHandler(BaseHTTPRequestHandler):
    server: KeystoneServer

    def log_message(self, format, *args):
        # Test requests can contain credential bodies/headers. Do not log them.
        return

    @property
    def base_url(self):
        return "http://" + self.headers.get("Host", "fake-keystone:5000")

    def respond(self, status: int, payload: object, *, token: str | None = None):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if token is not None:
            self.send_header("X-Subject-Token", token)
        self.end_headers()
        self.wfile.write(body)

    def authorized(self, header: str, expected: str) -> bool:
        return hmac.compare_digest(self.headers.get(header, ""), expected)

    def body(self) -> dict:
        size = int(self.headers.get("Content-Length", "0"))
        if size < 0 or size > 1_048_576:
            raise ValueError("Invalid synthetic request size")
        payload = json.loads(self.rfile.read(size))
        if not isinstance(payload, dict):
            raise ValueError("JSON object required")
        return payload

    def do_GET(self):
        parsed = urlsplit(self.path)
        path = parsed.path.rstrip("/") or "/"
        state = self.server.state
        if path == "/health":
            self.respond(200, {"status": "ok"})
            return
        if path in {"/", "/v3"}:
            version = {"id": "v3.0", "status": "stable", "updated": "2026-10-07T00:00:00Z",
                       "links": [{"rel": "self", "href": self.base_url + "/v3/"}],
                       "media-types": [{"base": "application/json", "type": "application/vnd.openstack.identity-v3+json"}]}
            self.respond(200, {"version": version} if path == "/v3" else {"versions": {"values": [version]}})
            return
        if not self.authorized("X-Auth-Token", DIRECTORY_TOKEN):
            self.respond(401, {"error": {"code": 401, "message": "Synthetic directory credential required"}})
            return
        with state.lock:
            state.requests.append(("GET", path, parse_qs(parsed.query, keep_blank_values=True)))
            query = parse_qs(parsed.query, keep_blank_values=True)
            overrides = [override for override in state.collection_overrides
                         if override["path"] == path and all(query.get(key) == [value]
                             for key, value in override["query"].items())]
            if state.unavailable or (state.authority_unavailable and path != "/v3/auth/tokens"):
                self.respond(503, {"error": {"code": 503, "message": "Synthetic directory unavailable"}})
            elif overrides:
                self.respond(200, overrides[0]["payload"])
            elif path == "/v3/roles":
                self.respond(200, {"roles": list(state.roles.values()), "links": {"next": None, "previous": None}})
            elif path == "/v3/role_inferences":
                self.respond(200, {"role_inferences": [{"prior_role": {"id": prior},
                             "implies": [{"id": child} for child in sorted(children)]}
                             for prior, children in sorted(state.graph.items()) if children],
                                   "links": {"next": None, "previous": None}})
            elif path == "/v3/role_assignments":
                self.respond(200, {"role_assignments": state.assignments(parse_qs(parsed.query)),
                                   "links": {"next": None, "previous": None}})
            elif path == "/v3/auth/tokens":
                token = self.headers.get("X-Subject-Token", "")
                subject, scope = _TOKEN_SCOPES.get(token, (None, None))
                if subject is not None:
                    document = state.token_document(subject, self.base_url, scope=scope, token_id=token)
                    if "nocatalog" in parse_qs(parsed.query, keep_blank_values=True):
                        document["token"].pop("catalog", None)
                    self.respond(200, document, token=token)
                else:
                    self.respond(404, {"error": {"code": 404, "message": "Unknown synthetic token"}})
            elif path in {"/v3/users/" + subject for subject in (OWNER_ID, DIRECTORY_ID, PROJECTLESS_ADMIN_ID)}:
                subject = path.rsplit("/", 1)[-1]
                self.respond(200, {"user": {"id": subject, "name": subject, "domain_id": state.user_domain_id,
                                           "enabled": state.owner_enabled if subject == OWNER_ID else True,
                                           **state.user_overrides}})
            elif path in {"/v3/projects/" + PROJECT_ID, "/v3/projects/" + DIRECTORY_PROJECT_ID}:
                project = path.rsplit("/", 1)[-1]
                if project == PROJECT_ID and not state.project_exists:
                    self.respond(404, {"error": {"code": 404, "message": "Unknown synthetic project"}})
                    return
                self.respond(200, {"project": {"id": project, "name": project, "domain_id": state.project_domain_id,
                                              "enabled": state.project_enabled if project == PROJECT_ID else True,
                                              **state.project_overrides}})
            elif path.startswith("/v3/domains/"):
                domain = state.domains.get(path.rsplit("/", 1)[-1])
                if domain is None:
                    self.respond(404, {"error": {"code": 404, "message": "Unknown synthetic domain"}})
                else:
                    self.respond(200, {"domain": domain})
            elif path.startswith("/v3/users/") and path.endswith("/groups"):
                subject = path.split("/")[-2]
                groups = [{"id": SYSTEM_GROUP_ID, "name": SYSTEM_GROUP_ID, "domain_id": "default"}]
                self.respond(200, {"groups": groups if subject in state.system_group_members else [],
                                   "links": {"next": None, "previous": None}})
            else:
                self.respond(404, {"error": {"code": 404, "message": "Unknown synthetic identity endpoint"}})

    def do_POST(self):
        path = urlsplit(self.path).path.rstrip("/")
        state = self.server.state
        try:
            payload = self.body()
            if path in {"/_control/configure", "/_control/reset"}:
                if not self.authorized("X-Test-Control-Token", CONTROL_TOKEN):
                    self.respond(403, {"error": {"code": 403, "message": "Synthetic control credential required"}})
                    return
                if path.endswith("reset"):
                    state.reset()
                else:
                    state.configure(payload)
                self.respond(200, {"configured": True})
                return
            if path != "/v3/auth/tokens":
                self.respond(404, {"error": {"code": 404, "message": "Unknown synthetic identity endpoint"}})
                return
            with state.lock:
                if state.unavailable:
                    self.respond(503, {"error": {"code": 503, "message": "Synthetic directory unavailable"}})
                    return
                authentication = payload.get("auth", {})
                identity = authentication.get("identity", {})
                methods = identity.get("methods", [])
                state.requests.append(("POST", path, {
                    "methods": list(methods), "scope": authentication.get("scope", {}),
                }))
                subject = None
                source_scope = "project"
                source_token = None
                if methods == ["password"]:
                    user = identity.get("password", {}).get("user", {})
                    if (user.get("name") == DIRECTORY_ID
                            and hmac.compare_digest(str(user.get("password", "")), DIRECTORY_PASSWORD)):
                        subject = DIRECTORY_ID
                elif methods == ["token"]:
                    source_token = identity.get("token", {}).get("id")
                    subject, source_scope = _TOKEN_SCOPES.get(source_token, (None, None))
                scope = authentication.get("scope", {})
                system = scope.get("system", {}).get("all") is True
                project = scope.get("project", {})
                expected_project = {DIRECTORY_ID: DIRECTORY_PROJECT_ID, OWNER_ID: PROJECT_ID}.get(subject)
                project_matches = expected_project is not None and project.get("id", project.get("name")) == expected_project
                preserve_scope = methods == ["token"] and not scope
                if subject is None or not (preserve_scope or project_matches or (
                        system and subject in {DIRECTORY_ID, PROJECTLESS_ADMIN_ID} and state.directory_system_admin)):
                    self.respond(401, {"error": {"code": 401, "message": "Invalid synthetic credential or scope"}})
                    return
                issued_scope = source_scope if preserve_scope else "system" if system else "project"
                if preserve_scope:
                    token = source_token
                elif subject == PROJECTLESS_ADMIN_ID:
                    token = PROJECTLESS_SYSTEM_TOKEN
                else:
                    token = DIRECTORY_TOKEN if subject == DIRECTORY_ID else OWNER_TOKEN
                self.respond(201, state.token_document(subject, self.base_url, scope=issued_scope,
                             token_id=source_token if preserve_scope else None), token=token)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            self.respond(400, {"error": {"code": 400, "message": str(exc)}})


if __name__ == "__main__":
    KeystoneServer(("0.0.0.0", int(os.environ.get("PORT", "5000")))).serve_forever()
