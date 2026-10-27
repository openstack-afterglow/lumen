"""Typed Kolla preflight regression definitions; no cloud inspection is mocked as proof."""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from lumen.services.infrastructure.config import PoolConfig

ROLE = Path(__file__).resolve().parents[1] / "deploy/kolla/ansible/roles/lumen"
SPEC = importlib.util.spec_from_file_location("kolla_precheck", ROLE / "files/validate_runtime_precheck.py")
precheck = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(precheck)
NOW = datetime(2026, 10, 6, tzinfo=UTC)
DIGEST = "a" * 64
PLUGIN = "b" * 64


def _certificate(*, expires=NOW + timedelta(days=30), begins=NOW - timedelta(days=1), ca=True,
                 signing=True, key_identifier=True, key=None):
    key = key or ec.generate_private_key(ec.SECP256R1())
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


def _signed_worker(ca, ca_key, *, uri="spiffe://lumen/fixed-worker/worker-a",
                   begins=NOW - timedelta(days=1), expires=NOW + timedelta(days=7),
                   client_auth=True):
    key = ec.generate_private_key(ec.SECP256R1())
    certificate = (x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "fixed-worker-test")]))
        .issuer_name(ca.subject).public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(begins).not_valid_after(expires)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName([x509.UniformResourceIdentifier(uri)]), critical=False)
        .add_extension(x509.ExtendedKeyUsage([
            ExtendedKeyUsageOID.CLIENT_AUTH if client_auth else ExtendedKeyUsageOID.SERVER_AUTH,
        ]), critical=False).sign(ca_key, hashes.SHA256()))
    return (certificate.public_bytes(serialization.Encoding.PEM),
            key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                              serialization.NoEncryption()))


def _mounted_reader(mounted, overrides=None):
    def reader(container_path):
        if container_path not in mounted:
            raise FileNotFoundError(container_path)
        data, metadata = precheck._read_material(str(mounted[container_path]))
        return data, {**metadata, "uid": 0, "gid": 0, **(overrides or {}).get(container_path, {})}
    return reader


def _worker_directory_reader(root, overrides=None):
    walk = precheck._inspect_worker_directory
    def inspect():
        # Exercise the production non-following walk against real local files;
        # only deployment uid/gid are injected for non-root developer runners.
        return {
            "/etc/lumen/worker-precheck" + path.removeprefix(str(root)):
                {**metadata, "uid": 0, "gid": 0, **(overrides or {}).get(path, {})}
            for path, metadata in walk(str(root)).items()
        }
    return inspect


@pytest.fixture
def kubernetes_deployment(deployment, tmp_path):
    """Local bytes/metadata only: no claim of cluster, CNI or TLS smoke evidence."""
    payload, _, mounted = deployment
    runtime = payload["runtime_config"]
    runtime.update(deployment_id="lumen", cloud_profiles=[], guest_profiles=[], workload_pools={})
    payload.update(worker_enabled=True, dynamic_ingress_vip="", batch_enabled=False, media_enabled=False,
                   glance_image_evidence={}, guest_artifact_evidence={})
    tls = runtime["tls"]
    tls.update(worker_client_cert_file="/etc/lumen/controller/worker.pem",
               worker_client_key_file="/etc/lumen/controller/worker-key.pem")
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_bytes = _certificate(key=ca_key)
    ca = x509.load_pem_x509_certificate(ca_bytes)
    leaf, key = _signed_worker(ca, ca_key)
    server, server_key = _signed_worker(ca, ca_key, uri="spiffe://lumen/controller", client_auth=False)
    operator, operator_key = _signed_worker(ca, ca_key, uri="spiffe://lumen/operator/probe")
    runtime["fixed_workers"] = [{"id": "worker-a", "certificate_fingerprint":
                                x509.load_pem_x509_certificate(leaf).fingerprint(hashes.SHA256()).hex()}]
    manifest = b'{"version":1,"profiles":[]}'
    cluster = {
        "id": "cluster", "server": "https://kubernetes.test:6443",
        "ca_file": "/etc/lumen/controller/kubernetes/kube-api-ca.pem",
        "bearer_token": {"file": "/etc/lumen/controller/kubernetes/token.env"},
        "namespace": "runs", "runtime_class_name": "sandbox", "runtime_class_handler": "runsc",
        "cluster_cidrs": ["10.42.0.0/16", "10.43.0.0/16"],
        "qualification_file": "/etc/lumen/controller/kubernetes/qualification.json",
    }
    pool = {
        "name": "mcp", "role": "sandbox", "backend": "kubernetes", "enabled": True,
        "image": f"registry.test/sandbox@sha256:{DIGEST}", "architecture": "x86_64",
        "min_replicas": 0, "max_replicas": 5,
        "profile": {"backend": "kubernetes", "cluster_profile_id": "cluster",
                    "tool_manifest_file": "/etc/lumen/controller/kubernetes/manifest.json",
                    "tool_manifest_sha256": hashlib.sha256(manifest).hexdigest(),
                    "controller_ingress_cidrs": ["192.168.50.10/32"],
                    "bootstrap_egress_cidrs": ["192.168.50.11/32"]},
        "sandbox": {"workspace_bytes": 16 * 1024**2, "memory_bytes": 256 * 1024**2,
                    "cpu_millis": 1000, "pids_limit": 128},
    }
    qualification = {
        "version": 1, "server": cluster["server"], "namespace": cluster["namespace"],
        "namespace_uid": "00000000-0000-0000-0000-000000000001",
        "runtime_class_name": cluster["runtime_class_name"],
        "runtime_class_handler": cluster["runtime_class_handler"],
        "runtime_class_uid": "00000000-0000-0000-0000-000000000002",
        "runtime_class_spec_sha256": hashlib.sha256(b'{"handler":"runsc"}').hexdigest(),
        "cluster_cidrs": cluster["cluster_cidrs"],
        "pool_digest": PoolConfig.model_validate_json(json.dumps(pool), strict=True).digest(),
        "runtime_probe_sha256": "d" * 64, "cni_probe_sha256": "e" * 64, "measured_pids_max": 128,
    }
    qualification_bytes = json.dumps(qualification).encode()
    cluster["qualification_sha256"] = hashlib.sha256(qualification_bytes).hexdigest()
    runtime.update(pools=[pool], kubernetes_clusters=[cluster])
    contents = {
        tls["ca_file"]: ca_bytes,
        tls["ca_key_file"]: ca_key.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption()),
        tls["worker_client_cert_file"]: leaf, tls["worker_client_key_file"]: key,
        tls["cert_file"]: server, tls["key_file"]: server_key,
        tls["operator_client_cert_file"]: operator, tls["operator_client_key_file"]: operator_key,
        cluster["ca_file"]: _certificate(), cluster["bearer_token"]["file"]: b"controller-only-token\n",
        cluster["qualification_file"]: qualification_bytes, pool["profile"]["tool_manifest_file"]: manifest,
    }
    for index, (container_path, data) in enumerate(contents.items()):
        if container_path in {tls["worker_client_cert_file"], tls["worker_client_key_file"]}:
            continue
        path = tmp_path / f"kubernetes-{index}"
        path.write_bytes(data)
        path.chmod(0o600)
        mounted[container_path] = path
    worker_mounted = {}
    worker_root = tmp_path / "worker"
    worker_root.mkdir(mode=0o750)
    worker_root.chmod(0o750)
    for field in ("ca_file", "worker_client_cert_file", "worker_client_key_file"):
        path = worker_root / Path(tls[field]).name
        path.write_bytes(contents[tls[field]])
        path.chmod(0o640 if field == "worker_client_key_file" else 0o644)
        worker_mounted[f"/etc/lumen/worker-precheck/{Path(tls[field]).name}"] = path
    manifest_directory = worker_root / "kubernetes"
    manifest_directory.mkdir(mode=0o750)
    manifest_directory.chmod(0o750)
    manifest_copy = manifest_directory / "manifest.json"
    manifest_copy.write_bytes(manifest)
    manifest_copy.chmod(0o644)
    worker_mounted["/etc/lumen/worker-precheck/kubernetes/manifest.json"] = manifest_copy
    return SimpleNamespace(payload=payload, runtime=runtime, tls=tls, cluster=cluster, pool=pool,
                           mounted=mounted, worker_mounted=worker_mounted, worker_root=worker_root,
                           reader=_mounted_reader(mounted), worker_reader=_mounted_reader(worker_mounted),
                           inspect_worker_directory=_worker_directory_reader(worker_root), ca=ca, ca_key=ca_key)


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
    assert "worker_enabled" not in payload
    assert "worker_client_cert_file" not in payload["runtime_config"]["tls"]
    assert "worker_client_key_file" not in payload["runtime_config"]["tls"]
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


@pytest.mark.parametrize(("runtime_enabled", "worker_enabled"), [(False, False), (True, False), (True, True)])
def test_deployment_wires_helper_result_into_assertions(runtime_enabled, worker_enabled):
    from jinja2.nativetypes import NativeEnvironment

    tasks = _tasks("precheck.yml")
    block = next(task for task in tasks if task.get("name") == "Precheck | Validate typed runtime and deployment prerequisites")
    command = next(task for task in block["block"] if "ansible.builtin.command" in task)
    environment = NativeEnvironment()
    environment.filters.update(bool=bool, from_json=json.loads)
    context = {"lumen_runtime_enabled": runtime_enabled, "enable_lumen_worker": worker_enabled,
               "lumen_precheck_script": {"path": "/tmp/validator.py"},
               "lumen_controller_secrets_dir": "/srv/controller-only",
               "lumen_worker_secrets_dir": "/srv/worker-client",
               "lumen_api_image_ref": f"registry.test/lumen@sha256:{DIGEST}"}
    argv = environment.from_string(command["ansible.builtin.command"]["argv"]).render(**context)
    assert argv[argv.index("--network") + 1] == "none"
    assert argv[-3:] == [context["lumen_api_image_ref"], "/precheck.py", "material"]
    assert [argv[index + 1] for index, value in enumerate(argv) if value == "--volume"] == [
        "/tmp/validator.py:/precheck.py:ro",
        *(["/srv/controller-only:/etc/lumen/controller:ro"] if runtime_enabled else []),
        *(["/srv/worker-client:/etc/lumen/worker-precheck:ro"] if runtime_enabled and worker_enabled else []),
    ]
    assert environment.from_string(command["vars"]["lumen_precheck_input"]["worker_enabled"]).render(
        **context) is worker_enabled
    assert command["no_log"] is True
    assert command["failed_when"] is False
    assertion = next(task for task in block["block"] if task.get("name") == "Precheck | Require typed prerequisites with stable reasons")
    for ok in (False, True):
        result = {"lumen_runtime_precheck_result": {"stdout": json.dumps({"ok": ok, "reasons": ["runtime_worker_identity"]})}}
        assert all(environment.compile_expression(condition)(**result)
                   for condition in assertion["ansible.builtin.assert"]["that"]) is ok
        assert environment.from_string(assertion["ansible.builtin.assert"]["fail_msg"]).render(**result) == "runtime_worker_identity"
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


def _kubernetes_path(deployment, field):
    if field == "bearer_token":
        return deployment.cluster[field]["file"]
    if field == "tool_manifest_file":
        return deployment.pool["profile"][field]
    return deployment.cluster[field]


def _set_kubernetes_path(deployment, field, path):
    if field == "bearer_token":
        deployment.cluster[field] = {"file": path}
    elif field == "tool_manifest_file":
        deployment.pool["profile"][field] = path
    else:
        deployment.cluster[field] = path


@pytest.mark.parametrize("field", ["ca_file", "bearer_token", "qualification_file", "tool_manifest_file"])
@pytest.mark.parametrize("path", [
    "/tmp/material", "/etc/lumen/worker-precheck/material", "/etc/lumen/controller/../material",
    "/etc/lumen/controller/kubernetes/./material", "/etc/lumen/controller//material",
    "/etc/lumen/controller/kubernetes/",
])
def test_kubernetes_local_references_require_canonical_controller_paths(kubernetes_deployment, field, path):
    _set_kubernetes_path(kubernetes_deployment, field, path)
    with pytest.raises(precheck.PrecheckFailure, match="runtime_kubernetes_mount"):
        precheck.validate_config(kubernetes_deployment.payload)


def test_kubernetes_token_environment_reference_cannot_bypass_controller_mount(kubernetes_deployment, monkeypatch):
    monkeypatch.setenv("KUBERNETES_TOKEN", "NEVER_LOG_CONTROLLER_TOKEN")
    kubernetes_deployment.cluster["bearer_token"] = {"env": "KUBERNETES_TOKEN"}
    with pytest.raises(precheck.PrecheckFailure, match="runtime_kubernetes_mount") as denied:
        precheck.validate_config(kubernetes_deployment.payload)
    assert "NEVER_LOG_CONTROLLER_TOKEN" not in str(denied.value)


@pytest.mark.parametrize("field", ["ca_file", "bearer_token", "qualification_file", "tool_manifest_file"])
@pytest.mark.parametrize("reserved", [
    "ca_file", "ca_key_file", "cert_file", "key_file", "operator_client_cert_file",
    "operator_client_key_file", "worker_client_cert_file", "worker_client_key_file", "dispatch",
])
def test_kubernetes_files_cannot_alias_controller_or_worker_identity(kubernetes_deployment, field, reserved):
    d = kubernetes_deployment
    path = d.runtime["dispatch_key"]["file"] if reserved == "dispatch" else d.tls[reserved]
    _set_kubernetes_path(d, field, path)
    with pytest.raises(precheck.PrecheckFailure, match="runtime_kubernetes_mount"):
        precheck.validate_config(d.payload)


@pytest.mark.parametrize(("field", "other"), [
    ("ca_file", "bearer_token"), ("ca_file", "qualification_file"),
    ("ca_file", "tool_manifest_file"), ("bearer_token", "qualification_file"),
    ("bearer_token", "tool_manifest_file"), ("qualification_file", "tool_manifest_file"),
])
def test_kubernetes_credential_and_artifact_files_cannot_alias_each_other(kubernetes_deployment, field, other):
    d = kubernetes_deployment
    _set_kubernetes_path(d, field, _kubernetes_path(d, other))
    with pytest.raises(precheck.PrecheckFailure, match="runtime_kubernetes_mount"):
        precheck.validate_config(d.payload)


@pytest.mark.parametrize(("path", "value"), [
    (("runtime_config", "pools", 0, "min_replicas"), 1),
    (("runtime_config", "pools", 0, "max_replicas"), 0),
    (("runtime_config", "pools", 0, "image"), "registry.test/sandbox:latest"),
    (("runtime_config", "pools", 0, "network_id"), "nova-network"),
    (("runtime_config", "pools", 0, "cloud_profile_id"), "nova-cloud"),
    (("runtime_config", "pools", 0, "security_group_ids"), ["nova-security-group"]),
    (("runtime_config", "pools", 0, "profile", "controller_ingress_cidrs"), ["0.0.0.0/0"]),
    (("runtime_config", "pools", 0, "profile", "egress"), "public"),
    (("runtime_config", "pools", 0, "sandbox", "workspace_bytes"), 128 * 1024**2),
    (("runtime_config", "kubernetes_clusters", 0, "server"), "http://kubernetes.test:6443"),
    (("runtime_config", "kubernetes_clusters", 0, "runtime_class_handler"), "runc"),
])
def test_kubernetes_only_kolla_inputs_keep_typed_isolation_constraints(kubernetes_deployment, path, value):
    _set(kubernetes_deployment.payload, path, value)
    with pytest.raises(precheck.PrecheckFailure):
        precheck.validate_config(kubernetes_deployment.payload)


def _validate_kubernetes(deployment, *, controller_reader=None, worker_reader=None, inspect_worker_directory=None):
    controller_reader = controller_reader or deployment.reader
    worker_reader = worker_reader or deployment.worker_reader

    def reader(path):
        if path.startswith("/etc/lumen/worker-precheck/"):
            return worker_reader(path)
        return controller_reader(path)

    precheck.validate_material(deployment.payload, now=NOW, read_material=reader,
                              inspect_worker_directory=inspect_worker_directory or deployment.inspect_worker_directory)


def test_kubernetes_only_configuration_and_actual_material_succeed(kubernetes_deployment):
    d = kubernetes_deployment
    from jinja2.nativetypes import NativeEnvironment

    block = next(task for task in _tasks("precheck.yml") if "block" in task
                 and any("ansible.builtin.command" in child for child in task["block"]))
    command = next(task for task in block["block"] if "ansible.builtin.command" in task)
    inputs = command["vars"]["lumen_precheck_input"]
    environment = NativeEnvironment()
    environment.filters["bool"] = bool
    context = {"lumen_runtime_enabled": True, "enable_lumen_worker": True, "lumen_runtime_config": d.runtime}
    for field in ("runtime_enabled", "worker_enabled", "runtime_config"):
        d.payload[field] = environment.from_string(inputs[field]).render(**context)
    config, files = precheck.validate_config(d.payload)
    assert config.enabled and d.payload["worker_enabled"] is True
    assert config.cloud_profiles == config.guest_profiles == ()
    assert config.workload_pools == {}
    assert config.pool("mcp").backend == "kubernetes"
    expected = {d.tls[field] for field in (
        "ca_file", "ca_key_file", "cert_file", "key_file", "operator_client_cert_file", "operator_client_key_file",
    )} | {d.runtime["dispatch_key"]["file"]} | {
        _kubernetes_path(d, field) for field in ("ca_file", "bearer_token", "qualification_file", "tool_manifest_file")
    }
    assert {entry["path"] for entry in files} == expected
    requested = []

    def worker_reader(path):
        requested.append(path)
        return d.worker_reader(path)

    _validate_kubernetes(d, worker_reader=worker_reader)
    assert set(requested) == set(d.worker_mounted) == {
        "/etc/lumen/worker-precheck/ca.pem", "/etc/lumen/worker-precheck/worker.pem",
        "/etc/lumen/worker-precheck/worker-key.pem",
        "/etc/lumen/worker-precheck/kubernetes/manifest.json",
    }
    assert all("token.env" not in path for path in requested)
    assert d.tls["worker_client_cert_file"] not in d.mounted
    assert d.tls["worker_client_key_file"] not in d.mounted


@pytest.mark.parametrize(("field", "reason"), [
    ("qualification_sha256", "runtime_kubernetes_qualification"),
    ("tool_manifest_sha256", "runtime_kubernetes_manifest"),
])
@pytest.mark.parametrize("pin", [None, "", "not-a-digest", "A" * 64, "a" * 63])
def test_kubernetes_missing_or_malformed_artifact_pins_rejected(kubernetes_deployment, field, reason, pin):
    d = kubernetes_deployment
    target = d.cluster if field == "qualification_sha256" else d.pool["profile"]
    if pin is None:
        target.pop(field)
    else:
        target[field] = pin
    with pytest.raises(precheck.PrecheckFailure, match=reason):
        precheck.validate_config(d.payload)


@pytest.mark.parametrize("field", ["ca_file", "bearer_token", "qualification_file", "tool_manifest_file"])
@pytest.mark.parametrize("fault", ["missing", "owner", "symlink", "not-regular", "writable"])
def test_kubernetes_mounted_files_require_real_restricted_material(kubernetes_deployment, field, fault):
    d = kubernetes_deployment
    path = _kubernetes_path(d, field)
    overrides = {}
    if fault == "missing":
        d.mounted.pop(path)
    elif fault == "writable":
        d.mounted[path].chmod(0o666)
    else:
        key, value = {"owner": ("uid", 1000), "symlink": ("islnk", True),
                      "not-regular": ("isreg", False)}[fault]
        overrides[path] = {key: value}
    with pytest.raises(precheck.PrecheckFailure, match="runtime_kubernetes_material"):
        _validate_kubernetes(d, controller_reader=_mounted_reader(d.mounted, overrides))


@pytest.mark.parametrize("field", ["bearer_token", "qualification_file", "tool_manifest_file"])
def test_kubernetes_controller_only_files_cannot_be_group_readable(kubernetes_deployment, field):
    d = kubernetes_deployment
    d.mounted[_kubernetes_path(d, field)].chmod(0o640)
    with pytest.raises(precheck.PrecheckFailure, match="runtime_kubernetes_material"):
        _validate_kubernetes(d)


@pytest.mark.parametrize("data", [
    b"not-a-ca", _certificate(ca=False), _certificate(expires=NOW),
    _certificate(begins=NOW + timedelta(seconds=1)),
])
def test_kubernetes_ca_is_validated_from_mounted_bytes(kubernetes_deployment, data):
    d = kubernetes_deployment
    d.mounted[d.cluster["ca_file"]].write_bytes(data)
    with pytest.raises(precheck.PrecheckFailure, match="runtime_kubernetes_material"):
        _validate_kubernetes(d)


@pytest.mark.parametrize("data", [b"", b" \n", b"two tokens", b"token\ninjected", b"\xff", b"x" * 16385])
def test_kubernetes_token_env_file_must_contain_one_bounded_token(kubernetes_deployment, data):
    d = kubernetes_deployment
    d.mounted[d.cluster["bearer_token"]["file"]].write_bytes(data)
    with pytest.raises(precheck.PrecheckFailure, match="runtime_kubernetes_material") as denied:
        _validate_kubernetes(d)
    assert "injected" not in str(denied.value)


@pytest.mark.parametrize(("field", "reason"), [
    ("qualification_file", "runtime_kubernetes_qualification"),
    ("tool_manifest_file", "runtime_kubernetes_manifest"),
])
@pytest.mark.parametrize("fault", ["changed", "empty", "oversized"])
def test_kubernetes_artifact_pins_are_checked_against_actual_bytes(kubernetes_deployment, field, reason, fault):
    d = kubernetes_deployment
    data = {"changed": b"changed-bytes", "empty": b"",
            "oversized": b"x" * (65537 if field == "qualification_file" else 1048577)}[fault]
    d.mounted[_kubernetes_path(d, field)].write_bytes(data)
    if fault != "changed":
        target, digest = ((d.cluster, "qualification_sha256") if field == "qualification_file"
                          else (d.pool["profile"], "tool_manifest_sha256"))
        target[digest] = hashlib.sha256(data).hexdigest()
        if field == "tool_manifest_file":
            qualification_path = d.mounted[d.cluster["qualification_file"]]
            report = json.loads(qualification_path.read_bytes())
            report["pool_digest"] = PoolConfig.model_validate_json(json.dumps(d.pool), strict=True).digest()
            qualification = json.dumps(report).encode()
            qualification_path.write_bytes(qualification)
            d.cluster["qualification_sha256"] = hashlib.sha256(qualification).hexdigest()
    with pytest.raises(precheck.PrecheckFailure, match=reason):
        _validate_kubernetes(d)


@pytest.mark.parametrize(("field", "value"), [
    ("version", 2), ("server", "https://foreign-cluster.test:6443"), ("namespace", "foreign"),
    ("namespace_uid", ""), ("runtime_class_uid", ""), ("runtime_class_name", "other"),
    ("runtime_class_handler", "runc"), ("cluster_cidrs", ["192.168.0.0/16"]),
    ("pool_digest", "f" * 64), ("runtime_probe_sha256", None), ("cni_probe_sha256", "bad"),
    ("runtime_class_spec_sha256", "bad"), ("measured_pids_max", 0), ("measured_pids_max", 129),
    ("measured_pids_max", True),
])
def test_kubernetes_pinned_qualification_must_bind_cluster_pool_and_limits(kubernetes_deployment, field, value):
    d = kubernetes_deployment
    path = d.mounted[d.cluster["qualification_file"]]
    report = json.loads(path.read_bytes())
    report[field] = value
    data = json.dumps(report).encode()
    path.write_bytes(data)
    d.cluster["qualification_sha256"] = hashlib.sha256(data).hexdigest()
    with pytest.raises(precheck.PrecheckFailure, match="runtime_kubernetes_qualification"):
        _validate_kubernetes(d)


@pytest.mark.parametrize("data", [b"not-json", b"[]", b"null"])
def test_kubernetes_qualification_digest_does_not_replace_content_validation(kubernetes_deployment, data):
    d = kubernetes_deployment
    d.mounted[d.cluster["qualification_file"]].write_bytes(data)
    d.cluster["qualification_sha256"] = hashlib.sha256(data).hexdigest()
    with pytest.raises(precheck.PrecheckFailure, match="runtime_kubernetes_qualification"):
        _validate_kubernetes(d)


@pytest.mark.parametrize("fault", ["missing-pair", "missing-cert", "missing-key", "unapproved", "misbound-id"])
def test_enabled_fixed_worker_requires_separate_approved_identity_configuration(kubernetes_deployment, fault):
    d = kubernetes_deployment
    if fault == "missing-pair":
        d.tls.pop("worker_client_cert_file")
        d.tls.pop("worker_client_key_file")
    elif fault in {"missing-cert", "missing-key"}:
        d.tls.pop("worker_client_cert_file" if fault == "missing-cert" else "worker_client_key_file")
    elif fault == "unapproved":
        d.runtime["fixed_workers"] = []
    else:
        d.runtime["fixed_workers"][0]["id"] = "worker-b"
    with pytest.raises(precheck.PrecheckFailure, match="runtime_worker_identity"):
        _validate_kubernetes(d)


@pytest.mark.parametrize("worker_field", ["worker_client_cert_file", "worker_client_key_file"])
@pytest.mark.parametrize("reserved", [
    "ca_file", "ca_key_file", "cert_file", "key_file", "operator_client_cert_file", "operator_client_key_file",
])
def test_fixed_worker_paths_cannot_bind_controller_or_operator_material(kubernetes_deployment, worker_field, reserved):
    d = kubernetes_deployment
    d.tls[worker_field] = d.tls[reserved]
    with pytest.raises(precheck.PrecheckFailure, match="runtime_worker_identity"):
        precheck.validate_config(d.payload)


@pytest.mark.parametrize("field", ["ca_file", "worker_client_cert_file", "worker_client_key_file"])
@pytest.mark.parametrize("fault", ["missing", "owner", "symlink", "not-regular", "unreadable", "empty", "oversized"])
def test_worker_mount_checks_its_own_files_and_metadata(kubernetes_deployment, field, fault):
    d = kubernetes_deployment
    path = "/etc/lumen/worker-precheck/" + Path(d.tls[field]).name
    overrides = {}
    if fault == "missing":
        d.worker_mounted.pop(path)
    elif fault == "unreadable":
        d.worker_mounted[path].chmod(0o600)
    elif fault in {"empty", "oversized"}:
        d.worker_mounted[path].write_bytes(b"" if fault == "empty" else b"x" * 16385)
    else:
        key, value = {"owner": ("uid", 1000), "symlink": ("islnk", True),
                      "not-regular": ("isreg", False)}[fault]
        overrides[path] = {key: value}
    with pytest.raises(precheck.PrecheckFailure, match="runtime_worker_mount_material"):
        _validate_kubernetes(d, worker_reader=_mounted_reader(d.worker_mounted, overrides))


def test_worker_mount_ca_must_match_controller_ca_bytes(kubernetes_deployment):
    d = kubernetes_deployment
    d.worker_mounted["/etc/lumen/worker-precheck/ca.pem"].write_bytes(_certificate())
    with pytest.raises(precheck.PrecheckFailure, match="runtime_worker_mount_material"):
        _validate_kubernetes(d)


@pytest.mark.parametrize("fault", [
    "unapproved-pin", "operator", "other-worker", "foreign-ca", "expired", "not-yet-valid", "server-auth",
    "key-mismatch", "bad-cert", "certificate-bundle",
])
def test_worker_mount_requires_signed_bound_approved_leaf_and_matching_key(kubernetes_deployment, fault):
    d = kubernetes_deployment
    cert_path = d.worker_mounted["/etc/lumen/worker-precheck/worker.pem"]
    key_path = d.worker_mounted["/etc/lumen/worker-precheck/worker-key.pem"]
    if fault == "unapproved-pin":
        d.runtime["fixed_workers"][0]["certificate_fingerprint"] = "f" * 64
    elif fault == "bad-cert":
        cert_path.write_bytes(b"not-a-certificate")
    elif fault == "certificate-bundle":
        cert_path.write_bytes(cert_path.read_bytes() + cert_path.read_bytes())
    elif fault == "key-mismatch":
        _, key = _signed_worker(d.ca, d.ca_key)
        key_path.write_bytes(key)
    else:
        ca, ca_key = d.ca, d.ca_key
        options = {}
        if fault == "foreign-ca":
            ca_key = ec.generate_private_key(ec.SECP256R1())
            ca = x509.load_pem_x509_certificate(_certificate(key=ca_key))
        elif fault in {"operator", "other-worker"}:
            options["uri"] = ("spiffe://lumen/operator/probe" if fault == "operator"
                              else "spiffe://lumen/fixed-worker/worker-b")
        elif fault == "expired":
            options["expires"] = NOW
        elif fault == "not-yet-valid":
            options["begins"] = NOW + timedelta(seconds=1)
        elif fault == "server-auth":
            options["client_auth"] = False
        cert, key = _signed_worker(ca, ca_key, **options)
        cert_path.write_bytes(cert)
        key_path.write_bytes(key)
        d.runtime["fixed_workers"][0]["certificate_fingerprint"] = (
            x509.load_pem_x509_certificate(cert).fingerprint(hashes.SHA256()).hex())
    with pytest.raises(precheck.PrecheckFailure, match="runtime_worker_identity"):
        _validate_kubernetes(d)


@pytest.mark.parametrize("protected", ["ca_key_file", "key_file", "operator_client_key_file"])
@pytest.mark.parametrize("same_encoding", [False, True])
def test_approved_worker_key_cannot_reuse_controller_private_identity(kubernetes_deployment, protected, same_encoding):
    d = kubernetes_deployment
    worker_key = d.worker_mounted["/etc/lumen/worker-precheck/worker-key.pem"].read_bytes()
    if not same_encoding:
        worker_key = serialization.load_pem_private_key(worker_key, password=None).private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption())
    d.mounted[d.tls[protected]].write_bytes(worker_key)
    with pytest.raises(precheck.PrecheckFailure, match="runtime_worker_identity"):
        _validate_kubernetes(d)


@pytest.mark.parametrize("worker_file", ["ca.pem", "worker.pem", "worker-key.pem"])
@pytest.mark.parametrize("protected", ["ca_key_file", "key_file", "operator_client_key_file"])
def test_worker_pem_files_cannot_hide_appended_controller_private_keys(kubernetes_deployment, worker_file, protected):
    d = kubernetes_deployment
    path = d.worker_mounted["/etc/lumen/worker-precheck/" + worker_file]
    path.write_bytes(path.read_bytes() + d.mounted[d.tls[protected]].read_bytes())
    with pytest.raises(precheck.PrecheckFailure, match="runtime_worker_mount_material"):
        _validate_kubernetes(d)


@pytest.mark.parametrize("field", ["ca_file", "bearer_token", "qualification_file", "tool_manifest_file"])
def test_kubernetes_missing_local_file_references_rejected(kubernetes_deployment, field):
    d = kubernetes_deployment
    target = d.pool["profile"] if field == "tool_manifest_file" else d.cluster
    target.pop(field)
    with pytest.raises(precheck.PrecheckFailure, match="runtime_kubernetes_mount"):
        precheck.validate_config(d.payload)


@pytest.mark.parametrize("field", ["worker_client_cert_file", "worker_client_key_file"])
@pytest.mark.parametrize("path", [
    "/tmp/worker.pem", "/etc/lumen/controller/guest/worker.pem",
    "/etc/lumen/controller/../worker.pem", "/etc/lumen/controller//worker.pem",
    "/etc/lumen/controller/dispatch.key",
])
def test_worker_identity_paths_cannot_escape_or_alias_dispatch_material(kubernetes_deployment, field, path):
    d = kubernetes_deployment
    d.tls[field] = path
    with pytest.raises(precheck.PrecheckFailure, match="runtime_controller_mount"):
        precheck.validate_config(d.payload)


def test_worker_certificate_and_private_key_cannot_share_a_path(kubernetes_deployment):
    d = kubernetes_deployment
    d.tls["worker_client_key_file"] = d.tls["worker_client_cert_file"]
    with pytest.raises(precheck.PrecheckFailure, match="runtime_worker_identity"):
        precheck.validate_config(d.payload)


@pytest.mark.parametrize("field", ["worker_client_cert_file", "worker_client_key_file"])
@pytest.mark.parametrize("consumer", ["guest-config", "guest-secret", "cloud-secret"])
def test_guest_and_cloud_inputs_cannot_rebind_fixed_worker_material(deployment, field, consumer):
    payload, _, _ = deployment
    runtime = payload["runtime_config"]
    runtime["tls"].update(worker_client_cert_file="/etc/lumen/controller/worker.pem",
                           worker_client_key_file="/etc/lumen/controller/worker-key.pem")
    path = runtime["tls"][field]
    if consumer == "guest-config":
        runtime["guest_profiles"][0]["config_file"] = path
    elif consumer == "guest-secret":
        runtime["guest_profiles"][0]["secrets"]["DATABASE_URL"] = {"file": path}
    else:
        runtime["cloud_profiles"][0]["application_credential_secret"] = {"file": path}
    with pytest.raises(precheck.PrecheckFailure, match="runtime_controller_mount"):
        precheck.validate_config(payload)


@pytest.mark.parametrize("field", ["worker_client_cert_file", "worker_client_key_file"])
def test_controller_copy_cannot_substitute_for_missing_worker_mount(kubernetes_deployment, field):
    d = kubernetes_deployment
    worker_path = "/etc/lumen/worker-precheck/" + Path(d.tls[field]).name
    d.mounted[d.tls[field]] = d.worker_mounted.pop(worker_path)
    with pytest.raises(precheck.PrecheckFailure, match="runtime_worker_mount_material"):
        _validate_kubernetes(d)


@pytest.mark.parametrize(("data", "reason"), [
    (b"not-a-private-key", "runtime_worker_mount_material"),
    (b"-----BEGIN " + b"PRIVATE KEY-----\ninvalid\n-----END " + b"PRIVATE KEY-----\n", "runtime_worker_identity"),
])
def test_worker_private_key_must_be_parseable_dedicated_material(kubernetes_deployment, data, reason):
    d = kubernetes_deployment
    d.worker_mounted["/etc/lumen/worker-precheck/worker-key.pem"].write_bytes(data)
    with pytest.raises(precheck.PrecheckFailure, match=reason):
        _validate_kubernetes(d)


def test_worker_private_key_cannot_be_world_readable(kubernetes_deployment):
    d = kubernetes_deployment
    d.worker_mounted["/etc/lumen/worker-precheck/worker-key.pem"].chmod(0o644)
    with pytest.raises(precheck.PrecheckFailure, match="runtime_worker_mount_material"):
        _validate_kubernetes(d)


def test_worker_mount_cannot_substitute_kubernetes_api_ca_for_controller_ca(kubernetes_deployment):
    d = kubernetes_deployment
    kubernetes_ca = d.mounted[d.cluster["ca_file"]].read_bytes()
    assert kubernetes_ca != d.mounted[d.tls["ca_file"]].read_bytes()
    d.worker_mounted["/etc/lumen/worker-precheck/ca.pem"].write_bytes(kubernetes_ca)
    with pytest.raises(precheck.PrecheckFailure, match="runtime_worker_mount_material"):
        _validate_kubernetes(d)


def _validate_worker_only(deployment):
    config, _ = precheck.validate_config(deployment.payload)
    mounted = deployment.mounted | deployment.worker_mounted
    precheck._validate_worker_material(config, _mounted_reader(mounted), NOW, deployment.inspect_worker_directory)


@pytest.mark.parametrize("mode", [0o444, 0o644])
def test_worker_manifest_copy_accepts_exact_pinned_public_bytes(kubernetes_deployment, mode):
    d = kubernetes_deployment
    d.worker_mounted["/etc/lumen/worker-precheck/kubernetes/manifest.json"].chmod(mode)
    _validate_kubernetes(d)


def test_worker_manifest_copy_is_required_even_when_controller_has_it(kubernetes_deployment):
    d = kubernetes_deployment
    d.worker_mounted.pop("/etc/lumen/worker-precheck/kubernetes/manifest.json").unlink()
    with pytest.raises(precheck.PrecheckFailure, match="runtime_worker_mount_material"):
        _validate_kubernetes(d)


@pytest.mark.parametrize("data", [b"changed", b"", b"x" * 1048577])
def test_worker_manifest_copy_cannot_differ_from_controller_pin(kubernetes_deployment, data):
    d = kubernetes_deployment
    d.worker_mounted["/etc/lumen/worker-precheck/kubernetes/manifest.json"].write_bytes(data)
    with pytest.raises(precheck.PrecheckFailure, match="runtime_kubernetes_manifest"):
        _validate_kubernetes(d)


@pytest.mark.parametrize("data", [b"", b"x" * 1048577, b"{}", b'{"version":1,"profiles":[],"unexpected":true}'])
def test_worker_manifest_pin_does_not_bypass_bounds_or_shared_parser(kubernetes_deployment, data):
    d = kubernetes_deployment
    d.worker_mounted["/etc/lumen/worker-precheck/kubernetes/manifest.json"].write_bytes(data)
    d.mounted[d.pool["profile"]["tool_manifest_file"]].write_bytes(data)
    d.pool["profile"]["tool_manifest_sha256"] = hashlib.sha256(data).hexdigest()
    with pytest.raises(precheck.PrecheckFailure, match="runtime_kubernetes_manifest"):
        _validate_worker_only(d)


@pytest.mark.parametrize("mode", [0o400, 0o600, 0o440, 0o640, 0o664, 0o777, 0])
def test_worker_manifest_copy_requires_public_read_only_modes(kubernetes_deployment, mode):
    d = kubernetes_deployment
    d.worker_mounted["/etc/lumen/worker-precheck/kubernetes/manifest.json"].chmod(mode)
    with pytest.raises(precheck.PrecheckFailure, match="runtime_worker_mount_material"):
        _validate_kubernetes(d)


@pytest.mark.parametrize("extra", [
    "ca-key.pem", "server-key.pem", "operator-key.pem", "dispatch.key", "token.env",
    "kubernetes/token.env", "kubernetes/qualification.json", "kubernetes/.hidden", "manifest.json",
])
def test_python_worker_inventory_rejects_every_unexpected_file(kubernetes_deployment, extra):
    d = kubernetes_deployment
    (d.worker_root / extra).write_bytes(b"not-authorized-worker-material")
    with pytest.raises(precheck.PrecheckFailure, match="runtime_worker_mount_material"):
        _validate_kubernetes(d)


@pytest.mark.parametrize("extra", ["unused", "kubernetes/unused", "kubernetes/.hidden"])
def test_python_worker_inventory_rejects_unneeded_directories(kubernetes_deployment, extra):
    d = kubernetes_deployment
    (d.worker_root / extra).mkdir(mode=0o750)
    with pytest.raises(precheck.PrecheckFailure, match="runtime_worker_mount_material"):
        _validate_kubernetes(d)


@pytest.mark.parametrize("target", ["manifest", "ancestor", "root"])
def test_python_worker_inventory_rejects_symlink_files_and_directories(kubernetes_deployment, tmp_path, target):
    d = kubernetes_deployment
    path = (d.worker_root / "kubernetes/manifest.json" if target == "manifest" else
            d.worker_root / "kubernetes" if target == "ancestor" else d.worker_root)
    saved = tmp_path / "saved-worker-material"
    path.rename(saved)
    path.symlink_to(saved, target_is_directory=target != "manifest")
    with pytest.raises(precheck.PrecheckFailure, match="runtime_worker_mount_material"):
        _validate_kubernetes(d)


@pytest.mark.parametrize("relative", ["", "kubernetes", "kubernetes/manifest.json", "worker.pem"])
@pytest.mark.parametrize("field", ["uid", "gid"])
def test_python_worker_inventory_requires_root_owner_and_group(kubernetes_deployment, relative, field):
    d = kubernetes_deployment
    path = str(d.worker_root / relative)
    inspect = _worker_directory_reader(d.worker_root, {path: {field: 1000}})
    with pytest.raises(precheck.PrecheckFailure, match="runtime_worker_mount_material"):
        _validate_kubernetes(d, inspect_worker_directory=inspect)


@pytest.mark.parametrize("relative", ["", "kubernetes"])
@pytest.mark.parametrize("mode", [0o700, 0o755, 0o770])
def test_python_worker_inventory_requires_traversable_restricted_directories(kubernetes_deployment, relative, mode):
    d = kubernetes_deployment
    (d.worker_root / relative).chmod(mode)
    with pytest.raises(precheck.PrecheckFailure, match="runtime_worker_mount_material"):
        _validate_kubernetes(d)


def test_worker_inventory_covers_all_enabled_manifests_at_exact_nested_paths(kubernetes_deployment):
    d = kubernetes_deployment
    second = json.loads(json.dumps(d.pool))
    second["name"] = "second"
    second["profile"]["tool_manifest_file"] = "/etc/lumen/controller/other/deep/manifest.json"
    disabled = json.loads(json.dumps(second))
    disabled.update(name="disabled", enabled=False)
    disabled["profile"]["tool_manifest_file"] = "/etc/lumen/controller/disabled/manifest.json"
    d.runtime["pools"].extend([second, disabled])
    folder = d.worker_root / "other/deep"
    folder.mkdir(parents=True)
    folder.chmod(0o750)
    folder.parent.chmod(0o750)
    copy = folder / "manifest.json"
    copy.write_bytes(d.mounted[d.pool["profile"]["tool_manifest_file"]].read_bytes())
    copy.chmod(0o444)
    d.worker_mounted["/etc/lumen/worker-precheck/other/deep/manifest.json"] = copy
    d.mounted[second["profile"]["tool_manifest_file"]] = d.mounted[d.pool["profile"]["tool_manifest_file"]]
    _validate_worker_only(d)
    copy.unlink()
    with pytest.raises(precheck.PrecheckFailure, match="runtime_worker_mount_material"):
        _validate_worker_only(d)


def test_material_helper_defaults_to_production_worker_inventory(kubernetes_deployment, monkeypatch):
    d = kubernetes_deployment
    observed = []

    def inspect():
        observed.append(True)
        return d.inspect_worker_directory()

    monkeypatch.setattr(precheck, "_inspect_worker_directory", inspect)
    reader = _mounted_reader(d.mounted | d.worker_mounted)
    precheck.validate_material(d.payload, now=NOW, read_material=reader)
    assert observed == [True]


def test_cli_emits_precise_worker_inventory_for_ansible(kubernetes_deployment, monkeypatch, capsys):
    d = kubernetes_deployment
    disabled = json.loads(json.dumps(d.pool))
    disabled.update(name="disabled", enabled=False)
    disabled["profile"]["tool_manifest_file"] = "/etc/lumen/controller/disabled/manifest.json"
    d.runtime["pools"].append(disabled)
    monkeypatch.setattr(precheck.sys, "stdin", io.StringIO(json.dumps(d.payload)))
    monkeypatch.setattr(precheck.sys, "argv", ["validate_runtime_precheck.py"])
    assert precheck.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is True
    assert result["worker_inventory"] == {
        "files": sorted(d.worker_mounted),
        "directories": ["/etc/lumen/worker-precheck/kubernetes"],
        "manifests": ["/etc/lumen/worker-precheck/kubernetes/manifest.json"],
    }
    (d.worker_root / "disabled").mkdir(mode=0o750)
    with pytest.raises(precheck.PrecheckFailure, match="runtime_worker_mount_material"):
        _validate_kubernetes(d)


@pytest.mark.parametrize("protected", ["ca_key_file", "key_file", "operator_client_key_file"])
def test_manifest_copy_cannot_hide_controller_private_key_bytes(kubernetes_deployment, protected):
    d = kubernetes_deployment
    manifest = json.dumps({"version": 1, "profiles": [{
        "id": "tools", "command": ["/usr/bin/tool-server"], "tools": [{
            "name": "inspect", "description": d.mounted[d.tls[protected]].read_text(),
            "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
            "effect": "read_only",
        }],
    }]}).encode()
    d.worker_mounted["/etc/lumen/worker-precheck/kubernetes/manifest.json"].write_bytes(manifest)
    d.mounted[d.pool["profile"]["tool_manifest_file"]].write_bytes(manifest)
    d.pool["profile"]["tool_manifest_sha256"] = hashlib.sha256(manifest).hexdigest()
    with pytest.raises(precheck.PrecheckFailure, match="runtime_worker_mount_material"):
        _validate_worker_only(d)
