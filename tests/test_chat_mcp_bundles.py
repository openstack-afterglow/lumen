"""Built-in remote MCP connector bundles (Notion, GitHub).

Three properties decide whether a shipped connector is safe:
- installation must be additive, because rewriting a shared global row bumps
  ``config_version`` and revokes every user's OAuth connection to it;
- a bundle must never carry a credential, so each user authorizes separately;
- a connector nobody has connected must not turn every run into a warning.
"""

import pytest
from fastapi import HTTPException

from lumen.models.chat_contracts import ChatFeatureOptions
from lumen.services import chat_admission, mcp_bundles
from lumen.services import extensions_store as es

_ADMIN_BUNDLES = "/api/v1/chat/admin/mcp-bundles"


def _return(value):
    async def _call():
        return value

    return _call()


def _row(item_id: int, url: str, name: str = "Notion") -> dict:
    return {
        "id": item_id,
        "scope": "global",
        "name": name,
        "url": url,
        "transport": "http",
        "auth_mode": "oauth",
        "is_active": True,
        "load_policy": "on_demand",
        "config_version": 1,
    }


class TestDefinitions:
    def test_both_requested_connectors_ship(self):
        assert set(mcp_bundles.BUNDLES) == {"notion", "github"}

    @pytest.mark.parametrize("slug", ["notion", "github"])
    def test_destination_is_a_public_https_endpoint(self, slug):
        assert mcp_bundles.BUNDLES[slug]["url"].startswith("https://")
        assert mcp_bundles.BUNDLES[slug]["transport"] == "http"

    @pytest.mark.parametrize("slug", ["notion", "github"])
    def test_every_bundle_delegates_auth_to_per_user_oauth(self, slug):
        # A shared static token would make one person's access everyone's, and
        # extension_packages rejects credential-bearing manifests outright.
        assert mcp_bundles.BUNDLES[slug]["auth_mode"] == "oauth"

    @pytest.mark.parametrize("slug", ["notion", "github"])
    def test_no_credential_material_is_declared(self, slug):
        forbidden = {"token", "password", "secret", "api_key", "authorization", "headers", "oauth_client_secret"}
        assert not forbidden & set(mcp_bundles.BUNDLES[slug])

    @pytest.mark.parametrize("slug", ["notion", "github"])
    def test_large_toolsets_stay_behind_the_catalog(self, slug):
        # Preloading either server's full toolset would spend the per-request
        # schema budget on tools the model usually does not need.
        assert mcp_bundles.BUNDLES[slug]["load_policy"] == "on_demand"

    def test_unknown_slug_is_rejected(self):
        with pytest.raises(mcp_bundles.McpBundleError):
            mcp_bundles.definition("slack")

    def test_install_fields_stay_inside_the_store_whitelist(self):
        fields = mcp_bundles._install_fields(mcp_bundles.definition("github"))
        assert set(fields) == {"name", "transport", "url", "auth_mode", "load_policy", "is_active"}


class TestCatalog:
    async def test_reports_uninstalled_bundles(self, monkeypatch):
        monkeypatch.setattr(es, "list_global", lambda kind: _return([]))
        entries = {entry["slug"]: entry for entry in await mcp_bundles.catalog()}
        assert entries["notion"]["installed"] is False
        assert entries["notion"]["server_id"] is None

    async def test_matches_an_installed_row_by_destination(self, monkeypatch):
        monkeypatch.setattr(es, "list_global", lambda kind: _return([_row(7, "https://mcp.notion.com/mcp")]))
        entries = {entry["slug"]: entry for entry in await mcp_bundles.catalog()}
        assert entries["notion"]["installed"] is True
        assert entries["notion"]["server_id"] == 7
        assert entries["github"]["installed"] is False

    async def test_trailing_slash_and_case_do_not_create_a_duplicate(self, monkeypatch):
        monkeypatch.setattr(es, "list_global", lambda kind: _return([_row(9, "HTTPS://MCP.NOTION.COM/MCP/")]))
        entries = {entry["slug"]: entry for entry in await mcp_bundles.catalog()}
        assert entries["notion"]["server_id"] == 9

    async def test_catalog_never_leaks_credential_state(self, monkeypatch):
        monkeypatch.setattr(es, "list_global", lambda kind: _return([]))
        for entry in await mcp_bundles.catalog():
            assert "headers" not in entry
            assert "oauth_client_secret" not in entry


class TestInstall:
    async def test_creates_a_global_row_with_the_declared_fields(self, monkeypatch):
        captured: dict = {}

        async def fake_create(kind, fields, **kwargs):
            captured["kind"] = kind
            captured["fields"] = fields
            captured.update(kwargs)
            return _row(3, fields["url"], name=fields["name"])

        monkeypatch.setattr(es, "list_global", lambda kind: _return([]))
        monkeypatch.setattr(es, "create", fake_create)

        result = await mcp_bundles.install("github")
        assert result["created"] is True
        assert captured["kind"] == "mcp"
        assert captured["scope"] == "global"
        assert captured["fields"]["url"] == "https://api.githubcopilot.com/mcp/"
        assert captured["fields"]["auth_mode"] == "oauth"
        assert captured["fields"]["is_active"] is True

    async def test_reinstall_is_a_no_op_and_never_rewrites_the_row(self, monkeypatch):
        """Rewriting the row would bump config_version and disconnect every user."""

        async def must_not_create(*_args, **_kwargs):
            raise AssertionError("existing bundle row must not be re-created")

        async def must_not_update(*_args, **_kwargs):
            raise AssertionError("existing bundle row must not be updated")

        monkeypatch.setattr(es, "list_global", lambda kind: _return([_row(7, "https://mcp.notion.com/mcp")]))
        monkeypatch.setattr(es, "create", must_not_create)
        monkeypatch.setattr(es, "update", must_not_update)

        result = await mcp_bundles.install("notion")
        assert result["created"] is False
        assert result["server"]["id"] == 7

    async def test_unknown_slug_raises_before_touching_the_store(self, monkeypatch):
        async def must_not_list(*_args, **_kwargs):
            raise AssertionError("unknown slug must not reach the store")

        monkeypatch.setattr(es, "list_global", must_not_list)
        with pytest.raises(mcp_bundles.McpBundleError):
            await mcp_bundles.install("slack")


class TestAdminRoutes:
    async def test_catalog_requires_admin(self, non_admin_client):
        assert (await non_admin_client.get(_ADMIN_BUNDLES)).status_code == 403

    async def test_install_requires_admin(self, non_admin_client):
        assert (await non_admin_client.post(f"{_ADMIN_BUNDLES}/notion/install")).status_code == 403

    async def test_admin_lists_bundles(self, admin_client, monkeypatch):
        monkeypatch.setattr(es, "list_global", lambda kind: _return([]))
        resp = await admin_client.get(_ADMIN_BUNDLES)
        assert resp.status_code == 200
        assert {entry["slug"] for entry in resp.json()} == {"notion", "github"}

    async def test_admin_installs_a_bundle(self, admin_client, monkeypatch):
        async def fake_create(kind, fields, **kwargs):
            return _row(4, fields["url"], name=fields["name"])

        monkeypatch.setattr(es, "list_global", lambda kind: _return([]))
        monkeypatch.setattr(es, "create", fake_create)

        resp = await admin_client.post(f"{_ADMIN_BUNDLES}/github/install")
        assert resp.status_code == 200
        assert resp.json()["created"] is True

    async def test_unknown_bundle_is_404(self, admin_client, monkeypatch):
        monkeypatch.setattr(es, "list_global", lambda kind: _return([]))
        assert (await admin_client.post(f"{_ADMIN_BUNDLES}/slack/install")).status_code == 404


class TestUnconnectedConnectorSelection:
    """An installed bundle is visible to everyone; most users have not connected it."""

    @staticmethod
    def _patch(monkeypatch, credential_versions):
        async def fake_list(kind, **_kwargs):
            if kind != "mcp":
                return []
            return [_row(7, "https://mcp.notion.com/mcp"), _row(8, "https://mcp.other.example", name="Other")]

        async def fake_versions(server_ids, *, user_id, project_id):
            return credential_versions

        monkeypatch.setattr(chat_admission.es, "list_for_user", fake_list)
        monkeypatch.setattr(chat_admission.es, "mcp_credential_versions", fake_versions)

    async def test_implicit_selection_quietly_excludes_an_unconnected_connector(self, monkeypatch):
        # Freezing credential_version=None would make the worker reject it and
        # emit "credential changed or was revoked" on every single run.
        self._patch(monkeypatch, {7: None, 8: 2})
        selection = await chat_admission._resolve_extension_selection(
            None, ChatFeatureOptions(), user_id="u1", project_id="p1"
        )
        assert [item["id"] for item in selection["mcp"]] == [8]

    async def test_implicit_selection_keeps_a_connected_connector(self, monkeypatch):
        self._patch(monkeypatch, {7: 5, 8: 2})
        selection = await chat_admission._resolve_extension_selection(
            None, ChatFeatureOptions(), user_id="u1", project_id="p1"
        )
        assert [item["id"] for item in selection["mcp"]] == [7, 8]
        assert selection["mcp"][0]["credential_version"] == 5

    async def test_non_oauth_source_is_unaffected(self, monkeypatch):
        # Servers without OAuth never appear in the credential map at all.
        self._patch(monkeypatch, {})
        selection = await chat_admission._resolve_extension_selection(
            None, ChatFeatureOptions(), user_id="u1", project_id="p1"
        )
        assert [item["credential_version"] for item in selection["mcp"]] == [0, 0]

    async def test_explicit_selection_of_an_unconnected_connector_still_surfaces(self, monkeypatch):
        """A request that named the server asked for something it cannot reach."""
        self._patch(monkeypatch, {7: None, 8: 2})
        selection = await chat_admission._resolve_extension_selection(
            None,
            ChatFeatureOptions(tool_policy={"enabled_mcp_ids": [7]}),
            user_id="u1",
            project_id="p1",
        )
        assert [item["id"] for item in selection["mcp"]] == [7]
        assert selection["mcp"][0]["credential_version"] is None

    async def test_agent_allowlist_counts_as_explicit(self, monkeypatch):
        self._patch(monkeypatch, {7: None, 8: 2})
        selection = await chat_admission._resolve_extension_selection(
            {"tool_ids": [], "mcp_ids": [7]},
            ChatFeatureOptions(),
            user_id="u1",
            project_id="p1",
        )
        assert [item["id"] for item in selection["mcp"]] == [7]

    async def test_missing_extension_is_still_a_hard_error(self, monkeypatch):
        self._patch(monkeypatch, {7: None, 8: 2})
        with pytest.raises(HTTPException):
            await chat_admission._resolve_extension_selection(
                None,
                ChatFeatureOptions(tool_policy={"enabled_mcp_ids": [99]}),
                user_id="u1",
                project_id="p1",
            )
