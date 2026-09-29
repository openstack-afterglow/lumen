"""Transaction-level speech and transcription accounting and no-replay checks."""

from __future__ import annotations

import io
import os
import uuid
import wave
from decimal import Decimal

import pytest
from sqlalchemy import select

from lumen.db import close_db, get_session_factory, init_db
from lumen.models.chat_assets import ChatAsset, ChatRunAsset
from lumen.models.chat_db import ChatUsageLog, LlmModel, LlmProvider
from lumen.models.chat_runs import ChatModelCallReservation, ChatRun, ChatRunSegment
from lumen.services import assets, credit
from lumen.services.durable_runs import audio, queries
from lumen.services.durable_runs.errors import DurableRunConflict
from lumen.services.providers import routing
from lumen.services.run_store import claim_queued_run

pytestmark = pytest.mark.integration


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
            return raw, "audio/wav"
        async def transcription(_route, *, data, mime_type, language, prompt):
            calls.append("stt")
            assert data == raw and mime_type == "audio/wav" and language == "en" and prompt is None
            return "recognized actual words"
        monkeypatch.setattr(audio.audio_transport, "generate_speech", speech)
        monkeypatch.setattr(audio.audio_transport, "transcribe_audio", transcription)

        tts_request = {"kind": "tts", "model_id": str(ids[0]), "input": "hello", "voice": "alloy", "response_format": "wav"}
        key = str(uuid.uuid4())
        first = await audio.admit_audio_run(tts_request, user_id=user_id, project_id=project_id, client_request_id=key)
        repeated = await audio.admit_audio_run(tts_request, user_id=user_id, project_id=project_id, client_request_id=key)
        assert first.run_id == repeated.run_id
        with pytest.raises(DurableRunConflict, match="idempotency_key_reused"):
            await audio.admit_audio_run({**tts_request, "input": "different"}, user_id=user_id,
                                        project_id=project_id, client_request_id=key)
        async with factory() as session, session.begin():
            claimed = await claim_queued_run(session, first.run_id, owner="audio-worker")
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
            claimed = await claim_queued_run(session, second.run_id, owner="audio-worker")
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
            claimed = await claim_queued_run(session, third.run_id, owner="audio-worker")
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
