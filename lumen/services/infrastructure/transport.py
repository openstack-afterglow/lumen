"""Bounded, identity-pinned internal HTTPS and per-generation dispatch capabilities."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import os
import re
import ssl
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import h11
from cryptography import x509

from lumen.services.infrastructure.config import RuntimeConfig, SecretRef

_MAX_REQUEST_BYTES = 256 * 1024
_MAX_RESPONSE_BYTES = 5 * 1024 * 1024
_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_PATHS = (
    ("POST", re.compile(r"/v1/executions\Z")),
    ("GET", re.compile(rf"/v1/executions/{_UUID}\Z")),
    ("DELETE", re.compile(rf"/v1/executions/{_UUID}\Z")),
    ("GET", re.compile(rf"/v1/artifacts/{_UUID}\Z")),
    ("GET", re.compile(r"/readyz\Z")),
    ("GET", re.compile(r"/v1/ready\Z")),
)
_POST_KEYS = {"run_id", "call_id", "fence", "language", "source", "timeout_seconds", "workspace_revision"}


class InternalTransportError(RuntimeError):
    """Non-diagnostic transport/auth failure, never includes credentials or response bodies."""


def _canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def encode_key(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def resolve_operator_key(ref: SecretRef | bytes) -> bytes:
    if isinstance(ref, bytes):
        key = ref
    elif not isinstance(ref, SecretRef):
        raise InternalTransportError("operator key is unavailable")
    elif ref.env is not None:
        value = os.environ.get(ref.env)
        if value is None:
            raise InternalTransportError("operator key is unavailable")
        key = value.encode("utf-8")
    else:
        try:
            key = Path(ref.file).read_bytes().rstrip(b"\r\n")
        except OSError as exc:
            raise InternalTransportError("operator key is unavailable") from exc
    if len(key) < 32:
        raise InternalTransportError("operator key is too short")
    return key


def resource_identity(role: str, resource_id: str, generation: int) -> str:
    if role not in {"api", "worker", "sandbox"} or not re.fullmatch(_UUID, resource_id) or type(generation) is not int or generation < 1:
        raise InternalTransportError("invalid resource identity")
    return f"spiffe://lumen/{role}/{resource_id}/{generation}"


def derive_dispatch_key(operator_key: SecretRef | bytes, *, resource_id: str,
                        run_id: str, generation: int) -> bytes:
    """Stable 256-bit per-generation secret; workers and controller derive independently.

    Never expose the operator key to guests. Rotate it only after dependent resource
    generations have drained; a changed key invalidates their existing capabilities.
    """
    resource_identity("sandbox", resource_id, generation)
    if not re.fullmatch(_UUID, run_id):
        raise InternalTransportError("invalid run identity")
    info = _canonical({"purpose": "lumen-sandbox-dispatch-v1", "resource_id": resource_id,
                       "run_id": run_id, "generation": generation})
    return hmac.new(resolve_operator_key(operator_key), info, hashlib.sha256).digest()


def _allowed(method: str, path: str) -> bool:
    return any(method == verb and regex.fullmatch(path) for verb, regex in _PATHS)


def sign_dispatch_capability(dispatch_key: bytes, *, resource_id: str, run_id: str,
                             generation: int, method: str, path: str, fence: int,
                             body: dict[str, Any] | None = None, call_id: str | None = None,
                             workspace_revision: int | None = None, ttl_seconds: int = 15) -> str:
    """Sign a single method/path/fence; caller must check authorization before signing."""
    resource_identity("sandbox", resource_id, generation)
    if (not isinstance(dispatch_key, bytes) or not re.fullmatch(_UUID, run_id) or len(dispatch_key) < 32
            or type(fence) is not int or fence < 0 or type(ttl_seconds) is not int
            or not 1 <= ttl_seconds <= 15 or not _allowed(method, path)
            or path in {"/readyz", "/v1/ready"}):
        raise InternalTransportError("invalid dispatch scope")
    payload: dict[str, Any] = {
        "aud": "lumen-sandbox", "resource_id": resource_id, "run_id": run_id,
        "generation": generation, "method": method, "path": path,
        "fence": fence, "exp": int(time.time()) + ttl_seconds,
    }
    if method == "POST":
        if (not isinstance(body, dict) or set(body) != _POST_KEYS or body.get("run_id") != run_id
                or body.get("fence") != fence or body.get("language") not in {"python", "javascript", "shell"}
                or not isinstance(body.get("source"), str) or len(body["source"].encode("utf-8")) > 65_536
                or type(body.get("timeout_seconds")) not in (int, float)
                or not 0 < body["timeout_seconds"] <= 300
                or type(body.get("workspace_revision")) is not int or body["workspace_revision"] < 0
                or not isinstance(body.get("call_id"), str) or not 0 < len(body["call_id"]) <= 128
                or body["call_id"] != call_id or body["workspace_revision"] != workspace_revision):
            raise InternalTransportError("invalid execution scope")
        payload.update(call_id=call_id, workspace_revision=workspace_revision,
                       fingerprint=hashlib.sha256(_canonical(body)).hexdigest())
    elif body is not None or call_id is not None or workspace_revision is not None:
        raise InternalTransportError("unexpected dispatch payload")
    raw = _canonical(payload)
    return f"{encode_key(raw)}.{encode_key(hmac.new(dispatch_key, raw, hashlib.sha256).digest())}"


class InternalTransport:
    """mTLS client bound to an exact managed-network IP and pinned resource certificate.
    No request bytes leave the TLS socket until CA chain, URI SAN and certificate
    fingerprint verification have completed on that same socket. TLS hostname
    matching is replaced by the per-generation fingerprint because guest IPs
    are assigned after boot. Managed CIDRs are operator-supplied, never DNS-derived.
    """

    def __init__(self, config: RuntimeConfig, *, managed_networks: tuple[str, ...] | None = None,
                 max_request_bytes: int = _MAX_REQUEST_BYTES,
                 max_response_bytes: int = _MAX_RESPONSE_BYTES):
        guest_dir = os.environ.get("LUMEN_GUEST_IDENTITY_DIR")
        if guest_dir:
            from lumen.services.infrastructure.guest_bootstrap import load_identity
            directory = Path(guest_dir)
            load_identity(directory, role="worker", resource_id=os.environ.get("LUMEN_RESOURCE_ID"),
                          generation=int(os.environ["LUMEN_RESOURCE_GENERATION"]))
            ca_file = str(directory / "ca.pem")
            cert_file, key_file = str(directory / "cert.pem"), str(directory / "key.pem")
        elif config.tls is not None and config.tls.operator_client_cert_file:
            ca_file = config.tls.ca_file
            cert_file, key_file = config.tls.operator_client_cert_file, config.tls.operator_client_key_file
        else:
            raise InternalTransportError("dedicated internal client identity missing")
        networks = managed_networks if managed_networks is not None else config.managed_networks
        if not networks:
            raise InternalTransportError("managed networks missing")
        self.networks = tuple(ipaddress.ip_network(value, strict=True) for value in networks)
        self.max_request_bytes = min(max_request_bytes, _MAX_REQUEST_BYTES)
        self.max_response_bytes = min(max_response_bytes, _MAX_RESPONSE_BYTES)
        if self.max_request_bytes < 1 or self.max_response_bytes < 1:
            raise InternalTransportError("invalid payload bound")
        try:
            context = ssl.create_default_context(cafile=ca_file)
            context.load_cert_chain(cert_file, key_file)
        except (OSError, ssl.SSLError) as exc:
            raise InternalTransportError("internal TLS material unavailable") from exc
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        # The guest address may not exist when its CSR is exchanged. Validate
        # the chain in TLS and enforce the pinned URI SAN and fingerprint below.
        context.check_hostname = False
        self.context = context
        controller_context = ssl.create_default_context(cafile=ca_file)
        controller_context.load_cert_chain(cert_file, key_file)
        controller_context.minimum_version = ssl.TLSVersion.TLSv1_2
        self.controller_context = controller_context

    def _address(self, address: str, port: int) -> str:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError as exc:
            raise InternalTransportError("invalid managed address") from exc
        if (not ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified
                or not any(ip in network for network in self.networks)
                or type(port) is not int or not 1 <= port <= 65535):
            raise InternalTransportError("address outside managed network")
        return f"[{ip}]" if ip.version == 6 else str(ip)

    async def request(self, *, address: str, port: int, role: str, resource_id: str,
                      generation: int, certificate_fingerprint: str, method: str, path: str,
                      deadline: datetime, capability: str | None = None,
                      body: dict[str, Any] | None = None) -> bytes:
        """One bounded request; bytes returned only for an authenticated peer.

        Caller authorizes run/resource/fence before invoking this method. The
        role-specific readiness probes are mTLS-only; dispatch requires capability.
        """
        host = self._address(address, port)
        identity = resource_identity(role, resource_id, generation)
        if (not _allowed(method, path)
                or (role == "sandbox" and path == "/v1/ready")
                or (role == "api" and path != "/v1/ready")
                or (role == "worker" and path != "/readyz")
                or (path in {"/readyz", "/v1/ready"}) != (capability is None)
                or not re.fullmatch(r"[a-f0-9]{64}", certificate_fingerprint)):
            raise InternalTransportError("invalid internal request")
        if (method == "POST" and not isinstance(body, dict)) or (method != "POST" and body is not None):
            raise InternalTransportError("invalid internal payload")
        encoded = _canonical(body) if body is not None else None
        if encoded is not None and len(encoded) > self.max_request_bytes:
            raise InternalTransportError("internal payload too large")
        remaining = (deadline - datetime.now(UTC)).total_seconds()
        if remaining <= 0:
            raise InternalTransportError("internal request deadline exceeded")
        response_limit = min(self.max_response_bytes,
                             _MAX_RESPONSE_BYTES if path.startswith("/v1/artifacts/") else 1024 * 1024)
        writer = None
        try:
            # Pin the peer on the *same* TLS connection before writing source or
            # bearer headers. A probe followed by a second HTTP connection is
            # unsafe: a replacement endpoint could receive the second request.
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(address, port, ssl=self.context, server_hostname=None,
                                        ssl_handshake_timeout=min(remaining, 10.0)),
                timeout=min(remaining, 10.0),
            )
            ssl_object = writer.get_extra_info("ssl_object")
            peer = ssl_object.getpeercert(binary_form=True) if ssl_object else None
            if not peer or not hmac.compare_digest(hashlib.sha256(peer).hexdigest(), certificate_fingerprint):
                raise InternalTransportError("resource certificate mismatch")
            certificate = x509.load_der_x509_certificate(peer)
            san = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            if identity not in san.get_values_for_type(x509.UniformResourceIdentifier):
                raise InternalTransportError("resource generation mismatch")

            connection = h11.Connection(h11.CLIENT, max_incomplete_event_size=16 * 1024)
            headers = [(b"host", f"{host}:{port}".encode("ascii")),
                       (b"connection", b"close"), (b"accept-encoding", b"identity")]
            if capability is not None:
                headers.append((b"authorization", f"Bearer {capability}".encode("ascii")))
            if encoded is not None:
                headers.extend(((b"content-type", b"application/json"),
                                (b"content-length", str(len(encoded)).encode("ascii"))))
            writer.write(connection.send(h11.Request(method=method, target=path, headers=headers)))
            if encoded is not None:
                writer.write(connection.send(h11.Data(data=encoded)))
            writer.write(connection.send(h11.EndOfMessage()))
            await asyncio.wait_for(writer.drain(), timeout=min(10.0, max(0.001, (deadline - datetime.now(UTC)).total_seconds())))

            content = bytearray()
            status = None
            while True:
                event = connection.next_event()
                if event is h11.NEED_DATA:
                    left = (deadline - datetime.now(UTC)).total_seconds()
                    if left <= 0:
                        raise InternalTransportError("internal request deadline exceeded")
                    chunk = await asyncio.wait_for(reader.read(65536), timeout=min(left, 10.0))
                    connection.receive_data(chunk)
                elif isinstance(event, h11.Response):
                    status = event.status_code
                    if not 200 <= status < 300:
                        raise InternalTransportError("internal request rejected")
                elif isinstance(event, h11.Data):
                    if len(content) + len(event.data) > response_limit:
                        raise InternalTransportError("internal response bound exceeded")
                    content.extend(event.data)
                elif isinstance(event, h11.EndOfMessage):
                    if status is None or datetime.now(UTC) >= deadline:
                        raise InternalTransportError("internal request deadline exceeded")
                    return bytes(content)
                else:
                    raise InternalTransportError("invalid internal response")
        except (OSError, TimeoutError, ssl.SSLError, x509.ExtensionNotFound,
                h11.ProtocolError, ValueError) as exc:
            raise InternalTransportError("internal transport unavailable") from exc
        finally:
            if writer is not None:
                writer.close()
