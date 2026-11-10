"""OpenAI Chat Completions through Lumen's compatibility route.

Environment:
  LUMEN_BASE_URL   default http://127.0.0.1:8012
  LUMEN_API_KEY    Lumen API key (scope: models:read, compat:completions:write)
  LUMEN_MODEL      default gpt-4.1-mini (must appear in GET /v1/models)

The key is read from the environment only and is never printed.
"""

import os
import sys

import httpx

BASE = os.environ.get("LUMEN_BASE_URL", "http://127.0.0.1:8012").rstrip("/")
KEY = os.environ.get("LUMEN_API_KEY", "")
MODEL = os.environ.get("LUMEN_MODEL", "gpt-4.1-mini")


def main() -> int:
    if not KEY:
        print("LUMEN_API_KEY is not set", file=sys.stderr)
        return 2
    headers = {"Authorization": f"Bearer {KEY}"}
    with httpx.Client(base_url=BASE, timeout=120, headers=headers) as client:
        models = client.get("/v1/models")
        models.raise_for_status()
        ids = [m["id"] for m in models.json().get("data", [])]
        if MODEL not in ids:
            print(f"model {MODEL!r} not available; available: {ids}", file=sys.stderr)
            return 1

        response = client.post(
            "/v1/chat/completions",
            json={
                "model": MODEL,
                "messages": [{"role": "user", "content": "Reply with exactly: OK-OPENAI"}],
                "max_tokens": 20,
            },
        )
        response.raise_for_status()
        body = response.json()
        print("content:", body["choices"][0]["message"]["content"])
        print("usage:", body.get("usage"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
