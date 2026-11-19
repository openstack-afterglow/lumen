"""Clamd protocol readiness is not implied by a listening TCP socket."""
from __future__ import annotations

import importlib.util
import socketserver
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

ROLE = Path(__file__).resolve().parents[1] / "deploy/kolla/ansible/roles/lumen"
SPEC = importlib.util.spec_from_file_location("clamav_readiness", ROLE / "files/validate_clamav.py")
assert SPEC and SPEC.loader
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)
NOW = datetime(2026, 11, 18, 12, tzinfo=UTC)
VALID = "ClamAV 1.4.3/28289/Wed Nov 18 07:24:39 2026"


def test_version_requires_loaded_recent_signature_database():
    probe.validate_version(VALID, max_age_hours=72, now=NOW)


@pytest.mark.parametrize("version, reason", [
    ("ClamAV 1.4.3", "scanner_signatures_missing"),
    ("ClamAV 1.4.3/0/Wed Nov 18 07:24:39 2026", "scanner_signatures_missing"),
    ("ClamAV 1.4.3/28289/invalid", "scanner_signature_date_invalid"),
    ("ClamAV 1.4.3/27927/Sun Mar  1 07:24:39 2026", "scanner_signatures_stale"),
    ("ClamAV 1.4.3/28289/Thu Nov 19 07:24:39 2026", "scanner_signatures_stale"),
    ("PONG", "scanner_signatures_missing"),
])
def test_stale_absent_or_invalid_database_is_not_ready(version, reason):
    with pytest.raises(probe.ScannerNotReady, match=reason):
        probe.validate_version(version, max_age_hours=72, now=NOW)


@pytest.fixture
def scanner():
    state = {"ping": b"PONG\0", "version": VALID.encode() + b"\0", "commands": []}

    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.settimeout(2)
            command = self.request.recv(256)
            state["commands"].append(command)
            response = state["ping"] if command == b"zPING\0" else state["version"]
            # Exercise a fragmented null-terminated protocol response.
            for index in range(0, len(response), 3):
                self.request.sendall(response[index:index + 3])

    with socketserver.TCPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield state, server.server_address[1]
        finally:
            server.shutdown()
            thread.join(timeout=2)


def test_real_loopback_protocol_requires_ping_and_version(scanner, monkeypatch):
    state, port = scanner
    validate = probe.validate_version
    monkeypatch.setattr(probe, "validate_version", lambda value, **kwargs: validate(value, **kwargs, now=NOW))
    probe.check("127.0.0.1", port, max_age_hours=72)
    assert state["commands"] == [b"zPING\0", b"zVERSION\0"]


@pytest.mark.parametrize("response, reason", [
    (b"NO\0", "scanner_ping_failed"),
    (b"PONG", "scanner_response_invalid"),
    (b"PONG\0extra", "scanner_response_invalid"),
])
def test_ping_mismatch_or_truncated_response_fails_closed(scanner, response, reason):
    state, port = scanner
    state["ping"] = response
    with pytest.raises(probe.ScannerNotReady, match=reason):
        probe.check("127.0.0.1", port, max_age_hours=72)


def test_accepting_connection_with_stale_database_is_not_ready(scanner):
    state, port = scanner
    state["version"] = b"ClamAV 1.4.3/27927/Sun Mar  1 07:24:39 2026\0"
    with pytest.raises(probe.ScannerNotReady, match="scanner_signatures_stale"):
        probe.check("127.0.0.1", port, max_age_hours=72)
