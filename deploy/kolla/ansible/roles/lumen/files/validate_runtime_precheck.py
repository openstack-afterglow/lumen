#!/usr/bin/env python3
"""Kolla preflight using the release's RuntimeConfig, without cloud side effects.

Inventory evidence is an explicit operator attestation of inspected Glance/OCI
artifacts, not a claim that this helper inspected a cloud. Material mode checks
bytes and metadata collected from the actual controller mount on each host.
Only stable reason codes are returned: never echo config, secrets or exceptions.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import stat
import sys
import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import ValidationError

from lumen.services.infrastructure.config import RuntimeConfig
from lumen.services.infrastructure.tool_manifest import MAX_TOOL_MANIFEST_BYTES, ToolManifestError, parse_tool_manifest


class PrecheckFailure(ValueError):
    def __init__(self, *reasons: str):
        self.reasons = sorted(set(reasons))
        super().__init__(", ".join(self.reasons))


def _typed_reason(error: dict) -> str:
    loc = error["loc"]
    field = str(loc[-1]) if loc else ""
    message = str(error.get("ctx", {}).get("error", ""))
    if "fixed worker" in message or "fixed_workers" in loc or field.startswith("worker_client_"):
        return "runtime_worker_identity"
    if field == "qualification_sha256":
        return "runtime_kubernetes_qualification"
    if field == "tool_manifest_sha256":
        return "runtime_kubernetes_manifest"
    if field == "tool_manifest_file" or ("kubernetes_clusters" in loc and (
            field in {"ca_file", "qualification_file"} or "bearer_token" in loc)):
        return "runtime_kubernetes_mount"
    if "DB and PG budgets" in message:
        return "runtime_db_headroom"
    if "PG" in message or field in {"pg_connection_budget", "fixed_pg_connection_reserve", "controller_pg_connection_reserve", "pg_connections_per_process"}:
        return "runtime_pg_headroom"
    if "DB" in message or field in {"db_connection_budget", "fixed_db_connection_reserve", "controller_db_connection_reserve", "db_connections_per_process"}:
        return "runtime_db_headroom"
    if "replicas" in message or "lifetime" in message or field in {"min_replicas", "max_replicas", "max_surge", "max_lifetime_seconds", "boot_timeout_seconds", "drain_seconds"}:
        return "runtime_replica_bounds"
    if "workload" in message or field == "workload_class" or "workload_pools" in loc:
        return "runtime_class_mapping"
    if "guest profile" in message or "guest_profile_id" in message:
        return "runtime_guest_profile"
    if field == "image":
        return "runtime_oci_digest"
    if field == "plugin_digest":
        return "runtime_plugin_digest"
    if field in {"config_sha256", "config_keys", "schema_version"}:
        return "runtime_config_digest" if field == "config_sha256" else "runtime_config_schema"
    if field == "protocol_version":
        return "runtime_protocol_version"
    if "guest_profiles" in loc:
        return "runtime_guest_profile"
    if "cloud profile" in message or "OpenStack projects" in message or "cloud_profiles" in loc or field == "cloud_profile_id":
        return "runtime_cloud_profile"
    if field == "guest_image_hash":
        return "runtime_glance_hash"
    if field == "architecture":
        return "runtime_glance_architecture"
    if "https controller_url" in message or field == "controller_url":
        return "runtime_controller_https"
    if "enabled pool" in message:
        return "runtime_enabled_pools"
    return "runtime_config_invalid"


def _integer(value, low: int, high: int, reason: str) -> int:
    # Ansible inventory commonly supplies integer settings as decimal strings.
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
        try:
            value = int(value)
        except ValueError:
            raise PrecheckFailure(reason) from None
    if type(value) is not int or not low <= value <= high:
        raise PrecheckFailure(reason)
    return value


def _https(value: str) -> bool:
    try:
        if not isinstance(value, str) or re.search(r"\s", value):
            return False
        url = urlsplit(value)
        return bool(url.scheme == "https" and url.hostname and not url.username and not url.password
                    and not url.query and not url.fragment and (url.port is None or 1 <= url.port <= 65535))
    except (ValueError, TypeError):
        return False

def _controller_path(path: str | None) -> bool:
    prefix = "/etc/lumen/controller/"
    return (isinstance(path, str) and path.startswith(prefix)
            and all(part not in {"", ".", ".."} for part in path[len(prefix):].split("/")))


def validate_config(payload: dict) -> tuple[RuntimeConfig | None, list[dict]]:
    """Validate deploy inputs, returning the precise files material mode needs."""
    timeout = _integer(payload.get("proxy_idle_timeout_seconds"), 1, 2147483, "runtime_proxy_timeout")
    port = _integer(payload.get("dynamic_ingress_port"), 1, 65535, "runtime_dynamic_ingress_port")
    vip = payload.get("dynamic_ingress_vip", "")
    if vip:
        if not isinstance(vip, str) or re.search(r"[\s\[\]%]", vip):
            raise PrecheckFailure("runtime_dynamic_ingress_vip")
        try:
            ipaddress.ip_address(vip)
        except (ValueError, TypeError):
            raise PrecheckFailure("runtime_dynamic_ingress_vip") from None
    if payload.get("shared_external_frontend") is True:
        extras = payload.get("external_single_frontend_options", [])
        if not isinstance(extras, list) or any(not isinstance(line, str) for line in extras):
            raise PrecheckFailure("runtime_proxy_shared_timeout")
        configured = {}
        units = {"": 0.001, "ms": 0.001, "us": 0.000001, "s": 1, "m": 60, "h": 3600, "d": 86400}
        for line in extras:
            match = re.fullmatch(r"\s*timeout\s+(client|tunnel)\s+([0-9]+)(us|ms|s|m|h|d)?\s*", line)
            if match:
                if match[1] == "tunnel":
                    raise PrecheckFailure("runtime_proxy_shared_timeout")
                configured[match[1]] = int(match[2]) * units[match[3] or ""]
        if configured.get("client") != timeout:
            raise PrecheckFailure("runtime_proxy_shared_timeout")
    storage_required = payload.get("batch_enabled") is True or payload.get("media_enabled") is True
    config = None
    files = []
    if payload.get("runtime_enabled") is True:
        raw = payload.get("runtime_config")
        if not isinstance(raw, dict):
            raise PrecheckFailure("runtime_config_invalid")
        try:
            # JSON strict validation accepts JSON arrays for immutable tuples, while
            # refusing float/string/bool coercion of finite capacity integers.
            config = RuntimeConfig.model_validate_json(json.dumps({**raw, "enabled": True}), strict=True)
        except ValidationError as exc:
            raise PrecheckFailure(*(_typed_reason(error) for error in exc.errors(include_input=False))) from None
        if not _https(config.controller_url):
            raise PrecheckFailure("runtime_controller_https")
        _integer(payload.get("ca_expiry_margin_seconds"), 1, 2147483647, "runtime_ca_margin")
        if not config.tls:
            raise PrecheckFailure("runtime_config_invalid")
        mount = "/etc/lumen/controller/"
        paths = [config.tls.ca_file, config.tls.ca_key_file, config.tls.cert_file, config.tls.key_file,
                 config.tls.operator_client_cert_file, config.tls.operator_client_key_file]
        if any(not isinstance(path, str) or not re.fullmatch(r"/etc/lumen/controller/[^/.][^/]*", path)
               or path.rsplit("/", 1)[-1] in {".", ".."} for path in paths):
            raise PrecheckFailure("runtime_controller_mount")
        if len(set(paths)) != len(paths) or config.dispatch_key.file in paths or config.dispatch_key.file != mount + "dispatch.key":
            raise PrecheckFailure("runtime_controller_mount")
        worker_paths = [config.tls.worker_client_cert_file, config.tls.worker_client_key_file]
        if payload.get("worker_enabled") is True and (None in worker_paths or not config.fixed_workers):
            raise PrecheckFailure("runtime_worker_identity")
        if worker_paths[0] is not None:
            if (any(not _controller_path(path) or "/" in path[len(mount):] for path in worker_paths)
                    or len(set(paths + worker_paths + [config.dispatch_key.file])) != len(paths) + 3):
                raise PrecheckFailure("runtime_controller_mount")
        files.append({"path": config.tls.ca_file, "kind": "ca"})
        files.extend({"path": path, "kind": "tls_private" if path in {
            config.tls.ca_key_file, config.tls.key_file, config.tls.operator_client_key_file,
        } else "tls_public"} for path in paths[1:])
        files.append({"path": config.dispatch_key.file, "kind": "dispatch"})
        pools = [pool for pool in config.pools if pool.enabled]
        trusted = [pool for pool in pools if pool.role != "sandbox"]
        if trusted:
            reserves = {"fixed_db_connection_reserve", "controller_db_connection_reserve",
                        "fixed_pg_connection_reserve", "controller_pg_connection_reserve"}
            if not reserves.issubset(raw):
                raise PrecheckFailure("runtime_reserves_required")
        guests = {profile.id: profile for profile in config.guest_profiles}
        glance = payload.get("glance_image_evidence", {})
        artifacts = payload.get("guest_artifact_evidence", {})
        if not isinstance(glance, dict) or not isinstance(artifacts, dict):
            raise PrecheckFailure("runtime_artifact_evidence")
        kubernetes_files = []
        for pool in pools:
            if pool.backend == "kubernetes":
                cluster = config.kubernetes_cluster(pool.profile.cluster_profile_id)
                entries = [
                    {"path": cluster.ca_file, "kind": "kubernetes_ca"},
                    {"path": cluster.bearer_token.file, "kind": "kubernetes_token"},
                    {"path": cluster.qualification_file, "kind": "kubernetes_qualification", "pool_name": pool.name},
                    {"path": pool.profile.tool_manifest_file, "kind": "kubernetes_manifest", "pool_name": pool.name},
                ]
                if any(not _controller_path(entry["path"]) for entry in entries):
                    raise PrecheckFailure("runtime_kubernetes_mount")
                for entry in entries:
                    if entry not in kubernetes_files:
                        kubernetes_files.append(entry)
                continue
            cloud = config.profile(pool.cloud_profile_id)
            if not _https(cloud.auth_url):
                raise PrecheckFailure("runtime_cloud_profile")
            identifiers = [cloud.project_id, cloud.application_credential_id, pool.network_id,
                           pool.profile.flavor_id, pool.profile.guest_image_id, *pool.security_group_ids]
            if pool.ingress is not None:
                identifiers.extend((pool.ingress.ingress_pool_id, pool.ingress.ingress_subnet_id))
            if any(re.match(r"(?:REPLACE(?:_WITH)?|YOUR|EXAMPLE)(?:[_ -]|$)", value, re.IGNORECASE)
                   or value == "00000000-0000-0000-0000-000000000000" for value in identifiers):
                raise PrecheckFailure("runtime_cloud_identity")
            image = glance.get(pool.profile.guest_image_id)
            if not isinstance(image, dict) or image.get("source") != "operator":
                raise PrecheckFailure("runtime_glance_evidence")
            if image.get("architecture") != pool.architecture:
                raise PrecheckFailure("runtime_glance_architecture")
            if image.get("os_hash_algo") != "sha256" or image.get("os_hash_value") != pool.profile.guest_image_hash.removeprefix("sha256:"):
                raise PrecheckFailure("runtime_glance_hash")
            if not re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", pool.image):
                raise PrecheckFailure("runtime_oci_digest")
            if pool.role == "sandbox":
                continue
            guest = guests[pool.guest_profile_id]
            evidence = artifacts.get(guest.image)
            if not isinstance(evidence, dict) or evidence.get("source") != "operator":
                raise PrecheckFailure("runtime_guest_artifact_evidence")
            if evidence.get("plugin_digest") != guest.plugin_digest:
                raise PrecheckFailure("runtime_plugin_digest")
            if type(evidence.get("schema_version")) is not int or evidence["schema_version"] != guest.schema_version:
                raise PrecheckFailure("runtime_config_schema")
            protocols = evidence.get("protocol_versions")
            if not isinstance(protocols, list) or any(type(version) is not int for version in protocols) or guest.protocol_version not in protocols:
                raise PrecheckFailure("runtime_protocol_version")
            if not guest.config_file.startswith(mount) or any(
                part in {"", ".", ".."} for part in guest.config_file[len(mount):].split("/")
            ):
                raise PrecheckFailure("runtime_controller_mount")
            if guest.config_file in paths or guest.config_file == config.dispatch_key.file:
                raise PrecheckFailure("runtime_controller_mount")
            entry = {"path": guest.config_file, "kind": "profile", "profile_id": guest.id}
            if entry not in files:
                files.append(entry)
            if pool.role == "api":
                if payload.get("octavia_weight_zero_supported") is not True:
                    raise PrecheckFailure("runtime_octavia_weight_zero")
                if payload.get("octavia_status_query_supported") is not True:
                    raise PrecheckFailure("runtime_octavia_status_query")
                if payload.get("octavia_member_tags_supported") is not True:
                    raise PrecheckFailure("runtime_octavia_member_tags")
                for key in ("octavia_timeout_client_data", "octavia_timeout_member_data"):
                    if _integer(payload.get(key), 1, 2147483647, "runtime_proxy_timeout") != timeout * 1000:
                        raise PrecheckFailure("runtime_proxy_timeout")
                try:
                    pool_vip = ipaddress.ip_address(pool.ingress.ingress_vip)
                except ValueError:
                    raise PrecheckFailure("runtime_dynamic_ingress_vip") from None
                if vip and (pool_vip != ipaddress.ip_address(vip) or pool.ingress.ingress_member_port != port):
                    raise PrecheckFailure("runtime_dynamic_ingress_mismatch")
            storage_required = storage_required or pool.workload_class in {"online_media", "batch"}
        # No controller-only credential/artifact may alias a worker identity or
        # another kind of material. The worker directory is separately allowlisted.
        reserved = paths + [config.dispatch_key.file] + [path for path in worker_paths if path]
        kinds = {}
        for entry in kubernetes_files:
            path = entry["path"]
            if path in reserved or (path in kinds and kinds[path] != entry["kind"]):
                raise PrecheckFailure("runtime_kubernetes_mount")
            kinds[path] = entry["kind"]
        for guest in config.guest_profiles:
            if guest.config_file in worker_paths or guest.config_file in kinds:
                raise PrecheckFailure("runtime_controller_mount")
        secrets = [profile.application_credential_secret for profile in config.cloud_profiles]
        secrets.extend(secret for guest in config.guest_profiles for secret in guest.secrets.values())
        if any(secret.file is not None and secret.file in worker_paths for secret in secrets):
            raise PrecheckFailure("runtime_controller_mount")
        files.extend(kubernetes_files)
        if vip and not any(pool.role == "api" for pool in pools):
            raise PrecheckFailure("runtime_dynamic_ingress_pool")
        if payload.get("batch_enabled") is True and not {"online_text", "batch"}.issubset(config.workload_pools):
            raise PrecheckFailure("runtime_class_mapping")
    elif vip:
        raise PrecheckFailure("runtime_dynamic_ingress_pool")
    if storage_required:
        storage = payload.get("storage", {})
        if not isinstance(storage, dict) or not _https(storage.get("endpoint_url", "")) or not all(
            isinstance(storage.get(key), str) and storage[key].strip() for key in ("bucket", "access_key", "secret_key")
        ):
            raise PrecheckFailure("runtime_s3_required")
        scanner = payload.get("scanner", {})
        if not isinstance(scanner, dict) or not isinstance(scanner.get("host"), str) or not scanner["host"].strip():
            raise PrecheckFailure("runtime_scanner_required")
        _integer(scanner.get("port"), 1, 65535, "runtime_scanner_required")
        encryption = storage.get("server_side_encryption")
        if encryption not in {"none", "AES256", "aws:kms"} or (
            encryption == "aws:kms" and not (
                isinstance(storage.get("kms_key_id"), str) and storage["kms_key_id"].strip()
            )
        ):
            raise PrecheckFailure("runtime_s3_encryption_required")
    return config, files


def _worker_inventory(config: RuntimeConfig) -> dict[str, list[str]]:
    root = Path("/etc/lumen/worker-precheck")
    files = {str(root / Path(path).name) for path in (
        config.tls.ca_file, config.tls.worker_client_cert_file, config.tls.worker_client_key_file,
    )}
    manifests = set()
    directories = set()
    for pool in config.pools:
        if pool.enabled and pool.role == "sandbox" and pool.backend == "kubernetes":
            path = root / Path(pool.profile.tool_manifest_file).relative_to("/etc/lumen/controller")
            manifests.add(str(path))
            directories.update(str(parent) for parent in path.parents if parent.is_relative_to(root) and parent != root)
    if files & manifests or (files | manifests) & directories:
        raise PrecheckFailure("runtime_worker_mount_material")
    return {"files": sorted(files | manifests), "directories": sorted(directories), "manifests": sorted(manifests)}


def _inspect_worker_directory(root: str = "/etc/lumen/worker-precheck") -> dict[str, dict]:
    """Inventory every entry without following links, including the mount root."""
    inventory = {}

    def inspect(path: Path) -> None:
        metadata = path.lstat()
        inventory[str(path)] = {
            "isreg": stat.S_ISREG(metadata.st_mode), "isdir": stat.S_ISDIR(metadata.st_mode),
            "islnk": stat.S_ISLNK(metadata.st_mode), "uid": metadata.st_uid, "gid": metadata.st_gid,
            "mode": f"{stat.S_IMODE(metadata.st_mode):04o}",
        }
        if stat.S_ISDIR(metadata.st_mode):
            with os.scandir(path) as entries:
                for entry in entries:
                    inspect(Path(entry.path))

    inspect(Path(root))
    return inventory


def _read_material(path: str, *, mount_root: str = "/etc/lumen/controller") -> tuple[bytes, dict]:
    candidate = Path(path)
    root = Path("/etc/lumen/worker-precheck") if candidate.is_relative_to("/etc/lumen/worker-precheck") else Path(mount_root)
    if candidate.is_relative_to(root) and not candidate.parent.resolve(strict=True).is_relative_to(root.resolve(strict=True)):
        raise OSError("parent escapes controller mount")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError("not a regular file")
        limit = MAX_TOOL_MANIFEST_BYTES + 1 if candidate.is_relative_to("/etc/lumen/worker-precheck") else -1
        return stream.read(limit), {"isreg": True, "uid": metadata.st_uid, "gid": metadata.st_gid,
                                   "mode": f"{stat.S_IMODE(metadata.st_mode):04o}"}


def _validate_worker_material(config: RuntimeConfig, read_material, now: datetime, inspect_worker_directory) -> None:
    from cryptography import x509
    from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.x509.oid import ExtendedKeyUsageOID

    from lumen.services.infrastructure.fixed_worker import approved_peer

    tls = config.tls
    root = "/etc/lumen/worker-precheck"
    expected = _worker_inventory(config)
    try:
        inventory = inspect_worker_directory()
    except OSError:
        raise PrecheckFailure("runtime_worker_mount_material") from None
    if set(inventory) != {root, *expected["files"], *expected["directories"]}:
        raise PrecheckFailure("runtime_worker_mount_material")
    for path, metadata in inventory.items():
        directory = path == root or path in expected["directories"]
        modes = ({"0750"} if directory else {"0444", "0644"} if path in expected["manifests"] else
                 {"0440", "0640"} if path == root + "/" + Path(tls.worker_client_key_file).name else
                 {"0440", "0640", "0444", "0644"})
        if (metadata.get("islnk") or not metadata.get("isdir" if directory else "isreg")
                or metadata.get("uid") != 0 or metadata.get("gid") != 0 or metadata.get("mode") not in modes):
            raise PrecheckFailure("runtime_worker_mount_material")
    contents = {}
    for path in (tls.ca_file, tls.worker_client_cert_file, tls.worker_client_key_file):
        try:
            data, metadata = read_material("/etc/lumen/worker-precheck/" + Path(path).name)
        except OSError:
            raise PrecheckFailure("runtime_worker_mount_material") from None
        modes = {"0440", "0640"} if path == tls.worker_client_key_file else {"0440", "0640", "0444", "0644"}
        if (not metadata.get("isreg") or metadata.get("islnk") or metadata.get("uid") != 0
                or metadata.get("gid") != 0 or metadata.get("mode") not in modes or not data.strip() or len(data) > 16384):
            raise PrecheckFailure("runtime_worker_mount_material")
        private_blocks = re.findall(br"-----BEGIN [^-]*PRIVATE KEY-----", data)
        if len(private_blocks) != (1 if path == tls.worker_client_key_file else 0):
            raise PrecheckFailure("runtime_worker_mount_material")
        contents[path] = data
    try:
        ca_bytes, _ = read_material(tls.ca_file)
    except OSError:
        raise PrecheckFailure("runtime_worker_mount_material") from None
    if contents[tls.ca_file] != ca_bytes:
        raise PrecheckFailure("runtime_worker_mount_material")
    try:
        leaves = x509.load_pem_x509_certificates(contents[tls.worker_client_cert_file])
        if len(leaves) != 1:
            raise ValueError()
        leaf = leaves[0]
        ca = x509.load_pem_x509_certificate(ca_bytes)
        leaf.verify_directly_issued_by(ca)
        uris = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(
            x509.UniformResourceIdentifier)
        usage = leaf.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
        constraints = leaf.extensions.get_extension_for_class(x509.BasicConstraints).value
        if (len(uris) != 1 or not approved_peer(config, uris[0], leaf.fingerprint(hashes.SHA256()).hex())
                or constraints.ca or ExtendedKeyUsageOID.CLIENT_AUTH not in usage
                or not leaf.not_valid_before_utc <= now < leaf.not_valid_after_utc):
            raise ValueError()
        key = serialization.load_pem_private_key(contents[tls.worker_client_key_file], password=None)

        def public_bytes(public_key):
            return public_key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)

        public = public_bytes(key.public_key())
        if public != public_bytes(leaf.public_key()) or public == public_bytes(ca.public_key()):
            raise ValueError()
        # Reject copying controller private material even under worker filenames.
        for path in (tls.ca_key_file, tls.key_file, tls.operator_client_key_file):
            data, _ = read_material(path)
            if data == contents[tls.worker_client_key_file]:
                raise ValueError()
            try:
                protected = serialization.load_pem_private_key(data, password=None)
            except (ValueError, TypeError):
                continue
            if public == public_bytes(protected.public_key()):
                raise ValueError()
    except (OSError, ValueError, TypeError, InvalidSignature, UnsupportedAlgorithm, x509.ExtensionNotFound):
        raise PrecheckFailure("runtime_worker_identity") from None
    for pool in config.pools:
        if not (pool.enabled and pool.role == "sandbox" and pool.backend == "kubernetes"):
            continue
        path = root + "/" + str(Path(pool.profile.tool_manifest_file).relative_to("/etc/lumen/controller"))
        try:
            data, metadata = read_material(path)
        except OSError:
            raise PrecheckFailure("runtime_worker_mount_material") from None
        if (not metadata.get("isreg") or metadata.get("islnk") or metadata.get("uid") != 0
                or metadata.get("gid") != 0 or metadata.get("mode") not in {"0444", "0644"}):
            raise PrecheckFailure("runtime_worker_mount_material")
        if re.search(br"-----BEGIN [^-]*PRIVATE KEY-----", data):
            raise PrecheckFailure("runtime_worker_mount_material")
        try:
            parse_tool_manifest(data, pool.profile.tool_manifest_sha256)
            controller_data, _ = read_material(pool.profile.tool_manifest_file)
            if data != controller_data:
                raise PrecheckFailure("runtime_kubernetes_manifest")
        except ToolManifestError:
            raise PrecheckFailure("runtime_kubernetes_manifest") from None
        except OSError:
            raise PrecheckFailure("runtime_kubernetes_material") from None


def _validate_kubernetes_material(config: RuntimeConfig, entry: dict, data: bytes, metadata: dict,
                                  now: datetime) -> None:
    from cryptography import x509

    kind = entry["kind"]
    public_modes = {"0400", "0600", "0440", "0640", "0444", "0644"}
    if metadata.get("mode") not in (public_modes if kind == "kubernetes_ca" else {"0400", "0600"}):
        raise PrecheckFailure("runtime_kubernetes_material")
    if kind == "kubernetes_ca":
        try:
            cert = x509.load_pem_x509_certificate(data)
            if (not cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
                    or not cert.not_valid_before_utc <= now < cert.not_valid_after_utc):
                raise ValueError()
        except (ValueError, x509.ExtensionNotFound):
            raise PrecheckFailure("runtime_kubernetes_material") from None
        return
    if kind == "kubernetes_token":
        try:
            token = data.decode("utf-8").strip()
            if not token or len(data) > 16384 or any(char.isspace() for char in token):
                raise ValueError()
        except (ValueError, UnicodeError):
            raise PrecheckFailure("runtime_kubernetes_material") from None
        return
    pool = config.pool(entry["pool_name"])
    cluster = config.kubernetes_cluster(pool.profile.cluster_profile_id)
    qualification = kind == "kubernetes_qualification"
    limit = 65536 if qualification else 1048576
    digest = cluster.qualification_sha256 if qualification else pool.profile.tool_manifest_sha256
    reason = "runtime_kubernetes_qualification" if qualification else "runtime_kubernetes_manifest"
    if not data or len(data) > limit or hashlib.sha256(data).hexdigest() != digest:
        raise PrecheckFailure(reason)
    if not qualification:
        return
    try:
        report = json.loads(data)
        expected = {
            "version": 1, "server": cluster.server.rstrip("/"), "namespace": cluster.namespace,
            "runtime_class_name": cluster.runtime_class_name, "runtime_class_handler": cluster.runtime_class_handler,
            "cluster_cidrs": list(cluster.cluster_cidrs), "pool_digest": pool.digest(),
        }
        if not isinstance(report, dict) or any(report.get(key) != value for key, value in expected.items()):
            raise ValueError()
        for key in ("runtime_probe_sha256", "cni_probe_sha256", "runtime_class_spec_sha256"):
            if not isinstance(report.get(key), str) or not re.fullmatch(r"[0-9a-f]{64}", report[key]):
                raise ValueError()
        if any(not isinstance(report.get(key), str) or not report[key] for key in ("namespace_uid", "runtime_class_uid")):
            raise ValueError()
        maximum = report.get("measured_pids_max")
        if type(maximum) is not int or not 1 <= maximum <= pool.sandbox.pids_limit:
            raise ValueError()
    except (ValueError, TypeError):
        raise PrecheckFailure(reason) from None
    # API-observed namespace/RuntimeClass UID/spec, RBAC and CNI default deny
    # remain the provider's mandatory startup preflight, never inventory claims.


def validate_material(payload: dict, *, now: datetime | None = None, read_material=None,
                      inspect_worker_directory=None) -> None:
    from cryptography import x509

    config, files = validate_config(payload)
    if config is None:
        return
    now = now or datetime.now(UTC)
    read_material = read_material or _read_material
    profiles = {profile.id: profile for profile in config.guest_profiles}
    for entry in files:
        reason = ("runtime_ca_invalid" if entry["kind"] == "ca" else
                  "runtime_guest_config_file" if entry["kind"] == "profile" else
                  "runtime_kubernetes_material" if entry["kind"].startswith("kubernetes_") else "runtime_tls_material")
        try:
            data, metadata = read_material(entry["path"])
        except OSError:
            raise PrecheckFailure(reason) from None
        if not metadata.get("isreg") or metadata.get("islnk") or metadata.get("uid") != 0:
            raise PrecheckFailure(reason)
        if entry["kind"].startswith("kubernetes_"):
            _validate_kubernetes_material(config, entry, data, metadata, now)
            continue
        if entry["kind"] == "profile" and metadata.get("mode") not in {"0600", "0400"}:
            raise PrecheckFailure("runtime_guest_config_permissions")
        if entry["kind"] in {"tls_private", "dispatch"}:
            if metadata.get("mode") not in {"0600", "0400"} or not data.strip():
                raise PrecheckFailure("runtime_tls_material")
            continue
        if entry["kind"] == "tls_public":
            if metadata.get("mode") not in {"0600", "0400", "0640", "0440", "0644", "0444"} or not data.strip():
                raise PrecheckFailure("runtime_tls_material")
            continue
        if entry["kind"] == "ca":
            try:
                cert = x509.load_pem_x509_certificate(data)
                ca = cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
            except (ValueError, x509.ExtensionNotFound):
                raise PrecheckFailure("runtime_ca_invalid") from None
            try:
                signing = cert.extensions.get_extension_for_class(x509.KeyUsage).value.key_cert_sign
                cert.extensions.get_extension_for_class(x509.SubjectKeyIdentifier)
            except x509.ExtensionNotFound:
                raise PrecheckFailure("runtime_ca_invalid") from None
            if not ca or not signing or cert.not_valid_before_utc > now:
                raise PrecheckFailure("runtime_ca_invalid")
            margin = _integer(payload["ca_expiry_margin_seconds"], 1, 2147483647, "runtime_ca_margin")
            if cert.not_valid_after_utc <= now + timedelta(seconds=margin):
                raise PrecheckFailure("runtime_ca_expiry")
            continue
        profile = profiles[entry["profile_id"]]
        if hashlib.sha256(data).hexdigest() != profile.config_sha256:
            raise PrecheckFailure("runtime_config_digest")
        try:
            from pydantic import TypeAdapter

            from lumen.config import Settings
            content = json.loads(data) if profile.config_file.endswith(".json") else tomllib.loads(data.decode("utf-8"))
            content = content.get("lumen", content)
            if not isinstance(content, dict) or any(key not in content for key in profile.config_keys):
                raise ValueError()
            for key in profile.config_keys:
                field = Settings.model_fields.get(key)
                if field is None:
                    raise ValueError()
                TypeAdapter(field.rebuild_annotation()).validate_python(content[key], strict=True)
            selected = {key: content[key] for key in profile.config_keys}
            # All selected fields are strict-typed above. Run cross-field policy
            # without BaseSettings reading deployment-host environment variables.
            effective = Settings.model_construct(**selected)
            effective.coherent_execution_policy()
            Settings.validate_chat_execution_protocol_version(effective.chat_execution_protocol_version)
        except (ValueError, TypeError, AttributeError, UnicodeError):
            raise PrecheckFailure("runtime_config_schema") from None
        for pool in config.pools:
            if pool.enabled and pool.guest_profile_id == profile.id and pool.role == "worker":
                if "worker_workload_classes" not in profile.config_keys or content.get("worker_workload_classes") != [pool.workload_class]:
                    raise PrecheckFailure("runtime_class_mapping")
    if payload.get("worker_enabled") is True:
        _validate_worker_material(config, read_material, now, inspect_worker_directory or _inspect_worker_directory)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise PrecheckFailure("runtime_config_invalid")
        config, files = validate_config(payload)
        if len(sys.argv) > 1 and sys.argv[1] == "material":
            validate_material(payload)
        result = {"ok": True, "reasons": [], "files": files,
                  "worker_inventory": _worker_inventory(config) if config and payload.get("worker_enabled") is True else {}}
    except PrecheckFailure as exc:
        result = {"ok": False, "reasons": exc.reasons}
    except (ValueError, TypeError, KeyError, AttributeError):
        result = {"ok": False, "reasons": ["runtime_config_invalid"]}
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
