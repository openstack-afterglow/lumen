"""Complete, secret-free registry projection and opt-in featured publication contracts."""

import importlib.util
import json
from pathlib import Path

import pytest

from lumen.services import mcp_registry_catalog as catalog
from lumen.services import mcp_registry_model as model


def registry_item(identity="one", *, url="https://service.example.com/mcp", headers=(), repository=None):
    server = {
        "id": identity,
        "name": f"org/{identity}",
        "remotes": [{"transport_type": "streamable-http", "url": url, "headers": list(headers)}],
    }
    if repository:
        server["repository"] = {"url": repository}
    return {"server": server}


def test_shared_endpoints_keep_every_stable_registry_id():
    items = [registry_item("two"), registry_item("one")]
    probes = {"https://service.example.com/mcp": "open"}
    result = model.build_catalog(items, probes)
    assert [candidate["id"] for candidate in result] == ["one", "two"]
    assert model.build_catalog(list(reversed(items)), probes) == result


@pytest.mark.parametrize(
    "url",
    [
        "http://{tenant}.example.com/mcp",
        "https://user:password@example.com/mcp",
        "https://service.example.com/mcp?api_key=literal-secret",
        "https://service.example.com/mcp#secret",
        "https://localhost/mcp",
        "https://127.0.0.1/mcp",
        "https://169.254.169.254/mcp",
        "https://[::1]/mcp",
        "https://service.internal/mcp",
        "https://service.example.com/mcp%0d%0aAuthorization:bad",
        "https://service.example.com\\@127.0.0.1/mcp",
    ],
)
def test_unsafe_remote_is_visible_without_unsafe_url_or_probe(url):
    item = registry_item(url=url)
    result = model.build_entry(item, None)
    assert result["access"] == "unavailable"
    assert result["unavailable_reason"]
    assert result["url"] == ""
    assert model.probe_targets([item]) == {}


def test_unsafe_remote_does_not_hide_supported_alternative():
    item = registry_item(url="http://unsafe.example.com/mcp")
    item["server"]["remotes"].append({"transport_type": "sse", "url": "https://safe.example.com/sse"})
    result = model.build_entry(item, "oauth")
    assert (result["transport"], result["url"]) == ("sse", "https://safe.example.com/sse")


def test_non_secret_query_selector_is_kept_and_required_header_decides_access():
    item = registry_item(
        url="https://mcp.hasdata.com/mcp?apis=google_serp",
        headers=[{"name": "x-api-key", "description": "Key", "is_required": True, "is_secret": True}],
    )
    result = model.build_entry(item, None)
    assert result["access"] == "credentials"
    assert result["url"] == "https://mcp.hasdata.com/mcp?apis=google_serp"
    assert result["header_hints"][0]["name"] == "x-api-key"
    assert model.probe_targets([item]) == {}


@pytest.mark.parametrize(
    "query", ["api_key=x", "apiKey=x", "token=x", "access-key=x", "key=x", "sig=x", "Authorization=x", "session=x"]
)
def test_literal_value_under_credential_like_query_key_is_refused(query):
    assert model.safe_catalog_url(f"https://service.example.com/mcp?{query}") == ""
    assert model.safe_catalog_url(f"https://service.example.com/mcp?apis=a&{query}") == ""


@pytest.mark.parametrize("url", ["https://{host}/.api/mcp", "https://{baseUrl}/mcp/{server-name}"])
def test_whole_host_templates_are_configure_entries(url):
    result = model.build_entry(registry_item(url=url), None)
    assert result["access"] == "configure"
    assert result["url"] == url
    assert result["variables"] == model.url_variables(url)


def test_filled_template_is_rechecked():
    assert catalog._fill_url(
        {"url": "https://{host}/mcp", "variables": ["host"]}, {"host": "sg.example.com"}
    ) == "https://sg.example.com/mcp"
    for host in ("localhost", "127.0.0.1", "app.internal"):
        with pytest.raises(model.McpRegistryError):
            catalog._fill_url({"url": "https://{host}/mcp", "variables": ["host"]}, {"host": host})
    # A placeholder under a credential-like key is catalogued, but a filled value is never stored in the URL.
    template = {"url": "https://svc.example.com/mcp?token={token}", "variables": ["token"]}
    assert model.safe_catalog_url(template["url"]) == template["url"]
    with pytest.raises(model.McpRegistryError):
        catalog._fill_url(template, {"token": "secret"})


def test_header_metadata_preserves_names_flags_and_never_values():
    hints = model.header_hints(
        {"headers": [{
            "name": "Authorization", "description": "Account token", "is_required": True,
            "is_secret": True, "value": "Bearer literal-secret", "default": "default-secret",
            "variables": {"token": {"default": "nested-secret"}},
        }]}
    )
    assert hints == [{"name": "Authorization", "description": "Account token", "required": True, "secret": True}]
    assert "literal-secret" not in json.dumps(hints)
    assert "default-secret" not in json.dumps(hints)
    assert "nested-secret" not in json.dumps(hints)


def test_missing_header_metadata_stays_unknown_and_requires_configuration():
    hints = model.header_hints({"headers": [{"description": "Publisher omitted the header name"}]})
    assert hints[0]["name"] is None
    assert hints[0]["required"] is None
    assert hints[0]["secret"] is None
    assert model.classify("https://service.example.com/mcp", hints, "open") == "credentials"


def test_optional_header_does_not_claim_authentication_is_required():
    item = registry_item(headers=[{"name": "X-Feature", "is_required": False, "is_secret": False}])
    assert model.probe_targets([item]) == {"https://service.example.com/mcp": "http"}
    assert model.build_entry(item, "open")["access"] == "open"


@pytest.mark.parametrize(
    ("project", "profile"), [("playwright-mcp", "playwright"), ("markitdown", "markitdown")]
)
def test_official_stdio_only_featured_entries_are_managed_not_fabricated_hosts(project, profile):
    item = {"server": {
        "id": project, "name": f"microsoft/{project}", "packages": [{"registry_name": "npm"}],
        "repository": {"url": f"https://github.com/microsoft/{project}"},
    }}
    result = model.build_entry(item, None)
    assert result["featured"] is True
    assert result["access"] == "managed"
    assert result["managed_profile"] == profile
    assert result["unavailable_reason"] is None
    assert result["url"] == ""
    assert result["transport"] is None


def test_managed_requires_exact_official_repository():
    item = {"server": {
        "id": "x", "name": "impostor/playwright", "packages": [{}],
        "repository": {"url": "https://github.com/impostor/playwright-mcp"},
    }}
    result = model.build_entry(item, None)
    assert (result["access"], result["managed_profile"], result["featured"]) == ("unavailable", None, False)
    assert result["unavailable_reason"] == "stdio_only"


def test_featured_remote_requires_official_repository_and_exact_endpoint():
    official = registry_item(url="https://mcp.stripe.com", repository="https://github.com/stripe/agent-toolkit")
    assert model.build_entry(official, "oauth")["featured"] is True
    impostor = registry_item(url="https://mcp.stripe.com", repository="https://github.com/impostor/stripe")
    assert model.build_entry(impostor, "oauth")["featured"] is False
    wrong_endpoint = registry_item(repository="https://github.com/stripe/agent-toolkit")
    assert model.build_entry(wrong_endpoint, "open")["featured"] is False


def test_coverage_counts_include_local_only_and_managed_entries():
    remote = model.build_entry(registry_item(), "open")
    local = model.build_entry({"server": {"id": "local", "name": "org/local", "packages": [{}]}}, None)
    managed = model.build_entry({"server": {
        "id": "pw", "name": "microsoft/playwright-mcp", "packages": [{}],
        "repository": {"url": "https://github.com/microsoft/playwright-mcp"},
    }}, None)
    result = model.coverage_counts([remote, local, managed], 3)
    assert result["registry_total"] == result["catalogued"] == 3
    assert result["remote"] == result["unavailable"] == result["managed"] == 1
    assert result["featured"] == 1
    assert sum(result["by_access"].values()) == 3


async def test_featured_install_reuses_idempotent_installs_and_never_selects_sources(monkeypatch):
    remote = model.build_entry(registry_item("featured", url="https://mcp.stripe.com", repository="https://github.com/stripe/agent-toolkit"), "oauth")
    ordinary = model.build_entry(registry_item("ordinary"), "open")
    configured = {**remote, "id": "configured", "access": "credentials"}
    local = {**remote, "id": "local", "access": "unavailable", "url": "", "unavailable_reason": "stdio_only"}
    managed = {**remote, "id": "managed", "access": "managed", "url": "", "managed_profile": "playwright",
               "unavailable_reason": None}
    monkeypatch.setattr(catalog, "_load", lambda: (remote, ordinary, configured, local, managed))
    calls = []
    existing = set()

    async def create_connection(**kwargs):
        calls.append(kwargs)
        created = kwargs["template_key"] not in existing
        existing.add(kwargs["template_key"])
        return {"created": created, "server": {"id": 1}}

    monkeypatch.setattr(catalog.mcp_connections, "create_connection", create_connection)
    first = await catalog.install_featured()
    second = await catalog.install_featured()
    assert first["created"] == ["featured"]
    assert second["created"] == []
    assert second["existing"] == ["featured"]
    assert [(item["id"], item["reason"]) for item in first["skipped"]] == [
        ("configured", "credentials"), ("local", "stdio_only"), ("managed", "managed"),
    ]
    assert all(call["implicit_selection"] is False for call in calls)
    assert all(call["headers"] is None for call in calls)
    assert all(call["auth_mode"] == "oauth" for call in calls)


async def test_open_bulk_install_never_enables_implicit_selection(monkeypatch):
    candidate = model.build_entry(registry_item(), "open")
    monkeypatch.setattr(catalog, "_load", lambda: (candidate,))
    calls = []

    async def create_connection(**kwargs):
        calls.append(kwargs)
        return {"created": True, "server": {"id": 1}}

    monkeypatch.setattr(catalog.mcp_connections, "create_connection", create_connection)
    await catalog.install_all()
    assert calls[0]["implicit_selection"] is False
    assert calls[0]["auth_mode"] == "none"


async def test_unavailable_install_fails_before_connection_service(monkeypatch):
    candidate = model.build_entry({"server": {"id": "local", "name": "org/local", "packages": [{}]}}, None)
    monkeypatch.setattr(catalog, "_load", lambda: (candidate,))

    async def create_connection(**kwargs):
        raise AssertionError("Unavailable entries must not reach installation")

    monkeypatch.setattr(catalog.mcp_connections, "create_connection", create_connection)
    with pytest.raises(model.McpRegistryError, match="stdio_only"):
        await catalog.install("local")


async def test_managed_install_fails_before_connection_service(monkeypatch):
    candidate = model.build_entry({"server": {
        "id": "md", "name": "microsoft/markitdown", "packages": [{}],
        "repository": {"url": "https://github.com/microsoft/markitdown"},
    }}, None)
    monkeypatch.setattr(catalog, "_load", lambda: (candidate,))

    async def create_connection(**kwargs):
        raise AssertionError("Managed entries need no connection")

    monkeypatch.setattr(catalog.mcp_connections, "create_connection", create_connection)
    with pytest.raises(model.McpRegistryError, match="markitdown"):
        await catalog.install("md")
    for result in (await catalog.install_all(), await catalog.install_featured()):
        assert result["skipped"] == [{"id": "md", "name": "microsoft/markitdown", "reason": "managed"}]


@pytest.mark.parametrize("value", ["tenant%2fother", "user:password", "x&token=secret", "x\\evil", "x@evil", "x?token"])
def test_template_substitution_refuses_url_structure_injection(value):
    candidate = {"url": "https://{tenant}.example.com/mcp", "variables": ["tenant"]}
    with pytest.raises(model.McpRegistryError):
        catalog._fill_url(candidate, {"tenant": value})


def test_bundled_snapshot_has_complete_coverage_and_no_header_values():
    document = json.loads(catalog.CATALOG_PATH.read_text(encoding="utf-8"))
    entries = document["entries"]
    assert len(entries) == document["registry_total"]
    assert len({candidate["id"] for candidate in entries}) == len(entries)
    assert entries == sorted(entries, key=lambda candidate: (candidate["registry_name"], candidate["id"]))
    assert document["source_url"] == "https://api.mcp.github.com/v0/servers"
    assert document["coverage"] == model.coverage_counts(entries, document["registry_total"])
    for candidate in entries:
        assert {"featured", "unavailable_reason", "managed_profile"} <= set(candidate)
        if candidate["access"] == "unavailable":
            assert candidate["url"] == ""
            assert candidate["unavailable_reason"] in {"stdio_only", "no_supported_remote", "no_remote"}
            assert candidate["managed_profile"] is None
        elif candidate["access"] == "managed":
            assert candidate["url"] == "" and candidate["unavailable_reason"] is None
            assert candidate["featured"] is True and candidate["managed_profile"] in {"playwright", "markitdown"}
        else:
            assert model.safe_catalog_url(candidate["url"]) == candidate["url"]
            assert candidate["managed_profile"] is None
        for hint in candidate["header_hints"]:
            assert set(hint) == {"name", "description", "required", "secret"}


@pytest.fixture
def sync_script():
    path = Path(__file__).resolve().parents[1] / "scripts" / "sync_mcp_registry.py"
    spec = importlib.util.spec_from_file_location("sync_mcp_registry_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_read_only_refresh_does_not_probe_remotes(monkeypatch, tmp_path, sync_script):
    item = registry_item()
    previous = tmp_path / "previous.json"
    previous.write_text(json.dumps({"entries": [model.build_entry(item, "open")]}))
    output = tmp_path / "generated.json"
    monkeypatch.setattr(sync_script, "OUTPUT", previous)
    monkeypatch.setattr(sync_script, "fetch_registry", lambda: (1, [item]))
    monkeypatch.setattr(sync_script, "enrich_header_metadata", lambda items: {})
    monkeypatch.setattr("sys.argv", ["sync_mcp_registry.py", "--registry-only", "--output", str(output)])

    def unexpected_probe(*args):
        raise AssertionError("Read-only registry refresh must not probe any remote")

    monkeypatch.setattr(sync_script, "probe", unexpected_probe)
    sync_script.main()
    document = json.loads(output.read_text())
    assert document["entries"][0]["access"] == "open"
    assert document["auth_observation"] == "registry_only_retained_previous"


def test_header_enrichment_copies_only_exact_upstream_metadata(monkeypatch, sync_script):
    item = registry_item(headers=[{"description": "Token", "is_secret": True}], repository="https://github.com/org/one")
    upstream = {"server": {
        "name": "org/one", "repository": {"url": "https://github.com/org/one"},
        "remotes": [{"type": "streamable-http", "url": "https://service.example.com/mcp", "headers": [{
            "name": "X-Token", "description": "Token", "isRequired": True, "isSecret": True,
            "value": "do-not-copy", "default": "do-not-copy-default",
        }]}],
    }}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps(upstream).encode()

    monkeypatch.setattr(sync_script.urllib.request, "urlopen", lambda *args, **kwargs: Response())
    result = sync_script.enrich_header_metadata([item])
    assert result["headers_recovered"] == 1
    hints = model.header_hints(item["server"]["remotes"][0])
    assert hints[0] == {"name": "X-Token", "description": "Token", "required": True, "secret": True}
    assert "do-not-copy" not in json.dumps(item)


@pytest.mark.parametrize("mismatch", ["name", "repository", "url", "transport", "description"])
def test_header_enrichment_rejects_mismatched_upstream_identity(monkeypatch, sync_script, mismatch):
    item = registry_item(headers=[{"description": "Token"}], repository="https://github.com/org/one")
    server = {
        "name": "org/one", "repository": {"url": "https://github.com/org/one"},
        "remotes": [{"type": "streamable-http", "url": "https://service.example.com/mcp",
                     "headers": [{"name": "X-Token", "description": "Token", "isRequired": True}]}],
    }
    if mismatch == "name":
        server["name"] = "org/other"
    elif mismatch == "repository":
        server["repository"]["url"] = "https://github.com/org/other"
    elif mismatch == "url":
        server["remotes"][0]["url"] = "https://other.example.com/mcp"
    elif mismatch == "transport":
        server["remotes"][0]["type"] = "sse"
    else:
        server["remotes"][0]["headers"][0]["description"] = "Different header"

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps({"server": server}).encode()

    monkeypatch.setattr(sync_script.urllib.request, "urlopen", lambda *args, **kwargs: Response())
    assert sync_script.enrich_header_metadata([item])["headers_recovered"] == 0
    assert model.header_hints(item["server"]["remotes"][0])[0]["name"] is None


def test_incremental_refresh_probes_only_new_remote(monkeypatch, tmp_path, sync_script):
    old = registry_item("old", url="https://old.example.com/mcp")
    new = registry_item("new", url="https://new.example.com/mcp")
    previous = tmp_path / "previous.json"
    previous.write_text(json.dumps({"entries": [model.build_entry(old, "oauth")]}))
    output = tmp_path / "generated.json"
    monkeypatch.setattr(sync_script, "OUTPUT", previous)
    monkeypatch.setattr(sync_script, "fetch_registry", lambda: (2, [old, new]))
    monkeypatch.setattr(sync_script, "enrich_header_metadata", lambda items: {})
    monkeypatch.setattr("sys.argv", ["sync_mcp_registry.py", "--output", str(output)])
    probed = []
    monkeypatch.setattr(sync_script, "probe", lambda url, transport: probed.append(url) or "open")
    sync_script.main()
    assert probed == ["https://new.example.com/mcp"]
    access = {entry["id"]: entry["access"] for entry in json.loads(output.read_text())["entries"]}
    assert access == {"old": "oauth", "new": "open"}


async def test_admin_may_supply_an_optional_key_on_a_header_hinted_entry(monkeypatch):
    candidate = model.build_entry(
        registry_item(headers=[{"name": "Authorization", "is_required": False, "is_secret": True}]), "open"
    )
    monkeypatch.setattr(catalog, "_load", lambda: (candidate,))
    calls = []

    async def create_connection(**kwargs):
        calls.append(kwargs)
        return {"created": True, "server": {"id": 1}}

    monkeypatch.setattr(catalog.mcp_connections, "create_connection", create_connection)
    await catalog.install("one")
    await catalog.install("one", auth_mode="admin", headers={"Authorization": "Bearer key"}, label="keyed")
    assert (calls[0]["auth_mode"], calls[0]["headers"]) == ("none", None)
    assert (calls[1]["auth_mode"], calls[1]["headers"]) == ("admin", {"Authorization": "Bearer key"})
    assert all(call["implicit_selection"] is False for call in calls)


async def test_admin_override_is_refused_without_header_hints(monkeypatch):
    candidate = model.build_entry(registry_item(), "open")
    monkeypatch.setattr(catalog, "_load", lambda: (candidate,))

    async def create_connection(**kwargs):
        raise AssertionError("A refused override must not reach installation")

    monkeypatch.setattr(catalog.mcp_connections, "create_connection", create_connection)
    with pytest.raises(model.McpRegistryError, match="none"):
        await catalog.install("one", auth_mode="admin", headers={"Authorization": "x"})


async def test_bulk_install_skips_a_second_registry_id_for_one_endpoint(monkeypatch):
    first = model.build_entry(registry_item("a"), "open")
    second = model.build_entry(registry_item("b", url="https://SERVICE.example.com/mcp/"), "open")
    monkeypatch.setattr(catalog, "_load", lambda: (first, second))
    calls = []

    async def create_connection(**kwargs):
        calls.append(kwargs["template_key"])
        return {"created": True, "server": {"id": 1}}

    monkeypatch.setattr(catalog.mcp_connections, "create_connection", create_connection)
    result = await catalog.install_all()
    assert calls == ["a"]
    assert result["skipped"] == [{"id": "b", "name": "org/b", "reason": "duplicate_endpoint"}]
