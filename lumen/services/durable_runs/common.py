"""Durable chat-run creation, journal access, and worker execution.

The HTTP API only records client intent and appends journal rows.  Provider work is
performed by ``lumen.worker`` after a conditional MySQL lease claim; SSE is a
reader of that journal and never owns provider work.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from lumen.crypto import decrypt_chat_content
from lumen.db import get_session_factory, is_db_available
from lumen.models.chat_contracts import (
    ChatRunDescriptor,
    ChatRunEvent,
    validate_chat_parts,
    validate_chat_run_event,
)
from lumen.models.chat_runs import (
    ChatRun,
)
from lumen.services.agent_policy import default_execution_policy, resolve_direct_effects
from lumen.services.agent_protocol import AgentExecutionPolicy
from lumen.services.checkpointer import chat_checkpointer
from lumen.services.execution_protocol import SUPPORTED_EXECUTION_PROTOCOL_VERSIONS, is_supported
from lumen.services.message_timestamps import message_timestamps

from .errors import DurableRunError, DurableRunInputError

logger = logging.getLogger(__name__)
_LEASE_SECONDS = 45
_V2_DEFAULT_MAX_MODEL_TURNS = 8
_V2_DEFAULT_MAX_TOOL_CALLS = 24


def _message_timestamps_for_run(run: ChatRun) -> tuple[datetime, datetime | None, str | None]:
    """Resolve the validated browser timezone frozen in a durable run payload."""
    try:
        payload = json.loads(decrypt_chat_content(run.request_payload) or "{}")
        timezone_name = payload.get("client_timezone") if isinstance(payload, dict) else None
        return message_timestamps(timezone_name if isinstance(timezone_name, str) else None)
    except (TypeError, ValueError, json.JSONDecodeError):
        return message_timestamps(None)


def _freeze_v2_tool_call_limit(request_payload: dict[str, Any], execution_protocol_version: int) -> dict[str, Any]:
    if execution_protocol_version != 2:
        return request_payload
    try:
        policy = AgentExecutionPolicy.model_validate(
            request_payload.get("execution_policy") or default_execution_policy().model_dump(mode="json")
        )
        execution_mode = str(request_payload.get("execution_mode") or "chat")
        default_effects = resolve_direct_effects(None, execution_mode=execution_mode)
        raw_effects = request_payload.get("allowed_direct_effects", default_effects)
        if not isinstance(raw_effects, (list, tuple, set, frozenset)) or not all(
            isinstance(effect, str) for effect in raw_effects
        ):
            raise ValueError("v2 direct effects are invalid")
        direct_effects = frozenset(raw_effects)
        if not direct_effects <= {"read", "workspace_write", "process", "external_mutation"}:
            raise ValueError("v2 direct effects are invalid")
    except Exception as exc:
        raise DurableRunInputError("v2 execution policy is invalid") from exc
    return {
        **request_payload,
        "execution_policy": policy.model_dump(mode="json"),
        "allowed_direct_effects": sorted(direct_effects),
        "v2_max_model_turns": policy.max_model_turns,
        "v2_max_tool_calls": policy.max_tool_calls,
    }


def _require_supported_execution_protocol_version(version: int) -> None:
    if not is_supported(version):
        raise DurableRunInputError(f"execution protocol version {version} is not deployed")
    if version == 2 and not chat_checkpointer.available:
        raise DurableRunInputError("execution protocol version 2 requires an encrypted PostgreSQL checkpointer")


async def _freeze_v2_lumen_snapshot(
    request_payload: dict[str, Any],
    *,
    user_id: str,
    project_id: str,
    execution_protocol_version: int,
) -> dict[str, Any]:
    """Freeze the selected Lumen grant at run admission, never at worker execution."""
    if execution_protocol_version != 2:
        return request_payload
    from lumen.services import mcp_adapter as mcp_lumen

    try:
        snapshot = await mcp_lumen.selected_lumen_snapshot(user_id=user_id, project_id=project_id)
    except mcp_lumen.McpLumenAuthorityError:
        snapshot = None
    return {
        **request_payload,
        "lumen_snapshot": mcp_lumen.snapshot_payload(snapshot) if snapshot is not None else None,
    }


def _tool_result_status(value: object) -> str:
    return "completed" if value in (None, "completed") else "failed"


def _tool_result_error_code(value: object) -> str | None:
    return value if isinstance(value, str) and value and len(value) <= 100 else None


def _tool_display_parts(payload: dict[str, Any], *, content: str, visible: bool) -> list[dict[str, Any]]:
    """Project a validated v2 result into durable, display-only chat parts."""
    raw_display = payload.get("display")
    parts = list(raw_display) if isinstance(raw_display, list) else []
    raw_artifacts = payload.get("artifacts")
    seen_asset_ids = {
        part.get("asset_id")
        for part in parts
        if isinstance(part, dict) and part.get("type") == "file" and isinstance(part.get("asset_id"), str)
    }
    if isinstance(raw_artifacts, list):
        for artifact in raw_artifacts:
            if not isinstance(artifact, dict):
                continue
            asset_id = artifact.get("asset_id")
            media_type = artifact.get("media_type")
            name = artifact.get("name")
            size_bytes = artifact.get("size_bytes")
            if (
                isinstance(asset_id, str)
                and asset_id
                and asset_id not in seen_asset_ids
                and isinstance(media_type, str)
                and media_type
                and isinstance(name, str)
                and name
                and isinstance(size_bytes, int)
                and size_bytes >= 0
            ):
                parts.append(
                    {
                        "type": "file",
                        "asset_id": asset_id,
                        "mime_type": media_type,
                        "name": name,
                        "size_bytes": size_bytes,
                    }
                )
                seen_asset_ids.add(asset_id)
    if not parts and visible:
        return [{"type": "text", "text": content}]
    return parts


def _validate_run_protocol_payload(run: ChatRun, payload: dict[str, Any]) -> bool:
    """Keep pre-rollout v1 payloads compatible; v2 must carry an exact version."""
    payload_version = payload.get("execution_protocol_version")
    if payload_version is None:
        return run.execution_protocol_version == 1 and is_supported(run.execution_protocol_version)
    return (
        run.execution_protocol_version in SUPPORTED_EXECUTION_PROTOCOL_VERSIONS
        and payload_version == run.execution_protocol_version
    )


def _factory():
    if not is_db_available() or (factory := get_session_factory()) is None:
        raise DurableRunError("chat DB 를 사용할 수 없습니다")
    return factory


def _now() -> datetime:
    return datetime.now(UTC)


_TEMP_THREAD_RETENTION = timedelta(days=30)


def _event(run: ChatRun, event_type: str, payload: dict[str, Any]) -> ChatRunEvent:
    seq = run.last_seq + 1
    return validate_chat_run_event(
        {
            "event_id": f"{run.id}:{seq}",
            "run_id": run.id,
            "seq": seq,
            "type": event_type,
            "created_at": _now(),
            "payload": payload,
        }
    )


def _canonical_intent(value: Any, *, key: str | None = None) -> Any:
    if isinstance(value, dict):
        return {name: _canonical_intent(item, key=name) for name, item in sorted(value.items())}
    if isinstance(value, list):
        normalized = [_canonical_intent(item) for item in value]
        if key in {"allowed_domains", "blocked_domains"}:
            return sorted(normalized)
        return normalized
    return value


def _fingerprint(intent: dict[str, Any]) -> str:
    canonical = json.dumps(_canonical_intent(intent), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def descriptor(run: ChatRun) -> ChatRunDescriptor:
    return ChatRunDescriptor(
        run_id=run.id,
        conversation_id=run.conversation_id,
        temp_thread_id=run.temp_thread_id,
        status=run.status,
        events_url=f"/v1/runs/{run.id}/events",
        cancel_url=f"/v1/runs/{run.id}/cancel",
    )


def _payload(run: ChatRun) -> dict[str, Any]:
    if not run.request_payload:
        raise DurableRunError("chat run request payload is unavailable")
    try:
        value = json.loads(decrypt_chat_content(run.request_payload))
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise DurableRunError("chat run request payload is invalid") from exc
    if not isinstance(value, dict):
        raise DurableRunError("chat run request payload is invalid")
    return value


def _provider_response_format(features: object) -> dict[str, Any] | None:
    """Project validated canonical response options to LiteLLM's public request shape."""
    if not isinstance(features, dict):
        raise DurableRunError("chat run feature payload is invalid")
    response_format = features.get("response_format")
    if response_format is None:
        return None
    if not isinstance(response_format, dict):
        raise DurableRunError("chat run response format is invalid")
    kind = response_format.get("kind")
    if kind == "text":
        return None
    if kind == "json_object":
        return {"type": "json_object"}
    if kind == "json_schema":
        name = response_format.get("name")
        schema = response_format.get("schema")
        if not isinstance(name, str) or not isinstance(schema, dict):
            raise DurableRunError("chat run JSON schema format is invalid")
        return {"type": "json_schema", "json_schema": {"name": name, "strict": True, "schema": schema}}
    raise DurableRunError("chat run response format is invalid")


def _tools_enabled(features: object) -> bool:
    if not isinstance(features, dict):
        raise DurableRunError("chat run feature payload is invalid")
    policy = features.get("tool_policy")
    return not isinstance(policy, dict) or policy.get("mode", "agent_default") != "none"


def _selected_tool_ids(features: object) -> tuple[int, ...] | None:
    """v1 compatibility only; v2 runs persist an encrypted extension snapshot."""
    if not isinstance(features, dict):
        raise DurableRunError("chat run feature payload is invalid")
    policy = features.get("tool_policy")
    if not isinstance(policy, dict):
        return None
    if policy.get("mode", "agent_default") == "none":
        return ()
    ids = policy.get("enabled_tool_ids")
    if ids is None:
        return None
    if not isinstance(ids, list):
        raise DurableRunError("chat run enabled tool IDs are invalid")
    normalized: list[int] = []
    for item in ids:
        if isinstance(item, int) and item > 0:
            normalized.append(item)
        elif isinstance(item, str) and item.isdecimal() and int(item) > 0:
            normalized.append(int(item))
        else:
            raise DurableRunError("chat run enabled tool IDs are invalid")
    return tuple(normalized)


def _selected_mcp_credential_versions(
    snapshot: object, selected_mcp_ids: tuple[int, ...]
) -> tuple[tuple[int, int], ...]:
    """Project immutable expected MCP credential versions for every later tool dispatch."""
    if not isinstance(snapshot, dict):
        return ()
    selected = set(selected_mcp_ids)
    versions: list[tuple[int, int]] = []
    for item in snapshot.get("mcp", []):
        if not isinstance(item, dict):
            continue
        server_id = item.get("id")
        version = item.get("credential_version")
        if isinstance(server_id, int) and server_id in selected and isinstance(version, int):
            versions.append((server_id, version))
    return tuple(versions)


def _temporary_history_messages(history_ciphertext: str) -> list[dict[str, str]]:
    """Validate encrypted temporary history and project only provider-safe text turns."""
    try:
        history = json.loads(decrypt_chat_content(history_ciphertext))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise DurableRunError("temporary chat history is invalid") from exc
    if not isinstance(history, list):
        raise DurableRunError("temporary chat history is invalid")

    messages: list[dict[str, str]] = []
    for turn in history:
        if not isinstance(turn, dict) or turn.get("role") not in {"user", "assistant"}:
            raise DurableRunError("temporary chat history is invalid")
        try:
            parts = validate_chat_parts(turn.get("parts"))
        except (TypeError, ValueError) as exc:
            raise DurableRunError("temporary chat history is invalid") from exc
        text = "\n".join(part.text for part in parts if part.type == "text")
        if text:
            messages.append({"role": turn["role"], "content": text})
    return messages


async def wake_run(run_id: str) -> None:
    """Best-effort post-commit wakeup; MySQL scanning remains authoritative."""
    try:
        from lumen.cache import _get_redis

        redis = await _get_redis()
        await redis.lpush("afterglow:chat:runs", run_id)
    except Exception:
        # A missed wakeup only adds up to the worker's bounded DB-poll delay.
        return
