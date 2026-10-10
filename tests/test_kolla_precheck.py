"""Typed Kolla preflight regression definitions; no cloud inspection is mocked as proof."""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

ROLE = Path(__file__).resolve().parents[1] / "deploy/kolla/ansible/roles/lumen"
SPEC = importlib.util.spec_from_file_location("kolla_precheck", ROLE / "files/validate_runtime_precheck.py")
precheck = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(precheck)
NOW = datetime(2026, 10, 6, tzinfo=UTC)
DIGEST = "a" * 64
PLUGIN = "b" * 64


def _certificate(*, expires=NOW + timedelta(days=30), begins=NOW - timedelta(days=1), ca=True,
                 signing=True, key_identifier=True):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "precheck-test-ca")])
    builder = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
               .serial_number(x509.random_serial_number()).not_valid_before(begins).not_valid_after(expires)
               .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
               .add_extension(x509.KeyUsage(digital_signature=False, content_commitment=False,
                   key_encipherment=False, data_encipherment=False, key_agreement=False,
                   key_cert_sign=signing, crl_sign=signing, encipher_only=False, decipher_only=False), critical=True))
    if key_identifier:
        builder = builder.add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
    return builder.sign(key, hashes.SHA256()).public_bytes(serialization.Encoding.PEM)


@pytest.fixture
def deployment(tmp_path):
    tls = {name: f"/etc/lumen/controller/{filename}" for name, filename in {
        "ca_file": "ca.pem", "ca_key_file": "ca-key.pem", "cert_file": "server.pem",
        "key_file": "server-key.pem", "operator_client_cert_file": "operator.pem",
        "operator_client_key_file": "operator-key.pem",
    }.items()}
    runtime = {
        "controller_url": "https://controller.test:8013/v1", "tls": tls,
        "dispatch_key": {"file": "/etc/lumen/controller/dispatch.key"},
        "managed_networks": ["10.42.0.0/24"],
        "db_connection_budget": 500, "pg_connection_budget": 100,
        "fixed_db_connection_reserve": 30, "controller_db_connection_reserve": 10,
        "fixed_pg_connection_reserve": 10, "controller_pg_connection_reserve": 2,
        "cloud_profiles": [{
            "id": "trusted", "auth_url": "https://keystone.test/v3", "project_id": "test-project",
            "region_name": "RegionOne", "application_credential_id": "test-credential",
            "application_credential_secret": {"env": "CLOUD_SECRET"}, "purpose": "trusted",
        }],
        "pools": [], "guest_profiles": [], "workload_pools": {},
    }
    payload = {
        "runtime_enabled": True, "runtime_config": runtime, "ca_expiry_margin_seconds": 86400,
        "dynamic_ingress_vip": "10.42.0.9", "dynamic_ingress_port": 8012,
        "proxy_idle_timeout_seconds": 3600,
        "octavia_weight_zero_supported": True, "octavia_status_query_supported": True,
        "octavia_member_tags_supported": True,
        "octavia_timeout_client_data": 3600000, "octavia_timeout_member_data": 3600000,
        "batch_enabled": True, "media_enabled": True,
        "storage": {"endpoint_url": "https://s3.test", "bucket": "assets", "access_key": "test-key", "secret_key": "test-secret",
                    "server_side_encryption": "none", "kms_key_id": ""},
        "scanner": {"host": "clamav.test", "port": 3310},
        "glance_image_evidence": {}, "guest_artifact_evidence": {},
    }
    mounted = {}
    ca_path = tmp_path / "ca.pem"
    ca_path.write_bytes(_certificate())
    mounted[tls["ca_file"]] = ca_path
    # Material presence/mode checks are distinct from certificate-chain runtime
    # acceptance; synthetic key bytes here are not cloud/TLS smoke evidence.
    for container_path in [*list(tls.values())[1:], runtime["dispatch_key"]["file"]]:
        path = tmp_path / Path(container_path).name
        path.write_bytes(b"test-only-material")
        path.chmod(0o600)
        mounted[container_path] = path
    for index, workload in enumerate((None, "online_text", "online_media", "batch")):
        role = "api" if workload is None else "worker"
        name = workload or "api"
        image = f"registry.test/lumen-{name}@sha256:{DIGEST}"
        config = {"api_max_active_requests": 128} if role == "api" else {"worker_workload_classes": [workload], "worker_concurrency": 4}
        content = json.dumps(config).encode()
        path = tmp_path / f"{name}.json"
        path.write_bytes(content)
        path.chmod(0o600)
        container_path = f"/etc/lumen/controller/{name}.json"
        mounted[container_path] = path
        pool = {
            "name": name, "role": role, "backend": "nova", "enabled": True,
            "cloud_profile_id": "trusted", "image": image, "architecture": "x86_64",
            "network_id": "test-network", "security_group_ids": ["test-security-group"],
            "min_replicas": 0 if workload == "batch" else (2 if role == "api" else 1),
            "max_replicas": 3, "max_surge": 1, "guest_profile_id": name,
            "db_connection_budget": 100, "pg_connection_budget": 20,
            "db_connections_per_process": 30, "pg_connections_per_process": 4,
            "profile": {"backend": "nova", "flavor_id": "test-flavor", "guest_image_id": f"test-image-{index}", "guest_image_hash": DIGEST},
        }
        if workload:
            pool["workload_class"] = workload
            runtime["workload_pools"][workload] = name
        else:
            pool["ingress"] = {
                "ingress_pool_id": "test-octavia-pool", "ingress_vip": "10.42.0.9",
                "ingress_subnet_id": "test-subnet", "ingress_member_port": 8012,
                "api_target_active_requests": 128, "api_target_ttft_ms": 1000,
            }
        runtime["pools"].append(pool)
        names = ["DATABASE_URL", "REDIS_URL", "LUMEN_ENCRYPTION_KEY"]
        runtime["guest_profiles"].append({
            "id": name, "role": role, "image": image, "config_file": container_path,
            "config_sha256": hashlib.sha256(content).hexdigest(), "config_keys": list(config),
            "secret_env_names": names, "secrets": {name: {"env": name} for name in names},
            "plugin_digest": PLUGIN, "schema_version": 1, "protocol_version": 2,
        })
        payload["glance_image_evidence"][f"test-image-{index}"] = {
            "source": "operator", "architecture": "x86_64", "os_hash_algo": "sha256", "os_hash_value": DIGEST,
        }
        payload["guest_artifact_evidence"][image] = {
            "source": "operator", "plugin_digest": PLUGIN, "schema_version": 1, "protocol_versions": [1, 2],
        }

    def reader(container_path):
        # Real certificate/config bytes and file modes; root owner is the mounted
        # deployment metadata seam, since developer test runners are not root.
        if container_path not in mounted:
            raise FileNotFoundError(container_path)
        data, metadata = precheck._read_material(str(mounted[container_path]))
        return data, {**metadata, "uid": 0}

    return payload, reader, mounted


def _set(payload, path, value):
    target = payload
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value


@pytest.mark.parametrize(("path", "value", "reason"), [
    (("runtime_config", "pools", 0, "enabled"), False, "runtime_dynamic_ingress_pool"),
    (("runtime_config", "pools", 0, "cloud_profile_id"), "absent", "runtime_cloud_profile"),
    (("runtime_config", "cloud_profiles", 0, "purpose"), "sandbox", "runtime_cloud_profile"),
    (("runtime_config", "cloud_profiles", 0, "auth_url"), "http://keystone.test", "runtime_cloud_profile"),
    (("runtime_config", "pools", 0, "guest_profile_id"), "absent", "runtime_guest_profile"),
    (("runtime_config", "guest_profiles", 0, "role"), "worker", "runtime_guest_profile"),
    (("runtime_config", "guest_profiles", 0, "image"), "registry.test/api:latest", "runtime_oci_digest"),
    (("runtime_config", "pools", 0, "min_replicas"), 0, "runtime_replica_bounds"),
    (("runtime_config", "pools", 0, "min_replicas"), 4, "runtime_replica_bounds"),
    (("runtime_config", "pools", 0, "max_replicas"), 0, "runtime_replica_bounds"),
    (("runtime_config", "pools", 0, "max_surge"), -1, "runtime_replica_bounds"),
    (("runtime_config", "pools", 0, "max_surge"), float("inf"), "runtime_replica_bounds"),
    (("runtime_config", "pools", 0, "max_replicas"), True, "runtime_replica_bounds"),
    (("runtime_config", "workload_pools", "batch"), "online_text", "runtime_class_mapping"),
    (("runtime_config", "pools", 1, "enabled"), False, "runtime_class_mapping"),
    (("runtime_config", "pools", 0, "db_connection_budget"), 89, "runtime_db_headroom"),
    (("runtime_config", "pools", 0, "pg_connection_budget"), 11, "runtime_pg_headroom"),
    (("runtime_config", "db_connection_budget"), 439, "runtime_db_headroom"),
    (("runtime_config", "pg_connection_budget"), 91, "runtime_pg_headroom"),
    (("runtime_config", "fixed_db_connection_reserve"), -1, "runtime_db_headroom"),
    (("runtime_config", "controller_db_connection_reserve"), 0, "runtime_db_headroom"),
    (("runtime_config", "fixed_pg_connection_reserve"), 100, "runtime_pg_headroom"),
    (("runtime_config", "controller_pg_connection_reserve"), 100, "runtime_pg_headroom"),
    (("runtime_config", "controller_url"), "http://controller.test", "runtime_controller_https"),
    (("runtime_config", "controller_url"), "https://", "runtime_controller_https"),
    (("runtime_config", "controller_url"), "https://user:password@controller.test", "runtime_controller_https"),
    (("runtime_config", "unexpected"), True, "runtime_config_invalid"),
    (("ca_expiry_margin_seconds",), 0, "runtime_ca_margin"),
    (("ca_expiry_margin_seconds",), float("nan"), "runtime_ca_margin"),
    (("ca_expiry_margin_seconds",), "infinity", "runtime_ca_margin"),
    (("ca_expiry_margin_seconds",), True, "runtime_ca_margin"),
    (("octavia_weight_zero_supported",), False, "runtime_octavia_weight_zero"),
    (("octavia_weight_zero_supported",), "true", "runtime_octavia_weight_zero"),
    (("octavia_status_query_supported",), False, "runtime_octavia_status_query"),
    (("octavia_member_tags_supported",), False, "runtime_octavia_member_tags"),
    (("dynamic_ingress_vip",), "127.0.0.1;injected", "runtime_dynamic_ingress_vip"),
    (("dynamic_ingress_vip",), "10.42.0.10", "runtime_dynamic_ingress_mismatch"),
    (("dynamic_ingress_port",), 0, "runtime_dynamic_ingress_port"),
    (("dynamic_ingress_port",), 65536, "runtime_dynamic_ingress_port"),
    (("dynamic_ingress_port",), 8080, "runtime_dynamic_ingress_mismatch"),
    (("proxy_idle_timeout_seconds",), 0, "runtime_proxy_timeout"),
    (("proxy_idle_timeout_seconds",), float("inf"), "runtime_proxy_timeout"),
    (("octavia_timeout_client_data",), 30000, "runtime_proxy_timeout"),
    (("octavia_timeout_member_data",), 30000, "runtime_proxy_timeout"),
    (("storage", "endpoint_url"), "http://s3.test", "runtime_s3_required"),
    (("storage", "access_key"), "", "runtime_s3_required"),
    (("storage", "secret_key"), "", "runtime_s3_required"),
    (("scanner", "host"), "", "runtime_scanner_required"),
    (("scanner", "port"), 65536, "runtime_scanner_required"),
    (("glance_image_evidence",), {}, "runtime_glance_evidence"),
    (("glance_image_evidence", "test-image-0", "source"), "inferred", "runtime_glance_evidence"),
    (("glance_image_evidence", "test-image-0", "architecture"), "aarch64", "runtime_glance_architecture"),
    (("glance_image_evidence", "test-image-0", "os_hash_algo"), "md5", "runtime_glance_hash"),
    (("glance_image_evidence", "test-image-0", "os_hash_value"), "c" * 64, "runtime_glance_hash"),
    (("guest_artifact_evidence",), {}, "runtime_guest_artifact_evidence"),
    (("runtime_config", "guest_profiles", 0, "plugin_digest"), "c" * 64, "runtime_plugin_digest"),
    (("runtime_config", "guest_profiles", 0, "schema_version"), 2, "runtime_config_schema"),
    (("runtime_config", "guest_profiles", 0, "protocol_version"), 3, "runtime_protocol_version"),
    (("runtime_config", "guest_profiles", 0, "config_sha256"), "invalid", "runtime_config_digest"),
    (("runtime_config", "guest_profiles", 0, "config_file"), "/other/profile.json", "runtime_controller_mount"),
    (("runtime_config", "cloud_profiles", 0, "project_id"), "REPLACE_WITH_PROJECT_ID", "runtime_cloud_identity"),
    (("runtime_config", "cloud_profiles", 0, "application_credential_id"), "YOUR_CREDENTIAL_ID", "runtime_cloud_identity"),
    (("runtime_config", "pools", 0, "network_id"), "REPLACE_WITH_NETWORK_ID", "runtime_cloud_identity"),
    (("runtime_config", "pools", 0, "profile", "flavor_id"), "REPLACE_WITH_FLAVOR_ID", "runtime_cloud_identity"),
    (("runtime_config", "pools", 0, "profile", "guest_image_id"), "REPLACE_WITH_GLANCE_IMAGE_ID", "runtime_cloud_identity"),
    (("runtime_config", "pools", 0, "security_group_ids", 0), "YOUR_SECURITY_GROUP", "runtime_cloud_identity"),
    (("runtime_config", "pools", 0, "ingress", "ingress_pool_id"), "EXAMPLE_POOL", "runtime_cloud_identity"),
    (("runtime_config", "pools", 0, "security_group_ids", 0), 123, "runtime_config_invalid"),
])
def test_failure_mutations_return_stable_reasons(deployment, path, value, reason):
    payload, _, _ = deployment
    _set(payload, path, value)
    with pytest.raises(precheck.PrecheckFailure) as raised:
        precheck.validate_config(payload)
    assert reason in raised.value.reasons
    assert "password" not in str(raised.value)


def test_base_configuration_and_actual_material_succeed(deployment):
    payload, reader, _ = deployment
    config, files = precheck.validate_config(payload)
    assert config.enabled
    assert len(files) == 11
    assert config.pool("batch").min_replicas == 0
    precheck.validate_material(payload, now=NOW, read_material=reader)


def test_no_enabled_pools_fail_closed(deployment):
    payload, _, _ = deployment
    for pool in payload["runtime_config"]["pools"]:
        pool["enabled"] = False
    with pytest.raises(precheck.PrecheckFailure) as raised:
        precheck.validate_config(payload)
    assert raised.value.reasons == ["runtime_enabled_pools"]


def test_explicit_fixed_and_controller_reserves_required(deployment):
    payload, _, _ = deployment
    del payload["runtime_config"]["fixed_pg_connection_reserve"]
    with pytest.raises(precheck.PrecheckFailure, match="runtime_reserves_required"):
        precheck.validate_config(payload)


def test_proxy_and_port_validation_without_managed_runtime():
    payload = {"runtime_enabled": False, "dynamic_ingress_port": "8012", "proxy_idle_timeout_seconds": "3600"}
    assert precheck.validate_config(payload) == (None, [])
    payload["proxy_idle_timeout_seconds"] = "-1"
    with pytest.raises(precheck.PrecheckFailure, match="runtime_proxy_timeout"):
        precheck.validate_config(payload)


@pytest.mark.parametrize("feature", ["batch_enabled", "media_enabled"])
def test_fixed_batch_and_media_require_storage_and_scanner(feature):
    payload = {"runtime_enabled": False, "dynamic_ingress_port": 8012, "proxy_idle_timeout_seconds": 3600, feature: True}
    with pytest.raises(precheck.PrecheckFailure, match="runtime_s3_required"):
        precheck.validate_config(payload)
    payload["storage"] = {"endpoint_url": "https://s3.test", "bucket": "assets", "access_key": "key", "secret_key": "secret",
                          "server_side_encryption": "none"}
    with pytest.raises(precheck.PrecheckFailure, match="runtime_scanner_required"):
        precheck.validate_config(payload)
    payload["scanner"] = {"host": "scanner.test", "port": 3310}
    assert precheck.validate_config(payload) == (None, [])


@pytest.mark.parametrize(("data", "reason"), [
    (b"not-a-certificate", "runtime_ca_invalid"),
    (_certificate(expires=NOW - timedelta(seconds=1)), "runtime_ca_expiry"),
    (_certificate(expires=NOW + timedelta(seconds=86400)), "runtime_ca_expiry"),
    (_certificate(begins=NOW + timedelta(seconds=1)), "runtime_ca_invalid"),
    (_certificate(ca=False), "runtime_ca_invalid"),
    (_certificate(signing=False), "runtime_ca_invalid"),
    (_certificate(key_identifier=False), "runtime_ca_invalid"),
])
def test_ca_checks_actual_deployed_cert_bytes(deployment, data, reason):
    payload, reader, mounted = deployment
    mounted[payload["runtime_config"]["tls"]["ca_file"]].write_bytes(data)
    with pytest.raises(precheck.PrecheckFailure, match=reason):
        precheck.validate_material(payload, now=NOW, read_material=reader)


def test_ca_margin_boundary_success(deployment):
    payload, reader, mounted = deployment
    mounted[payload["runtime_config"]["tls"]["ca_file"]].write_bytes(_certificate(expires=NOW + timedelta(seconds=86401)))
    precheck.validate_material(payload, now=NOW, read_material=reader)


def test_missing_ca_cannot_be_replaced_by_inventory_evidence(deployment):
    payload, reader, mounted = deployment
    payload["material"] = {"operator_claim": "valid until 2099"}
    mounted.pop(payload["runtime_config"]["tls"]["ca_file"])
    with pytest.raises(precheck.PrecheckFailure, match="runtime_ca_invalid"):
        precheck.validate_material(payload, now=NOW, read_material=reader)


def test_actual_profile_digest_mismatch(deployment):
    payload, reader, mounted = deployment
    mounted["/etc/lumen/controller/api.json"].write_bytes(b"{}")
    with pytest.raises(precheck.PrecheckFailure, match="runtime_config_digest"):
        precheck.validate_material(payload, now=NOW, read_material=reader)


@pytest.mark.parametrize("content", [{}, {"api_max_active_requests": "many"}, ["not-a-mapping"]])
def test_actual_profile_schema_validated_not_only_digest(deployment, content):
    payload, reader, mounted = deployment
    data = json.dumps(content).encode()
    mounted["/etc/lumen/controller/api.json"].write_bytes(data)
    payload["runtime_config"]["guest_profiles"][0]["config_sha256"] = hashlib.sha256(data).hexdigest()
    with pytest.raises(precheck.PrecheckFailure, match="runtime_config_schema"):
        precheck.validate_material(payload, now=NOW, read_material=reader)


def test_delivered_worker_class_must_match_pool(deployment):
    payload, reader, mounted = deployment
    data = json.dumps({"worker_workload_classes": ["batch"], "worker_concurrency": 4}).encode()
    mounted["/etc/lumen/controller/online_text.json"].write_bytes(data)
    payload["runtime_config"]["guest_profiles"][1]["config_sha256"] = hashlib.sha256(data).hexdigest()
    with pytest.raises(precheck.PrecheckFailure, match="runtime_class_mapping"):
        precheck.validate_material(payload, now=NOW, read_material=reader)


def test_profile_permissions_are_inspected(deployment):
    payload, reader, mounted = deployment
    mounted["/etc/lumen/controller/api.json"].chmod(0o644)
    with pytest.raises(precheck.PrecheckFailure, match="runtime_guest_config_permissions"):
        precheck.validate_material(payload, now=NOW, read_material=reader)


def test_file_reader_refuses_symlinks(tmp_path):
    real = tmp_path / "real"
    real.write_bytes(b"certificate")
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(OSError):
        precheck._read_material(str(link))


def test_cli_emits_only_stable_reason_not_secret_values(deployment, monkeypatch, capsys):
    payload, _, _ = deployment
    payload["runtime_config"]["cloud_profiles"][0]["application_credential_secret"] = {"raw_secret": "NEVER_LOG_THIS"}
    monkeypatch.setattr(precheck.sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(precheck.sys, "argv", ["validate_runtime_precheck.py"])
    assert precheck.main() == 0
    output = capsys.readouterr().out
    assert "NEVER_LOG_THIS" not in output
    result = json.loads(output)
    assert result["ok"] is False
    assert result["reasons"] == ["runtime_cloud_profile"]


def _tasks(name):
    return yaml.safe_load((ROLE / "tasks" / name).read_text())


def test_deployment_wires_helper_result_into_assertions():
    tasks = _tasks("precheck.yml")
    block = next(task for task in tasks if task.get("name") == "Precheck | Validate typed runtime and deployment prerequisites")
    command = next(task for task in block["block"] if "ansible.builtin.command" in task)
    argv = command["ansible.builtin.command"]["argv"]
    assert "lumen_api_image_ref" in argv
    assert "lumen_image_tag" not in argv
    assert "--network', 'none" in argv
    assert command["no_log"] is True
    assert command["failed_when"] is False
    assertion = next(task for task in block["block"] if task.get("name") == "Precheck | Require typed prerequisites with stable reasons")
    assert assertion["ansible.builtin.assert"]["that"] == ["(lumen_runtime_precheck_result.stdout | from_json).ok"]
    assert "reasons" in assertion["ansible.builtin.assert"]["fail_msg"]
    assert block["always"][0]["ansible.builtin.file"]["state"] == "absent"


@pytest.mark.parametrize(("name", "before", "after"), [
    ("deploy.yml", "pull.yml", "precheck.yml"),
    ("reconfigure.yml", "pull.yml", "precheck.yml"),
    ("upgrade.yml", "pull.yml", "precheck.yml"),
    ("upgrade.yml", "precheck.yml", "config.yml"),
])
def test_canonical_image_available_before_typed_precheck(name, before, after):
    includes = [task["ansible.builtin.include_tasks"] for task in _tasks(name) if "ansible.builtin.include_tasks" in task]
    assert includes.index(before) < includes.index(after)


def test_ipv6_ingress_uses_canonical_address_comparison(deployment):
    payload, _, _ = deployment
    payload["dynamic_ingress_vip"] = "2001:db8::9"
    payload["runtime_config"]["pools"][0]["ingress"]["ingress_vip"] = "2001:0db8:0:0:0:0:0:9"
    precheck.validate_config(payload)


@pytest.mark.parametrize("vip", ["[2001:db8::9]", "2001:db8::9 injected", "fe80::9%unsafe zone"])
def test_non_raw_ipv6_vip_rejected(deployment, vip):
    payload, _, _ = deployment
    payload["dynamic_ingress_vip"] = vip
    with pytest.raises(precheck.PrecheckFailure, match="runtime_dynamic_ingress_vip"):
        precheck.validate_config(payload)


@pytest.mark.parametrize("extras", [[], ["timeout client 30s"], ["timeout client 3600s", "timeout tunnel 30s"],
                                      ["timeout client 3600s", "timeout client 30s"]])
def test_shared_frontend_requires_matching_client_timeout_without_backend_tunnel(deployment, extras):
    payload, _, _ = deployment
    payload.update(shared_external_frontend=True, external_single_frontend_options=extras)
    with pytest.raises(precheck.PrecheckFailure, match="runtime_proxy_shared_timeout"):
        precheck.validate_config(payload)


@pytest.mark.parametrize("extras", [
    ["timeout client 3600s"],
    ["option httplog", "timeout client 1h"],
])
def test_shared_frontend_explicit_matching_timeouts_succeed(deployment, extras):
    payload, _, _ = deployment
    payload.update(shared_external_frontend=True, external_single_frontend_options=extras)
    precheck.validate_config(payload)


def test_nested_profile_is_covered_by_recursive_controller_mount(deployment, tmp_path):
    payload, reader, mounted = deployment
    profile = payload["runtime_config"]["guest_profiles"][1]
    old_path = profile["config_file"]
    new_path = "/etc/lumen/controller/guest/worker-text.toml"
    folder = tmp_path / "guest"
    folder.mkdir()
    path = folder / "worker-text.toml"
    data = b'worker_workload_classes = ["online_text"]\nworker_concurrency = 4\n'
    path.write_bytes(data)
    path.chmod(0o600)
    mounted.pop(old_path)
    mounted[new_path] = path
    profile.update(config_file=new_path, config_sha256=hashlib.sha256(data).hexdigest())
    precheck.validate_material(payload, now=NOW, read_material=reader)


@pytest.mark.parametrize("path", ["/etc/lumen/controller/guest/../api.json", "/etc/lumen/controller/guest/./api.json",
                                  "/etc/lumen/controller//api.json", "/etc/lumen/controller/guest/"])
def test_nested_profile_rejects_ambiguous_path_segments(deployment, path):
    payload, _, _ = deployment
    payload["runtime_config"]["guest_profiles"][0]["config_file"] = path
    with pytest.raises(precheck.PrecheckFailure, match="runtime_controller_mount"):
        precheck.validate_config(payload)


def test_parent_symlink_cannot_escape_controller_mount(tmp_path):
    root = tmp_path / "controller"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "profile.json").write_bytes(b"{}")
    (root / "guest").symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError, match="parent escapes controller mount"):
        precheck._read_material(str(root / "guest/profile.json"), mount_root=str(root))


def _actual_shared_frontend(inventory):
    import jinja2
    from jinja2.nativetypes import NativeEnvironment

    block = next(task for task in _tasks("precheck.yml") if task.get("name") == "Precheck | Validate typed runtime and deployment prerequisites")
    command = next(task for task in block["block"] if "ansible.builtin.command" in task)
    expression = command["vars"]["lumen_precheck_input"]["external_single_frontend_options"]
    environment = NativeEnvironment(undefined=jinja2.StrictUndefined)
    options = environment.from_string(expression).render(**inventory)
    environment.filters["bool"] = lambda value: value in (True, "yes", "true")
    fixture = ROLE.parents[4] / "tests/fixtures/kolla/haproxy_external_frontend.cfg.j2"
    rendered = environment.from_string(fixture.read_text()).render(
        kolla_enable_tls_external="yes", kolla_external_vip_address="192.0.2.10",
        haproxy_single_external_frontend_public_port=443,
        haproxy_external_single_frontend_default_backend="horizon_external_back",
        **inventory,
    )
    return options, rendered


def test_unrelated_frontend_extra_cannot_satisfy_real_shared_timeouts(deployment):
    payload, _, _ = deployment
    # These are the actual pinned loadbalancer defaults (main.yml at commit
    # 570f2b77142d7145e55f39126e3987002afe7494): shared options include a 6h
    # Glance client timeout, no tunnel timeout. The ordinary frontend extras
    # are NOT read by the shared external template.
    options, rendered = _actual_shared_frontend({
        "haproxy_external_single_frontend_options": ["option httplog", "option forwardfor", "timeout client 6h"],
        "haproxy_frontend_http_extra": ["timeout client 3600s", "timeout tunnel 3600s"],
    })
    assert "timeout client 6h" in rendered
    assert "timeout client 3600s" not in rendered
    assert "timeout tunnel 3600s" not in rendered
    payload.update(shared_external_frontend=True, external_single_frontend_options=options)
    with pytest.raises(precheck.PrecheckFailure, match="runtime_proxy_shared_timeout"):
        precheck.validate_config(payload)


def test_real_shared_frontend_option_override_renders_and_validates(deployment):
    payload, _, _ = deployment
    options, rendered = _actual_shared_frontend({
        "haproxy_external_single_frontend_options": ["option httplog", "option forwardfor", "timeout client 3600s"],
        "haproxy_frontend_http_extra": ["timeout client 30s", "timeout tunnel 30s"],
    })
    assert "timeout client 3600s" in rendered
    assert "timeout tunnel" not in rendered
    assert "timeout client 30s" not in rendered
    payload.update(shared_external_frontend=True, external_single_frontend_options=options)
    precheck.validate_config(payload)


@pytest.mark.parametrize("storage", [
    {}, {"server_side_encryption": ""}, {"server_side_encryption": "invalid"},
    {"server_side_encryption": "aws:kms", "kms_key_id": ""},
])
def test_storage_encryption_is_explicit_and_kms_requires_a_key(deployment, storage):
    payload, _, _ = deployment
    payload["storage"].pop("server_side_encryption")
    payload["storage"].update(storage)
    with pytest.raises(precheck.PrecheckFailure, match="runtime_s3_encryption_required"):
        precheck.validate_config(payload)


@pytest.mark.parametrize("content", [
    {"batch_dispatch_window": 33, "batch_project_dispatch_window": 32},
    {"batch_enabled": True, "api_max_body_bytes": 100},
    {"chat_execution_protocol_version": 3},
])
def test_guest_profile_combined_settings_must_be_coherent(deployment, content):
    payload, reader, mounted = deployment
    content = {"api_max_active_requests": 128, **content}
    data = json.dumps(content).encode()
    mounted["/etc/lumen/controller/api.json"].write_bytes(data)
    payload["runtime_config"]["guest_profiles"][0].update(
        config_keys=list(content), config_sha256=hashlib.sha256(data).hexdigest(),
    )
    with pytest.raises(precheck.PrecheckFailure, match="runtime_config_schema"):
        precheck.validate_material(payload, now=NOW, read_material=reader)


@pytest.mark.parametrize("mutation", ["missing", "empty", "world-readable"])
def test_controller_private_material_is_required_and_restricted(deployment, mutation):
    payload, reader, mounted = deployment
    key = payload["runtime_config"]["tls"]["ca_key_file"]
    if mutation == "missing":
        mounted.pop(key)
    elif mutation == "empty":
        mounted[key].write_bytes(b"")
    else:
        mounted[key].chmod(0o644)
    with pytest.raises(precheck.PrecheckFailure, match="runtime_tls_material"):
        precheck.validate_material(payload, now=NOW, read_material=reader)
