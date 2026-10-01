"""Audio HTTP responses, owned streaming, validation, and upload replay protection."""

from __future__ import annotations

import asyncio
import hashlib
from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from lumen.api import audio as native
from lumen.api.compat import audio as compat
from lumen.auth import get_principal
from lumen.services.providers.errors import ProviderValidationError


def _principal():
    return {
        "auth_type": "api_key", "user_id": "user-1", "project_id": "project-1", "api_key_id": 4,
        "source": "api", "scopes": ("native:audio:write", "native:assets:read", "native:assets:write",
                              "compat:audio:write"),
    }


def _client():
    app = FastAPI()
    app.include_router(native.router, prefix="/v1")
    app.include_router(compat.router, prefix="/v1")
    app.dependency_overrides[get_principal] = _principal
    return TestClient(app), app


def test_native_speech_waits_for_completion_and_streams_owned_canonical_bytes(monkeypatch):
    audio_bytes = b"RIFF" + b"\x00" * 4 + b"WAVE" + b"\x01" * 96
    statuses = iter(("queued", "running", "completed"))
    owner_checks = []
    key = uuid4()

    async def admit_audio_run(payload, **owner):
        assert payload == {"model_id": "voice-model", "provider_id": None, "input": "hello", "voice": "alloy",
                           "response_format": "wav", "kind": "tts"}
        assert owner["client_request_id"] == str(key)
        return SimpleNamespace(run_id="run-1")

    async def owned_run_response(**owner):
        owner_checks.append(owner)
        return SimpleNamespace(status=next(statuses), last_seq=5)

    async def audio_result(**owner):
        assert owner == {"run_id": "run-1", "project_id": "project-1", "user_id": "user-1"}
        return {"kind": "tts", "asset_id": "asset-1", "size_bytes": len(audio_bytes), "mime_type": "audio/wav"}

    async def open_download(**owner):
        assert owner == {"asset_id": "asset-1", "user_id": "user-1", "project_id": "project-1"}

        async def chunks():
            yield audio_bytes[:12]
            yield audio_bytes[12:]

        return SimpleNamespace(chunks=chunks, mime_type="audio/wav", size_bytes=len(audio_bytes))

    monkeypatch.setattr(native, "admit_audio_run", admit_audio_run)
    monkeypatch.setattr(native.queries, "owned_run_response", owned_run_response)
    monkeypatch.setattr(native, "audio_result", audio_result)
    monkeypatch.setattr(native.assets, "open_download", open_download)
    monkeypatch.setattr(native, "_POLL_INTERVAL_SECONDS", 0)
    client, app = _client()
    response = client.post("/v1/chat/audio/speech", headers={"Idempotency-Key": str(key)},
                           json={"model_id": "voice-model", "input": "hello", "voice": "alloy", "response_format": "wav"})
    assert response.status_code == 200
    assert response.content == audio_bytes
    assert response.headers["content-type"] == "audio/wav"
    assert response.headers["content-length"] == str(len(audio_bytes))
    assert len(owner_checks) == 3
    assert all(owner["project_id"] == "project-1" and owner["user_id"] == "user-1" for owner in owner_checks)

    invalid_key = client.post("/v1/chat/audio/speech", headers={"Idempotency-Key": "not-a-uuid"},
                              json={"model_id": "voice-model", "input": "hello", "voice": "alloy"})
    assert invalid_key.status_code == 422
    app.dependency_overrides[get_principal] = lambda: {**_principal(), "scopes": ("compat:audio:write",)}
    forbidden = client.post("/v1/chat/audio/speech", headers={"Idempotency-Key": str(key)},
                            json={"model_id": "voice-model", "input": "hello", "voice": "alloy"})
    assert forbidden.status_code == 403


def test_native_transcription_returns_text_and_rejects_invalid_source_before_admission(monkeypatch):
    asset_id = uuid4()
    admitted = []

    async def admit_audio_run(payload, **owner):
        admitted.append(payload)
        return SimpleNamespace(run_id="run-stt")

    async def owned_run_response(**owner):
        return SimpleNamespace(status="completed")

    async def audio_result(**owner):
        return {"kind": "stt", "text": "Actual recognized words."}

    monkeypatch.setattr(native, "admit_audio_run", admit_audio_run)
    monkeypatch.setattr(native.queries, "owned_run_response", owned_run_response)
    monkeypatch.setattr(native, "audio_result", audio_result)
    client, _ = _client()
    key = str(uuid4())
    response = client.post("/v1/chat/audio/transcriptions", headers={"Idempotency-Key": key},
                           json={"model_id": "stt-model", "input_asset_id": str(asset_id), "language": "en"})
    assert response.status_code == 200
    assert response.json() == {"text": "Actual recognized words."}
    assert admitted[0]["input_asset_id"] == str(asset_id)
    bad = client.post("/v1/chat/audio/transcriptions", headers={"Idempotency-Key": key},
                      json={"model_id": "stt-model", "input_asset_id": "not-a-uuid"})
    assert bad.status_code == 422
    assert len(admitted) == 1


def test_native_timed_transcription_admits_explicit_timing_and_projects_segments(monkeypatch):
    asset_id = str(uuid4())
    admitted = []
    segments = [{"start": 0.0, "end": 1.25, "text": " Actual"}, {"start": 1.25, "end": 2.5, "text": " words."}]

    async def admit_audio_run(payload, **owner):
        if payload["model_id"] == "gpt-4o-transcribe":
            raise ProviderValidationError("transcription timestamps are unsupported for this audio model")
        admitted.append(payload)
        return SimpleNamespace(run_id="run-timed")

    async def owned_run_response(**owner):
        return SimpleNamespace(status="completed")

    async def audio_result(**owner):
        return {"kind": "stt", "text": "Actual words.", "segments": segments}

    monkeypatch.setattr(native, "admit_audio_run", admit_audio_run)
    monkeypatch.setattr(native.queries, "owned_run_response", owned_run_response)
    monkeypatch.setattr(native, "audio_result", audio_result)
    client, _ = _client()
    url = "/v1/chat/audio/transcriptions"
    body = {"model_id": "whisper-1", "input_asset_id": asset_id, "timestamp_granularities": ["segment"]}
    response = client.post(url, headers={"Idempotency-Key": str(uuid4())}, json=body)
    assert response.status_code == 200
    assert response.json() == {"text": "Actual words.", "segments": segments}
    assert admitted[0]["timestamp_granularities"] == ["segment"] and admitted[0]["kind"] == "stt"
    for invalid in (["segment", "segment"], ["word"], "segment", [None]):
        rejected = client.post(url, headers={"Idempotency-Key": str(uuid4())},
                               json={**body, "timestamp_granularities": invalid})
        assert rejected.status_code == 422
    unsupported = client.post(url, headers={"Idempotency-Key": str(uuid4())},
                              json={**body, "model_id": "gpt-4o-transcribe"})
    assert unsupported.status_code == 422 and "unsupported" in unsupported.json()["detail"]
    assert len(admitted) == 1


def test_compat_speech_returns_sdk_media_and_rejects_unsupported_options(monkeypatch):
    audio_bytes = b"ID3" + b"\x00" * 100
    calls = []

    async def admit_audio_run(payload, **owner):
        calls.append(payload)
        return SimpleNamespace(run_id="run-tts")

    async def owned_run_response(**owner):
        return SimpleNamespace(status="completed")

    async def audio_result(**owner):
        return {"kind": "tts", "asset_id": "out", "size_bytes": len(audio_bytes), "mime_type": "audio/mpeg"}

    async def open_download(**owner):
        async def chunks():
            yield audio_bytes

        return SimpleNamespace(chunks=chunks, mime_type="audio/mpeg", size_bytes=len(audio_bytes))

    monkeypatch.setattr(native, "admit_audio_run", admit_audio_run)
    monkeypatch.setattr(native.queries, "owned_run_response", owned_run_response)
    monkeypatch.setattr(native, "audio_result", audio_result)
    monkeypatch.setattr(native.assets, "open_download", open_download)
    client, _ = _client()
    response = client.post("/v1/audio/speech", json={"model": "tts-model", "input": "hello", "voice": "alloy"})
    assert response.status_code == 200
    assert response.content == audio_bytes
    assert response.headers["content-type"] == "audio/mpeg"
    for option in ({"speed": 1.2}, {"response_format": "opus"}, {"stream_format": "sse"}, {"voice": " "}):
        bad = client.post("/v1/audio/speech", json={"model": "tts-model", "input": "hello", "voice": "alloy", **option})
        assert bad.status_code == 400
        assert "error" in bad.json()
    assert len(calls) == 1


def test_compat_transcription_scans_upload_and_returns_json_text(monkeypatch):
    audio_bytes = b"RIFF" + b"\x00" * 4 + b"WAVE" + b"\x01" * 96
    source_id = str(uuid4())
    scans = []

    async def create_uploaded_asset(*, path, original_name, user_id, project_id):
        assert path.read_bytes() == audio_bytes
        assert (user_id, project_id, original_name) == ("user-1", "project-1", "clip.wav")
        scans.append(source_id)
        return {"id": source_id}

    async def admit_audio_run(payload, **owner):
        assert payload["kind"] == "stt" and payload["input_asset_id"] == source_id
        return SimpleNamespace(run_id="run-transcription")

    async def owned_run_response(**owner):
        return SimpleNamespace(status="completed")

    async def audio_result(**owner):
        return {"kind": "stt", "text": "Recorded text, not a placeholder."}

    monkeypatch.setattr(native.assets, "create_uploaded_asset", create_uploaded_asset)
    monkeypatch.setattr(native, "admit_audio_run", admit_audio_run)
    monkeypatch.setattr(native.queries, "owned_run_response", owned_run_response)
    monkeypatch.setattr(native, "audio_result", audio_result)
    client, _ = _client()
    url = "/v1/audio/transcriptions"
    bad = client.post(url, data={"model": "stt-model", "response_format": "verbose_json"},
                      files={"file": ("clip.wav", audio_bytes, "audio/wav")})
    assert bad.status_code == 400
    assert not scans
    response = client.post(url, data={"model": "stt-model", "language": "en"},
                           files={"file": ("clip.wav", audio_bytes, "audio/wav")})
    assert response.status_code == 200
    assert response.json() == {"text": "Recorded text, not a placeholder."}
    text = client.post(url, data={"model": "stt-model", "response_format": "text"},
                       files={"file": ("clip.wav", audio_bytes, "audio/wav")})
    assert text.status_code == 200 and text.text == "Recorded text, not a placeholder."
    assert text.headers["content-type"].startswith("text/plain")
    assert scans == [source_id, source_id]


def test_replayed_source_rejects_changed_bytes(monkeypatch, tmp_path):

    original = b"RIFF" + b"\x00" * 4 + b"WAVE" + b"\x01" * 96
    path = tmp_path / "reupload.wav"
    key = uuid4()
    saved = SimpleNamespace(id="asset-1", sha256=hashlib.sha256(original).hexdigest(), size_bytes=len(original))

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def execute(self, statement):
            return SimpleNamespace(scalar_one_or_none=lambda: saved)

    monkeypatch.setattr(compat, "get_session_factory", lambda: Session)
    path.write_bytes(original)
    assert asyncio.run(compat._replayed_source(path, key, _principal())) == saved.id
    path.write_bytes(original[:-1] + b"x")
    try:
        asyncio.run(compat._replayed_source(path, key, _principal()))
        assert False, "different source bytes must conflict"
    except HTTPException as exc:
        assert exc.status_code == 409
        assert exc.detail == "idempotency_key_reused_with_different_audio"
