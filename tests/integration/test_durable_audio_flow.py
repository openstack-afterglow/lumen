"""Transaction-level speech and transcription accounting and no-replay checks."""

from __future__ import annotations

import base64
import io
import json
import os
import uuid
import wave
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select

from lumen.crypto import decrypt_chat_content
from lumen.db import close_db, get_session_factory, init_db
from lumen.models.chat_assets import ChatAsset, ChatRunAsset
from lumen.models.chat_batches import ChatBatch, ChatBatchItem
from lumen.models.chat_db import ChatUsageLog, LlmModel, LlmProvider
from lumen.models.chat_runs import ChatModelCallReservation, ChatRun, ChatRunSegment
from lumen.services import assets, credit
from lumen.services.durable_runs import audio, queries
from lumen.services.durable_runs.common import _fingerprint
from lumen.services.durable_runs.errors import DurableRunConflict, DurableRunInputError
from lumen.services.infrastructure.store import register_worker
from lumen.services.providers import routing
from lumen.services.providers.errors import ProviderValidationError
from lumen.services.run_store import claim_queued_run, load_segment_payload
from lumen.services.worker_routing import use_read_committed

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("current_project_authority")]


async def _claim(session, run_id: str, owner: str):
    """Claim through a fresh fixed online_media registration, as a real worker must."""
    registration_id = await register_worker(worker_identity=owner, boot_id=str(uuid.uuid4()), capacity=4,
                                            protocol_versions=[1, 2], plugin_digest="0" * 64, schema_version=1,
                                            workload_classes=["online_media"])
    await use_read_committed(session)
    return await claim_queued_run(session, run_id, owner=owner, registration_id=registration_id)


def _wave() -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(8000)
        writer.writeframes(b"\x00\x00" * 8000)
    return output.getvalue()


async def test_audio_durable_scanned_assets_idempotency_and_unknown(monkeypatch):
    nonce = uuid.uuid4().hex
    user_id, project_id = f"audio-user-{nonce}", f"audio-project-{nonce}"
    init_db(os.environ["DATABASE_URL"], pool_size=1, max_overflow=0)
    factory = get_session_factory()
    assert factory is not None
    raw = _wave()
    calls: list[str] = []
    monkeypatch.setattr(routing, "resolve_api_key", lambda _provider: "fixture-provider-key")
    monkeypatch.setattr(assets, "asset_pipeline_available", lambda: True)
    async def no_wake(_run_id):
        return None
    monkeypatch.setattr(audio, "wake_run", no_wake)
    try:
        async with factory() as session, session.begin():
            provider = LlmProvider(name=f"audio-provider-{nonce}", provider_type="openai", is_active=True,
                                   margin_multiplier=Decimal("2"))
            session.add(provider)
            await session.flush()
            session.add_all([
                LlmModel(provider_id=provider.id, model_name="tts-1", model_kind="tts",
                         media_pricing={"audio_per_second": "0.001"}, is_active=True),
                LlmModel(provider_id=provider.id, model_name="whisper-1", model_kind="stt",
                         media_pricing={"audio_per_minute": "0.06"}, is_active=True),
            ])
            await session.flush()
            ids = (await session.execute(select(LlmModel.id).where(LlmModel.provider_id == provider.id)
                                           .order_by(LlmModel.id))).scalars().all()
            source_id = str(uuid.uuid4())
            session.add(ChatAsset(id=source_id, project_id=project_id, user_id=user_id,
                object_key=f"audio-test/{nonce}/{source_id}", bucket_name="audio-test",
                original_name="voice.wav", mime_type="audio/wav", size_bytes=len(raw),
                sha256="0" * 64, status="clean", media_metadata={"duration_ms": 1000}))
        async def stored_output(*, data, original_name, media_type, user_id, project_id, run_id):
            assert data == raw and media_type == "audio/wav"
            asset_id = str(uuid.uuid4())
            async with factory() as session, session.begin():
                session.add(ChatAsset(id=asset_id, project_id=project_id, user_id=user_id,
                    object_key=f"audio-test/{nonce}/{asset_id}", bucket_name="audio-test",
                    original_name=original_name, mime_type=media_type, size_bytes=len(data),
                    sha256="0" * 64, status="clean", media_metadata={"duration_ms": 1000}))
                session.add(ChatRunAsset(run_id=run_id, asset_id=asset_id, purpose="output"))
            return {"id": asset_id, "mime_type": media_type, "size_bytes": len(data),
                    "media_metadata": {"duration_ms": 1000}}
        monkeypatch.setattr(assets, "create_generated_asset_bytes", stored_output)
        async def open_owned(*, asset_id, user_id, project_id):
            row = await assets.get_asset(asset_id=asset_id, user_id=user_id, project_id=project_id)
            assert row["status"] == "clean"
            return assets.AssetDownload(body=io.BytesIO(raw), name=row["name"],
                                        mime_type=row["mime_type"], size_bytes=row["size_bytes"])
        monkeypatch.setattr(assets, "open_download", open_owned)
        async def speech(_route, *, text, voice, format):
            calls.append("tts")
            assert text == "hello" and voice == "alloy" and format == "wav"
            return audio.audio_transport.SpeechResult(raw, "audio/wav")
        async def transcription(_route, *, data, mime_type, language, prompt):
            calls.append("stt")
            assert data == raw and mime_type == "audio/wav" and language == "en" and prompt is None
            return audio.audio_transport.TranscriptionResult("recognized actual words")
        monkeypatch.setattr(audio.audio_transport, "generate_speech", speech)
        monkeypatch.setattr(audio.audio_transport, "transcribe_audio", transcription)

        tts_request = {"kind": "tts", "model_id": str(ids[0]), "input": "hello", "voice": "alloy", "response_format": "wav"}
        prepared = await audio.prepare_audio_run(
            {"model_id": str(ids[1]), "input_asset_id": source_id, "language": "en"},
            operation="audio.transcriptions", project_id=project_id, user_id=user_id)
        assert prepared.source_asset_id == source_id
        assert prepared.required_scopes == ("native:audio:write", "native:assets:read")
        rolled_back_id = None
        with pytest.raises(RuntimeError, match="caller rollback"):
            async with factory() as session, session.begin():
                batch = ChatBatch(id=str(uuid.uuid4()), user_id=user_id, project_id=project_id,
                    contract="native", status="in_progress", request_fingerprint="0" * 64,
                    idempotency_key_hash="1" * 64)
                session.add(batch)
                await session.flush()
                session.add(ChatBatchItem(batch_id=batch.id, ordinal=1, custom_id="stt", custom_id_hash="2" * 64,
                    operation="audio.transcriptions", request_ciphertext="sealed", input_asset_id=source_id,
                    state="pending"))
                # Deleted after acceptance: new reuse is refused, the accepted Batch pin survives.
                pinned_source = await session.get(ChatAsset, source_id)
                pinned_source.status = "deleting"
                await session.flush()
                async with session.begin_nested():
                    with pytest.raises(DurableRunInputError, match="source media is unavailable"):
                        await audio.persist_audio_run_in_transaction(
                            session, prepared, project_id=project_id, user_id=user_id,
                            client_request_id=str(uuid.uuid4()))
                staged = await audio.persist_audio_run_in_transaction(
                    session, prepared, project_id=project_id, user_id=user_id,
                    client_request_id=str(uuid.uuid4()), workload_class="batch", batch_id=batch.id)
                rolled_back_id = staged.id
                assert staged.workload_class == "batch" and staged.worker_pool_id is None
                await session.flush()
                binding = (await session.execute(select(ChatRunAsset).where(
                    ChatRunAsset.run_id == staged.id))).scalar_one()
                assert binding.asset_id == source_id and binding.purpose == "input"
                assert (await session.execute(select(ChatModelCallReservation).where(
                    ChatModelCallReservation.run_id == staged.id))).scalar_one_or_none() is None
                raise RuntimeError("caller rollback")
        async with factory() as session:
            assert await session.get(ChatRun, rolled_back_id) is None
        assert calls == []
        key = str(uuid.uuid4())
        first = await audio.admit_audio_run(tts_request, user_id=user_id, project_id=project_id, client_request_id=key)
        repeated = await audio.admit_audio_run(tts_request, user_id=user_id, project_id=project_id, client_request_id=key)
        assert first.run_id == repeated.run_id
        with pytest.raises(DurableRunConflict, match="idempotency_key_reused"):
            await audio.admit_audio_run({**tts_request, "input": "different"}, user_id=user_id,
                                        project_id=project_id, client_request_id=key)
        async with factory() as session, session.begin():
            claimed = await _claim(session, first.run_id, "audio-worker")
            assert claimed is not None
            owner = claimed.lease_owner
            capability, frozen = dict(claimed.capability_snapshot), dict(claimed.pricing_snapshot)
        assert await audio.execute_audio_run(first.run_id, owner=owner,
            payload={**tts_request, "user_id": user_id, "project_id": project_id},
            capability_snapshot=capability, pricing_snapshot=frozen)
        result = await audio.audio_result(first.run_id, user_id=user_id, project_id=project_id)
        assert result["kind"] == "tts" and result["mime_type"] == "audio/wav"
        opened = await assets.open_download(asset_id=result["asset_id"], user_id=user_id, project_id=project_id)
        assert b"".join([chunk async for chunk in opened.chunks()]) == raw
        projection = await queries.owned_run_response(run_id=first.run_id, user_id=user_id, project_id=project_id)
        assert projection.status == "completed" and projection.output_assets[0]["asset_id"] == result["asset_id"]
        async with factory() as session:
            usage = (await session.execute(select(ChatUsageLog).where(ChatUsageLog.run_id == first.run_id))).scalars().all()
            assert len(usage) == 1 and usage[0].raw_cost == Decimal("0.001")
            hold = (await session.execute(select(ChatModelCallReservation).where(
                ChatModelCallReservation.run_id == first.run_id))).scalar_one()
            assert hold.status == "settled" and hold.actual_credits == usage[0].credited_cost

        stt_request = {"kind": "stt", "model_id": str(ids[1]), "input_asset_id": source_id, "language": "en"}
        second = await audio.admit_audio_run(stt_request, user_id=user_id, project_id=project_id,
                                             client_request_id=str(uuid.uuid4()))
        async with factory() as session, session.begin():
            claimed = await _claim(session, second.run_id, "audio-worker")
            assert claimed is not None
            owner = claimed.lease_owner
            capability, frozen = dict(claimed.capability_snapshot), dict(claimed.pricing_snapshot)
        assert await audio.execute_audio_run(second.run_id, owner=owner,
            payload={**stt_request, "user_id": user_id, "project_id": project_id},
            capability_snapshot=capability, pricing_snapshot=frozen)
        assert await audio.audio_result(second.run_id, user_id=user_id, project_id=project_id) == {
            "kind": "stt", "text": "recognized actual words"}
        assert calls == ["tts", "stt"]
        with pytest.raises(assets.AssetError, match="asset forbidden"):
            await audio.admit_audio_run(stt_request, user_id=user_id, project_id=project_id + "-other",
                                        client_request_id=str(uuid.uuid4()))

        third = await audio.admit_audio_run({**tts_request, "input": "unknown"}, user_id=user_id,
                                            project_id=project_id, client_request_id=str(uuid.uuid4()))
        async with factory() as session, session.begin():
            claimed = await _claim(session, third.run_id, "audio-worker")
            assert claimed is not None
            owner = claimed.lease_owner
            capability, frozen = dict(claimed.capability_snapshot), dict(claimed.pricing_snapshot)
        bound = credit.credits_for_cost(Decimal("1.8"), Decimal("2"), Decimal(frozen["credit_per_usd"]))
        assert await audio._start(third.run_id, owner=owner, bound=bound, kind="tts") == "started"
        assert await audio.execute_audio_run(third.run_id, owner=owner,
            payload={**tts_request, "input": "unknown", "user_id": user_id, "project_id": project_id},
            capability_snapshot=capability, pricing_snapshot=frozen)
        assert calls == ["tts", "stt"]
        async with factory() as session:
            run = (await session.execute(select(ChatRun).where(ChatRun.id == third.run_id))).scalar_one()
            segment = (await session.execute(select(ChatRunSegment).where(ChatRunSegment.run_id == third.run_id))).scalar_one()
            hold = (await session.execute(select(ChatModelCallReservation).where(
                ChatModelCallReservation.run_id == third.run_id))).scalar_one()
            assert run.status == "failed" and segment.status == "failed"
            assert hold.status == "unknown" and hold.bound_credits == bound and run.reserved_credits == bound
            assert (await session.execute(select(ChatUsageLog).where(ChatUsageLog.run_id == third.run_id))).scalars().all() == []

        fourth = await audio.admit_audio_run({**tts_request, "input": "cancel"}, user_id=user_id,
                                             project_id=project_id, client_request_id=str(uuid.uuid4()))
        from lumen.services.durable_runs.lifecycle import request_cancelled
        cancelled = await request_cancelled(run_id=fourth.run_id, project_id=project_id, user_id=user_id)
        assert cancelled.status == "canceled" and calls == ["tts", "stt"]
        async with factory() as session:
            assert (await session.execute(select(ChatModelCallReservation).where(
                ChatModelCallReservation.run_id == fourth.run_id))).scalar_one_or_none() is None
    finally:
        await close_db()


@pytest.mark.parametrize("case", ["characters", "openai_tokens", "gemini_tokens", "missing", "invalid", "over_envelope", "asset_failure"])
async def test_audio_explicit_billing_checkpoint_ledger_and_unknown_hold(monkeypatch, case):
    nonce = uuid.uuid4().hex
    user_id, project_id = f"audio-bill-user-{nonce}", f"audio-bill-project-{nonce}"
    init_db(os.environ["DATABASE_URL"], pool_size=1, max_overflow=0)
    factory = get_session_factory()
    raw = _wave()
    kind = "tts" if case in {"characters", "asset_failure"} else "stt"
    provider_type = "gemini" if case in {"gemini_tokens", "asset_failure"} else "openai"
    model_name = ("gemini-2.5-flash-preview-tts" if kind == "tts" and provider_type == "gemini" else
                  "tts-1" if kind == "tts" else "gemini-2.5-flash" if provider_type == "gemini" else "gpt-4o-transcribe")
    envelope = "0.001" if case == "over_envelope" else "0.1"
    media = ({"billing_basis": "characters", "audio_per_character": "0.001", "audio_output_per_second": "99"}
        if case == "characters" else {"billing_basis": "tokens", "reservation_usd": envelope,
            f"audio_{'output' if kind == 'tts' else 'input'}_per_second": "99",
            "token_rates": {"audio": {"input_per_million": "10", "cache_read_per_million": "1", "output_per_million": "20"}}})
    monkeypatch.setattr(routing, "resolve_api_key", lambda _provider: "fixture-provider-key")
    monkeypatch.setattr(assets, "asset_pipeline_available", lambda: True)
    async def no_wake(_run_id):
        return None
    monkeypatch.setattr(audio, "wake_run", no_wake)
    calls = []
    try:
        async with factory() as session, session.begin():
            provider = LlmProvider(name=f"audio-bill-provider-{nonce}", provider_type=provider_type,
                is_active=True, margin_multiplier=Decimal("2"))
            session.add(provider)
            await session.flush()
            model = LlmModel(provider_id=provider.id, model_name=model_name, model_kind=kind, media_pricing=media,
                input_price=Decimal("0.000002"), output_price=Decimal("0.000004"), cache_read_price=Decimal("0.0000005"), is_active=True)
            session.add(model)
            await session.flush()
            model_id = model.id
            source_id = str(uuid.uuid4())
            session.add(ChatAsset(id=source_id, project_id=project_id, user_id=user_id,
                object_key=f"audio-test/{nonce}/{source_id}", bucket_name="audio-test", original_name="voice.wav",
                mime_type="audio/wav", size_bytes=len(raw), sha256="0" * 64, status="clean", media_metadata={"duration_ms": 1000}))
        async def open_owned(*, asset_id, user_id, project_id):
            row = await assets.get_asset(asset_id=asset_id, user_id=user_id, project_id=project_id)
            return assets.AssetDownload(body=io.BytesIO(raw), name=row["name"], mime_type=row["mime_type"], size_bytes=row["size_bytes"])
        monkeypatch.setattr(assets, "open_download", open_owned)
        async def stored_output(*, data, original_name, media_type, user_id, project_id, run_id):
            if case == "asset_failure":
                raise RuntimeError("generated audio storage failed")
            asset_id = str(uuid.uuid4())
            async with factory() as session, session.begin():
                session.add(ChatAsset(id=asset_id, project_id=project_id, user_id=user_id,
                    object_key=f"audio-test/{nonce}/{asset_id}", bucket_name="audio-test", original_name=original_name,
                    mime_type=media_type, size_bytes=len(data), sha256="0" * 64, status="clean", media_metadata={"duration_ms": 1000}))
                session.add(ChatRunAsset(run_id=run_id, asset_id=asset_id, purpose="output"))
            return {"id": asset_id, "mime_type": media_type, "size_bytes": len(data), "media_metadata": {"duration_ms": 1000}}
        monkeypatch.setattr(assets, "create_generated_asset_bytes", stored_output)
        text = "  Hi 👋é  "
        request = ({"kind": "tts", "model_id": str(model_id), "input": text,
                    "voice": "Kore" if provider_type == "gemini" else "alloy", "response_format": "wav"}
            if kind == "tts" else {"kind": "stt", "model_id": str(model_id), "input_asset_id": source_id})
        admitted = await audio.admit_audio_run(request, user_id=user_id, project_id=project_id, client_request_id=str(uuid.uuid4()))
        async with factory() as session, session.begin():
            claimed = await _claim(session, admitted.run_id, "audio-bill-worker")
            owner = claimed.lease_owner
            capability, frozen = dict(claimed.capability_snapshot), dict(claimed.pricing_snapshot)
        async def handle(http_request):
            calls.append(http_request)
            async with factory() as session:
                hold = (await session.execute(select(ChatModelCallReservation).where(
                    ChatModelCallReservation.run_id == admitted.run_id))).scalar_one()
                expected = Decimal("0.009") if case == "characters" else Decimal(envelope)
                assert hold.status == "reserved"
                assert hold.bound_credits == credit.credits_for_cost(expected, Decimal(2), Decimal(frozen["credit_per_usd"]))
            if kind == "tts":
                if provider_type == "openai":
                    return httpx.Response(200, content=raw)
                usage = {"total_input_tokens": 200, "total_output_tokens": 800, "total_cached_tokens": 100,
                    "total_thought_tokens": 0, "input_tokens_by_modality": [{"modality": "text", "tokens": 200}],
                    "output_tokens_by_modality": [{"modality": "audio", "tokens": 800}],
                    "cached_tokens_by_modality": [{"modality": "text", "tokens": 100}]}
                return httpx.Response(200, json={"status": "completed", "usage": usage,
                    "steps": [{"type": "model_output", "content": [{"type": "audio", "mime_type": "audio/wav",
                        "data": base64.b64encode(raw).decode()}]}]})
            if provider_type == "gemini":
                usage = {"total_input_tokens": 1000, "total_output_tokens": 50, "total_cached_tokens": 400, "total_thought_tokens": 10,
                    "input_tokens_by_modality": [{"modality": "text", "tokens": 200}, {"modality": "audio", "tokens": 800}],
                    "output_tokens_by_modality": [{"modality": "text", "tokens": 50}],
                    "cached_tokens_by_modality": [{"modality": "text", "tokens": 100}, {"modality": "audio", "tokens": 300}]}
                return httpx.Response(200, json={"status": "completed", "steps": [{"type": "model_output", "content": [{"type": "text", "text": "heard"}]}], "usage": usage})
            usage = {"type": "tokens", "input_tokens": 1000, "output_tokens": 50, "total_tokens": 1050,
                "input_token_details": {"audio_tokens": 800 if case != "invalid" else 1100, "text_tokens": 200}}
            return httpx.Response(200, json={"text": "heard", **({} if case == "missing" else {"usage": usage})})
        client = httpx.AsyncClient
        monkeypatch.setattr(audio.audio_transport.httpx, "AsyncClient", lambda **kwargs: client(**{**kwargs, "transport": httpx.MockTransport(handle)}))
        # Keep the real route/config resolver fence, but simulate changed current
        # effective prices. Admission's rates must remain the only charge source.
        resolve = routing.resolve_model_snapshot
        async def changed_current_rates(snapshot):
            route = await resolve(snapshot)
            return {**route, "input_price_per_token": None, "output_price_per_token": None,
                "cache_read_price_per_token": Decimal(999), "media_pricing": {**media, "audio_per_character": "999", "reservation_usd": "999"}}
        monkeypatch.setattr(routing, "resolve_model_snapshot", changed_current_rates)
        await audio.execute_audio_run(admitted.run_id, owner=owner,
            payload={**request, "user_id": user_id, "project_id": project_id}, capability_snapshot=capability, pricing_snapshot=frozen)
        async with factory() as session:
            run = (await session.execute(select(ChatRun).where(ChatRun.id == admitted.run_id))).scalar_one()
            segment = (await session.execute(select(ChatRunSegment).where(ChatRunSegment.run_id == admitted.run_id))).scalar_one()
            hold = (await session.execute(select(ChatModelCallReservation).where(ChatModelCallReservation.run_id == admitted.run_id))).scalar_one()
            rows = (await session.execute(select(ChatUsageLog).where(ChatUsageLog.run_id == admitted.run_id))).scalars().all()
            checkpoint = load_segment_payload(segment.usage_payload)
            assert len(calls) == 1
            if case != "asset_failure":
                assert checkpoint["duration_ms"] == 1000
            if case in {"missing", "invalid", "over_envelope", "asset_failure"}:
                assert run.status == "failed"
                assert run.usage_reconciled_at is None
                assert hold.status == "unknown" and hold.actual_credits is None
                assert hold.bound_credits == run.reserved_credits
                assert rows == []
                if case == "over_envelope":
                    assert checkpoint["tokens"]["modality_tokens"]["audio"]["input_tokens"] == 800
                if case == "asset_failure":
                    assert segment.status == "failed"
                    assert checkpoint["tokens"]["prompt_tokens"] == 200
                    assert checkpoint["tokens"]["cache_read_input_tokens"] == 100
                    assert checkpoint["tokens"]["modality_tokens"]["audio"]["output_tokens"] == 800
            else:
                expected_cost = Decimal("0.009") if kind == "tts" else Decimal("0.00579") if provider_type == "gemini" else Decimal("0.0086")
                assert run.status == "completed" and hold.status == "settled"
                assert len(rows) == 1 and rows[0].raw_cost == expected_cost
                assert hold.actual_credits == rows[0].credited_cost == credit.credits_for_cost(expected_cost, Decimal(2), Decimal(frozen["credit_per_usd"]))
                if kind == "tts":
                    assert checkpoint["input_characters"] == 9
                    assert rows[0].prompt_tokens == rows[0].completion_tokens == 0
                    assert hold.bound_credits == hold.actual_credits
                else:
                    expected_audio = {"input_tokens": 800, "cache_read_input_tokens": 300 if provider_type == "gemini" else 0}
                    if provider_type == "gemini":
                        expected_audio["output_tokens"] = 0
                    assert checkpoint["tokens"]["modality_tokens"]["audio"] == expected_audio
                    assert rows[0].prompt_tokens == 1000 and rows[0].completion_tokens == (60 if provider_type == "gemini" else 50)
                    assert rows[0].cache_read_input_tokens == (400 if provider_type == "gemini" else 0)
    finally:
        await close_db()


async def test_timed_transcription_intent_checkpoint_projection_and_invalid_timing(monkeypatch):
    nonce = uuid.uuid4().hex
    user_id, project_id = f"audio-timed-user-{nonce}", f"audio-timed-project-{nonce}"
    init_db(os.environ["DATABASE_URL"], pool_size=1, max_overflow=0)
    factory = get_session_factory()
    raw = _wave()
    calls: list[bytes] = []
    monkeypatch.setattr(routing, "resolve_api_key", lambda _provider: "fixture-provider-key")
    monkeypatch.setattr(assets, "asset_pipeline_available", lambda: True)
    async def no_wake(_run_id):
        return None
    monkeypatch.setattr(audio, "wake_run", no_wake)
    async def open_owned(*, asset_id, user_id, project_id):
        row = await assets.get_asset(asset_id=asset_id, user_id=user_id, project_id=project_id)
        return assets.AssetDownload(body=io.BytesIO(raw), name=row["name"], mime_type=row["mime_type"], size_bytes=row["size_bytes"])
    monkeypatch.setattr(assets, "open_download", open_owned)

    def handle(request):
        calls.append(request.content)
        if b"verbose_json" not in request.content:
            return httpx.Response(200, json={"text": "plain words"})
        overlap = b"overlap" in request.content
        return httpx.Response(200, json={"task": "transcribe", "language": "english", "duration": 1.0,
            "text": "Timed words.", "usage": {"type": "duration", "seconds": 1}, "segments": [
                {"id": 0, "start": 0.0, "end": 0.6, "text": " Timed", "tokens": [1, 2], "avg_logprob": -0.1},
                {"id": 1, "start": 0.5 if overlap else 0.6, "end": 1.0, "text": " words.", "tokens": [3]},
            ]})
    client = httpx.AsyncClient
    monkeypatch.setattr(audio.audio_transport.httpx, "AsyncClient",
                        lambda **kwargs: client(**{**kwargs, "transport": httpx.MockTransport(handle)}))

    async def run_row(run_id):
        async with factory() as session:
            return (await session.execute(select(ChatRun).where(ChatRun.id == run_id))).scalar_one()

    async def execute(run_id):
        async with factory() as session, session.begin():
            claimed = await _claim(session, run_id, "audio-timed-worker")
            assert claimed is not None
            owner = claimed.lease_owner
            capability, frozen = dict(claimed.capability_snapshot), dict(claimed.pricing_snapshot)
            # Execute from the persisted encrypted intent, not the caller's dict.
            payload = json.loads(decrypt_chat_content(claimed.request_payload))
        assert await audio.execute_audio_run(run_id, owner=owner,
            payload={**payload, "user_id": user_id, "project_id": project_id},
            capability_snapshot=capability, pricing_snapshot=frozen)

    try:
        async with factory() as session, session.begin():
            provider = LlmProvider(name=f"audio-timed-provider-{nonce}", provider_type="openai", is_active=True,
                                   margin_multiplier=Decimal("2"))
            session.add(provider)
            await session.flush()
            whisper = LlmModel(provider_id=provider.id, model_name="whisper-1", model_kind="stt",
                               media_pricing={"audio_per_minute": "0.06"}, is_active=True)
            untimed = LlmModel(provider_id=provider.id, model_name="gpt-4o-transcribe", model_kind="stt",
                               media_pricing={"audio_per_minute": "0.06"}, is_active=True)
            session.add_all([whisper, untimed])
            await session.flush()
            whisper_id, untimed_id = whisper.id, untimed.id
            source_id = str(uuid.uuid4())
            session.add(ChatAsset(id=source_id, project_id=project_id, user_id=user_id,
                object_key=f"audio-test/{nonce}/{source_id}", bucket_name="audio-test", original_name="voice.wav",
                mime_type="audio/wav", size_bytes=len(raw), sha256="0" * 64, status="clean", media_metadata={"duration_ms": 1000}))

        plain = {"kind": "stt", "model_id": str(whisper_id), "input_asset_id": source_id, "language": "en"}
        timed = {**plain, "timestamp_granularities": ["segment"]}
        plain_key, timed_key = str(uuid.uuid4()), str(uuid.uuid4())
        first = await audio.admit_audio_run({**plain, "timestamp_granularities": []}, user_id=user_id,
                                            project_id=project_id, client_request_id=plain_key)
        assert (await audio.admit_audio_run(plain, user_id=user_id, project_id=project_id,
                                            client_request_id=plain_key)).run_id == first.run_id
        legacy = {"kind": "stt", "model_id": str(whisper_id), "provider_id": None, "input_asset_id": source_id,
                  "language": "en", "prompt": None}
        plain_row = await run_row(first.run_id)
        assert plain_row.request_fingerprint == _fingerprint(legacy)
        assert "timestamp_granularities" not in json.loads(decrypt_chat_content(plain_row.request_payload))
        with pytest.raises(DurableRunConflict, match="idempotency_key_reused"):
            await audio.admit_audio_run(timed, user_id=user_id, project_id=project_id, client_request_id=plain_key)

        second = await audio.admit_audio_run(timed, user_id=user_id, project_id=project_id, client_request_id=timed_key)
        assert (await audio.admit_audio_run(timed, user_id=user_id, project_id=project_id,
                                            client_request_id=timed_key)).run_id == second.run_id
        with pytest.raises(DurableRunConflict, match="idempotency_key_reused"):
            await audio.admit_audio_run(plain, user_id=user_id, project_id=project_id, client_request_id=timed_key)
        assert (await run_row(second.run_id)).request_fingerprint == _fingerprint({**legacy, "timestamp_granularities": ["segment"]})

        rejected_key = str(uuid.uuid4())
        with pytest.raises(ProviderValidationError, match="unsupported"):
            await audio.admit_audio_run({**timed, "model_id": str(untimed_id)}, user_id=user_id,
                                        project_id=project_id, client_request_id=rejected_key)
        async with factory() as session:
            assert (await session.execute(select(ChatRun).where(ChatRun.project_id == project_id,
                ChatRun.client_request_id == rejected_key))).scalar_one_or_none() is None
        assert calls == []

        await execute(second.run_id)
        assert len(calls) == 1
        assert b'name="response_format"\r\n\r\nverbose_json\r\n' in calls[0]
        assert b'name="timestamp_granularities[]"\r\n\r\nsegment\r\n' in calls[0]
        expected = [{"start": 0.0, "end": 0.6, "text": " Timed"}, {"start": 0.6, "end": 1.0, "text": " words."}]
        assert await audio.audio_result(second.run_id, user_id=user_id, project_id=project_id) == {
            "kind": "stt", "text": "Timed words.", "segments": expected}
        async with factory() as session:
            segment = (await session.execute(select(ChatRunSegment).where(ChatRunSegment.run_id == second.run_id))).scalar_one()
            assert load_segment_payload(segment.result_payload) == {"text": "Timed words.", "segments": expected}
            rows = (await session.execute(select(ChatUsageLog).where(ChatUsageLog.run_id == second.run_id))).scalars().all()
            hold = (await session.execute(select(ChatModelCallReservation).where(
                ChatModelCallReservation.run_id == second.run_id))).scalar_one()
            assert len(rows) == 1 and rows[0].raw_cost == Decimal("0.001")
            assert hold.status == "settled" and hold.actual_credits == rows[0].credited_cost

        await execute(first.run_id)
        assert len(calls) == 2
        assert b'name="response_format"\r\n\r\njson\r\n' in calls[1] and b"timestamp_granularities" not in calls[1]
        assert await audio.audio_result(first.run_id, user_id=user_id, project_id=project_id) == {
            "kind": "stt", "text": "plain words"}

        third = await audio.admit_audio_run({**timed, "prompt": "overlap"}, user_id=user_id, project_id=project_id,
                                            client_request_id=str(uuid.uuid4()))
        await execute(third.run_id)
        assert len(calls) == 3
        run = await run_row(third.run_id)
        assert run.status == "failed" and run.usage_reconciled_at is None
        async with factory() as session:
            hold = (await session.execute(select(ChatModelCallReservation).where(
                ChatModelCallReservation.run_id == third.run_id))).scalar_one()
            assert hold.status == "unknown" and hold.actual_credits is None
            assert (await session.execute(select(ChatUsageLog).where(ChatUsageLog.run_id == third.run_id))).scalars().all() == []
    finally:
        await close_db()
