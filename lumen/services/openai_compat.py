"""OpenAI virtual model service logic for 'lumen' model."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from lumen.config import get_settings
from lumen.models.chat_contracts import ChatFeatureOptions, ToolPolicy
from lumen.services import completion_api as core
from lumen.services.chat_admission import _capability_extension_snapshot, _run_snapshots
from lumen.services.durable_runs import admission, lifecycle, queries
from lumen.services.durable_runs.common import _require_supported_execution_protocol_version
from lumen.services.durable_runs.errors import DurableRunError
from lumen.services.providers import repository

logger = logging.getLogger(__name__)

VIRTUAL_MODEL_ID = "lumen"
_DEFAULT_COMPAT_TIMEOUT_SECONDS = 300
_MAX_TOKENS_CAP = 4096
_MAX_MESSAGE_CHARS = 32000
_MAX_TOTAL_CHARS = 128000
_MAX_MESSAGES = 100
KEEPALIVE_INTERVAL_SECONDS = 15.0
_POLL_INTERVAL_SECONDS = 0.25


class OpenAICompatError(Exception):
    def __init__(
        self,
        status_code: int,
        message: str,
        type_: str | None = None,
        code: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.message = message
        self.type_ = type_ or ("invalid_request_error" if status_code < 500 else "api_error")
        self.code = code
        super().__init__(message)


def is_lumen_virtual_model(model_name: str) -> bool:
    return model_name.strip().lower() == VIRTUAL_MODEL_ID


def get_configured_default_model() -> str:
    settings = get_settings()
    default_model = getattr(settings, "chat_default_model", "").strip()
    if default_model.lower() == VIRTUAL_MODEL_ID:
        raise OpenAICompatError(503, "Configured chat_default_model cannot be reserved virtual model 'lumen'")
    return default_model


def get_compat_timeout_seconds() -> int:
    settings = get_settings()
    timeout = getattr(settings, "chat_compat_run_timeout_seconds", _DEFAULT_COMPAT_TIMEOUT_SECONDS)
    try:
        val = int(timeout)
        return max(1, min(val, 3600))
    except (TypeError, ValueError):
        return _DEFAULT_COMPAT_TIMEOUT_SECONDS


def get_execution_protocol_version() -> int:
    settings = get_settings()
    version = getattr(settings, "chat_execution_protocol_version", 1)
    try:
        val = int(version)
        _require_supported_execution_protocol_version(val)
        return val
    except Exception as exc:
        raise OpenAICompatError(503, "Configured chat execution protocol is not supported") from exc


async def is_virtual_model_active(models: list[dict] | None = None) -> bool:
    try:
        default_model = get_configured_default_model()
    except OpenAICompatError:
        return False
    if not default_model:
        return False
    try:
        if models is None:
            models = await repository.list_models(active_only=True)
        return any(m.get("model_name") == default_model for m in models)
    except Exception:
        return False


def validate_and_normalize_transcript(
    messages: list[dict],
    tools: Any = None,
    tool_choice: Any = None,
    max_tokens: int | None = None,
    temperature: float | None = None,
) -> tuple[list[dict], str, int | None, float | None]:
    """Validate OpenAI transcript rules for model=lumen and return normalized parameters."""
    if tools is not None or tool_choice is not None:
        raise OpenAICompatError(400, "tools and tool_choice are not supported for virtual model 'lumen'")

    if not isinstance(messages, list) or not messages:
        raise OpenAICompatError(400, "messages must be a non-empty list")

    if len(messages) > _MAX_MESSAGES:
        raise OpenAICompatError(400, f"Too many messages in transcript (maximum {_MAX_MESSAGES} allowed)")

    if max_tokens is not None:
        if max_tokens <= 0:
            raise OpenAICompatError(400, "max_tokens must be a positive integer")
        max_tokens = min(max_tokens, _MAX_TOKENS_CAP)

    if temperature is not None and (temperature < 0.0 or temperature > 2.0):
        raise OpenAICompatError(400, "temperature must be between 0.0 and 2.0")

    normalized_messages: list[dict] = []
    total_chars = 0

    for idx, msg in enumerate(messages):
        if not isinstance(msg, dict):
            raise OpenAICompatError(400, f"message[{idx}] must be a dict")
        role = msg.get("role")
        content = msg.get("content")

        if role in ("tool", "function") or msg.get("tool_calls"):
            raise OpenAICompatError(400, "tool messages and tool_calls are not supported for virtual model 'lumen'")

        if role not in ("system", "developer", "user", "assistant"):
            raise OpenAICompatError(400, f"Unsupported message role '{role}' for virtual model 'lumen'")

        if not isinstance(content, str):
            raise OpenAICompatError(
                400, "Multimodal or non-string message content is not supported for virtual model 'lumen'"
            )

        if len(content) > _MAX_MESSAGE_CHARS:
            raise OpenAICompatError(400, f"Message content exceeds maximum allowed length of {_MAX_MESSAGE_CHARS} characters")

        total_chars += len(content)

        norm_role = "system" if role == "developer" else role
        normalized_messages.append({"role": norm_role, "content": content})

    if total_chars > _MAX_TOTAL_CHARS:
        raise OpenAICompatError(400, f"Total transcript content exceeds maximum allowed length of {_MAX_TOTAL_CHARS} characters")

    last_msg = normalized_messages[-1]
    if last_msg["role"] != "user" or not last_msg["content"].strip():
        raise OpenAICompatError(400, "The transcript must end with a non-empty user message")

    return normalized_messages, last_msg["content"], max_tokens, temperature


async def create_lumen_temp_run(
    *,
    normalized_messages: list[dict],
    last_user_text: str,
    user_id: str,
    project_id: str,
    api_key_id: int | None,
    source: str,
    max_tokens: int | None = None,
    temperature: float | None = None,
) -> tuple[str, str]:
    """Create a durable temporary run for model=lumen. Return (run_id, resolved_model_name)."""
    default_model = get_configured_default_model()
    if not default_model:
        raise OpenAICompatError(503, "No backing default model configured for virtual model 'lumen'")

    protocol_version = get_execution_protocol_version()

    try:
        resolved = await core.resolve(default_model)
        await core.precheck(user_id, project_id, api_key_id=api_key_id)
    except core.CompletionError as exc:
        raise OpenAICompatError(exc.status_code, exc.message) from exc

    features_obj = ChatFeatureOptions(memory=False, tool_policy=ToolPolicy(mode="none"))
    features = features_obj.model_dump(mode="json", by_alias=True)

    capability_snapshot, pricing_snapshot = _run_snapshots(resolved, features, feature_routes={})
    capability_snapshot["extensions"] = _capability_extension_snapshot({"tools": [], "mcp": []})
    capability_snapshot["execution_protocol_version"] = protocol_version

    client_request_id = str(uuid.uuid4())
    intent = {"model": VIRTUAL_MODEL_ID, "messages": normalized_messages}
    input_parts = [{"type": "text", "text": last_user_text}]

    try:
        descriptor = await admission.create_temp_run(
            project_id=project_id,
            user_id=user_id,
            client_request_id=client_request_id,
            intent=intent,
            temp_thread_id=None,
            model_name=resolved["model_name"],
            request_payload={
                "input_messages": normalized_messages,
                "input_parts": input_parts,
                "features": features,
                "max_tokens": max_tokens or _MAX_TOKENS_CAP,
                "temperature": temperature,
                "reasoning_effort": "auto",
                "extension_snapshot": {"tools": [], "mcp": []},
            },
            capability_snapshot=capability_snapshot,
            pricing_snapshot=pricing_snapshot,
            execution_protocol_version=protocol_version,
            source=source,
            api_key_id=api_key_id,
        )
    except DurableRunError:
        raise OpenAICompatError(503, "Service temporarily unavailable")
    except OpenAICompatError:
        raise
    except Exception:
        raise OpenAICompatError(503, "Service temporarily unavailable")

    return descriptor.run_id, resolved["model_name"]


async def execute_lumen_nonstream(
    *,
    run_id: str,
    user_id: str,
    project_id: str,
    timeout_seconds: int | None = None,
    is_disconnected: Callable[[], Awaitable[bool]] | None = None,
) -> dict:
    """Poll journal events for non-stream completion. Return dict with content and usage."""
    if timeout_seconds is None:
        timeout_seconds = get_compat_timeout_seconds()

    start_time = asyncio.get_running_loop().time()
    after_seq = 0
    text_content = ""
    usage = {"prompt_tokens": 0, "completion_tokens": 0}
    run_completed = False
    run_terminal = False

    try:
        while True:
            if is_disconnected is not None and await is_disconnected():
                raise OpenAICompatError(499, "Client disconnected")

            elapsed = asyncio.get_running_loop().time() - start_time
            if elapsed > timeout_seconds:
                raise OpenAICompatError(504, "Request timed out waiting for virtual model execution", type_="timeout_error")

            try:
                events, terminal = await queries.owned_events(
                    run_id=run_id,
                    project_id=project_id,
                    user_id=user_id,
                    after_seq=after_seq,
                )
            except Exception:
                raise OpenAICompatError(503, "Service temporarily unavailable")

            for event in events:
                after_seq = max(after_seq, event.seq)
                event_type = event.type
                payload = event.payload.model_dump()

                if event_type == "part.delta":
                    if payload.get("part_type") == "text":
                        text_content += payload.get("delta", "")
                elif event_type == "usage.updated":
                    usage["prompt_tokens"] = payload.get("prompt_tokens", 0)
                    usage["completion_tokens"] = payload.get("completion_tokens", 0)
                elif event_type == "run.completed":
                    run_completed = True
                    run_terminal = True
                elif event_type == "run.failed":
                    run_terminal = True
                    safe_msg = payload.get("safe_message") or "Durable run execution failed"
                    raise OpenAICompatError(500, safe_msg)
                elif event_type == "run.canceled":
                    run_terminal = True
                    safe_msg = payload.get("safe_message") or "Durable run was canceled"
                    raise OpenAICompatError(500, safe_msg)

            if terminal:
                run_terminal = True
                if not run_completed:
                    raise OpenAICompatError(500, "Durable run terminated without completion")
                break

            await asyncio.sleep(_POLL_INTERVAL_SECONDS)

        return {
            "model": VIRTUAL_MODEL_ID,
            "content": text_content,
            "finish_reason": "stop",
            "prompt_tokens": usage["prompt_tokens"],
            "completion_tokens": usage["completion_tokens"],
        }
    finally:
        if not run_terminal:
            try:
                await lifecycle.request_cancelled(run_id=run_id, project_id=project_id, user_id=user_id)
            except Exception:
                pass


async def execute_lumen_stream(
    *,
    run_id: str,
    user_id: str,
    project_id: str,
    cmpl_id: str,
    created: int,
    include_usage: bool = False,
    timeout_seconds: int | None = None,
    is_disconnected: Callable[[], Awaitable[bool]] | None = None,
) -> AsyncIterator[dict]:
    """Yield dict chunks or errors for streaming model=lumen. Caller converts dicts to _sse()."""
    if timeout_seconds is None:
        timeout_seconds = get_compat_timeout_seconds()

    start_time = asyncio.get_running_loop().time()
    after_seq = 0
    usage = {"prompt_tokens": 0, "completion_tokens": 0}
    run_finished = False
    run_completed = False
    last_event_time = start_time

    # 1. Emit assistant role chunk
    yield {
        "kind": "chunk",
        "data": {
            "id": cmpl_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": VIRTUAL_MODEL_ID,
            "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}],
        },
    }

    try:
        while not run_finished:
            if is_disconnected is not None and await is_disconnected():
                logger.info("Client disconnected during streaming for run %s", run_id)
                return

            now = asyncio.get_running_loop().time()
            elapsed = now - start_time
            if elapsed > timeout_seconds:
                yield {
                    "kind": "error",
                    "data": {
                        "error": {
                            "message": "Request timed out waiting for virtual model execution",
                            "type": "timeout_error",
                            "code": None,
                        }
                    },
                }
                return

            try:
                events, terminal = await queries.owned_events(
                    run_id=run_id,
                    project_id=project_id,
                    user_id=user_id,
                    after_seq=after_seq,
                )
            except Exception:
                yield {
                    "kind": "error",
                    "data": {
                        "error": {
                            "message": "Service temporarily unavailable",
                            "type": "api_error",
                            "code": None,
                        }
                    },
                }
                return

            for event in events:
                after_seq = max(after_seq, event.seq)
                event_type = event.type
                payload = event.payload.model_dump()
                last_event_time = now

                if event_type == "part.delta":
                    if payload.get("part_type") == "text":
                        delta_text = payload.get("delta", "")
                        if delta_text:
                            yield {
                                "kind": "chunk",
                                "data": {
                                    "id": cmpl_id,
                                    "object": "chat.completion.chunk",
                                    "created": created,
                                    "model": VIRTUAL_MODEL_ID,
                                    "choices": [
                                        {"index": 0, "delta": {"content": delta_text}, "finish_reason": None}
                                    ],
                                },
                            }
                elif event_type == "usage.updated":
                    usage["prompt_tokens"] = payload.get("prompt_tokens", 0)
                    usage["completion_tokens"] = payload.get("completion_tokens", 0)
                elif event_type in ("run.failed", "run.canceled"):
                    run_finished = True
                    safe_msg = payload.get("safe_message") or "Durable run execution failed"
                    yield {
                        "kind": "error",
                        "data": {
                            "error": {
                                "message": safe_msg,
                                "type": "api_error",
                                "code": None,
                            }
                        },
                    }
                    return
                elif event_type == "run.completed":
                    run_completed = True

            if terminal:
                run_finished = True
                if not run_completed:
                    yield {
                        "kind": "error",
                        "data": {
                            "error": {
                                "message": "Durable run terminated without completion",
                                "type": "api_error",
                                "code": None,
                            }
                        },
                    }
                    return

                # Emit stop chunk
                yield {
                    "kind": "chunk",
                    "data": {
                        "id": cmpl_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": VIRTUAL_MODEL_ID,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    },
                }
                # Emit usage chunk if requested
                if include_usage:
                    yield {
                        "kind": "chunk",
                        "data": {
                            "id": cmpl_id,
                            "object": "chat.completion.chunk",
                            "created": created,
                            "model": VIRTUAL_MODEL_ID,
                            "choices": [],
                            "usage": {
                                "prompt_tokens": usage["prompt_tokens"],
                                "completion_tokens": usage["completion_tokens"],
                                "total_tokens": usage["prompt_tokens"] + usage["completion_tokens"],
                            },
                        },
                    }
                yield {"kind": "done"}
                return

            if not events and (now - last_event_time) >= KEEPALIVE_INTERVAL_SECONDS:
                yield {"kind": "keepalive"}
                last_event_time = now

            await asyncio.sleep(_POLL_INTERVAL_SECONDS)

    finally:
        if not run_finished:
            try:
                await lifecycle.request_cancelled(run_id=run_id, project_id=project_id, user_id=user_id)
            except Exception:
                pass
