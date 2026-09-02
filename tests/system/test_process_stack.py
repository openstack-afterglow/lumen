"""System tests for the complete process stack over real HTTP."""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.system


def test_process_stack_temp_completion_e2e() -> None:
    api_base_url = os.environ.get("LUMEN_API_BASE_URL", "http://localhost:8012").rstrip("/")
    connection_file = os.environ.get("LUMEN_CONNECTION_FILE", "/seed/connection.json")
    seed_key_file = os.environ.get("LUMEN_SEED_KEY_FILE", "/seed/api-key")

    conn_path = Path(connection_file)
    assert conn_path.exists(), f"Connection manifest file does not exist at {connection_file}"

    manifest_text = conn_path.read_text().strip()
    assert manifest_text, "Connection manifest file is empty"
    manifest = json.loads(manifest_text)

    assert manifest.get("schema_version") == 1, "Expected manifest schema_version == 1"
    base_url = manifest.get("base_url")
    container_base_url = manifest.get("container_base_url")
    assert isinstance(base_url, str) and base_url.endswith("/v1"), "base_url must end with /v1"
    assert isinstance(container_base_url, str) and container_base_url.endswith("/v1"), (
        "container_base_url must end with /v1"
    )

    api_key = manifest.get("api_key")
    assert isinstance(api_key, str) and api_key.startswith("sk-afgl-"), "api_key in manifest is invalid"

    key_path = Path(seed_key_file)
    if key_path.exists():
        raw_seed_key = key_path.read_text().strip()
        assert raw_seed_key == api_key, "api_key in manifest does not match seed key file"

    model_name = manifest.get("model") or os.environ.get("LUMEN_MODEL_NAME", "fake-gpt-4")
    assert isinstance(model_name, str) and model_name.strip(), "model must be a non-empty string"
    assert manifest.get("provider_api_key_configured") is True, "system provider credential must be configured"

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    with httpx.Client(base_url=api_base_url, timeout=30.0) as client:
        # 1. Call GET /v1/models
        models_resp = client.get(f"{container_base_url}/models", headers=headers)
        assert models_resp.status_code == 200, f"GET /v1/models failed: {models_resp.status_code}"
        models_data = models_resp.json()
        assert models_data.get("object") == "list", "GET /v1/models response must be an OpenAI model list"
        model_ids = {item.get("id") for item in models_data.get("data", [])}
        assert model_name in model_ids, "Generated connection model is absent from GET /v1/models"

        # 2. Call non-streaming POST /v1/chat/completions
        chat_payload = {
            "model": model_name,
            "messages": [{"role": "user", "content": "Hello process stack"}],
            "stream": False,
        }
        chat_resp = client.post(f"{container_base_url}/chat/completions", headers=headers, json=chat_payload)
        assert chat_resp.status_code == 200, f"POST /v1/chat/completions failed: {chat_resp.status_code}"
        chat_data = chat_resp.json()

        choices = chat_data.get("choices", [])
        assert len(choices) == 1, "Expected one chat completion choice"
        message_content = choices[0].get("message", {}).get("content")
        assert message_content == "Hello from fake provider!", "Unexpected provider message content"

        usage = chat_data.get("usage", {})
        assert usage.get("prompt_tokens") == 10, f"Expected prompt_tokens=10, got {usage.get('prompt_tokens')}"
        assert usage.get("completion_tokens") == 5, (
            f"Expected completion_tokens=5, got {usage.get('completion_tokens')}"
        )
        assert usage.get("total_tokens") == 15, f"Expected total_tokens=15, got {usage.get('total_tokens')}"

        # 3. Call streaming POST /v1/chat/completions with include_usage
        stream_payload = {
            "model": model_name,
            "messages": [{"role": "user", "content": "Hello streaming process stack"}],
            "stream": True,
            "stream_options": {"include_usage": True},
        }

        received_deltas: list[str] = []
        stream_usage = None
        seen_done = False

        with client.stream(
            "POST", f"{container_base_url}/chat/completions", headers=headers, json=stream_payload
        ) as stream_resp:
            assert stream_resp.status_code == 200, (
                f"Streaming POST /v1/chat/completions failed: {stream_resp.status_code}"
            )
            for line in stream_resp.iter_lines():
                line = line.strip()
                if not line or not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if data_str == "[DONE]":
                    seen_done = True
                    break
                chunk = json.loads(data_str)

                for choice in chunk.get("choices", []):
                    delta = choice.get("delta", {})
                    if "content" in delta and delta["content"]:
                        received_deltas.append(delta["content"])

                if "usage" in chunk and chunk["usage"]:
                    stream_usage = chunk["usage"]

        assert seen_done, "Streaming response did not terminate with [DONE]"
        full_stream_text = "".join(received_deltas)
        assert full_stream_text == "Hello from fake provider!", "Unexpected streamed provider content"
        assert stream_usage is not None, "Streaming response missing final usage chunk"
        assert stream_usage.get("prompt_tokens") == 10, (
            f"Expected prompt_tokens=10 in stream, got {stream_usage.get('prompt_tokens')}"
        )
        assert stream_usage.get("completion_tokens") == 5, (
            f"Expected completion_tokens=5 in stream, got {stream_usage.get('completion_tokens')}"
        )
        assert stream_usage.get("total_tokens") == 15, (
            f"Expected total_tokens=15 in stream, got {stream_usage.get('total_tokens')}"
        )

        # 4. Submit native temp completion
        temp_headers = {
            "Authorization": f"Bearer {api_key}",
            "Idempotency-Key": str(uuid.uuid4()),
            "Content-Type": "application/json",
        }
        temp_payload = {
            "parts": [{"type": "text", "text": "Hello process stack"}],
            "model_id": model_name,
            "features": {},
        }

        resp = client.post("/v1/temp-completions", headers=temp_headers, json=temp_payload)
        assert resp.status_code == 202, f"Expected 202 Accepted, got {resp.status_code}: {resp.text}"

        run_data = resp.json()
        run_id = run_data.get("run_id") or run_data.get("id")
        assert run_id, f"Response missing run_id: {run_data}"

        # Poll public run API until terminal
        max_attempts = 60
        status = None
        run_detail = None
        for _ in range(max_attempts):
            poll_resp = client.get(f"/v1/runs/{run_id}", headers={"Authorization": f"Bearer {api_key}"})
            assert poll_resp.status_code == 200, f"Failed polling run {run_id}: {poll_resp.text}"
            run_detail = poll_resp.json()
            status = run_detail.get("status")
            if status in ("completed", "failed", "canceled"):
                break
            time.sleep(0.5)

        assert status == "completed", f"Run did not complete successfully. Status: {status}, Detail: {run_detail}"

        # 5. Assert usage attribution via public usage API
        usage_resp = client.get("/v1/usage/records", headers={"Authorization": f"Bearer {api_key}"})
        assert usage_resp.status_code == 200, f"Failed getting usage records: {usage_resp.text}"
        usage_data = usage_resp.json()

        records = usage_data.get("records", usage_data) if isinstance(usage_data, dict) else usage_data
        assert isinstance(records, list), f"Expected list of usage records, got: {type(records)}"

        # Assert native run usage record
        native_record = next((item for item in records if item.get("run_id") == run_id), None)
        assert native_record is not None, f"Expected usage record for native run {run_id}"
        assert native_record["source"] == "api"
        api_key_id = native_record["api_key_id"]
        assert isinstance(api_key_id, int)

        # Assert internal billing/attribution for the two compat calls
        compat_records = [
            item
            for item in records
            if item.get("source") == "api"
            and item.get("api_key_id") == api_key_id
            and item.get("run_id") is None
            and item.get("total_tokens") == 15
        ]
        assert len(compat_records) >= 2, (
            f"Expected at least 2 compat usage records with run_id is None and total_tokens=15, found {len(compat_records)}"
        )
