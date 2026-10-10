"""Tests for Lumen Kolla-Ansible packaging and lifecycle assets."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from pathlib import Path

import jinja2
import pytest
import yaml

import lumen

REPO_ROOT = Path(__file__).parent.parent
KOLLA_DIR = REPO_ROOT / "deploy" / "kolla"
ROLE_DIR = KOLLA_DIR / "ansible" / "roles" / "lumen"
KOLLA_HAPROXY_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "kolla" / "haproxy_single_service_split.cfg.j2"
KOLLA_HAPROXY_SOURCE = (
    "https://raw.githubusercontent.com/openstack/kolla-ansible/"
    "570f2b77142d7145e55f39126e3987002afe7494/ansible/roles/haproxy-config/templates/"
    "haproxy_single_service_split.cfg.j2"
)


def _kolla_inventory():
    return {
        "inventory_hostname": "controller1",
        "groups": {"lumen": ["controller1", "controller2"], "valkey": ["controller1"]},
        "hostvars": {
            "controller1": {"ansible_facts": {"hostname": "controller1"}, "api_address": "10.42.0.11"},
            "controller2": {"ansible_facts": {"hostname": "controller2"}, "api_address": "10.42.0.12"},
        },
        "enable_lumen": "yes",
        "lumen_public_haproxy_enabled": True,
        "lumen_public_haproxy_fqdn": "lumen.example.org",
        "kolla_internal_vip_address": "10.42.0.10",
        "kolla_external_vip_address": "192.0.2.10",
        "kolla_external_fqdn": "lumen.example.org",
        "haproxy_enable_external_vip": "yes",
        "haproxy_single_external_frontend": "no",
        "kolla_enable_tls_external": "no",
        "kolla_enable_tls_internal": "no",
        "haproxy_enable_http2": "yes",
        "haproxy_http2_protocol": "alpn h2,http/1.1",
        "haproxy_frontend_http_extra": [],
        "haproxy_frontend_tcp_extra": [],
        "haproxy_frontend_redirect_extra": [],
        "haproxy_health_check": "check inter 2s fall 3 rise 2",
        "haproxy_health_check_ssl": "check inter 2s fall 3 rise 2 check-ssl verify none",
        "kolla_verify_tls_backend": "yes",
        "haproxy_backend_cacert": "/etc/haproxy/ca.pem",
        "database_address": "10.42.0.10",
        "database_port": 3306,
        "lumen_database_password": "test-only-database-password",
        "valkey_server_port": 6379,
        "valkey_master_password": "test-only-valkey-password",
        "keystone_internal_url": "http://10.42.0.10:5000/v3",
        "lumen_keystone_password": "test-only-keystone-password",
        "default_project_domain_name": "Default",
        "default_user_domain_name": "Default",
        "openstack_region_name": "RegionOne",
        "openstack_interface": "internal",
        "public_protocol": "https",
        "internal_protocol": "http",
    }


def _kolla_environment(inventory, *, native=False):
    from jinja2.nativetypes import NativeEnvironment

    def combine(values, other, recursive=False):
        combined = dict(values)
        for key, value in other.items():
            if recursive and isinstance(combined.get(key), dict) and isinstance(value, dict):
                combined[key] = combine(combined[key], value, recursive=True)
            else:
                combined[key] = value
        return combined

    environment_class = NativeEnvironment if native else jinja2.Environment
    environment = environment_class(undefined=jinja2.StrictUndefined, lstrip_blocks=True)
    environment.filters.update(
        bool=lambda value: str(value).lower() in {"true", "yes", "on", "1"},
        combine=combine,
        to_json=json.dumps,
        kolla_address=lambda network, host: inventory["hostvars"][host][f"{network}_address"],
    )
    return environment


def _resolved_kolla_defaults(names, overrides=None):
    """Resolve referenced role defaults as native values before rendering consumers."""
    from jinja2 import meta

    inventory = _kolla_inventory()
    inventory.update(overrides or {})
    defaults = yaml.safe_load((ROLE_DIR / "defaults" / "main.yml").read_text(encoding="utf-8"))
    inputs = defaults | inventory
    environment = _kolla_environment(inventory, native=True)
    resolved = {}

    def resolve_value(value):
        if isinstance(value, dict):
            return {key: resolve_value(item) for key, item in value.items()}
        if isinstance(value, list):
            return [resolve_value(item) for item in value]
        if isinstance(value, str) and ("{{" in value or "{%" in value):
            references = meta.find_undeclared_variables(environment.parse(value))
            context = {name: resolve_name(name) for name in references if name in inputs}
            return environment.from_string(value).render(**context)
        return value

    def resolve_name(name):
        if name not in resolved:
            resolved[name] = resolve_value(inputs[name])
        return resolved[name]

    return {name: resolve_name(name) for name in names}


def _haproxy_sections(rendered):
    sections = {}
    current = None
    for line in rendered.splitlines():
        directive = " ".join(line.split())
        if directive.startswith(("frontend ", "backend ")):
            current = directive
            sections[current] = []
        elif current and directive and not directive.startswith("#"):
            sections[current].append(directive)
    return sections


def test_upstream_haproxy_fixture_provenance():
    header, upstream = KOLLA_HAPROXY_FIXTURE.read_bytes().split(b"#}\n", 1)
    assert KOLLA_HAPROXY_SOURCE.encode() in header
    # Pin the exact upstream bytes, not a simplified local reimplementation.
    assert hashlib.sha256(upstream).hexdigest() == "bf345844eccf9d9c284deb728c2122c74fe19df00836d80aa9fb5dc67a501aad"


@pytest.mark.parametrize("vip", ["", "10.42.0.20", "2001:db8::20"])
@pytest.mark.parametrize("tls", [False, True])
@pytest.mark.parametrize("overrides", [
    {},
    {"lumen_api_port": 9012},
    {"lumen_api_dynamic_ingress_port": 9443, "lumen_api_proxy_idle_timeout_seconds": 900},
])
def test_kolla_upstream_haproxy_renders_inventory_or_octavia(vip, tls, overrides):
    inventory = _kolla_inventory() | overrides | {
        "lumen_api_dynamic_ingress_vip": vip,
        "kolla_enable_tls_external": tls,
        "kolla_enable_tls_internal": tls,
    }
    values = _resolved_kolla_defaults(
        ["lumen_haproxy_services", "lumen_api_dynamic_ingress_port", "lumen_api_proxy_idle_timeout_seconds"],
        inventory,
    )
    environment = _kolla_environment(inventory)
    template = environment.from_string(KOLLA_HAPROXY_FIXTURE.read_text(encoding="utf-8"))
    idle_timeout = values["lumen_api_proxy_idle_timeout_seconds"]
    assert idle_timeout == overrides.get("lumen_api_proxy_idle_timeout_seconds", 3600)
    ingress_port = values["lumen_api_dynamic_ingress_port"]
    assert ingress_port == overrides.get("lumen_api_dynamic_ingress_port", overrides.get("lumen_api_port", 8012))

    for name, bind_address in [("lumen-api", "10.42.0.10"), ("lumen-public", "192.0.2.10")]:
        service = values["lumen_haproxy_services"][name]
        haproxy = service["haproxy"][name]
        assert ("custom_member_list" in haproxy) is bool(vip)
        sections = _haproxy_sections(template.render(service=service, **inventory))
        frontend = sections[f"frontend {name}_front"]
        backend = sections[f"backend {name}_back"]
        port = overrides.get("lumen_api_port", 8012)
        bind = next(line for line in frontend if line.startswith("bind "))
        assert bind.startswith(f"bind {bind_address}:{port}")
        assert ("ssl crt" in bind) is tls
        assert "option forwardfor" in frontend
        assert f"timeout client {idle_timeout}s" in frontend
        assert "mode http" in backend
        assert "option httpchk GET /v1/ready" in backend
        assert "option httpchk GET /v1/health" not in backend
        assert "http-check expect status 200" in backend
        assert "timeout connect 5s" in backend
        assert f"timeout server {idle_timeout}s" in backend
        assert f"timeout tunnel {idle_timeout}s" in backend
        servers = [line for line in backend if line.startswith("server ")]
        if vip:
            member_address = f"[{vip}]" if ":" in vip else vip
            expected_member = f"server lumen-octavia {member_address}:{ingress_port} check inter 2s fall 3 rise 2"
            assert haproxy["custom_member_list"] == [expected_member]
            assert servers == [expected_member]
        else:
            assert servers == [
                "server controller1 10.42.0.11:18012 check inter 2s fall 3 rise 2",
                "server controller2 10.42.0.12:18012 check inter 2s fall 3 rise 2",
            ]
        # Neither SSE compression/buffering nor forced-close/Upgrade rewriting.
        assert not any(
            line.startswith(("compression ", "filter ", "option httpclose", "option http-server-close",
                             "option forceclose")) or "header Upgrade" in line
            for line in frontend + backend
        )

    healthcheck = values["lumen_haproxy_services"]["lumen-api"]["healthcheck"]["test"]
    assert healthcheck[:3] == ["CMD", "python", "-c"]
    assert "http://10.42.0.11:18012/v1/health" in healthcheck[3]
    assert "/v1/ready" not in healthcheck[3]


@pytest.mark.parametrize("vip", ["", "10.42.0.20", "2001:db8::20"])
def test_kolla_shared_external_frontend_retains_backend_timeouts(vip):
    inventory = _kolla_inventory() | {
        "haproxy_single_external_frontend": "yes",
        "kolla_enable_tls_external": "yes",
        "lumen_api_dynamic_ingress_vip": vip,
    }
    services = _resolved_kolla_defaults(["lumen_haproxy_services"], inventory)["lumen_haproxy_services"]
    template = _kolla_environment(inventory).from_string(KOLLA_HAPROXY_FIXTURE.read_text(encoding="utf-8"))
    sections = _haproxy_sections(template.render(service=services["lumen-public"], **inventory))
    # Upstream omits service frontend extras in this mode; shared client timeout
    # must be configured on Kolla's global public frontend (checked by precheck).
    assert "frontend lumen-public_front" not in sections
    backend = sections["backend lumen-public_back"]
    assert "timeout server 3600s" in backend
    assert "timeout tunnel 3600s" in backend
    assert "timeout connect 5s" in backend
    assert "option httpchk GET /v1/ready" in backend


@pytest.mark.parametrize("customized", [False, True])
def test_kolla_config_renders_effective_admission_batch_and_scanner_settings(customized):
    from jinja2 import meta

    expected = {
        "api_max_active_requests": 256,
        "api_max_sse_connections": 256,
        "api_max_websocket_connections": 64,
        "api_max_body_bytes": 220200960,
        "batch_enabled": False,
        "batch_dispatch_window": 8,
        "batch_project_dispatch_window": 32,
        "batch_native_max_items": 1000,
        "batch_native_max_bytes": 10485760,
        "batch_jsonl_max_rows": 50000,
        "batch_jsonl_max_bytes": 200000000,
        "batch_jsonl_max_line_bytes": 4194304,
        "batch_validation_chunk_rows": 100,
        "batch_validation_chunk_bytes": 1048576,
        "batch_upload_slots": 4,
        "batch_result_ttl_days": 7,
        "batch_input_ttl_days": 30,
        "batch_cancel_grace_seconds": 600,
    }
    overrides = {}
    if customized:
        expected = {key: value - 1 for key, value in expected.items() if key != "batch_enabled"}
        expected["batch_enabled"] = True
        overrides = {f"lumen_{key}": value for key, value in expected.items()}
        overrides.update(lumen_clamav_host="scanner.internal.example", lumen_clamav_port=13310)
    source = (ROLE_DIR / "templates" / "lumen.conf.j2").read_text(encoding="utf-8")
    environment = _kolla_environment(_kolla_inventory())
    names = meta.find_undeclared_variables(environment.parse(source))
    values = _resolved_kolla_defaults(names, overrides)
    config = tomllib.loads(environment.from_string(source).render(**values))
    for key, value in expected.items():
        assert config["lumen"][key] == value
        assert type(config["lumen"][key]) is type(value)
    assert config["chat"]["chat_clamav_host"] == overrides.get("lumen_clamav_host", "")
    assert config["chat"]["chat_clamav_port"] == overrides.get("lumen_clamav_port", 3310)


def test_kolla_required_assets_exist():
    assert KOLLA_DIR.exists()
    assert (REPO_ROOT / "pyproject.toml").exists()
    assert ROLE_DIR.exists()

    required_role_files = [
        "defaults/main.yml",
        "files/render_postgres_service.py",
        "files/validate_runtime_precheck.py",
        "handlers/main.yml",
        "meta/main.yml",
        "templates/lumen.conf.j2",
        "vars/main.yml",
        "tasks/main.yml",
        "tasks/deploy.yml",
        "tasks/reconfigure.yml",
        "tasks/upgrade.yml",
        "tasks/precheck.yml",
        "tasks/pull.yml",
        "tasks/config.yml",
        "tasks/bootstrap_service.yml",
        "tasks/start.yml",
        "tasks/destroy.yml",
        "tasks/loadbalancer.yml",
        "tasks/source_build.yml",
        "tasks/preconditions.yml",
        "tasks/preconditions_db.yml",
        "tasks/preconditions_keystone.yml",
        "tasks/preconditions_postgres.yml",
    ]

    for relative_path in required_role_files:
        path = ROLE_DIR / relative_path
        assert path.exists(), f"Missing required role asset: {relative_path}"


def test_kolla_yaml_and_jinja_validity():
    yaml_files = list(ROLE_DIR.glob("**/*.yml")) + list(ROLE_DIR.glob("**/*.yaml"))
    assert len(yaml_files) > 0

    for yml_file in yaml_files:
        content = yml_file.read_text(encoding="utf-8")
        parsed = yaml.safe_load(content)
        assert parsed is not None or yml_file.name == "main.yml"  # handlers/main.yml may be empty comment

    jinja_env = jinja2.Environment()
    template_path = ROLE_DIR / "templates" / "lumen.conf.j2"
    template_content = template_path.read_text(encoding="utf-8")
    parsed_ast = jinja_env.parse(template_content)
    assert parsed_ast is not None

def test_runtime_enabled_kolla_config_loads_as_typed_settings(tmp_path):
    from jinja2 import meta

    source = (ROLE_DIR / "templates" / "lumen.conf.j2").read_text(encoding="utf-8")
    environment = jinja2.Environment(undefined=jinja2.StrictUndefined)
    environment.filters.update(
        bool=bool,
        combine=lambda values, other: {**values, **other},
        to_json=json.dumps,
    )
    worker_image = "registry.example/lumen-worker@sha256:" + "b" * 64
    secret_names = ["DATABASE_URL", "REDIS_URL", "LUMEN_ENCRYPTION_KEY"]
    runtime = {
        "deployment_id": "example",
        "controller_url": "https://controller.example/v1",
        "dispatch_key": {"file": "/etc/lumen/controller/dispatch.key"},
        "tls": {
            "ca_file": "/etc/lumen/controller/ca.pem",
            "ca_key_file": "/etc/lumen/controller/ca-key.pem",
            "cert_file": "/etc/lumen/controller/server.pem",
            "key_file": "/etc/lumen/controller/server-key.pem",
            "operator_client_cert_file": "/etc/lumen/controller/worker.pem",
            "operator_client_key_file": "/etc/lumen/controller/worker-key.pem",
        },
        "managed_networks": ["10.42.0.0/24"],
        "cloud_profiles": [{
            "id": "trusted", "auth_url": "https://keystone.example/v3",
            "project_id": "trusted-project", "region_name": "RegionOne", "purpose": "trusted",
            "application_credential_id": "credential-id",
            "application_credential_secret": {"file": "/etc/lumen/controller/cloud.key"},
        }],
        "guest_profiles": [{
            "id": "worker-text", "role": "worker",
            "config_file": "/etc/lumen/controller/guest/worker-text.toml", "config_sha256": "c" * 64,
            "config_keys": ["worker_concurrency"], "secret_env_names": secret_names,
            "secrets": {name: {"file": f"/etc/lumen/controller/guest/{name.lower()}"} for name in secret_names},
            "image": worker_image, "plugin_digest": "d" * 64, "schema_version": 1, "protocol_version": 2,
        }],
        "workload_pools": {"online_text": "workers"},
        "db_connection_budget": 100, "pg_connection_budget": 10, "controller_db_connection_reserve": 2,
        "pools": [{
            "name": "workers", "role": "worker", "backend": "nova", "enabled": True,
            "cloud_profile_id": "trusted", "image": worker_image, "architecture": "aarch64",
            "network_id": "managed-net", "security_group_ids": ["worker-sg"],
            "workload_class": "online_text", "cold_service_time_ms": 30000, "guest_profile_id": "worker-text",
            "min_replicas": 1, "max_replicas": 2, "db_connection_budget": 60, "pg_connection_budget": 0,
            "profile": {"backend": "nova", "flavor_id": "flavor", "guest_image_id": "image",
                        "guest_image_hash": "a" * 64},
        }],
    }
    values = dict.fromkeys(meta.find_undeclared_variables(environment.parse(source)), "")
    values.update(
        lumen_runtime_enabled=True, lumen_runtime_config=runtime,
        lumen_controller_listen_address="10.42.0.5", lumen_controller_listen_port=8013,
        lumen_worker_concurrency=4, lumen_worker_heartbeat_seconds=5,
        lumen_worker_drain_seconds=300, lumen_chat_compat_run_timeout_seconds=300,
        lumen_api_max_active_requests=256, lumen_api_max_sse_connections=256,
        lumen_api_max_websocket_connections=64, lumen_api_max_body_bytes=220200960,
        lumen_batch_enabled=False, lumen_batch_dispatch_window=8,
        lumen_batch_project_dispatch_window=32, lumen_batch_native_max_items=1000,
        lumen_batch_native_max_bytes=10485760, lumen_batch_jsonl_max_rows=50000,
        lumen_batch_jsonl_max_bytes=200000000, lumen_batch_jsonl_max_line_bytes=4194304,
        lumen_batch_validation_chunk_rows=100, lumen_batch_validation_chunk_bytes=1048576,
        lumen_batch_upload_slots=4, lumen_batch_result_ttl_days=7,
        lumen_batch_input_ttl_days=30, lumen_batch_cancel_grace_seconds=600,
        lumen_clamav_port=3310,
    )
    config_file = tmp_path / "lumen.conf"
    config_file.write_text(environment.from_string(source).render(**values), encoding="utf-8")
    parsed = tomllib.loads(config_file.read_text(encoding="utf-8"))
    assert isinstance(parsed["lumen"]["runtime_config"], str)

    result = subprocess.run(
        [sys.executable, "-c", "import json; from lumen.config import get_settings; "
         "c = get_settings().runtime_config; "
         "print(json.dumps([c.enabled, c.listen_host, c.pools[0].cloud_profile_id]))"],
        env={"LUMEN_CONFIG_FILE": str(config_file), "PATH": os.environ.get("PATH", ""),
             "PYTHONPATH": str(REPO_ROOT)},
        capture_output=True, text=True, check=True, timeout=20,
    )
    assert json.loads(result.stdout) == [True, "10.42.0.5", "trusted"]


def test_runtime_worker_mount_excludes_controller_signing_keys():
    from jinja2.nativetypes import NativeEnvironment

    defaults = yaml.safe_load((ROLE_DIR / "defaults" / "main.yml").read_text(encoding="utf-8"))
    environment = NativeEnvironment()
    environment.filters["bool"] = bool
    render = environment.from_string
    for enabled in (False, True):
        worker_mounts = render(defaults["lumen_worker_runtime_volumes"]).render(
            lumen_runtime_enabled=enabled, lumen_worker_secrets_dir="/srv/worker-client",
        )
        mounts = render(defaults["lumen_services"]["lumen-worker"]["volumes"]).render(
            lumen_worker_runtime_volumes=worker_mounts,
        )
        assert isinstance(mounts, list)
        assert ("/srv/worker-client:/etc/lumen/controller:ro" in mounts) is enabled
        assert not any("/etc/kolla/lumen/controller" in mount for mount in mounts)


def test_kolla_package_and_image_version_contract():
    manifest = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    lock = tomllib.loads((REPO_ROOT / "uv.lock").read_text(encoding="utf-8"))
    locked_root = next(package for package in lock["package"] if package["name"] == "lumen")
    assert lumen.__version__ == manifest["project"]["version"] == locked_root["version"]


    defaults_yaml = yaml.safe_load((ROLE_DIR / "defaults" / "main.yml").read_text(encoding="utf-8"))
    assert defaults_yaml["lumen_image_tag"] == lumen.__version__

    assert defaults_yaml["lumen_source_version"] == "c561a1550921e49e6516c3e05fa89fee8457352a"

    defaults_raw = (ROLE_DIR / "defaults" / "main.yml").read_text(encoding="utf-8")
    assert "afterglow_image_tag" not in defaults_raw, "Lumen package default refers to afterglow_image_tag"
    assert defaults_yaml["lumen_encryption_key"] == "", "Lumen encryption key default must be explicit empty string"
    assert "afterglow_lumen_mcp_service_token" in defaults_raw, (
        "Lumen default must preserve MCP workload token integration"
    )
    assert defaults_yaml["lumen_chat_default_model"] == ""
    assert defaults_yaml["lumen_chat_compat_run_timeout_seconds"] == 300
    assert defaults_yaml["lumen_s3_region"] == "default"
    assert defaults_yaml["lumen_s3_server_side_encryption"] == ""
    assert defaults_yaml["lumen_s3_kms_key_id"] == ""

    assert defaults_yaml["lumen_image_namespace"] == "ghcr.io/openstack-afterglow"
    assert defaults_yaml["lumen_api_image"] == "{{ lumen_image_namespace }}/lumen-api"
    assert defaults_yaml["lumen_worker_image"] == "{{ lumen_image_namespace }}/lumen-worker"
    assert defaults_yaml["lumen_public_api_base"] == "{{ lumen_public_endpoint_url }}"
    template = (ROLE_DIR / "templates" / "lumen.conf.j2").read_text(encoding="utf-8")
    assert 'public_api_base = {{ lumen_public_api_base | to_json }}' in template
    assert 'chat_default_model = {{ lumen_chat_default_model | to_json }}' in template
    assert "chat_compat_run_timeout_seconds = {{ lumen_chat_compat_run_timeout_seconds }}" in template
    assert 'chat_asset_s3_region = {{ lumen_s3_region | to_json }}' in template
    assert 'chat_asset_s3_server_side_encryption = {{ lumen_s3_server_side_encryption | to_json }}' in template
    assert 'chat_asset_s3_kms_key_id = {{ lumen_s3_kms_key_id | to_json }}' in template


def test_bundled_postgres_binds_the_configured_host_interface():
    tasks = yaml.safe_load((ROLE_DIR / "tasks" / "preconditions_postgres.yml").read_text(encoding="utf-8"))
    container = tasks[0]["community.docker.docker_container"]
    assert container["network_mode"] == "host"
    assert "ports" not in container
    assert container["command"] == [
        "postgres",
        "-c",
        "listen_addresses={{ lumen_postgres_bind_address }}",
        "-c",
        "port={{ lumen_postgres_port }}",
    ]


def test_kolla_precheck_encryption_key_uncoupled():
    precheck_raw = (ROLE_DIR / "tasks" / "precheck.yml").read_text(encoding="utf-8")
    assert "afterglow_kubeconfig_encryption_key" not in precheck_raw, (
        "Precheck must not couple to afterglow_kubeconfig_encryption_key"
    )
    assert "lumen_encryption_key is regex('^[0-9a-fA-F]{64}$')" in precheck_raw, (
        "Precheck must require 64 hex characters fail-closed"
    )


def test_kolla_main_tasks_action_validation():
    main_tasks = yaml.safe_load((ROLE_DIR / "tasks" / "main.yml").read_text(encoding="utf-8"))
    assert len(main_tasks) == 2

    assert_task = main_tasks[0]
    include_task = main_tasks[1]

    allowed_actions = ["precheck", "pull", "deploy", "reconfigure", "upgrade", "destroy", "config"]

    # Task 1 assert check
    assert_that_str = str(assert_task["ansible.builtin.assert"]["that"])
    for action in allowed_actions:
        assert action in assert_that_str
    for unhandled in ["stop", "check", "deploy-containers", "config_validate"]:
        assert f"'{unhandled}'" not in assert_that_str

    # Task 2 include_tasks check
    when_str = str(include_task["when"])
    for action in allowed_actions:
        assert action in when_str
    for unhandled in ["stop", "check", "deploy-containers", "config_validate"]:
        assert f"'{unhandled}'" not in when_str


def test_root_package_shared_data_metadata():
    pyproject_data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    wheel_targets = pyproject_data["tool"]["hatch"]["build"]["targets"]["wheel"]
    shared_data = wheel_targets["shared-data"]

    assert shared_data.get("deploy/kolla/ansible/roles/lumen") == "share/kolla-ansible/ansible/roles/lumen"


def test_kolla_keystone_endpoint_registration():
    keystone_tasks = yaml.safe_load((ROLE_DIR / "tasks" / "preconditions_keystone.yml").read_text(encoding="utf-8"))
    assert isinstance(keystone_tasks, list)

    register_task = None
    for task in keystone_tasks:
        if task.get("name") == "Keystone | Register Lumen service, project, and user":
            register_task = task
            break

    assert register_task is not None
    vars_data = register_task["vars"]
    services = vars_data["service_ks_register_services"]
    assert len(services) == 1
    svc = services[0]
    assert svc["name"] == "lumen"
    assert svc["type"] == "lumen"

    interfaces = {ep["interface"]: ep["url"] for ep in svc["endpoints"]}
    assert interfaces["public"] == "{{ lumen_public_endpoint_url }}"
    assert interfaces["internal"] == "{{ lumen_internal_endpoint_url }}"
    assert interfaces["admin"] == "{{ lumen_admin_endpoint_url }}"


def get_included_tasks(task_file: Path) -> list[str]:
    content = yaml.safe_load(task_file.read_text(encoding="utf-8"))
    included = []
    for item in content:
        if "ansible.builtin.include_tasks" in item:
            included.append(item["ansible.builtin.include_tasks"])
    return included


def test_kolla_migration_ordering():
    for action_name in ["deploy.yml", "reconfigure.yml", "upgrade.yml"]:
        task_file = ROLE_DIR / "tasks" / action_name
        included = get_included_tasks(task_file)
        assert "bootstrap_service.yml" in included, f"{action_name} missing bootstrap_service.yml"
        assert "start.yml" in included, f"{action_name} missing start.yml"
        assert "config.yml" in included, f"{action_name} missing config.yml"

        bootstrap_idx = included.index("bootstrap_service.yml")
        config_idx = included.index("config.yml")
        assert config_idx < bootstrap_idx, f"In {action_name}, config.yml must precede bootstrap_service.yml"
        start_idx = included.index("start.yml")
        assert bootstrap_idx < start_idx, f"In {action_name}, bootstrap_service.yml must precede start.yml"


def test_kolla_provider_bootstrap_uses_inventory_environment(monkeypatch):
    defaults = yaml.safe_load((ROLE_DIR / "defaults" / "main.yml").read_text(encoding="utf-8"))
    tasks = yaml.safe_load((ROLE_DIR / "tasks" / "bootstrap_service.yml").read_text(encoding="utf-8"))
    migration = next(task for task in tasks if task["name"] == "Bootstrap | Apply Lumen database migrations")
    seed = next(task for task in tasks if task["name"] == "Bootstrap | Register configured Lumen providers")
    migration_container = migration["community.docker.docker_container"]
    seed_container = seed["community.docker.docker_container"]
    assert seed_container["command"] == ["python", "-m", "lumen.scripts.seed_providers"]
    assert seed_container["image"] == migration_container["image"] == "{{ lumen_api_image_ref }}"
    assert seed_container["volumes"] == migration_container["volumes"]
    assert seed_container["network_mode"] == migration_container["network_mode"]
    for option in ("state", "detach", "cleanup"):
        assert seed_container[option] == migration_container[option]
    assert seed["no_log"] is True
    assert seed["run_once"] is True
    assert seed["delegate_to"] == migration["delegate_to"]

    credentials = {
        "OPENAI_API_KEY": "lumen_openai_api_key",
        "GEMINI_API_KEY": "lumen_gemini_api_key",
        "LUMEN_BOOTSTRAP_MODELS_JSON": "lumen_bootstrap_models_json",
    }
    monkeypatch.setenv("OPENAI_API_KEY", "host-shell-key-must-not-leak")
    for env_name, inventory_name in credentials.items():
        assert defaults[inventory_name] == ""
        reference = "{{ " + inventory_name + " }}"
        assert seed_container["env"][env_name] == reference
        assert env_name not in migration_container["env"]
        for service in ("lumen-api", "lumen-worker"):
            assert defaults["lumen_service_environments"][service][env_name] == reference
        assert jinja2.Template(reference).render(**{inventory_name: defaults[inventory_name]}) == ""
        assert jinja2.Template(reference).render(**{inventory_name: "inventory-value"}) == "inventory-value"

    config_template = (ROLE_DIR / "templates" / "lumen.conf.j2").read_text(encoding="utf-8")
    for inventory_name in credentials.values():
        assert inventory_name not in config_template


def test_kolla_reconfigure_refresh_ordering():
    reconfigure_file = ROLE_DIR / "tasks" / "reconfigure.yml"
    included = get_included_tasks(reconfigure_file)

    expected = ["pull.yml", "precheck.yml", "config.yml", "bootstrap_service.yml", "start.yml"]
    assert included == expected, f"Reconfigure task inclusion sequence must be {expected}, got {included}"

    pull_idx = included.index("pull.yml")
    bootstrap_idx = included.index("bootstrap_service.yml")
    assert pull_idx < bootstrap_idx, (
        "Reconfigure must pull refreshed images before running bootstrap_service migrations"
    )


def test_root_wheel_contains_kolla_shared_data():
    with tempfile.TemporaryDirectory() as tmpdir:
        result = subprocess.run(
            ["uv", "build", "--wheel", "--out-dir", tmpdir],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"Wheel build failed: {result.stderr}"

        wheels = list(Path(tmpdir).glob("*.whl"))
        assert len(wheels) == 1
        wheel_path = wheels[0]
        assert wheel_path.name == f"lumen-{lumen.__version__}-py3-none-any.whl"

        with zipfile.ZipFile(wheel_path, "r") as zf:
            namelist = zf.namelist()
            assert "lumen/__init__.py" in namelist

            prefix = f"lumen-{lumen.__version__}.data/data/share/kolla-ansible/ansible/roles/lumen/"
            role_files_in_wheel = [name for name in namelist if name.startswith(prefix)]

            assert len(role_files_in_wheel) > 0
            assert f"{prefix}defaults/main.yml" in namelist
            assert f"{prefix}tasks/main.yml" in namelist
            assert f"{prefix}tasks/reconfigure.yml" in namelist
            assert f"{prefix}templates/lumen.conf.j2" in namelist
            assert f"{prefix}files/validate_runtime_precheck.py" in namelist
            assert "lumen/migrations/manifest.txt" in namelist
            for migration in (REPO_ROOT / "lumen/migrations").glob("*.sql"):
                assert f"lumen/migrations/{migration.name}" in namelist


def test_kolla_secret_isolation():
    defaults_file = ROLE_DIR / "defaults" / "main.yml"
    defaults = yaml.safe_load(defaults_file.read_text(encoding="utf-8"))

    assert "lumen_service_environments" in defaults
    service_envs = defaults["lumen_service_environments"]
    services = defaults["lumen_services"]

    container_services = {"lumen-api", "lumen-worker", "lumen-controller"}
    assert set(service_envs.keys()) == container_services
    assert set(services.keys()) == container_services

    for svc_name, svc_def in services.items():
        assert "environment" not in svc_def, f"Service {svc_name} in lumen_services must not contain 'environment'"

    serialized_services = yaml.dump(services)
    secret_keys = [
        "DATABASE_URL",
        "REDIS_URL",
        "KEYSTONE_ADMIN_PASSWORD",
        "LUMEN_ENCRYPTION_KEY",
        "LUMEN_MCP_SERVICE_TOKEN",
        "CHAT_CHECKPOINTER_POSTGRES_URL",
        "CHAT_MEMORY_PGVECTOR_URL",
        "CHAT_ASSET_S3_ACCESS_KEY",
        "CHAT_ASSET_S3_SECRET_KEY",
        "CHAT_SANDBOX_API_KEY",
    ]
    for secret_key in secret_keys:
        assert secret_key not in serialized_services, f"Secret key {secret_key} found in serialized lumen_services"

    for svc_name in container_services:
        env = service_envs[svc_name]
        for secret_key in secret_keys:
            assert secret_key in env, f"Isolated environment for {svc_name} missing {secret_key}"

    start_file = ROLE_DIR / "tasks" / "start.yml"
    start_tasks = yaml.safe_load(start_file.read_text(encoding="utf-8"))
    container_start_task = next(
        (task for task in start_tasks if task.get("name") == "Start | Start Lumen containers"),
        None,
    )
    assert container_start_task is not None, "Container start task not found in start.yml"
    assert container_start_task.get("no_log") is True, "Start containers task must have no_log: true"
    docker_container_args = container_start_task.get("community.docker.docker_container", {})
    assert docker_container_args.get("env") == "{{ lumen_service_environments[item.key] | default({}) }}", (
        "start.yml container env must reference lumen_service_environments[item.key] | default({})"
    )

    task_files = list((ROLE_DIR / "tasks").glob("*.yml"))
    for task_file in task_files:
        if task_file.name == "start.yml":
            continue
        content = task_file.read_text(encoding="utf-8")
        assert "lumen_service_environments" not in content, (
            f"Isolated map lumen_service_environments referenced in unexpected task file: {task_file.name}"
        )


def test_kolla_uses_prechecked_local_images_through_migration_and_start():
    for action in ("deploy.yml", "upgrade.yml", "reconfigure.yml"):
        includes = get_included_tasks(ROLE_DIR / "tasks" / action)
        assert includes.index("pull.yml") < includes.index("precheck.yml")
        assert includes.index("precheck.yml") < includes.index("bootstrap_service.yml")
        assert includes.index("bootstrap_service.yml") < includes.index("start.yml")
    pull = yaml.safe_load((ROLE_DIR / "tasks/pull.yml").read_text())
    assert pull[0]["ansible.builtin.include_tasks"] == "source_build.yml"
    for filename in ("bootstrap_service.yml", "start.yml", "config.yml"):
        for task in yaml.safe_load((ROLE_DIR / "tasks" / filename).read_text()):
            container = task.get("community.docker.docker_container")
            if container:
                assert container["pull"] == "never"


def test_kolla_config_escapes_operator_strings_as_toml():
    from jinja2 import meta

    source = (ROLE_DIR / "templates/lumen.conf.j2").read_text()
    environment = _kolla_environment(_kolla_inventory())
    value = 'operator "quoted" \\secret\nnext-line'
    overrides = {"lumen_keystone_password": value, "lumen_s3_secret_key": value}
    names = meta.find_undeclared_variables(environment.parse(source))
    values = _resolved_kolla_defaults(names, overrides)
    config = tomllib.loads(environment.from_string(source).render(**values))
    assert config["keystone"]["keystone_admin_password"] == value
    assert config["chat"]["chat_asset_s3_secret_key"] == value
