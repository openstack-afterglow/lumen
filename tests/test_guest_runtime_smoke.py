"""Locally runnable real-socket/real-child renewal and drain smoke.

Run with pytest tests/test_guest_runtime_smoke.py. Only host UID/GID metadata is
emulated; both private listeners, TLS handshakes and child PID are real.
"""
from __future__ import annotations

import asyncio
import json
import os
import ssl
import subprocess
import sys
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from guest_runtime_support import guest_config, identity_fixture, issue, pem_key

from lumen.services.infrastructure import guest_api, guest_bootstrap
from lumen.services.infrastructure.transport import resource_identity

_CHILD = r'''
import hmac, json, os
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
fence = int(os.environ['LUMEN_INTERNAL_DRAIN_FENCE']) if 'LUMEN_INTERNAL_DRAIN_FENCE' in os.environ else None
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def reply(self, status, data):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def do_GET(self):
        if self.path != '/v1/ready?include_load=1':
            return self.reply(404, {})
        self.reply(503, {'status':'unavailable','database':False,'plugins':True,'checkpointer':None,
            'active_requests':0,'active_sse':0,'active_ws':0,'ttft_samples':0,'p95_ttft_ms':None,
            'observed_at':datetime.now(UTC).isoformat(), 'draining':fence is not None,
            'drain_fence':fence,'drain_acknowledged':fence is not None})
    def do_POST(self):
        global fence
        if self.path != '/v1/internal/drain' or not hmac.compare_digest(
            self.headers.get('X-Lumen-Drain-Token',''), os.environ['LUMEN_INTERNAL_DRAIN_TOKEN']):
            return self.reply(403, {})
        data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        if fence is not None and data['fence'] < fence:
            return self.reply(409, {})
        fence = data['fence']
        self.reply(200, {'draining':True,'drain_fence':fence,'drain_acknowledged':True})
server = HTTPServer(('127.0.0.1',0), Handler)
print(json.dumps({'pid':os.getpid(),'port':server.server_port}), flush=True)
server.serve_forever()
'''


@pytest.mark.asyncio
async def test_real_tls_two_rotations_lost_responses_drain_restart_and_unchanged_child(tmp_path, monkeypatch):
    directory, identity, ca_key, ca_cert = identity_fixture(tmp_path, monkeypatch)
    ca_pem = ca_cert.public_bytes(serialization.Encoding.PEM)
    ca_path = tmp_path / "ca.pem"
    ca_path.write_bytes(ca_pem)
    server_key = ec.generate_private_key(ec.SECP256R1())
    server_cert = issue(ca_key, ca_cert, server_key.public_key(), hostname=True)
    controller_cert = tmp_path / "controller.pem"
    controller_key = tmp_path / "controller-key.pem"
    controller_cert.write_bytes(server_cert.public_bytes(serialization.Encoding.PEM))
    controller_key.write_bytes(pem_key(server_key))
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.load_cert_chain(str(controller_cert), str(controller_key))
    context.load_verify_locations(cafile=str(ca_path))
    context.verify_mode = ssl.CERT_REQUIRED
    initial_pin = guest_bootstrap.load_identity(directory, role="api")["certificate_fingerprint"]
    active = {"pin": initial_pin, "previous": None, "request_id": None}
    pending = {}
    renew_requests = []
    lose = {"renew": True, "activate": True}

    async def controller(reader, writer):
        try:
            headers = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            lines = headers.decode().split("\r\n")
            path = lines[0].split()[1]
            length = next(int(line.split(":", 1)[1]) for line in lines if line.lower().startswith("content-length:"))
            body = json.loads(await reader.readexactly(length))
            peer = writer.get_extra_info("ssl_object").getpeercert(binary_form=True)
            fingerprint = x509.load_der_x509_certificate(peer).fingerprint(hashes.SHA256()).hex()
            request_id = body["renewal_request_id"]
            if path.endswith("/renew"):
                assert fingerprint in {active["pin"], active["previous"]}
                renew_requests.append(body.copy())
                if request_id not in pending:
                    csr = x509.load_pem_x509_csr(body["csr_pem"].encode())
                    cert = issue(ca_key, ca_cert, csr.public_key(), identity=resource_identity(**identity),
                                 seconds=7200 + len(pending) * 3600)
                    pending[request_id] = {"cert": cert, "csr": body["csr_pem"]}
                assert pending[request_id]["csr"] == body["csr_pem"]
                cert = pending[request_id]["cert"]
                response = {"certificate_pem": cert.public_bytes(serialization.Encoding.PEM).decode(),
                            "ca_pem": ca_pem.decode(), "not_after": int(cert.not_valid_after_utc.timestamp()),
                            "pending_expires_at": int((datetime.now(UTC) + timedelta(seconds=120)).timestamp())}
                if lose["renew"]:
                    lose["renew"] = False
                    return  # issuance committed, response lost
            elif path.endswith("/activate"):
                cert = pending[request_id]["cert"]
                assert fingerprint == cert.fingerprint(hashes.SHA256()).hex()
                if active["request_id"] != request_id:
                    active.update(previous=active["pin"], pin=fingerprint, request_id=request_id)
                response = {"activated": True, "not_after": int(cert.not_valid_after_utc.timestamp())}
                if lose["activate"]:
                    lose["activate"] = False
                    return  # activation committed, response lost
            else:
                raise AssertionError(path)
            encoded = json.dumps(response).encode()
            writer.write(b"HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: "
                         + str(len(encoded)).encode() + b"\r\n\r\n" + encoded)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    controller_server = await asyncio.start_server(controller, "127.0.0.1", 0, ssl=context)
    monkeypatch.setenv("LUMEN_CONTROLLER_URL", f"https://localhost:{controller_server.sockets[0].getsockname()[1]}")
    operator_key = ec.generate_private_key(ec.SECP256R1())
    operator = issue(ca_key, ca_cert, operator_key.public_key(), identity="spiffe://lumen/operator/probe")
    operator_cert, operator_key_path = tmp_path / "operator.pem", tmp_path / "operator-key.pem"
    operator_cert.write_bytes(operator.public_bytes(serialization.Encoding.PEM))
    operator_key_path.write_bytes(pem_key(operator_key))
    client_context = ssl.create_default_context(cafile=str(ca_path))
    client_context.check_hostname = False
    client_context.load_cert_chain(str(operator_cert), str(operator_key_path))
    data = guest_config(identity)
    data["operator_probe_fingerprints"] = [operator.fingerprint(hashes.SHA256()).hex()]
    drain_path = tmp_path / "drain" / "state.json"
    runtime = guest_api.GuestRuntime(directory, data, drain_path=drain_path)
    child = subprocess.Popen([sys.executable, "-u", "-c", _CHILD], stdout=subprocess.PIPE,
                             text=True, env={**os.environ, "LUMEN_INTERNAL_DRAIN_TOKEN": runtime.token})
    listener = None
    restarted_child = None
    try:
        started = json.loads(await asyncio.wait_for(asyncio.to_thread(child.stdout.readline), 5))
        pid = started["pid"]
        runtime.service_port = started["port"]
        assert child.pid == pid and child.poll() is None
        listener = await asyncio.start_server(runtime.probe, "127.0.0.1", 0, ssl=runtime.context)
        port = listener.sockets[0].getsockname()[1]

        async def listener_pin():
            reader, writer = await asyncio.open_connection("127.0.0.1", port, ssl=client_context)
            certificate = writer.get_extra_info("ssl_object").getpeercert(binary_form=True)
            fingerprint = x509.load_der_x509_certificate(certificate).fingerprint(hashes.SHA256()).hex()
            writer.write(b"GET /v1/ready HTTP/1.1\r\nHost: localhost\r\n\r\n")
            await writer.drain()
            await reader.read()
            writer.close()
            await writer.wait_closed()
            return fingerprint

        assert await listener_pin() == initial_pin
        unpinned_key = ec.generate_private_key(ec.SECP256R1())
        unpinned = issue(ca_key, ca_cert, unpinned_key.public_key(), identity="spiffe://lumen/operator/unlisted")
        unpinned_cert, unpinned_key_file = tmp_path / "unlisted.pem", tmp_path / "unlisted-key.pem"
        unpinned_cert.write_bytes(unpinned.public_bytes(serialization.Encoding.PEM))
        unpinned_key_file.write_bytes(pem_key(unpinned_key))
        unpinned_context = ssl.create_default_context(cafile=str(ca_path))
        unpinned_context.check_hostname = False
        unpinned_context.load_cert_chain(str(unpinned_cert), str(unpinned_key_file))
        async with httpx.AsyncClient(verify=unpinned_context, trust_env=False) as client:
            assert (await client.get(f"https://127.0.0.1:{port}/v1/ready")).status_code == 403
        cutoff = runtime.cutoff
        with pytest.raises(httpx.HTTPError):
            await runtime.rotate()  # lost renew response
        original_state = json.loads(runtime.renewal.state_file.read_text())
        with pytest.raises(httpx.HTTPError):
            await runtime.rotate()  # same renew request; activate committed but response lost
        assert renew_requests[0] == renew_requests[1]
        assert json.loads(runtime.renewal.state_file.read_text()) == original_state
        # Reconstruct the renewal agent as a root-supervisor crash would do.
        runtime.renewal = guest_api.RenewalAgent(directory, identity, os.environ["LUMEN_CONTROLLER_URL"])
        await runtime.rotate()
        assert runtime.cutoff > cutoff
        first_rotated = await listener_pin()
        assert first_rotated == active["pin"] and first_rotated != initial_pin
        assert child.pid == pid and child.poll() is None

        async with httpx.AsyncClient(verify=client_context, trust_env=False) as client:
            url = f"https://127.0.0.1:{port}"
            ready = await client.get(url + "/v1/ready")
            assert ready.status_code == 200
            assert ready.json()["ready"] is False  # dependencies down, counters still verified
            assert ready.json()["load"]["active_ws"] == 0
            drained = await client.post(url + "/v1/drain", json={**runtime.drain.identity, "fence": 17})
            assert drained.status_code == 200 and drained.json()["drain_acknowledged"] is True
            stale = await client.post(url + "/v1/drain", json={**runtime.drain.identity, "fence": 16})
            assert stale.status_code == 409
            await runtime.rotate()
            assert (await client.get(url + "/v1/ready")).json()["drain_fence"] == 17
        assert await listener_pin() == active["pin"] != first_rotated
        assert child.pid == pid and child.poll() is None
        restarted = guest_api.GuestRuntime(directory, data, drain_path=drain_path, service_port=runtime.service_port)
        assert restarted.drain.fence == 17 and not restarted.drain.acknowledged
        # Per-boot token changes; persisted drain is injected before admission on child restart.
        assert restarted.token != runtime.token
        restarted_child = subprocess.Popen([sys.executable, "-u", "-c", _CHILD], stdout=subprocess.PIPE,
            text=True, env={**os.environ, "LUMEN_INTERNAL_DRAIN_TOKEN": restarted.token,
                            "LUMEN_INTERNAL_DRAIN_FENCE": str(restarted.drain.fence)})
        restarted_info = json.loads(await asyncio.wait_for(asyncio.to_thread(restarted_child.stdout.readline), 5))
        restarted.service_port = restarted_info["port"]
        async with httpx.AsyncClient(trust_env=False) as client:
            local = await client.get(f"http://127.0.0.1:{restarted.service_port}/v1/ready?include_load=1")
            assert local.json()["drain_fence"] == 17 and local.json()["draining"] is True
            old_token = await client.post(f"http://127.0.0.1:{restarted.service_port}/v1/internal/drain",
                headers={"X-Lumen-Drain-Token": runtime.token}, json={"fence": 17})
            assert old_token.status_code == 403
        await restarted.drain.forward(restarted.service_port, restarted.token)
        assert restarted.drain.acknowledged
        assert runtime.cutoff > cutoff
        assert len(pending) == 2 and not runtime.renewal.state_file.exists()
        runtime.cutoff = datetime.now(UTC) - timedelta(seconds=1)
        async with httpx.AsyncClient(verify=client_context, trust_env=False) as client:
            assert (await client.get(f"https://127.0.0.1:{port}/v1/ready")).status_code == 503
    finally:
        if listener:
            listener.close()
            await listener.wait_closed()
        controller_server.close()
        await controller_server.wait_closed()
        for process in (child, restarted_child):
            if process is None:
                continue
            process.terminate()
            try:
                await asyncio.wait_for(asyncio.to_thread(process.wait), 5)
            except TimeoutError:
                process.kill()
                await asyncio.to_thread(process.wait)
            if process.stdout:
                process.stdout.close()
