"""Renewable credentials: synthetic TLS wire, expiry, concurrency and no replay."""
from __future__ import annotations

import ipaddress
import json
import os
import ssl
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from keystoneauth1 import session
from keystoneauth1.identity import v3
from pydantic import ValidationError
from test_kubernetes_provider import _cluster, _pool
from test_kubernetes_provider import provider as provider

from lumen.services.infrastructure import sandbox_credentials as credentials
from lumen.services.infrastructure.config import KubernetesClusterProfile, RuntimeConfig

NOW = datetime(2026, 11, 18, tzinfo=UTC)
SOURCE = {
    "endpoint": "https://drover.test/v1", "auth_url": "https://identity.test/v3",
    "user_id": "native-owner", "project_id": "native-project",
    "password": {"file": "/etc/lumen/controller/drover/password"},
    "ca_file": "/etc/lumen/controller/drover/ca.pem",
    "cluster_id": str(UUID(int=40)), "namespace_uid": str(UUID(int=1)), "service_account": "lumen-controller",
}


def certificate(tmp_path):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "sandbox-wire-test")])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(NOW - timedelta(days=2))
            .not_valid_after(NOW + timedelta(days=365))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost"),
                x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
            .sign(key, hashes.SHA256()))
    pem = cert.public_bytes(serialization.Encoding.PEM)
    ca_path, key_path = tmp_path / "ca.pem", tmp_path / "key.pem"
    ca_path.write_bytes(pem)
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                         serialization.NoEncryption()))
    return pem, ca_path, key_path


class Clock:
    def __init__(self):
        self.now, self.tick = NOW, 0.0

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)
        self.tick += seconds


class Boundary:
    def __init__(self, pem, clock):
        self.pem, self.clock = pem, clock
        self.calls = []
        self.auth_change = {}
        self.scope_change = {}
        self.auth_status, self.drover_status = 201, 200
        self.sequence = 0
        self.entered, self.release = None, None

    def auth(self):
        return {"token": {"user": {"id": SOURCE["user_id"], "name": "owner", "domain": {"id": "domain"}},
            "project": {"id": SOURCE["project_id"], "name": "lumen-service", "domain": {"id": "domain"}},
            "methods": ["password"], "expires_at": (self.clock.now + timedelta(hours=1)).isoformat(),
            "issued_at": self.clock.now.isoformat(), "roles": [{"id": "role", "name": "member"}],
            "catalog": [], **self.auth_change}}

    def scoped(self):
        self.sequence += 1
        return {"server": "https://kubernetes.example:6443", "ca_pem": self.pem.decode(),
            "namespace": "runs", "namespace_uid": SOURCE["namespace_uid"],
            "service_account": SOURCE["service_account"], "runtime_class_name": "sandbox",
            "token": f"namespace-token-{self.sequence}",
            "expires_at": (self.clock.now + timedelta(seconds=900)).isoformat(), **self.scope_change}

    def __call__(self, request):
        self.calls.append(request)
        if request.url.path.endswith("/auth/tokens"):
            return httpx.Response(self.auth_status, json=self.auth(), headers={"X-Subject-Token": "native-human-token"})
        assert request.url.path == "/v1/clusters/" + SOURCE["cluster_id"] + "/sandbox-connection"
        if self.entered is not None:
            self.entered.set()
            assert self.release.wait(5)
        return httpx.Response(self.drover_status, json=self.scoped())


@pytest.fixture
def renewable(tmp_path, monkeypatch):
    pem, ca, _ = certificate(tmp_path)
    clock = Clock()
    boundary = Boundary(pem, clock)
    def material(path, *, password=False):
        assert path == SOURCE["password"]["file"] if password else path == SOURCE["ca_file"]
        return b"operator-password\n" if password else pem
    monkeypatch.setattr(credentials, "read_controller_file", material)
    cluster = KubernetesClusterProfile.model_validate(_cluster() | {"bearer_token": None, "drover": SOURCE,
                                                                    "ca_file": str(ca)})
    source = credentials.DroverSandboxCredentials(cluster, "lumen", "mcp", transport=httpx.MockTransport(boundary),
                                                 clock=lambda: clock.now, monotonic=lambda: clock.tick)
    yield SimpleNamespace(source=source, boundary=boundary, clock=clock, pem=pem, cluster=cluster)
    source.close()


def test_exclusive_source_and_secret_free_public_view():
    with pytest.raises(ValidationError, match="exactly one"):
        KubernetesClusterProfile.model_validate(_cluster() | {"drover": SOURCE})
    with pytest.raises(ValidationError, match="exactly one"):
        KubernetesClusterProfile.model_validate(_cluster() | {"bearer_token": None})
    raw = _cluster() | {"bearer_token": None, "drover": SOURCE}
    config = RuntimeConfig.model_validate({"kubernetes_clusters": [raw], "pools": [_pool()]})
    public = json.dumps(config.public_view())
    assert "drover" not in public and SOURCE["user_id"] not in public and "password" not in public


@pytest.mark.parametrize("change", [
    {"password": {"env": "PASSWORD"}}, {"password": {"file": "/tmp/password"}},
    {"password": {"file": "/etc/lumen/controller/drover/../password"}},
    {"ca_file": "/etc/lumen/controller/drover/../ca.pem"}, {"auth_url": "http://identity.test/v3"},
    {"endpoint": "https://owner:secret@drover.test"}, {"cluster_id": "choose-one"},
])
def test_reject_ambient_or_noncanonical_source(change):
    with pytest.raises(ValidationError):
        KubernetesClusterProfile.model_validate(_cluster() | {"bearer_token": None, "drover": SOURCE | change})


def test_refresh_singleflight_before_expiry_with_bounded_lease(renewable):
    source, boundary, clock = renewable.source, renewable.boundary, renewable.clock
    assert source.token() == "namespace-token-1"
    clock.advance(839)
    assert source.token() == "namespace-token-1"
    clock.advance(1)
    boundary.entered, boundary.release = threading.Event(), threading.Event()
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = [executor.submit(source.token) for _ in range(12)]
        assert boundary.entered.wait(5)
        boundary.release.set()
        assert [future.result(5) for future in futures] == ["namespace-token-2"] * 12
    assert len(boundary.calls) == 4
    assert "namespace-token" not in repr(source._lease)
    # A wall clock rollback cannot extend the monotonic lease.
    clock.now -= timedelta(hours=2)
    clock.tick += 841
    assert source.token() == "namespace-token-3"


def test_delayed_issuance_bounds_lifetime_at_receipt_not_request_start(renewable):
    original = renewable.boundary.scoped
    def delayed():
        renewable.clock.advance(5)
        return original()
    renewable.boundary.scoped = delayed
    assert renewable.source.token() == "namespace-token-1"
    assert renewable.source._lease.expires_at == NOW + timedelta(seconds=905)
    assert renewable.source._lease.deadline == 905
    renewable.clock.advance(839)
    assert renewable.source.token() == "namespace-token-1"


@pytest.mark.parametrize("change", [
    {"user": {"id": "foreign-owner"}}, {"project": {"id": "foreign-project"}},
    {"methods": ["application_credential"]}, {"system": {"all": True}}, {"domain": {"id": "domain"}},
    {"OS-TRUST:trust": {"id": "trust"}}, {"expires_at": (NOW + timedelta(seconds=599)).isoformat()},
    {"expires_at": NOW.isoformat()},
])
def test_foreign_reduced_or_stale_native_identity_rejected(renewable, change):
    renewable.boundary.auth_change = change
    with pytest.raises(credentials.SandboxCredentialError):
        renewable.source.token()
    assert len(renewable.boundary.calls) == 1


@pytest.mark.parametrize("change", [
    {"server": "https://foreign.test:6443"}, {"namespace": "other"}, {"namespace_uid": str(UUID(int=2))},
    {"service_account": "admin"}, {"runtime_class_name": "runc"}, {"ca_pem": "not a certificate"},
    {"expires_at": NOW.isoformat()}, {"expires_at": (NOW + timedelta(seconds=901)).isoformat()},
    {"expires_at": "2099-01-01"}, {"token": "admin token"}, {"token": "x" * 16385}, {"token": ""},
])
def test_scoped_metadata_or_token_drift_rejected(renewable, change):
    renewable.boundary.scope_change = change
    with pytest.raises(credentials.SandboxCredentialError):
        renewable.source.token()
    assert renewable.source._lease is None


def test_valid_foreign_ca_identity_rejected(renewable, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    pem, _, _ = certificate(other)
    renewable.boundary.scope_change["ca_pem"] = pem.decode()
    with pytest.raises(credentials.SandboxCredentialError, match="scope_rejected"):
        renewable.source.token()


@pytest.mark.parametrize("which", ["auth_status", "drover_status"])
def test_revoked_identity_or_drover_denial_has_no_cached_fallback(renewable, which):
    assert renewable.source.token() == "namespace-token-1"
    renewable.clock.advance(840)
    setattr(renewable.boundary, which, 403)
    with pytest.raises(credentials.SandboxCredentialError, match="request_rejected"):
        renewable.source.token()
    assert renewable.source._lease is None
    assert len(renewable.boundary.calls) == (3 if which == "auth_status" else 4)


def test_native_expiry_also_bounds_scoped_expiry(renewable):
    renewable.boundary.auth_change["expires_at"] = (NOW + timedelta(seconds=600)).isoformat()
    with pytest.raises(credentials.SandboxCredentialError, match="scope_rejected"):
        renewable.source.token()


def test_changed_protected_trust_stops_renewal(renewable, monkeypatch):
    assert renewable.source.token() == "namespace-token-1"
    renewable.clock.advance(840)
    monkeypatch.setattr(credentials, "read_controller_file", lambda *args, **kwargs: b"changed")
    with pytest.raises(credentials.SandboxCredentialError, match="material_changed"):
        renewable.source.token()
    assert len(renewable.boundary.calls) == 2


def test_concurrent_requests_keep_independent_authorization(provider):
    from lumen.services.infrastructure.kubernetes import KubernetesAPIError
    result, _ = provider
    entered, release = threading.Event(), threading.Event()
    state = threading.local()
    invalidated, observed = [], []
    result._credentials = SimpleNamespace(token=lambda: state.token, invalidate=invalidated.append, close=lambda: None)
    def handler(request):
        observed.append((request.url.path, request.headers["Authorization"]))
        if request.url.path == "/old":
            entered.set()
            assert release.wait(5)
            return httpx.Response(401, json={})
        release.set()
        return httpx.Response(200, json={})
    result.client.close()
    result.client = httpx.Client(base_url=result.cluster.server, transport=httpx.MockTransport(handler))
    def request(path, token):
        state.token = token
        return result._request("GET", path)
    with ThreadPoolExecutor(max_workers=2) as executor:
        old = executor.submit(request, "/old", "old-lease")
        assert entered.wait(5)
        assert executor.submit(request, "/new", "new-lease").result(5) == {}
        with pytest.raises(KubernetesAPIError):
            old.result(5)
    assert observed == [("/old", "Bearer old-lease"), ("/new", "Bearer new-lease")]
    assert invalidated == ["old-lease"]
    assert "Authorization" not in result.client.headers


@pytest.mark.parametrize("method,status", [("POST", 401), ("DELETE", 401), ("POST", "timeout"), ("DELETE", "timeout")])
def test_uncertain_mutation_never_replayed(provider, method, status):
    from lumen.services.infrastructure.kubernetes import KubernetesAPIError
    result, _ = provider
    calls, invalidated = [], []
    result._credentials = SimpleNamespace(token=lambda: "namespace-token", invalidate=invalidated.append, close=lambda: None)
    def handler(request):
        calls.append(request)
        if status == "timeout":
            raise httpx.ReadTimeout("uncertain", request=request)
        return httpx.Response(status, json={})
    result.client.close()
    result.client = httpx.Client(base_url=result.cluster.server, transport=httpx.MockTransport(handler))
    with pytest.raises((KubernetesAPIError, httpx.ReadTimeout)):
        result._request(method, "/api/v1/namespaces/runs/pods", body={"kind": "Pod"})
    assert len(calls) == 1
    assert invalidated == (["namespace-token"] if status == 401 else [])


def test_stale_401_cannot_invalidate_newer_lease(renewable):
    assert renewable.source.token() == "namespace-token-1"
    renewable.clock.advance(840)
    assert renewable.source.token() == "namespace-token-2"
    renewable.source.invalidate("namespace-token-1")
    assert renewable.source.token() == "namespace-token-2"
    renewable.source.invalidate("namespace-token-2")
    assert renewable.source.token() == "namespace-token-3"


def test_shutdown_drains_stream_before_closing_clients(provider):
    result, _ = provider
    entered, release, closing, closed = (threading.Event() for _ in range(4))
    result._credentials = SimpleNamespace(token=lambda: "namespace-token", invalidate=lambda token: None,
                                          close=lambda: closed.set())
    def handler(request):
        entered.set()
        assert release.wait(5)
        return httpx.Response(200, json={})
    result.client.close()
    result.client = httpx.Client(base_url=result.cluster.server, transport=httpx.MockTransport(handler))
    def close():
        closing.set()
        result.close()
    with ThreadPoolExecutor(max_workers=2) as executor:
        request = executor.submit(result._request, "GET", "/read")
        assert entered.wait(5)
        shutdown = executor.submit(close)
        assert closing.wait(5)
        with result._condition:
            assert result._closing
        assert not closed.is_set() and not result.client.is_closed
        with pytest.raises(RuntimeError, match="closed"):
            result._request("POST", "/new", body={})
        release.set()
        assert request.result(5) == {}
        shutdown.result(5)
    assert closed.is_set() and result.client.is_closed


def test_closed_source_never_reauthenticates(renewable):
    renewable.source.close()
    with pytest.raises(credentials.SandboxCredentialError, match="source_closed"):
        renewable.source.token()
    assert renewable.boundary.calls == []


def test_provider_builds_renewable_source_and_renews_without_replacing_http_client(renewable, provider, monkeypatch):
    from lumen.services.infrastructure import kubernetes
    original, _ = provider
    monkeypatch.setattr(kubernetes, "DroverSandboxCredentials", lambda cluster, deployment, pool: renewable.source)
    result = kubernetes.KubernetesProvider(renewable.cluster, original.pool, "lumen",
        controller_url=original.controller_url, controller_ca_pem=original.controller_ca_pem)
    result.client.close()
    observed = []
    def handler(request):
        observed.append(request.headers["Authorization"])
        return httpx.Response(200, json={})
    result.client = httpx.Client(base_url=result.cluster.server, transport=httpx.MockTransport(handler))
    client = result.client
    try:
        assert result._request("GET", "/one") == {}
        renewable.clock.advance(840)
        assert result._request("GET", "/two") == {}
        assert result.client is client
        assert observed == ["Bearer namespace-token-1", "Bearer namespace-token-2"]
    finally:
        result.close()


def test_provider_preflight_rejects_recreated_namespace_before_mutation(renewable, provider):
    result, api = provider
    result.cluster = renewable.cluster
    api.namespace["metadata"]["uid"] = str(UUID(int=99))
    with pytest.raises(ValueError, match="namespace UID"):
        result.preflight()
    assert [call[0] for call in api.calls] == ["GET"]


async def test_controller_closes_providers_after_drain_even_preflight_failure(monkeypatch):
    import lumen.controller as controller
    events = []
    instance = SimpleNamespace(close=lambda: events.append("close"))
    async def run(config, providers, *, stop=None):
        events.append("drain")
        raise ValueError("preflight failed")
    monkeypatch.setattr(controller, "_run_controller", run)
    with pytest.raises(ValueError, match="preflight failed"):
        await controller.run_controller(None, {"one": instance, "same": instance})
    assert events == ["drain", "close"]


@pytest.mark.parametrize("fault", [None, "symlink", "parent-link", "mode", "owner", "oversized", "parent-mode"])
def test_protected_password_uses_real_files_and_nofollow(tmp_path, monkeypatch, fault):
    root = tmp_path / "controller"
    directory = root / "drover"
    directory.mkdir(parents=True, mode=0o700)
    path = directory / "password"
    path.write_bytes(b"password")
    path.chmod(0o600)
    monkeypatch.setattr(credentials, "_CONTROLLER_ROOT", root)
    real_fstat = os.fstat
    def metadata(fd):
        original = real_fstat(fd)
        return SimpleNamespace(st_mode=original.st_mode, st_uid=1000 if fault == "owner" else 0, st_gid=0)
    monkeypatch.setattr(credentials.os, "fstat", metadata)
    if fault == "symlink":
        target = directory / "target"
        path.rename(target)
        path.symlink_to(target)
    elif fault == "parent-link":
        target = root / "other"
        directory.rename(target)
        directory.symlink_to(target)
    elif fault == "mode":
        path.chmod(0o644)
    elif fault == "oversized":
        path.write_bytes(b"x" * 16385)
    elif fault == "parent-mode":
        directory.chmod(0o777)
    if fault is None:
        assert credentials.read_controller_file(str(path), password=True) == b"password"
    else:
        with pytest.raises(credentials.SandboxCredentialError, match="material_invalid"):
            credentials.read_controller_file(str(path), password=True)


def test_installed_keystone_sdk_and_source_use_actual_scoped_https_wire(tmp_path, monkeypatch):
    pem, ca_path, key_path = certificate(tmp_path)
    clock = Clock()
    boundary = Boundary(pem, clock)
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append((self.path, body, self.headers.get("X-Auth-Token")))
            auth = self.path == "/v3/auth/tokens"
            response = boundary.auth() if auth else boundary.scoped()
            raw = json.dumps(response).encode()
            self.send_response(201 if auth else 200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            if auth:
                self.send_header("X-Subject-Token", "native-human-token")
            self.end_headers()
            self.wfile.write(raw)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(ca_path, key_path)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"https://127.0.0.1:{server.server_port}"
    source_values = SOURCE | {"endpoint": origin, "auth_url": origin + "/v3"}
    monkeypatch.setattr(credentials, "read_controller_file", lambda path, password=False: b"operator-password" if password else pem)
    cluster = KubernetesClusterProfile.model_validate(_cluster() | {"bearer_token": None, "drover": source_values,
                                                                    "ca_file": str(ca_path)})
    sdk_http = session.Session(verify=str(ca_path), connect_retries=0)
    source = None
    try:
        plugin = v3.Password(auth_url=origin + "/v3", user_id=SOURCE["user_id"], password="operator-password",
                             project_id=SOURCE["project_id"], reauthenticate=False)
        access = plugin.get_auth_ref(sdk_http)
        assert access.user_id == SOURCE["user_id"] and access.project_id == SOURCE["project_id"]
        assert access.auth_token == "native-human-token" and access.expires == NOW + timedelta(hours=1)
        source = credentials.DroverSandboxCredentials(cluster, "lumen", "chat-tools", clock=lambda: clock.now)
        assert source.token() == "namespace-token-1"
        assert calls[0][0] == calls[1][0] == "/v3/auth/tokens"
        assert calls[0][1] == calls[1][1] == {"auth": {"identity": {"methods": ["password"], "password": {
            "user": {"id": SOURCE["user_id"], "password": "operator-password"}}},
            "scope": {"project": {"id": SOURCE["project_id"]}}}}
        assert calls[2] == ("/v1/clusters/" + SOURCE["cluster_id"] + "/sandbox-connection",
            {"deployment_id": "lumen", "pool_name": "chat-tools", "runtime_class_name": "sandbox"}, "native-human-token")
        assert all("admin" not in path and "kubeconfig" not in path for path, _, _ in calls)
    finally:
        if source is not None:
            source.close()
        sdk_http.session.close()
        server.shutdown()
        server.server_close()
        thread.join(5)


def test_failed_refresh_is_singleflight_for_waiters_without_cached_fallback(renewable, monkeypatch):
    source, boundary = renewable.source, renewable.boundary
    boundary.entered, boundary.release = threading.Event(), threading.Event()
    boundary.drover_status = 403
    joined, count = threading.Event(), [0]
    wait_for = source._lock.wait_for
    def joined_wait(predicate, timeout=None):
        count[0] += 1
        if count[0] == 7:
            joined.set()
        return wait_for(predicate, timeout)
    monkeypatch.setattr(source._lock, "wait_for", joined_wait)
    with ThreadPoolExecutor(max_workers=8) as executor:
        first = executor.submit(source.token)
        assert boundary.entered.wait(5)
        waiting = [executor.submit(source.token) for _ in range(7)]
        assert joined.wait(5)
        boundary.release.set()
        for future in [first, *waiting]:
            with pytest.raises(credentials.SandboxCredentialError, match="request_rejected"):
                future.result(5)
    assert len(boundary.calls) == 2
    assert source._lease is None


def test_refresh_wait_is_bounded(renewable, monkeypatch):
    source = renewable.source
    source._refreshing = True
    waits = []
    def wait(predicate, timeout=None):
        waits.append(timeout)
        return False
    with monkeypatch.context() as patch:
        patch.setattr(source._lock, "wait_for", wait)
        try:
            with pytest.raises(credentials.SandboxCredentialError, match="refresh_busy"):
                source.token()
        finally:
            source._refreshing = False
    assert waits == [65]


def test_source_close_waits_for_refresh_and_never_returns_new_token(renewable):
    source, boundary = renewable.source, renewable.boundary
    boundary.entered, boundary.release = threading.Event(), threading.Event()
    with ThreadPoolExecutor(max_workers=2) as executor:
        refreshing = executor.submit(source.token)
        assert boundary.entered.wait(5)
        closing = executor.submit(source.close)
        with source._lock:
            assert source._lock.wait_for(lambda: source._closed, timeout=5)
        assert not source._client.is_closed
        boundary.release.set()
        with pytest.raises(credentials.SandboxCredentialError, match="source_closed"):
            refreshing.result(5)
        closing.result(5)
    assert source._client.is_closed and source._lease is None
