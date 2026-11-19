#!/usr/bin/env python3
"""Validate a published release and package its native Nova guest artifacts."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import artifact

REPOSITORY = "openstack-afterglow/lumen"
ENVIRONMENT = "nova-guest-build"
ROOT = Path(__file__).resolve().parent
ARCHITECTURES = ("amd64", "arm64")
ROLES = ("api", "worker")
PIN_FIELDS = {"ubuntu_url", "ubuntu_sha256", "ubuntu_snapshot", "docker_packages_url", "docker_packages_sha256"}


def inputs() -> dict:
    tag = os.environ["RELEASE_TAG"]
    commit = os.environ["RELEASE_COMMIT"]
    if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9][A-Za-z0-9.-]*)?", tag):
        raise ValueError("release_tag must be a version tag")
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("release_commit must be a full commit SHA")
    data = json.loads(os.environ["BUILD_INPUTS"])
    if not isinstance(data, dict) or set(data) != {"dib_version", *ARCHITECTURES}:
        raise ValueError("build inputs require dib_version, amd64 and arm64")
    if not isinstance(data["dib_version"], str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", data["dib_version"]):
        raise ValueError("diskimage-builder version must be pinned")
    for arch in ARCHITECTURES:
        pins = data[arch]
        if not isinstance(pins, dict) or set(pins) != PIN_FIELDS or not all(isinstance(value, str) for value in pins.values()):
            raise ValueError("each architecture requires exactly the five public build pins")
        artifact.https_url(pins["ubuntu_url"])
        artifact.https_url(pins["docker_packages_url"])
        for field in ("ubuntu_sha256", "docker_packages_sha256"):
            if not re.fullmatch(r"[0-9a-f]{64}", pins[field]):
                raise ValueError("build SHA-256 must be pinned")
        if not re.fullmatch(r"20[0-9]{6}T[0-9]{6}Z", pins["ubuntu_snapshot"]):
            raise ValueError("Ubuntu package snapshot must be pinned")
    images = {}
    for role in ROLES:
        image = os.environ[role.upper() + "_IMAGE"]
        if not re.fullmatch(rf"ghcr\.io/openstack-afterglow/lumen-{role}@sha256:[0-9a-f]{{64}}", image):
            raise ValueError("OCI images must be canonical digest-only release references")
        images[role] = image
    return {"tag": tag, "commit": commit, "images": images, "build_inputs": data}


def github(path: str) -> dict:
    completed = subprocess.run(["gh", "api", f"repos/{REPOSITORY}/{path}"],
                               capture_output=True, check=True, timeout=60)
    return json.loads(completed.stdout)


def validate_source(data: dict) -> None:
    if os.environ.get("GITHUB_REPOSITORY") != REPOSITORY or os.environ.get("GITHUB_REF") not in {
        "refs/heads/dev", "refs/heads/main",
    }:
        raise ValueError("guest workflow must run from canonical dev/main")
    policy = github(f"environments/{ENVIRONMENT}")
    if not any(rule.get("type") == "required_reviewers" and rule.get("reviewers")
               and rule.get("prevent_self_review") is True for rule in policy.get("protection_rules", [])):
        raise ValueError("nova-guest-build requires reviewers and prevention of self-review")
    release = github(f"releases/tags/{data['tag']}")
    if release.get("draft") is not False or not release.get("published_at") or release.get("tag_name") != data["tag"]:
        raise ValueError("release tag must already be published")
    if github(f"commits/{data['tag']}").get("sha") != data["commit"]:
        raise ValueError("release tag/commit mismatch")
    for filename in ("release.py", "build-guest.sh", "artifact.py"):
        if github(f"contents/deploy/nova/{filename}?ref={data['commit']}").get("type") != "file":
            raise ValueError("release commit does not contain the guest builder")
    for workflow in ("docker-build.yml", "release.yml"):
        runs = github(f"actions/workflows/{workflow}/runs?head_sha={data['commit']}&status=completed&per_page=100")
        if not any(run.get("head_sha") == data["commit"] and run.get("conclusion") == "success"
                   and run.get("event") in {"push", "workflow_dispatch"} for run in runs.get("workflow_runs", [])):
            raise ValueError("published image/package CI proof is missing for the release commit")
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        values = {"release_tag": data["tag"], "release_commit": data["commit"],
                  "api_image": data["images"]["api"], "worker_image": data["images"]["worker"],
                  "dib_version": data["build_inputs"]["dib_version"],
                  "build_inputs": json.dumps(data["build_inputs"], separators=(",", ":"))}
        with Path(output).open("a") as stream:
            for key, value in values.items():
                stream.write(f"{key}={value}\n")


def verify_registry(data: dict, arch: str) -> None:
    with tempfile.TemporaryDirectory(prefix="lumen-release-manifest-") as directory:
        for role, image in data["images"].items():
            tag_ref = image.split("@", 1)[0] + ":" + data["tag"][1:]
            raw = subprocess.check_output(["skopeo", "inspect", "--raw", "docker://" + tag_ref], timeout=60)
            if "sha256:" + hashlib.sha256(raw).hexdigest() != image.split("@", 1)[1]:
                raise ValueError("published OCI version tag/digest mismatch")
            manifest = Path(directory) / (role + ".json")
            manifest.write_bytes(raw)
            selected = io.StringIO()
            with contextlib.redirect_stdout(selected):
                artifact.select_image(argparse.Namespace(image=image, arch=arch, manifest=str(manifest)))
            config = json.loads(subprocess.check_output(
                ["skopeo", "inspect", "--config", "docker://" + selected.getvalue().strip()], timeout=60))
            if config.get("architecture") != arch or config.get("os") != "linux":
                raise ValueError("release OCI architecture/OS mismatch")
            if config.get("config", {}).get("Labels", {}).get("org.opencontainers.image.revision") != data["commit"]:
                raise ValueError("release OCI revision mismatch")


def build(data: dict, arch: str, role: str, directory: Path) -> None:
    pins = data["build_inputs"][arch]
    verify_registry(data, arch)
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="lumen-release-packages-") as temporary:
        lock = Path(temporary) / "docker-packages.json"
        subprocess.run(["curl", "--fail", "--location", "--silent", "--show-error", "--proto", "=https",
                        "--proto-redir", "=https", "--connect-timeout", "10", "--max-time", "120",
                        pins["docker_packages_url"], "--output", str(lock)], check=True)
        if artifact.sha256(lock) != pins["docker_packages_sha256"]:
            raise ValueError("Docker package lock SHA-256 mismatch")
        output = directory.resolve() / f"lumen-{role}-{arch}.qcow2"
        subprocess.run(["bash", str(ROOT / "build-guest.sh"), "--role", role, "--arch", arch,
                        "--image", data["images"][role], "--ubuntu-url", pins["ubuntu_url"],
                        "--ubuntu-sha256", pins["ubuntu_sha256"], "--ubuntu-snapshot", pins["ubuntu_snapshot"],
                        "--docker-packages", str(lock), "--docker-packages-sha256", pins["docker_packages_sha256"],
                        "--output", str(output)], check=True)
    manifest_path = Path(str(output) + ".manifest.json")
    manifest = json.loads(manifest_path.read_text())
    manifest["release"] = {"repository": REPOSITORY, "tag": data["tag"], "commit": data["commit"]}
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
    verify_artifact(data, arch, role, directory)


def verify_artifact(data: dict, arch: str, role: str, directory: Path) -> list[Path]:
    output = directory / f"lumen-{role}-{arch}.qcow2"
    checksum = Path(str(output) + ".sha256")
    manifest_path = Path(str(output) + ".manifest.json")
    if any(path.is_symlink() or not path.is_file() for path in (output, checksum, manifest_path)):
        raise ValueError("guest artifact files must be regular, non-symlink files")
    if not 0 < output.stat().st_size < 2 * 1024**3:
        raise ValueError("qcow2 exceeds the GitHub release asset size limit")
    manifest = json.loads(manifest_path.read_text())
    pins = data["build_inputs"][arch]
    if (manifest.get("role"), manifest.get("architecture"), manifest.get("image")) != (role, arch, data["images"][role]):
        raise ValueError("guest manifest role/architecture/image mismatch")
    if manifest.get("release") != {"repository": REPOSITORY, "tag": data["tag"], "commit": data["commit"]}:
        raise ValueError("guest release provenance mismatch")
    os_record = manifest.get("os", {})
    if (os_record.get("url"), os_record.get("sha256"), os_record.get("package_snapshot")) != (
        pins["ubuntu_url"], pins["ubuntu_sha256"], pins["ubuntu_snapshot"],
    ) or manifest.get("package_lock_sha256") != pins["docker_packages_sha256"]:
        raise ValueError("guest build pins mismatch")
    if manifest.get("diskimage_builder_version") != data["build_inputs"]["dib_version"]:
        raise ValueError("guest diskimage-builder version mismatch")
    digest = artifact.sha256(output)
    if manifest.get("output") != {"file": output.name, "sha256": digest}:
        raise ValueError("guest output SHA-256 mismatch")
    if checksum.read_text() != f"{digest}  {output.name}\n":
        raise ValueError("guest checksum must match and use a portable filename")
    inspection = json.loads(subprocess.check_output(["qemu-img", "info", "--output=json", str(output)], timeout=60))
    if inspection.get("format") != "qcow2":
        raise ValueError("guest output must be qcow2")
    subprocess.run(["qemu-img", "check", str(output)], check=True, timeout=120)
    return [output, checksum, manifest_path]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("validate-inputs", "validate-source", "build", "verify-bundle"))
    parser.add_argument("--arch", choices=ARCHITECTURES)
    parser.add_argument("--role", choices=ROLES)
    parser.add_argument("--directory", type=Path, default=Path("guest-assets"))
    args = parser.parse_args()
    data = inputs()
    if args.command == "validate-source":
        validate_source(data)
    elif args.command == "build":
        if not args.arch or not args.role:
            parser.error("build requires --arch and --role")
        build(data, args.arch, args.role, args.directory)
    elif args.command == "verify-bundle":
        assets = []
        for arch in ARCHITECTURES:
            for role in ROLES:
                assets.extend(verify_artifact(data, arch, role, args.directory))
        if set(args.directory.iterdir()) != set(assets):
            raise ValueError("guest bundle must contain exactly the four qcow2/checksum/manifest sets")
    print("guest release validation passed")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        message = str(error) if isinstance(error, ValueError) else "required release input or external verification unavailable"
        print("guest release rejected: " + message, file=sys.stderr)
        raise SystemExit(1) from None
