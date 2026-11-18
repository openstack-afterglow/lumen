"""Native media ingress: verify that selection happens before credit or provider I/O."""
from decimal import Decimal
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from lumen.services import assets, credit
from lumen.services.durable_runs import admission, audio, images, realtime
from lumen.services.providers import routing


@pytest.mark.parametrize("path,kind,fields", [
    ("images/generations", "image", {"prompt": "Draw a circle"}),
    ("images/edits", "image", {"prompt": "Make it red", "input_asset_id": str(uuid4())}),
    ("audio/speech", "tts", {"input": "Hello", "voice": "alloy"}),
    ("audio/transcriptions", "stt", {"input_asset_id": str(uuid4())}),
    ("realtime/sessions", "realtime", {}),
])
async def test_native_media_exact_row_reaches_validation_without_reroute(client, monkeypatch, path, kind, fields):
    model_name = "shared-model"
    routes = {row_id: {"model_id": row_id, "provider_id": provider_id, "model_name": model_name,
                       "api_model_name": model_name, "provider_type": "openai", "api_provider": "openai",
                       "media_pricing": {}, "margin_multiplier": "1"}
              for row_id, provider_id in ((11, 1), (12, 2))}

    async def exact(row_id, *, model_kind):
        assert model_kind == kind
        return routes.get(row_id)

    async def legacy(name, *, provider_id, model_kind, **_):
        assert (name, model_kind) == (model_name, kind)
        if provider_id is None:
            raise routing.AmbiguousModelRouteError("duplicate model")
        return routes[provider_id + 10]

    selected = []

    def validate(route, **_):
        selected.append(route)
        return Decimal("1")

    existing = AsyncMock(return_value=None)
    precheck = AsyncMock(side_effect=credit.QuotaExceeded("test credit boundary"))
    monkeypatch.setattr(admission, "existing_run_for_intent", existing)
    monkeypatch.setattr(routing, "resolve_model_by_id", exact)
    monkeypatch.setattr(routing, "resolve_api_model", legacy)
    monkeypatch.setattr(credit, "precheck", precheck)
    monkeypatch.setattr(assets, "asset_pipeline_available", lambda: True)
    asset = {"status": "clean", "size_bytes": 256, "mime_type": "image/png" if kind == "image" else "audio/wav",
             "media_metadata": {"width": 1024, "height": 1024, "duration_ms": 1000}}
    monkeypatch.setattr(assets, "get_asset", AsyncMock(return_value=asset))
    monkeypatch.setattr(images.image_transport, "validate_image_request", validate)
    monkeypatch.setattr(images.image_transport, "image_billing_basis", lambda _route: "variants")
    monkeypatch.setattr(audio.audio_transport, "validate_audio_request", validate)
    monkeypatch.setattr(realtime.realtime_transport, "realtime_kind", lambda _route: "conversation")
    monkeypatch.setattr(realtime.realtime_transport, "available_realtime_options", lambda _route: {"default_voice": "alloy"})
    monkeypatch.setattr(realtime.realtime_transport, "validate_realtime_request", validate)

    async def post(**selection):
        return await client.post(f"/v1/chat/{path}", json={**fields, **selection}, headers={"Idempotency-Key": str(uuid4())})

    result = await post(model_row_id=12)
    assert result.status_code == 402, result.text
    assert selected == [routes[12]]
    assert existing.await_args.kwargs["intent"]["model_row_id"] == 12
    for other in ({"model_id": "wrong"}, {"model_id": "11"}, {"provider_id": 1}):
        result = await post(model_row_id=12, **other)
        assert (result.status_code, result.json()["detail"]) == (422, "model_selection_mismatch")
    assert (await post(model_row_id="12")).status_code == 422
    assert precheck.await_count == 1
    assert (await post(model_id="12")).status_code == 402
    assert "model_row_id" not in existing.await_args.kwargs["intent"]
    assert (await post(model_id=model_name, provider_id=2)).status_code == 402
    assert selected == [routes[12]] * 3
    assert (await post(model_id=model_name)).status_code == 422
