"""Print local Lumen OpenAI-compatible connection manifest."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from stat import S_IMODE
from typing import Never

from lumen.scripts.seed_local import normalize_base_url

_FIELDS = (
    "schema_version",
    "base_url",
    "container_base_url",
    "api_key",
    "model",
    "provider_api_key_configured",
)


def _fail(message: str) -> Never:
    sys.stderr.write(f"{message}\n")
    raise SystemExit(1)


def show_connection() -> None:
    connection_path = Path(os.environ.get("LUMEN_LOCAL_CONNECTION_PATH", "/seed/connection.json"))
    if not connection_path.is_file():
        _fail(f"Connection manifest not found: {connection_path}")

    try:
        data = json.loads(connection_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        _fail("Failed to read or parse connection manifest: corrupt or unreadable")

    if not isinstance(data, dict):
        _fail("Invalid connection manifest: expected JSON object")
    if set(data) != set(_FIELDS):
        _fail("Invalid connection manifest: unexpected fields")

    schema_version = data["schema_version"]
    base_url = data["base_url"]
    container_base_url = data["container_base_url"]
    api_key = data["api_key"]
    model = data["model"]
    provider_api_key_configured = data["provider_api_key_configured"]

    if type(schema_version) is not int or schema_version != 1:
        _fail("Invalid connection manifest: invalid schema_version")
    if not isinstance(base_url, str) or not base_url.strip():
        _fail("Invalid connection manifest: invalid base_url")
    if not isinstance(container_base_url, str) or not container_base_url.strip():
        _fail("Invalid connection manifest: invalid container_base_url")
    if not isinstance(api_key, str) or not api_key.startswith("sk-afgl-"):
        _fail("Invalid connection manifest: invalid api_key")
    if not isinstance(model, str) or not model.strip():
        _fail("Invalid connection manifest: invalid model")
    if type(provider_api_key_configured) is not bool:
        _fail("Invalid connection manifest: invalid provider_api_key_configured")

    try:
        if normalize_base_url(base_url) != base_url or normalize_base_url(container_base_url) != container_base_url:
            raise ValueError
    except ValueError:
        _fail("Invalid connection manifest: malformed base URL")
    if S_IMODE(connection_path.stat().st_mode) != 0o600:
        _fail("Invalid connection manifest: insecure file permissions")

    manifest = {field: data[field] for field in _FIELDS}
    print(json.dumps(manifest, indent=2))


def main() -> None:
    show_connection()


if __name__ == "__main__":
    main()
