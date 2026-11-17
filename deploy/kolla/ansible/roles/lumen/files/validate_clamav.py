#!/usr/bin/env python3
"""Bounded clamd readiness: PING plus a loaded, recent signature database.

No file or signature is created by this probe. A TCP listener alone is not ready.
"""
from __future__ import annotations

import argparse
import json
import re
import socket
from datetime import datetime, timedelta, timezone

# Host-side Kolla probes also run on Python 3.10, before the app image exists.
UTC = timezone.utc  # noqa: UP017 -- datetime.UTC requires Python 3.11


class ScannerNotReady(RuntimeError):
    """Safe diagnostic code, never raw remote output."""


def command(host: str, port: int, verb: bytes) -> str:
    with socket.create_connection((host, port), timeout=5) as connection:
        connection.settimeout(5)
        connection.sendall(b"z" + verb + b"\0")
        response = bytearray()
        while b"\0" not in response:
            part = connection.recv(256 - len(response))
            if not part or len(response) + len(part) >= 256:
                raise ScannerNotReady("scanner_response_invalid")
            response.extend(part)
        if response.count(b"\0") != 1 or not response.endswith(b"\0"):
            raise ScannerNotReady("scanner_response_invalid")
        try:
            return response[:-1].decode("ascii")
        except UnicodeDecodeError as exc:
            raise ScannerNotReady("scanner_response_invalid") from exc


def validate_version(version: str, *, max_age_hours: int, now: datetime | None = None) -> None:
    # VERSION has engine/signature-version/database-build-date. Engine-only
    # output (no signatures), malformed dates and stale/future databases fail.
    match = re.fullmatch(r"ClamAV [0-9]+\.[0-9]+\.[0-9]+(?:[-A-Za-z0-9.]*)/([1-9][0-9]*)/(.+)", version)
    if not match:
        raise ScannerNotReady("scanner_signatures_missing")
    try:
        built = datetime.strptime(match[2], "%a %b %d %H:%M:%S %Y").replace(tzinfo=UTC)
    except ValueError as exc:
        raise ScannerNotReady("scanner_signature_date_invalid") from exc
    now = now or datetime.now(UTC)
    if built > now + timedelta(minutes=5) or now - built > timedelta(hours=max_age_hours):
        raise ScannerNotReady("scanner_signatures_stale")


def check(host: str, port: int, *, max_age_hours: int) -> None:
    if command(host, port, b"PING") != "PONG":
        raise ScannerNotReady("scanner_ping_failed")
    validate_version(command(host, port, b"VERSION"), max_age_hours=max_age_hours)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--max-age-hours", type=int, default=72)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535 or not 1 <= args.max_age_hours <= 168:
        parser.error("invalid scanner port or signature age bound")
    try:
        check(args.host, args.port, max_age_hours=args.max_age_hours)
    except ScannerNotReady as exc:
        print(json.dumps({"ready": False, "reason": str(exc)}))
        return 1
    except OSError:
        print(json.dumps({"ready": False, "reason": "scanner_unreachable"}))
        return 1
    print(json.dumps({"ready": True, "checks": ["PING", "VERSION", "recent_signatures"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
