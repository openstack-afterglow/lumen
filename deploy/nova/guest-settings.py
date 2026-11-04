#!/usr/bin/env python3
"""Read public guest metadata and root-only cloud-init data, never shell-source it."""
from __future__ import annotations

import hashlib
import json
import re
import stat
from pathlib import Path

ARTIFACT = Path("/usr/share/lumen-guest")
BOOTSTRAP = Path("/var/lib/lumen/bootstrap")
IMAGE = re.compile(r"[a-z0-9][a-z0-9./:_-]*@sha256:[0-9a-f]{64}\Z")
BOOT_KEYS = {"LUMEN_CONTROLLER_URL", "LUMEN_CONTROLLER_CA", "LUMEN_BOOTSTRAP_FILE",
             "LUMEN_GUEST_ROLE", "LUMEN_GUEST_PROFILE_ID", "LUMEN_GUEST_PROFILE_DIGEST",
             "LUMEN_GUEST_IMAGE"}


def root_file(path: Path, *, private: bool = False) -> bytes:
    metadata = path.lstat()
    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0
            or (metadata.st_mode & 0o777) != (0o600 if private else 0o644)
            or metadata.st_size > 65536):
        raise ValueError("invalid root-owned guest metadata file")
    return path.read_bytes()


def settings() -> list[str]:
    manifest = json.loads(root_file(ARTIFACT / "image.json"))
    environment = {}
    for line in root_file(BOOTSTRAP / "environment", private=True).decode().splitlines():
        key, separator, value = line.partition("=")
        if not separator or key not in BOOT_KEYS or key in environment or not value:
            raise ValueError("invalid cloud-init guest environment")
        environment[key] = value
    if set(environment) != BOOT_KEYS:
        raise ValueError("missing cloud-init guest environment keys")
    role, image, architecture = (manifest[key] for key in ("role", "image", "architecture"))
    if (manifest.get("schema_version") != 1 or role not in {"api", "worker"}
            or architecture not in {"amd64", "arm64"} or not IMAGE.fullmatch(image)
            or environment["LUMEN_GUEST_ROLE"] != role or environment["LUMEN_GUEST_IMAGE"] != image):
        raise ValueError("guest role/image metadata mismatch")
    if (not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", environment["LUMEN_GUEST_PROFILE_ID"])
            or not re.fullmatch(r"[0-9a-f]{64}", environment["LUMEN_GUEST_PROFILE_DIGEST"])):
        raise ValueError("invalid guest profile pin")
    image_id, archive_hash = manifest["image_id"], manifest["archive_sha256"]
    if (not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id)
            or not re.fullmatch(r"[0-9a-f]{64}", archive_hash)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", manifest["platform_manifest_digest"])):
        raise ValueError("missing preload digest")
    archive = ARTIFACT / "image.tar"
    metadata = archive.lstat()
    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0
            or metadata.st_mode & 0o777 != 0o600):
        raise ValueError("invalid root-owned preload archive")
    digest = hashlib.sha256()
    with archive.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    if digest.hexdigest() != archive_hash:
        raise ValueError("preload archive SHA-256 mismatch")
    port = "0"
    if role == "api":
        readiness = json.loads(root_file(BOOTSTRAP / "api-readiness.json", private=True))
        service_port = readiness.get("service_port")
        if type(service_port) is not int or not 1 <= service_port <= 65535:
            raise ValueError("invalid API service port")
        port = str(service_port)
    # These validated public values are the only data printed for the shell wrapper.
    return [role, image, image_id, architecture, port]


if __name__ == "__main__":
    print("\n".join(settings()))
