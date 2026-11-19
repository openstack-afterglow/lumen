"""Offline behavior checks for trusted Nova guest input and boot boundaries.

Image construction, installed confinement and live registry checks need runtime acceptance.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from lumen.services.infrastructure.config import NovaProfile
from lumen.services.infrastructure.nova import NovaProvider
from lumen.services.infrastructure.providers import ResourceIntent

ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = ROOT / "deploy" / "nova"
IMAGE = "registry.example/lumen-api@sha256:" + "a" * 64


def _module(name: str, filename: str):
    specification = importlib.util.spec_from_file_location(name, ARTIFACT / filename)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


@pytest.fixture
def build_helper():
    return _module("nova_artifact", "artifact.py")




def _build_inputs(tmp_path):
    packages = [{"package": package, "version": "5:28.1.1-1~ubuntu.24.04~noble",
                 "url": f"https://download.docker.com/linux/ubuntu/dists/noble/pool/stable/amd64/{package}.deb",
                 "sha256": "b" * 64} for package in ("docker-ce", "docker-ce-cli", "containerd.io")]
    lock = tmp_path / "packages.json"
    lock.write_text(json.dumps(packages))
    return argparse.Namespace(role="api", arch="amd64", image=IMAGE,
                              ubuntu_url="https://cloud-images.ubuntu.com/noble/20261001/noble-server-cloudimg-amd64.squashfs",
                              ubuntu_sha256="c" * 64, docker_packages=str(lock),
                              ubuntu_snapshot="20261001T000000Z",
                              docker_packages_sha256=hashlib.sha256(lock.read_bytes()).hexdigest(),
                              staging=str(tmp_path / "staging"))


@pytest.mark.parametrize("field,value", [
    ("image", "registry.example/lumen-api:latest"),
    ("image", "registry.example/lumen-api:stable@sha256:" + "a" * 64),
    ("image", "registry.example/lumen-api@sha256:" + "a" * 63),
    ("ubuntu_sha256", ""), ("ubuntu_sha256", "sha256:" + "a" * 64),
    ("docker_packages_sha256", ""), ("docker_packages_sha256", "d" * 64),
    ("ubuntu_snapshot", "latest"), ("ubuntu_snapshot", ""),
    ("ubuntu_url", "https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.squashfs"),
    ("ubuntu_url", "http://cloud-images.ubuntu.com/noble/20261001/noble-server-cloudimg-amd64.squashfs"),
    ("ubuntu_url", "https://secret@cloud-images.ubuntu.com/noble/20261001/noble-server-cloudimg-amd64.squashfs"),
    ("ubuntu_url", "https://cloud-images.ubuntu.com/noble/20261001/noble-server-cloudimg-arm64.squashfs"),
])
def test_missing_mismatched_or_floating_build_inputs_fail(build_helper, tmp_path, field, value):
    args = _build_inputs(tmp_path)
    setattr(args, field, value)
    with pytest.raises(ValueError):
        build_helper.prepare(args)


@pytest.mark.parametrize("alteration", ["missing_hash", "bad_hash", "floating_version", "missing_engine", "secret_url"])
def test_package_locks_fail_closed(build_helper, tmp_path, alteration):
    args = _build_inputs(tmp_path)
    path = Path(args.docker_packages)
    packages = json.loads(path.read_text())
    if alteration == "missing_hash":
        del packages[0]["sha256"]
    elif alteration == "bad_hash":
        packages[0]["sha256"] = ""
    elif alteration == "floating_version":
        packages[0]["version"] = "latest"
    elif alteration == "missing_engine":
        packages = packages[1:]
    else:
        packages[0]["url"] = "https://user:secret@download.docker.com/engine.deb"
    path.write_text(json.dumps(packages))
    args.docker_packages_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ValueError):
        build_helper.prepare(args)


def test_source_digest_and_platform_selection_are_verified(build_helper, tmp_path):
    raw = json.dumps({"manifests": [
        {"digest": "sha256:" + "a" * 64, "platform": {"os": "linux", "architecture": "amd64"}},
        {"digest": "sha256:" + "b" * 64, "platform": {"os": "linux", "architecture": "arm64"}},
    ]}).encode()
    manifest = tmp_path / "manifest.json"
    manifest.write_bytes(raw)
    reference = "registry.example/lumen-api@sha256:" + hashlib.sha256(raw).hexdigest()
    args = argparse.Namespace(manifest=str(manifest), image=reference, arch="amd64")
    build_helper.select_image(args)
    args.image = IMAGE
    with pytest.raises(ValueError, match="digest mismatch"):
        build_helper.select_image(args)
    args.image, args.arch = reference, "s390x"
    with pytest.raises(ValueError, match="exactly one"):
        build_helper.select_image(args)


def test_manifest_refuses_an_installed_engine_that_differs_from_the_lock(build_helper, tmp_path):
    args = _build_inputs(tmp_path)
    build_helper.prepare(args)
    directory = Path(args.staging)
    (directory / "image.json").write_text(json.dumps({"role": "api", "architecture": "amd64", "image": IMAGE}))
    packages = json.loads(Path(args.docker_packages).read_text())
    (directory / "installed-packages.tsv").write_text("".join(
        f"{item['package']}\t{'1.0' if item['package'] == 'docker-ce' else item['version']}\tamd64\n"
        for item in packages))
    with pytest.raises(ValueError, match="installed package does not match"):
        build_helper.build_manifest(argparse.Namespace(staging=str(directory), output=str(tmp_path / "guest.qcow2")))


def _nova(role):
    provider = NovaProvider.__new__(NovaProvider)
    provider.pool = SimpleNamespace(
        name="trusted", role=role, profile=NovaProfile(flavor_id="flavor", guest_image_id="glance-image",
                                                       guest_image_hash="a" * 64),
        network_id="network", ingress=SimpleNamespace(api_readiness_port=8443, ingress_member_port=8012),
    )
    provider._security_group_names = ["trusted-internal"]
    provider.deployment_id = "deployment"
    provider.controller_url = "https://controller.example:8443"
    provider.controller_ca_pem = "-----BEGIN CERTIFICATE-----\npublic-test-ca\n-----END CERTIFICATE-----\n"
    provider.managed_networks = ("10.0.0.0/24",)
    captured = []
    def create_server(**attributes):
        captured.append(attributes)
        return SimpleNamespace(id="server", status="BUILD", addresses={})
    provider.compute = SimpleNamespace(create_server=create_server)
    intent = ResourceIntent(resource_id="resource", generation=2, pool_id="trusted", role=role,
                            image_ref=IMAGE, policy_digest="b" * 64, request_fingerprint="c" * 64,
                            bootstrap_token="one-use-test-token", guest_profile_id="profile",
                            guest_profile_digest="d" * 64)
    return provider, intent, captured


@pytest.mark.parametrize("role", ["api", "worker", "sandbox"])
def test_cloud_init_exact_keys_root_modes_and_no_operator_secrets(role, monkeypatch):
    monkeypatch.setenv("APPLICATION_CREDENTIAL_SECRET", "must-not-be-serialized")
    monkeypatch.setenv("DB_PASSWORD", "must-not-be-serialized")
    provider, intent, captured = _nova(role)
    provider.create(intent)
    raw = base64.b64decode(captured[0]["user_data"]).decode()
    cloud = yaml.safe_load(raw)
    assert "must-not-be-serialized" not in raw
    assert set(cloud) == {"bootcmd", "write_files"}
    assert cloud["bootcmd"] == [["mkdir", "-p", "-m", "0700", "/var/lib/lumen/bootstrap"]]
    files = {Path(item["path"]).name: item for item in cloud["write_files"]}
    for item in files.values():
        assert item["path"].startswith("/var/lib/lumen/bootstrap/")
        assert item["owner"] == "root:root" and item["permissions"] == "0600"
    assert files["token"]["content"] == intent.bootstrap_token
    assert files["ca.pem"]["content"] == provider.controller_ca_pem
    environment = dict(line.split("=", 1) for line in files["environment"]["content"].splitlines())
    prefix = "SANDBOX" if role == "sandbox" else "LUMEN"
    keys = {f"{prefix}_CONTROLLER_URL", f"{prefix}_CONTROLLER_CA", f"{prefix}_BOOTSTRAP_FILE"}
    if role != "sandbox":
        keys |= {"LUMEN_GUEST_ROLE", "LUMEN_GUEST_PROFILE_ID", "LUMEN_GUEST_PROFILE_DIGEST", "LUMEN_GUEST_IMAGE"}
        assert environment["LUMEN_GUEST_ROLE"] == role
        assert environment["LUMEN_GUEST_PROFILE_ID"] == intent.guest_profile_id
        assert environment["LUMEN_GUEST_PROFILE_DIGEST"] == intent.guest_profile_digest
        assert environment["LUMEN_GUEST_IMAGE"] == intent.image_ref
    assert set(environment) == keys
    assert environment[f"{prefix}_CONTROLLER_URL"] == provider.controller_url
    assert set(files) == {"token", "ca.pem", "environment"} | ({"runtime.json"} if role == "worker" else {"api-readiness.json"} if role == "api" else set())
    if role == "worker":
        assert json.loads(files["runtime.json"]["content"]) == {"controller_url": provider.controller_url, "managed_networks": ["10.0.0.0/24"]}
    if role == "api":
        assert json.loads(files["api-readiness.json"]["content"]) == {"readiness_port": 8443, "service_port": 8012}


@pytest.mark.parametrize("field,value", [("guest_profile_id", None), ("guest_profile_digest", None),
                                         ("guest_profile_digest", "mismatch"), ("image_ref", "registry.example/api:latest")])
def test_trusted_cloud_init_refuses_missing_pins(field, value):
    provider, intent, captured = _nova("api")
    with pytest.raises(ValueError, match="immutable profile/image pins"):
        provider.create(replace(intent, **{field: value}))
    assert not captured


def test_boot_metadata_validator_rejects_unsafe_modes_and_symlinks(tmp_path, monkeypatch):
    helper = _module("nova_guest_settings", "guest-settings.py")
    path = tmp_path / "environment"
    path.write_text("not shell code")
    monkeypatch.setattr(Path, "lstat", lambda _: SimpleNamespace(st_uid=0, st_mode=0o100644, st_size=14))
    with pytest.raises(ValueError, match="root-owned"):
        helper.root_file(path, private=True)
    monkeypatch.setattr(Path, "lstat", lambda _: SimpleNamespace(st_uid=0, st_mode=0o120600, st_size=14))
    with pytest.raises(ValueError, match="root-owned"):
        helper.root_file(path, private=True)


@pytest.fixture
def boot_artifact(tmp_path, monkeypatch):
    helper = _module("nova_guest_settings", "guest-settings.py")
    artifact, bootstrap = tmp_path / "artifact", tmp_path / "bootstrap"
    artifact.mkdir()
    bootstrap.mkdir()
    monkeypatch.setattr(helper, "ARTIFACT", artifact)
    monkeypatch.setattr(helper, "BOOTSTRAP", bootstrap)
    original_lstat = Path.lstat
    def root_owned(path):
        metadata = original_lstat(path)
        return SimpleNamespace(st_uid=0, st_mode=metadata.st_mode, st_size=metadata.st_size)
    monkeypatch.setattr(Path, "lstat", root_owned)
    archive = artifact / "image.tar"
    archive.write_bytes(b"verified-preload")
    archive.chmod(0o600)
    manifest = {
        "schema_version": 1, "role": "api", "architecture": "amd64", "image": IMAGE,
        "image_id": "sha256:" + "e" * 64, "platform_manifest_digest": "sha256:" + "a" * 64,
        "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
    }
    (artifact / "image.json").write_text(json.dumps(manifest))
    (artifact / "image.json").chmod(0o644)
    environment = {
        "LUMEN_CONTROLLER_URL": "https://controller.example:8443",
        "LUMEN_CONTROLLER_CA": "/var/lib/lumen/bootstrap/ca.pem",
        "LUMEN_BOOTSTRAP_FILE": "/var/lib/lumen/bootstrap/token", "LUMEN_GUEST_ROLE": "api",
        "LUMEN_GUEST_PROFILE_ID": "profile", "LUMEN_GUEST_PROFILE_DIGEST": "d" * 64,
        "LUMEN_GUEST_IMAGE": IMAGE,
    }
    (bootstrap / "environment").write_text("".join(f"{key}={value}\n" for key, value in environment.items()))
    (bootstrap / "environment").chmod(0o600)
    (bootstrap / "api-readiness.json").write_text(json.dumps({"readiness_port": 8443, "service_port": 8012}))
    (bootstrap / "api-readiness.json").chmod(0o600)
    return helper, artifact, bootstrap, manifest, environment


def test_boot_validator_returns_only_verified_public_launcher_values(boot_artifact):
    helper, _, _, _, _ = boot_artifact
    assert helper.settings() == ["api", IMAGE, "sha256:" + "e" * 64, "amd64", "8012"]


@pytest.mark.parametrize("tamper", ["archive", "missing_archive_hash", "bad_image_id", "image_claim",
                                  "role_claim", "missing_profile", "secret_key", "unsafe_environment_mode"])
def test_boot_validator_rejects_tampered_hashes_claims_and_secret_environment(boot_artifact, tamper):
    helper, artifact, bootstrap, manifest, environment = boot_artifact
    if tamper == "archive":
        (artifact / "image.tar").write_bytes(b"changed-preload")
    elif tamper == "missing_archive_hash":
        manifest["archive_sha256"] = ""
    elif tamper == "bad_image_id":
        manifest["image_id"] = "unknown"
    elif tamper == "image_claim":
        environment["LUMEN_GUEST_IMAGE"] = "registry.example/lumen-api:latest"
    elif tamper == "role_claim":
        environment["LUMEN_GUEST_ROLE"] = "worker"
    elif tamper == "missing_profile":
        del environment["LUMEN_GUEST_PROFILE_DIGEST"]
    elif tamper == "secret_key":
        environment["APPLICATION_CREDENTIAL_SECRET"] = "must-never-be-accepted"
    else:
        (bootstrap / "environment").chmod(0o644)
    (artifact / "image.json").write_text(json.dumps(manifest))
    (bootstrap / "environment").write_text("".join(f"{key}={value}\n" for key, value in environment.items()))
    with pytest.raises(ValueError):
        helper.settings()
