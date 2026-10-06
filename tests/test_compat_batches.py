"""OpenAI-compatible Batch route contract: API-key auth, envelopes, IDs, pagination and wire DTOs."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest

from lumen.auth import get_principal
from lumen.config import get_settings
from lumen.main import app
from lumen.models.batch_contracts import (
    OpenAIBatchList,
    OpenAIBatchObject,
    OpenAIFileDeleted,
    OpenAIFileObject,
    batch_item_scopes,
    openai_internal_id,
    openai_public_id,
    unix_seconds,
)
from lumen.services import batches as batch_service

_BATCH_ID = "0b7e3c1a-5a4f-4c39-9c51-2b1b7f7a9e10"
_FILE_ID = "5d0c6f4e-0f8e-4c1e-9d4b-3f6a5b2c1d0e"
_OUTPUT_ID = "6d0c6f4e-0f8e-4c1e-9d4b-3f6a5b2c1d0e"
_PUBLIC_BATCH = "batch_" + UUID(_BATCH_ID).hex
_PUBLIC_FILE = "file-" + UUID(_FILE_ID).hex
_CREATED = datetime(2026, 10, 6, 1, 2, 3, tzinfo=UTC)
_ALL_SCOPES = ("compat:batches:read", "compat:batches:write", "compat:completions:write", "compat:images:write")


def _view(**overrides) -> batch_service.BatchView:
    values = {
        "id": _BATCH_ID, "contract": "openai", "endpoint": "/v1/chat/completions", "status": "completed",
        "metadata": {"job": "nightly"}, "errors": [], "input_file_id": _FILE_ID, "output_file_id": _OUTPUT_ID,
        "error_file_id": None, "output_expires_after_seconds": None,
        "counts": {"total": 3, "completed": 2, "failed": 1, "pending": 0, "queued": 0, "running": 0,
                   "cancelled": 1, "expired": 0, "unknown": 0},
        "created_at": _CREATED, "expires_at": _CREATED + timedelta(hours=24),
        "in_progress_at": _CREATED + timedelta(seconds=5), "finalizing_at": _CREATED + timedelta(seconds=50),
        "completed_at": _CREATED + timedelta(seconds=60), "failed_at": None, "cancelling_at": None,
        "cancelled_at": None, "expired_at": None,
    }
    values.update(overrides)
    return batch_service.BatchView(**values)


def _create_body(endpoint: str = "/v1/chat/completions") -> dict:
    return {"input_file_id": _PUBLIC_FILE, "endpoint": endpoint, "completion_window": "24h", "metadata": {"job": "nightly"}}


def _api_key(*scopes: str):
    async def principal():
        return {"auth_type": "api_key", "user_id": "u1", "project_id": "p1", "api_key_id": 7,
                "scopes": tuple(scopes), "source": "api", "roles": [], "is_system_admin": False}

    return principal


@pytest.fixture(autouse=True)
def _allow_all_hosts(monkeypatch):
    monkeypatch.setattr("lumen.auth.get_settings", lambda: SimpleNamespace(chat_api_hosts=""))


@pytest.fixture
def api_key(client):
    """The client fixture installs a Keystone principal; compat routes need an API key."""
    app.dependency_overrides[get_principal] = _api_key(*_ALL_SCOPES)


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(get_settings(), "batch_enabled", True)


@pytest.fixture
def service(monkeypatch):
    calls: list[tuple[str, dict]] = []

    def record(name, result):
        async def call(**kwargs):
            calls.append((name, kwargs))
            if isinstance(result, Exception):
                raise result
            return result
        monkeypatch.setattr(batch_service, name, call)

    record("create_openai_batch", _view(status="validating", output_file_id=None, completed_at=None,
                                        in_progress_at=None, finalizing_at=None))
    record("list_batches", ([_view()], False))
    record("get_batch", _view())
    record("cancel_batch", _view(status="cancelling", cancelling_at=_CREATED + timedelta(seconds=9)))
    return SimpleNamespace(calls=calls, set=record)


def _routes():
    return [
        ("POST", "/v1/batches", {"json": _create_body()}),
        ("GET", "/v1/batches", {}),
        ("GET", f"/v1/batches/{_PUBLIC_BATCH}", {}),
        ("POST", f"/v1/batches/{_PUBLIC_BATCH}/cancel", {}),
    ]


def _assert_envelope(response, status: int, code: str | None) -> dict:
    assert response.status_code == status
    error = response.json()["error"]
    assert set(error) >= {"message", "type", "code"}
    assert error["code"] == code
    return error


# ── Wire DTOs ──────────────────────────────────────────────────────────────────
def test_public_ids_round_trip_and_reject_malformed_values():
    assert openai_public_id("batch_", _BATCH_ID) == _PUBLIC_BATCH
    assert openai_internal_id("batch_", _PUBLIC_BATCH) == _BATCH_ID
    assert openai_internal_id("file-", _PUBLIC_FILE) == _FILE_ID
    for malformed in ("batch_", "batch_" + "A" * 32, "batch_" + "a" * 31, "file-" + "a" * 32, _BATCH_ID,
                      "batch_" + "g" * 32, "batch_" + "a" * 33):
        assert openai_internal_id("batch_", malformed) is None


def test_unix_timestamps_treat_naive_database_values_as_utc():
    assert unix_seconds(None) is None
    assert unix_seconds(_CREATED) == 1791248523
    assert unix_seconds(_CREATED.replace(tzinfo=None)) == 1791248523


def test_openai_wire_objects_serialize_standard_shapes():
    from lumen.api.compat.batches import openai_batch

    body = openai_batch(_view(errors=[{"code": "invalid_json", "message": "line is not JSON", "line": 3}])).model_dump(mode="json")
    assert body["id"] == _PUBLIC_BATCH and body["object"] == "batch"
    assert body["input_file_id"] == _PUBLIC_FILE
    assert body["output_file_id"] == "file-" + UUID(_OUTPUT_ID).hex and body["error_file_id"] is None
    assert body["completion_window"] == "24h"
    assert body["created_at"] == 1791248523 and body["expires_at"] == 1791248523 + 86400
    assert body["in_progress_at"] == 1791248528 and body["completed_at"] == 1791248583
    assert body["cancelled_at"] is None and body["failed_at"] is None
    assert all(isinstance(body[key], int) for key in ("created_at", "expires_at", "finalizing_at"))
    # OpenAI request_counts expose only total/completed/failed; failed already includes cancelled items.
    assert body["request_counts"] == {"total": 3, "completed": 2, "failed": 1}
    assert body["errors"] == {"object": "list", "data": [
        {"code": "invalid_json", "message": "line is not JSON", "param": None, "line": 3},
    ]}
    assert openai_batch(_view()).errors is None
    OpenAIBatchObject.model_validate(body)

    file_object = OpenAIFileObject(id=_PUBLIC_FILE, bytes=12, created_at=1791248523, filename="in.jsonl",
                                   purpose="batch", status="processed", expires_at=None).model_dump(mode="json")
    assert file_object == {"id": _PUBLIC_FILE, "object": "file", "bytes": 12, "created_at": 1791248523,
                           "filename": "in.jsonl", "purpose": "batch", "status": "processed",
                           "status_details": None, "expires_at": None}
    assert OpenAIFileDeleted(id=_PUBLIC_FILE).model_dump() == {"id": _PUBLIC_FILE, "object": "file", "deleted": True}


def test_compat_endpoint_scopes():
    assert batch_item_scopes("chat.completions", contract="openai") == ("compat:completions:write",)
    assert batch_item_scopes("responses", contract="openai") == ("compat:completions:write",)
    assert batch_item_scopes("images.generations", contract="openai") == ("compat:images:write",)


def test_batch_scopes_are_issuable_but_not_default():
    from lumen.services import api_key_store as aks

    batch_scopes = {"native:batches:read", "native:batches:write", "compat:batches:read", "compat:batches:write",
                    "compat:files:read", "compat:files:write"}
    assert batch_scopes <= aks.API_KEY_SCOPES
    assert batch_scopes.isdisjoint(aks.DEFAULT_API_KEY_SCOPES)


# ── Routes ─────────────────────────────────────────────────────────────────────
async def test_compat_routes_return_batch_unavailable_when_disabled(client, api_key, service, monkeypatch):
    monkeypatch.setattr(get_settings(), "batch_enabled", False)
    for method, path, kwargs in _routes():
        _assert_envelope(await client.request(method, path, **kwargs), 503, "batch_unavailable")
    assert service.calls == []


async def test_compat_routes_require_an_api_key(client, enabled, service):
    for method, path, kwargs in _routes():
        assert (await client.request(method, path, **kwargs)).status_code == 401
    assert service.calls == []


@pytest.mark.parametrize(("method", "path", "scope"), [
    ("POST", "/v1/batches", "compat:batches:write"),
    ("GET", "/v1/batches", "compat:batches:read"),
    ("GET", f"/v1/batches/{_PUBLIC_BATCH}", "compat:batches:read"),
    ("POST", f"/v1/batches/{_PUBLIC_BATCH}/cancel", "compat:batches:write"),
])
async def test_compat_routes_deny_keys_without_batch_scope(client, api_key, enabled, service, method, path, scope):
    app.dependency_overrides[get_principal] = _api_key(*(set(_ALL_SCOPES) - {scope}))
    kwargs = {"json": _create_body()} if (method, path) == ("POST", "/v1/batches") else {}
    response = await client.request(method, path, **kwargs)
    assert response.status_code == 403
    assert service.calls == []


@pytest.mark.parametrize(("endpoint", "scope"), [
    ("/v1/chat/completions", "compat:completions:write"),
    ("/v1/responses", "compat:completions:write"),
    ("/v1/images/generations", "compat:images:write"),
])
async def test_compat_create_requires_the_endpoint_scope(client, api_key, enabled, service, endpoint, scope):
    app.dependency_overrides[get_principal] = _api_key(*(set(_ALL_SCOPES) - {scope}))
    error = _assert_envelope(await client.post("/v1/batches", json=_create_body(endpoint)), 403, "missing_scope")
    assert error["type"] == "permission_error" and scope in error["message"]
    assert service.calls == []

    app.dependency_overrides[get_principal] = _api_key("compat:batches:write", scope)
    response = await client.post("/v1/batches", json=_create_body(endpoint))
    assert response.status_code == 200
    [(_, kwargs)] = service.calls
    assert kwargs["scopes"] == ("compat:batches:write", scope)
    assert kwargs["request"].endpoint == endpoint and kwargs["request"].input_file_id == _PUBLIC_FILE


async def test_compat_create_returns_batch_object_and_passes_owner_context(client, api_key, enabled, service):
    response = await client.post("/v1/batches", json=_create_body())
    assert response.status_code == 200
    body = OpenAIBatchObject.model_validate(response.json())
    assert body.id == _PUBLIC_BATCH and body.status == "validating" and body.output_file_id is None
    assert response.json()["created_at"] == 1791248523
    [(name, kwargs)] = service.calls
    assert name == "create_openai_batch"
    assert (kwargs["project_id"], kwargs["user_id"], kwargs["api_key_id"], kwargs["source"]) == ("p1", "u1", 7, "api")
    assert kwargs["idempotency_key"] is None


async def test_compat_idempotent_replay_and_conflict(client, api_key, enabled, service):
    replay = await client.post("/v1/batches", json=_create_body(), headers={"Idempotency-Key": "same-key"})
    assert replay.status_code == 200 and replay.json()["id"] == _PUBLIC_BATCH
    assert service.calls[-1][1]["idempotency_key"] == "same-key"

    service.set("create_openai_batch", batch_service.BatchConflict("idempotency_key_reused"))
    _assert_envelope(await client.post("/v1/batches", json=_create_body(), headers={"Idempotency-Key": "same-key"}),
                     409, "idempotency_key_reused")
    calls = len(service.calls)
    _assert_envelope(await client.post("/v1/batches", json=_create_body(), headers={"Idempotency-Key": "k" * 129}),
                     400, "invalid_idempotency_key")
    assert len(service.calls) == calls


@pytest.mark.parametrize("body", [
    {**_create_body(), "completion_window": "48h"},
    {**_create_body(), "endpoint": "/v1/audio/speech"},
    {**_create_body(), "endpoint": "/v1/embeddings"},
    {**_create_body(), "input_file_id": "file-abc"},
    {**_create_body(), "metadata": {str(index): "v" for index in range(17)}},
    {**_create_body(), "output_expires_after": {"anchor": "created_at", "seconds": 60}},
    {**_create_body(), "unexpected": True},
])
async def test_compat_create_rejects_invalid_requests_with_openai_envelope(client, api_key, enabled, service, body):
    error = _assert_envelope(await client.post("/v1/batches", json=body), 400, "invalid_batch_request")
    assert error["type"] == "invalid_request_error"
    assert service.calls == []


async def test_compat_create_maps_input_and_size_errors(client, api_key, enabled, service):
    _assert_envelope(await client.post("/v1/batches", content=b"[", headers={"Content-Type": "application/json"}),
                     400, "invalid_json")
    _assert_envelope(await client.post("/v1/batches", content=b"{" + b" " * (64 * 1024) + b"}",
                                       headers={"Content-Type": "application/json"}), 413, "request_too_large")
    service.set("create_openai_batch", batch_service.BatchInputError("input_file_unavailable"))
    _assert_envelope(await client.post("/v1/batches", json=_create_body()), 400, "input_file_unavailable")
    service.set("create_openai_batch", batch_service.BatchUnavailable())
    error = _assert_envelope(await client.post("/v1/batches", json=_create_body()), 503, "batch_unavailable")
    assert error["type"] == "api_error"


@pytest.mark.parametrize(("method", "suffix"), [("GET", ""), ("POST", "/cancel")])
@pytest.mark.parametrize("batch_id", [_BATCH_ID, "batch_xyz", "batch_" + UUID(_BATCH_ID).hex.upper(), "file-" + UUID(_BATCH_ID).hex])
async def test_compat_malformed_batch_ids_are_404_without_lookup(client, api_key, enabled, service, method, suffix, batch_id):
    _assert_envelope(await client.request(method, f"/v1/batches/{batch_id}{suffix}"), 404, "batch_not_found")
    assert service.calls == []


@pytest.mark.parametrize(("method", "suffix", "name"), [("GET", "", "get_batch"), ("POST", "/cancel", "cancel_batch")])
async def test_compat_foreign_or_native_batches_are_404(client, api_key, enabled, service, method, suffix, name):
    service.set(name, batch_service.BatchNotFound())
    _assert_envelope(await client.request(method, f"/v1/batches/{_PUBLIC_BATCH}{suffix}"), 404, "batch_not_found")
    [(called, kwargs)] = service.calls
    assert called == name
    assert kwargs == {"project_id": "p1", "user_id": "u1", "batch_id": _BATCH_ID, "contract": "openai"}


async def test_compat_cancel_returns_cancelling_batch_and_conflict_for_terminal(client, api_key, enabled, service):
    response = await client.post(f"/v1/batches/{_PUBLIC_BATCH}/cancel")
    assert response.status_code == 200
    assert response.json()["status"] == "cancelling" and response.json()["cancelling_at"] == 1791248532
    service.set("cancel_batch", batch_service.BatchConflict("invalid_batch_state"))
    _assert_envelope(await client.post(f"/v1/batches/{_PUBLIC_BATCH}/cancel"), 409, "invalid_batch_state")


async def test_compat_list_pagination_shape(client, api_key, enabled, service):
    second = "1b7e3c1a-5a4f-4c39-9c51-2b1b7f7a9e11"
    service.set("list_batches", ([_view(), _view(id=second)], True))
    response = await client.get("/v1/batches", params={"after": _PUBLIC_BATCH, "limit": 2})
    assert response.status_code == 200
    page = OpenAIBatchList.model_validate(response.json())
    assert page.object == "list" and page.has_more is True
    assert page.first_id == _PUBLIC_BATCH and page.last_id == "batch_" + UUID(second).hex
    [(_, kwargs)] = service.calls
    assert kwargs == {"project_id": "p1", "user_id": "u1", "contract": "openai", "after": _BATCH_ID, "limit": 2}

    service.set("list_batches", ([], False))
    assert (await client.get("/v1/batches")).json() == {"object": "list", "data": [], "first_id": None,
                                                         "last_id": None, "has_more": False}
    assert service.calls[-1][1]["limit"] == 20 and service.calls[-1][1]["after"] is None
    calls = len(service.calls)
    _assert_envelope(await client.get("/v1/batches", params={"limit": 101}), 400, "invalid_limit")
    _assert_envelope(await client.get("/v1/batches", params={"limit": 0}), 400, "invalid_limit")
    _assert_envelope(await client.get("/v1/batches", params={"after": "file-" + "a" * 32}), 400, "invalid_cursor")
    assert len(service.calls) == calls


async def test_compat_batch_routes_are_host_gated(client, api_key, enabled, service, monkeypatch):
    monkeypatch.setattr("lumen.auth.get_settings", lambda: SimpleNamespace(chat_api_hosts="api.cloud.example"))
    assert (await client.get("/v1/batches")).status_code == 404
    assert (await client.get("/v1/files")).status_code == 404
    assert service.calls == []


async def test_discovery_publishes_batch_contract_and_enabled_flag(client, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "batch_enabled", False)
    monkeypatch.setattr(settings, "batch_native_max_items", 7)
    monkeypatch.setattr(settings, "batch_jsonl_max_rows", 11)
    body = (await client.get("/v1/compat")).json()
    assert body["batches"]["enabled"] is False
    openai = body["batches"]["openai"]
    assert openai["supported_endpoints"] == ["/v1/chat/completions", "/v1/responses", "/v1/images/generations"]
    assert (openai["max_rows"], openai["max_file_bytes"], openai["max_line_bytes"]) == (
        11, settings.batch_jsonl_max_bytes, settings.batch_jsonl_max_line_bytes,
    )
    assert openai["files"].endswith("/v1/files") and openai["batches"].endswith("/v1/batches")
    assert openai["endpoint_scopes"]["/v1/images/generations"] == ["compat:images:write"]
    assert openai["required_scopes"]["files_write"] == ["compat:files:write"]
    native = body["batches"]["native"]
    assert native["operations"] == ["chat.completions", "responses", "images.generations", "images.edits",
                                    "audio.speech", "audio.transcriptions"]
    assert (native["max_items"], native["max_body_bytes"]) == (7, settings.batch_native_max_bytes)
    assert native["operation_scopes"]["audio.transcriptions"] == ["native:audio:write", "native:assets:read"]
    assert {"/v1/files", "/v1/batches"} <= set(body["host_gate"]["gated_routes"])

    monkeypatch.setattr(settings, "batch_enabled", True)
    assert (await client.get("/v1/compat")).json()["batches"]["enabled"] is True
