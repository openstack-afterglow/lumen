"""Install one MCP registry entry as a global source and refresh its real tool catalog.

Administrator call. The tool list is fetched from the remote server with tools/list.
No tool is invoked.

Environment:
  LUMEN_BASE_URL          default http://127.0.0.1:8012
  LUMEN_ADMIN_TOKEN       Keystone token with system/admin authority (X-Auth-Token)
  LUMEN_PROJECT_ID        optional X-Project-Id for a project-scoped admin token
  LUMEN_TARGET_PROJECT_ID optional X-Target-Project-Id for a projectless system admin
  LUMEN_MCP_ENTRY_ID      registry entry id; default is CertScore.ai MCP Light (no auth)

The admin token is read from the environment only and is never printed.
"""

import os
import sys

import httpx

BASE = os.environ.get("LUMEN_BASE_URL", "http://127.0.0.1:8012").rstrip("/")
TOKEN = os.environ.get("LUMEN_ADMIN_TOKEN", "")
ENTRY = os.environ.get("LUMEN_MCP_ENTRY_ID", "afb289a7fa0547cdaa827b88e6325938")


def admin_headers() -> dict[str, str]:
    headers = {"X-Auth-Token": TOKEN}
    if os.environ.get("LUMEN_PROJECT_ID"):
        headers["X-Project-Id"] = os.environ["LUMEN_PROJECT_ID"]
    if os.environ.get("LUMEN_TARGET_PROJECT_ID"):
        headers["X-Target-Project-Id"] = os.environ["LUMEN_TARGET_PROJECT_ID"]
    return headers


def main() -> int:
    if not TOKEN:
        print("LUMEN_ADMIN_TOKEN is not set", file=sys.stderr)
        return 2
    with httpx.Client(base_url=BASE, timeout=120, headers=admin_headers()) as client:
        installed = client.post(f"/v1/admin/mcp-registry/{ENTRY}/install", json={})
        installed.raise_for_status()
        server_id = installed.json()["server"]["id"]
        print("installed server id:", server_id, "created:", installed.json().get("created"))

        refreshed = client.post(f"/v1/admin/mcp-connections/{server_id}/catalog/refresh")
        refreshed.raise_for_status()
        result = refreshed.json()
        print("status:", result.get("status"), "tool_count:", result.get("tool_count"))
        print("diff:", result.get("diff"), "error:", result.get("error"))
        return 0 if result.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
