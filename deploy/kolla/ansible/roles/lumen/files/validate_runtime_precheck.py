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


class PrecheckFailure(ValueError):
    def __init__(self, *reasons: str):
        self.reasons = sorted(set(reasons))
        super().__init__(", ".join(self.reasons))


def _typed_reason(error: dict) -> str:
    loc = error["loc"]
    field = str(loc[-1]) if loc else ""
    message = str(error.get("ctx", {}).get("error", ""))
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
        for pool in pools:
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


def _read_material(path: str, *, mount_root: str = "/etc/lumen/controller") -> tuple[bytes, dict]:
    candidate = Path(path)
    root = Path(mount_root)
    if candidate.is_relative_to(root) and not candidate.parent.resolve(strict=True).is_relative_to(root.resolve(strict=True)):
        raise OSError("parent escapes controller mount")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError("not a regular file")
        return stream.read(), {"isreg": True, "uid": metadata.st_uid,
                               "mode": f"{stat.S_IMODE(metadata.st_mode):04o}"}


def validate_material(payload: dict, *, now: datetime | None = None, read_material=None) -> None:
    from cryptography import x509

    config, files = validate_config(payload)
    if config is None:
        return
    now = now or datetime.now(UTC)
    read_material = read_material or _read_material
    profiles = {profile.id: profile for profile in config.guest_profiles}
    for entry in files:
        reason = ("runtime_ca_invalid" if entry["kind"] == "ca" else
                  "runtime_guest_config_file" if entry["kind"] == "profile" else "runtime_tls_material")
        try:
            data, metadata = read_material(entry["path"])
        except OSError:
            raise PrecheckFailure(reason) from None
        if not metadata.get("isreg") or metadata.get("islnk") or metadata.get("uid") != 0:
            raise PrecheckFailure(reason)
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


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise PrecheckFailure("runtime_config_invalid")
        _, files = validate_config(payload)
        if len(sys.argv) > 1 and sys.argv[1] == "material":
            validate_material(payload)
        result = {"ok": True, "reasons": [], "files": files}
    except PrecheckFailure as exc:
        result = {"ok": False, "reasons": exc.reasons}
    except (ValueError, TypeError, KeyError, AttributeError):
        result = {"ok": False, "reasons": ["runtime_config_invalid"]}
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
