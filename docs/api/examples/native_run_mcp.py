"""Native temporary completion with MCP tools, including the approval step.

This is the only path where remote MCP tools run: the execution protocol must be v2
(the server must be started with CHAT_EXECUTION_PROTOCOL_VERSION=2 and an encrypted
PostgreSQL checkpointer, CHAT_CHECKPOINTER_POSTGRES_URL). MCP tools default to the
external_mutation effect, so every call pauses for approval.

Environment:
  LUMEN_BASE_URL     default http://127.0.0.1:8012
  LUMEN_TOKEN        Keystone project token (X-Auth-Token); API keys cannot run MCP tools here
  LUMEN_PROJECT_ID   project id (X-Project-Id)
  LUMEN_MCP_IDS      comma-separated active connection ids, e.g. "2,3"
  LUMEN_MODEL        default gpt-4.1-mini (a model id from GET /v1/chat/models)
  LUMEN_AUTO_APPROVE "1" approves each pending call. Default "0": the script prints the
                     pending call and stops, so a human decides.

Exit codes: 0 completed, 3 waiting for a human decision, 1 failure.
The token is read from the environment only and never printed.
"""

import json
import os
import sys
import time
import uuid

import httpx

BASE = os.environ.get("LUMEN_BASE_URL", "http://127.0.0.1:8012").rstrip("/")
TOKEN = os.environ.get("LUMEN_TOKEN", "")
PROJECT = os.environ.get("LUMEN_PROJECT_ID", "")
MCP_IDS = [int(x) for x in os.environ.get("LUMEN_MCP_IDS", "").split(",") if x.strip()]
MODEL = os.environ.get("LUMEN_MODEL", "gpt-4.1-mini")
AUTO = os.environ.get("LUMEN_AUTO_APPROVE", "0") == "1"

PROMPT = (
    "Call list_available_tools with query 'certscore' and max_results 8, then load the scan status tool. "
    "Call it once with scanId 'verify-scan-0001'. Reply with one line naming the tool you called."
)


def read_events(client: httpx.Client, run_id: str, seconds: float) -> list[dict]:
    """Read SSE events for a bounded time; the stream stays open until the run is terminal."""
    events: list[dict] = []
    try:
        with client.stream("GET", f"/v1/runs/{run_id}/events", timeout=httpx.Timeout(10, read=seconds)) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if line.startswith("data:"):
                    events.append(json.loads(line[5:]))
    except httpx.ReadTimeout:
        # No further event within the window: the run is still open, so return what was read.
        pass
    return events


def main() -> int:
    if not (TOKEN and PROJECT and MCP_IDS):
        print("LUMEN_TOKEN, LUMEN_PROJECT_ID and LUMEN_MCP_IDS are required", file=sys.stderr)
        return 2
    headers = {"X-Auth-Token": TOKEN, "X-Project-Id": PROJECT}
    with httpx.Client(base_url=BASE, timeout=120, headers=headers) as client:
        created = client.post(
            "/v1/temp-completions",
            headers={"Idempotency-Key": str(uuid.uuid4())},
            json={
                "model_id": MODEL,
                "parts": [{"type": "text", "text": PROMPT}],
                "features": {"memory": False, "tool_policy": {"mode": "agent_default", "enabled_mcp_ids": MCP_IDS}},
            },
        )
        created.raise_for_status()
        run_id = created.json()["run_id"]
        print("run:", run_id)

        answered: set[str] = set()
        status = None
        for _ in range(300):
            status = client.get(f"/v1/runs/{run_id}").json().get("status")
            if status in {"completed", "failed", "cancelled"}:
                break
            if status == "awaiting_input":
                for event in read_events(client, run_id, seconds=3):
                    if event["type"] != "tool.approval_required" or event["payload"]["call_id"] in answered:
                        continue
                    payload = event["payload"]
                    call_id = payload["call_id"]
                    print("approval required:", payload["name"], "effect:", payload["effect"])
                    if not AUTO:
                        print("pending call id:", call_id, "(set LUMEN_AUTO_APPROVE=1 to approve)")
                        return 3
                    decision = client.post(
                        f"/v1/runs/{run_id}/approvals/{call_id}", json={"decision": "approve"}
                    )
                    decision.raise_for_status()
                    answered.add(call_id)
            time.sleep(2)
        print("status:", status)

        tool_failed = False
        for event in read_events(client, run_id, seconds=3):
            if event["type"] in {"tool.call.started", "tool.call.completed"}:
                payload = event["payload"]
                print(event["type"], payload.get("name"), payload.get("status", ""), payload.get("error_code", ""))
                tool_failed |= event["type"] == "tool.call.completed" and payload.get("status") != "completed"
        return 0 if status == "completed" and not tool_failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
