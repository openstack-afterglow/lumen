"""Real transaction coverage for image idempotency, output ownership and irreversible holds."""

from __future__ import annotations

import asyncio
import base64
import io
import os
import uuid
from contextlib import suppress
from decimal import Decimal

import pytest
from sqlalchemy import delete, event, func, select, text

from lumen.db import close_db, get_session_factory, init_db
from lumen.models.chat_assets import ChatAsset, ChatRunAsset
from lumen.models.chat_db import ChatUsageLog, LlmModel, LlmProvider
from lumen.models.chat_runs import ChatModelCallReservation, ChatRun, ChatRunSegment
from lumen.services import assets, credit
from lumen.services.durable_runs import budgets, images, queries
from lumen.services.durable_runs.errors import DurableRunConflict, DurableRunInputError
from lumen.services.providers import routing
from lumen.services.run_store import claim_queued_run
from lumen.services.usage_breakdown import ModalityTokens, UsageBreakdown

pytestmark = pytest.mark.integration

# Tiny valid PNG. The scanner/object-store boundary is replaced, not the owned asset ledger.
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScL/nwAAAABJRU5ErkJggg=="
)


async def test_durable_image_settlement_and_unknown_recovery(monkeypatch):
    nonce = uuid.uuid4().hex
    user_id, project_id = f"image-user-{nonce}", f"image-project-{nonce}"
    init_db(os.environ["DATABASE_URL"], pool_size=1, max_overflow=0)
    factory = get_session_factory()
    assert factory is not None
    monkeypatch.setattr(routing, "resolve_api_key", lambda _provider: "fixture-provider-key")
    monkeypatch.setattr(assets, "asset_pipeline_available", lambda: True)
    async def no_wake(_run_id):
        return None
    monkeypatch.setattr(images, "wake_run", no_wake)
    try:
        async with factory() as session, session.begin():
            provider = LlmProvider(name=f"image-provider-{nonce}", provider_type="openai",
                                   is_active=True, margin_multiplier=Decimal("2"))
            session.add(provider)
            await session.flush()
            model = LlmModel(provider_id=provider.id, model_name="gpt-image-1", model_kind="image",
                             media_pricing={"image_variants": {"1024x1024:high": "0.04"}},
                             is_active=True)
            session.add(model)
            await session.flush()
            model_id = model.id
            provider_id = provider.id
        async def store_generated(*, data, original_name, media_type, user_id, project_id, run_id):
            assert data == _PNG and media_type == "image/png"
            asset_id = str(uuid.uuid4())
            async with factory() as session, session.begin():
                session.add(ChatAsset(id=asset_id, project_id=project_id, user_id=user_id,
                      object_key=f"image-test/{nonce}/{asset_id}", bucket_name="image-test",
                      original_name=original_name, mime_type=media_type, size_bytes=len(data),
                      sha256="0" * 64, status="clean", media_metadata={}))
                session.add(ChatRunAsset(run_id=run_id, asset_id=asset_id, purpose="output"))
            return {"id": asset_id}
        monkeypatch.setattr(assets, "create_generated_asset_bytes", store_generated)
        async def open_owned(*, asset_id, user_id, project_id):
            row = await assets.get_asset(asset_id=asset_id, user_id=user_id, project_id=project_id)
            assert row["status"] == "clean"
            return assets.AssetDownload(body=io.BytesIO(_PNG), name=row["name"],
                                        mime_type=row["mime_type"], size_bytes=row["size_bytes"])
        monkeypatch.setattr(assets, "open_download", open_owned)
        invoked = []
        async def provider_call(route, *, prompt, size, quality, n, source_image, source_mime):
            invoked.append(prompt)
            assert size == "1024x1024" and quality == "high" and n == 1
            assert source_image is None and source_mime is None
            return images.image_transport.ImageResult([(_PNG, "image/png")])
        monkeypatch.setattr(images.image_transport, "generate_images", provider_call)
        request = {"model_id": str(model_id), "prompt": "A cobalt cube", "size": "1024x1024", "quality": "high", "n": 1}
        key = str(uuid.uuid4())
        first = await images.admit_image_run(request, project_id=project_id, user_id=user_id, client_request_id=key)
        repeated = await images.admit_image_run({key: value for key, value in request.items() if key != "n"},
                                                project_id=project_id, user_id=user_id, client_request_id=key)
        assert first.run_id == repeated.run_id
        with pytest.raises(DurableRunConflict, match="idempotency_key_reused"):
            await images.admit_image_run({**request, "prompt": "A red sphere"}, project_id=project_id,
                                         user_id=user_id, client_request_id=key)
        with pytest.raises(DurableRunInputError, match="image provider or model is unavailable"):
            await images.admit_image_run({**request, "provider_id": provider_id + 1},
                        project_id=project_id, user_id=user_id, client_request_id=str(uuid.uuid4()))
        async with factory() as session, session.begin():
            claimed = await claim_queued_run(session, first.run_id, owner="image-worker")
            assert claimed is not None
            owner = claimed.lease_owner
            capability = dict(claimed.capability_snapshot)
            frozen = dict(claimed.pricing_snapshot)
        assert await images.execute_image_run(first.run_id, owner=owner,
                   payload={**request, "user_id": user_id, "project_id": project_id},
                   capability_snapshot=capability, pricing_snapshot=frozen)
        assert invoked == ["A cobalt cube"]
        response = await queries.owned_run_response(run_id=first.run_id, user_id=user_id, project_id=project_id)
        assert response.status == "completed" and response.run_kind == "image"
        assert len(response.output_assets) == 1
        asset_id = response.output_assets[0]["asset_id"]
        assert response.output_assets[0]["download_url"] == f"/v1/assets/{asset_id}/download"
        materialized = await images.image_result(run_id=first.run_id, user_id=user_id, project_id=project_id)
        assert base64.b64decode(materialized["data"][0]["b64_json"]) == _PNG
        with pytest.raises(assets.AssetError, match="asset forbidden"):
            await images.admit_image_run({**request, "source_asset_id": asset_id},
                        project_id=project_id + "-other", user_id=user_id,
                        client_request_id=str(uuid.uuid4()))
        async with factory() as session:
            usage = (await session.execute(select(ChatUsageLog).where(ChatUsageLog.run_id == first.run_id))).scalars().all()
            assert len(usage) == 1
            assert usage[0].raw_cost == Decimal("0.04")
            assert usage[0].credited_cost == credit.credits_for_cost(Decimal("0.04"), Decimal("2"))
            hold = (await session.execute(select(ChatModelCallReservation).where(
                ChatModelCallReservation.run_id == first.run_id))).scalar_one()
            assert hold.status == "settled" and hold.actual_credits == usage[0].credited_cost
            assert (await session.execute(select(ChatRun).where(ChatRun.id == first.run_id))).scalar_one().last_seq > 3

        second = await images.admit_image_run({**request, "prompt": "A teal tree"}, project_id=project_id,
                                             user_id=user_id, client_request_id=str(uuid.uuid4()))
        async with factory() as session, session.begin():
            claimed = await claim_queued_run(session, second.run_id, owner="image-worker")
            assert claimed is not None
            owner = claimed.lease_owner
            capability = dict(claimed.capability_snapshot)
            frozen = dict(claimed.pricing_snapshot)
        bound = credit.credits_for_cost(Decimal("0.04"), Decimal("2"), Decimal(frozen["credit_per_usd"]))
        assert await images._image_segment_start(second.run_id, owner=owner, bound=bound) == "started"
        # Simulate a worker process dying after its committed provider_started boundary.
        assert await images.execute_image_run(second.run_id, owner=owner,
                   payload={**request, "prompt": "A teal tree", "user_id": user_id, "project_id": project_id},
                   capability_snapshot=capability, pricing_snapshot=frozen)
        assert invoked == ["A cobalt cube"]
        async with factory() as session:
            run = (await session.execute(select(ChatRun).where(ChatRun.id == second.run_id))).scalar_one()
            reservation = (await session.execute(select(ChatModelCallReservation).where(
                ChatModelCallReservation.run_id == second.run_id))).scalar_one()
            segment = (await session.execute(select(ChatRunSegment).where(ChatRunSegment.run_id == second.run_id))).scalar_one()
            usage = (await session.execute(select(ChatUsageLog).where(ChatUsageLog.run_id == second.run_id))).scalars().all()
            assert run.status == "failed" and segment.status == "failed"
            assert reservation.status == "unknown" and reservation.bound_credits == bound
            assert run.reserved_credits == bound and usage == []

        third = await images.admit_image_run({**request, "prompt": "An orange orbit"}, project_id=project_id,
                                            user_id=user_id, client_request_id=str(uuid.uuid4()))
        from lumen.services.durable_runs.lifecycle import request_cancelled

        canceled = await request_cancelled(run_id=third.run_id, project_id=project_id, user_id=user_id)
        assert canceled.status == "canceled"
        async with factory() as session:
            assert (await session.execute(select(ChatModelCallReservation).where(
                ChatModelCallReservation.run_id == third.run_id))).scalar_one_or_none() is None
            assert (await session.execute(select(ChatUsageLog).where(
                ChatUsageLog.run_id == third.run_id))).scalars().all() == []
        assert invoked == ["A cobalt cube"]
    finally:
        await close_db()


@pytest.mark.parametrize("snapshot_isolation", ["OFF", "ON"])
async def test_concurrent_media_holds_observe_committed_reservations(monkeypatch, request, snapshot_isolation):
    """A second request must not reserve against a snapshot predating the wallet lock."""
    from sqlalchemy.pool import Pool

    def pin_isolation(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute(f"SET SESSION innodb_snapshot_isolation={snapshot_isolation}")
        cursor.close()

    event.listen(Pool, "connect", pin_isolation)
    request.addfinalizer(lambda: event.remove(Pool, "connect", pin_isolation))
    init_db(os.environ["DATABASE_URL"], pool_size=3, max_overflow=0)
    factory = get_session_factory()
    assert factory is not None
    nonce = uuid.uuid4().hex
    user_id, project_id = f"media-user-{nonce}", f"media-project-{nonce}"
    first_id, second_id = str(uuid.uuid4()), str(uuid.uuid4())
    bound = Decimal("6")
    snapshot_seen = asyncio.Event()
    second_task = None

    async def quota(_session, _user_id):
        return type("Quota", (), {"monthly": Decimal("10"), "weekly": Decimal("0")})()

    monkeypatch.setattr(credit.quota_policy, "get_system_quota", quota)

    async def reserve_second():
        async def transaction():
            async with factory() as session, session.begin():
                await session.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
                isolation = (await session.execute(text("SELECT @@SESSION.innodb_snapshot_isolation"))).scalar_one()
                assert int(isolation) == (snapshot_isolation == "ON")
                # The run was read before the competing reservation commits.
                await session.execute(select(ChatRun.id).where(ChatRun.id == second_id))
                snapshot_seen.set()
                await credit.reserve_media_credit_in_transaction(
                    session, user_id=user_id, project_id=project_id, api_key_id=None, bound=bound,
                )
                session.add(ChatModelCallReservation(
                    run_id=second_id, segment_id="image:1", bound_credits=bound, status="reserved",
                ))
        return await budgets.retry_deadlocks(transaction)

    try:
        async with factory() as session, session.begin():
            await credit._get_or_create_wallet(session, user_id, project_id)
            for run_id in (first_id, second_id):
                session.add(ChatRun(
                    id=run_id, run_scope="image", run_kind="image", project_id=project_id,
                    user_id=user_id, model_name="hold-race", status="running",
                    capability_snapshot={}, pricing_snapshot={}, client_request_id=str(uuid.uuid4()),
                    request_fingerprint=nonce, fingerprint_version=1, execution_protocol_version=1,
                ))
        async with factory() as session, session.begin():
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
            await credit.reserve_media_credit_in_transaction(
                session, user_id=user_id, project_id=project_id, api_key_id=None, bound=bound,
            )
            session.add(ChatModelCallReservation(
                run_id=first_id, segment_id="image:1", bound_credits=bound, status="reserved",
            ))
            await session.flush()
            second_task = asyncio.create_task(reserve_second())
            await asyncio.wait_for(snapshot_seen.wait(), 10)
        with pytest.raises(credit.QuotaExceeded, match="월"):
            await asyncio.wait_for(second_task, 10)
        async with factory() as session:
            held = (await session.execute(select(func.sum(ChatModelCallReservation.bound_credits)).where(
                ChatModelCallReservation.run_id.in_((first_id, second_id)),
            ))).scalar_one()
            assert held == bound
    finally:
        if second_task is not None and not second_task.done():
            second_task.cancel()
            with suppress(asyncio.CancelledError):
                await second_task
        async with factory() as session, session.begin():
            await session.execute(delete(ChatModelCallReservation).where(
                ChatModelCallReservation.run_id.in_((first_id, second_id)),
            ))
            await session.execute(delete(ChatRun).where(ChatRun.id.in_((first_id, second_id))))
            await session.execute(delete(credit.UserWallet).where(credit.UserWallet.user_id == user_id))
        await close_db()


@pytest.mark.parametrize("snapshot_isolation", ["OFF", "ON"])
async def test_media_start_does_not_block_another_users_text_start(monkeypatch, request, snapshot_isolation):
    """A media wallet hold must not lock another user's reservation insert."""
    from sqlalchemy.pool import Pool

    def pin_isolation(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute(f"SET SESSION innodb_snapshot_isolation={snapshot_isolation}")
        cursor.close()

    event.listen(Pool, "connect", pin_isolation)
    request.addfinalizer(lambda: event.remove(Pool, "connect", pin_isolation))
    init_db(os.environ["DATABASE_URL"], pool_size=2, max_overflow=0)
    factory = get_session_factory()
    assert factory is not None
    nonce = uuid.uuid4().hex
    media_id, text_id = str(uuid.uuid4()), str(uuid.uuid4())
    media_user, text_user = f"media-user-{nonce}", f"text-user-{nonce}"
    media_project, text_project = f"media-project-{nonce}", f"text-project-{nonce}"
    text_task = None

    async def quota(_session, _user_id):
        return type("Quota", (), {"monthly": Decimal("10"), "weekly": Decimal("0")})()

    monkeypatch.setattr(credit.quota_policy, "get_system_quota", quota)

    async def start_text():
        async with factory() as session, session.begin():
            await session.execute(select(ChatRun).where(ChatRun.id == text_id).with_for_update())
            session.add(ChatModelCallReservation(
                run_id=text_id, segment_id="provider:0:1", bound_credits=Decimal("1"), status="reserved",
            ))
            await session.flush()

    try:
        async with factory() as session, session.begin():
            await credit._get_or_create_wallet(session, media_user, media_project)
            for run_id, run_scope, run_kind, user_id, project_id in (
                (media_id, "image", "image", media_user, media_project),
                (text_id, "temp", "completion", text_user, text_project),
            ):
                session.add(ChatRun(
                    id=run_id, run_scope=run_scope, run_kind=run_kind, project_id=project_id,
                    user_id=user_id, model_name="hold-race", status="running",
                    capability_snapshot={}, pricing_snapshot={}, client_request_id=str(uuid.uuid4()),
                    request_fingerprint=nonce, fingerprint_version=1, execution_protocol_version=1,
                ))
        async with factory() as session, session.begin():
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
            await credit.reserve_media_credit_in_transaction(
                session, user_id=media_user, project_id=media_project,
                api_key_id=None, bound=Decimal("1"),
            )
            text_task = asyncio.create_task(start_text())
            # The text run must commit while the media transaction still owns its wallet lock.
            await asyncio.wait_for(text_task, 10)
            session.add(ChatModelCallReservation(
                run_id=media_id, segment_id="image:1", bound_credits=Decimal("1"), status="reserved",
            ))
        async with factory() as session:
            held = (await session.execute(select(func.sum(ChatModelCallReservation.bound_credits)).where(
                ChatModelCallReservation.run_id.in_((media_id, text_id)),
            ))).scalar_one()
            assert held == Decimal("2")
    finally:
        if text_task is not None and not text_task.done():
            text_task.cancel()
            with suppress(asyncio.CancelledError):
                await text_task
        async with factory() as session, session.begin():
            await session.execute(delete(ChatModelCallReservation).where(
                ChatModelCallReservation.run_id.in_((media_id, text_id)),
            ))
            await session.execute(delete(ChatRun).where(ChatRun.id.in_((media_id, text_id))))
            await session.execute(delete(credit.UserWallet).where(credit.UserWallet.user_id == media_user))
        await close_db()


@pytest.mark.parametrize("outcome", ["observed", "missing", "over_envelope", "invalid", "storage_failure"])
async def test_image_token_usage_frozen_settlement_or_unknown_hold(monkeypatch, outcome):
    nonce = uuid.uuid4().hex
    user_id, project_id = f"image-token-user-{nonce}", f"image-token-project-{nonce}"
    init_db(os.environ["DATABASE_URL"], pool_size=1, max_overflow=0)
    factory = get_session_factory()
    monkeypatch.setattr(routing, "resolve_api_key", lambda _provider: "fixture-provider-key")
    monkeypatch.setattr(assets, "asset_pipeline_available", lambda: True)

    async def no_wake(_run_id):
        return None

    monkeypatch.setattr(images, "wake_run", no_wake)
    try:
        async with factory() as session, session.begin():
            provider = LlmProvider(name=f"image-token-provider-{nonce}", provider_type="openai",
                                   is_active=True, margin_multiplier=Decimal("2"))
            session.add(provider)
            await session.flush()
            model = LlmModel(provider_id=provider.id, model_name="gpt-image-1.5", model_kind="image",
                input_price=Decimal("0.000001"), output_price=Decimal("0.000002"),
                cache_read_price=Decimal("0.0000005"), is_active=True,
                media_pricing={"billing_basis": "tokens", "reservation_usd": "0.01",
                    "image_variants": {"1024x1024:high": "0.99"},
                    "token_rates": {"image": {"input_per_million": "10", "cache_read_per_million": "2",
                                               "output_per_million": "20"}}})
            session.add(model)
            await session.flush()
            model_id = model.id

        async def store_generated(*, data, original_name, media_type, user_id, project_id, run_id):
            if outcome == "storage_failure":
                raise assets.AssetError("object storage unavailable")
            asset_id = str(uuid.uuid4())
            async with factory() as session, session.begin():
                session.add(ChatAsset(id=asset_id, project_id=project_id, user_id=user_id,
                    object_key=f"image-token/{nonce}/{asset_id}", bucket_name="image-test",
                    original_name=original_name, mime_type=media_type, size_bytes=len(data),
                    sha256="0" * 64, status="clean", media_metadata={}))
                session.add(ChatRunAsset(run_id=run_id, asset_id=asset_id, purpose="output"))
            return {"id": asset_id}

        monkeypatch.setattr(assets, "create_generated_asset_bytes", store_generated)
        calls = []
        observed = UsageBreakdown(100, 200 if outcome != "over_envelope" else 2000,
            cache_read_input_tokens=40,
            modality_tokens={"image": ModalityTokens(input_tokens=50,
                output_tokens=200 if outcome != "over_envelope" else 2000,
                cache_read_input_tokens=30)}, modality_usage_invalid=outcome == "invalid")

        async def provider_call(route, **kwargs):
            calls.append(kwargs["prompt"])
            return images.image_transport.ImageResult([(_PNG, "image/png")],
                observed if outcome != "missing" else None)

        monkeypatch.setattr(images.image_transport, "generate_images", provider_call)
        request = {"model_id": str(model_id), "prompt": "A cached cobalt cube", "size": "1024x1024", "quality": "high"}
        admitted = await images.admit_image_run(request, project_id=project_id, user_id=user_id,
                                               client_request_id=str(uuid.uuid4()))
        async with factory() as session, session.begin():
            claimed = await claim_queued_run(session, admitted.run_id, owner="image-token-worker")
            owner, capability, frozen = claimed.lease_owner, dict(claimed.capability_snapshot), dict(claimed.pricing_snapshot)
        # The transport revalidation sees a changed current rate; settlement must still use admission's prices.
        resolve = routing.resolve_model_snapshot

        async def changed_prices(snapshot):
            route = await resolve(snapshot)
            return {**route, "input_price_per_token": Decimal("1"), "output_price_per_token": Decimal("2"),
                    "media_pricing": {**route["media_pricing"], "reservation_usd": "10",
                        "token_rates": {"image": {"input_per_million": "1000", "output_per_million": "2000"}}}}

        monkeypatch.setattr(routing, "resolve_model_snapshot", changed_prices)
        await images.execute_image_run(admitted.run_id, owner=owner,
            payload={**request, "user_id": user_id, "project_id": project_id},
            capability_snapshot=capability, pricing_snapshot=frozen)
        async with factory() as session:
            run = await session.get(ChatRun, admitted.run_id)
            hold = (await session.execute(select(ChatModelCallReservation).where(
                ChatModelCallReservation.run_id == run.id))).scalar_one()
            ledger = (await session.execute(select(ChatUsageLog).where(ChatUsageLog.run_id == run.id))).scalars().all()
            segment = (await session.execute(select(ChatRunSegment).where(ChatRunSegment.run_id == run.id))).scalar_one()
            assert hold.bound_credits == credit.credits_for_cost(Decimal("0.01"), Decimal("2"), Decimal(frozen["credit_per_usd"]))
            if outcome == "observed":
                assert run.status == "completed" and hold.status == "settled"
                assert len(ledger) == 1
                assert ledger[0].raw_cost == Decimal("0.004305")
                assert ledger[0].prompt_tokens == 100 and ledger[0].cache_read_input_tokens == 40
                assert hold.actual_credits == credit.credits_for_cost(Decimal("0.004305"), Decimal("2"), Decimal(frozen["credit_per_usd"]))
                checkpoint = images.load_segment_payload(segment.usage_payload)
                assert checkpoint["token_usage"]["modality_tokens"]["image"]["cache_read_input_tokens"] == 30
            else:
                assert run.status == "failed" and hold.status == "unknown" and ledger == []
                assert run.reserved_credits == hold.bound_credits
                if outcome in {"invalid", "storage_failure"}:
                    # The provider already charged: its observed usage must survive as resolution evidence.
                    checkpoint = images.load_segment_payload(segment.usage_payload)
                    assert checkpoint["token_usage"]["prompt_tokens"] == 100
                    assert checkpoint["token_usage"].get("modality_usage_invalid", False) is (outcome == "invalid")
                if outcome == "storage_failure":
                    assert segment.status == "failed"
                    assert (await session.execute(select(ChatRunAsset).where(
                        ChatRunAsset.run_id == run.id, ChatRunAsset.purpose == "output"))).scalars().all() == []
        assert calls == ["A cached cobalt cube"]
    finally:
        await close_db()
