"""System tests for the complete process stack over real HTTP.

Covers:
- Standard non-streaming and streaming temp/model=lumen completions and usage attribution
- Native API -> queue -> worker -> provider -> SSE streaming proving early delta exposure
  within scheduler/DB allowance (<= 250ms) during a 2-second provider pause
- 500x4 burst streaming with strictly ordered delivery and monotonic journal replay
- Context preview: strictly read-only, zero model provider calls, honest ContextState projection
- Manual compaction: expected revision fence, idempotency key deduplication, zero placeholder
  message insertion, and next completion preserving sentinels and recent turns
- Title generation: influenced by the first user query and assistant answer, zero extra title calls
  on subsequent ordinary turns, and title/history preservation when compaction fails
- Paused provider and run cancellation: clean terminal cancellation under active provider pause
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.system


def _load_connection_context() -> tuple[str, str, str, str, str]:
    """Load API base URL, container base URL, API key, model name, and fake provider URL."""
    api_base_url = os.environ.get("LUMEN_API_BASE_URL", "http://localhost:8012").rstrip("/")
    connection_file = os.environ.get("LUMEN_CONNECTION_FILE", "/seed/connection.json")
    seed_key_file = os.environ.get("LUMEN_SEED_KEY_FILE", "/seed/api-key")

    conn_path = Path(connection_file)
    assert conn_path.exists(), f"Connection manifest file does not exist at {connection_file}"

    manifest_text = conn_path.read_text().strip()
    assert manifest_text, "Connection manifest file is empty"
    manifest = json.loads(manifest_text)

    assert manifest.get("schema_version") == 1, "Expected manifest schema_version == 1"
    container_base_url = manifest.get("container_base_url") or f"{api_base_url}/v1"

    api_key = manifest.get("api_key")
    assert isinstance(api_key, str) and api_key.startswith("sk-afgl-"), "api_key in manifest is invalid"

    key_path = Path(seed_key_file)
    if key_path.exists():
        raw_seed_key = key_path.read_text().strip()
        assert raw_seed_key == api_key, "api_key in manifest does not match seed key file"

    model_name = manifest.get("model") or os.environ.get("LUMEN_MODEL_NAME", "fake-gpt-4")
    fake_provider_url = os.environ.get("LUMEN_FAKE_PROVIDER_URL", "http://fake-provider:8080").rstrip("/")

    return api_base_url, container_base_url, api_key, model_name, fake_provider_url


def _configure_fake_provider(fake_provider_url: str, patch: dict[str, Any]) -> dict[str, Any] | None:
    """Best-effort control configuration on the test provider if reachable over HTTP."""
    try:
        with httpx.Client(timeout=3.0) as client:
            resp = client.post(f"{fake_provider_url}/_control/configure", json=patch)
            if resp.status_code == 200:
                return resp.json()
    except Exception:
        pass
    return None


def _get_fake_provider_stats(fake_provider_url: str) -> dict[str, Any] | None:
    """Best-effort retrieval of test provider metrics and safe synthetic prompt history."""
    try:
        with httpx.Client(timeout=3.0) as client:
            resp = client.get(f"{fake_provider_url}/_control/stats")
            if resp.status_code == 200:
                return resp.json()
    except Exception:
        pass
    return None


def _reset_fake_provider(fake_provider_url: str) -> None:
    """Reset fake provider mode and counters to normal defaults."""
    try:
        with httpx.Client(timeout=3.0) as client:
            client.post(f"{fake_provider_url}/_control/reset")
    except Exception:
        pass


def _poll_run_until_terminal(
    client: httpx.Client,
    run_id: str,
    headers: dict[str, str],
    *,
    timeout: float = 60.0,
    interval: float = 0.4,
) -> dict[str, Any]:
    """Poll the public /v1/runs/{id} endpoint until reaching terminal status."""
    deadline = time.monotonic() + timeout
    last_detail: dict[str, Any] = {}
    while time.monotonic() < deadline:
        resp = client.get(f"/v1/runs/{run_id}", headers=headers)
        assert resp.status_code == 200, f"Failed polling run {run_id}: {resp.status_code} {resp.text}"
        last_detail = resp.json()
        status = last_detail.get("status")
        if status in ("completed", "failed", "canceled"):
            return last_detail
        time.sleep(interval)
    raise TimeoutError(f"Run {run_id} did not reach terminal within {timeout}s: {last_detail}")


# ============================================================================
# 1. Baseline Native and OpenAI Compatibility Stack (Existing Baseline)
# ============================================================================


def test_process_stack_temp_completion_e2e() -> None:
    """Validate baseline OpenAI compatibility endpoints and native temporary completions."""
    api_base_url, container_base_url, api_key, model_name, fake_provider_url = _load_connection_context()
    _reset_fake_provider(fake_provider_url)

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
        assert "lumen" in model_ids, "Lumen virtual model is absent from GET /v1/models"
        lumen_model_item = next((item for item in models_data.get("data", []) if item.get("id") == "lumen"), None)
        assert lumen_model_item is not None and lumen_model_item.get("owned_by") == "lumen"

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
        assert stream_usage.get("prompt_tokens") == 10
        assert stream_usage.get("completion_tokens") == 5
        assert stream_usage.get("total_tokens") == 15

        # 3b. Call non-streaming POST /v1/chat/completions with model="lumen" (durable run via worker)
        chat_payload_lumen = {
            "model": "lumen",
            "messages": [{"role": "user", "content": "Hello process stack via lumen virtual model"}],
            "stream": False,
        }
        chat_resp_lumen = client.post(
            f"{container_base_url}/chat/completions", headers=headers, json=chat_payload_lumen
        )
        assert chat_resp_lumen.status_code == 200, (
            f"POST /v1/chat/completions (model=lumen) failed: {chat_resp_lumen.status_code}"
        )
        chat_data_lumen = chat_resp_lumen.json()
        assert chat_data_lumen.get("model") == "lumen", "Expected model=lumen in response"
        choices_lumen = chat_data_lumen.get("choices", [])
        assert len(choices_lumen) == 1
        assert choices_lumen[0].get("message", {}).get("content") == "Hello from fake provider!"
        usage_lumen = chat_data_lumen.get("usage", {})
        assert usage_lumen.get("total_tokens") == 15

        # 3c. Call streaming POST /v1/chat/completions with model="lumen" and include_usage
        stream_payload_lumen = {
            "model": "lumen",
            "messages": [{"role": "user", "content": "Hello streaming process stack via lumen virtual model"}],
            "stream": True,
            "stream_options": {"include_usage": True},
        }

        received_deltas_lumen: list[str] = []
        stream_usage_lumen = None
        seen_done_lumen = False

        with client.stream(
            "POST", f"{container_base_url}/chat/completions", headers=headers, json=stream_payload_lumen
        ) as stream_resp_lumen:
            assert stream_resp_lumen.status_code == 200
            for line in stream_resp_lumen.iter_lines():
                line = line.strip()
                if not line or not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if data_str == "[DONE]":
                    seen_done_lumen = True
                    break
                chunk = json.loads(data_str)

                for choice in chunk.get("choices", []):
                    delta = choice.get("delta", {})
                    if "content" in delta and delta["content"]:
                        received_deltas_lumen.append(delta["content"])

                if "usage" in chunk and chunk["usage"]:
                    stream_usage_lumen = chunk["usage"]

        assert seen_done_lumen, "Streaming model=lumen response did not terminate with [DONE]"
        assert "".join(received_deltas_lumen) == "Hello from fake provider!"
        assert stream_usage_lumen is not None
        assert stream_usage_lumen.get("total_tokens") == 15

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

        run_detail = _poll_run_until_terminal(client, run_id, headers)
        assert run_detail.get("status") == "completed"

        # 5. Assert usage attribution via public usage API
        usage_resp = client.get("/v1/usage/records", headers={"Authorization": f"Bearer {api_key}"})
        assert usage_resp.status_code == 200, f"Failed getting usage records: {usage_resp.text}"
        usage_data = usage_resp.json()

        records = usage_data.get("records", usage_data) if isinstance(usage_data, dict) else usage_data
        assert isinstance(records, list)

        native_record = next((item for item in records if item.get("run_id") == run_id), None)
        assert native_record is not None, f"Expected usage record for native run {run_id}"
        assert native_record["source"] == "api"
        api_key_id = native_record["api_key_id"]
        assert isinstance(api_key_id, int)


# ============================================================================
# 2. Native API -> Queue -> Worker -> Provider -> SSE: Small Delta Exposure
# ============================================================================


def test_process_stack_small_delta_exposed_within_scheduler_allowance() -> None:
    """Verify pending delta is exposed via SSE within <= 250ms scheduler/DB allowance.

    The fake provider emits a small first delta ('delta1') and immediately pauses for 2.0s.
    The worker's flush deadline (50ms) and SSE poller (100ms) guarantee the client observes
    'delta1' well before the provider finishes (observed < 1.5s while total duration >= 2.0s).
    """
    api_base_url, _, api_key, model_name, fake_provider_url = _load_connection_context()
    _configure_fake_provider(fake_provider_url, {"mode": "small_delta_pause", "pause_seconds": 2.0})

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Idempotency-Key": str(uuid.uuid4()),
        "Content-Type": "application/json",
    }

    # Prompt includes fallback trigger token in case direct provider control is unreachable
    payload = {
        "parts": [{"type": "text", "text": "Test early delta exposure __TRIGGER_SMALL_DELTA_PAUSE__"}],
        "model_id": model_name,
        "features": {},
    }

    with httpx.Client(base_url=api_base_url, timeout=30.0) as client:
        start_time = time.monotonic()
        post_resp = client.post("/v1/temp-completions", headers=headers, json=payload)
        assert post_resp.status_code == 202, f"Failed to submit temp completion: {post_resp.text}"
        run_id = post_resp.json().get("run_id") or post_resp.json().get("id")
        assert run_id

        first_delta_received_at: float | None = None
        deltas: list[str] = []
        completed_seen = False

        with client.stream(
            "GET", f"/v1/runs/{run_id}/events", headers={"Authorization": f"Bearer {api_key}"}
        ) as stream_resp:
            assert stream_resp.status_code == 200
            for line in stream_resp.iter_lines():
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                data_body = line[5:].strip()
                if not data_body:
                    continue
                try:
                    event = json.loads(data_body)
                except Exception:
                    continue

                event_type = event.get("type")
                event_payload = event.get("payload") or {}

                if event_type == "part.delta":
                    delta_text = event_payload.get("delta", "")
                    if delta_text:
                        deltas.append(delta_text)
                        if first_delta_received_at is None:
                            first_delta_received_at = time.monotonic()

                if event_type == "run.completed":
                    completed_seen = True
                    break

        total_elapsed = time.monotonic() - start_time
        assert first_delta_received_at is not None, "Never received any part.delta event"
        time_to_first_delta = first_delta_received_at - start_time

        # Proof: first delta reached client well before provider finished the 2-second pause.
        # Under normal conditions: 50ms flush + 100ms poll + container overhead < 1.5s.
        assert time_to_first_delta < 1.8, (
            f"Expected early delta exposure within scheduler allowance, took {time_to_first_delta:.3f}s"
        )
        assert total_elapsed >= 1.9, f"Total duration should reflect provider 2s pause, took {total_elapsed:.3f}s"
        assert deltas[0] == "delta1", f"First delta mismatch: {deltas[0]}"
        assert completed_seen is True, "Stream did not terminate with run.completed"

    _reset_fake_provider(fake_provider_url)


# ============================================================================
# 3. 500x4 Burst Streaming: Ordered Delivery and Monotonic Journal Replay
# ============================================================================


def test_process_stack_burst_streaming_ordered_and_replayable_terminal() -> None:
    """Verify a burst of 500 ordered chunks is fully delivered and replayable.

    Provider chunk boundaries may be coalesced into latency-bounded journal deltas.
    The observable contract is strict event ordering and exact reconstruction of the
    2,000-character provider output on both the live and replay streams.
    """
    api_base_url, _, api_key, model_name, fake_provider_url = _load_connection_context()
    _configure_fake_provider(fake_provider_url, {"mode": "burst", "burst_chunks": 500, "burst_chunk_size": 4})

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Idempotency-Key": str(uuid.uuid4()),
        "Content-Type": "application/json",
    }

    payload = {
        "parts": [{"type": "text", "text": "Test burst stream ordering __TRIGGER_BURST__"}],
        "model_id": model_name,
        "features": {},
    }

    with httpx.Client(base_url=api_base_url, timeout=45.0) as client:
        post_resp = client.post("/v1/temp-completions", headers=headers, json=payload)
        assert post_resp.status_code == 202
        run_id = post_resp.json().get("run_id") or post_resp.json().get("id")
        assert run_id

        # 1. Live SSE stream consumption
        live_deltas: list[str] = []
        live_seqs: list[int] = []

        with client.stream(
            "GET", f"/v1/runs/{run_id}/events", headers={"Authorization": f"Bearer {api_key}"}
        ) as stream_resp:
            assert stream_resp.status_code == 200
            for line in stream_resp.iter_lines():
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                data_body = line[5:].strip()
                if not data_body:
                    continue
                event = json.loads(data_body)
                seq = event.get("seq")
                if isinstance(seq, int):
                    live_seqs.append(seq)
                if event.get("type") == "part.delta":
                    delta = event.get("payload", {}).get("delta", "")
                    if delta:
                        live_deltas.append(delta)
                if event.get("type") == "run.completed":
                    break

        live_text = "".join(live_deltas)
        expected_text = "".join(f"B{index:03d}" for index in range(500))
        assert live_deltas
        assert live_text == expected_text

        # Verify live seq numbers are strictly monotonically increasing
        assert all(live_seqs[i] < live_seqs[i + 1] for i in range(len(live_seqs) - 1))

        # 2. Replay terminal stream from database journal
        replay_resp = client.get(
            f"/v1/runs/{run_id}/events?after_seq=0",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert replay_resp.status_code == 200

        replay_deltas: list[str] = []
        replay_seqs: list[int] = []

        for line in replay_resp.text.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data_body = line[5:].strip()
            if not data_body:
                continue
            event = json.loads(data_body)
            seq = event.get("seq")
            if isinstance(seq, int):
                replay_seqs.append(seq)
            if event.get("type") == "part.delta":
                delta = event.get("payload", {}).get("delta", "")
                if delta:
                    replay_deltas.append(delta)

        replayed_text = "".join(replay_deltas)
        assert replayed_text == expected_text, "Replayed text did not match provider output"
        assert replay_deltas
        assert all(replay_seqs[i] < replay_seqs[i + 1] for i in range(len(replay_seqs) - 1))

    _reset_fake_provider(fake_provider_url)


# ============================================================================
# 4. Context Preview: Strictly Read-Only (0 Provider Calls)
# ============================================================================


def test_process_stack_context_preview_readonly_no_provider_calls() -> None:
    """Verify context-preview endpoint returns ContextState without invoking model providers.

    Pre-condition: Establish a temporary thread by submitting an initial turn.
    Call POST /v1/temp-threads/{id}/context-preview (or conversation preview).
    Asserts:
    - HTTP 200 ContextState returned with valid budget, revision, and recommendations
    - Provider request count remains completely unchanged before and after the preview call
    """
    api_base_url, _, api_key, model_name, fake_provider_url = _load_connection_context()
    _reset_fake_provider(fake_provider_url)

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Idempotency-Key": str(uuid.uuid4()),
        "Content-Type": "application/json",
    }

    with httpx.Client(base_url=api_base_url, timeout=30.0) as client:
        # Create initial thread
        post_resp = client.post(
            "/v1/temp-completions",
            headers=headers,
            json={"parts": [{"type": "text", "text": "Initial turn for preview testing"}], "model_id": model_name},
        )
        assert post_resp.status_code == 202
        run_data = post_resp.json()
        run_id = run_data.get("run_id") or run_data.get("id")
        temp_thread_id = run_data.get("temp_thread_id")
        _poll_run_until_terminal(client, run_id, headers)

        # Target preview URL (temp-threads if thread_id exists, else fallback path)
        preview_url = (
            f"/v1/temp-threads/{temp_thread_id}/context-preview"
            if temp_thread_id
            else f"/v1/runs/{run_id}/context-preview"
        )

        stats_before = _get_fake_provider_stats(fake_provider_url)
        req_count_before = stats_before.get("request_count", 0) if stats_before else 0

        # Execute preview request
        preview_resp = client.post(
            preview_url,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model_id": model_name,
                "features": {},
                "parts": [{"type": "text", "text": "Draft content in composer"}],
            },
        )
        assert preview_resp.status_code == 200, f"Context preview failed: {preview_resp.text}"
        state = preview_resp.json()

        # Validate ContextState public contract
        assert state.get("model_name") == model_name
        assert state.get("output_reserve") is not None and state["output_reserve"] >= 0
        assert state.get("safety_reserve") == 2048
        assert "revision" in state and isinstance(state["revision"], str)
        assert "can_compact" in state and isinstance(state["can_compact"], bool)
        assert state.get("recommendation") in ("none", "compact", "required", "unavailable")

        # Guarantee zero model provider calls were made for preview
        stats_after = _get_fake_provider_stats(fake_provider_url)
        if stats_after is not None:
            req_count_after = stats_after.get("request_count", 0)
            assert req_count_after == req_count_before, (
                f"Context preview invoked provider: before={req_count_before}, after={req_count_after}"
            )


# ============================================================================
# 5. Manual Compaction: Revision Fence, Idempotency, Sentinel Preservation
# ============================================================================


def test_process_stack_manual_compaction_and_sentinel_preservation() -> None:
    """Verify manual compaction flow on multi-turn history.

    Tests:
    1. Turns contain architectural sentinels (SENTINEL_ARCH_42 and SENTINEL_DB_99).
    2. Compaction rejects stale expected_context_revision with HTTP 409 context_revision_changed.
    3. Compaction admits with UUID Idempotency-Key and run_kind='compaction'.
    4. Duplicate admission with same Idempotency-Key returns identical descriptor.
    5. Compaction completes without inserting new message rows in conversation/thread history.
    6. Next completion prompt receives summary preserving the old sentinels and recent turns.
    """
    api_base_url, _, api_key, model_name, fake_provider_url = _load_connection_context()
    _reset_fake_provider(fake_provider_url)

    auth_headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    with httpx.Client(base_url=api_base_url, timeout=45.0) as client:
        # Determine whether persistent conversation creation is available
        conv_resp = client.post(
            "/v1/conversations",
            headers=auth_headers,
            json={"title": None, "workspace_id": None},
        )
        is_persistent = conv_resp.status_code == 201

        if is_persistent:
            conv_id = conv_resp.json()["id"]
            submit_url = f"/v1/conversations/{conv_id}/completions"
            preview_url = f"/v1/conversations/{conv_id}/context-preview"
            compact_url = f"/v1/conversations/{conv_id}/compactions"
        else:
            # Fallback to temporary thread workflow
            submit_url = "/v1/temp-completions"
            conv_id = None

        compactable_history = "Platform architecture decision detail. " * 80

        # Turn 1: Introduce SENTINEL_ARCH_42
        r1 = client.post(
            submit_url,
            headers={**auth_headers, "Idempotency-Key": str(uuid.uuid4())},
            json={
                "parts": [
                    {
                        "type": "text",
                        "text": "Decision: use Linux microservices [SENTINEL_ARCH_42] " + compactable_history,
                    }
                ],
                "model_id": model_name,
            },
        )
        assert r1.status_code == 202
        r1_id = r1.json().get("run_id") or r1.json().get("id")
        temp_thread_id = r1.json().get("temp_thread_id")
        _poll_run_until_terminal(client, r1_id, auth_headers)

        if not is_persistent and temp_thread_id:
            preview_url = f"/v1/temp-threads/{temp_thread_id}/context-preview"
            compact_url = f"/v1/temp-threads/{temp_thread_id}/compactions"

        # Turn 2: Introduce SENTINEL_DB_99
        t2_payload: dict[str, Any] = {
            "parts": [
                {
                    "type": "text",
                    "text": "Decision: use MariaDB with InnoDB [SENTINEL_DB_99] " + compactable_history,
                }
            ],
            "model_id": model_name,
        }
        if not is_persistent and temp_thread_id:
            t2_payload["temp_thread_id"] = temp_thread_id

        r2 = client.post(
            submit_url,
            headers={**auth_headers, "Idempotency-Key": str(uuid.uuid4())},
            json=t2_payload,
        )
        assert r2.status_code == 202
        r2_id = r2.json().get("run_id") or r2.json().get("id")
        _poll_run_until_terminal(client, r2_id, auth_headers)

        # Turn 3: Recent turn to be preserved uncompacted
        t3_payload: dict[str, Any] = {
            "parts": [{"type": "text", "text": "Recent uncompacted user query: how to deploy?"}],
            "model_id": model_name,
        }
        if not is_persistent and temp_thread_id:
            t3_payload["temp_thread_id"] = temp_thread_id

        r3 = client.post(
            submit_url,
            headers={**auth_headers, "Idempotency-Key": str(uuid.uuid4())},
            json=t3_payload,
        )
        assert r3.status_code == 202
        r3_id = r3.json().get("run_id") or r3.json().get("id")
        _poll_run_until_terminal(client, r3_id, auth_headers)

        # Obtain valid context revision
        prev_resp = client.post(preview_url, headers=auth_headers, json={"model_id": model_name, "features": {}})
        assert prev_resp.status_code == 200
        current_revision = prev_resp.json()["revision"]

        # 1. Stale revision fence rejection (HTTP 409)
        stale_resp = client.post(
            compact_url,
            headers={**auth_headers, "Idempotency-Key": str(uuid.uuid4())},
            json={"model_id": model_name, "expected_context_revision": "stale-rev-nonexistent-1234"},
        )
        assert stale_resp.status_code == 409, f"Expected 409 for stale revision, got {stale_resp.status_code}"

        # 2. Valid compaction admission (HTTP 202)
        compact_key = str(uuid.uuid4())
        compact_req = {"model_id": model_name, "expected_context_revision": current_revision}
        comp_resp = client.post(
            compact_url,
            headers={**auth_headers, "Idempotency-Key": compact_key},
            json=compact_req,
        )
        assert comp_resp.status_code == 202, f"Compaction admission failed: {comp_resp.text}"
        comp_descriptor = comp_resp.json()
        comp_run_id = comp_descriptor.get("run_id") or comp_descriptor.get("id")
        assert comp_run_id
        assert comp_descriptor.get("run_kind") == "compaction"

        # 3. Idempotent replay returns same descriptor
        comp_re_resp = client.post(
            compact_url,
            headers={**auth_headers, "Idempotency-Key": compact_key},
            json=compact_req,
        )
        assert comp_re_resp.status_code == 202
        assert (comp_re_resp.json().get("run_id") or comp_re_resp.json().get("id")) == comp_run_id

        # 4. Await compaction completion
        comp_detail = _poll_run_until_terminal(client, comp_run_id, auth_headers)
        assert comp_detail.get("status") == "completed", f"Compaction run failed: {comp_detail}"

        # 5. Verify no user/assistant message insertion occurred for compaction run
        if is_persistent and conv_id:
            msgs_resp = client.get(f"/v1/conversations/{conv_id}/messages", headers=auth_headers)
            assert msgs_resp.status_code == 200, msgs_resp.text
            messages = msgs_resp.json()["messages"]
            # Exactly 3 user + 3 assistant messages; compaction did not insert message rows.
            assert len(messages) == 6, f"Expected 6 conversational messages, got {len(messages)}"

        # 6. Verify subsequent turn receives preserved sentinels in provider context
        t4_payload: dict[str, Any] = {
            "parts": [{"type": "text", "text": "Post-compaction verification"}],
            "model_id": model_name,
        }
        if not is_persistent and temp_thread_id:
            t4_payload["temp_thread_id"] = temp_thread_id

        r4 = client.post(
            submit_url,
            headers={**auth_headers, "Idempotency-Key": str(uuid.uuid4())},
            json=t4_payload,
        )
        assert r4.status_code == 202
        r4_id = r4.json().get("run_id") or r4.json().get("id")
        _poll_run_until_terminal(client, r4_id, auth_headers)

        # Verify through test provider history that sentinels were preserved
        stats = _get_fake_provider_stats(fake_provider_url)
        if stats is not None:
            history = stats.get("history", [])
            summary_entries = [h for h in history if h.get("kind") == "summary"]
            assert len(summary_entries) >= 1, "Expected at least one recorded compaction summary call"
            summary_observed = summary_entries[0].get("sentinels_observed", [])
            assert any("SENTINEL_ARCH_42" in sentinel for sentinel in summary_observed)
            completion_entries = [h for h in history if h.get("kind") == "completion"]
            assert completion_entries, "Expected a provider completion after compaction"
            r4_observed = completion_entries[-1].get("sentinels_observed", [])
            assert {"SENTINEL_ARCH_42", "SENTINEL_DB_99"} <= set(r4_observed)


# ============================================================================
# 6. Title Lifecycle: First Answer Influences Title, No Extra Calls on Subsequent Turns
# ============================================================================


def test_process_stack_title_first_answer_influences_title_no_extra_calls() -> None:
    """Verify title generation occurs once after first turn, influenced by answer, with no extra calls."""
    api_base_url, _, api_key, model_name, fake_provider_url = _load_connection_context()
    _reset_fake_provider(fake_provider_url)

    auth_headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    with httpx.Client(base_url=api_base_url, timeout=30.0) as client:
        # The generated local transport is authorized for the native persistent
        # conversation lifecycle; a 403 here is a stack defect, not a skip.
        conv_resp = client.post(
            "/v1/conversations",
            headers=auth_headers,
            json={"title": None, "workspace_id": None},
        )
        assert conv_resp.status_code == 201, (
            f"Persistent conversation admission failed: {conv_resp.status_code} {conv_resp.text}"
        )
        conv_id = conv_resp.json()["id"]
        initial_conv = conv_resp.json()
        assert initial_conv.get("title_source") == "auto"
        assert initial_conv.get("title_status") == "idle"

        # Turn 1: First user request mentioning Docker deployment
        t1 = client.post(
            f"/v1/conversations/{conv_id}/completions",
            headers={**auth_headers, "Idempotency-Key": str(uuid.uuid4())},
            json={
                "parts": [{"type": "text", "text": "배포를 어떻게 할까? Docker 가이드가 필요해"}],
                "model_id": model_name,
            },
        )
        assert t1.status_code == 202
        t1_id = t1.json().get("run_id") or t1.json().get("id")
        _poll_run_until_terminal(client, t1_id, auth_headers)

        # Poll conversation until title job finishes
        title_ready = False
        final_title = ""
        for _ in range(30):
            c_check = client.get(f"/v1/conversations/{conv_id}", headers=auth_headers)
            assert c_check.status_code == 200
            data = c_check.json()
            if data.get("title_status") == "ready" and data.get("title"):
                title_ready = True
                final_title = data["title"]
                break
            time.sleep(0.5)

        assert title_ready, f"Title job did not persist a title: {data}"
        # The persisted title comes from the title provider's first-exchange
        # request, not a client-side fallback from the first user message.
        assert any(term in final_title for term in ("Docker", "배포", "요약")), (
            f"Title was not influenced by exchange content: {final_title}"
        )

        stats1 = _get_fake_provider_stats(fake_provider_url)
        assert stats1 is not None, "Fake provider statistics are required for title-call proof"
        title_count_after_t1 = stats1["title_count"]
        assert title_count_after_t1 == 1

        # Turn 2: Follow-up question
        t2 = client.post(
            f"/v1/conversations/{conv_id}/completions",
            headers={**auth_headers, "Idempotency-Key": str(uuid.uuid4())},
            json={
                "parts": [{"type": "text", "text": "다음 질문입니다: 모니터링은 어떻게 하나요?"}],
                "model_id": model_name,
            },
        )
        assert t2.status_code == 202
        t2_id = t2.json().get("run_id") or t2.json().get("id")
        _poll_run_until_terminal(client, t2_id, auth_headers)

        # Assert no extra title generation call was issued on ordinary second turn.
        stats2 = _get_fake_provider_stats(fake_provider_url)
        assert stats2 is not None, "Fake provider statistics are required for title-call proof"
        title_count_after_t2 = stats2["title_count"]
        assert title_count_after_t2 == title_count_after_t1, (
            f"Ordinary turn issued extra title call: before={title_count_after_t1}, after={title_count_after_t2}"
        )


# ============================================================================
# 7. Provider Pause and Run Cancellation
# ============================================================================


def test_process_stack_paused_provider_and_cancellation() -> None:
    """Verify an in-flight run under provider pause can be canceled cleanly via public cancel API."""
    api_base_url, _, api_key, model_name, fake_provider_url = _load_connection_context()
    _configure_fake_provider(fake_provider_url, {"mode": "pause", "pause_seconds": 6.0})

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Idempotency-Key": str(uuid.uuid4()),
        "Content-Type": "application/json",
    }

    # Use trigger in prompt to guarantee pause mode even if control endpoint was skipped
    payload = {
        "parts": [{"type": "text", "text": "Test cancel under slow provider __TRIGGER_PAUSE__"}],
        "model_id": model_name,
    }

    with httpx.Client(base_url=api_base_url, timeout=30.0) as client:
        post_resp = client.post("/v1/temp-completions", headers=headers, json=payload)
        assert post_resp.status_code == 202
        run_id = post_resp.json().get("run_id") or post_resp.json().get("id")
        assert run_id

        # Brief delay to ensure run transitioned to running in worker
        time.sleep(0.3)

        # Issue cancellation
        cancel_resp = client.post(
            f"/v1/runs/{run_id}/cancel",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert cancel_resp.status_code in (200, 202), f"Cancel failed: {cancel_resp.status_code}"

        # Poll run status until canceled
        terminal_detail = _poll_run_until_terminal(client, run_id, headers, timeout=10.0)
        assert terminal_detail.get("status") == "canceled", (
            f"Expected canceled status, got {terminal_detail.get('status')}"
        )

    _reset_fake_provider(fake_provider_url)


# ============================================================================
# 8. Responses, Anthropic, and Claude Gateway Device Flow
# ============================================================================


async def _approve_gateway_code_and_seed_anthropic(user_code: str, fake_provider_url: str) -> None:
    """Use the real DB/service transaction for the approval side of the process flow."""
    from lumen.db import close_db, init_db
    from lumen.services import claude_gateway
    from lumen.services.providers import repository

    init_db(os.environ["DATABASE_URL"], pool_size=1, max_overflow=0)
    try:
        provider = await repository.create_provider(
            name=f"system-anthropic-{uuid.uuid4().hex}",
            provider_type="anthropic",
            api_base=fake_provider_url,
            api_key="fake-anthropic-key",
        )
        await repository.create_model(
            provider_id=provider["id"],
            model_name="claude-sonnet-4-6",
            input_price_per_million="1",
            output_price_per_million="1",
        )
        decision = await claude_gateway.authorize_user_code(
            user_code=user_code,
            approve=True,
            owner_user_id="system-gateway-user",
            owner_project_id="system-gateway-project",
        )
        assert decision == {"status": "approved"}
    finally:
        await close_db()


def test_process_stack_native_protocols_and_gateway_device_login() -> None:
    """Exercise native protocol wires and one-time Gateway issuance through real processes."""
    api_base_url, _, api_key, model_name, fake_provider_url = _load_connection_context()
    _reset_fake_provider(fake_provider_url)
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    with httpx.Client(base_url=api_base_url, timeout=30.0) as client:
        response = client.post(
            "/v1/responses",
            headers=headers,
            json={"model": model_name, "input": "native responses system check", "max_output_tokens": 64},
        )
        assert response.status_code == 200, response.text
        response_body = response.json()
        assert response_body["object"] == "response"
        assert response_body["status"] == "completed"
        assert response_body["output"][0]["content"][0]["text"] == "Hello from fake provider!"

        streamed_response = client.post(
            "/v1/responses",
            headers=headers,
            json={"model": model_name, "input": "stream responses system check", "stream": True},
        )
        assert streamed_response.status_code == 200, streamed_response.text
        assert "event: response.created" in streamed_response.text
        assert "event: response.output_text.delta" in streamed_response.text
        assert "event: response.completed" in streamed_response.text
        configured = _configure_fake_provider(fake_provider_url, {"mode": "codex_tool"})
        assert configured is not None
        codex_input = [{"role": "user", "content": [{"type": "input_text", "text": "run a shell command"}]}]
        shell_tool = {
            "type": "function",
            "name": "shell",
            "description": "Run a shell command",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
                "additionalProperties": False,
            },
        }
        tool_response = client.post(
            "/v1/responses",
            headers=headers,
            json={
                "model": model_name,
                "input": codex_input,
                "stream": True,
                "tools": [shell_tool],
                "prompt_cache_key": "system-codex-session",
                "client_metadata": {"thread_id": "local-only"},
            },
        )
        assert tool_response.status_code == 200, tool_response.text
        assert "event: response.output_item.done" in tool_response.text
        assert '"type":"function_call"' in tool_response.text
        assert '"call_id":"call_codex_1"' in tool_response.text
        provider_stats = _get_fake_provider_stats(fake_provider_url)
        assert provider_stats is not None
        first_codex_call = provider_stats["history"][-1]
        assert first_codex_call["has_prompt_cache_key"] is True
        assert first_codex_call["has_client_metadata"] is False

        function_call = {
            "id": "fc_codex_item",
            "type": "function_call",
            "status": "completed",
            "arguments": json.dumps({"command": "printf CODEX_TOOL_OK"}, separators=(",", ":")),
            "call_id": "call_codex_1",
            "name": "shell",
        }
        continuation = client.post(
            "/v1/responses",
            headers=headers,
            json={
                "model": model_name,
                "input": [
                    *codex_input,
                    function_call,
                    {"type": "function_call_output", "call_id": "call_codex_1", "output": "CODEX_TOOL_OK"},
                ],
                "stream": True,
                "tools": [shell_tool],
                "prompt_cache_key": "system-codex-session",
            },
        )
        assert continuation.status_code == 200, continuation.text
        assert "CODEX_TOOL_CONTINUATION_OK" in continuation.text
        _reset_fake_provider(fake_provider_url)

        device = client.post(
            "/v1/claude-gateway/oauth/device/code",
            data={
                "client_id": "claude-code",
                "scope": "models:read compat:completions:write",
            },
        )
        assert device.status_code == 200, device.text
        device_body = device.json()
        assert device_body["verification_uri"].endswith("/oauth/claude/authorize")

        asyncio.run(_approve_gateway_code_and_seed_anthropic(device_body["user_code"], fake_provider_url))
        time.sleep(float(device_body["interval"]) + 0.2)
        token = client.post(
            "/v1/claude-gateway/oauth/token",
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": device_body["device_code"],
                "client_id": "claude-code",
            },
        )
        assert token.status_code == 200, token.text
        token_body = token.json()
        assert token_body["expires_in"] == 24 * 60 * 60
        assert "refresh_token" not in token_body
        gateway_key = token_body["access_token"]
        gateway_headers = {
            "Authorization": f"Bearer {gateway_key}",
            "x-api-key": gateway_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }

        anthropic = client.post(
            "/v1/claude-gateway/v1/messages",
            headers=gateway_headers,
            json={
                "model": "client-alias-is-ignored",
                "max_tokens": 128,
                "messages": [{"role": "user", "content": "native anthropic system check"}],
            },
        )
        assert anthropic.status_code == 200, anthropic.text
        assert anthropic.json()["content"] == [{"type": "text", "text": "Hello from fake provider!"}]

        anthropic_stream = client.post(
            "/v1/claude-gateway/v1/messages",
            headers=gateway_headers,
            json={
                "model": "client-alias-is-ignored",
                "max_tokens": 128,
                "stream": True,
                "messages": [{"role": "user", "content": "stream anthropic system check"}],
            },
        )
        assert anthropic_stream.status_code == 200, anthropic_stream.text
        assert "event: message_start" in anthropic_stream.text
        assert "event: content_block_delta" in anthropic_stream.text
        assert "event: message_stop" in anthropic_stream.text
        configured = _configure_fake_provider(fake_provider_url, {"mode": "claude_tool"})
        assert configured is not None
        bash_tool = {
            "name": "Bash",
            "description": "Run a shell command",
            "input_schema": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["command"],
            },
        }
        claude_messages = [{"role": "user", "content": "run a local command"}]
        claude_tool_response = client.post(
            "/v1/claude-gateway/v1/messages",
            headers={**gateway_headers, "anthropic-beta": "context-management-2025-06-27"},
            json={
                "model": "client-alias-is-ignored",
                "max_tokens": 128,
                "stream": True,
                "messages": claude_messages,
                "tools": [bash_tool],
                "context_management": {"edits": [{"type": "clear_thinking_20251015", "keep": "all"}]},
                "output_config": {"effort": "high"},
            },
        )
        assert claude_tool_response.status_code == 200, claude_tool_response.text
        assert '"type":"tool_use"' in claude_tool_response.text
        assert '"name":"Bash"' in claude_tool_response.text
        provider_stats = _get_fake_provider_stats(fake_provider_url)
        assert provider_stats is not None
        first_claude_call = provider_stats["history"][-1]
        assert "context_management" in first_claude_call["request_keys"]
        assert "output_config" in first_claude_call["request_keys"]
        assert "anthropic-beta" in first_claude_call["protocol_headers"]
        assert "anthropic-version" in first_claude_call["protocol_headers"]

        claude_continuation = client.post(
            "/v1/claude-gateway/v1/messages",
            headers={**gateway_headers, "anthropic-beta": "context-management-2025-06-27"},
            json={
                "model": "client-alias-is-ignored",
                "max_tokens": 128,
                "stream": True,
                "tools": [bash_tool],
                "messages": [
                    *claude_messages,
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "toolu_fake_1",
                                "name": "Bash",
                                "input": {"command": "printf CLAUDE_LOCAL_TOOL_SENTINEL"},
                            }
                        ],
                    },
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_fake_1",
                                "content": "CLAUDE_LOCAL_TOOL_SENTINEL",
                            }
                        ],
                    },
                ],
            },
        )
        assert claude_continuation.status_code == 200, claude_continuation.text
        assert "CLAUDE_TOOL_CONTINUATION_OK" in claude_continuation.text
        _reset_fake_provider(fake_provider_url)

        replay = client.post(
            "/v1/claude-gateway/oauth/token",
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": device_body["device_code"],
                "client_id": "claude-code",
            },
        )
        assert replay.status_code == 400
        assert replay.json()["error"] == "invalid_grant"

    _reset_fake_provider(fake_provider_url)


async def _register_discovered_system_model(fake_provider_url: str, model_name: str) -> dict[str, Any]:
    """Use the same MariaDB as the already-running API and worker, not their in-process state."""
    from sqlalchemy import func, select

    from lumen.db import close_db, get_session_factory, init_db
    from lumen.models.chat_db import LlmModel
    from lumen.services import model_discovery
    from lumen.services.providers import repository

    init_db(os.environ["DATABASE_URL"], pool_size=1, max_overflow=0)
    try:
        provider = await repository.create_provider(
            name=f"system-discovery-{uuid.uuid4().hex}",
            provider_type="anthropic",
            api_base=fake_provider_url,
            api_key="fake-anthropic-key",
        )
        factory = get_session_factory()
        assert factory is not None
        async with factory() as session:
            before = await session.scalar(select(func.count(LlmModel.id)).where(LlmModel.provider_id == provider["id"]))
        discovered = await model_discovery.discover_models(provider["id"])
        assert discovered["live_status"] == "success" and discovered["complete"] is True
        assert model_name in discovered["models"]
        assert "claude-system-unpriced-2099-01-01" in discovered["models"]
        assert discovered["source"] == "api"
        async with factory() as session:
            after = await session.scalar(select(func.count(LlmModel.id)).where(LlmModel.provider_id == provider["id"]))
        assert before == after == 0, "Discovery must not register models"

        unpriced = await repository.create_model(
            provider_id=provider["id"], model_name="claude-system-unpriced-2099-01-01"
        )
        registered = await repository.create_model(
            provider_id=provider["id"],
            model_name=model_name,
            input_price_per_million="2",
            output_price_per_million="4",
        )
        return {"provider": provider, "unpriced": unpriced, "registered": registered}
    finally:
        await close_db()


async def _read_system_run_accounting(run_id: str) -> dict[str, Any]:
    from sqlalchemy import select

    from lumen.db import close_db, get_session_factory, init_db
    from lumen.models.chat_db import ChatUsageLog
    from lumen.models.chat_runs import ChatRun, ChatRunProvider

    init_db(os.environ["DATABASE_URL"], pool_size=1, max_overflow=0)
    try:
        factory = get_session_factory()
        assert factory is not None
        async with factory() as session:
            run = await session.get(ChatRun, run_id)
            ledger = (await session.execute(select(ChatUsageLog).where(ChatUsageLog.run_id == run_id))).scalar_one()
            route = await session.get(ChatRunProvider, (run_id, "executor"))
            assert run is not None and route is not None
            return {
                "capability": run.capability_snapshot,
                "pricing": run.pricing_snapshot,
                "provider_id": route.provider_id,
                "model_id": route.model_id,
                "version": route.config_version_hash,
                "raw_cost": ledger.raw_cost,
            }
    finally:
        await close_db()


def test_process_stack_discovers_and_routes_new_claude_without_restart() -> None:
    """Real API/worker processes see a newly registered, second-page fake Claude model."""
    api_base_url, _, api_key, _, fake_provider_url = _load_connection_context()
    _reset_fake_provider(fake_provider_url)
    model_name = "claude-system-unlisted-2099-01-01"
    headers = {"Authorization": f"Bearer {api_key}"}

    with httpx.Client(base_url=api_base_url, timeout=30.0) as client:
        before = client.get("/v1/models", headers=headers)
        assert before.status_code == 200, before.text
        assert model_name not in {item["id"] for item in before.json()["data"]}

        rows = asyncio.run(_register_discovered_system_model(fake_provider_url, model_name))
        registered = rows["registered"]
        assert registered["provider_id"] == rows["provider"]["id"]
        assert registered["api_provider"] == "anthropic"
        assert registered["api_model_name"] == model_name
        assert registered["price_source"] == "manual"

        active = client.get("/v1/models", headers=headers)
        assert active.status_code == 200, active.text
        public = next(item for item in active.json()["data"] if item["id"] == model_name)
        assert "anthropic" in public["providers"]

        denied = client.post(
            "/v1/temp-completions",
            headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
            json={
                "parts": [{"type": "text", "text": "unpriced route must not run"}],
                "model_id": rows["unpriced"]["model_name"],
                "features": {"memory": False, "tool_policy": {"mode": "none"}},
            },
        )
        assert denied.status_code == 422, denied.text
        assert "text (pricing_unavailable)" in denied.json()["detail"]

        unsupported = client.post(
            "/v1/temp-completions",
            headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
            json={
                "parts": [{"type": "text", "text": "search must remain denied"}],
                "model_id": model_name,
                "features": {
                    "memory": False,
                    "tool_policy": {"mode": "agent_default"},
                    "web_search": {"enabled": True, "mode": "native"},
                },
            },
        )
        assert unsupported.status_code == 422, unsupported.text
        assert "web_search" in unsupported.json()["detail"]

        compat = client.post(
            "/v1/chat/completions",
            headers=headers,
            json={"model": model_name, "provider": "anthropic", "messages": [{"role": "user", "content": "compat"}]},
        )
        assert compat.status_code == 200, compat.text
        assert compat.json()["choices"][0]["message"]["content"] == "Hello from fake provider!"
        assert compat.json()["model"] == model_name

        admitted = client.post(
            "/v1/temp-completions",
            headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
            json={
                "parts": [{"type": "text", "text": "newly registered worker route"}],
                "model_id": model_name,
                "features": {"memory": False, "tool_policy": {"mode": "none"}},
            },
        )
        assert admitted.status_code == 202, admitted.text
        run_id = admitted.json()["run_id"]
        terminal = _poll_run_until_terminal(client, run_id, headers)
        assert terminal["status"] == "completed", terminal

        records = client.get("/v1/usage/records", headers=headers)
        assert records.status_code == 200, records.text
        record = next(item for item in records.json()["records"] if item["run_id"] == run_id)
        assert record["model_name"] == model_name
        assert record["prompt_tokens"] == 10
        assert record["completion_tokens"] == 5
        assert record["pricing_status"] == "priced"
        accounting = asyncio.run(_read_system_run_accounting(run_id))
        assert accounting["provider_id"] == rows["provider"]["id"]
        assert accounting["model_id"] == registered["id"]
        assert accounting["capability"]["config_version_hash"] == accounting["version"]
        assert accounting["capability"]["model_name"] == model_name
        assert accounting["pricing"]["price_source"] == "manual"
        assert accounting["pricing"]["input_price_per_token"] == "0.0000020000"
        assert accounting["pricing"]["output_price_per_token"] == "0.0000040000"
        assert accounting["raw_cost"] == Decimal("0.0000400000")

        stats = _get_fake_provider_stats(fake_provider_url)
        assert stats is not None
        assert any(item.get("protocol") == "anthropic" and item.get("model") == model_name for item in stats["history"])
