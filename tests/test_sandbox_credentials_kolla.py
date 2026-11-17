"""Typed role precheck and public-only Kolla mounts; no production access."""
from __future__ import annotations

import json
import tomllib
from pathlib import Path

import jinja2
import pytest
import yaml
from test_kolla_assets import _kolla_environment, _resolved_kolla_defaults
from test_kolla_precheck import NOW, ROLE, _mounted_reader, precheck
from test_kolla_precheck import deployment as deployment
from test_kolla_precheck import kubernetes_deployment as kubernetes_deployment
from test_sandbox_credentials import SOURCE

from lumen.services.infrastructure.config import RuntimeConfig


@pytest.fixture
def drover_deployment(kubernetes_deployment, tmp_path):
    fixture = kubernetes_deployment
    fixture.cluster.pop("bearer_token")
    fixture.cluster["drover"] = SOURCE
    for name, path, data in (
        ("password", SOURCE["password"]["file"], b"operator-password\n"),
        ("trust", SOURCE["ca_file"], fixture.mounted[fixture.cluster["ca_file"]].read_bytes()),
    ):
        local = tmp_path / ("drover-" + name)
        local.write_bytes(data)
        local.chmod(0o600)
        fixture.mounted[path] = local
    fixture.reader = _mounted_reader(fixture.mounted)
    return fixture


def test_controller_requires_only_renewable_files_not_static_token(drover_deployment):
    fixture = drover_deployment
    config, files = precheck.validate_config(fixture.payload)
    assert config.kubernetes_clusters[0].drover.project_id == SOURCE["project_id"]
    assert {entry["kind"] for entry in files} >= {"drover_password", "drover_ca", "kubernetes_ca"}
    assert not any(entry["kind"] == "kubernetes_token" for entry in files)
    precheck.validate_material(fixture.payload, now=NOW,
        read_material=_mounted_reader({**fixture.mounted, **fixture.worker_mounted}),
        inspect_worker_directory=fixture.inspect_worker_directory)


@pytest.mark.parametrize("fault", ["missing", "mode", "owner", "group", "symlink", "oversized", "newline"])
def test_controller_rejects_bad_protected_password(drover_deployment, fault):
    fixture = drover_deployment
    path = SOURCE["password"]["file"]
    if fault == "missing":
        del fixture.mounted[path]
    elif fault == "oversized":
        fixture.mounted[path].write_bytes(b"x" * 16385)
    elif fault == "newline":
        fixture.mounted[path].write_bytes(b"password\nsecond-line")
    overrides = {"mode": {"mode": "0644"}, "owner": {"uid": 1000}, "group": {"gid": 1000},
                 "symlink": {"islnk": True}}.get(fault, {})
    with pytest.raises(precheck.PrecheckFailure):
        precheck.validate_material(fixture.payload, now=NOW, read_material=_mounted_reader(fixture.mounted, {path: overrides}))


def test_qualification_namespace_uid_must_match_credential_pin(drover_deployment):
    fixture = drover_deployment
    fixture.cluster["drover"] = SOURCE | {"namespace_uid": "foreign-namespace-uid"}
    with pytest.raises(precheck.PrecheckFailure, match="runtime_kubernetes_qualification"):
        precheck.validate_material(fixture.payload, now=NOW, read_material=fixture.reader)


def _api_mount(fixture, tmp_path):
    config, _ = precheck.validate_config(fixture.payload)
    inventory = precheck._api_inventory(config)
    root = tmp_path / "api-tools"
    root.mkdir(mode=0o755)
    root.chmod(0o755)
    mapping = {}
    for path in inventory["directories"]:
        target = root / Path(path).relative_to("/etc/lumen/api-precheck")
        target.mkdir(parents=True, exist_ok=True, mode=0o755)
        target.chmod(0o755)
    for path in inventory["files"]:
        target = root / Path(path).relative_to("/etc/lumen/api-precheck")
        controller = path.replace("/etc/lumen/api-precheck/", "/etc/lumen/controller/")
        target.write_bytes(fixture.mounted[controller].read_bytes())
        target.chmod(0o644)
        mapping[path] = target
    def inspect(_root):
        assert _root == "/etc/lumen/api-precheck"
        return {str(Path(_root) / Path(path).relative_to(root)): {**metadata, "uid": 0, "gid": 0}
                for path, metadata in precheck._inspect_worker_directory(str(root)).items()}
    return root, mapping, inspect


def test_api_reads_public_manifest_only_never_password_or_signing_key(drover_deployment, tmp_path):
    fixture = drover_deployment
    root, mapping, inspect = _api_mount(fixture, tmp_path)
    reads = []
    reader = _mounted_reader(mapping)
    def public_only(path):
        reads.append(path)
        assert path in mapping, "API attempted to read protected controller material"
        return reader(path)
    precheck.validate_material(fixture.payload | {"material_role": "api"}, now=NOW, read_material=public_only,
                              inspect_api_directory=inspect)
    assert reads == list(mapping)
    assert {path.name for path in root.rglob("*") if path.is_file()} == {"manifest.json"}
    assert not any("password" in path or "key" in path for path in reads)


@pytest.mark.parametrize("fault", ["password", "signing-key", "symlink", "modified", "mode", "missing"])
def test_api_public_mount_refuses_secret_extras_or_manifest_drift(drover_deployment, tmp_path, fault):
    fixture = drover_deployment
    root, mapping, inspect = _api_mount(fixture, tmp_path)
    manifest = next(iter(mapping.values()))
    if fault in {"password", "signing-key"}:
        (root / fault).write_text("secret")
    elif fault == "symlink":
        manifest.unlink()
        manifest.symlink_to(fixture.mounted[fixture.pool["profile"]["tool_manifest_file"]])
    elif fault == "modified":
        manifest.write_bytes(b'{"version":1,"profiles":[{}]}')
    elif fault == "mode":
        manifest.chmod(0o600)
    elif fault == "missing":
        manifest.unlink()
    with pytest.raises(precheck.PrecheckFailure, match="runtime_api_mount_material"):
        precheck.validate_material(fixture.payload | {"material_role": "api"}, now=NOW,
                                  read_material=_mounted_reader(mapping), inspect_api_directory=inspect)


def test_worker_reads_only_public_manifest_and_dedicated_mtls_not_controller_keys(drover_deployment):
    fixture = drover_deployment
    reads = []
    def worker_only(path):
        reads.append(path)
        assert path in fixture.worker_mounted, "worker attempted to read protected controller material"
        return fixture.worker_reader(path)
    precheck.validate_material(fixture.payload | {"material_role": "worker", "worker_enabled": True}, now=NOW,
                              read_material=worker_only, inspect_worker_directory=fixture.inspect_worker_directory)
    assert all(path.startswith("/etc/lumen/worker-precheck/") for path in reads)
    assert any(path.endswith("manifest.json") for path in reads)
    assert not any("drover" in path or "ca-key" in path or "operator-key" in path for path in reads)


def test_kolla_template_roundtrips_exact_renewable_source(drover_deployment):
    fixture = drover_deployment
    line = next(line for line in (ROLE / "templates/lumen.conf.j2").read_text().splitlines()
                if line.startswith("runtime_config ="))
    environment = jinja2.Environment(undefined=jinja2.StrictUndefined)
    environment.filters.update(combine=lambda left, right: {**left, **right}, to_json=json.dumps)
    rendered = environment.from_string(line).render(lumen_runtime_config=fixture.runtime,
        lumen_controller_listen_address="127.0.0.1", lumen_controller_listen_port=8013)
    runtime = RuntimeConfig.model_validate_json(tomllib.loads(rendered)["runtime_config"], strict=True)
    assert runtime.kubernetes_clusters[0].drover.model_dump(exclude_defaults=True) == SOURCE
    assert runtime.kubernetes_clusters[0].bearer_token is None


@pytest.mark.parametrize("enabled", [False, True])
def test_kolla_api_public_only_mount_and_no_controller_secret_volume(enabled):
    overrides = {"lumen_runtime_enabled": enabled, "enable_lumen_api": True,
        "lumen_runtime_config": {"pools": [{"backend": "kubernetes", "enabled": True}]},
        "lumen_api_tools_dir": "/srv/api-public-tools", "lumen_controller_secrets_dir": "/srv/controller-private",
        "lumen_worker_secrets_dir": "/srv/worker-mtls"}
    values = _resolved_kolla_defaults(["lumen_api_runtime_volumes", "lumen_policy_directory"], overrides)
    defaults = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
    mounts = _kolla_environment({}, native=True).from_string(defaults["lumen_services"]["lumen-api"]["volumes"]).render(**values)
    assert ("/srv/api-public-tools:/etc/lumen/controller:ro" in mounts) is enabled
    assert not any("controller-private" in mount or "worker-mtls" in mount for mount in mounts)


def test_kolla_public_copy_and_role_precheck_commands_are_isolated():
    tasks = yaml.safe_load((ROLE / "tasks/precheck.yml").read_text())
    block = next(task["block"] for task in tasks if task["name"] == "Precheck | Validate typed runtime and deployment prerequisites")
    copy = next(task for task in block if task["name"] == "Precheck | Copy only approved public tool manifest bytes")
    assert copy["ansible.builtin.copy"]["remote_src"] is True
    assert copy["ansible.builtin.copy"]["mode"] == "0644"
    assert ".api_inventory.files" in copy["loop"]
    role_task = next(task for task in block if task["name"].startswith("Precheck | Validate isolated API"))
    assert role_task["no_log"] is True
    for role in ("api", "worker"):
        context = {"item": role, "lumen_precheck_script": {"path": "/tmp/precheck.py"},
                   "lumen_api_tools_dir": "/srv/api-public", "lumen_worker_secrets_dir": "/srv/worker-mtls",
                   "lumen_api_image_ref": "api-image", "lumen_worker_image_ref": "worker-image"}
        command = _kolla_environment({}, native=True).from_string(role_task["ansible.builtin.command"]["argv"]).render(**context)
        assert not any("controller-private" in part or ":/etc/lumen/controller:" in part for part in command)
        assert ("/srv/api-public:/etc/lumen/api-precheck:ro" in command) is (role == "api")
        assert ("/srv/worker-mtls:/etc/lumen/worker-precheck:ro" in command) is (role == "worker")
        assert command[-3:] == ["api-image" if role == "api" else "worker-image", "/precheck.py", "material"]


@pytest.mark.parametrize("destination", ["/srv/controller", "/srv/controller/public", "/srv", "/srv/worker", "/srv/worker/public"])
def test_kolla_api_public_root_cannot_overlap_credential_roots(destination):
    tasks = yaml.safe_load((ROLE / "tasks/precheck.yml").read_text())
    block = next(task["block"] for task in tasks if task["name"] == "Precheck | Validate typed runtime and deployment prerequisites")
    task = next(task for task in block if task["name"] == "Precheck | Keep public API directory separate from credential roots")
    environment = _kolla_environment({})
    context = {"lumen_api_tools_dir": destination, "lumen_controller_secrets_dir": "/srv/controller",
               "lumen_worker_secrets_dir": "/srv/worker"}
    assert not all(environment.compile_expression(expression)(**context)
                   for expression in task["ansible.builtin.assert"]["that"])


def test_controller_checks_public_schema_before_any_copy_of_pinned_bytes(drover_deployment):
    import hashlib
    fixture = drover_deployment
    secret = fixture.mounted[fixture.tls["ca_key_file"]].read_bytes()
    fixture.pool["profile"]["tool_manifest_sha256"] = hashlib.sha256(secret).hexdigest()
    config, _ = precheck.validate_config(fixture.payload)
    with pytest.raises(precheck.PrecheckFailure, match="runtime_kubernetes_manifest"):
        precheck._validate_kubernetes_material(config, {"kind": "kubernetes_manifest", "pool_name": fixture.pool["name"]},
            secret, {"mode": "0600"}, NOW)


@pytest.mark.parametrize("pool", [{"backend": "kubernetes"}, {"backend": "kubernetes", "enabled": False}])
def test_kolla_omitted_or_disabled_pool_needs_no_api_tool_mount(pool):
    values = _resolved_kolla_defaults(["lumen_api_runtime_volumes"], {
        "lumen_runtime_enabled": True, "lumen_runtime_config": {"pools": [pool]},
    })
    assert values["lumen_api_runtime_volumes"] == []


def test_public_source_checks_only_pinned_manifests_without_controller_credentials(drover_deployment):
    fixture = drover_deployment
    manifest = fixture.pool["profile"]["tool_manifest_file"]
    source_path = manifest.replace("/etc/lumen/controller/", "/etc/lumen/public-source/")
    mapping = {source_path: fixture.mounted[manifest]}
    reads = []
    reader = _mounted_reader(mapping)
    def public_only(path):
        reads.append(path)
        assert path in mapping
        return reader(path)
    precheck.validate_material(fixture.payload | {"material_role": "public_source"}, now=NOW, read_material=public_only)
    assert reads == [source_path]


@pytest.mark.parametrize("host", ["dms-controller1", "dms-controller2", "dms-controller3"])
def test_single_controller_override_and_public_source_resolve_per_host(host):
    values = _resolved_kolla_defaults(["enable_lumen_controller", "lumen_public_manifest_source_dir"], {
        "inventory_hostname": host, "lumen_runtime_enabled": True,
        "enable_lumen_controller": "{{ inventory_hostname == 'dms-controller1' }}",
        "lumen_controller_secrets_dir": "/srv/controller-private", "lumen_worker_secrets_dir": "/srv/worker",
    })
    assert values["enable_lumen_controller"] is (host == "dms-controller1")
    assert values["lumen_public_manifest_source_dir"] == ("/srv/controller-private" if host == "dms-controller1" else "/srv/worker")


def test_dispatch_private_file_checks_apply_only_to_controller_hosts():
    tasks = yaml.safe_load((ROLE / "tasks/precheck.yml").read_text())
    for name in ("Precheck | Verify Lumen controller secret material", "Precheck | Require the controller dispatch key file"):
        task = next(task for task in tasks if task["name"] == name)
        assert "enable_lumen_controller | bool" in task["when"]
