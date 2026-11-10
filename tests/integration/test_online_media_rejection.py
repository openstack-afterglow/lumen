"""An online media run whose provider refusal is proven to precede inference settles at zero.

Only Batch used to release the hold; an online refusal (a safety 400, an unknown model, a rate limit)
returned HTTP 500 and pinned the whole reservation ``unknown`` until an administrator resolved it. Any
other failure keeps the uncertain-result accounting.
"""

from __future__ import annotations

import json
import os
import uuid
from decimal import Decimal

import pytest
from provider_fixtures import configured_provider
from sqlalchemy import select

from lumen.crypto import decrypt_chat_content
from lumen.db import close_db, get_session_factory, init_db
from lumen.models.chat_db import LlmModel
from lumen.models.chat_runs import ChatModelCallReservation, ChatRun, ChatRunEventRow, ChatRunSegment
from lumen.services import assets
from lumen.services.durable_runs import images
from lumen.services.infrastructure.store import register_worker
from lumen.services.providers import image_transport
from lumen.services.run_store import claim_queued_run
from lumen.services.worker_routing import use_read_committed

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("current_project_authority")]


@pytest.mark.parametrize("status, run_error, hold_status", [
    (400, "provider_rejected", "settled"),
    (429, "provider_rejected", "settled"),
    (404, "provider_rejected", "settled"),
    (500, "provider_result_unknown", "unknown"),
    (None, "provider_result_unknown", "unknown"),
])
async def test_online_image_refusal_settles_at_zero_only_when_proven_to_precede_inference(
        monkeypatch, status, run_error, hold_status):
    init_db(os.environ["DATABASE_URL"], pool_size=2, max_overflow=0)
    factory = get_session_factory()
    assert factory is not None
    nonce = uuid.uuid4().hex
    user_id, project_id = f"refusal-user-{nonce}", f"refusal-project-{nonce}"
    monkeypatch.setattr(assets, "asset_pipeline_available", lambda: True)

    async def no_wake(_run_id):
        return None

    monkeypatch.setattr(images, "wake_run", no_wake)
    calls = []

    async def refuse(*_args, **_kwargs):
        calls.append(status)
        raise image_transport.ImageTransportError("upstream refused", status_code=status)

    monkeypatch.setattr(image_transport, "generate_images", refuse)
    try:
        async with factory() as session, session.begin():
            provider = configured_provider(name=f"refusal-provider-{nonce}", provider_type="openai",
                                           is_active=True, margin_multiplier=Decimal("1"))
            session.add(provider)
            await session.flush()
            model = LlmModel(provider_id=provider.id, model_name="gpt-image-1.5", model_kind="image",
                             media_pricing={"image_variants": {"1024x1024:high": "0.04"}}, is_active=True)
            session.add(model)
            await session.flush()
            model_id = model.id
        request = {"model_id": str(model_id), "prompt": "A cobalt cube", "size": "1024x1024", "quality": "high", "n": 1}
        admitted = await images.admit_image_run(request, project_id=project_id, user_id=user_id,
                                                client_request_id=str(uuid.uuid4()))
        async with factory() as session, session.begin():
            registration = await register_worker(worker_identity="refusal-worker", boot_id=str(uuid.uuid4()),
                                                 capacity=4, protocol_versions=[1, 2], plugin_digest="0" * 64,
                                                 schema_version=1, workload_classes=["online_media"])
            await use_read_committed(session)
            claimed = await claim_queued_run(session, admitted.run_id, owner="refusal-worker",
                                             registration_id=registration)
            assert claimed is not None
            owner = claimed.lease_owner
            capability, frozen = dict(claimed.capability_snapshot), dict(claimed.pricing_snapshot)
        assert await images.execute_image_run(admitted.run_id, owner=owner,
            payload={**request, "user_id": user_id, "project_id": project_id},
            capability_snapshot=capability, pricing_snapshot=frozen)
        assert calls == [status]  # exactly one provider call, never replayed
        async with factory() as session:
            run = await session.get(ChatRun, admitted.run_id)
            hold = await session.get(ChatModelCallReservation, (admitted.run_id, "image:1"))
            segment = await session.get(ChatRunSegment, (admitted.run_id, "image:1"))
            terminal = (await session.execute(select(ChatRunEventRow).where(
                ChatRunEventRow.run_id == admitted.run_id, ChatRunEventRow.event_type == "run.failed"))).scalar_one()
        payload = json.loads(decrypt_chat_content(terminal.payload))
        assert run.status == "failed" and payload["error_code"] == run_error
        assert hold.status == hold_status
        if hold_status == "settled":
            assert hold.actual_credits == 0 and run.usage_reconciled_at is not None and segment.status == "failed"
            assert payload["safe_message"] == f"upstream provider rejected the request (HTTP {status})"
        else:
            assert hold.actual_credits is None
    finally:
        await close_db()


@pytest.mark.parametrize("status, run_error, hold_status", [
    (400, "provider_rejected", "settled"),
    (500, "provider_result_unknown", "unknown"),
])
async def test_online_tts_refusal_follows_the_same_accounting(monkeypatch, status, run_error, hold_status):
    from lumen.services.durable_runs import audio
    from lumen.services.providers import audio_transport

    init_db(os.environ["DATABASE_URL"], pool_size=2, max_overflow=0)
    factory = get_session_factory()
    assert factory is not None
    nonce = uuid.uuid4().hex
    user_id, project_id = f"tts-refusal-user-{nonce}", f"tts-refusal-project-{nonce}"
    monkeypatch.setattr(assets, "asset_pipeline_available", lambda: True)

    async def no_wake(_run_id):
        return None

    monkeypatch.setattr(audio, "wake_run", no_wake)
    calls = []

    async def refuse(*_args, **_kwargs):
        calls.append(status)
        raise audio_transport.AudioTransportError("upstream refused", status_code=status)

    monkeypatch.setattr(audio_transport, "generate_speech", refuse)
    try:
        async with factory() as session, session.begin():
            provider = configured_provider(name=f"tts-refusal-provider-{nonce}", provider_type="openai",
                                           is_active=True, margin_multiplier=Decimal("1"))
            session.add(provider)
            await session.flush()
            model = LlmModel(provider_id=provider.id, model_name="tts-1", model_kind="tts",
                             media_pricing={"audio_per_second": "0.001"}, is_active=True)
            session.add(model)
            await session.flush()
            model_id = model.id
        request = {"kind": "tts", "model_id": str(model_id), "input": "hello", "voice": "alloy",
                   "response_format": "wav"}
        admitted = await audio.admit_audio_run(request, user_id=user_id, project_id=project_id,
                                               client_request_id=str(uuid.uuid4()))
        async with factory() as session, session.begin():
            registration = await register_worker(worker_identity="tts-refusal-worker", boot_id=str(uuid.uuid4()),
                                                 capacity=4, protocol_versions=[1, 2], plugin_digest="0" * 64,
                                                 schema_version=1, workload_classes=["online_media"])
            await use_read_committed(session)
            claimed = await claim_queued_run(session, admitted.run_id, owner="tts-refusal-worker",
                                             registration_id=registration)
            assert claimed is not None
            owner = claimed.lease_owner
            capability, frozen = dict(claimed.capability_snapshot), dict(claimed.pricing_snapshot)
        assert await audio.execute_audio_run(admitted.run_id, owner=owner,
            payload={**request, "user_id": user_id, "project_id": project_id},
            capability_snapshot=capability, pricing_snapshot=frozen)
        assert calls == [status]
        async with factory() as session:
            run = await session.get(ChatRun, admitted.run_id)
            hold = await session.get(ChatModelCallReservation, (admitted.run_id, "tts:1"))
            terminal = (await session.execute(select(ChatRunEventRow).where(
                ChatRunEventRow.run_id == admitted.run_id, ChatRunEventRow.event_type == "run.failed"))).scalar_one()
        assert run.status == "failed" and json.loads(decrypt_chat_content(terminal.payload))["error_code"] == run_error
        assert hold.status == hold_status
        assert (hold.actual_credits == 0) is (hold_status == "settled")
    finally:
        await close_db()
