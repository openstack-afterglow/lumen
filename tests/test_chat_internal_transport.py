"""A managed guest must be authenticated before any dispatch secret leaves the worker."""
from __future__ import annotations

import asyncio
import hashlib
import json
import ssl
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from lumen.services.infrastructure import bootstrap, guest_bootstrap
from lumen.services.infrastructure.config import RuntimeConfig
from lumen.services.infrastructure.transport import (
    InternalTransport,
    InternalTransportError,
    resource_identity,
    sign_dispatch_capability,
)


def _certificate(key, issuer, signer, *, san=None, client=False, subject_key_id=None, dns=None):
    subject = issuer if signer is key else x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "Lumen test guest")])
    builder = (x509.CertificateBuilder().subject_name(subject)
        .issuer_name(issuer).public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
        .not_valid_after(datetime.now(UTC) + timedelta(minutes=10)))
    if signer is key:
        builder = builder.add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        builder = builder.add_extension(x509.SubjectKeyIdentifier(
            subject_key_id if subject_key_id is not None else
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()).digest), critical=False)
        builder = builder.add_extension(x509.KeyUsage(
            digital_signature=True, content_commitment=False, key_encipherment=False,
            data_encipherment=False, key_agreement=False, key_cert_sign=True,
            crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
    else:
        builder = builder.add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        builder = builder.add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(signer.public_key()), critical=False)
        builder = builder.add_extension(x509.KeyUsage(
            digital_signature=True, content_commitment=False, key_encipherment=False,
            data_encipherment=False, key_agreement=False, key_cert_sign=False,
            crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
        uris = san if isinstance(san, list) else [san]
        names = [x509.UniformResourceIdentifier(uri) for uri in uris]
        if dns is not None:
            names.append(x509.DNSName(dns))
        builder = builder.add_extension(x509.SubjectAlternativeName(names), critical=False)
        builder = builder.add_extension(x509.ExtendedKeyUsage([
            ExtendedKeyUsageOID.CLIENT_AUTH if client else ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
    return builder.sign(signer, hashes.SHA256())


def _store(tmp_path, stem, key, certificate):
    cert_file, key_file = tmp_path / f"{stem}.pem", tmp_path / f"{stem}.key"
    cert_file.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                           serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    return str(cert_file), str(key_file)


@pytest.mark.asyncio
async def test_internal_transport_never_sends_capability_before_pin_and_san(tmp_path, monkeypatch):
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_cert = _certificate(ca_key, x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "Lumen test CA")]), ca_key)
    ca_file = tmp_path / "ca.pem"
    ca_file.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    client_key = ec.generate_private_key(ec.SECP256R1())
    client_cert = _certificate(client_key, ca_cert.subject, ca_key,
                               san="spiffe://lumen/worker/" + str(uuid4()) + "/1", client=True)
    client_files = _store(tmp_path, "client", client_key, client_cert)
    resource_id = str(uuid4())
    server_key = ec.generate_private_key(ec.SECP256R1())
    server_cert = _certificate(server_key, ca_cert.subject, ca_key,
                               san=resource_identity("sandbox", resource_id, 1))
    server_files = _store(tmp_path, "server", server_key, server_cert)
    fingerprint = hashlib.sha256(server_cert.public_bytes(serialization.Encoding.DER)).hexdigest()
    server_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    server_context.load_cert_chain(*server_files)
    server_context.load_verify_locations(str(ca_file))
    server_context.verify_mode = ssl.CERT_REQUIRED
    received = []
    notices = asyncio.Queue()

    async def handle(reader, writer):
        try:
            data = await reader.readuntil(b"\r\n\r\n")
        except asyncio.IncompleteReadError as exc:
            data = exc.partial
        received.append(data)
        notices.put_nowait(None)
        if data:
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
            await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=server_context)
    port = server.sockets[0].getsockname()[1]
    monkeypatch.delenv("LUMEN_GUEST_IDENTITY_DIR", raising=False)
    transport = InternalTransport(SimpleNamespace(
        managed_networks=("10.0.0.0/24",), tls=SimpleNamespace(
            ca_file=str(ca_file), operator_client_cert_file=client_files[0],
            operator_client_key_file=client_files[1])))
    # The production address gate rejects loopback; this test isolates TLS using
    # local sockets instead of needing an operator-managed private network.
    monkeypatch.setattr(transport, "_address", lambda address, port: address)
    path = f"/v1/artifacts/{uuid4()}"

    async def attempt(*, fingerprint, identity):
        return await transport.request(address="127.0.0.1", port=port, role="sandbox",
                                       resource_id=identity, generation=1,
                                       certificate_fingerprints=frozenset({fingerprint}),
                                       method="GET", path=path,
                                       deadline=datetime.now(UTC) + timedelta(seconds=4),
                                       capability="top-secret-capability")

    try:
        with pytest.raises(InternalTransportError, match="certificate mismatch"):
            await attempt(fingerprint="0" * 64, identity=resource_id)
        await asyncio.wait_for(notices.get(), 2)
        assert received[-1] == b""
        with pytest.raises(InternalTransportError, match="generation mismatch"):
            await attempt(fingerprint=fingerprint, identity=str(uuid4()))
        await asyncio.wait_for(notices.get(), 2)
        assert received[-1] == b""
        assert await attempt(fingerprint=fingerprint, identity=resource_id) == b"ok"
        await asyncio.wait_for(notices.get(), 2)
        assert b"authorization: bearer " + b"top-secret-capability" in received[-1].lower()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_controller_issued_sandbox_certificate_binds_the_signing_ca(monkeypatch):
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_cert = _certificate(ca_key, x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "Lumen test CA")]), ca_key,
        subject_key_id=b"\x12" * 20)
    now = datetime.now(UTC)
    token = "test-token-" + "a" * 48
    resource = SimpleNamespace(
        id=str(uuid4()), generation=1, role="sandbox", run_id=str(uuid4()),
        image_ref="test-image", policy_digest="test-policy",
        bootstrap_token_hash=hashlib.sha256(token.encode("ascii")).hexdigest(),
        bootstrap_expires_at=now + timedelta(minutes=5), deadline_at=now + timedelta(minutes=2),
        desired_state="requested", observed_state="requested", certificate_fingerprint=None,
    )

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        def begin(self):
            return self

        async def execute(self, _query):
            return SimpleNamespace(scalar_one_or_none=lambda: resource)

    monkeypatch.setattr(bootstrap, "_ca", lambda _config: (ca_cert, ca_key))
    monkeypatch.setattr(bootstrap, "_factory", lambda: Session)
    guest_key = ec.generate_private_key(ec.SECP256R1())
    csr = x509.CertificateSigningRequestBuilder().subject_name(x509.Name([])).sign(guest_key, hashes.SHA256())

    issued = await bootstrap.exchange_bootstrap_token(
        token, csr.public_bytes(serialization.Encoding.PEM).decode("ascii"),
        object(), operator_key=b"operator-key-" + b"0" * 32,
    )

    certificate = x509.load_pem_x509_certificate(issued["certificate_pem"].encode("ascii"))
    authority = certificate.extensions.get_extension_for_class(x509.AuthorityKeyIdentifier).value
    subject = ca_cert.extensions.get_extension_for_class(x509.SubjectKeyIdentifier).value
    assert authority.key_identifier == subject.digest
    assert resource.certificate_fingerprint == certificate.fingerprint(hashes.SHA256()).hex()
    assert resource.bootstrap_token_hash is None


def _request_transport(monkeypatch, *, max_request_bytes=256 * 1024):
    transport = object.__new__(InternalTransport)
    transport.max_request_bytes = max_request_bytes
    transport.max_response_bytes = 1024 * 1024
    monkeypatch.setattr(transport, "_address", lambda address, port: address)
    return transport


def _request_arguments(**overrides):
    arguments = dict(address="10.0.0.2", port=8013, role="api", resource_id=str(uuid4()),
                     generation=1, method="GET", path="/v1/ready",
                     deadline=datetime.now(UTC) + timedelta(seconds=4),
                     certificate_fingerprints=frozenset({"a" * 64}))
    arguments.update(overrides)
    return arguments


@pytest.mark.asyncio
@pytest.mark.parametrize("pins", [None, frozenset(), {"a" * 64}, "a" * 64,
    frozenset({"a" * 63}), frozenset({"g" * 64}), frozenset({1}),
    frozenset({"a" * 64, "not-a-pin"})])
async def test_internal_transport_rejects_invalid_pin_sets_before_connect(monkeypatch, pins):
    transport = _request_transport(monkeypatch)
    with pytest.raises(InternalTransportError, match="invalid internal request"):
        await transport.request(**_request_arguments(certificate_fingerprints=pins))


@pytest.mark.asyncio
@pytest.mark.parametrize("role,method,path,capability,allowed", [
    ("api", "GET", "/v1/ready", None, True),
    ("api", "POST", "/v1/drain", None, True),
    ("api", "POST", "/v1/drain", "capability", False),
    ("api", "GET", "/v1/drain", None, False),
    ("api", "DELETE", "/v1/drain", None, False),
    ("api", "POST", "/v1/internal/drain", None, False),
    ("api", "GET", "/readyz", None, False),
    ("api", "GET", "/v1/ready", "capability", False),
    ("worker", "GET", "/readyz", None, True),
    ("worker", "GET", "/v1/ready", None, False),
    ("worker", "POST", "/v1/drain", None, False),
    ("sandbox", "POST", "/v1/drain", None, False),
    ("sandbox", "GET", "/v1/ready", None, False),
    ("sandbox", "GET", "/readyz", None, True),
    ("sandbox", "GET", "/v1/artifacts/{uuid}", "capability", True),
    ("sandbox", "GET", "/v1/executions/{uuid}", "capability", True),
    ("sandbox", "DELETE", "/v1/executions/{uuid}", "capability", True),
    ("sandbox", "POST", "/v1/executions", "capability", True),
    ("sandbox", "GET", "/v1/artifacts/{uuid}", None, False),
    ("api", "GET", "/v1/artifacts/{uuid}", "capability", False),
    ("worker", "GET", "/v1/executions/{uuid}", "capability", False),
    ("api", "GET", "/v1/ready?include_load=1", None, False),
])
async def test_internal_transport_role_and_path_scope(monkeypatch, role, method, path, capability, allowed):
    transport = _request_transport(monkeypatch)
    context = object()
    contexts = []
    monkeypatch.setattr(transport, "_load_context", lambda **kwargs: contexts.append(kwargs) or context)
    connections = []

    async def connect(*args, **kwargs):
        connections.append((args, kwargs))
        raise InternalTransportError("connection reached")

    monkeypatch.setattr(asyncio, "open_connection", connect)
    arguments = _request_arguments(role=role, method=method, path=path.format(uuid=uuid4()),
                                   capability=capability)
    if method == "POST":
        arguments["body"] = ({"resource_id": arguments["resource_id"], "generation": 1, "fence": 0}
                             if path == "/v1/drain" else {})
    with pytest.raises(InternalTransportError, match="connection reached" if allowed else "invalid internal request"):
        await transport.request(**arguments)
    assert len(connections) == int(allowed)
    assert len(contexts) == int(allowed)
    if allowed:
        assert connections[0][1]["ssl"] is context
        assert contexts == [{"check_hostname": False}]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    {"fence": -1}, {"fence": True}, {"fence": 1.0}, {"generation": True},
    {"generation": 2}, {"resource_id": "other"}, {"extra": "forbidden"},
])
async def test_internal_transport_drain_requires_exact_generation_and_nonnegative_fence(monkeypatch, change):
    transport = _request_transport(monkeypatch)
    arguments = _request_arguments(method="POST", path="/v1/drain")
    body = {"resource_id": arguments["resource_id"], "generation": 1, "fence": 0}
    body.update(change)
    with pytest.raises(InternalTransportError, match="invalid internal payload"):
        await transport.request(**arguments, body=body)


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["resource_id", "generation", "fence"])
async def test_internal_transport_drain_rejects_missing_fields(monkeypatch, missing):
    transport = _request_transport(monkeypatch)
    arguments = _request_arguments(method="POST", path="/v1/drain")
    body = {"resource_id": arguments["resource_id"], "generation": 1, "fence": 0}
    del body[missing]
    with pytest.raises(InternalTransportError, match="invalid internal payload"):
        await transport.request(**arguments, body=body)


@pytest.mark.asyncio
async def test_internal_transport_drain_body_obeys_request_bound(monkeypatch):
    transport = _request_transport(monkeypatch, max_request_bytes=1)
    arguments = _request_arguments(method="POST", path="/v1/drain")
    with pytest.raises(InternalTransportError, match="payload too large"):
        await transport.request(**arguments, body={"resource_id": arguments["resource_id"],
                                                  "generation": 1, "fence": 0})


def test_drain_can_never_be_signed_as_sandbox_dispatch():
    with pytest.raises(InternalTransportError, match="invalid dispatch scope"):
        sign_dispatch_capability(b"a" * 32, resource_id=str(uuid4()), run_id=str(uuid4()),
                                 generation=1, method="POST", path="/v1/drain", fence=0,
                                 body={"resource_id": str(uuid4()), "generation": 1, "fence": 0})


@pytest.fixture
def tls_material(tmp_path, monkeypatch):
    monkeypatch.delenv("LUMEN_GUEST_IDENTITY_DIR", raising=False)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_cert = _certificate(ca_key, x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "Lumen rotation test CA")]), ca_key)
    ca_file = tmp_path / "ca.pem"
    ca_file.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    client_key = ec.generate_private_key(ec.SECP256R1())
    client_cert = _certificate(client_key, ca_cert.subject, ca_key,
                              san=resource_identity("worker", str(uuid4()), 1), client=True)
    client_files = _store(tmp_path, "client", client_key, client_cert)
    transport = InternalTransport(SimpleNamespace(
        managed_networks=("10.0.0.0/24",), tls=SimpleNamespace(
            ca_file=str(ca_file), operator_client_cert_file=client_files[0],
            operator_client_key_file=client_files[1])))
    monkeypatch.setattr(transport, "_address", lambda address, port: address)
    return SimpleNamespace(directory=tmp_path, ca_key=ca_key, ca_cert=ca_cert,
                           ca_file=ca_file, transport=transport)


def _tls_server_identity(material, stem, *, san, dns=None):
    key = ec.generate_private_key(ec.SECP256R1())
    certificate = _certificate(key, material.ca_cert.subject, material.ca_key, san=san, dns=dns)
    files = _store(material.directory, stem, key, certificate)
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.load_cert_chain(*files)
    context.load_verify_locations(str(material.ca_file))
    context.verify_mode = ssl.CERT_REQUIRED
    return context, certificate.fingerprint(hashes.SHA256()).hex()


@asynccontextmanager
async def _tls_server(context):
    notices = asyncio.Queue()

    async def handle(reader, writer):
        peer = writer.get_extra_info("ssl_object").getpeercert(binary_form=True)
        try:
            headers = await reader.readuntil(b"\r\n\r\n")
        except asyncio.IncompleteReadError as exc:
            headers = exc.partial
        body = b""
        if headers:
            for line in headers.split(b"\r\n"):
                name, separator, value = line.partition(b":")
                if separator and name.lower() == b"content-length":
                    body = await reader.readexactly(int(value.strip()))
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
            await writer.drain()
        writer.close()
        with suppress(ConnectionError, ssl.SSLError):
            await writer.wait_closed()
        notices.put_nowait((headers, body, hashlib.sha256(peer).hexdigest()))

    server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=context)
    try:
        yield server.sockets[0].getsockname()[1], notices
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
@pytest.mark.parametrize("served", ["current", "previous"])
async def test_real_tls_accepts_current_or_previous_peer_during_overlap(tls_material, served):
    resource_id = str(uuid4())
    identity = resource_identity("api", resource_id, 1)
    current_context, current_pin = _tls_server_identity(tls_material, "current", san=identity)
    previous_context, previous_pin = _tls_server_identity(tls_material, "previous", san=identity)
    context = current_context if served == "current" else previous_context
    async with _tls_server(context) as (port, notices):
        assert await tls_material.transport.request(**_request_arguments(
            address="127.0.0.1", port=port, resource_id=resource_id,
            certificate_fingerprints=frozenset({current_pin.upper(), previous_pin}))) == b"ok"
        headers, body, _ = await asyncio.wait_for(notices.get(), 2)
        assert headers.startswith(b"GET /v1/ready ") and body == b""


@pytest.mark.asyncio
async def test_real_tls_rejects_previous_peer_after_overlap_without_sending_http(tls_material):
    resource_id = str(uuid4())
    identity = resource_identity("api", resource_id, 1)
    _, current_pin = _tls_server_identity(tls_material, "current", san=identity)
    previous_context, _ = _tls_server_identity(tls_material, "previous", san=identity)
    async with _tls_server(previous_context) as (port, notices):
        with pytest.raises(InternalTransportError, match="certificate mismatch"):
            await tls_material.transport.request(**_request_arguments(
                address="127.0.0.1", port=port, resource_id=resource_id,
                certificate_fingerprints=frozenset({current_pin})))
        headers, body, _ = await asyncio.wait_for(notices.get(), 2)
        assert headers == body == b""


@pytest.mark.asyncio
@pytest.mark.parametrize("uris", ["additional", "duplicate", "absent"])
async def test_real_tls_requires_sole_matching_uri_san_before_http(tls_material, uris):
    resource_id = str(uuid4())
    identity = resource_identity("api", resource_id, 1)
    names = {"additional": [identity, resource_identity("worker", str(uuid4()), 1)],
             "duplicate": [identity, identity], "absent": []}[uris]
    context, pin = _tls_server_identity(tls_material, "multiple-san", san=names, dns="localhost")
    async with _tls_server(context) as (port, notices):
        with pytest.raises(InternalTransportError, match="generation mismatch"):
            await tls_material.transport.request(**_request_arguments(
                address="127.0.0.1", port=port, resource_id=resource_id,
                certificate_fingerprints=frozenset({pin})))
        headers, body, _ = await asyncio.wait_for(notices.get(), 2)
        assert headers == body == b""


@pytest.mark.asyncio
async def test_real_tls_sends_api_drain_as_bounded_json_without_capability(tls_material):
    resource_id = str(uuid4())
    context, pin = _tls_server_identity(tls_material, "api", san=resource_identity("api", resource_id, 1))
    payload = {"resource_id": resource_id, "generation": 1, "fence": 17}
    async with _tls_server(context) as (port, notices):
        assert await tls_material.transport.request(**_request_arguments(
            address="127.0.0.1", port=port, resource_id=resource_id, method="POST", path="/v1/drain",
            certificate_fingerprints=frozenset({pin})), body=payload) == b"ok"
        headers, body, _ = await asyncio.wait_for(notices.get(), 2)
        assert headers.startswith(b"POST /v1/drain ")
        assert b"authorization:" not in headers.lower()
        assert b"content-type: application/json" in headers.lower()
        assert json.loads(body) == payload


def _guest_version(material, root, version, *, role, resource_id):
    directory = root / version
    directory.mkdir(parents=True, mode=0o750)
    key = ec.generate_private_key(ec.SECP256R1())
    certificate = _certificate(key, material.ca_cert.subject, material.ca_key,
                               san=resource_identity(role, resource_id, 1), client=True)
    directory.joinpath("ca.pem").write_bytes(material.ca_file.read_bytes())
    directory.joinpath("cert.pem").write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    directory.joinpath("key.pem").write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    directory.joinpath("identity.json").write_text(json.dumps({
        "role": role, "resource_id": resource_id, "generation": 1}), encoding="ascii")
    for path in directory.iterdir():
        path.chmod(0o440)
    return directory, certificate.fingerprint(hashes.SHA256()).hex()


def _swap_current(root, version):
    temporary = root / "next"
    temporary.symlink_to(version.name)
    temporary.replace(root / "current")


def _guest_transport(monkeypatch, root, *, role, resource_id):
    monkeypatch.setenv("LUMEN_GUEST_IDENTITY_DIR", str(root))
    monkeypatch.setenv("LUMEN_RESOURCE_ID", resource_id)
    monkeypatch.setenv("LUMEN_RESOURCE_GENERATION", "1")
    if role is None:
        monkeypatch.delenv("LUMEN_GUEST_ROLE", raising=False)
    else:
        monkeypatch.setenv("LUMEN_GUEST_ROLE", role)
    resolutions, validations = [], []

    def snapshot(directory):
        resolved = (directory / "current").resolve(strict=True)
        resolutions.append(resolved)
        return resolved

    # Ownership and certificate validation are guest_bootstrap's responsibility;
    # isolate those checks here while using real immutable files and TLS below.
    def validate(directory, **kwargs):
        validations.append((directory, kwargs))
        return json.loads((directory / "identity.json").read_text(encoding="ascii"))

    monkeypatch.setattr(guest_bootstrap, "identity_version", snapshot)
    monkeypatch.setattr(guest_bootstrap, "load_identity", validate)
    transport = InternalTransport(RuntimeConfig(managed_networks=("10.0.0.0/24",)))
    monkeypatch.setattr(transport, "_address", lambda address, port: address)
    return transport, resolutions, validations


@pytest.mark.parametrize("role", [None, "api"])
def test_guest_context_uses_one_snapshot_even_when_current_changes_during_validation(tls_material, monkeypatch, role):
    root = tls_material.directory / "identity"
    resource_id = str(uuid4())
    first, _ = _guest_version(tls_material, root, "first", role=role or "worker", resource_id=resource_id)
    second, _ = _guest_version(tls_material, root, "second", role=role or "worker", resource_id=resource_id)
    _swap_current(root, first)
    transport, resolutions, validations = _guest_transport(monkeypatch, root, role=role, resource_id=resource_id)
    validate = guest_bootstrap.load_identity

    def rotate_during_validation(directory, **kwargs):
        result = validate(directory, **kwargs)
        _swap_current(root, second)
        return result

    monkeypatch.setattr(guest_bootstrap, "load_identity", rotate_during_validation)
    loaded = []

    def create_context(*, cafile):
        context = SimpleNamespace(load_cert_chain=lambda cert, key: loaded.append((cafile, cert, key)))
        return context

    monkeypatch.setattr(ssl, "create_default_context", create_context)
    first_context = transport.context
    second_context = transport.controller_context
    assert resolutions == [first.resolve(), second.resolve()]
    assert validations == [(directory, {"role": role or "worker", "resource_id": resource_id, "generation": 1})
                           for directory in resolutions]
    assert loaded == [(str(directory / "ca.pem"), str(directory / "cert.pem"), str(directory / "key.pem"))
                      for directory in resolutions]
    assert first_context is not second_context
    assert first_context.check_hostname is False and second_context.check_hostname is True
    assert first_context.minimum_version == second_context.minimum_version == ssl.TLSVersion.TLSv1_2
    assert "context" not in vars(transport) and "controller_context" not in vars(transport)


@pytest.mark.asyncio
@pytest.mark.parametrize("context_name", ["context", "controller_context"])
async def test_real_tls_guest_client_credentials_reload_on_each_connection(tls_material, monkeypatch, context_name):
    root = tls_material.directory / "identity"
    resource_id = str(uuid4())
    first, first_pin = _guest_version(tls_material, root, "first", role="api", resource_id=resource_id)
    second, second_pin = _guest_version(tls_material, root, "second", role="api", resource_id=resource_id)
    _swap_current(root, first)
    transport, resolutions, validations = _guest_transport(monkeypatch, root, role="api", resource_id=resource_id)
    server_context, peer_pin = _tls_server_identity(
        tls_material, "server", san=resource_identity("api", resource_id, 1), dns="localhost")
    async with _tls_server(server_context) as (port, notices):
        for version, expected_pin in ((first, first_pin), (second, second_pin)):
            _swap_current(root, version)
            if context_name == "context":
                assert await transport.request(**_request_arguments(
                    address="127.0.0.1", port=port, resource_id=resource_id,
                    certificate_fingerprints=frozenset({peer_pin}))) == b"ok"
            else:
                reader, writer = await asyncio.open_connection(
                    "127.0.0.1", port, ssl=transport.controller_context, server_hostname="localhost")
                try:
                    writer.write(b"GET /v1/ready HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
                    await writer.drain()
                    assert (await asyncio.wait_for(reader.read(), 4)).endswith(b"ok")
                finally:
                    writer.close()
                    await writer.wait_closed()
            _, _, actual_pin = await asyncio.wait_for(notices.get(), 2)
            assert actual_pin == expected_pin
    assert resolutions == [first.resolve(), second.resolve()]
    assert [directory for directory, _ in validations] == resolutions
    assert all(kwargs == {"role": "api", "resource_id": resource_id, "generation": 1}
               for _, kwargs in validations)


@pytest.mark.asyncio
@pytest.mark.parametrize("context_name", ["context", "controller_context"])
async def test_guest_reload_validation_failure_never_reuses_previous_context(tls_material, monkeypatch, context_name):
    root = tls_material.directory / "identity"
    resource_id = str(uuid4())
    version, _ = _guest_version(tls_material, root, "first", role="worker", resource_id=resource_id)
    _swap_current(root, version)
    transport, resolutions, _ = _guest_transport(monkeypatch, root, role=None, resource_id=resource_id)
    assert isinstance(getattr(transport, context_name), ssl.SSLContext)

    def expired(directory, **kwargs):
        raise RuntimeError("guest certificate is invalid")

    monkeypatch.setattr(guest_bootstrap, "load_identity", expired)
    if context_name == "context":
        connections = []

        async def connect(*args, **kwargs):
            connections.append((args, kwargs))
            raise AssertionError("must not connect with an invalid identity")

        monkeypatch.setattr(asyncio, "open_connection", connect)
        with pytest.raises(RuntimeError, match="guest certificate is invalid"):
            await transport.request(**_request_arguments())
        assert connections == []
    else:
        with pytest.raises(RuntimeError, match="guest certificate is invalid"):
            transport.controller_context
    assert resolutions == [version.resolve(), version.resolve()]
