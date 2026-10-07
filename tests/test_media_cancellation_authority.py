"""Owned media cancellation never grants text-generation authority."""
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from lumen import auth
from lumen.api import batches as batch_routes
from lumen.api import completions
from lumen.services import batches, inference_authority
from lumen.services.durable_runs import lifecycle, queries
from lumen.services.durable_runs.errors import DurableRunNotFound


def actor(leaf, *, key=False, scopes=()):
    return {"auth_type": "api_key" if key else "keystone", "user_id": "owner", "project_id": "project",
            "api_key_id": 7 if key else None, "roles": ["member", leaf], "scopes": tuple(scopes),
            "source": "api" if key else "web", "is_system_admin": False}


@pytest.mark.parametrize(("kind", "scope", "leaf"), [
    ("image", "native:images:write", "lumen-images_user"),
    ("image", "compat:images:write", "lumen-images_user"),
    ("tts", "native:audio:write", "lumen-audio_user"),
    ("stt", "compat:audio:write", "lumen-audio_user"),
    ("realtime", "native:realtime:write", "lumen-audio_user"),
])
@pytest.mark.parametrize("key", [False, True])
def test_media_only_cancels_owned_matching_operation_without_chat(monkeypatch, kind, scope, leaf, key):
    run = SimpleNamespace(run_kind=kind, run_scope="image" if kind == "image" else "audio")
    required = inference_authority.cancel_run_scopes(run, {"required_scopes": [scope, "native:assets:read"]})
    assert required == (scope,)
    lookup = AsyncMock(return_value=required)
    cancel = AsyncMock(return_value={"status": "canceled"})
    monkeypatch.setattr(queries, "owned_run_cancel_scopes", lookup)
    monkeypatch.setattr(lifecycle, "request_cancelled", cancel)
    app = FastAPI()
    app.include_router(completions.router, prefix="/v1")
    principal = actor(leaf, key=key, scopes=(scope,))
    app.dependency_overrides[auth.get_principal] = lambda: principal
    with TestClient(app) as client:
        response = client.post("/v1/runs/owned-media/cancel")
    assert response.status_code == 200
    lookup.assert_awaited_once_with(run_id="owned-media", project_id="project", user_id="owner")
    cancel.assert_awaited_once_with(run_id="owned-media", project_id="project", user_id="owner")
    with pytest.raises(HTTPException):
        auth.ensure_scopes(principal, "native:runs:write")


@pytest.mark.parametrize(("scopes", "lookup_error", "expected"), [
    (("native:runs:write",), None, 403),
    (("native:images:write",), DurableRunNotFound("foreign owner"), 404),
])
def test_cancel_preserves_scope_attenuation_and_owner_failure(monkeypatch, scopes, lookup_error, expected):
    lookup = AsyncMock(return_value=("native:images:write",), side_effect=lookup_error)
    cancel = AsyncMock()
    monkeypatch.setattr(queries, "owned_run_cancel_scopes", lookup)
    monkeypatch.setattr(lifecycle, "request_cancelled", cancel)
    app = FastAPI()
    app.include_router(completions.router, prefix="/v1")
    app.dependency_overrides[auth.get_principal] = lambda: actor("lumen-images_user", key=True, scopes=scopes)
    with TestClient(app) as client:
        response = client.post("/v1/runs/media/cancel")
    assert response.status_code == expected
    cancel.assert_not_awaited()


@pytest.mark.parametrize(("operations", "required"), [
    (["images.generations"], {"native:batches:write", "native:images:write"}),
    (["audio.speech", "audio.transcriptions"], {"native:batches:write", "native:audio:write"}),
    (["images.generations", "chat.completions"], {"native:batches:write", "native:images:write", "compat:completions:write"}),
])
async def test_batch_cancel_derives_every_owned_primary_operation(monkeypatch, operations, required):
    row = SimpleNamespace(id="batch")
    owned = AsyncMock(return_value=row)
    monkeypatch.setattr(batches, "_owned", owned)
    class Session:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False
        async def execute(self, statement):
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: operations))
    monkeypatch.setattr(batches, "_factory", lambda: Session)
    actual = await batches.owned_cancel_scopes(project_id="project", user_id="owner", batch_id="batch", contract="native")
    assert set(actual) == required
    owned.assert_awaited_once_with(ANY,
        project_id="project", user_id="owner", batch_id="batch", contract="native")


@pytest.mark.parametrize("operation_scope", ["native:images:write", "compat:completions:write"])
async def test_image_only_batch_cancel_checks_item_action_before_mutation(monkeypatch, operation_scope):
    monkeypatch.setattr(batch_routes, "batch_enabled_or_503", lambda: None)
    lookup = AsyncMock(return_value=("native:batches:write", operation_scope))
    cancel = AsyncMock(return_value=SimpleNamespace(status="cancelling"))
    monkeypatch.setattr(batches, "owned_cancel_scopes", lookup)
    monkeypatch.setattr(batches, "cancel_batch", cancel)
    monkeypatch.setattr(batch_routes, "native_descriptor", lambda view: view)
    principal = actor("lumen-images_user", key=True, scopes=("native:batches:write", "native:images:write"))
    if operation_scope == "native:images:write":
        result = await batch_routes.cancel_batch(str(uuid4()), principal)
        assert result.status == "cancelling" and cancel.await_count == 1
    else:
        with pytest.raises(HTTPException) as error:
            await batch_routes.cancel_batch(str(uuid4()), principal)
        assert error.value.status_code == 403
        cancel.assert_not_awaited()
