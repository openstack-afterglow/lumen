"""A managed guest must be authenticated before any dispatch secret leaves the worker."""
from __future__ import annotations

import asyncio
import hashlib
import ssl
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from lumen.services.infrastructure import bootstrap
from lumen.services.infrastructure.transport import InternalTransport, InternalTransportError, resource_identity


def _certificate(key, issuer, signer, *, san=None, client=False):
    subject = issuer if signer is key else x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "Lumen test guest")])
    builder = (x509.CertificateBuilder().subject_name(subject)
        .issuer_name(issuer).public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
        .not_valid_after(datetime.now(UTC) + timedelta(minutes=10)))
    if signer is key:
        builder = builder.add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        builder = builder.add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
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
        builder = builder.add_extension(x509.SubjectAlternativeName([
            x509.UniformResourceIdentifier(san)]), critical=False)
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
    client_context = ssl.create_default_context(cafile=str(ca_file))
    client_context.check_hostname = False
    client_context.load_cert_chain(*client_files)

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
    transport = object.__new__(InternalTransport)
    transport.context = client_context
    transport.max_request_bytes = 256 * 1024
    transport.max_response_bytes = 1024 * 1024
    # The production address gate rejects loopback; this test isolates TLS using
    # local sockets instead of needing an operator-managed private network.
    monkeypatch.setattr(transport, "_address", lambda address, port: address)
    path = f"/v1/artifacts/{uuid4()}"

    async def attempt(*, fingerprint, identity):
        return await transport.request(address="127.0.0.1", port=port, role="sandbox",
                                       resource_id=identity, generation=1,
                                       certificate_fingerprint=fingerprint,
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
        x509.NameAttribute(NameOID.COMMON_NAME, "Lumen test CA")]), ca_key)
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
