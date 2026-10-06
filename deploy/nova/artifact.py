#!/usr/bin/env python3
"""Validate public build inputs and record the provenance of a Nova guest image."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
IMAGE = re.compile(r"[a-z0-9][a-z0-9./:_-]*@sha256:[0-9a-f]{64}\Z")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def https_url(value: str) -> None:
    url = urlsplit(value)
    if (url.scheme != "https" or not url.hostname or url.username or url.password
            or url.query or url.fragment or any(char.isspace() for char in value)):
        raise ValueError("inputs require clean HTTPS URLs without credentials")
    if any(part in {"current", "latest"} for part in url.path.split("/")):
        raise ValueError("floating input URL is forbidden")


def prepare(args: argparse.Namespace) -> None:
    if not IMAGE.fullmatch(args.image) or ":" in args.image.rsplit("/", 1)[-1].split("@")[0]:
        raise ValueError("image must be a digest-only OCI reference, without a tag")
    https_url(args.ubuntu_url)
    url = urlsplit(args.ubuntu_url)
    if (url.hostname != "cloud-images.ubuntu.com" or "/noble/" not in url.path
            or not re.search(r"/20[0-9]{6}(?:\.[0-9]+)?/", url.path)
            or not url.path.endswith(f"noble-server-cloudimg-{args.arch}.squashfs")):
        raise ValueError("Ubuntu input must be a dated Ubuntu 24.04 noble cloud squashfs")
    if not re.fullmatch(r"20[0-9]{6}T[0-9]{6}Z", args.ubuntu_snapshot):
        raise ValueError("Ubuntu package snapshot timestamp must be pinned")
    for expected in (args.ubuntu_sha256, args.docker_packages_sha256):
        if not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise ValueError("missing or invalid input SHA-256")
    lock = Path(args.docker_packages)
    if sha256(lock) != args.docker_packages_sha256:
        raise ValueError("Docker package lock SHA-256 mismatch")
    packages = json.loads(lock.read_text())
    if not isinstance(packages, list) or not packages:
        raise ValueError("Docker package lock must be a nonempty JSON array")
    names = set()
    for item in packages:
        if not isinstance(item, dict) or set(item) != {"package", "version", "url", "sha256"}:
            raise ValueError("package lock requires exactly package/version/url/sha256")
        if not re.fullmatch(r"[a-z0-9][a-z0-9+.-]*", item["package"]) or item["package"] in names:
            raise ValueError("invalid or duplicate package")
        names.add(item["package"])
        if not re.fullmatch(r"[0-9][a-zA-Z0-9.+:~_-]*", item["version"]):
            raise ValueError("package version must be pinned")
        if not re.fullmatch(r"[0-9a-f]{64}", item["sha256"]):
            raise ValueError("package SHA-256 is required")
        https_url(item["url"])
    if not {"docker-ce", "docker-ce-cli", "containerd.io"} <= names:
        raise ValueError("lock must include docker-ce, docker-ce-cli and containerd.io")
    versions = {item["package"]: item["version"] for item in packages}
    if versions["docker-ce"] != versions["docker-ce-cli"]:
        raise ValueError("Docker engine/CLI versions must match")
    directory = Path(args.staging)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "inputs.json").write_text(json.dumps({
        "role": args.role, "architecture": args.arch, "image": args.image,
        "os": {"name": "Ubuntu", "release": "24.04", "codename": "noble",
               "url": args.ubuntu_url, "sha256": args.ubuntu_sha256,
               "package_snapshot": args.ubuntu_snapshot},
        "engine": packages, "package_lock_sha256": args.docker_packages_sha256,
    }, sort_keys=True, indent=2) + "\n")
    for item in packages:
        print("\t".join(item[key] for key in ("package", "version", "url", "sha256")))


def select_image(args: argparse.Namespace) -> None:
    raw = Path(args.manifest).read_bytes()
    if hashlib.sha256(raw).hexdigest() != args.image.rsplit("@sha256:", 1)[1]:
        raise ValueError("OCI source manifest digest mismatch")
    manifest = json.loads(raw)
    selected = args.image.split("@", 1)[1]
    if "manifests" in manifest:
        matches = [entry["digest"] for entry in manifest["manifests"]
                   if entry.get("platform", {}).get("os") == "linux"
                   and entry.get("platform", {}).get("architecture") == args.arch
                   and entry.get("platform", {}).get("variant", "") in {"", "v8"}]
        if len(matches) != 1:
            raise ValueError("OCI index must have exactly one requested Linux architecture")
        selected = matches[0]
    if not DIGEST.fullmatch(selected):
        raise ValueError("selected OCI manifest must use SHA-256")
    print(args.image.split("@", 1)[0] + "@" + selected)


def image_record(args: argparse.Namespace) -> None:
    directory = Path(args.staging)
    inputs = json.loads((directory / "inputs.json").read_text())
    raw = (directory / "platform-manifest.json").read_bytes()
    selected_digest = args.selected.split("@", 1)[1]
    if "sha256:" + hashlib.sha256(raw).hexdigest() != selected_digest:
        raise ValueError("selected OCI platform manifest digest mismatch")
    manifest = json.loads(raw)
    image_id = manifest["config"]["digest"]
    if not DIGEST.fullmatch(image_id):
        raise ValueError("OCI config digest must use SHA-256")
    inspection = json.loads(subprocess.check_output([
        "skopeo", "inspect", "--config", "docker-archive:" + str(directory / "image.tar"),
    ]))
    if inspection.get("architecture") != inputs["architecture"] or inspection.get("os") != "linux":
        raise ValueError("OCI config architecture/OS mismatch")
    # docker-archive preserves the config JSON bytes; hash those bytes independently
    # rather than trusting a registry tag or Docker's mutable local tag after loading.
    import tarfile
    with tarfile.open(directory / "image.tar") as archive:
        docker_manifest = json.load(archive.extractfile("manifest.json"))
        if len(docker_manifest) != 1:
            raise ValueError("preload archive must contain exactly one image")
        config = archive.extractfile(docker_manifest[0]["Config"])
        if config is None or "sha256:" + hashlib.sha256(config.read()).hexdigest() != image_id:
            raise ValueError("preload archive config digest mismatch")
    record = {"schema_version": 1, "role": inputs["role"], "architecture": inputs["architecture"],
              "image": inputs["image"], "platform_manifest_digest": selected_digest,
              "image_id": image_id, "archive_sha256": sha256(directory / "image.tar")}
    (directory / "image.json").write_text(json.dumps(record, sort_keys=True, indent=2) + "\n")


def build_manifest(args: argparse.Namespace) -> None:
    directory = Path(args.staging)
    manifest = json.loads((directory / "inputs.json").read_text())
    manifest.update(json.loads((directory / "image.json").read_text()))
    inventory = directory / "installed-packages.tsv"
    installed = []
    for line in inventory.read_text().splitlines():
        package, version, architecture = line.split("\t")
        installed.append({"package": package, "version": version, "architecture": architecture})
    versions = {item["package"].split(":", 1)[0]: item["version"] for item in installed}
    for item in manifest["engine"]:
        if versions.get(item["package"]) != item["version"]:
            raise ValueError("final installed package does not match the package lock")
    manifest["installed_packages"] = installed
    manifest["installed_packages_sha256"] = sha256(inventory)
    manifest["diskimage_builder_version"] = subprocess.check_output(
        ["disk-image-create", "--version"], text=True).strip()
    manifest["elements"] = {"ubuntu": manifest["diskimage_builder_version"],
                            "vm": manifest["diskimage_builder_version"],
                            "block-device-efi": manifest["diskimage_builder_version"],
                            "lumen-guest": "1"}
    root = Path(__file__).resolve().parent
    manifest["artifact_source_sha256"] = {
        str(path.relative_to(root)): sha256(path) for path in sorted(root.rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts
    }
    manifest["output"] = {"file": Path(args.output).name,
                          "sha256": sha256(Path(args.output))}
    Path(args.output + ".manifest.json").write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare_parser = commands.add_parser("prepare")
    for name in ("role", "arch", "image", "ubuntu-url", "ubuntu-sha256", "ubuntu-snapshot",
                 "docker-packages", "docker-packages-sha256", "staging"):
        prepare_parser.add_argument("--" + name, required=True)
    prepare_parser.set_defaults(handler=prepare)
    selection = commands.add_parser("select-image")
    for name in ("image", "arch", "manifest"):
        selection.add_argument("--" + name, required=True)
    selection.set_defaults(handler=select_image)
    record = commands.add_parser("image-record")
    record.add_argument("--staging", required=True)
    record.add_argument("--selected", required=True)
    record.set_defaults(handler=image_record)
    final = commands.add_parser("build-manifest")
    final.add_argument("--staging", required=True)
    final.add_argument("--output", required=True)
    final.set_defaults(handler=build_manifest)
    args = parser.parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
