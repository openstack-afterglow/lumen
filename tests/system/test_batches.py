"""System tests for Keystone Native Batch and model-key compatibility denials.

The API, the online worker and the batch-only worker run as separate processes on an
internal network. Text routes reach the fake provider over HTTP; image/audio transports
keep their official ``https://api.openai.com`` endpoints, which resolve to the fake
provider's TLS listener through compose aliases and a test-only CA bundle. Every check
here consumes the public wire (native Lumen SDK and compatibility endpoints).
Native tests also read the MariaDB ledger that the processes actually wrote.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import math
import os
import socket
import ssl
import struct
import time
import uuid
import wave
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

import certifi
import httpx
import pytest
from PIL import Image
from system.fake_keystone import OWNER_TOKEN

if TYPE_CHECKING:
    # The SDK is installed only in the system test image; host contract runs only collect.
    from lumen_sdk import AsyncClient, Client

pytestmark = pytest.mark.system

TERMINAL_BATCH_STATUSES = frozenset({"completed", "failed", "cancelled", "expired"})
FINITE_OPERATIONS = (
    "chat.completions",
    "responses",
    "images.generations",
    "images.edits",
    "audio.speech",
    "audio.transcriptions",
)
# Fake-provider fixtures (tests/system/fake_openai.py).
FAKE_TEXT = "Hello from fake provider!"
FAKE_TRANSCRIPTION = "Hello from fake transcription!"
FAKE_TOOL_CALL_ID = "call_batch_tool_1"
FAKE_TOOL_ARGUMENTS = {"city": "Seoul"}
BATCH_ERROR_SENTINEL = "SYSTEM_BATCH_CONFIRMED_ERROR"
FAKE_ERROR_MESSAGE = "Intentional system batch provider error"
# Explicit system-stack prices: text 1/3 USD per million tokens with fake usage 10/5,
# gpt-image-2 1024x1024:high, tts-1 per character, whisper-1 per minute of a 1 s WAV.
TEXT_RAW_COST = Decimal("0.000025")
IMAGE_RAW_COST = Decimal("0.05")
TTS_USD_PER_CHARACTER = Decimal("0.000015")
STT_RAW_COST_ONE_SECOND = Decimal("0.0001")
WAV_RATE = 24000
WAV_SECONDS = 1
SPEECH_INPUT = "Hello from the Lumen batch speech item."
TEXT_HOST = "fake-provider"
MEDIA_HOST = "api.openai.com"
OPERATION_PATHS = {
    "chat.completions": ("/v1/chat/completions", TEXT_HOST, False),
    "responses": ("/v1/responses", TEXT_HOST, False),
    "images.generations": ("/v1/images/generations", MEDIA_HOST, True),
    "images.edits": ("/v1/images/edits", MEDIA_HOST, True),
    "audio.speech": ("/v1/audio/speech", MEDIA_HOST, True),
    "audio.transcriptions": ("/v1/audio/transcriptions", MEDIA_HOST, True),
}
RUN_KINDS = {
    "chat.completions": "api_completion",
    "responses": "api_completion",
    "images.generations": "image",
    "images.edits": "image",
    "audio.speech": "tts",
    "audio.transcriptions": "stt",
}
ITEM_SCOPES = {
    "chat.completions": {"compat:completions:write"},
    "responses": {"compat:completions:write"},
    "images.generations": {"native:images:write"},
    "images.edits": {"native:images:write", "native:assets:read"},
    "audio.speech": {"native:audio:write"},
    "audio.transcriptions": {"native:audio:write", "native:assets:read"},
}
CHAT_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Look up the current weather for a city",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
            "additionalProperties": False,
        },
    },
}
RESPONSES_TOOL = {"type": "function", **CHAT_TOOL["function"]}


# ============================================================================
# Connection, fake-provider metrics and fixtures
# ============================================================================


def _connection() -> dict[str, str]:
    api_base_url = os.environ.get("LUMEN_API_BASE_URL", "http://localhost:8012").rstrip("/")
    manifest = json.loads(Path(os.environ.get("LUMEN_CONNECTION_FILE", "/seed/connection.json")).read_text())
    assert manifest.get("schema_version") == 1
    api_key = manifest["api_key"]
    assert isinstance(api_key, str) and api_key.startswith("sk-afgl-")
    return {
        "api_base_url": api_base_url,
        "compat_base_url": (manifest.get("container_base_url") or f"{api_base_url}/v1").rstrip("/"),
        "api_key": api_key,
        "model": manifest.get("model") or os.environ.get("LUMEN_MODEL_NAME", "fake-gpt-4"),
        "fake_provider_url": os.environ.get("LUMEN_FAKE_PROVIDER_URL", "http://fake-provider:8080").rstrip("/"),
    }


def _fake_control(fake_provider_url: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Required (not best-effort) control call: a missing metric must fail the test."""
    with httpx.Client(timeout=10.0) as client:
        if payload is None and path.endswith("/stats"):
            response = client.get(f"{fake_provider_url}{path}")
        else:
            response = client.post(f"{fake_provider_url}{path}", json=payload or {})
    assert response.status_code == 200, f"fake provider {path} failed: {response.status_code}"
    return response.json()


def _reset_fake(fake_provider_url: str) -> None:
    _fake_control(fake_provider_url, "/_control/reset")
    stats = _fake_stats(fake_provider_url)
    assert stats["calls"] == []
    assert stats["operation_counts"] == dict.fromkeys(FINITE_OPERATIONS, 0)


def _fake_stats(fake_provider_url: str) -> dict[str, Any]:
    return _fake_control(fake_provider_url, "/_control/stats")


def _api_model_name(model_name: str) -> str:
    return model_name.split("/", 1)[1] if model_name.startswith("openai/") else model_name


def _assert_provider_calls(
    stats: dict[str, Any],
    expected: dict[str, int],
    *,
    models: dict[str, str],
    statuses: dict[str, Counter] | None = None,
) -> None:
    """Every finite provider call hit the fake on its official path, host and transport."""
    assert stats["operation_counts"] == {**dict.fromkeys(FINITE_OPERATIONS, 0), **expected}
    calls = [call for call in stats["calls"] if call["operation"] in FINITE_OPERATIONS]
    assert len(calls) == sum(expected.values())
    observed: dict[str, Counter] = {}
    for call in calls:
        path, host, tls = OPERATION_PATHS[call["operation"]]
        assert call["path"] == path, call
        assert call["host"].split(":", 1)[0].lower() == host, call
        assert call["tls"] is tls, call
        assert call["model"] == models[call["operation"]], call
        observed.setdefault(call["operation"], Counter())[call["status"]] += 1
    expected_statuses = statuses or {operation: Counter({200: count}) for operation, count in expected.items() if count}
    assert observed == expected_statuses


def _png_fixture() -> bytes:
    image = Image.new("RGB", (1024, 1024), (24, 64, 196))
    for x in range(256, 768):
        for y in (256, 767):
            image.putpixel((x, y), (250, 250, 250))
            image.putpixel((y, x), (250, 250, 250))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _wav_fixture() -> bytes:
    frames = b"".join(
        struct.pack("<h", int(12000 * math.sin(2 * math.pi * 440 * index / WAV_RATE)))
        for index in range(WAV_RATE * WAV_SECONDS)
    )
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(WAV_RATE)
        writer.writeframes(frames)
    return buffer.getvalue()


def _assert_png(data: bytes) -> None:
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    with Image.open(io.BytesIO(data)) as image:
        image.verify()
    with Image.open(io.BytesIO(data)) as image:
        image.load()
        assert image.format == "PNG"
        assert image.size == (1024, 1024)


def _assert_wav(data: bytes) -> None:
    assert data[:4] == b"RIFF" and data[8:12] == b"WAVE"
    with wave.open(io.BytesIO(data), "rb") as reader:
        assert reader.getcomptype() == "NONE"
        assert reader.getnchannels() == 1
        assert reader.getsampwidth() == 2
        assert reader.getframerate() == WAV_RATE
        frames = reader.getnframes()
        assert frames / reader.getframerate() == pytest.approx(WAV_SECONDS)
        pcm = reader.readframes(frames)
    assert len(pcm) == frames * 2 == WAV_RATE * WAV_SECONDS * 2


def _assert_uploaded_asset(asset: dict[str, Any], data: bytes, mime_type: str) -> None:
    assert asset["status"] == "clean"
    assert asset["mime_type"] == mime_type
    assert asset["size_bytes"] == len(data)
    assert asset["sha256"] == hashlib.sha256(data).hexdigest()


# ============================================================================
# MariaDB reads (same database the running processes use)
# ============================================================================


async def _media_models() -> dict[str, dict[str, Any]]:
    from sqlalchemy import select

    from lumen.db import close_db, get_session_factory, init_db
    from lumen.models.chat_db import LlmModel, LlmProvider

    expected = {"image": "gpt-image-2", "tts": "tts-1", "stt": "whisper-1"}
    init_db(os.environ["DATABASE_URL"], pool_size=1, max_overflow=0)
    try:
        factory = get_session_factory()
        assert factory is not None
        async with factory() as session:
            rows = (
                await session.execute(
                    select(LlmModel, LlmProvider)
                    .join(LlmProvider, LlmProvider.id == LlmModel.provider_id)
                    .where(LlmModel.model_kind.in_(tuple(expected)), LlmModel.is_active.is_(True))
                )
            ).all()
        models: dict[str, dict[str, Any]] = {}
        for kind, name in expected.items():
            matches = [(model, provider) for model, provider in rows
                       if model.model_kind == kind and _api_model_name(model.model_name) == name]
            assert len(matches) == 1, f"expected exactly one bootstrapped {kind} model {name}"
            model, provider = matches[0]
            # Media transports must use the official endpoint; no product-level test base URL.
            assert provider.api_base in (None, "", "https://api.openai.com", "https://api.openai.com/v1")
            models[kind] = {"id": str(model.id), "model_name": model.model_name, "provider_id": model.provider_id}
        return models
    finally:
        await close_db()


async def _batch_ledger(batch_id: str) -> dict[str, Any]:
    """Load the batch, its items/runs and the exact run-level ledger rows."""
    from sqlalchemy import select

    from lumen.db import close_db, get_session_factory, init_db
    from lumen.models.chat_assets import ChatAsset, ChatRunAsset
    from lumen.models.chat_batches import ChatBatch, ChatBatchFile, ChatBatchItem
    from lumen.models.chat_db import ChatUsageLog
    from lumen.models.chat_infrastructure import ChatWorkerRegistration
    from lumen.models.chat_runs import ChatModelCallReservation, ChatRun

    init_db(os.environ["DATABASE_URL"], pool_size=1, max_overflow=0)
    try:
        factory = get_session_factory()
        assert factory is not None
        async with factory() as session:
            batch = await session.get(ChatBatch, batch_id)
            assert batch is not None, f"batch {batch_id} is absent from MariaDB"
            items = (
                await session.execute(
                    select(ChatBatchItem).where(ChatBatchItem.batch_id == batch_id).order_by(ChatBatchItem.ordinal)
                )
            ).scalars().all()
            runs = (await session.execute(select(ChatRun).where(ChatRun.batch_id == batch_id))).scalars().all()
            run_ids = [run.id for run in runs]
            reservations = (
                await session.execute(
                    select(ChatModelCallReservation).where(ChatModelCallReservation.run_id.in_(run_ids))
                )
            ).scalars().all()
            ledgers = (
                await session.execute(select(ChatUsageLog).where(ChatUsageLog.run_id.in_(run_ids)))
            ).scalars().all()
            run_assets = (
                await session.execute(select(ChatRunAsset).where(ChatRunAsset.run_id.in_(run_ids)))
            ).scalars().all()
            asset_ids = {link.asset_id for link in run_assets} | {
                item.input_asset_id for item in items if item.input_asset_id
            }
            assets = (
                await session.execute(select(ChatAsset).where(ChatAsset.id.in_(asset_ids)))
            ).scalars().all()
            registrations = (await session.execute(select(ChatWorkerRegistration))).scalars().all()
            file_ids = {batch.input_file_id, batch.output_file_id, batch.error_file_id} - {None}
            files = (
                await session.execute(select(ChatBatchFile).where(ChatBatchFile.id.in_(file_ids)))
            ).scalars().all()
        grouped: dict[str, dict[str, list]] = {run_id: {"reservations": [], "ledgers": [], "assets": []}
                                               for run_id in run_ids}
        for reservation in reservations:
            grouped[reservation.run_id]["reservations"].append(reservation)
        for ledger in ledgers:
            grouped[ledger.run_id]["ledgers"].append(ledger)
        for link in run_assets:
            grouped[link.run_id]["assets"].append(link)
        return {
            "batch": batch,
            "items": items,
            "runs": {run.id: run for run in runs},
            "by_run": grouped,
            "assets": {asset.id: asset for asset in assets},
            "registrations": {registration.id: registration for registration in registrations},
            "files": {file.id: file for file in files},
        }
    finally:
        await close_db()


def _assert_batch_only_worker(ledger: dict[str, Any], run: Any) -> None:
    assert run.workload_class == "batch"
    assert run.worker_registration_id is not None, f"run {run.id} was never claimed by a registered worker"
    registration = ledger["registrations"][run.worker_registration_id]
    assert list(registration.workload_classes) == ["batch"]
    # The stack also runs a separate online worker that must not have claimed batch runs.
    assert any("batch" not in list(other.workload_classes) for other in ledger["registrations"].values())


def _assert_settled_once(ledger: dict[str, Any], run: Any, raw_cost: Decimal) -> None:
    """Exactly one settled hold and one priced ledger row keyed to the durable run."""
    rows = ledger["by_run"][run.id]
    assert run.status == "completed"
    assert run.usage_reconciled_at is not None
    assert len(rows["reservations"]) == 1
    hold = rows["reservations"][0]
    assert hold.segment_id == f"{run.run_kind}:1"
    assert hold.status == "settled" and hold.settled_at is not None
    assert len(rows["ledgers"]) == 1
    usage = rows["ledgers"][0]
    assert usage.event_id == f"run:{run.id}"
    assert usage.pricing_status == "priced"
    assert usage.raw_cost == raw_cost
    assert usage.credited_cost > 0
    assert hold.actual_credits == usage.credited_cost
    assert hold.actual_credits <= hold.bound_credits
    assert usage.source == run.source == "web"
    assert usage.api_key_id == run.api_key_id == ledger["batch"].api_key_id
    assert usage.project_id == run.project_id == ledger["batch"].project_id
    assert usage.user_id == run.user_id == ledger["batch"].user_id


def _assert_output_asset(ledger: dict[str, Any], run: Any, reference: dict[str, Any], data: bytes, mime: str) -> None:
    links = [link for link in ledger["by_run"][run.id]["assets"] if link.purpose == "output"]
    assert [link.asset_id for link in links] == [reference["asset_id"]]
    asset = ledger["assets"][reference["asset_id"]]
    assert asset.status == "clean"
    assert asset.mime_type == reference["mime_type"] == mime
    assert asset.size_bytes == reference["size_bytes"] == len(data)
    assert asset.sha256 == hashlib.sha256(data).hexdigest()
    assert (asset.project_id, asset.user_id) == (run.project_id, run.user_id)


def _assert_input_pin(ledger: dict[str, Any], item: Any, run: Any, asset_id: str) -> None:
    assert item.input_asset_id == asset_id
    inputs = [link.asset_id for link in ledger["by_run"][run.id]["assets"] if link.purpose == "input"]
    assert inputs == [asset_id]
    assert ledger["assets"][asset_id].status == "clean"


# ============================================================================
# Native mixed batch (Lumen SDK)
# ============================================================================


def _native_items(model_name: str, models: dict[str, dict[str, Any]], png_id: str, wav_id: str,
                  tag: str) -> list[dict[str, Any]]:
    return [
        {"custom_id": "chat-text", "operation": "chat.completions", "body": {
            "model": model_name, "max_tokens": 64,
            "messages": [{"role": "user", "content": f"{tag} native batch chat"}]}},
        {"custom_id": "chat-tool", "operation": "chat.completions", "body": {
            "model": model_name, "max_tokens": 64, "tools": [CHAT_TOOL],
            "tool_choice": {"type": "function", "function": {"name": "get_weather"}},
            "messages": [{"role": "user", "content": f"{tag} what is the weather in Seoul?"}]}},
        {"custom_id": "responses-text", "operation": "responses", "body": {
            "model": model_name, "max_output_tokens": 64, "input": f"{tag} native batch responses"}},
        {"custom_id": "responses-tool", "operation": "responses", "body": {
            "model": model_name, "max_output_tokens": 64, "tools": [RESPONSES_TOOL],
            "tool_choice": {"type": "function", "name": "get_weather"},
            "input": [{"role": "user", "content": [
                {"type": "input_text", "text": f"{tag} what is the weather in Seoul?"}]}]}},
        {"custom_id": "image-generate", "operation": "images.generations", "body": {
            "model_id": models["image"]["id"], "prompt": f"{tag} a cobalt cube on a white table",
            "size": "1024x1024", "quality": "high", "n": 1}},
        {"custom_id": "image-edit", "operation": "images.edits", "body": {
            "model_id": models["image"]["id"], "prompt": f"{tag} make the frame teal",
            "size": "1024x1024", "quality": "high", "n": 1, "input_asset_id": png_id}},
        {"custom_id": "speech", "operation": "audio.speech", "body": {
            "model_id": models["tts"]["id"], "input": SPEECH_INPUT, "voice": "alloy", "response_format": "wav"}},
        {"custom_id": "transcription", "operation": "audio.transcriptions", "body": {
            "model_id": models["stt"]["id"], "input_asset_id": wav_id}},
    ]


def _assert_native_descriptor(descriptor: dict[str, Any], batch_id: str, metadata: dict[str, str]) -> None:
    assert descriptor["id"] == batch_id
    assert descriptor["metadata"] == metadata
    assert descriptor["links"] == {
        "self": f"/v1/chat/batches/{batch_id}",
        "items": f"/v1/chat/batches/{batch_id}/items",
        "cancel": f"/v1/chat/batches/{batch_id}/cancel",
    }


def _assert_native_items(items: list[dict[str, Any]], submitted: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    assert [item["ordinal"] for item in items] == list(range(1, len(submitted) + 1))
    assert [(item["custom_id"], item["operation"]) for item in items] == [
        (entry["custom_id"], entry["operation"]) for entry in submitted
    ]
    by_id = {item["custom_id"]: item for item in items}
    for item in items:
        assert item["status"] == "completed", item
        assert item["error"] is None
        assert item["settlement_status"] == "settled"
        assert uuid.UUID(item["run_id"])

    chat = by_id["chat-text"]["response"]
    assert chat["object"] == "chat.completion"
    assert chat["choices"][0]["message"]["content"] == FAKE_TEXT
    assert chat["choices"][0]["finish_reason"] == "stop"
    assert (chat["usage"]["prompt_tokens"], chat["usage"]["completion_tokens"]) == (10, 5)

    tool = by_id["chat-tool"]["response"]
    assert tool["object"] == "chat.completion"
    assert tool["choices"][0]["finish_reason"] == "tool_calls"
    calls = tool["choices"][0]["message"]["tool_calls"]
    assert len(calls) == 1
    assert calls[0]["id"] == FAKE_TOOL_CALL_ID and calls[0]["type"] == "function"
    assert calls[0]["function"]["name"] == "get_weather"
    assert json.loads(calls[0]["function"]["arguments"]) == FAKE_TOOL_ARGUMENTS

    responses = by_id["responses-text"]["response"]
    assert responses["object"] == "response" and responses["status"] == "completed"
    texts = [part["text"] for output in responses["output"] if output["type"] == "message"
             for part in output["content"] if part["type"] == "output_text"]
    assert texts == [FAKE_TEXT]

    responses_tool = by_id["responses-tool"]["response"]
    assert responses_tool["object"] == "response" and responses_tool["status"] == "completed"
    function_calls = [output for output in responses_tool["output"] if output["type"] == "function_call"]
    assert len(function_calls) == 1
    assert function_calls[0]["call_id"] == FAKE_TOOL_CALL_ID
    assert function_calls[0]["name"] == "get_weather"
    assert json.loads(function_calls[0]["arguments"]) == FAKE_TOOL_ARGUMENTS

    for custom_id in ("image-generate", "image-edit"):
        data = by_id[custom_id]["response"]["data"]
        assert len(data) == 1 and data[0]["mime_type"] == "image/png"
        assert set(data[0]) >= {"asset_id", "mime_type", "size_bytes"}
    speech = by_id["speech"]["response"]
    assert speech["mime_type"] == "audio/wav" and speech["size_bytes"] > 44
    assert by_id["transcription"]["response"]["text"] == FAKE_TRANSCRIPTION
    return by_id


def _assert_native_ledger(
    ledger: dict[str, Any],
    by_id: dict[str, dict[str, Any]],
    downloads: dict[str, bytes],
    *,
    png_id: str,
    wav_id: str,
) -> None:
    batch = ledger["batch"]
    assert batch.contract == "native"
    assert batch.endpoint is None
    assert batch.status == "completed" and batch.completed_at is not None
    assert (batch.request_total, batch.request_completed, batch.request_failed) == (8, 8, 0)
    assert (batch.request_cancelled, batch.request_expired, batch.request_unknown) == (0, 0, 0)
    assert batch.api_key_id is None
    assert batch.idempotency_key_hash is not None
    assert batch.input_file_id is None and batch.output_file_id is None and batch.error_file_id is None

    assert len(ledger["items"]) == 8
    assert len(ledger["runs"]) == 8, "each item must materialize exactly one durable run"
    expected_cost = {
        "chat-text": TEXT_RAW_COST,
        "chat-tool": TEXT_RAW_COST,
        "responses-text": TEXT_RAW_COST,
        "responses-tool": TEXT_RAW_COST,
        "image-generate": IMAGE_RAW_COST,
        "image-edit": IMAGE_RAW_COST,
        "speech": TTS_USD_PER_CHARACTER * len(SPEECH_INPUT),
        "transcription": STT_RAW_COST_ONE_SECOND,
    }
    for item in ledger["items"]:
        public = by_id[item.custom_id]
        assert item.state == "completed"
        assert item.settlement_status == "settled"
        assert item.error_code is None
        assert item.run_id == public["run_id"]
        assert item.custom_id_hash == hashlib.sha256(item.custom_id.encode()).hexdigest()
        assert item.capability_snapshot and item.pricing_snapshot
        assert ITEM_SCOPES[item.operation] <= set(item.required_scopes)
        assert item.request_ciphertext and item.result_ciphertext
        # Bodies are stored encrypted; prompts must not appear in the ledger as plaintext.
        assert "native batch" not in item.request_ciphertext and "Seoul" not in item.request_ciphertext
        assert FAKE_TEXT not in item.result_ciphertext and FAKE_TRANSCRIPTION not in item.result_ciphertext
        run = ledger["runs"][item.run_id]
        assert run.batch_id == batch.id
        assert run.run_kind == RUN_KINDS[item.operation]
        assert run.client_request_id == str(uuid.uuid5(uuid.UUID(batch.id), item.custom_id))
        _assert_batch_only_worker(ledger, run)
        _assert_settled_once(ledger, run, expected_cost[item.custom_id])

    runs_by_custom = {item.custom_id: ledger["runs"][item.run_id] for item in ledger["items"]}
    items_by_custom = {item.custom_id: item for item in ledger["items"]}
    for custom_id in ("image-generate", "image-edit"):
        reference = by_id[custom_id]["response"]["data"][0]
        _assert_output_asset(
            ledger, runs_by_custom[custom_id], reference, downloads[reference["asset_id"]], "image/png"
        )
    speech = by_id["speech"]["response"]
    _assert_output_asset(ledger, runs_by_custom["speech"], speech, downloads[speech["asset_id"]], "audio/wav")
    _assert_input_pin(ledger, items_by_custom["image-edit"], runs_by_custom["image-edit"], png_id)
    _assert_input_pin(ledger, items_by_custom["transcription"], runs_by_custom["transcription"], wav_id)
    for custom_id in ("chat-text", "chat-tool", "responses-text", "responses-tool", "image-generate", "speech"):
        assert items_by_custom[custom_id].input_asset_id is None


def _native_provider_models(models: dict[str, dict[str, Any]], text_model: str) -> dict[str, str]:
    return {
        "chat.completions": text_model,
        "responses": text_model,
        "images.generations": _api_model_name(models["image"]["model_name"]),
        "images.edits": _api_model_name(models["image"]["model_name"]),
        "audio.speech": _api_model_name(models["tts"]["model_name"]),
        "audio.transcriptions": _api_model_name(models["stt"]["model_name"]),
    }


NATIVE_PROVIDER_CALLS = {
    "chat.completions": 2,
    "responses": 2,
    "images.generations": 1,
    "images.edits": 1,
    "audio.speech": 1,
    "audio.transcriptions": 1,
}


def _poll_native(client: Client, batch_id: str, *, timeout: float = 240.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = client.get_batch(batch_id)
        if last["status"] in TERMINAL_BATCH_STATUSES:
            return last
        time.sleep(0.5)
    raise TimeoutError(f"native batch {batch_id} did not finish within {timeout}s: {last}")


async def _poll_native_async(client: AsyncClient, batch_id: str, *, timeout: float = 240.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = await client.get_batch(batch_id)
        if last["status"] in TERMINAL_BATCH_STATUSES:
            return last
        await asyncio.sleep(0.5)
    raise TimeoutError(f"native batch {batch_id} did not finish within {timeout}s: {last}")


def _assert_final_native_descriptor(final: dict[str, Any], batch_id: str, metadata: dict[str, str]) -> None:
    _assert_native_descriptor(final, batch_id, metadata)
    assert final["status"] == "completed", final
    assert final["errors"] == []
    counts = final["request_counts"]
    assert (counts["total"], counts["completed"], counts["failed"]) == (8, 8, 0)
    assert all(counts[key] == 0 for key in ("pending", "queued", "running", "cancelled", "expired", "unknown"))


def test_native_mixed_batch_sync_sdk_six_operations() -> None:
    from lumen_sdk import Client

    conn = _connection()
    models = asyncio.run(_media_models())
    _reset_fake(conn["fake_provider_url"])
    png, wav = _png_fixture(), _wav_fixture()
    metadata = {"suite": "system", "client": "sync"}

    # The fixture token is project scoped; the SDK sends it via Authorization: Bearer.
    with Client(conn["api_base_url"], OWNER_TOKEN, timeout=60.0) as client:
        png_asset = client.upload_asset(file=("source.png", png, "image/png"))
        wav_asset = client.upload_asset(file=("speech.wav", wav, "audio/wav"))
        _assert_uploaded_asset(png_asset, png, "image/png")
        _assert_uploaded_asset(wav_asset, wav, "audio/wav")

        items = _native_items(conn["model"], models, png_asset["id"], wav_asset["id"], "sync")
        idempotency_key = f"system-native-sync-{uuid.uuid4()}"
        created = client.create_batch(idempotency_key=idempotency_key, items=items,
                                      completion_window="24h", metadata=metadata)
        batch_id = created["id"]
        assert uuid.UUID(batch_id)
        _assert_native_descriptor(created, batch_id, metadata)
        assert created["status"] in {"validating", "in_progress", "finalizing", "completed"}
        assert created["request_counts"]["total"] == 8

        replay = client.create_batch(idempotency_key=idempotency_key, items=items,
                                     completion_window="24h", metadata=metadata)
        assert replay["id"] == batch_id
        with pytest.raises(httpx.HTTPStatusError) as conflict:
            client.create_batch(idempotency_key=idempotency_key, items=items[:1],
                                completion_window="24h", metadata=metadata)
        assert conflict.value.response.status_code == 409

        final = _poll_native(client, batch_id)
        _assert_final_native_descriptor(final, batch_id, metadata)
        assert batch_id in {batch["id"] for batch in client.list_batches(limit=100)["batches"]}

        public_items: list[dict[str, Any]] = []
        cursor: int | None = None
        while True:
            page = client.list_batch_items(batch_id, after=cursor, limit=3)
            assert len(page["items"]) <= 3
            public_items.extend(page["items"])
            cursor = page["next_cursor"]
            if cursor is None:
                break
        by_id = _assert_native_items(public_items, items)

        downloads: dict[str, bytes] = {}
        for custom_id in ("image-generate", "image-edit"):
            asset_id = by_id[custom_id]["response"]["data"][0]["asset_id"]
            downloads[asset_id] = client.download_asset(asset_id)
            _assert_png(downloads[asset_id])
        speech_id = by_id["speech"]["response"]["asset_id"]
        downloads[speech_id] = client.download_asset(speech_id)
        _assert_wav(downloads[speech_id])

    ledger = asyncio.run(_batch_ledger(batch_id))
    _assert_native_ledger(ledger, by_id, downloads, png_id=png_asset["id"], wav_id=wav_asset["id"])
    _assert_provider_calls(_fake_stats(conn["fake_provider_url"]), NATIVE_PROVIDER_CALLS,
                           models=_native_provider_models(models, conn["model"]))


async def test_native_mixed_batch_async_sdk_six_operations() -> None:
    from lumen_sdk import AsyncClient

    conn = _connection()
    models = await _media_models()
    _reset_fake(conn["fake_provider_url"])
    png, wav = _png_fixture(), _wav_fixture()
    metadata = {"suite": "system", "client": "async"}

    async with AsyncClient(conn["api_base_url"], OWNER_TOKEN, timeout=60.0) as client:
        png_asset = await client.upload_asset(file=("source.png", png, "image/png"))
        wav_asset = await client.upload_asset(file=("speech.wav", wav, "audio/wav"))
        _assert_uploaded_asset(png_asset, png, "image/png")
        _assert_uploaded_asset(wav_asset, wav, "audio/wav")

        items = _native_items(conn["model"], models, png_asset["id"], wav_asset["id"], "async")
        created = await client.create_batch(idempotency_key=f"system-native-async-{uuid.uuid4()}", items=items,
                                            completion_window="24h", metadata=metadata)
        batch_id = created["id"]
        _assert_native_descriptor(created, batch_id, metadata)

        final = await _poll_native_async(client, batch_id)
        _assert_final_native_descriptor(final, batch_id, metadata)

        page = await client.list_batch_items(batch_id, limit=1000)
        assert page["next_cursor"] is None
        by_id = _assert_native_items(page["items"], items)

        downloads: dict[str, bytes] = {}
        for custom_id in ("image-generate", "image-edit"):
            asset_id = by_id[custom_id]["response"]["data"][0]["asset_id"]
            downloads[asset_id] = await client.download_asset(asset_id)
            _assert_png(downloads[asset_id])
        speech_id = by_id["speech"]["response"]["asset_id"]
        downloads[speech_id] = await client.download_asset(speech_id)
        _assert_wav(downloads[speech_id])

    ledger = await _batch_ledger(batch_id)
    _assert_native_ledger(ledger, by_id, downloads, png_id=png_asset["id"], wav_id=wav_asset["id"])
    _assert_provider_calls(_fake_stats(conn["fake_provider_url"]), NATIVE_PROVIDER_CALLS,
                           models=_native_provider_models(models, conn["model"]))


# ============================================================================
# OpenAI-compatible Files + Batches (OpenAI Python SDK)
# ============================================================================


@pytest.mark.parametrize(("method", "path", "kwargs"), [
    ("GET", "/files", {}),
    ("POST", "/files", {"files": {"file": ("input.jsonl", b"{}\n")}, "data": {"purpose": "batch"}}),
    ("GET", "/files/file-fixture", {}),
    ("DELETE", "/files/file-fixture", {}),
    ("GET", "/files/file-fixture/content", {}),
    ("GET", "/batches", {}),
    ("POST", "/batches", {"json": {"input_file_id": "file-fixture", "endpoint": "/v1/chat/completions",
                                  "completion_window": "24h"}}),
    ("GET", "/batches/batch_fixture", {}),
    ("POST", "/batches/batch_fixture/cancel", {}),
])
def test_model_key_cannot_access_compat_files_or_batches(method, path, kwargs):
    conn = _connection()
    with httpx.Client(timeout=30.0, trust_env=False) as client:
        response = client.request(method, conn["compat_base_url"] + path,
                                  headers={"Authorization": "Bearer " + conn["api_key"]}, **kwargs)
        assert response.status_code == 403, response.text


def test_native_batch_cancel_releases_unstarted_items():
    from lumen_sdk import Client

    conn = _connection()
    _reset_fake(conn["fake_provider_url"])
    configured = _fake_control(conn["fake_provider_url"], "/_control/configure", {"pause_seconds": 3.0})
    assert configured["state"]["pause_seconds"] == 3.0
    items = [
        {"custom_id": f"slow-{index:02d}", "operation": "chat.completions", "body": {
            "model": conn["model"], "max_tokens": 32,
            "messages": [{"role": "user", "content": f"__TRIGGER_PAUSE__ cancel row {index}"}]}}
        for index in range(24)
    ]
    try:
        with Client(conn["api_base_url"], OWNER_TOKEN, timeout=60.0) as client:
            created = client.create_batch(idempotency_key=f"system-cancel-{uuid.uuid4()}", items=items)
            batch_id = created["id"]
            deadline = time.monotonic() + 120.0
            while client.get_batch(batch_id)["status"] != "in_progress" or not _fake_stats(conn["fake_provider_url"])["calls"]:
                assert time.monotonic() < deadline, "batch never started executing"
                time.sleep(0.2)
            cancelling = client.cancel_batch(batch_id)
            assert cancelling["id"] == batch_id
            assert cancelling["status"] in {"cancelling", "cancelled"}
            final = _poll_native(client, batch_id)
            rows = client.list_batch_items(batch_id, limit=1000)["items"]
    finally:
        _fake_control(conn["fake_provider_url"], "/_control/configure", {"pause_seconds": 2.0})

    assert final["status"] == "cancelled", final
    counts = final["request_counts"]
    assert counts["total"] == 24 and counts["completed"] + counts["cancelled"] == 24
    assert counts["cancelled"] >= 1 and counts["completed"] >= 1
    assert {row["custom_id"] for row in rows} == {item["custom_id"] for item in items}
    for row in rows:
        if row["status"] == "completed":
            assert row["response"]["choices"][0]["message"]["content"] == FAKE_TEXT
        else:
            assert row["status"] == "cancelled" and row["response"] is None

    ledger = asyncio.run(_batch_ledger(batch_id))
    batch = ledger["batch"]
    assert batch.status == "cancelled" and batch.final_target_status == "cancelled"
    assert batch.cancelling_at is not None and batch.cancelled_at is not None
    assert batch.request_completed == counts["completed"] and batch.request_cancelled == counts["cancelled"]
    assert batch.request_unknown == 0
    started = 0
    for item in ledger["items"]:
        if item.state == "completed":
            run = ledger["runs"][item.run_id]
            _assert_batch_only_worker(ledger, run)
            _assert_settled_once(ledger, run, TEXT_RAW_COST)
            started += 1
        elif item.run_id is not None:
            run = ledger["runs"][item.run_id]
            assert run.status == "canceled"
            assert ledger["by_run"][run.id]["reservations"] == []
            assert ledger["by_run"][run.id]["ledgers"] == []
    for run_id, rows_by_kind in ledger["by_run"].items():
        assert all(hold.status != "reserved" for hold in rows_by_kind["reservations"]), run_id
    holds = sum(len(entry["reservations"]) for entry in ledger["by_run"].values())
    assert holds == started
    _assert_provider_calls(_fake_stats(conn["fake_provider_url"]), {"chat.completions": started},
                           models={"chat.completions": conn["model"]})


# ============================================================================
# Isolation: no public egress; official media hosts terminate at the fake over test TLS
# ============================================================================


def test_batch_stack_has_no_public_egress_and_media_hosts_resolve_to_fake() -> None:
    routes = Path("/proc/net/route").read_text().splitlines()[1:]
    default_routes = [line for line in routes if line.split() and line.split()[1] == "00000000"]
    assert default_routes == [], "test container must have no default route"
    for address in (("1.1.1.1", 443), ("8.8.8.8", 53), ("9.9.9.9", 443)):
        with pytest.raises(OSError):
            socket.create_connection(address, timeout=3).close()

    def addresses(host: str, port: int) -> set[str]:
        return {info[4][0] for info in socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)}

    assert addresses(MEDIA_HOST, 443) == addresses(TEXT_HOST, 443)

    bundle = os.environ.get("SSL_CERT_FILE")
    assert bundle, "system tests must trust only the generated test CA bundle"
    assert Path(bundle).read_text().count("BEGIN CERTIFICATE") == 1
    context = ssl.create_default_context(cafile=bundle)
    with socket.create_connection((MEDIA_HOST, 443), timeout=5) as raw, context.wrap_socket(
        raw, server_hostname=MEDIA_HOST
    ) as tls:
        names = {value for kind, value in tls.getpeercert().get("subjectAltName", ()) if kind == "DNS"}
    assert MEDIA_HOST in names

    public_trust = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    public_trust.load_verify_locations(certifi.where())
    with socket.create_connection((MEDIA_HOST, 443), timeout=5) as raw, pytest.raises(ssl.SSLCertVerificationError):
        public_trust.wrap_socket(raw, server_hostname=MEDIA_HOST).close()
