"""WebSocket API key scope and provider setup enforce route ownership before connection."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from lumen.api.compat import realtime as compat


def test_realtime_websocket_requires_scoped_key_and_frozen_gemini_setup(monkeypatch):
    app = FastAPI()
    app.include_router(compat.router)
    verify = AsyncMock(return_value={"user_id": "owner", "project_id": "project", "api_key_id": 37,
                                      "scopes": ["compat:realtime:write"]})
    monkeypatch.setattr(compat.api_key_store, "verify_key", verify)
    monkeypatch.setattr(compat, "get_settings", lambda: SimpleNamespace(chat_api_hosts="testserver"))
    monkeypatch.setattr(compat, "origin_allowed", lambda _websocket: True)
    admission = AsyncMock(return_value={"session_id": str(uuid4()), "connect_token": "one-use-token"})
    monkeypatch.setattr(compat, "admit_realtime_session", admission)

    async def connected(websocket, **kwargs):
        await websocket.send_json({"type": "connected", "wire": kwargs["wire"]})
    monkeypatch.setattr(compat, "run_realtime_session", connected)
    with TestClient(app) as browser:
        with browser.websocket_connect("/v1/realtime?model=gpt-realtime", headers={
            "Authorization": "Bearer scoped-key"}) as socket:
            assert socket.receive_json() == {"type": "connected", "wire": "openai"}
        verify.assert_awaited_with("scoped-key")
        assert admission.call_args.args[0] == {"model_id": "gpt-realtime", "voice": None, "instructions": None}
        with browser.websocket_connect("/v1/realtime?model=gpt-realtime", subprotocols=[
            "realtime", "openai-insecure-api-key.scoped-key"]) as socket:
            assert socket.accepted_subprotocol == "realtime"
            assert socket.receive_json() == {"type": "connected", "wire": "openai"}
        verify.assert_awaited_with("scoped-key")
        with browser.websocket_connect("/v1beta/realtime", headers={"x-api-key": "scoped-key"}) as socket:
            socket.send_json({"setup": {"model": "models/gemini-2.5-flash-live", "generationConfig": {
                "responseModalities": ["AUDIO"], "speechConfig": {
                    "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": "Kore"}}}},
                "systemInstruction": {"parts": [{"text": "respond politely"}]}}})
            assert socket.receive_json() == {"type": "connected", "wire": "gemini"}
        assert admission.call_args.args[0] == {"model_id": "gemini-2.5-flash-live", "voice": "Kore",
                                               "instructions": "respond politely"}
        calls = admission.await_count
        with browser.websocket_connect("/v1beta/realtime?model=another-model", headers={"x-api-key": "scoped-key"}) as socket:
            socket.send_json({"setup": {"model": "models/gemini-2.5-flash-live"}})
            try:
                socket.receive_json()
                raise AssertionError("mismatched setup must close")
            except WebSocketDisconnect as exc:
                assert exc.code == 4400
        assert admission.await_count == calls
        verify.return_value = {"user_id": "owner", "project_id": "project", "api_key_id": 37,
                               "scopes": ["compat:audio:write"]}
        try:
            with browser.websocket_connect("/v1/realtime?model=gpt-realtime", headers={
                "Authorization": "Bearer scoped-key"}):
                raise AssertionError("unscoped key must be rejected")
        except WebSocketDisconnect as exc:
            assert exc.code == 4401
        assert admission.await_count == calls
