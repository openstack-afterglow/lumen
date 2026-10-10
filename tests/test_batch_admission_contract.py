"""Focused admission, frozen projection, authority and JSONL contract tests."""
from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4, uuid5

import pytest

from lumen.config import get_settings
from lumen.models.batch_contracts import NativeBatchCreateRequest
from lumen.models.chat_batches import ChatBatchItem
from lumen.services import batches
from lumen.services.api_key_store import ApiKeyAuthorityUnavailable, ApiKeyForbidden
from lumen.services.durable_runs import api_completion

pytestmark = pytest.mark.usefixtures("current_project_authority")


@pytest.mark.parametrize("key", ["", "a" * 129, "bad\nkey", "é", "\x7f"])
def test_idempotency_key_rejects_non_printable_or_unbounded_input(key):
    with pytest.raises(batches.BatchInputError) as error:
        batches._key_hash(key)
    assert error.value.code == "invalid_idempotency_key"


def test_idempotency_key_hash_is_exact_and_optional_only_for_compat():
    assert batches._key_hash(None) is None
    assert batches._key_hash("Key") != batches._key_hash("key")
    assert len(batches._key_hash(" " * 128)) == 64


@pytest.mark.parametrize(("operation", "contract", "body", "required"), [
    ("chat.completions", "native", {}, ("compat:completions:write", "native:batches:write")),
    ("responses", "openai", {}, ("compat:batches:write", "compat:completions:write")),
    ("images.generations", "openai", {}, ("compat:batches:write", "compat:images:write")),
    ("images.edits", "native", {"source_asset_id": "asset"},
     ("native:batches:write", "native:images:write", "native:assets:read")),
    ("audio.transcriptions", "native", {"input_asset_id": "asset"},
     ("native:batches:write", "native:audio:write", "native:assets:read")),
    ("audio.speech", "native", {}, ("native:batches:write", "native:audio:write")),
])
def test_operation_scope_mapping(operation, contract, body, required):
    assert batches._scopes(operation, contract, body) == required
    batches._check_scopes(None, required)  # Keystone authority
    batches._check_scopes(required, required)
    with pytest.raises(batches.BatchInputError) as error:
        batches._check_scopes((), required)
    assert error.value.code == "missing_scope"


@pytest.mark.parametrize("contract", ["native", "openai"])
async def test_removing_batch_write_after_admission_blocks_frozen_authority(contract, monkeypatch):
    from lumen.services import api_key_store

    async def current_authority(user_id, project_id):
        return {"roles": ["member", "lumen-chat_user"], "is_system_admin": False}

    monkeypatch.setattr("lumen.auth.resolve_project_authority", current_authority)

    required = batches._scopes("chat.completions", contract, {})
    write = "native:batches:write" if contract == "native" else "compat:batches:write"
    row = SimpleNamespace(id=7, owner_user_id="u", owner_project_id="p", is_active=True, revoked_at=None,
                          expires_at=None, scopes=["compat:completions:write"])

    class Session:
        async def execute(self, _statement):
            return SimpleNamespace(scalar_one_or_none=lambda: row)

    with pytest.raises(ApiKeyForbidden):
        await api_key_store.authorize_api_key_in_transaction(
            Session(), api_key_id=7, user_id="u", project_id="p", required_scopes=required)
    row.scopes = ["compat:completions:write", write]
    await api_key_store.authorize_api_key_in_transaction(
        Session(), api_key_id=7, user_id="u", project_id="p", required_scopes=required)


@pytest.mark.parametrize(("error_type", "status", "code"), [
    (batches.BatchNotFound, 404, "batch_not_found"),
    (batches.BatchConflict, 409, "batch_conflict"),
    (batches.BatchInputError, 400, "invalid_batch_input"),
    (batches.BatchUnavailable, 503, "batch_unavailable"),
])
def test_safe_file_and_batch_error_contract(error_type, status, code):
    error = error_type()
    assert error.status_code == status and error.code == code
    located = error_type("line_too_large", "Input line exceeds the limit", start_byte=42)
    assert located.start_byte == 42 and located.message == str(located)


async def test_disabled_batch_fails_closed_before_admission(monkeypatch):
    monkeypatch.setattr(get_settings(), "batch_enabled", False)
    request = NativeBatchCreateRequest.model_validate({"items": [{"custom_id": "a", "operation": "responses",
        "body": {"model": "fixture", "input": "hello"}}]})
    with pytest.raises(batches.BatchUnavailable):
        await batches.create_native_batch(project_id="p", user_id="u", api_key_id=None, scopes=None,
            source="web", idempotency_key="key", request=request)


@pytest.mark.parametrize(("setting", "limit", "code"), [
    ("batch_native_max_items", 1, "too_many_items"),
    ("batch_native_max_bytes", 1, "request_too_large"),
])
async def test_native_limits_fail_before_persistence(monkeypatch, setting, limit, code):
    monkeypatch.setattr(batches, "_factory", lambda: object())
    monkeypatch.setattr(get_settings(), setting, limit)
    persist = AsyncMock(side_effect=AssertionError("must not persist oversized requests"))
    monkeypatch.setattr(batches, "_create", persist)
    request = NativeBatchCreateRequest.model_validate({"items": [
        {"custom_id": key, "operation": "responses", "body": {"model": "fixture", "input": "hello"}}
        for key in ("a", "b")]})
    with pytest.raises(batches.BatchInputError) as error:
        await batches.create_native_batch(project_id="p", user_id="u", api_key_id=None, scopes=None,
            source="web", idempotency_key="key", request=request)
    assert error.value.code == code
    persist.assert_not_awaited()


async def test_native_create_rejects_missing_item_scope_immediately(monkeypatch):
    monkeypatch.setattr(batches, "_factory", lambda: object())
    request = NativeBatchCreateRequest.model_validate({"items": [{"custom_id": "a", "operation": "responses",
        "body": {"model": "fixture", "input": "hello"}}]})
    with pytest.raises(batches.BatchInputError) as error:
        await batches.create_native_batch(project_id="p", user_id="u", api_key_id=7,
            scopes=("native:batches:write",), source="api", idempotency_key="key", request=request)
    assert error.value.code == "missing_scope"


def test_prepared_payload_round_trip_uses_frozen_snapshots_not_reresolution():
    batch = SimpleNamespace(project_id="p", user_id="u")
    item = ChatBatchItem(operation="responses", request_ciphertext=batches._seal({"operation": "responses", "model": "fixture"}),
        capability_snapshot={"version": "frozen"}, pricing_snapshot={"rate": "0.25"}, required_scopes=["compat:completions:write"])
    prepared, persist = batches._prepared(batch, item)
    assert isinstance(prepared, api_completion.PreparedApiCompletion)
    assert prepared.payload == {"operation": "responses", "model": "fixture"}
    assert prepared.pricing_snapshot == {"rate": "0.25"}
    assert prepared.capability_snapshot == {"version": "frozen"}
    assert prepared.required_scopes == ("compat:completions:write",)
    assert persist is api_completion.persist_api_completion_run_in_transaction
    assert "fixture" not in item.request_ciphertext


def test_native_terminal_item_view_uses_only_frozen_result():
    item = ChatBatchItem(custom_id="case-sensitive", ordinal=1, operation="responses", state="queued", settlement_status="settled")
    body = {"output": [{"text": "Private result"}]}
    envelope = {"status_code": 200, "request_id": "r", "body": body}
    batches._freeze(item, "completed", response=body, envelope=envelope)
    view = batches._item_view(item)
    assert view.response == body and view.error is None and view.settlement_status == "settled"
    assert item.http_status == 200 and item.request_id == "r"
    assert "Private result" not in item.result_ciphertext
    with pytest.raises(FrozenInstanceError):
        view.status = "failed"


async def test_current_key_authorization_never_falls_back_to_keystone(monkeypatch):
    class Session:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_args):
            pass

    monkeypatch.setattr(batches, "_factory", lambda: Session)
    check = AsyncMock(side_effect=ApiKeyForbidden("revoked"))
    monkeypatch.setattr(batches, "current_api_key_scopes", check)
    batch = SimpleNamespace(project_id="p", user_id="u", api_key_id=7, contract="openai")
    allowed = await batches._authority(batch)
    assert check.await_args.kwargs["api_key_id"] == 7
    with pytest.raises(batches.BatchInputError) as error:
        await batches._prepare(batch, "chat.completions",
                               {"model": "m", "messages": [{"role": "user", "content": "hi"}]}, allowed)
    assert error.value.code == "api_key_unauthorized"
    check.side_effect = ApiKeyAuthorityUnavailable("directory down")
    with pytest.raises(batches.BatchUnavailable):
        await batches._authority(batch)


async def test_result_jsonl_routes_success_http_failure_and_unknown(monkeypatch):
    batch_id = str(uuid4())
    success = ChatBatchItem(custom_id="success", ordinal=1, operation="responses")
    rejection = ChatBatchItem(custom_id="http", ordinal=2, operation="responses")
    unknown = ChatBatchItem(custom_id="unknown", ordinal=3, operation="responses")
    batches._freeze(success, "completed", envelope={"status_code": 200, "request_id": "a", "body": {"output": []}})
    batches._freeze(rejection, "failed", envelope={"status_code": 429, "request_id": "b", "body": {"error": {"code": "rate_limited"}}}, code="provider_rejected")
    batches._freeze(unknown, "unknown", code="provider_result_unknown")

    class Session:
        def __init__(self):
            self.calls = 0
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_args):
            pass
        async def execute(self, _query):
            self.calls += 1
            rows = [success, rejection, unknown]
            if self.calls == 1:
                return SimpleNamespace(all=lambda: [(item.ordinal, len(item.result_ciphertext)) for item in rows])
            if self.calls == 2:
                return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))
            return SimpleNamespace(all=lambda: [])

    session = Session()
    monkeypatch.setattr(batches, "_factory", lambda: lambda: session)
    output = [json.loads(line) async for line in batches._result_lines(batch_id, "output")]
    session.calls = 0
    errors = [json.loads(line) async for line in batches._result_lines(batch_id, "error")]
    assert [row["custom_id"] for row in output] == ["success"]
    assert [row["custom_id"] for row in errors] == ["http", "unknown"]
    assert errors[0]["response"]["status_code"] == 429 and errors[0]["error"] is None
    assert errors[1]["response"] is None and errors[1]["error"]["code"] == "provider_result_unknown"
    assert output[0]["id"] == "batch_req_" + uuid5(UUID(batch_id), "success").hex
    assert all(set(row) == {"id", "custom_id", "response", "error"} for row in output + errors)
