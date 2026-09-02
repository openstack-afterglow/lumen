#!/usr/bin/env python3
"""Render a libpq service stanza from an external PostgreSQL URL.

The URL is read only from the environment so it never enters a process argv.
The result is intended for a mode-0600 temporary file consumed by `psql`.
"""

from __future__ import annotations

import json
import os
import sys
from urllib.parse import parse_qsl, unquote, urlsplit


ALLOWED_QUERY_PARAMETERS = frozenset(
    {
        "application_name",
        "channel_binding",
        "client_encoding",
        "connect_timeout",
        "gssencmode",
        "keepalives",
        "keepalives_count",
        "keepalives_idle",
        "keepalives_interval",
        "krbsrvname",
        "load_balance_hosts",
        "options",
        "require_auth",
        "requirepeer",
        "sslcert",
        "sslcrl",
        "sslcrldir",
        "sslkey",
        "sslmode",
        "sslnegotiation",
        "sslrootcert",
        "sslsni",
        "target_session_attrs",
        "tcp_user_timeout",
    }
)


def fail(message: str) -> None:
    raise ValueError(message)


def reject_controls(value: str, name: str) -> str:
    if (
        not value
        or value != value.strip()
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        fail(f"external PostgreSQL URL has an invalid {name}")
    return value


def main() -> int:
    url = os.environ.get("LUMEN_EXTERNAL_POSTGRES_URL", "")
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in {"postgres", "postgresql"}:
            fail("external PostgreSQL URL must use postgres:// or postgresql://")
        if parsed.fragment:
            fail("external PostgreSQL URL must not include a fragment")
        if not parsed.hostname or parsed.username is None or parsed.password is None:
            fail("external PostgreSQL URL must include host, username, and password")
        if not parsed.path or parsed.path == "/":
            fail("external PostgreSQL URL must include a database name")

        port = parsed.port or 5432
        host = reject_controls(unquote(parsed.hostname), "host")
        user = reject_controls(unquote(parsed.username), "username")
        password = reject_controls(unquote(parsed.password), "password")
        database = reject_controls(unquote(parsed.path[1:]), "database name")

        parameters = {
            "host": host,
            "port": str(port),
            "user": user,
            "password": password,
            "dbname": database,
        }
        for key, value in parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True):
            if key not in ALLOWED_QUERY_PARAMETERS or key in parameters:
                fail(f"external PostgreSQL URL has an unsupported query parameter: {key}")
            parameters[key] = reject_controls(value, f"query parameter {key}")

        service = "[external]\n" + "\n".join(
            f"{key}={value}" for key, value in parameters.items()
        ) + "\n"
        print(json.dumps({"service": service}))
        return 0
    except (TypeError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
