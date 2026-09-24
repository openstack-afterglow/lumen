"""Guest certificate exchange and identity fail-closed coverage."""
from __future__ import annotations

import json as json_module
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from lumen.services.infrastructure import guest_bootstrap
from lumen.services.infrastructure.config import RuntimeConfig
from lumen.services.infrastructure.transport import InternalTransport, resource_identity


def _ca():
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "guest test CA")])
    certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                   .public_key(key.public_key()).serial_number(x509.random_serial_number())
                   .not_valid_before(datetime.now(UTC) - timedelta(days=1))
                   .not_valid_after(datetime.now(UTC) + timedelta(days=1))
                   .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
                   .sign(key, hashes.SHA256()))
    return key, certificate.public_bytes(serialization.Encoding.PEM)


def test_worker_exchanges_csr_persists_private_identity_and_scrubs_token(tmp_path, monkeypatch):
    ca_key, ca_pem = _ca()
    token_file, ca_file = tmp_path / "token", tmp_path / "ca.pem"
    token_file.write_text("secret-token-for-one-use-bootstrap", encoding="ascii")
    ca_file.write_bytes(ca_pem)
    for path in (token_file, ca_file):
        path.chmod(0o600)
    identity_dir = tmp_path / "identity"
    resource_id = str(uuid4())
    generation = 7
    received = []

    class Response:
        status_code = 200
        def __enter__(self):
            return self
        def __exit__(self, *_):
            return None
        def iter_bytes(self):
            yield self.data

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["verify"] == str(ca_file) and kwargs["trust_env"] is False
        def __enter__(self):
            return self
        def __exit__(self, *_):
            return None
        def stream(self, method, url, *, json):
            assert method == "POST" and url == "https://controller.example/v1/sandbox/bootstrap"
            assert json["token"] == "secret-token-for-one-use-bootstrap"
            received.append(json)
            csr = x509.load_pem_x509_csr(json["csr_pem"].encode("ascii"))
            identity = resource_identity("worker", resource_id, generation)
            cert = (x509.CertificateBuilder().subject_name(x509.Name([]))
                    .issuer_name(x509.load_pem_x509_certificate(ca_pem).subject)
                    .public_key(csr.public_key()).serial_number(x509.random_serial_number())
                    .not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
                    .not_valid_after(datetime.now(UTC) + timedelta(minutes=30))
                    .add_extension(x509.SubjectAlternativeName([x509.UniformResourceIdentifier(identity)]), False)
                    .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), False)
                    .sign(ca_key, hashes.SHA256()))
            response = Response()
            response.data = json_module.dumps({"role": "worker", "resource_id": resource_id,
                "generation": generation, "client_ca_pem": ca_pem.decode("ascii"),
                "certificate_pem": cert.public_bytes(serialization.Encoding.PEM).decode("ascii")}).encode()
            return response

    monkeypatch.setattr(guest_bootstrap.httpx, "Client", Client)
    # The production entrypoint runs as root. In an unprivileged test process,
    # still exercise the same permission modes and the real cert/key verifier.
    monkeypatch.setattr(guest_bootstrap, "_require_root", lambda: None)
    monkeypatch.setattr(guest_bootstrap, "_private_file", lambda path: path.read_bytes())
    result = guest_bootstrap.bootstrap(role="worker", controller_url="https://controller.example",
                                       ca_file=ca_file, token_file=token_file, identity_dir=identity_dir)
    assert {key: result[key] for key in ("role", "resource_id", "generation")} == {
        "role": "worker", "resource_id": resource_id, "generation": generation}
    assert result["certificate_fingerprint"] == x509.load_pem_x509_certificate(
        (identity_dir / "cert.pem").read_bytes()).fingerprint(hashes.SHA256()).hex()
    assert received and not token_file.exists()
    for name in ("key.pem", "cert.pem", "ca.pem", "identity.json"):
        assert (identity_dir / name).stat().st_mode & 0o777 == 0o600
    assert guest_bootstrap.load_identity(identity_dir, role="worker", resource_id=resource_id,
                                         generation=generation) == result
    with pytest.raises(RuntimeError, match="identity mismatch"):
        guest_bootstrap.load_identity(identity_dir, role="worker", resource_id=str(uuid4()))
    with pytest.raises(RuntimeError, match="identity mismatch"):
        guest_bootstrap.load_identity(identity_dir, role="worker", generation=generation + 1)
    monkeypatch.setenv("LUMEN_GUEST_IDENTITY_DIR", str(identity_dir))
    monkeypatch.setenv("LUMEN_RESOURCE_ID", resource_id)
    monkeypatch.setenv("LUMEN_RESOURCE_GENERATION", str(generation))
    transport = InternalTransport(RuntimeConfig(managed_networks=("10.0.0.0/24",)))
    assert transport.context.check_hostname is False
    assert transport.controller_context.check_hostname is True
    import asyncio
    from types import SimpleNamespace

    from lumen import worker
    from lumen.services.infrastructure import store

    recorded = []
    async def register_worker(**kwargs):
        recorded.append(kwargs)
        return "registered"
    monkeypatch.setattr(store, "register_worker", register_worker)
    monkeypatch.setattr(worker, "get_registry", lambda: SimpleNamespace(digest="test-digest"))
    loop = worker.WorkerLoop(owner="test", capacity=1, heartbeat_seconds=5, drain_seconds=30)
    asyncio.run(loop.register())
    assert recorded[0]["resource_id"] == resource_id
    assert recorded[0]["resource_generation"] == generation
    assert recorded[0]["certificate_fingerprint"] == result["certificate_fingerprint"]
    monkeypatch.setenv("LUMEN_RESOURCE_GENERATION", "8")
    with pytest.raises(RuntimeError, match="identity mismatch"):
        InternalTransport(RuntimeConfig(managed_networks=("10.0.0.0/24",)))


def test_unissued_worker_resource_cannot_register(monkeypatch):
    from lumen.services.infrastructure import store
    from lumen.worker import WorkerLoop
    called = []
    async def register_worker(**kwargs):
        called.append(kwargs)
        return "registered"
    monkeypatch.setattr(store, "register_worker", register_worker)
    monkeypatch.setenv("LUMEN_RESOURCE_ID", str(uuid4()))
    monkeypatch.delenv("LUMEN_GUEST_IDENTITY_DIR", raising=False)
    loop = WorkerLoop(owner="test", capacity=1, heartbeat_seconds=5, drain_seconds=30)
    import asyncio
    with pytest.raises(RuntimeError, match="identity missing"):
        asyncio.run(loop.register())
    assert not called
