"""Guest runtime contract tests. These tests never need root or appuser on the host."""
from __future__ import annotations

import json
import stat
import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from guest_runtime_support import counters, guest_config, identity_fixture, issue, portable_permissions

from lumen.services.infrastructure import guest_api, guest_bootstrap
from lumen.services.infrastructure.transport import resource_identity


def expected(data):
    return {"LUMEN_GUEST_ROLE": data["role"], "LUMEN_GUEST_PROFILE_ID": data["profile_id"],
            "LUMEN_GUEST_PROFILE_DIGEST": data["profile_digest"], "LUMEN_GUEST_IMAGE": data["image"]}


@pytest.mark.parametrize("key,value", [("profile_id", "wrong"), ("profile_digest", "d" * 64),
    ("image", "other@sha256:" + "e" * 64), ("role", "worker"), ("resource_id", "wrong"), ("generation", 2)])
def test_guest_config_mismatches_fail_closed(tmp_path, monkeypatch, key, value):
    _, identity, _, _ = identity_fixture(tmp_path, monkeypatch)
    data = guest_config(identity)
    boot = expected(data)
    image = data["image"]
    data[key] = value
    with pytest.raises(RuntimeError, match="identity mismatch"):
        guest_bootstrap.verify_guest_config(data, identity, boot, image)


@pytest.mark.parametrize("running_image", ["", "unknown", "other@sha256:" + "f" * 64])
def test_unverified_running_image_never_starts(tmp_path, monkeypatch, running_image):
    _, identity, _, _ = identity_fixture(tmp_path, monkeypatch)
    data = guest_config(identity)
    with pytest.raises(RuntimeError, match="identity mismatch"):
        guest_bootstrap.verify_guest_config(data, identity, expected(data), running_image)


def test_missing_required_secret_is_closed(tmp_path, monkeypatch):
    _, identity, _, _ = identity_fixture(tmp_path, monkeypatch)
    data = guest_config(identity)
    del data["secrets"]["REDIS_URL"]
    with pytest.raises(RuntimeError, match="config invalid"):
        guest_bootstrap.verify_guest_config(data, identity, expected(data), data["image"])


def test_versions_atomic_pointer_permissions_and_config(tmp_path, monkeypatch):
    ownership = []
    directory, identity, _, _ = identity_fixture(tmp_path, monkeypatch, role="worker")
    monkeypatch.setattr(guest_bootstrap.os, "chown", lambda path, uid, gid: ownership.append((uid, gid)))
    monkeypatch.setattr(guest_bootstrap.os, "fchown", lambda fd, uid, gid: ownership.append((uid, gid)))
    version = guest_bootstrap.identity_version(directory)
    assert (directory / "current").is_symlink()
    assert stat.S_IMODE(directory.stat().st_mode) == 0o750
    assert stat.S_IMODE(version.stat().st_mode) == 0o750
    for name in ("key.pem", "cert.pem", "ca.pem", "identity.json"):
        assert stat.S_IMODE((version / name).stat().st_mode) == 0o440
        assert guest_bootstrap._identity_file(version / name)
    with pytest.raises(RuntimeError, match="permissions invalid"):
        guest_bootstrap._bootstrap_file(version / "key.pem")
    data = guest_config(identity)
    environment = guest_bootstrap.install_config(data, tmp_path / "run")
    assert environment["WORKER_WORKLOAD_CLASSES"] == '["batch"]'
    conf = Path(environment["LUMEN_CONFIG_FILE"])
    assert tomllib.loads(conf.read_text())["lumen"] == data["config"]
    assert stat.S_IMODE(conf.stat().st_mode) == 0o440
    assert stat.S_IMODE((conf.parent / "secrets.json").stat().st_mode) == 0o440
    assert ownership and all(item == (0, 10001) for item in ownership)
    (version / "key.pem").chmod(0o444)
    with pytest.raises(RuntimeError, match="permissions invalid"):
        guest_bootstrap.load_identity(directory, role="worker")


def test_token_reader_rejects_group_access_and_symlinks(tmp_path, monkeypatch):
    portable_permissions(monkeypatch)
    path = tmp_path / "bootstrap" / "token"
    guest_bootstrap._atomic_file(path, b"one-use-token")
    assert guest_bootstrap._bootstrap_file(path) == b"one-use-token"
    path.chmod(0o640)
    with pytest.raises(RuntimeError):
        guest_bootstrap._bootstrap_file(path)
    path.chmod(0o600)
    link = path.parent / "link"
    link.symlink_to(path.name)
    with pytest.raises(OSError):
        guest_bootstrap._bootstrap_file(link)


def test_drain_fences_survive_restart_and_reject_lower(tmp_path, monkeypatch):
    portable_permissions(monkeypatch)
    identity = {"resource_id": "resource", "generation": 7}
    path = tmp_path / "drain" / "state.json"
    drain = guest_api.DrainState(path, identity)
    drain.accept({**identity, "fence": 12})
    drain.accept({**identity, "fence": 12})
    restart = guest_api.DrainState(path, identity)
    assert restart.fence == 12 and not restart.acknowledged
    with pytest.raises(ValueError, match="stale"):
        restart.accept({**identity, "fence": 11})
    assert json.loads(path.read_text())["fence"] == 12
    restart.accept({**identity, "fence": 13})
    assert guest_api.DrainState(path, identity).fence == 13
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    with pytest.raises(RuntimeError):
        guest_api.DrainState(path, {**identity, "generation": 8})
    with pytest.raises(ValueError):
        restart.accept({**identity, "fence": True})


def test_verified_counters_independent_of_dependencies_and_never_stale_zeros():
    unavailable = counters(status="unavailable", database=False)
    assert guest_api.verified_load(json.dumps(unavailable).encode())["active_requests"] == 0
    assert not guest_api.dependency_ready(json.dumps(unavailable).encode())
    for update in ({"observed_at": (datetime.now(UTC) - timedelta(seconds=11)).isoformat()},
                   {"active_ws": None}, {"active_requests": True}, {"active_sse": 1}):
        assert guest_api.verified_load(json.dumps(counters(**update)).encode()) is None
    assert guest_api.verified_load(b'{}') is None


def test_renewal_persists_identical_csr_and_key_before_network_and_replays_activate(tmp_path, monkeypatch):
    directory, identity, ca_key, ca_cert = identity_fixture(tmp_path, monkeypatch)
    agent = guest_api.RenewalAgent(directory, identity, "https://controller")
    requests = []
    fail = {"renew": True, "activate": True}
    certs = {}

    def controller(url, path, version, *, body=None):
        state = json.loads(agent.state_file.read_text())
        assert stat.S_IMODE(agent.state_file.stat().st_mode) == 0o600
        assert stat.S_IMODE(agent.state_file.parent.stat().st_mode) == 0o700
        assert body["renewal_request_id"] == state["renewal_request_id"]
        if path.endswith("/renew"):
            requests.append(body.copy())
            if fail["renew"]:
                fail["renew"] = False
                raise OSError("lost response")
            csr = x509.load_pem_x509_csr(body["csr_pem"].encode())
            cert = issue(ca_key, ca_cert, csr.public_key(), identity=resource_identity(**identity))
            certs[body["renewal_request_id"]] = cert
            return {"certificate_pem": cert.public_bytes(serialization.Encoding.PEM).decode(),
                    "ca_pem": ca_cert.public_bytes(serialization.Encoding.PEM).decode()}
        assert version.name == state["renewal_request_id"]
        assert guest_bootstrap.load_identity(version, **identity)
        assert guest_bootstrap.identity_version(directory) != version
        if fail["activate"]:
            fail["activate"] = False
            raise OSError("activation response lost")
        certificate = x509.load_pem_x509_certificate((version / "cert.pem").read_bytes())
        return {"activated": True, "not_after": int(certificate.not_valid_after_utc.timestamp())}

    monkeypatch.setattr(guest_api, "controller_request", controller)
    with pytest.raises(OSError):
        agent.renew()
    original = json.loads(agent.state_file.read_text())
    with pytest.raises(OSError):
        guest_api.RenewalAgent(directory, identity, "https://controller").renew()
    assert len(requests) == 2 and requests[0] == requests[1]
    assert json.loads(agent.state_file.read_text()) == original
    recovered = guest_api.RenewalAgent(directory, identity, "https://controller").renew()
    assert len(requests) == 2  # complete pending version recovers without a new renewal/key
    guest_bootstrap.activate_version(directory, recovered)
    agent.complete()
    assert not agent.state_file.exists()


@pytest.mark.asyncio
async def test_worker_heartbeat_reads_rotation_and_drains_invalid_identity(tmp_path, monkeypatch):
    directory, identity, _, _ = identity_fixture(tmp_path, monkeypatch, role="worker")
    monkeypatch.setenv("LUMEN_GUEST_IDENTITY_DIR", str(directory))
    monkeypatch.setenv("LUMEN_RESOURCE_ID", identity["resource_id"])
    monkeypatch.setenv("LUMEN_RESOURCE_GENERATION", "1")
    from lumen.services.infrastructure import store
    from lumen.worker import WorkerLoop
    received = []
    async def heartbeat(registration_id, **kwargs):
        received.append(kwargs)
        return True
    monkeypatch.setattr(store, "heartbeat_worker", heartbeat)
    loop = WorkerLoop(owner="test", capacity=1, heartbeat_seconds=5, drain_seconds=30, workload_classes=("batch",))
    loop.registration_id = "test-registration"
    await loop.heartbeat()
    assert received[-1]["certificate_fingerprint"] == guest_bootstrap.load_identity(directory, role="worker")["certificate_fingerprint"]
    monkeypatch.setenv("LUMEN_RESOURCE_GENERATION", "2")
    await loop.heartbeat()
    assert loop.draining.is_set() and len(received) == 1


@pytest.mark.asyncio
async def test_cutoff_stops_admissions_then_terminates_real_process_group(tmp_path, monkeypatch):
    """Fake clock drives production supervisor cutoff; child lifecycle is observed."""
    import asyncio
    import os
    import subprocess
    import sys
    directory, identity, _, _ = identity_fixture(tmp_path, monkeypatch, role="worker")
    data = guest_config(identity)
    real_datetime = datetime
    clock = {"now": real_datetime.now(UTC)}
    class Clock:
        @staticmethod
        def now(tz):
            return clock["now"]
    monkeypatch.setattr(guest_api, "datetime", Clock)
    monkeypatch.setattr(guest_api, "service_account", lambda: SimpleNamespace(
        pw_uid=os.getuid(), pw_gid=os.getgid(), pw_dir=str(tmp_path), pw_name="test-user"))
    real_popen = subprocess.Popen
    children = []
    def spawn(command, **kwargs):
        # Host tests don't require root; Linux privilege dropping is asserted separately.
        assert kwargs.pop("user") == os.getuid()
        assert kwargs.pop("group") == os.getgid()
        assert kwargs.pop("extra_groups") == []
        assert kwargs["start_new_session"] is True
        process = real_popen(command, **kwargs)
        children.append(process)
        return process
    monkeypatch.setattr(guest_api.subprocess, "Popen", spawn)
    task = asyncio.create_task(guest_api._run([sys.executable, "-c", "import time; time.sleep(300)"],
        directory, guest_config=data, drain_path=tmp_path / "drain" / "state.json"))
    try:
        await asyncio.sleep(0.05)
        assert len(children) == 1 and children[0].poll() is None
        clock["now"] += timedelta(hours=2)
        await asyncio.wait_for(task, timeout=15)
        assert children[0].poll() is not None
    finally:
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        for process in children:
            if process.poll() is None:
                process.kill()
                process.wait()


def test_guest_config_mismatch_never_launches_child(tmp_path, monkeypatch):
    import sys
    directory, identity, _, _ = identity_fixture(tmp_path, monkeypatch)
    data = guest_config(identity)
    environment = {**expected(data), "LUMEN_CONTROLLER_URL": "https://controller", "LUMEN_CONTROLLER_CA": str(tmp_path / "ca"),
                   "LUMEN_BOOTSTRAP_FILE": str(tmp_path / "token")}
    bootstrap = tmp_path / "bootstrap"
    guest_bootstrap._atomic_file(bootstrap / "environment", "\n".join(k + "=" + v for k, v in environment.items()).encode())
    monkeypatch.setattr(guest_bootstrap, "BOOTSTRAP_DIR", bootstrap)
    monkeypatch.setenv("LUMEN_GUEST_IDENTITY_DIR", str(directory))
    monkeypatch.setenv("LUMEN_GUEST_IMAGE", data["image"])
    monkeypatch.setattr(guest_bootstrap, "_require_root", lambda: None)
    data["generation"] = 2
    monkeypatch.setattr(guest_bootstrap, "controller_request", lambda *args, **kwargs: data)
    called = []
    monkeypatch.setattr(guest_api, "run", lambda *args, **kwargs: called.append(args))
    monkeypatch.setattr(sys, "argv", ["guest_bootstrap", "--role", "api", "--", "service"])
    with pytest.raises(RuntimeError, match="identity mismatch"):
        guest_bootstrap.main()
    assert not called
    assert not (tmp_path / "run" / "lumen.conf").exists()


@pytest.mark.asyncio
async def test_pre_cutoff_api_drain_persists_and_worker_receives_term(tmp_path, monkeypatch):
    directory, identity, _, _ = identity_fixture(tmp_path, monkeypatch)
    runtime = guest_api.GuestRuntime(directory, guest_config(identity), service_port=8080,
                                    drain_path=tmp_path / "drain" / "state.json")
    forwarded = []
    async def forward(port, token):
        assert runtime.drain.fence == 0
        assert json.loads(runtime.drain.path.read_text())["fence"] == 0
        forwarded.append((port, token))
        runtime.drain.acknowledged = True
    monkeypatch.setattr(runtime.drain, "forward", forward)
    await runtime.admission_stop(SimpleNamespace(pid=999))
    assert forwarded == [(8080, runtime.token)]
    runtime.service_port = None
    signals = []
    monkeypatch.setattr(guest_api.os, "killpg", lambda pid, sig: signals.append((pid, sig)))
    await runtime.admission_stop(SimpleNamespace(pid=999))
    assert signals == [(999, guest_api.signal.SIGTERM)]


def test_service_account_cannot_be_root(monkeypatch):
    monkeypatch.setattr(guest_bootstrap.pwd, "getpwnam", lambda name: SimpleNamespace(pw_uid=0, pw_gid=10001))
    with pytest.raises(RuntimeError, match="must not be root"):
        guest_bootstrap.service_account()


def test_expired_pending_reissues_same_persisted_request_not_new_key(tmp_path, monkeypatch):
    directory, identity, ca_key, ca_cert = identity_fixture(tmp_path, monkeypatch)
    agent = guest_api.RenewalAgent(directory, identity, "https://controller")
    requests = []
    activations = {"count": 0}
    def controller(url, path, version, *, body=None):
        if path.endswith("/renew"):
            requests.append(body.copy())
            csr = x509.load_pem_x509_csr(body["csr_pem"].encode())
            certificate = issue(ca_key, ca_cert, csr.public_key(), identity=resource_identity(**identity))
            return {"ca_pem": ca_cert.public_bytes(serialization.Encoding.PEM).decode(),
                    "certificate_pem": certificate.public_bytes(serialization.Encoding.PEM).decode()}
        activations["count"] += 1
        if activations["count"] == 1:
            raise OSError("lost activation response before commit")
        if activations["count"] == 2:
            raise RuntimeError("pending expired")
        certificate = x509.load_pem_x509_certificate((version / "cert.pem").read_bytes())
        return {"activated": True, "not_after": int(certificate.not_valid_after_utc.timestamp())}
    monkeypatch.setattr(guest_api, "controller_request", controller)
    with pytest.raises(OSError):
        agent.renew()
    original = json.loads(agent.state_file.read_text())
    version = guest_api.RenewalAgent(directory, identity, "https://controller").renew()
    assert len(requests) == 2 and requests[0] == requests[1]
    assert json.loads(agent.state_file.read_text()) == original
    assert version.name == original["renewal_request_id"]
    assert activations["count"] == 3


def test_renewal_ca_mismatch_does_not_swap_or_delete_persisted_key(tmp_path, monkeypatch):
    from guest_runtime_support import ca_material
    directory, identity, ca_key, ca_cert = identity_fixture(tmp_path, monkeypatch)
    before = guest_bootstrap.identity_version(directory)
    _, wrong_ca = ca_material()
    def controller(url, path, version, *, body=None):
        assert path.endswith("/renew")
        csr = x509.load_pem_x509_csr(body["csr_pem"].encode())
        cert = issue(ca_key, ca_cert, csr.public_key(), identity=resource_identity(**identity))
        return {"certificate_pem": cert.public_bytes(serialization.Encoding.PEM).decode(),
                "ca_pem": wrong_ca.public_bytes(serialization.Encoding.PEM).decode()}
    monkeypatch.setattr(guest_api, "controller_request", controller)
    agent = guest_api.RenewalAgent(directory, identity, "https://controller")
    with pytest.raises(RuntimeError, match="CA mismatch"):
        agent.renew()
    assert guest_bootstrap.identity_version(directory) == before
    assert agent.state_file.exists()


@pytest.mark.parametrize("response", [{}, {"activated": False, "not_after": 1},
                                      {"activated": True, "not_after": True},
                                      {"activated": True, "not_after": 1}])
def test_activation_requires_verified_success_envelope(tmp_path, monkeypatch, response):
    directory, identity, _, _ = identity_fixture(tmp_path, monkeypatch)
    before = guest_bootstrap.identity_version(directory)
    agent = guest_api.RenewalAgent(directory, identity, "https://controller")
    monkeypatch.setattr(guest_api, "controller_request", lambda *args, **kwargs: response)
    with pytest.raises(RuntimeError, match="activation response invalid"):
        agent.activate(before, "00000000-0000-0000-0000-000000000001")
    assert guest_bootstrap.identity_version(directory) == before


def test_boot_replays_pending_activation_before_guest_config_even_if_old_leaf_expired(tmp_path, monkeypatch):
    import sys
    directory, identity, ca_key, ca_cert = identity_fixture(tmp_path, monkeypatch)
    data = guest_config(identity)
    agent = guest_api.RenewalAgent(directory, identity, "https://controller")
    before = guest_bootstrap.identity_version(directory)
    lost = {"activate": True}
    def controller(url, path, version, *, body=None):
        if path.endswith("/renew"):
            csr = x509.load_pem_x509_csr(body["csr_pem"].encode())
            cert = issue(ca_key, ca_cert, csr.public_key(), identity=resource_identity(**identity))
            return {"ca_pem": ca_cert.public_bytes(serialization.Encoding.PEM).decode(),
                    "certificate_pem": cert.public_bytes(serialization.Encoding.PEM).decode()}
        if lost["activate"]:
            lost["activate"] = False
            raise OSError("committed activation response lost")
        cert = x509.load_pem_x509_certificate((version / "cert.pem").read_bytes())
        return {"activated": True, "not_after": int(cert.not_valid_after_utc.timestamp())}
    monkeypatch.setattr(guest_api, "controller_request", controller)
    with pytest.raises(OSError):
        agent.renew()
    old_key = serialization.load_pem_private_key((before / "key.pem").read_bytes(), password=None)
    expired = issue(ca_key, ca_cert, old_key.public_key(), identity=resource_identity(**identity), seconds=-1)
    guest_bootstrap._atomic_file(before / "cert.pem", expired.public_bytes(serialization.Encoding.PEM), shared=True)
    with pytest.raises(RuntimeError, match="certificate is invalid"):
        guest_bootstrap.load_identity(directory, role="api")
    environment = {**expected(data), "LUMEN_CONTROLLER_URL": "https://controller", "LUMEN_CONTROLLER_CA": str(tmp_path / "ca"),
                   "LUMEN_BOOTSTRAP_FILE": str(tmp_path / "token")}
    bootstrap = tmp_path / "bootstrap"
    guest_bootstrap._atomic_file(bootstrap / "environment", "\n".join(k + "=" + v for k, v in environment.items()).encode())
    monkeypatch.setattr(guest_bootstrap, "BOOTSTRAP_DIR", bootstrap)
    monkeypatch.setenv("LUMEN_GUEST_IDENTITY_DIR", str(directory))
    monkeypatch.setenv("LUMEN_GUEST_IMAGE", data["image"])
    monkeypatch.setattr(guest_bootstrap, "_require_root", lambda: None)
    def config_request(url, path, version, **kwargs):
        assert path == "/v1/runtime/guest-config"
        assert guest_bootstrap.identity_version(directory) != before
        assert not agent.state_file.exists()
        return data
    monkeypatch.setattr(guest_bootstrap, "controller_request", config_request)
    monkeypatch.setattr(guest_bootstrap, "install_config", lambda data: {})
    launches = []
    monkeypatch.setattr(guest_api, "run", lambda *args, **kwargs: launches.append(args) or 0)
    monkeypatch.setattr(sys, "argv", ["guest_bootstrap", "--role", "api", "--", "service"])
    with pytest.raises(SystemExit) as exited:
        guest_bootstrap.main()
    assert exited.value.code == 0 and len(launches) == 1
    assert guest_bootstrap.load_identity(directory, role="api")
