"""Anthropic Messages through Lumen's native Messages compatibility route.

Environment:
  LUMEN_BASE_URL   default http://127.0.0.1:8012
  LUMEN_API_KEY    Lumen API key (scope: models:read, compat:completions:write)
  LUMEN_MODEL      default claude-haiku-4-5 (must appear in GET /v1/models)

The key is read from the environment only and is never printed.
"""

import os
import sys

import httpx

BASE = os.environ.get("LUMEN_BASE_URL", "http://127.0.0.1:8012").rstrip("/")
KEY = os.environ.get("LUMEN_API_KEY", "")
MODEL = os.environ.get("LUMEN_MODEL", "claude-haiku-4-5")


def main() -> int:
    if not KEY:
        print("LUMEN_API_KEY is not set", file=sys.stderr)
        return 2
    headers = {"x-api-key": KEY, "anthropic-version": "2023-06-01"}
    with httpx.Client(base_url=BASE, timeout=120, headers=headers) as client:
        response = client.post(
            "/v1/messages",
            json={
                "model": MODEL,
                "max_tokens": 20,
                "messages": [{"role": "user", "content": "Reply with exactly: OK-ANTHROPIC"}],
            },
        )
        response.raise_for_status()
        body = response.json()
        texts = [block.get("text", "") for block in body.get("content", []) if block.get("type") == "text"]
        print("content:", texts)
        print("usage:", body.get("usage"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
