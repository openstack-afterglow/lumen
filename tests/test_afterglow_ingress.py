"""Afterglow signature checks through actual Vault and chat admission routes."""

import hashlib
import hmac
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from test_chat_completions import _patch_text_execution

from lumen import auth
from lumen.main import app
from lumen.services import afterglow_ingress, chat_admission, vault_store
from lumen.services.durable_runs import admission

_TIME = 1_800_000_000
_SECRET = "fixture only " * 4
_TOKEN = "keystone-user-fixture"
_PROJECT = "test-project-123"
_TARGET = "owner-target-project"
_NOTES = "/v1/vault/notes"
_CHAT = "/v1/conversations/c1/completions"


def _sign(method, path, *, token=_TOKEN, project=_PROJECT, target="", timestamp=_TIME):
    fingerprint = hashlib.sha256(token.encode()).hexdigest()
    payload = f"v1\n{timestamp}\n{method}\n{path}\n{fingerprint}\n{project}\n{target}"
    return f"v1:{timestamp}:{hmac.digest(_SECRET.encode(), payload.encode(), 'sha256').hex()}"


@pytest.fixture
def directory(monkeypatch):
    monkeypatch.setenv("LUMEN_AFTERGLOW_INGRESS_SECRET", _SECRET)
    monkeypatch.setattr(afterglow_ingress, "time", lambda: _TIME)

    def validate(token, *, project_id="", target_project_id=""):
        return {"auth_token": f"rescoped-{token}", "project_id": target_project_id or project_id or _PROJECT,
                "connection_project_id": project_id or _PROJECT, "user_id": "test-user-123",
                "roles": ["member"], "is_system_admin": True}

    monkeypatch.setattr(auth, "validate_token", validate)
    monkeypatch.setattr(auth, "resolve_project_authority", AsyncMock(return_value={
        "roles": ["member"], "is_system_admin": True, "domain_id": "test-domain-123",
    }))


@pytest.mark.parametrize("case", [
    "valid", "valid_target", "valid_boundary", "absent", "bad_mac", "expired", "future",
    "wrong_token", "wrong_method", "wrong_path",
    "project_added", "project_removed", "project_changed", "target_added", "target_removed", "target_changed",
    "without_secret", "short_secret", "bearer", "duplicate_scope",
])
async def test_vault_ingress_and_persistent_chat_admission(directory, monkeypatch, case):
    """Invalid or absent origin proof must not load notes or freeze Vault tool authority."""
    plan_schemas = chat_admission._preview_tool_schemas
    await _patch_text_execution(monkeypatch)
    monkeypatch.setattr(chat_admission, "_preview_tool_schemas", plan_schemas)
    notes = AsyncMock(return_value=[])
    recall = AsyncMock(return_value=["Dashboard-only fixture note"])
    monkeypatch.setattr(vault_store, "list_notes", notes)
    monkeypatch.setattr(chat_admission.memory_host, "recall_for_run", recall)
    created = []

    async def capture(**kwargs):
        created.append(kwargs)
        return {"run_id": str(uuid4()), "status": "queued", "events_url": "/events", "cancel_url": "/cancel"}

    monkeypatch.setattr(admission, "create_persistent_run", capture)

    if case == "without_secret":
        monkeypatch.delenv("LUMEN_AFTERGLOW_INGRESS_SECRET")
    elif case == "short_secret":
        monkeypatch.setenv("LUMEN_AFTERGLOW_INGRESS_SECRET", "too short")
    afterglow_ingress.get_settings.cache_clear()

    def headers(method, path):
        signed = {}
        sent = {"X-Auth-Token": _TOKEN, "X-Project-Id": _PROJECT, "Idempotency-Key": str(uuid4())}
        if case == "valid_target":
            signed["target"] = _TARGET
            sent["X-Target-Project-Id"] = _TARGET
        elif case == "valid_boundary":
            signed["timestamp"] = _TIME - 120
        elif case == "wrong_token":
            signed["token"] = "another-keystone-user-fixture"
        elif case == "wrong_method":
            method = "PATCH"
        elif case == "wrong_path":
            path += "/other"
        elif case == "expired":
            signed["timestamp"] = _TIME - 121
        elif case == "future":
            signed["timestamp"] = _TIME + 121
        elif case == "project_added":
            signed["project"] = ""
        elif case == "project_removed":
            sent.pop("X-Project-Id")
        elif case == "project_changed":
            sent["X-Project-Id"] += "-elsewhere"
        elif case == "target_added":
            sent["X-Target-Project-Id"] = _TARGET
        elif case == "target_removed":
            signed["target"] = _TARGET
        elif case == "target_changed":
            signed["target"] = _TARGET
            sent["X-Target-Project-Id"] = _TARGET + "-elsewhere"
        elif case == "bearer":
            sent["Authorization"] = "Bearer " + sent.pop("X-Auth-Token")
        if case != "absent":
            marker = _sign(method, path, **signed)
            if case == "bad_mac":
                marker = marker.rsplit(":", 1)[0] + ":" + "0" * 64
            sent["X-Lumen-Ingress"] = marker
        if case == "duplicate_scope":
            return [*sent.items(), ("X-Project-Id", _PROJECT)]
        return sent

    valid = case.startswith("valid")
    async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
        response = await client.get(_NOTES + "?kind=user_note", headers=headers("GET", _NOTES))
        assert response.status_code == (200 if valid else 403), response.text
        if valid:
            notes.assert_awaited_once()
        else:
            assert response.json() == {"detail": "vault_ingress_unverified"}
            notes.assert_not_awaited()
        response = await client.post(
            _CHAT, headers=headers("POST", _CHAT),
            json={"parts": [{"type": "text", "text": "Read my Vault"}], "model_id": "gpt-3.5-turbo",
                  "features": {"memory": True, "tool_policy": {"mode": "agent_default"}}},
        )
    assert response.status_code == 202, response.text
    assert len(created) == 1
    run = SimpleNamespace(source="web", api_key_id=None, run_scope="persistent", parent_run_id=None,
                          capability_snapshot=created[0]["capability_snapshot"])
    assert vault_store.frozen_run_domain(run) == ("test-domain-123" if valid else None)
    payload = created[0]["request_payload"]
    assert any(item["function"]["name"] == "vault_context" for item in payload["tool_schemas"]) is valid
    assert any("Dashboard-only fixture note" in item["content"] for item in payload["input_messages"]) is valid
    assert recall.await_count == int(valid)
