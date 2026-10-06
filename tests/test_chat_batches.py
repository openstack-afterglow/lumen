"""Consumer-visible native/JSONL batch validation, route and durable admission contracts."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from lumen.auth import get_principal
from lumen.config import get_settings
from lumen.main import app
from lumen.models.batch_contracts import (
    NativeBatchCreateRequest,
    NativeBatchDescriptor,
    OpenAIBatchCreateRequest,
    OpenAIBatchRow,
    batch_item_scopes,
)
from lumen.models.chat_contracts import ChatRunDescriptor, ChatRunResponse, validate_chat_run_event
from lumen.services import batches as batch_service

_CHAT = {"model": "registered-model", "messages": [{"role": "user", "content": "hello"}]}


def _native(items):
    return NativeBatchCreateRequest(items=items)


def test_native_mixed_operations_preserve_asset_ids_and_case_sensitive_custom_ids():
    asset_id = "12345678-1234-1234-1234-123456789abc"
    request = _native([
        {"custom_id": "A", "operation": "chat.completions", "body": _CHAT},
        {"custom_id": "a", "operation": "responses", "body": {"model": "registered-model", "input": "hello"}},
        {"custom_id": "image", "operation": "images.generations", "body": {"model_id": "image", "prompt": "blue"}},
        {"custom_id": "edit", "operation": "images.edits", "body": {"model_id": "image", "prompt": "red", "input_asset_id": asset_id}},
        {"custom_id": "tts", "operation": "audio.speech", "body": {"model_id": "tts", "input": "hello", "voice": "alloy"}},
        {"custom_id": "stt", "operation": "audio.transcriptions", "body": {"model_id": "stt", "input_asset_id": asset_id}},
    ])
    assert [item.custom_id for item in request.items[:2]] == ["A", "a"]
    assert request.items[3].body["input_asset_id"] == asset_id
    assert request.items[5].body["input_asset_id"] == asset_id
    with pytest.raises(ValidationError, match="unique"):
        _native([request.items[0], request.items[0]])


@pytest.mark.parametrize("extra", [
    {"stream": True}, {"stream": 0}, {"headers": {"Authorization": "secret"}},
    {"api_key": "secret"}, {"api_base": "https://foreign.invalid"}, {"model": "lumen"},
    {"tools": [{"type": "web_search"}]}, {"context_management": [{"type": "compaction"}]},
])
def test_jsonl_rejects_unbounded_or_unauthorized_execution(extra):
    with pytest.raises(ValidationError):
        OpenAIBatchRow(custom_id="one", method="POST", url="/v1/chat/completions", body={**_CHAT, **extra})


@pytest.mark.parametrize("url", ["https://other.invalid/v1/chat/completions", "/v1/audio/speech", "/v1/images/edits"])
def test_compatible_contract_never_dispatches_arbitrary_or_native_only_urls(url):
    with pytest.raises(ValidationError):
        OpenAIBatchRow(custom_id="one", method="POST", url=url, body=_CHAT)


def test_compatible_metadata_and_output_expiry_bounds():
    base = {"input_file_id": "file-" + "a" * 32, "endpoint": "/v1/responses", "completion_window": "24h"}
    for seconds in (3600, 30 * 86400):
        assert OpenAIBatchCreateRequest(**base, output_expires_after={"anchor": "created_at", "seconds": seconds}).output_expires_after.seconds == seconds
    for seconds in (3599, 30 * 86400 + 1):
        with pytest.raises(ValidationError):
            OpenAIBatchCreateRequest(**base, output_expires_after={"anchor": "created_at", "seconds": seconds})
    with pytest.raises(ValidationError, match="16 pairs"):
        OpenAIBatchCreateRequest(**base, metadata={str(index): "value" for index in range(17)})
    with pytest.raises(ValidationError, match="512"):
        OpenAIBatchCreateRequest(**base, metadata={"key": "v" * 513})


def test_api_completion_journal_and_run_response_are_readable():
    run_id = "12345678-1234-1234-1234-123456789abc"
    descriptor = ChatRunDescriptor(run_id=run_id, run_kind="api_completion", status="queued", events_url="/events", cancel_url="/cancel")
    response = ChatRunResponse(run_id=run_id, run_kind="api_completion", status="completed", last_seq=2, terminal=True)
    event = validate_chat_run_event({
        "event_id": f"{run_id}:1", "run_id": run_id, "seq": 1, "type": "run.started",
        "created_at": "2026-10-06T00:00:00Z", "payload": {"model_name": "model", "effective_features": {}, "run_kind": "api_completion"},
    })
    assert descriptor.run_kind == response.run_kind == event.payload.run_kind == "api_completion"


# ── Native route contract ──────────────────────────────────────────────────────
_BATCH_ID = "0b7e3c1a-5a4f-4c39-9c51-2b1b7f7a9e10"
_ASSET_ID = "12345678-1234-1234-1234-123456789abc"
_CREATED = datetime(2026, 10, 6, 1, 2, 3, tzinfo=UTC)
_ITEMS_BY_OPERATION = {
    "chat.completions": _CHAT,
    "responses": {"model": "registered-model", "input": "hello"},
    "images.generations": {"model_id": "image", "prompt": "blue"},
    "images.edits": {"model_id": "image", "prompt": "red", "input_asset_id": _ASSET_ID},
    "audio.speech": {"model_id": "tts", "input": "hello", "voice": "alloy"},
    "audio.transcriptions": {"model_id": "stt", "input_asset_id": _ASSET_ID},
}
_COUNTS = {"total": 2, "completed": 1, "failed": 0, "pending": 0, "queued": 0, "running": 1,
           "cancelled": 0, "expired": 0, "unknown": 0}


def _view(**overrides) -> batch_service.BatchView:
    values = {
        "id": _BATCH_ID, "contract": "native", "endpoint": None, "status": "in_progress",
        "metadata": {"job": "nightly"}, "errors": [], "input_file_id": None, "output_file_id": None,
        "error_file_id": None, "output_expires_after_seconds": None, "counts": dict(_COUNTS),
        "created_at": _CREATED, "expires_at": _CREATED + timedelta(hours=24), "in_progress_at": _CREATED,
        "finalizing_at": None, "completed_at": None, "failed_at": None, "cancelling_at": None,
        "cancelled_at": None, "expired_at": None,
    }
    values.update(overrides)
    return batch_service.BatchView(**values)


def _create_body(*operations: str) -> dict:
    return {
        "items": [{"custom_id": f"item-{index}", "operation": operation, "body": _ITEMS_BY_OPERATION[operation]}
                  for index, operation in enumerate(operations)],
        "completion_window": "24h",
        "metadata": {"job": "nightly"},
    }


def _api_key(*scopes: str):
    async def principal():
        return {"auth_type": "api_key", "user_id": "test-user-123", "project_id": "test-project-123",
                "api_key_id": 41, "scopes": tuple(scopes), "source": "api", "roles": [], "is_system_admin": False}

    return principal


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(get_settings(), "batch_enabled", True)


@pytest.fixture
def service(monkeypatch):
    """Record service calls; every route must reach the coordinator only through these functions."""
    calls: list[tuple[str, dict]] = []

    def record(name, result):
        async def call(**kwargs):
            calls.append((name, kwargs))
            if isinstance(result, Exception):
                raise result
            return result
        monkeypatch.setattr(batch_service, name, call)

    record("create_native_batch", (_view(status="validating"), True))
    record("list_batches", ([_view()], False))
    record("get_batch", _view())
    record("list_batch_items", ([], None))
    record("cancel_batch", _view(status="cancelling", cancelling_at=_CREATED))
    return SimpleNamespace(calls=calls, set=record)


def _routes():
    return [
        ("POST", "/v1/chat/batches", {"json": _create_body("chat.completions"), "headers": {"Idempotency-Key": "k-1"}}),
        ("GET", "/v1/chat/batches", {}),
        ("GET", f"/v1/chat/batches/{_BATCH_ID}", {}),
        ("GET", f"/v1/chat/batches/{_BATCH_ID}/items", {}),
        ("POST", f"/v1/chat/batches/{_BATCH_ID}/cancel", {}),
    ]


async def test_native_routes_return_503_when_batch_disabled_without_touching_the_service(client, service, monkeypatch):
    monkeypatch.setattr(get_settings(), "batch_enabled", False)
    for method, path, kwargs in _routes():
        response = await client.request(method, path, **kwargs)
        assert response.status_code == 503, path
        assert response.json()["detail"] == "batch_unavailable"
    assert service.calls == []


async def test_native_create_returns_descriptor_links_and_keystone_authority(client, enabled, service):
    response = await client.post(
        "/v1/chat/batches", json=_create_body(*_ITEMS_BY_OPERATION), headers={"Idempotency-Key": "nightly 2026/10/06"},
    )
    assert response.status_code == 202
    descriptor = NativeBatchDescriptor.model_validate(response.json())
    assert str(descriptor.id) == _BATCH_ID and descriptor.status == "validating"
    assert descriptor.links == {
        "self": f"/v1/chat/batches/{_BATCH_ID}",
        "items": f"/v1/chat/batches/{_BATCH_ID}/items",
        "cancel": f"/v1/chat/batches/{_BATCH_ID}/cancel",
    }
    assert response.json()["created_at"].startswith("2026-10-06T01:02:03")
    assert response.json()["created_at"].endswith(("Z", "+00:00"))
    [(name, kwargs)] = service.calls
    assert name == "create_native_batch"
    assert kwargs["scopes"] is None and kwargs["source"] == "web" and kwargs["api_key_id"] is None
    assert kwargs["idempotency_key"] == "nightly 2026/10/06"
    assert [item.operation for item in kwargs["request"].items] == list(_ITEMS_BY_OPERATION)


async def test_native_idempotent_replay_returns_original_and_mismatch_is_409(client, enabled, service):
    service.set("create_native_batch", (_view(status="in_progress"), False))
    replay = await client.post("/v1/chat/batches", json=_create_body("responses"), headers={"Idempotency-Key": "k"})
    assert replay.status_code == 202
    assert replay.json()["id"] == _BATCH_ID and replay.json()["status"] == "in_progress"

    service.set("create_native_batch", batch_service.BatchConflict("idempotency_key_reused"))
    conflict = await client.post("/v1/chat/batches", json=_create_body("audio.speech"), headers={"Idempotency-Key": "k"})
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == "idempotency_key_reused"


@pytest.mark.parametrize("headers", [{}, {"Idempotency-Key": ""}, {"Idempotency-Key": "k" * 129}])
async def test_native_create_requires_bounded_idempotency_key(client, enabled, service, headers):
    response = await client.post("/v1/chat/batches", json=_create_body("responses"), headers=headers)
    assert response.status_code == 422
    assert service.calls == []


async def test_native_create_bounds_body_and_items_before_validation(client, enabled, service, monkeypatch):
    monkeypatch.setattr(get_settings(), "batch_native_max_bytes", 256)
    oversized = json.dumps(_create_body("chat.completions", "responses", "images.generations")).encode()
    assert len(oversized) > 256
    response = await client.post("/v1/chat/batches", content=oversized,
                                 headers={"Idempotency-Key": "k", "Content-Type": "application/json"})
    assert response.status_code == 413
    assert response.json()["detail"] == "request_too_large"

    monkeypatch.setattr(get_settings(), "batch_native_max_bytes", 10 * 1024 * 1024)
    monkeypatch.setattr(get_settings(), "batch_native_max_items", 2)
    # Invalid bodies prove the item cap is enforced before per-item schema validation.
    too_many = {"items": [{"custom_id": str(index), "operation": "responses", "body": {}} for index in range(3)]}
    response = await client.post("/v1/chat/batches", json=too_many, headers={"Idempotency-Key": "k"})
    assert response.status_code == 422
    assert response.json()["detail"] == "too_many_items"

    response = await client.post("/v1/chat/batches", content=b"{not json", headers={"Idempotency-Key": "k"})
    assert response.status_code == 400
    assert response.json()["detail"] == "invalid_json"

    response = await client.post("/v1/chat/batches", json={"items": []}, headers={"Idempotency-Key": "k"})
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"][:2] == ["body", "items"]
    assert service.calls == []


@pytest.mark.parametrize(("method", "path", "scope"), [
    ("POST", "/v1/chat/batches", "native:batches:write"),
    ("GET", "/v1/chat/batches", "native:batches:read"),
    ("GET", f"/v1/chat/batches/{_BATCH_ID}", "native:batches:read"),
    ("GET", f"/v1/chat/batches/{_BATCH_ID}/items", "native:batches:read"),
    ("POST", f"/v1/chat/batches/{_BATCH_ID}/cancel", "native:batches:write"),
])
async def test_native_routes_deny_api_keys_without_batch_scope(client, enabled, service, method, path, scope):
    every_other = {"native:batches:read", "native:batches:write", "compat:completions:write"} - {scope}
    app.dependency_overrides[get_principal] = _api_key(*every_other)  # the client fixture clears overrides
    is_create = (method, path) == ("POST", "/v1/chat/batches")
    kwargs = {"json": _create_body("chat.completions"), "headers": {"Idempotency-Key": "k"}} if is_create else {}
    response = await client.request(method, path, **kwargs)
    assert response.status_code == 403
    assert scope in response.json()["detail"]
    assert service.calls == []


@pytest.mark.parametrize("operation", list(_ITEMS_BY_OPERATION))
async def test_native_create_denies_each_item_operation_without_its_scope(client, enabled, service, operation):
    required = batch_item_scopes(operation, contract="native")
    for missing in required:
        granted = {"native:batches:write", *required} - {missing}
        app.dependency_overrides[get_principal] = _api_key(*granted)
        response = await client.post("/v1/chat/batches", json=_create_body(operation), headers={"Idempotency-Key": "k"})
        assert response.status_code == 403, (operation, missing)
        assert missing in response.json()["detail"]
    assert service.calls == []

    app.dependency_overrides[get_principal] = _api_key("native:batches:write", *required)
    response = await client.post("/v1/chat/batches", json=_create_body(operation), headers={"Idempotency-Key": "k"})
    assert response.status_code == 202
    [(_, kwargs)] = service.calls
    assert kwargs["scopes"] == ("native:batches:write", *required)
    assert kwargs["api_key_id"] == 41 and kwargs["source"] == "api"


def test_native_item_scopes_match_operation_contract():
    assert batch_item_scopes("chat.completions", contract="native") == ("compat:completions:write",)
    assert batch_item_scopes("responses", contract="native") == ("compat:completions:write",)
    assert batch_item_scopes("images.generations", contract="native") == ("native:images:write",)
    assert batch_item_scopes("images.edits", contract="native") == ("native:images:write", "native:assets:read")
    assert batch_item_scopes("audio.speech", contract="native") == ("native:audio:write",)
    assert batch_item_scopes("audio.transcriptions", contract="native") == ("native:audio:write", "native:assets:read")


@pytest.mark.parametrize(("method", "path"), [
    ("GET", "/v1/chat/batches/not-a-uuid"),
    ("GET", "/v1/chat/batches/not-a-uuid/items"),
    ("POST", "/v1/chat/batches/not-a-uuid/cancel"),
])
async def test_native_malformed_batch_ids_are_404_without_lookup(client, enabled, service, method, path):
    response = await client.request(method, path)
    assert response.status_code == 404
    assert response.json()["detail"] == "batch_not_found"
    assert service.calls == []


@pytest.mark.parametrize(("method", "path", "name"), [
    ("GET", f"/v1/chat/batches/{_BATCH_ID}", "get_batch"),
    ("GET", f"/v1/chat/batches/{_BATCH_ID}/items", "list_batch_items"),
    ("POST", f"/v1/chat/batches/{_BATCH_ID}/cancel", "cancel_batch"),
])
async def test_native_foreign_owner_batches_are_404(client, enabled, service, method, path, name):
    service.set(name, batch_service.BatchNotFound())
    response = await client.request(method, path)
    assert response.status_code == 404
    assert response.json()["detail"] == "batch_not_found"
    [(called, kwargs)] = service.calls
    assert called == name
    assert (kwargs["project_id"], kwargs["user_id"], kwargs["batch_id"]) == ("test-project-123", "test-user-123", _BATCH_ID)


async def test_native_cancel_returns_202_and_terminal_cancel_conflict_is_409(client, enabled, service):
    response = await client.post(f"/v1/chat/batches/{_BATCH_ID}/cancel")
    assert response.status_code == 202
    assert response.json()["status"] == "cancelling"
    assert service.calls[0][1]["contract"] == "native"

    service.set("cancel_batch", batch_service.BatchConflict("invalid_batch_state"))
    response = await client.post(f"/v1/chat/batches/{_BATCH_ID}/cancel")
    assert response.status_code == 409
    assert response.json()["detail"] == "invalid_batch_state"


async def test_native_list_pagination_shapes(client, enabled, service):
    second = "1b7e3c1a-5a4f-4c39-9c51-2b1b7f7a9e11"
    service.set("list_batches", ([_view(), _view(id=second)], True))
    response = await client.get("/v1/chat/batches", params={"limit": 2, "after": _BATCH_ID})
    assert response.status_code == 200
    body = response.json()
    assert [batch["id"] for batch in body["batches"]] == [_BATCH_ID, second]
    assert body["next_cursor"] == second
    [(_, kwargs)] = service.calls
    assert kwargs == {"project_id": "test-project-123", "user_id": "test-user-123", "contract": "native",
                      "after": _BATCH_ID, "limit": 2}

    service.set("list_batches", ([_view()], False))
    assert (await client.get("/v1/chat/batches")).json()["next_cursor"] is None
    assert service.calls[-1][1]["limit"] == 20
    for params in ({"limit": 0}, {"limit": 101}, {"after": "not-a-uuid"}):
        assert (await client.get("/v1/chat/batches", params=params)).status_code == 422


async def test_native_item_page_projects_results_and_ordinal_cursor(client, enabled, service):
    run_id = "22222222-2222-4222-8222-222222222222"
    items = [
        batch_service.BatchItemView("img", 1, "images.generations", run_id, "completed",
                                    {"data": [{"asset_id": _ASSET_ID, "mime_type": "image/png", "size_bytes": 68}]},
                                    None, "settled"),
        batch_service.BatchItemView("stt", 2, "audio.transcriptions", None, "failed", None,
                                    {"code": "api_key_unauthorized", "message": "API key is not authorized"}, "none"),
    ]
    service.set("list_batch_items", (items, 2))
    response = await client.get(f"/v1/chat/batches/{_BATCH_ID}/items", params={"after": 0, "limit": 2})
    assert response.status_code == 200
    body = response.json()
    assert body["next_cursor"] == 2
    assert body["items"][0] == {
        "custom_id": "img", "ordinal": 1, "operation": "images.generations", "run_id": run_id, "status": "completed",
        "response": {"data": [{"asset_id": _ASSET_ID, "mime_type": "image/png", "size_bytes": 68}]},
        "error": None, "settlement_status": "settled",
    }
    assert body["items"][1]["error"]["code"] == "api_key_unauthorized"
    [(_, kwargs)] = service.calls
    assert (kwargs["after"], kwargs["limit"]) == (0, 2)

    service.set("list_batch_items", ([], None))
    assert (await client.get(f"/v1/chat/batches/{_BATCH_ID}/items")).json() == {"items": [], "next_cursor": None}
    assert service.calls[-1][1]["limit"] == 100
    for params in ({"limit": 1001}, {"limit": 0}, {"after": -1}):
        assert (await client.get(f"/v1/chat/batches/{_BATCH_ID}/items", params=params)).status_code == 422


async def test_native_service_unavailable_and_input_errors_keep_stable_codes(client, enabled, service):
    service.set("get_batch", batch_service.BatchUnavailable())
    response = await client.get(f"/v1/chat/batches/{_BATCH_ID}")
    assert (response.status_code, response.json()["detail"]) == (503, "batch_unavailable")
    service.set("create_native_batch", batch_service.BatchInputError("request_too_large"))
    response = await client.post("/v1/chat/batches", json=_create_body("responses"), headers={"Idempotency-Key": "k"})
    assert (response.status_code, response.json()["detail"]) == (413, "request_too_large")
    service.set("create_native_batch", batch_service.BatchInputError("missing_scope"))
    response = await client.post("/v1/chat/batches", json=_create_body("responses"), headers={"Idempotency-Key": "k"})
    assert (response.status_code, response.json()["detail"]) == (403, "missing_scope")
