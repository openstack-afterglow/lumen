"""MariaDB/Redis realtime reservation, exact PCM/token settlement and unknown recovery."""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select, update

from lumen.db import close_db, get_session_factory, init_db
from lumen.models.chat_db import ChatUsageLog, LlmModel, LlmProvider
from lumen.models.chat_runs import ChatModelCallReservation, ChatRun, ChatRunSegment
from lumen.services import credit
from lumen.services.durable_runs import realtime
from lumen.services.durable_runs.common import _now
from lumen.services.durable_runs.errors import DurableRunProviderResultUnknown
from lumen.services.durable_runs.execution import _finish
from lumen.services.durable_runs.lifecycle import recover_stale_runs, request_cancelled
from lumen.services.infrastructure.store import register_worker
from lumen.services.providers import routing
from lumen.services.providers.realtime_protocol import AudioMeter
from lumen.services.run_store import load_segment_payload
from lumen.services.usage_breakdown import UsageBreakdown

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("legacy_snapshot", [False, True])
async def test_realtime_durable_exact_pcm_usage_and_crash_unknown(monkeypatch, legacy_snapshot):
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
        if legacy_snapshot:
            async with factory() as session, session.begin():
                run = (await session.execute(select(ChatRun).where(ChatRun.id == run_id).with_for_update())).scalar_one()
                old = {key: run.pricing_snapshot[key] for key in (
                    "max_duration_seconds", "bound_credits", "margin_multiplier", "credit_per_usd")}
                run.pricing_snapshot = {**old, "input_per_minute": "0.012", "output_per_minute": "0.024"}
        capability, prices, payload, owner = await realtime._start(run_id, owner="voice-test", user_id=user_id,
                                                                  project_id=project_id)
        executable_route = await routing.resolve_model_snapshot(capability)
        assert realtime._plan_matches(prices, realtime.realtime_transport.validate_realtime_request(executable_route))
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
        assert seconds == {"input_audio_seconds": "1", "output_audio_seconds": "2"}
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
        # Recovery reconciles any stale run; a real recovering worker identity is required.
        recovery_registration = await register_worker(
            worker_identity="voice-recovery", boot_id=str(uuid.uuid4()), capacity=1, protocol_versions=[1, 2],
            plugin_digest="0" * 64, schema_version=1, workload_classes=["online_text"])
        await recover_stale_runs(owner="voice-recovery", registration_id=recovery_registration)
        async with factory() as session:
            run = (await session.execute(select(ChatRun).where(ChatRun.id == unknown["session_id"]))).scalar_one()
            hold = (await session.execute(select(ChatModelCallReservation).where(
                ChatModelCallReservation.run_id == run.id))).scalar_one()
            assert run.status == "failed" and hold.status == "unknown"
            assert (await session.execute(select(ChatUsageLog).where(ChatUsageLog.run_id == run.id))).scalars().all() == []
    finally:
        await close_db()


def _realtime_usage(*, text_in, audio_in, cached_text, cached_audio, text_out, audio_out):
    return UsageBreakdown.from_openai_media({
        "input_tokens": text_in + audio_in, "output_tokens": text_out + audio_out,
        "input_token_details": {"text_tokens": text_in, "audio_tokens": audio_in, "image_tokens": 0,
                                "cached_tokens": cached_text + cached_audio, "cached_tokens_details": {
                                    "text_tokens": cached_text, "audio_tokens": cached_audio, "image_tokens": 0}},
        "output_token_details": {"text_tokens": text_out, "audio_tokens": audio_out}})


async def test_realtime_token_usage_settles_once_or_keeps_checkpointed_unknown_hold(monkeypatch):
    nonce = uuid.uuid4().hex
    user_id, project_id = f"realtime-token-user-{nonce}", f"realtime-token-project-{nonce}"
    init_db(os.environ["DATABASE_URL"], pool_size=1, max_overflow=0)
    factory = get_session_factory()
    assert factory is not None
    monkeypatch.setattr(routing, "resolve_api_key", lambda _provider: "fixture-provider-key")
    usage = (_realtime_usage(text_in=200, audio_in=800, cached_text=100, cached_audio=300, text_out=100, audio_out=400)
             + _realtime_usage(text_in=100, audio_in=500, cached_text=0, cached_audio=0, text_out=50, audio_out=250))
    meter = AudioMeter(input_bytes=4800, output_bytes=4800, input_sample_rate_hz=24000,
                       output_sample_rate_hz=24000, usage=usage, usage_state="complete")
    try:
        async with factory() as session, session.begin():
            provider = LlmProvider(name=f"realtime-token-provider-{nonce}", provider_type="openai",
                                   is_active=True, margin_multiplier=Decimal("1"))
            session.add(provider)
            await session.flush()
            model_ids = {}
            for name, envelope in (("gpt-realtime", "0.5"), ("gpt-realtime-mini", "0.05")):
                model = LlmModel(provider_id=provider.id, model_name=name, model_kind="realtime",
                                 input_price=Decimal("0.000004"), output_price=Decimal("0.000016"),
                                 cache_read_price=Decimal("0.0000004"), is_active=True, media_pricing={
                                     "billing_basis": "tokens", "reservation_usd": envelope, "token_rates": {"audio": {
                                         "input_per_million": "32", "cache_read_per_million": "0.4",
                                         "output_per_million": "64"}}})
                session.add(model)
                await session.flush()
                model_ids[envelope] = model.id

        async def started(envelope):
            admitted = await realtime.admit_realtime_session(
                {"model_id": str(model_ids[envelope]), "max_duration_seconds": 60}, project_id=project_id,
                user_id=user_id, client_request_id=str(uuid.uuid4()))
            _, prices, _, owner = await realtime._start(admitted["session_id"], owner=f"voice-{envelope}",
                                                         user_id=user_id, project_id=project_id)
            assert prices["billing_basis"] == "tokens" and prices["reservation_usd"] == envelope
            return admitted["session_id"], owner

        run_id, owner = await started("0.5")
        # Later administrator price edits never reach an admitted session's settlement.
        async with factory() as session, session.begin():
            model = (await session.execute(select(LlmModel).where(LlmModel.id == model_ids["0.5"]))).scalar_one()
            model.input_price, model.output_price, model.cache_read_price = (
                Decimal("0.001"), Decimal("0.001"), Decimal("0.001"))
            model.media_pricing = {**model.media_pricing, "token_rates": {"audio": {
                "input_per_million": "999", "cache_read_per_million": "999", "output_per_million": "999"}}}
        assert await realtime._settle(run_id, owner=owner, meter=meter) == {
            "input_tokens": "1600", "output_tokens": "800"}
        # A duplicate terminal settlement is a no-op against the same hold and ledger event.
        assert await realtime._settle(run_id, owner=owner, meter=meter) == {}
        await _finish(run_id, status="completed", message_id=None, owner=owner)
        async with factory() as session:
            rows = (await session.execute(select(ChatUsageLog).where(ChatUsageLog.run_id == run_id))).scalars().all()
            assert len(rows) == 1 and rows[0].raw_cost == Decimal("0.07696")
            assert (rows[0].prompt_tokens, rows[0].completion_tokens) == (1600, 800)
            hold = (await session.execute(select(ChatModelCallReservation).where(
                ChatModelCallReservation.run_id == run_id))).scalar_one()
            assert hold.status == "settled" and hold.actual_credits == rows[0].credited_cost

        unknown_cases = (
            (meter, "exceeds"),
            (AudioMeter(4800, 4800, 24000, 24000, usage=None, usage_state="complete"), "missing"),
            (AudioMeter(4800, 4800, 24000, 24000, usage=usage, usage_state="incomplete"), "incomplete"),
        )
        for uncertain_meter, reason in unknown_cases:
            unknown_id, unknown_owner = await started("0.05")
            with pytest.raises(DurableRunProviderResultUnknown, match=reason):
                await realtime._settle(unknown_id, owner=unknown_owner, meter=uncertain_meter)
            await _finish(unknown_id, status="failed", message_id=None, owner=unknown_owner,
                          error_code="provider_result_unknown", safe_message="provider usage is uncertain")
            async with factory() as session:
                assert (await session.execute(select(ChatUsageLog).where(
                    ChatUsageLog.run_id == unknown_id))).scalars().all() == []
                hold = (await session.execute(select(ChatModelCallReservation).where(
                    ChatModelCallReservation.run_id == unknown_id))).scalar_one()
                segment = (await session.execute(select(ChatRunSegment).where(
                    ChatRunSegment.run_id == unknown_id))).scalar_one()
                assert hold.status == "unknown" and segment.status == "failed"
                # Known provider counts survive even when final usage is missing or over-envelope.
                checkpoint = load_segment_payload(segment.usage_payload)
                assert checkpoint["usage_state"] == uncertain_meter.usage_state
                if uncertain_meter.usage is None:
                    assert checkpoint["usage"] is None
                else:
                    assert UsageBreakdown.from_canonical(checkpoint["usage"]) == uncertain_meter.usage
    finally:
        await close_db()


async def test_session_deadline_closes_and_settles_connected_time_once(monkeypatch):
    from lumen.services.providers import realtime_protocol

    nonce = uuid.uuid4().hex
    user_id, project_id = f"session-user-{nonce}", f"session-project-{nonce}"
    init_db(os.environ["DATABASE_URL"], pool_size=1, max_overflow=0)
    factory = get_session_factory()
    assert factory is not None
    monkeypatch.setattr(routing, "resolve_api_key", lambda _provider: "fixture-provider-key")

    class Upstream:
        def __init__(self):
            self.queue = asyncio.Queue()
            self.sent = []
            self.transport = SimpleNamespace(abort=self.abort)

        def abort(self):
            self.queue.put_nowait(RuntimeError("connection aborted"))

        async def send(self, message):
            self.sent.append(message)

        async def recv(self):
            item = await self.queue.get()
            if isinstance(item, Exception):
                raise item
            return item

    upstream = Upstream()

    class Connection:
        closed = False

        async def __aenter__(self):
            return upstream

        async def __aexit__(self, *_):
            self.closed = True

    class Browser:
        def __init__(self):
            self.queue = asyncio.Queue()
            self.events = []

        async def receive_text(self):
            return await self.queue.get()

        async def send_json(self, event):
            self.events.append(event)

    connection = Connection()
    monkeypatch.setattr(realtime_protocol.websockets, "connect", lambda *_args, **_kwargs: connection)
    try:
        async with factory() as session, session.begin():
            provider = LlmProvider(name=f"session-provider-{nonce}", provider_type="openai",
                                   is_active=True, margin_multiplier=Decimal("1"))
            session.add(provider)
            await session.flush()
            model = LlmModel(provider_id=provider.id, model_name="gpt-realtime", model_kind="realtime", is_active=True,
                             media_pricing={"billing_basis": "session", "realtime_session_per_hour": "3.6"})
            session.add(model)
            await session.flush()
            model_id = model.id
        admitted = await realtime.admit_realtime_session({"model_id": str(model_id), "max_duration_seconds": 10},
            project_id=project_id, user_id=user_id, client_request_id=str(uuid.uuid4()))
        run_id = admitted["session_id"]
        await realtime.consume_ticket(run_id, admitted["connect_token"])
        capability, prices, payload, owner = await realtime._start(run_id, owner="session-deadline",
                                                                   user_id=user_id, project_id=project_id)
        assert Decimal(prices["reservation_usd"]) == Decimal("0.01")
        route = await routing.resolve_model_snapshot(capability)

        async def not_canceled():
            return False

        # No client close or provider event: the relay's own deadline must end the session.
        meter = await realtime_protocol.relay_audio(Browser(), route, session_id=run_id, voice=payload["voice"],
            instructions=None, max_duration_seconds=10, is_canceled=not_canceled, session_time=True)
        assert connection.closed and Decimal(0) < meter.connected_seconds <= Decimal(10)
        summary = await realtime._settle(run_id, owner=owner, meter=meter)
        assert Decimal(summary["session_seconds"]) == meter.connected_seconds
        assert await realtime._settle(run_id, owner=owner, meter=meter) == {}
        await _finish(run_id, status="completed", message_id=None, owner=owner)
        async with factory() as session:
            usage = (await session.execute(select(ChatUsageLog).where(ChatUsageLog.run_id == run_id))).scalar_one()
            expected = (meter.connected_seconds * Decimal("0.001")).quantize(Decimal("0.0000000001"))
            assert usage.raw_cost == expected
            hold = (await session.execute(select(ChatModelCallReservation).where(
                ChatModelCallReservation.run_id == run_id))).scalar_one()
            run = (await session.execute(select(ChatRun).where(ChatRun.id == run_id))).scalar_one()
            assert run.status == "completed" and hold.status == "settled"
            assert hold.actual_credits == usage.credited_cost
    finally:
        await close_db()


async def test_compat_gateways_route_by_wire_transport_not_renamed_selector(monkeypatch):
    """A renamed public selector must not hide the route whose transport a vendor wire fixes."""
    from lumen.api.compat import realtime as compat

    nonce = uuid.uuid4().hex
    user_id, project_id = f"realtime-compat-user-{nonce}", f"realtime-compat-project-{nonce}"
    init_db(os.environ["DATABASE_URL"], pool_size=1, max_overflow=0)
    factory = get_session_factory()
    assert factory is not None
    monkeypatch.setattr(routing, "resolve_api_key", lambda _provider: "fixture-provider-key")
    monkeypatch.setattr(compat, "get_settings", lambda: SimpleNamespace(chat_api_hosts=""))

    async def verify_key(_key):
        return {"user_id": user_id, "project_id": project_id, "api_key_id": None,
                "scopes": ["compat:realtime:write"]}

    connected = []

    async def relay(_websocket, *, run_id, token, wire):
        connected.append((run_id, wire))

    monkeypatch.setattr(compat.api_key_store, "verify_key", verify_key)
    monkeypatch.setattr(compat, "run_realtime_session", relay)

    class Socket:
        headers = {"x-api-key": "fixture-lumen-key"}

        def __init__(self, *, query=None, setup_model=None):
            self.scope = {"type": "websocket"}
            self.query_params = query or {}
            self.setup_model = setup_model
            self.code = None

        async def accept(self, subprotocol=None):
            return None

        async def receive_json(self):
            return {"setup": {"model": self.setup_model}}

        async def close(self, code=1000):
            self.code = code

    prices = {"realtime_input_per_minute": "0.012", "realtime_output_per_minute": "0.024"}
    created: list[int] = []
    try:
        async with factory() as session, session.begin():
            google = LlmProvider(name=f"realtime-google-{nonce}", provider_type="gemini", api_provider="google",
                                 is_active=True, margin_multiplier=Decimal("1"))
            openai = LlmProvider(name=f"realtime-openai-{nonce}", provider_type="openai",
                                 is_active=True, margin_multiplier=Decimal("1"))
            session.add_all([google, openai])
            await session.flush()
            created = [google.id, openai.id]
            openai_model = LlmModel(provider_id=openai.id, model_name="gpt-realtime", model_kind="realtime",
                                    media_pricing=prices, is_active=True)
            session.add_all([openai_model, LlmModel(provider_id=google.id, model_name="gemini-2.5-flash-live",
                                                    model_kind="realtime", media_pricing=prices, is_active=True)])
            await session.flush()
            google_id, openai_model_id = google.id, openai_model.id

        live = Socket(setup_model="models/gemini-2.5-flash-live")
        await compat.gemini_live(live)
        assert live.code == 1000 and [wire for _, wire in connected] == ["gemini"]
        run_id = connected[0][0]
        async with factory() as session:
            run = (await session.execute(select(ChatRun).where(ChatRun.id == run_id))).scalar_one()
        assert (run.capability_snapshot["provider_id"], run.capability_snapshot["provider_type"]) == (google_id, "gemini")
        assert (await request_cancelled(run_id=run_id, project_id=project_id, user_id=user_id)).status == "canceled"

        # Each wire executes only its own transport, whether named publicly or by numeric model ID.
        for socket, route in ((Socket(query={"model": "gemini-2.5-flash-live"}), compat.openai_realtime),
                              (Socket(setup_model=f"models/{openai_model_id}"), compat.gemini_live)):
            await route(socket)
            assert socket.code == 1011
        assert len(connected) == 1
        async with factory() as session:
            runs = (await session.execute(select(ChatRun.id).where(ChatRun.project_id == project_id))).scalars().all()
        assert runs == [run_id]
    finally:
        if created:
            async with factory() as session, session.begin():
                await session.execute(update(LlmProvider).where(LlmProvider.id.in_(created)).values(is_active=False))
        await close_db()
