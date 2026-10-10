"""Standalone Keystone v3 boundary for the isolated system-test stack.

This is a test service, never an authentication fallback in Lumen. It exposes
real token/catalog/assignment/inference HTTP contracts to the installed SDK.
Production credentials and caller-supplied role headers are never accepted.
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
OWNER_TOKEN = "synthetic-owner-token"
CONTROL_TOKEN = "synthetic-directory-control"
READER_LEAVES = {"lumen-inventory_reader", "lumen-history_reader"}
USER_LEAVES = {"lumen-chat_user", "lumen-images_user", "lumen-audio_user", "lumen-tools_user"}
EDITOR_LEAVES = {"lumen-assets_editor", "lumen-agents_editor", "lumen-mcp_editor", "lumen-keys_editor", "lumen-history_editor"}
ADMIN_LEAVES = {"lumen-resources_admin"}


def role_id(name: str) -> str:
    return "role-" + name


class DirectoryState:
    def __init__(self):
        self.lock = threading.RLock()
        self.reset()

    def reset(self):
        with self.lock:
            names = (READER_LEAVES | USER_LEAVES | EDITOR_LEAVES | ADMIN_LEAVES
                     | {"admin", "manager", "member", "reader", "project_member", "project_reader",
                        "lumen_admin", "lumen_editor", "lumen_user", "lumen_reader"})
            self.roles = {role_id(name): {"id": role_id(name), "name": name, "domain_id": None}
                          for name in sorted(names)}
            implications = {
                "project_member": {"member", "project_reader"}, "project_reader": {"reader"},
                "lumen_admin": {"lumen_editor", *ADMIN_LEAVES},
                "lumen_editor": {"lumen_user", *EDITOR_LEAVES},
                "lumen_user": {"lumen_reader", *USER_LEAVES}, "lumen_reader": READER_LEAVES,
                **{leaf: {"lumen_reader"} for leaf in USER_LEAVES | EDITOR_LEAVES | ADMIN_LEAVES},
            }
            self.graph = {rid: set() for rid in self.roles}
            for prior, children in implications.items():
                self.graph[role_id(prior)] = {role_id(child) for child in children}
            self.owner_roles = {role_id("project_member"), role_id("lumen_admin")}
            self.unavailable = False
            self.owner_enabled = True
            self.project_enabled = True

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
            if set(payload) - {"owner_roles", "remove_edges", "unavailable", "owner_enabled", "project_enabled"}:
                raise ValueError("Unknown synthetic directory control field")
            owner_roles = self.owner_roles
            if "owner_roles" in payload:
                names = payload["owner_roles"]
                if not isinstance(names, list) or any(not isinstance(name, str) or role_id(name) not in self.roles for name in names):
                    raise ValueError("Unknown synthetic owner role")
                owner_roles = {role_id(name) for name in names}
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
            self.owner_roles = owner_roles
            for prior, implied in parsed:
                self.graph[prior].discard(implied)
            self.unavailable = unavailable
            self.owner_enabled = owner_enabled
            self.project_enabled = project_enabled

    def assignments(self, query: dict[str, list[str]]) -> list[dict]:
        user = query.get("user.id", [None])[0]
        project = query.get("scope.project.id", [None])[0]
        system = query.get("scope.system", [None])[0]
        selected_role = query.get("role.id", [None])[0]
        # Keystone effective expansion emits project/domain rows, not direct system grants.
        effective = query.get("effective", ["false"])[0].lower() == "true"
        rows = [
            *({"user": {"id": OWNER_ID}, "role": {"id": rid}, "scope": {"project": {"id": PROJECT_ID}}}
              for rid in sorted(self.owner_roles)),
            {"user": {"id": DIRECTORY_ID}, "role": {"id": role_id("admin")}, "scope": {"system": {"all": True}}},
            {"user": {"id": DIRECTORY_ID}, "role": {"id": role_id("admin")}, "scope": {"project": {"id": DIRECTORY_PROJECT_ID}}},
            {"user": {"id": DIRECTORY_ID}, "role": {"id": role_id("member")}, "scope": {"project": {"id": DIRECTORY_PROJECT_ID}}},
        ]
        return [row for row in rows
                if (user is None or row["user"]["id"] == user)
                and (project is None or row["scope"].get("project", {}).get("id") == project)
                and (system is None or (system == "all" and row["scope"].get("system", {}).get("all") is True))
                and not (effective and "system" in row["scope"])
                and (selected_role is None or row["role"]["id"] == selected_role)]

    def token_document(self, subject: str, base_url: str, *, system: bool = False) -> dict:
        roots = self.owner_roles if subject == OWNER_ID else {role_id("admin"), role_id("member")}
        now = datetime.now(UTC)
        token = {
            "methods": ["password" if subject == DIRECTORY_ID else "token"],
            "issued_at": now.isoformat(), "expires_at": (now + timedelta(hours=1)).isoformat(),
            "audit_ids": ["synthetic-audit"],
            "user": {"id": subject, "name": subject, "domain": {"id": "default", "name": "Default"}},
            "roles": [{"id": rid, "name": self.roles[rid]["name"]} for rid in sorted(self.closure(roots))],
            "catalog": [{"id": "synthetic-identity", "name": "keystone", "type": "identity",
                         "endpoints": [{"id": "identity-" + interface, "interface": interface,
                                        "region": "RegionOne", "region_id": "RegionOne", "url": base_url + "/v3"}
                                       for interface in ("public", "internal", "admin")]}],
        }
        if system:
            token["system"] = {"all": True}
        else:
            project_id = PROJECT_ID if subject == OWNER_ID else DIRECTORY_PROJECT_ID
            token["project"] = {"id": project_id, "name": project_id, "domain": {"id": "default", "name": "Default"}}
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

    def respond(self, status: int, payload: dict, *, token: str | None = None):
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
            if state.unavailable:
                self.respond(503, {"error": {"code": 503, "message": "Synthetic directory unavailable"}})
            elif path == "/v3/roles":
                self.respond(200, {"roles": list(state.roles.values()), "links": {"next": None, "previous": None}})
            elif path == "/v3/role_inferences":
                self.respond(200, {"role_inferences": [{"prior_role": {"id": prior},
                             "implies": [{"id": child} for child in sorted(children)]}
                             for prior, children in sorted(state.graph.items()) if children]})
            elif path == "/v3/role_assignments":
                self.respond(200, {"role_assignments": state.assignments(parse_qs(parsed.query)),
                                   "links": {"next": None, "previous": None}})
            elif path == "/v3/auth/tokens":
                token = self.headers.get("X-Subject-Token", "")
                if token == DIRECTORY_TOKEN or (token == OWNER_TOKEN and state.owner_roles and state.owner_enabled and state.project_enabled):
                    subject = DIRECTORY_ID if token == DIRECTORY_TOKEN else OWNER_ID
                    self.respond(200, state.token_document(subject, self.base_url), token=token)
                else:
                    self.respond(404, {"error": {"code": 404, "message": "Unknown synthetic token"}})
            elif path in {"/v3/users/" + OWNER_ID, "/v3/users/" + DIRECTORY_ID}:
                subject = path.rsplit("/", 1)[-1]
                self.respond(200, {"user": {"id": subject, "name": subject, "domain_id": "default",
                                           "enabled": state.owner_enabled if subject == OWNER_ID else True}})
            elif path in {"/v3/projects/" + PROJECT_ID, "/v3/projects/" + DIRECTORY_PROJECT_ID}:
                project = path.rsplit("/", 1)[-1]
                self.respond(200, {"project": {"id": project, "name": project, "domain_id": "default",
                                              "enabled": state.project_enabled if project == PROJECT_ID else True}})
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
                subject = None
                if methods == ["password"]:
                    user = identity.get("password", {}).get("user", {})
                    if (user.get("name") == DIRECTORY_ID
                            and hmac.compare_digest(str(user.get("password", "")), DIRECTORY_PASSWORD)):
                        subject = DIRECTORY_ID
                elif methods == ["token"]:
                    token = identity.get("token", {}).get("id")
                    if token == DIRECTORY_TOKEN:
                        subject = DIRECTORY_ID
                    elif token == OWNER_TOKEN and state.owner_roles and state.owner_enabled and state.project_enabled:
                        subject = OWNER_ID
                scope = authentication.get("scope", {})
                system = scope.get("system", {}).get("all") is True
                project = scope.get("project", {})
                expected_project = DIRECTORY_PROJECT_ID if subject == DIRECTORY_ID else PROJECT_ID
                project_matches = project.get("id", project.get("name")) == expected_project
                if methods == ["token"] and not scope:
                    project_matches = True  # Both known fixture tokens already have a fixed project scope.
                if subject is None or not (project_matches or (system and subject == DIRECTORY_ID)):
                    self.respond(401, {"error": {"code": 401, "message": "Invalid synthetic credential or scope"}})
                    return
                token = DIRECTORY_TOKEN if subject == DIRECTORY_ID else OWNER_TOKEN
                self.respond(201, state.token_document(subject, self.base_url, system=system), token=token)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            self.respond(400, {"error": {"code": 400, "message": str(exc)}})


if __name__ == "__main__":
    KeystoneServer(("0.0.0.0", int(os.environ.get("PORT", "5000")))).serve_forever()
