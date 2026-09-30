"""MariaDB/Redis realtime reservation, exact PCM settlement and unknown recovery."""

from __future__ import annotations

import os
import uuid
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from lumen.db import close_db, get_session_factory, init_db
from lumen.models.chat_db import ChatUsageLog, LlmModel, LlmProvider
from lumen.models.chat_runs import ChatModelCallReservation, ChatRun, ChatRunSegment
from lumen.services import credit
from lumen.services.durable_runs import realtime
from lumen.services.durable_runs.common import _now
from lumen.services.durable_runs.execution import _finish
from lumen.services.durable_runs.lifecycle import recover_stale_runs, request_cancelled
from lumen.services.providers import routing
from lumen.services.providers.realtime_protocol import AudioMeter
from lumen.services.run_store import load_segment_payload

pytestmark = pytest.mark.integration


async def test_realtime_durable_exact_pcm_usage_and_crash_unknown(monkeypatch):
    nonce = uuid.uuid4().hex
    user_id, project_id = f"realtime-user-{nonce}", f"realtime-project-{nonce}"
    init_db(os.environ["DATABASE_URL"], pool_size=1, max_overflow=0)
    factory = get_session_factory()
    assert factory is not None
    monkeypatch.setattr(routing, "resolve_api_key", lambda _provider: "fixture-provider-key")
    try:
        async with factory() as session, session.begin():
            provider = LlmProvider(name=f"realtime-provider-{nonce}", provider_type="openai",
                                   is_active=True, margin_multiplier=Decimal("2"))
            session.add(provider)
            await session.flush()
            model = LlmModel(provider_id=provider.id, model_name="gpt-realtime", model_kind="realtime",
                             media_pricing={"realtime_input_per_minute": "0.012",
                                            "realtime_output_per_minute": "0.024"}, is_active=True)
            session.add(model)
            await session.flush()
            model_id = model.id
        request = {"model_id": str(model_id), "max_duration_seconds": 10}
        key = str(uuid.uuid4())
        admitted = await realtime.admit_realtime_session(request, project_id=project_id, user_id=user_id,
                                                          client_request_id=key)
        assert admitted["status"] == "ready" and admitted["provider_type"] == "openai"
        replay = await realtime.admit_realtime_session(request, project_id=project_id, user_id=user_id,
                                                        client_request_id=key)
        assert replay["session_id"] == admitted["session_id"]
        from lumen.services.durable_runs.errors import DurableRunConflict
        with pytest.raises(DurableRunConflict, match="idempotency_key_reused"):
            await realtime.admit_realtime_session({**request, "voice": "nova"}, project_id=project_id,
                                                   user_id=user_id, client_request_id=key)
        from lumen.services.durable_runs.errors import DurableRunNotFound
        with pytest.raises(DurableRunNotFound):
            await realtime.consume_ticket(replay["session_id"], admitted["connect_token"])
        assert await realtime.consume_ticket(replay["session_id"], replay["connect_token"]) == (user_id, project_id)
        with pytest.raises(DurableRunNotFound):
            await realtime.consume_ticket(replay["session_id"], replay["connect_token"])
        run_id = replay["session_id"]
        _, prices, payload, owner = await realtime._start(run_id, owner="voice-test", user_id=user_id,
                                                           project_id=project_id)
        assert prices["max_duration_seconds"] == 10 and payload["voice"] == "alloy"
        bound = Decimal(prices["bound_credits"])
        original_quota = credit.quota_policy.get_system_quota
        async def limited_quota(_session, _user_id):
            return SimpleNamespace(monthly=bound + bound / 2, weekly=Decimal(0))
        monkeypatch.setattr(credit.quota_policy, "get_system_quota", limited_quota)
        competing = await realtime.admit_realtime_session(request, project_id=project_id, user_id=user_id,
                                                            client_request_id=str(uuid.uuid4()))
        with pytest.raises(credit.QuotaExceeded):
            await realtime._start(competing["session_id"], owner="competing-voice",
                                  user_id=user_id, project_id=project_id)
        async with factory() as session:
            assert (await session.execute(select(ChatModelCallReservation).where(
                ChatModelCallReservation.run_id == competing["session_id"]))).scalar_one_or_none() is None
        monkeypatch.setattr(credit.quota_policy, "get_system_quota", original_quota)
        assert (await request_cancelled(run_id=competing["session_id"], project_id=project_id,
                                        user_id=user_id)).status == "canceled"
        seconds = await realtime._settle(run_id, owner=owner,
            meter=AudioMeter(input_bytes=48000, output_bytes=96000,
                             input_sample_rate_hz=24000, output_sample_rate_hz=24000))
        assert seconds == ("1", "2")
        await _finish(run_id, status="completed", message_id=None, owner=owner)
        async with factory() as session:
            rows = (await session.execute(select(ChatUsageLog).where(ChatUsageLog.run_id == run_id))).scalars().all()
            assert len(rows) == 1 and rows[0].raw_cost == Decimal("0.001")
            hold = (await session.execute(select(ChatModelCallReservation).where(
                ChatModelCallReservation.run_id == run_id))).scalar_one()
            assert hold.status == "settled" and hold.actual_credits == rows[0].credited_cost
            run = (await session.execute(select(ChatRun).where(ChatRun.id == run_id))).scalar_one()
            assert run.status == "completed" and run.usage_reconciled_at is not None
            segment = (await session.execute(select(ChatRunSegment).where(ChatRunSegment.run_id == run_id))).scalar_one()
            assert segment.status == "completed"
            assert load_segment_payload(segment.usage_payload)["input_bytes"] == 48000
            assert "fixture-provider-key" not in str(segment.usage_payload)

        unknown = await realtime.admit_realtime_session(request, project_id=project_id, user_id=user_id,
                                                        client_request_id=str(uuid.uuid4()))
        _, _, _, owner = await realtime._start(unknown["session_id"], owner="voice-crashed",
                                                user_id=user_id, project_id=project_id)
        async with factory() as session, session.begin():
            run = (await session.execute(select(ChatRun).where(ChatRun.id == unknown["session_id"])
                 .with_for_update())).scalar_one()
            assert run.lease_owner == owner
            run.lease_expires_at = _now() - timedelta(seconds=1)
        await recover_stale_runs(owner="voice-recovery")
        async with factory() as session:
            run = (await session.execute(select(ChatRun).where(ChatRun.id == unknown["session_id"]))).scalar_one()
            hold = (await session.execute(select(ChatModelCallReservation).where(
                ChatModelCallReservation.run_id == run.id))).scalar_one()
            assert run.status == "failed" and hold.status == "unknown"
            assert (await session.execute(select(ChatUsageLog).where(ChatUsageLog.run_id == run.id))).scalars().all() == []
    finally:
        await close_db()
