"""Tests for Lumen Kolla-Ansible packaging and lifecycle assets."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from pathlib import Path

import jinja2
import yaml

import lumen

REPO_ROOT = Path(__file__).parent.parent
KOLLA_DIR = REPO_ROOT / "deploy" / "kolla"
ROLE_DIR = KOLLA_DIR / "ansible" / "roles" / "lumen"


def test_kolla_required_assets_exist():
    assert KOLLA_DIR.exists()
    assert (REPO_ROOT / "pyproject.toml").exists()
    assert ROLE_DIR.exists()

    required_role_files = [
        "defaults/main.yml",
        "files/render_postgres_service.py",
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
        "pools": [{
            "name": "workers", "role": "worker", "backend": "nova", "enabled": True,
            "cloud_profile_id": "trusted", "image": "worker-image", "architecture": "aarch64",
            "network_id": "managed-net", "security_group_ids": ["worker-sg"],
            "min_replicas": 1, "max_replicas": 2, "db_connection_budget": 20,
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
    assert lumen.__version__ == "0.2.2"

    sdk_init = (REPO_ROOT / "sdk" / "lumen_sdk" / "__init__.py").read_text(encoding="utf-8")
    assert '__version__ = "0.2.1"' in sdk_init

    defaults_yaml = yaml.safe_load((ROLE_DIR / "defaults" / "main.yml").read_text(encoding="utf-8"))

    # The root package revision does not imply a new published runtime image.
    assert defaults_yaml["lumen_image_tag"] == "0.2.1"
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
    assert 'public_api_base = "{{ lumen_public_api_base }}"' in template
    assert 'chat_default_model = "{{ lumen_chat_default_model }}"' in template
    assert "chat_compat_run_timeout_seconds = {{ lumen_chat_compat_run_timeout_seconds }}" in template
    assert 'chat_asset_s3_region = "{{ lumen_s3_region }}"' in template
    assert 'chat_asset_s3_server_side_encryption = "{{ lumen_s3_server_side_encryption }}"' in template
    assert 'chat_asset_s3_kms_key_id = "{{ lumen_s3_kms_key_id }}"' in template


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


def test_kolla_reconfigure_refresh_ordering():
    reconfigure_file = ROLE_DIR / "tasks" / "reconfigure.yml"
    included = get_included_tasks(reconfigure_file)

    expected = ["precheck.yml", "pull.yml", "config.yml", "bootstrap_service.yml", "start.yml"]
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
