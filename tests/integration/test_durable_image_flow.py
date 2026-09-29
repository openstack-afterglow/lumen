"""Real transaction coverage for image idempotency, output ownership and irreversible holds."""

from __future__ import annotations

import base64
import io
import os
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from lumen.db import close_db, get_session_factory, init_db
from lumen.models.chat_assets import ChatAsset, ChatRunAsset
from lumen.models.chat_db import ChatUsageLog, LlmModel, LlmProvider
from lumen.models.chat_runs import ChatModelCallReservation, ChatRun, ChatRunSegment
from lumen.services import assets, credit
from lumen.services.durable_runs import images, queries
from lumen.services.durable_runs.errors import DurableRunConflict, DurableRunInputError
from lumen.services.providers import routing
from lumen.services.run_store import claim_queued_run

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
            return [(_PNG, "image/png")]
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
