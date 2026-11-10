"""MCP connections: several per template, registry transports, and catalog refresh.

Properties:
- one template yields as many connections as there are distinct labels, each with its own credentials;
- an existing ``(template, label)`` pair is returned unchanged, never rewritten;
- a pre-template row is recognised by URL only for an unlabeled request;
- SSE and streamable HTTP are catalogued; stdio packages are not;
- a failed discovery keeps the last good tool catalog and records only the error;
- an OAuth connection is discovered only with the requesting user's token.
"""

import pytest

from lumen.services import extensions_store as es
from lumen.services import mcp_bundles, mcp_connections
from lumen.services import mcp_registry_catalog as registry
from lumen.services import mcp_registry_model as model

ADMIN_CONNECTIONS = "/api/v1/chat/admin/mcp-connections"


class FakeStore:
    def __init__(self, rows=()):
        self.rows = [dict(row) for row in rows]
        self.created = []
        self.catalog_writes = []
        self.next_id = 100

    async def list_global(self, kind):
        return [dict(row) for row in self.rows]

    async def create(self, kind, fields, *, scope):
        assert (kind, scope) == ("mcp", "global")
        self.next_id += 1
        row = {"id": self.next_id, "template_key": None, "connection_label": None, "is_active": True, **fields}
        self.rows.append(row)
        self.created.append(fields)
        return dict(row)

    async def record_catalog(self, server_id, *, tools, error):
        self.catalog_writes.append((server_id, tools, error))
        row = next((row for row in self.rows if row["id"] == server_id), {"id": server_id})
        previous = row.get("tools", [])
        if tools is not None:
            row["tools"] = tools
        return previous


@pytest.fixture
def store(monkeypatch):
    fake = FakeStore()
    monkeypatch.setattr(es, "list_global", fake.list_global)
    monkeypatch.setattr(es, "create", fake.create)
    monkeypatch.setattr(es, "record_catalog", fake.record_catalog)
    return fake


def _entry(entry_id="tmpl", access="open", url="https://svc.example.com/mcp", transport="http", variables=()):
    return {
        "id": entry_id,
        "registry_name": f"org/{entry_id}",
        "name": "Service",
        "description": "",
        "url": url,
        "transport": transport,
        "variables": list(variables),
        "header_hints": [],
        "docs_url": "",
        "access": access,
    }


class TestSeveralConnectionsPerTemplate:
    async def test_distinct_labels_create_distinct_connections(self, store):
        first = await mcp_connections.create_connection(
            name="Notion", url="https://mcp.notion.com/mcp", auth_mode="oauth", template_key="notion", label="work"
        )
        second = await mcp_connections.create_connection(
            name="Notion", url="https://mcp.notion.com/mcp", auth_mode="oauth", template_key="notion", label="personal"
        )
        assert first["created"] is True and second["created"] is True
        assert first["server"]["id"] != second["server"]["id"]
        assert [row["connection_label"] for row in store.created] == ["work", "personal"]

    async def test_same_template_and_label_returns_the_existing_row_untouched(self, store):
        await mcp_connections.create_connection(
            name="Notion", url="https://mcp.notion.com/mcp", auth_mode="oauth", template_key="notion", label="work"
        )
        again = await mcp_connections.create_connection(
            name="Notion", url="https://mcp.notion.com/mcp", auth_mode="oauth", template_key="notion", label="work"
        )
        assert again["created"] is False
        assert len(store.created) == 1

    async def test_admin_credentials_are_per_connection(self, store):
        await mcp_connections.create_connection(
            name="Afterglow",
            url="https://afterglow.example.com/mcp",
            auth_mode="admin",
            headers={"Authorization": "Bearer read-only"},
            template_key="afterglow",
            label="readonly",
        )
        await mcp_connections.create_connection(
            name="Afterglow",
            url="https://afterglow.example.com/mcp",
            auth_mode="admin",
            headers={"Authorization": "Bearer admin"},
            template_key="afterglow",
            label="admin",
        )
        assert [fields["headers"]["Authorization"] for fields in store.created] == [
            "Bearer read-only",
            "Bearer admin",
        ]

    async def test_label_defaults_to_the_template_name(self, store):
        result = await mcp_connections.create_connection(
            name="GitHub", url="https://api.githubcopilot.com/mcp/", template_key="github", default_label="GitHub"
        )
        assert result["server"]["connection_label"] == "GitHub"

    @pytest.mark.parametrize("label", ["", "   ", "x" * 101, "bad\nlabel"])
    async def test_invalid_label_is_refused(self, store, label):
        with pytest.raises(mcp_connections.McpConnectionError):
            await mcp_connections.create_connection(
                name="N", url="https://n.example.com/mcp", template_key="t", label=label
            )
        assert store.created == []

    async def test_label_without_template_is_refused(self, store):
        with pytest.raises(mcp_connections.McpConnectionError):
            await mcp_connections.create_connection(name="N", url="https://n.example.com/mcp", label="x")

    async def test_stdio_transport_is_refused(self, store):
        with pytest.raises(mcp_connections.McpConnectionError):
            await mcp_connections.create_connection(name="N", url="https://n.example.com/mcp", transport="stdio")

    async def test_admin_mode_requires_headers_and_headers_require_admin(self, store):
        with pytest.raises(mcp_connections.McpConnectionError):
            await mcp_connections.create_connection(name="N", url="https://n.example.com/mcp", auth_mode="admin")
        with pytest.raises(mcp_connections.McpConnectionError):
            await mcp_connections.create_connection(
                name="N", url="https://n.example.com/mcp", auth_mode="none", headers={"Authorization": "x"}
            )
        assert store.created == []


class TestLegacyRows:
    async def test_unlabeled_request_recognises_a_pre_template_row_by_url(self, store):
        store.rows.append(
            {"id": 7, "template_key": None, "connection_label": None, "url": "https://mcp.notion.com/mcp/"}
        )
        result = await mcp_bundles.install("notion")
        assert result["created"] is False
        assert result["server"]["id"] == 7
        assert store.created == []

    async def test_labeled_request_does_not_adopt_a_pre_template_row(self, store):
        store.rows.append(
            {"id": 7, "template_key": None, "connection_label": None, "url": "https://mcp.notion.com/mcp"}
        )
        result = await mcp_bundles.install("notion", label="second")
        assert result["created"] is True
        assert store.created[0]["connection_label"] == "second"

    def test_find_existing_only_matches_legacy_rows_when_allowed(self):
        rows = [{"id": 1, "template_key": None, "connection_label": None, "url": "https://a.example.com/mcp"}]
        assert (
            mcp_connections.find_existing(
                rows, template_key="t", label=None, url="https://a.example.com/mcp", allow_legacy=True
            )
            is rows[0]
        )
        assert (
            mcp_connections.find_existing(
                rows, template_key="t", label="x", url="https://a.example.com/mcp", allow_legacy=False
            )
            is None
        )


class TestRegistryTransports:
    def test_sse_only_remote_is_catalogued_with_sse_transport(self):
        item = {
            "server": {
                "id": "s",
                "name": "s",
                "remotes": [{"transport_type": "sse", "url": "https://s.example.com/sse"}],
            }
        }
        entry = model.build_entry(item, "open")
        assert entry["transport"] == "sse"

    def test_streamable_http_is_preferred_when_both_are_offered(self):
        item = {
            "server": {
                "id": "b",
                "name": "b",
                "remotes": [
                    {"transport_type": "sse", "url": "https://b.example.com/sse"},
                    {"transport_type": "streamable-http", "url": "https://b.example.com/mcp"},
                ],
            }
        }
        entry = model.build_entry(item, "open")
        assert (entry["transport"], entry["url"]) == ("http", "https://b.example.com/mcp")

    def test_local_package_is_not_catalogued(self):
        item = {"server": {"id": "p", "name": "p", "remotes": [], "packages": [{"registry_name": "npm"}]}}
        assert model.build_entry(item, "open") is None

    def test_probe_targets_carry_the_transport_for_each_url(self):
        items = [
            {
                "server": {
                    "id": "a",
                    "name": "a",
                    "remotes": [{"transport_type": "sse", "url": "https://a.example.com/sse"}],
                }
            },
            {
                "server": {
                    "id": "b",
                    "name": "b",
                    "remotes": [{"transport_type": "streamable-http", "url": "https://b.example.com/mcp"}],
                }
            },
        ]
        assert model.probe_targets(items) == {
            "https://a.example.com/sse": "sse",
            "https://b.example.com/mcp": "http",
        }


class TestRegistryInstall:
    async def test_registry_connection_is_created_from_the_template(self, monkeypatch, store):
        monkeypatch.setattr(registry, "_load", lambda: (_entry(),))
        result = await registry.install("tmpl", label="team-a")
        assert result["created"] is True
        created = store.created[0]
        assert created["template_key"] == "tmpl"
        assert created["connection_label"] == "team-a"
        assert created["transport"] == "http"

    async def test_registry_install_passes_the_transport_of_the_entry(self, monkeypatch, store):
        monkeypatch.setattr(registry, "_load", lambda: (_entry(transport="sse", url="https://s.example.com/sse"),))
        await registry.install("tmpl")
        assert store.created[0]["transport"] == "sse"

    async def test_unreachable_entry_is_never_installed(self, monkeypatch, store):
        monkeypatch.setattr(registry, "_load", lambda: (_entry(access="unreachable"),))
        with pytest.raises(model.McpRegistryError):
            await registry.install("tmpl")
        assert store.created == []

    async def test_bulk_install_reports_rejected_rows_and_continues(self, monkeypatch, store):
        entries = (_entry("a", url="https://a.example.com/mcp"), _entry("b", url="https://b.example.com/mcp"))
        monkeypatch.setattr(registry, "_load", lambda: entries)

        original = es.create

        async def reject_a(kind, fields, *, scope):
            if fields["url"].startswith("https://a."):
                raise es.ExtensionValidationError("url 은 공개 HTTPS 주소여야 합니다")
            return await original(kind, fields, scope=scope)

        monkeypatch.setattr(es, "create", reject_a)
        result = await registry.install_all()
        assert result["created"] == ["b"]
        assert result["failed"][0]["id"] == "a"

    async def test_repeat_bulk_install_creates_nothing(self, monkeypatch, store):
        monkeypatch.setattr(registry, "_load", lambda: (_entry(),))
        await registry.install_all()
        second = await registry.install_all()
        assert second["created"] == []
        assert second["existing"] == ["tmpl"]
        assert len(store.created) == 1


class TestCatalog:
    def test_catalog_is_sorted_and_digests_track_schema_changes(self):
        tools = [
            {"name": "b", "description": "B", "input_schema": {"type": "object"}},
            {"name": "a", "description": "A", "input_schema": {"type": "object", "properties": {"q": {}}}},
        ]
        catalog = mcp_connections.catalog_from_tools(tools)
        assert [item["name"] for item in catalog] == ["a", "b"]
        changed = mcp_connections.catalog_from_tools(
            [{**tools[0], "input_schema": {"type": "object", "required": ["x"]}}, tools[1]]
        )
        assert changed[1]["schema_sha256"] != catalog[1]["schema_sha256"]

    def test_diff_reports_added_removed_and_changed_tools(self):
        before = [
            {"name": "keep", "schema_sha256": "1"},
            {"name": "gone", "schema_sha256": "2"},
            {"name": "edit", "schema_sha256": "3"},
        ]
        after = [
            {"name": "keep", "schema_sha256": "1"},
            {"name": "edit", "schema_sha256": "9"},
            {"name": "new", "schema_sha256": "4"},
        ]
        assert mcp_connections.diff_catalog(before, after) == {
            "added": ["new"],
            "removed": ["gone"],
            "changed": ["edit"],
        }

    async def test_successful_refresh_records_tools_and_reports_the_diff(self, monkeypatch, store):
        store.rows.append({"id": 5, "tools": [{"name": "old", "schema_sha256": "x"}]})
        monkeypatch.setattr(es, "get_global_mcp_revealed", _reveal({"id": 5, "is_active": True, "auth_mode": "none"}))

        async def discover(server):
            return [{"name": "new", "description": "N", "input_schema": {}}]

        monkeypatch.setattr(mcp_connections.mcp_client, "discover_tools", discover)
        result = await mcp_connections.refresh_catalog(5, user_id=None, project_id=None)
        assert result["status"] == "ok"
        assert result["diff"]["added"] == ["new"] and result["diff"]["removed"] == ["old"]
        assert store.catalog_writes[0][1][0]["name"] == "new"

    async def test_failed_refresh_keeps_the_last_catalog_and_records_only_the_error(self, monkeypatch, store):
        monkeypatch.setattr(es, "get_global_mcp_revealed", _reveal({"id": 5, "is_active": True, "auth_mode": "none"}))

        async def fail(server):
            raise TimeoutError("https://secret.example.com/?token=abc")

        monkeypatch.setattr(mcp_connections.mcp_client, "discover_tools", fail)
        result = await mcp_connections.refresh_catalog(5, user_id=None, project_id=None)
        assert result == {"status": "failed", "error": "MCP 도구 검색 실패: TimeoutError", "diff": None}
        server_id, tools, error = store.catalog_writes[0]
        assert tools is None
        assert "secret" not in error and "token" not in error

    async def test_inactive_connection_is_not_discovered(self, monkeypatch, store):
        monkeypatch.setattr(es, "get_global_mcp_revealed", _reveal({"id": 5, "is_active": False, "auth_mode": "none"}))
        with pytest.raises(mcp_connections.McpConnectionError):
            await mcp_connections.refresh_catalog(5, user_id=None, project_id=None)

    async def test_oauth_discovery_uses_the_requesters_token(self, monkeypatch, store):
        monkeypatch.setattr(
            es, "get_global_mcp_revealed", _reveal({"id": 5, "is_active": True, "auth_mode": "oauth", "headers": {}})
        )
        seen = {}

        async def bearer(*, user_id, project_id):
            return {5: {"Authorization": "Bearer user-token"}}

        async def discover(server):
            seen["headers"] = server["headers"]
            return []

        monkeypatch.setattr(mcp_connections.mcp_oauth, "headers_for_user", bearer)
        monkeypatch.setattr(mcp_connections.mcp_client, "discover_tools", discover)
        await mcp_connections.refresh_catalog(5, user_id="u1", project_id="p1")
        assert seen["headers"] == {"Authorization": "Bearer user-token"}

    async def test_oauth_discovery_without_the_requesters_connection_is_refused(self, monkeypatch, store):
        monkeypatch.setattr(es, "get_global_mcp_revealed", _reveal({"id": 5, "is_active": True, "auth_mode": "oauth"}))

        async def nobody(*, user_id, project_id):
            return {}

        monkeypatch.setattr(mcp_connections.mcp_oauth, "headers_for_user", nobody)
        with pytest.raises(mcp_connections.McpConnectionError):
            await mcp_connections.refresh_catalog(5, user_id="u1", project_id="p1")

    async def test_oauth_discovery_without_a_user_is_refused(self, monkeypatch, store):
        monkeypatch.setattr(es, "get_global_mcp_revealed", _reveal({"id": 5, "is_active": True, "auth_mode": "oauth"}))
        with pytest.raises(mcp_connections.McpConnectionError):
            await mcp_connections.refresh_catalog(5, user_id=None, project_id=None)


def _reveal(row):
    async def reveal(item_id):
        return dict(row)

    return reveal


class TestRoutes:
    async def test_create_rejects_stdio_transport_at_the_boundary(self, admin_client, store):
        resp = await admin_client.post(
            ADMIN_CONNECTIONS, json={"name": "n", "url": "https://n.example.com/mcp", "transport": "stdio"}
        )
        assert resp.status_code == 422
        assert store.created == []

    async def test_create_rejects_unknown_fields(self, admin_client, store):
        resp = await admin_client.post(
            ADMIN_CONNECTIONS, json={"name": "n", "url": "https://n.example.com/mcp", "command": "npx"}
        )
        assert resp.status_code == 422
        assert store.created == []

    async def test_non_admin_cannot_refresh_a_catalog(self, non_admin_client):
        assert (await non_admin_client.post(f"{ADMIN_CONNECTIONS}/1/catalog/refresh")).status_code == 403

    async def test_missing_connection_refresh_is_404(self, admin_client, monkeypatch):
        async def missing(item_id):
            raise es.ExtensionNotFound("missing")

        monkeypatch.setattr(es, "get_global_mcp_revealed", missing)
        assert (await admin_client.post(f"{ADMIN_CONNECTIONS}/999/catalog/refresh")).status_code == 404
