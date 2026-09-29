"""API-key-only OpenAI Realtime and Gemini Live WebSocket compatibility gateways."""

from __future__ import annotations

import asyncio
from uuid import uuid4

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from lumen.config import get_settings
from lumen.services import api_key_store
from lumen.services.durable_runs.realtime import admit_realtime_session, run_realtime_session

from ..realtime import origin_allowed

router = APIRouter()


async def _principal(websocket: WebSocket) -> dict | None:
    hosts = {host.strip().lower() for host in get_settings().chat_api_hosts.split(",") if host.strip()}
    if hosts and websocket.headers.get("host", "").split(":")[0].lower() not in hosts:
        return None
    if not origin_allowed(websocket):
        return None
    authorization = websocket.headers.get("authorization", "")
    bearer = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
    insecure = [item.strip().removeprefix("openai-insecure-api-key.") for part in
                websocket.headers.get("sec-websocket-protocol", "").split(",")
                if (item := part.strip()).startswith("openai-insecure-api-key.")]
    header = websocket.headers.get("x-api-key", "").strip()
    keys = [part for part in (bearer, header, *(item.strip() for item in insecure)) if part]
    if len(keys) != 1:
        return None
    info = await api_key_store.verify_key(keys[0])
    if info is None or "compat:realtime:write" not in info.get("scopes", ()):
        return None
    return info


async def _compat_socket(websocket: WebSocket, *, wire: str, provider: str):
    principal = await _principal(websocket)
    if principal is None:
        await websocket.close(code=4401)
        return
    model = websocket.query_params.get("model")
    voice = websocket.query_params.get("voice")
    instructions = None
    offered = [part.strip() for part in websocket.headers.get("sec-websocket-protocol", "").split(",")]
    await websocket.accept(subprotocol="realtime" if wire == "openai" and "realtime" in offered else None)
    if wire == "gemini":
        try:
            initial = await asyncio.wait_for(websocket.receive_json(), timeout=10)
            setup = initial.get("setup") if isinstance(initial, dict) else None
            name = setup.get("model") if isinstance(setup, dict) else None
            if set(setup) - {"model", "generationConfig", "systemInstruction", "inputAudioTranscription", "outputAudioTranscription"}:
                raise ValueError("unsupported Gemini setup")
            if not isinstance(name, str):
                raise ValueError("Gemini setup model is required")
            setup_model = name.removeprefix("models/")
            if model is not None and model != setup_model:
                raise ValueError("Gemini model does not match setup")
            model = setup_model
            generation = setup.get("generationConfig", {})
            if not isinstance(generation, dict) or set(generation) - {"responseModalities", "speechConfig"}:
                raise ValueError("unsupported Gemini generation config")
            if generation.get("responseModalities", ["AUDIO"]) != ["AUDIO"]:
                raise ValueError("Gemini session must output audio")
            speech = generation.get("speechConfig", {})
            if not isinstance(speech, dict) or set(speech) - {"voiceConfig"}:
                raise ValueError("unsupported Gemini speech config")
            config_voice = speech.get("voiceConfig", {}).get("prebuiltVoiceConfig", {}).get("voiceName")
            if config_voice is not None:
                if voice is not None and voice != config_voice:
                    raise ValueError("Gemini voice does not match setup")
                voice = config_voice
            instruction = setup.get("systemInstruction")
            if instruction is not None:
                parts = instruction.get("parts") if isinstance(instruction, dict) else None
                if not isinstance(parts, list) or len(parts) != 1 or not isinstance(parts[0], dict):
                    raise ValueError("unsupported Gemini instructions")
                text = parts[0].get("text")
                if not isinstance(text, str):
                    raise ValueError("unsupported Gemini instructions")
                instructions = text
        except (ValueError, TypeError, AttributeError, TimeoutError, WebSocketDisconnect):
            await websocket.close(code=4400)
            return
    if not model or len(model) > 190:
        await websocket.close(code=4400)
        return
    try:
        result = await admit_realtime_session({"model_id": model, "provider_id": provider,
                                               "voice": voice, "instructions": instructions},
            project_id=principal["project_id"], user_id=principal["user_id"],
            client_request_id=str(uuid4()), source="api", api_key_id=principal["api_key_id"])
        await run_realtime_session(websocket, run_id=result["session_id"], token=result["connect_token"], wire=wire)
    except WebSocketDisconnect:
        pass
    except Exception:
        await websocket.close(code=1011)
    else:
        await websocket.close(code=1000)


@router.websocket("/v1/realtime")
async def openai_realtime(websocket: WebSocket):
    await _compat_socket(websocket, wire="openai", provider="openai")


@router.websocket("/v1beta/realtime")
async def gemini_live(websocket: WebSocket):
    await _compat_socket(websocket, wire="gemini", provider="gemini")
