"""Real native HTTP handlers + owned provider_client against a loopback runtime fixture."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
import test_chat_api_model_routing
from fastapi import HTTPException
from test_provider_accounts import _pool
from test_provider_transport import connect_runtime, http_fixture

from lumen.services import provider_client
from lumen.services.providers import accounts, probe, repository, routing

provider_db = test_chat_api_model_routing.provider_db


@pytest.fixture
def probe_authority(monkeypatch):
    authority = AsyncMock()
    monkeypatch.setattr(probe, "_authorize_admin", authority)
    return authority


def capabilities(monkeypatch, structured=False, budget=True):
    monkeypatch.setattr(routing, "_effective_capabilities", lambda *_a, **_kw: (
        {"structured_output": structured, "output_budget_enforced": budget}, "test"))


def response(content='{"response":"pong"}', **message):
    return {"choices": [{"message": {"content": content, **message}, "finish_reason": "stop"}]}


def path(account_id, key_id, provider_id=1):
    return f"/v1/admin/providers/{provider_id}/accounts/{account_id}/credentials/{key_id}/probe"


async def test_two_keys_exact_single_send_with_structured_then_preselected_plain(
        provider_db, admin_client, monkeypatch, probe_authority):
    account_id, first, second = await _pool(provider_db, admin_client, monkeypatch)
    await repository.update_provider(1, {"selection_policy": "explicit", "selected_credential_id": first})
    async with http_fixture({"body": response()}, {"body": response("Hello, connection works!")}) as fixture:
        await connect_runtime(monkeypatch, fixture)
        for key, structured in ((first, True), (second, False)):
            capabilities(monkeypatch, structured)
            result = await admin_client.post(path(account_id, key), json={"model_id": 10, "generation": 0})
            assert result.status_code == 200, result.text
            assert result.headers["cache-control"] == "no-store"
            assert result.json().items() >= {"status": "connected", "reason": None,
                "mode": "structured" if structured else "plain", "model_id": 10, "generation": 0}.items()
            assert result.json()["checked_at"].endswith("Z")
            assert result.json()["elapsed_ms"] >= 0
            assert set(result.json()) == {"status", "reason", "mode", "model_id", "generation", "checked_at", "elapsed_ms"}
            assert "upstream-secret" not in result.text
        assert [entry["body"]["api_key"] for entry in fixture.requests] == ["upstream-secret-A", "upstream-secret-B"]
        assert len(fixture.requests) == 2
        first_body, second_body = [entry["body"] for entry in fixture.requests]
        assert first_body["body"]["response_format"] == probe._PONG_FORMAT
        assert "response_format" not in second_body["body"]
        for envelope in (first_body, second_body):
            assert envelope["single_attempt"] is True
            assert envelope["body"]["max_tokens"] == 32
            assert envelope["body"]["stream"] is False
    assert probe_authority.await_count == 4


@pytest.mark.parametrize("status,reason", [(400, "provider_error"), (401, "auth_rejected"), (403, "auth_rejected"),
                                           (429, "provider_error"), (500, "provider_error")])
async def test_provider_rejection_sanitized_no_format_retry_or_key_fallback(
        provider_db, admin_client, monkeypatch, probe_authority, status, reason):
    account_id, first, second = await _pool(provider_db, admin_client, monkeypatch)
    capabilities(monkeypatch, True)
    async with http_fixture({"status": status, "body": {"error": {"code": "upstream_rejected",
            "message": "unsupported response_format echoed upstream-secret-A"}}}) as fixture:
        await connect_runtime(monkeypatch, fixture)
        result = await admin_client.post(path(account_id, first), json={"model_id": 10, "generation": 0})
        assert result.status_code == 200, result.text
        assert result.json()["status"] == "failed"
        assert result.json()["reason"] == reason
        assert len(fixture.requests) == 1
        assert fixture.requests[0]["body"]["api_key"] == "upstream-secret-A"
        assert "upstream-secret" not in result.text and "unsupported" not in result.text
    rows = await accounts.list_accounts(1)
    keys = {credential["id"]: credential for row in rows for credential in row["credentials"]}
    assert keys[second]["subscription_status"] == "disconnected"
    assert keys[first]["subscription_status"] == ("reauth_required" if status in {401, 403} else "disconnected")


@pytest.mark.parametrize("content,structured,message", [('', False, {}), ('{"response":"other"}', True, {}),
    ('{"response":"pong","extra":true}', True, {}), ('pong', True, {}), ('pong', False, {"refusal": "cannot"}),
    ('pong', False, {"tool_calls": [{"id": "call"}]})])
async def test_invalid_normal_or_structured_response_fails_without_disclosure(
        provider_db, admin_client, monkeypatch, probe_authority, content, structured, message):
    account_id, first, _ = await _pool(provider_db, admin_client, monkeypatch)
    capabilities(monkeypatch, structured)
    async with http_fixture({"body": response(content, **message)}) as fixture:
        await connect_runtime(monkeypatch, fixture)
        result = await admin_client.post(path(account_id, first), json={"model_id": 10, "generation": 0})
        assert result.status_code == 200, result.text
        assert result.json()["reason"] == "invalid_response"
        assert len(fixture.requests) == 1


async def test_stale_generation_ownership_model_and_output_budget_never_send(
        provider_db, admin_client, monkeypatch, probe_authority):
    account_id, first, _ = await _pool(provider_db, admin_client, monkeypatch)
    capabilities(monkeypatch)
    send = AsyncMock()
    monkeypatch.setattr(provider_client, "acompletion", send)
    for url, body, status in [
        (path(account_id, first), {"model_id": 10, "generation": 99}, 409),
        (path(9999, first), {"model_id": 10, "generation": 0}, 404),
        (path(account_id, first), {"model_id": 9999, "generation": 0}, 404),
        (path(account_id, first), {"model_id": True, "generation": 0}, 422),
        (path(account_id, first), {"model_id": 10, "generation": 0, "api_key": "never-echo"}, 422),
    ]:
        result = await admin_client.post(url, json=body)
        assert result.status_code == status, result.text
        assert "never-echo" not in result.text
    capabilities(monkeypatch, budget=False)
    assert (await admin_client.post(path(account_id, first), json={"model_id": 10, "generation": 0})).status_code == 400
    send.assert_not_awaited()


async def test_rotation_during_send_masks_connected_response(provider_db, admin_client, monkeypatch, probe_authority):
    account_id, first, _ = await _pool(provider_db, admin_client, monkeypatch)
    capabilities(monkeypatch)
    async def rotated(**_kw):
        await accounts.update_credential(1, account_id, first, {"api_key": "rotated-secret"})
        return response("pong")
    monkeypatch.setattr(provider_client, "acompletion", rotated)
    result = await admin_client.post(path(account_id, first), json={"model_id": 10, "generation": 0})
    assert result.status_code == 409
    assert "connected" not in result.text and "secret" not in result.text


async def test_authority_revocation_before_send_fails_closed(provider_db, admin_client, monkeypatch, probe_authority):
    account_id, first, _ = await _pool(provider_db, admin_client, monkeypatch)
    capabilities(monkeypatch)
    probe_authority.side_effect = HTTPException(403, "System administration denied")
    send = AsyncMock()
    monkeypatch.setattr(provider_client, "acompletion", send)
    assert (await admin_client.post(path(account_id, first), json={"model_id": 10, "generation": 0})).status_code == 403
    send.assert_not_awaited()


async def test_project_member_cannot_probe(client, monkeypatch):
    send = AsyncMock()
    monkeypatch.setattr(provider_client, "acompletion", send)
    result = await client.post(path(1, 1), json={"model_id": 10, "generation": 0})
    assert result.status_code == 403
    send.assert_not_awaited()


async def test_timeout_returns_unknown_safe_failure_without_second_send(
        provider_db, admin_client, monkeypatch, probe_authority):
    import asyncio

    account_id, first, _ = await _pool(provider_db, admin_client, monkeypatch)
    capabilities(monkeypatch)
    monkeypatch.setattr(probe, "_TIMEOUT_SECONDS", 0.01)
    async def stalled(**_kw):
        await asyncio.sleep(1)
    send = AsyncMock(side_effect=stalled)
    monkeypatch.setattr(provider_client, "acompletion", send)
    result = await admin_client.post(path(account_id, first), json={"model_id": 10, "generation": 0})
    assert result.status_code == 200
    assert result.json()["reason"] == "timeout"
    send.assert_awaited_once()


async def test_native_probe_authority_refresh_does_not_trust_old_system_flag(monkeypatch):
    from lumen import auth, policy

    principal = {"user_id": "u", "project_id": "p", "is_system_admin": True}
    async def revoked(value):
        value["is_system_admin"] = False
    refresh = AsyncMock(side_effect=revoked)
    monkeypatch.setattr(auth, "_refresh_token_authority", refresh)
    with pytest.raises(HTTPException) as denied:
        await probe._authorize_admin(principal)
    assert denied.value.status_code == 403
    refresh.assert_awaited_once_with(principal)
    principal["is_system_admin"] = True
    refresh.side_effect = None
    monkeypatch.setattr(policy, "authorize", lambda *_a: False)
    with pytest.raises(HTTPException) as policy_denied:
        await probe._authorize_admin(principal)
    assert policy_denied.value.status_code == 403


async def test_revoked_authority_after_send_masks_completed_result(
        provider_db, admin_client, monkeypatch, probe_authority):
    account_id, first, _ = await _pool(provider_db, admin_client, monkeypatch)
    capabilities(monkeypatch)
    probe_authority.side_effect = [None, HTTPException(403, "System administration denied")]
    send = AsyncMock(return_value=response("pong"))
    monkeypatch.setattr(provider_client, "acompletion", send)
    result = await admin_client.post(path(account_id, first), json={"model_id": 10, "generation": 0})
    assert result.status_code == 403
    assert "connected" not in result.text
    send.assert_awaited_once()


@pytest.mark.parametrize("structured,content,status", [
    (False, "A normal response at the configured output cap.", "connected"),
    (True, '{"response":"pong"}', "failed"),
])
async def test_length_terminal_only_qualifies_nonempty_plain_connection(
        provider_db, admin_client, monkeypatch, probe_authority, structured, content, status):
    account_id, first, _ = await _pool(provider_db, admin_client, monkeypatch)
    capabilities(monkeypatch, structured)
    payload = response(content)
    payload["choices"][0]["finish_reason"] = "length"
    async with http_fixture({"body": payload}) as fixture:
        await connect_runtime(monkeypatch, fixture)
        result = await admin_client.post(path(account_id, first), json={"model_id": 10, "generation": 0})
        assert result.status_code == 200, result.text
        assert result.json()["status"] == status
        assert result.json()["reason"] == (None if status == "connected" else "invalid_response")
        assert len(fixture.requests) == 1
        assert fixture.requests[0]["body"]["body"]["messages"] == [{"role": "user", "content": '{"request":"ping"}' if structured else "ping"}]
