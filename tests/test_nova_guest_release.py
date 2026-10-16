"""Release provenance and artifact tampering boundaries; no guest boot claim."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "deploy" / "nova"


@pytest.fixture
def release(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT))
    spec = importlib.util.spec_from_file_location("nova_release", ROOT / "release.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def pinned(release, monkeypatch):
    pins = {arch: {"ubuntu_url": f"https://cloud-images.ubuntu.com/noble/20261001/noble-server-cloudimg-{arch}.squashfs",
                   "ubuntu_sha256": "a" * 64, "ubuntu_snapshot": "20261001T000000Z",
                   "docker_packages_url": f"https://example.org/20261001/{arch}.json",
                   "docker_packages_sha256": "b" * 64} for arch in release.ARCHITECTURES}
    monkeypatch.setenv("BUILD_INPUTS", json.dumps({"dib_version": "3.40.0", **pins}))
    monkeypatch.setenv("RELEASE_TAG", "v1.2.3")
    monkeypatch.setenv("RELEASE_COMMIT", "c" * 40)
    for role in release.ROLES:
        monkeypatch.setenv(role.upper() + "_IMAGE", f"ghcr.io/openstack-afterglow/lumen-{role}@sha256:" + "d" * 64)
    monkeypatch.setenv("GITHUB_REPOSITORY", release.REPOSITORY)
    monkeypatch.setenv("GITHUB_REF", "refs/heads/dev")
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    return release.inputs()


@pytest.mark.parametrize("key,value", [
    ("RELEASE_TAG", "dev"), ("RELEASE_TAG", "v1.2.3\nattack"),
    ("RELEASE_COMMIT", "c" * 39),
    ("API_IMAGE", "ghcr.io/openstack-afterglow/lumen-api:latest"),
    ("WORKER_IMAGE", "ghcr.io/outsider/lumen-worker@sha256:" + "d" * 64),
])
def test_release_rejects_floating_or_noncanonical_inputs(release, pinned, monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    with pytest.raises(ValueError):
        release.inputs()


@pytest.mark.parametrize("alteration", ["missing_arch", "unpinned_dib", "extra_pin", "credential_url", "missing_hash"])
def test_build_pin_policy_rejects_unbounded_inputs(release, pinned, monkeypatch, alteration):
    data = pinned["build_inputs"]
    if alteration == "missing_arch":
        del data["arm64"]
    elif alteration == "unpinned_dib":
        data["dib_version"] = "latest"
    elif alteration == "extra_pin":
        data["amd64"]["password"] = "not-permitted"
    elif alteration == "credential_url":
        data["amd64"]["docker_packages_url"] = "https://user:secret@example.org/lock.json"
    else:
        data["amd64"]["ubuntu_sha256"] = ""
    monkeypatch.setenv("BUILD_INPUTS", json.dumps(data))
    with pytest.raises(ValueError):
        release.inputs()


@pytest.mark.parametrize("alteration,reason", [
    ("fork", "canonical"), ("pr_ref", "canonical"),
    ("no_reviewers", "requires reviewers"), ("self_review", "requires reviewers"),
    ("draft", "already be published"), ("unpublished", "already be published"),
    ("tag_changed", "tag/commit mismatch"), ("missing_builder", "guest builder"),
    ("pr_only", "CI proof"), ("failed_ci", "CI proof"),
])
def test_publication_requires_approval_and_exact_successful_release_source(release, pinned, monkeypatch, alteration, reason):
    policy = {"protection_rules": [{"type": "required_reviewers", "reviewers": [{"id": 7}], "prevent_self_review": True}]}
    publication = {"draft": False, "published_at": "2026-10-01T00:00:00Z", "tag_name": pinned["tag"]}
    commit = {"sha": pinned["commit"]}
    builder = {"type": "file"}
    run = {"head_sha": pinned["commit"], "conclusion": "success", "event": "push"}
    if alteration == "fork":
        monkeypatch.setenv("GITHUB_REPOSITORY", "outsider/lumen")
    elif alteration == "pr_ref":
        monkeypatch.setenv("GITHUB_REF", "refs/pull/7/merge")
    elif alteration == "no_reviewers":
        policy["protection_rules"][0]["reviewers"] = []
    elif alteration == "self_review":
        policy["protection_rules"][0]["prevent_self_review"] = False
    elif alteration == "draft":
        publication["draft"] = True
    elif alteration == "unpublished":
        publication["published_at"] = None
    elif alteration == "tag_changed":
        commit["sha"] = "e" * 40
    elif alteration == "missing_builder":
        builder["type"] = "dir"
    elif alteration == "pr_only":
        run["event"] = "pull_request"
    else:
        run["conclusion"] = "failure"

    def metadata(path):
        if path.startswith("environments/"):
            return policy
        if path.startswith("releases/"):
            return publication
        if path.startswith("commits/"):
            return commit
        if path.startswith("contents/"):
            return builder
        return {"workflow_runs": [run]}

    monkeypatch.setattr(release, "github", metadata)
    with pytest.raises(ValueError, match=reason):
        release.validate_source(pinned)


@pytest.mark.parametrize("tamper,reason", [
    ("symlink", "regular"), ("empty", "size limit"),
    ("role", "role/architecture/image"), ("release", "provenance"),
    ("pins", "build pins"), ("builder", "version"),
    ("bytes", "SHA-256"), ("absolute_checksum", "portable"),
])
def test_artifact_tampering_is_rejected_before_image_inspection(release, pinned, tmp_path, tamper, reason):
    output = tmp_path / "lumen-api-amd64.qcow2"
    output.write_bytes(b"boundary fixture, never booted")
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    checksum = Path(str(output) + ".sha256")
    checksum.write_text(f"{digest}  {output.name}\n")
    pins = pinned["build_inputs"]["amd64"]
    record = {"role": "api", "architecture": "amd64", "image": pinned["images"]["api"],
              "release": {"repository": release.REPOSITORY, "tag": pinned["tag"], "commit": pinned["commit"]},
              "os": {"url": pins["ubuntu_url"], "sha256": pins["ubuntu_sha256"], "package_snapshot": pins["ubuntu_snapshot"]},
              "package_lock_sha256": pins["docker_packages_sha256"], "diskimage_builder_version": "3.40.0",
              "output": {"file": output.name, "sha256": digest}}
    if tamper == "symlink":
        output.rename(tmp_path / "target")
        output.symlink_to(tmp_path / "target")
    elif tamper == "empty":
        output.write_bytes(b"")
    elif tamper == "role":
        record["role"] = "worker"
    elif tamper == "release":
        record["release"]["commit"] = "e" * 40
    elif tamper == "pins":
        record["package_lock_sha256"] = "e" * 64
    elif tamper == "builder":
        record["diskimage_builder_version"] = "3.41.0"
    elif tamper == "bytes":
        output.write_bytes(b"tampered")
    else:
        checksum.write_text(f"{digest}  {output}\n")
    Path(str(output) + ".manifest.json").write_text(json.dumps(record))
    with pytest.raises(ValueError, match=reason):
        release.verify_artifact(pinned, "amd64", "api", tmp_path)
