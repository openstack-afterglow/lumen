"""Durable chat-run creation, journal access, and worker execution.

The HTTP API only records client intent and appends journal rows.  Provider work is
performed by ``lumen.worker`` after a conditional MySQL lease claim; SSE is a
reader of that journal and never owns provider work.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from time import monotonic
from typing import Any

from sqlalchemy import delete, select, update

from lumen.config import get_settings
from lumen.db import get_session_factory, is_db_available
from lumen.models.chat_agent_platform import ChatRunInteraction
from lumen.models.chat_assets import ChatAsset, ChatMessageAsset, ChatRunAsset
from lumen.models.chat_contracts import (
    ChatRunDescriptor,
    ChatRunEvent,
    ChatRunResponse,
    validate_chat_parts,
    validate_chat_run_event,
    validate_user_input_parts,
)
from lumen.models.chat_db import ChatConversation, ChatMessage
from lumen.models.chat_runs import (
    ChatRun,
    ChatRunEventRow,
    ChatRunProvider,
    ChatRunSegment,
    ChatRunTurn,
    ChatTempThread,
    ChatToolApproval,
)
from lumen.services import assets, credit, engine, extensions_store, ssrf
from lumen.services import conversation_store as cs
from lumen.services import provider_store as ps
from lumen.services.agent_policy import default_execution_policy, resolve_direct_effects
from lumen.services.agent_protocol import AgentExecutionPolicy
from lumen.services.checkpointer import chat_checkpointer
from lumen.services.execution_protocol import SUPPORTED_EXECUTION_PROTOCOL_VERSIONS, is_supported, v2_runtime_ready
from lumen.services.litellm_client import UsageCost
from lumen.services.memory_jobs import enqueue_completed_run_in_transaction
from lumen.services.message_parts import serialize_parts
from lumen.services.message_timestamps import message_timestamps
from lumen.services.run_protocol_v2 import transition_allowed
from lumen.services.run_store import (
    NONTERMINAL,
    RunStoreError,
    append_event,
    begin_segment_io,
    claim_queued_run,
    complete_segment_io,
    fail_unresolved_segment,
    load_owned_run,
    load_segment_payload,
    prepare_segment,
    renew_run_lease,
    replay_events,
)
from lumen.services.structured_output import StructuredOutputError, parse_structured_output
from lumen.crypto import decrypt_chat_content, derive_encryption_subkey, encrypt_chat_content

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


class DurableRunError(RuntimeError):
    pass


class DurableRunProtocolMismatch(DurableRunError):
    """A queued payload cannot safely be interpreted by this worker."""


class DurableRunInputError(DurableRunError):
    pass


class DurableRunLeaseLost(DurableRunError):
    """The worker must stop before a stale lease holder writes another event."""


class DurableRunProviderResultUnknown(DurableRunError):
    """An external provider call may have completed, but no result is durable."""

    pass


class DurableRunConflict(DurableRunError):
    pass


class DurableRunNotFound(DurableRunError):
    pass


class DurableRunCursorExpired(DurableRunError):
    pass


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
        events_url=f"/api/v1/chat/runs/{run.id}/events",
        cancel_url=f"/api/v1/chat/runs/{run.id}/cancel",
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


def _normalize_interaction_response(response_schema: object, response: object) -> dict[str, Any]:
    if not isinstance(response_schema, dict) or not isinstance(response, dict):
        raise DurableRunInputError("interaction response is invalid")
    option_ids = response.get("option_ids", [])
    text = response.get("text")
    allowed_option_ids = response_schema.get("option_ids", [])
    allow_multiple = response_schema.get("allow_multiple")
    allow_text = response_schema.get("allow_text")
    if (
        not isinstance(option_ids, list)
        or len(option_ids) > 5
        or not all(isinstance(option_id, str) and option_id for option_id in option_ids)
        or len(set(option_ids)) != len(option_ids)
        or not isinstance(allowed_option_ids, list)
        or len(allowed_option_ids) > 5
        or not all(isinstance(option_id, str) and option_id for option_id in allowed_option_ids)
        or len(set(allowed_option_ids)) != len(allowed_option_ids)
        or not isinstance(allow_multiple, bool)
        or not isinstance(allow_text, bool)
    ):
        raise DurableRunInputError("interaction response is invalid")
    if not set(option_ids) <= set(allowed_option_ids):
        raise DurableRunInputError("interaction response selects an unknown option")
    if option_ids and not allow_multiple and len(option_ids) != 1:
        raise DurableRunInputError("interaction response must select exactly one option")
    if text is not None and (not isinstance(text, str) or len(text) > 4_000):
        raise DurableRunInputError("interaction response text is invalid")
    if text is not None and not allow_text:
        raise DurableRunInputError("interaction response does not allow text")
    if not option_ids and not (text and text.strip()):
        raise DurableRunInputError("interaction response cannot be empty")
    return {"option_ids": sorted(option_ids), "text": text}


def _approval_resolved_payload(approval: ChatToolApproval, decision: str) -> dict[str, Any]:
    if approval.decided_at is None:
        raise DurableRunError("resolved approval is missing its decision timestamp")
    return {
        "call_id": approval.call_id,
        "decision": decision,
        "decided_by_user_id": approval.decided_by_user_id,
        "decided_at": approval.decided_at,
    }


_APPROVAL_HMAC_DOMAIN = b"chat-tool-approval-v1"


def _approval_timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _approval_preview_fingerprint(parts: list[dict[str, Any]]) -> str:
    canonical = json.dumps(parts, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _canonical_approval_dispatch(
    *,
    run_id: str,
    owner_user_id: str,
    project_id: str,
    call_id: str,
    name: str,
    arguments: dict[str, Any],
    source: str,
    effect: str,
    tool_definition_hash: str,
    config_fingerprint: str | None,
    destination_origin: str | None,
    preview_fingerprint: str,
    expires_at: datetime,
):
    return json.dumps(
        {
            "run_id": run_id,
            "owner_user_id": owner_user_id,
            "project_id": project_id,
            "call_id": call_id,
            "name": name,
            "arguments": arguments,
            "source": source,
            "effect": effect,
            "tool_definition_hash": tool_definition_hash,
            "config_fingerprint": config_fingerprint,
            "destination_origin": destination_origin,
            "preview_fingerprint": preview_fingerprint,
            "expires_at": _approval_timestamp(expires_at),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _approval_dispatch_hmac(**kwargs: Any) -> str:
    return hmac.new(
        derive_encryption_subkey(_APPROVAL_HMAC_DOMAIN), _canonical_approval_dispatch(**kwargs), hashlib.sha256
    ).hexdigest()


def _approval_decision_hmac(
    *,
    dispatch_hmac: str,
    owner_user_id: str,
    project_id: str,
    call_id: str,
    decision: str,
    decided_by_user_id: str | None,
    decided_at: datetime,
) -> str:
    return hmac.new(
        derive_encryption_subkey(_APPROVAL_HMAC_DOMAIN),
        json.dumps(
            {
                "dispatch_hmac": dispatch_hmac,
                "owner_user_id": owner_user_id,
                "project_id": project_id,
                "call_id": call_id,
                "decision": decision,
                "decided_by_user_id": decided_by_user_id,
                "decided_at": _approval_timestamp(decided_at),
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _validated_approval_dispatch_hmac(*, run: ChatRun, approval: ChatToolApproval) -> str:
    if (
        not approval.arguments_ciphertext
        or not approval.dispatch_hmac
        or not isinstance(approval.preview_fingerprint, str)
        or len(approval.preview_fingerprint) != 64
    ):
        raise DurableRunError("v2 approval dispatch identity is invalid")
    try:
        arguments = json.loads(decrypt_chat_content(approval.arguments_ciphertext))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise DurableRunError("v2 approval arguments are unreadable") from exc
    if not isinstance(arguments, dict):
        raise DurableRunError("v2 approval dispatch identity is invalid")
    expected_hmac = _approval_dispatch_hmac(
        run_id=run.id,
        owner_user_id=run.user_id,
        project_id=run.project_id,
        call_id=approval.call_id,
        name=approval.tool_name,
        arguments=arguments,
        source=approval.source or "",
        effect=approval.effect or "",
        tool_definition_hash=approval.tool_definition_hash or "",
        config_fingerprint=approval.config_fingerprint,
        destination_origin=approval.destination_origin,
        preview_fingerprint=approval.preview_fingerprint,
        expires_at=approval.expires_at,
    )
    if not hmac.compare_digest(approval.dispatch_hmac, expected_hmac):
        raise DurableRunError("v2 approval dispatch identity changed")
    return expected_hmac


def _validated_approval_decision_hmac(
    *,
    run: ChatRun,
    approval: ChatToolApproval,
    decision: str,
    dispatch_hmac: str,
) -> None:
    if not approval.decision_hmac or approval.decided_at is None:
        raise DurableRunError("v2 approval decision identity is invalid")
    expected_hmac = _approval_decision_hmac(
        dispatch_hmac=dispatch_hmac,
        owner_user_id=run.user_id,
        project_id=run.project_id,
        call_id=approval.call_id,
        decision=decision,
        decided_by_user_id=approval.decided_by_user_id,
        decided_at=approval.decided_at,
    )
    if not hmac.compare_digest(approval.decision_hmac, expected_hmac):
        raise DurableRunError("v2 approval decision identity changed")


def _redact_approval_arguments(arguments: dict[str, Any]) -> dict[str, str]:
    return {str(key): "[REDACTED]" for key in arguments}


def _normalize_approval_destination(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise DurableRunError("v2 approval destination is invalid")
    try:
        hostname, _port, scheme = ssrf._parse_and_validate_url(value)
    except ssrf.SsrfBlocked as exc:
        raise DurableRunError("v2 approval destination is invalid") from exc
    normalized = f"{scheme}://{hostname}"
    if value != normalized:
        raise DurableRunError("v2 approval destination is invalid")
    return normalized


async def _persist_v2_approval_interrupt(*, run_id: str, owner: str, calls: object) -> None:
    if not isinstance(calls, list) or not calls:
        raise DurableRunError("v2 approval interrupt is invalid")
    factory = _factory()
    async with factory() as session, session.begin():
        run = (await session.execute(select(ChatRun).where(ChatRun.id == run_id).with_for_update())).scalar_one()
        _require_owned_running_lease(run, owner)
        if run.execution_protocol_version != 2:
            raise DurableRunProtocolMismatch("v1 run emitted a v2 approval interrupt")
        seen_call_ids: set[str] = set()
        for raw_call in calls:
            if not isinstance(raw_call, dict):
                raise DurableRunError("v2 approval interrupt call is invalid")
            call_id = raw_call.get("call_id")
            name = raw_call.get("name")
            source = raw_call.get("source")
            effect = raw_call.get("effect")
            if (
                not isinstance(call_id, str)
                or not call_id
                or len(call_id) > 190
                or not isinstance(name, str)
                or not name
                or source not in {"builtin", "managed", "custom_http", "mcp", "workspace", "agent"}
                or effect not in {"read", "workspace_write", "process", "external_mutation"}
            ):
                raise DurableRunError("v2 approval interrupt call is invalid")
            if call_id in seen_call_ids:
                raise DurableRunError("v2 approval interrupt has duplicate call IDs")
            seen_call_ids.add(call_id)
            try:
                arguments = json.loads(raw_call.get("arguments") or "{}")
            except (TypeError, json.JSONDecodeError) as exc:
                raise DurableRunError("v2 approval arguments are invalid") from exc
            if not isinstance(arguments, dict):
                raise DurableRunError("v2 approval arguments are invalid")
            tool_definition_hash = raw_call.get("tool_definition_hash")
            config_fingerprint = raw_call.get("config_fingerprint")
            if (
                not isinstance(tool_definition_hash, str)
                or len(tool_definition_hash) != 64
                or any(char not in "0123456789abcdef" for char in tool_definition_hash)
                or (
                    config_fingerprint is not None
                    and (
                        not isinstance(config_fingerprint, str)
                        or len(config_fingerprint) != 64
                        or any(char not in "0123456789abcdef" for char in config_fingerprint)
                    )
                )
            ):
                raise DurableRunError("v2 approval dispatch identity is invalid")
            destination_origin = _normalize_approval_destination(raw_call.get("destination_origin"))
            try:
                preview_parts = validate_chat_parts(raw_call.get("preview", []))
            except (TypeError, ValueError) as exc:
                raise DurableRunError("v2 approval preview is invalid") from exc
            expected_state_revision = raw_call.get("expected_state_revision")
            writer_fence = raw_call.get("writer_fence")
            if (
                expected_state_revision is not None
                and (type(expected_state_revision) is not int or expected_state_revision < 0)
            ) or (writer_fence is not None and (type(writer_fence) is not int or writer_fence < 0)):
                raise DurableRunError("v2 approval state fence is invalid")
            preview_payload = [part.model_dump(mode="json") for part in preview_parts]
            preview_fingerprint = _approval_preview_fingerprint(preview_payload)
            approval = (
                await session.execute(
                    select(ChatToolApproval)
                    .where(ChatToolApproval.run_id == run.id, ChatToolApproval.call_id == call_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            expires_at = approval.expires_at if approval is not None else _now() + timedelta(minutes=15)
            expected_hmac = _approval_dispatch_hmac(
                run_id=run.id,
                owner_user_id=run.user_id,
                project_id=run.project_id,
                call_id=call_id,
                name=name,
                arguments=arguments,
                source=source,
                effect=effect,
                tool_definition_hash=tool_definition_hash,
                config_fingerprint=config_fingerprint,
                destination_origin=destination_origin,
                preview_fingerprint=preview_fingerprint,
                expires_at=expires_at,
            )
            if approval is not None:
                if not approval.dispatch_hmac or not hmac.compare_digest(approval.dispatch_hmac, expected_hmac):
                    raise DurableRunError("v2 approval interrupt identity changed")
                continue
            approval = ChatToolApproval(
                run_id=run.id,
                call_id=call_id,
                tool_name=name,
                arguments="{}",
                arguments_ciphertext=encrypt_chat_content(
                    json.dumps(arguments, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
                ),
                dispatch_hmac=expected_hmac,
                preview_fingerprint=preview_fingerprint,
                source=source,
                effect=effect,
                tool_definition_hash=tool_definition_hash,
                config_fingerprint=config_fingerprint,
                destination_origin=destination_origin,
                expected_state_revision=expected_state_revision,
                writer_fence=writer_fence,
                status="pending",
                expires_at=expires_at,
            )
            session.add(approval)
            await append_event(
                session,
                run,
                _event(
                    run,
                    "tool.approval_required",
                    {
                        "call_id": call_id,
                        "name": name,
                        "source": source,
                        "effect": effect,
                        "destination": destination_origin,
                        "redacted_arguments": _redact_approval_arguments(arguments),
                        "preview": preview_payload,
                        "expected_state_revision": expected_state_revision,
                        "writer_fence": writer_fence,
                        "expires_at": approval.expires_at,
                    },
                ),
            )
        if not transition_allowed(run.status, "awaiting_input"):
            raise DurableRunConflict("approval interrupt cannot pause this run")
        run.status = "awaiting_input"
        run.lease_owner = None
        run.lease_expires_at = None
        await append_event(session, run, _event(run, "run.stage.changed", {"stage": "awaiting_input"}))


async def _v2_approval_resume(*, run_id: str, owner: str) -> list[dict[str, str]] | None:
    factory = _factory()
    async with factory() as session:
        run = (await session.execute(select(ChatRun).where(ChatRun.id == run_id).with_for_update())).scalar_one()
        _require_owned_running_lease(run, owner)
        if run.execution_protocol_version != 2:
            return None
        approvals = list(
            (
                await session.execute(
                    select(ChatToolApproval)
                    .where(ChatToolApproval.run_id == run.id)
                    .order_by(ChatToolApproval.call_id)
                    .with_for_update()
                )
            ).scalars()
        )
        if not approvals:
            return None
        if any(approval.status == "pending" for approval in approvals):
            raise DurableRunError("queued v2 run has unresolved approvals")
        decisions: list[dict[str, str]] = []
        for approval in approvals:
            if approval.status not in {"approved", "denied"}:
                raise DurableRunError("v2 approval cannot resume")
            dispatch_hmac = _validated_approval_dispatch_hmac(run=run, approval=approval)
            decision = "approve" if approval.status == "approved" else "deny"
            _validated_approval_decision_hmac(
                run=run,
                approval=approval,
                decision=decision,
                dispatch_hmac=dispatch_hmac,
            )
            decisions.append(
                {"call_id": approval.call_id, "decision": "approve" if approval.status == "approved" else "deny"}
            )
        return decisions


async def resolve_tool_approval(
    *,
    run_id: str,
    call_id: str,
    decision: str,
    project_id: str,
    user_id: str,
) -> dict[str, Any]:
    """CAS-resolve one v2 approval without trusting client-supplied tool state."""
    if decision not in {"approve", "deny"}:
        raise DurableRunInputError("approval decision is invalid")
    if not v2_runtime_ready(get_settings().chat_execution_protocol_version):
        raise DurableRunInputError("approval tools are not available")

    factory = _factory()
    should_wake = False
    async with factory() as session, session.begin():
        try:
            run = await load_owned_run(session, run_id, user_id=user_id, project_id=project_id, lock=True)
        except RunStoreError as exc:
            raise DurableRunNotFound("chat run was not found") from exc
        if run.execution_protocol_version != 2:
            raise DurableRunInputError("approval is not available for this run protocol")

        approvals = list(
            (
                await session.execute(
                    select(ChatToolApproval)
                    .where(ChatToolApproval.run_id == run.id)
                    .order_by(ChatToolApproval.call_id)
                    .with_for_update()
                )
            ).scalars()
        )
        interactions = list(
            (
                await session.execute(
                    select(ChatRunInteraction)
                    .where(ChatRunInteraction.run_id == run.id)
                    .order_by(ChatRunInteraction.id)
                    .with_for_update()
                )
            ).scalars()
        )
        approval = next((item for item in approvals if item.call_id == call_id), None)
        if approval is None:
            raise DurableRunNotFound("tool approval was not found")
        dispatch_hmac = _validated_approval_dispatch_hmac(run=run, approval=approval)

        requested_status = "approved" if decision == "approve" else "denied"
        resolved_now = False
        actual_decision = decision
        if approval.status != "pending":
            if approval.status != requested_status:
                raise DurableRunConflict("tool approval was already decided differently")
            _validated_approval_decision_hmac(
                run=run,
                approval=approval,
                decision=decision,
                dispatch_hmac=dispatch_hmac,
            )
        else:
            if run.status != "awaiting_input":
                raise DurableRunConflict("chat run is not awaiting approval input")
            now = _now()
            expires_at = approval.expires_at
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            if expires_at <= now:
                approval.status = "denied"
                approval.decided_at = now
                approval.decided_by_user_id = None
                actual_decision = "deny"
            else:
                approval.status = requested_status
                approval.decided_at = now
                approval.decided_by_user_id = user_id
            if approval.decided_at is None:
                raise DurableRunError("v2 approval decision timestamp is missing")
            approval.decision_hmac = _approval_decision_hmac(
                dispatch_hmac=dispatch_hmac,
                owner_user_id=run.user_id,
                project_id=run.project_id,
                call_id=approval.call_id,
                decision=actual_decision,
                decided_by_user_id=approval.decided_by_user_id,
                decided_at=approval.decided_at,
            )
            await append_event(
                session,
                run,
                _event(run, "tool.approval_resolved", _approval_resolved_payload(approval, actual_decision)),
            )
            resolved_now = True

        pending_approvals = sum(item.status == "pending" for item in approvals)
        pending_interactions = sum(item.status == "pending" for item in interactions)
        if resolved_now and pending_approvals + pending_interactions == 0 and run.status == "awaiting_input":
            if not transition_allowed(run.status, "queued"):
                raise DurableRunConflict("approval resolution cannot resume this run")
            run.status = "queued"
            run.lease_owner = None
            run.lease_expires_at = None
            await append_event(session, run, _event(run, "run.stage.changed", {"stage": "queued"}))
            should_wake = True

        result = {
            "run_id": run.id,
            "call_id": approval.call_id,
            "decision": actual_decision,
            "status": approval.status,
            "run_status": run.status,
            "pending_approvals": pending_approvals,
            "pending_interactions": pending_interactions,
        }
    if should_wake:
        await wake_run(run_id)
    return result


async def resolve_run_interaction(
    *,
    run_id: str,
    interaction_id: str,
    response: dict[str, Any],
    project_id: str,
    user_id: str,
) -> dict[str, Any]:
    """CAS-resolve an owned v2 ask-user interaction from its frozen response schema."""
    if not v2_runtime_ready(get_settings().chat_execution_protocol_version):
        raise DurableRunInputError("approval tools are not available")

    factory = _factory()
    should_wake = False
    async with factory() as session, session.begin():
        try:
            run = await load_owned_run(session, run_id, user_id=user_id, project_id=project_id, lock=True)
        except RunStoreError as exc:
            raise DurableRunNotFound("chat run was not found") from exc
        if run.execution_protocol_version != 2:
            raise DurableRunInputError("interaction is not available for this run protocol")

        approvals = list(
            (
                await session.execute(
                    select(ChatToolApproval)
                    .where(ChatToolApproval.run_id == run.id)
                    .order_by(ChatToolApproval.call_id)
                    .with_for_update()
                )
            ).scalars()
        )
        interactions = list(
            (
                await session.execute(
                    select(ChatRunInteraction)
                    .where(ChatRunInteraction.run_id == run.id)
                    .order_by(ChatRunInteraction.id)
                    .with_for_update()
                )
            ).scalars()
        )
        interaction = next((item for item in interactions if item.id == interaction_id), None)
        if interaction is None:
            raise DurableRunNotFound("run interaction was not found")
        normalized_response = _normalize_interaction_response(interaction.response_schema, response)

        resolved_now = False
        if interaction.status != "pending":
            if interaction.status == "answered":
                try:
                    persisted_response = json.loads(decrypt_chat_content(interaction.response_ciphertext or ""))
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise DurableRunError("stored interaction response is invalid") from exc
                actual = json.dumps(persisted_response, separators=(",", ":"), sort_keys=True)
                requested = json.dumps(normalized_response, separators=(",", ":"), sort_keys=True)
                if not hmac.compare_digest(actual, requested):
                    raise DurableRunConflict("run interaction was already answered differently")
        else:
            if run.status != "awaiting_input":
                raise DurableRunConflict("chat run is not awaiting interaction input")
            now = _now()
            expires_at = interaction.expires_at
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            if expires_at <= now:
                interaction.status = "timeout"
                interaction.response_ciphertext = None
                interaction.decided_at = now
                interaction.decided_by_user_id = None
                event_response = None
            else:
                interaction.status = "answered"
                interaction.response_ciphertext = encrypt_chat_content(
                    json.dumps(normalized_response, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
                )
                interaction.decided_at = now
                interaction.decided_by_user_id = user_id
                event_response = normalized_response
            await append_event(
                session,
                run,
                _event(
                    run,
                    "interaction.resolved",
                    {
                        "interaction_id": interaction.id,
                        "status": interaction.status,
                        "response": event_response,
                    },
                ),
            )
            resolved_now = True

        pending_approvals = sum(item.status == "pending" for item in approvals)
        pending_interactions = sum(item.status == "pending" for item in interactions)
        if resolved_now and pending_approvals + pending_interactions == 0 and run.status == "awaiting_input":
            if not transition_allowed(run.status, "queued"):
                raise DurableRunConflict("interaction resolution cannot resume this run")
            run.status = "queued"
            run.lease_owner = None
            run.lease_expires_at = None
            await append_event(session, run, _event(run, "run.stage.changed", {"stage": "queued"}))
            should_wake = True

        result = {
            "run_id": run.id,
            "interaction_id": interaction.id,
            "status": interaction.status,
            "run_status": run.status,
            "pending_approvals": pending_approvals,
            "pending_interactions": pending_interactions,
        }
    if should_wake:
        await wake_run(run_id)
    return result


def _is_due(value: datetime, now: datetime) -> bool:
    return (value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)) <= now


async def expire_pending_inputs(*, limit: int = 100) -> list[str]:
    """CAS-expire v2 approvals/interactions even when the client never reconnects."""
    if limit < 1:
        return []
    if not v2_runtime_ready(get_settings().chat_execution_protocol_version):
        return []

    factory = _factory()
    now = _now()
    resumed_run_ids: list[str] = []
    async with factory() as session, session.begin():
        approval_run_ids = (
            await session.execute(
                select(ChatToolApproval.run_id)
                .join(ChatRun, ChatRun.id == ChatToolApproval.run_id)
                .where(
                    ChatToolApproval.status == "pending",
                    ChatToolApproval.expires_at <= now,
                    ChatRun.execution_protocol_version == 2,
                    ChatRun.status == "awaiting_input",
                )
                .order_by(ChatToolApproval.expires_at, ChatToolApproval.run_id)
                .limit(limit)
            )
        ).scalars()
        interaction_run_ids = (
            await session.execute(
                select(ChatRunInteraction.run_id)
                .join(ChatRun, ChatRun.id == ChatRunInteraction.run_id)
                .where(
                    ChatRunInteraction.status == "pending",
                    ChatRunInteraction.expires_at <= now,
                    ChatRun.execution_protocol_version == 2,
                    ChatRun.status == "awaiting_input",
                )
                .order_by(ChatRunInteraction.expires_at, ChatRunInteraction.run_id)
                .limit(limit)
            )
        ).scalars()
        candidate_run_ids = sorted(set(approval_run_ids).union(interaction_run_ids))[:limit]
        for run_id in candidate_run_ids:
            run = (
                await session.execute(
                    select(ChatRun)
                    .where(
                        ChatRun.id == run_id,
                        ChatRun.execution_protocol_version == 2,
                        ChatRun.status == "awaiting_input",
                    )
                    .with_for_update(skip_locked=True)
                )
            ).scalar_one_or_none()
            if run is None:
                continue
            approvals = list(
                (
                    await session.execute(
                        select(ChatToolApproval)
                        .where(ChatToolApproval.run_id == run.id)
                        .order_by(ChatToolApproval.call_id)
                        .with_for_update()
                    )
                ).scalars()
            )
            interactions = list(
                (
                    await session.execute(
                        select(ChatRunInteraction)
                        .where(ChatRunInteraction.run_id == run.id)
                        .order_by(ChatRunInteraction.id)
                        .with_for_update()
                    )
                ).scalars()
            )
            for approval in approvals:
                if approval.status != "pending" or not _is_due(approval.expires_at, now):
                    continue
                approval.status = "denied"
                approval.decided_at = now
                approval.decided_by_user_id = None
                await append_event(
                    session,
                    run,
                    _event(run, "tool.approval_resolved", _approval_resolved_payload(approval, "deny")),
                )
            for interaction in interactions:
                if interaction.status != "pending" or not _is_due(interaction.expires_at, now):
                    continue
                interaction.status = "timeout"
                interaction.response_ciphertext = None
                interaction.decided_at = now
                interaction.decided_by_user_id = None
                await append_event(
                    session,
                    run,
                    _event(
                        run,
                        "interaction.resolved",
                        {"interaction_id": interaction.id, "status": "timeout", "response": None},
                    ),
                )
            if any(item.status == "pending" for item in approvals) or any(
                item.status == "pending" for item in interactions
            ):
                continue
            if not transition_allowed(run.status, "queued"):
                raise DurableRunConflict("expired input batch cannot resume this run")
            run.status = "queued"
            run.lease_owner = None
            run.lease_expires_at = None
            await append_event(session, run, _event(run, "run.stage.changed", {"stage": "queued"}))
            resumed_run_ids.append(run.id)
    for run_id in resumed_run_ids:
        await wake_run(run_id)
    return resumed_run_ids


async def existing_run_for_intent(
    *,
    project_id: str,
    user_id: str,
    client_request_id: str,
    intent: dict[str, Any],
    conversation_id: str | None,
) -> ChatRunDescriptor | None:
    """Check idempotency before any caller-owned side effect."""
    try:
        uuid.UUID(client_request_id)
    except ValueError as exc:
        raise DurableRunError("Idempotency-Key must be a UUID") from exc
    factory = _factory()
    async with factory() as session:
        existing = (
            await session.execute(
                select(ChatRun).where(
                    ChatRun.project_id == project_id,
                    ChatRun.user_id == user_id,
                    ChatRun.client_request_id == client_request_id,
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            return None
        if existing.request_fingerprint != _fingerprint(intent):
            raise DurableRunConflict("idempotency_key_reused_with_different_intent")
        return descriptor(existing)


def _add_run_providers(session, run: ChatRun, capability_snapshot: dict[str, Any]) -> None:
    """Persist every provider route that can incur work during this durable run."""
    session.add(
        ChatRunProvider(
            run_id=run.id,
            purpose="executor",
            provider_id=capability_snapshot["provider_id"],
            model_id=capability_snapshot["model_id"],
            provider_label=capability_snapshot["provider_name"],
            model_label=capability_snapshot["model_name"],
            config_version_hash=capability_snapshot["config_version_hash"],
        )
    )
    routes = capability_snapshot.get("feature_routes")
    if not isinstance(routes, dict):
        return
    search = routes.get("search")
    if isinstance(search, dict):
        session.add(
            ChatRunProvider(
                run_id=run.id,
                purpose="search",
                provider_id=search["provider_id"],
                model_id=None,
                provider_label=search["provider_name"],
                model_label="managed-search",
                config_version_hash=search["config_version_hash"],
            )
        )
    advisor = routes.get("advisor")
    if isinstance(advisor, dict):
        session.add(
            ChatRunProvider(
                run_id=run.id,
                purpose="advisor",
                provider_id=advisor["provider_id"],
                model_id=advisor["model_id"],
                provider_label=advisor["provider_name"],
                model_label=advisor["model_name"],
                config_version_hash=advisor["config_version_hash"],
            )
        )


async def _lock_run_configurations(
    session,
    capability_snapshot: dict[str, Any],
    *,
    model_name: str,
) -> None:
    """Serialize executor and every opted-in managed route with admin mutations."""
    if capability_snapshot.get("model_name") != model_name:
        raise DurableRunConflict("executor_model_snapshot_mismatch")
    routes = capability_snapshot.get("feature_routes")
    routes = routes if isinstance(routes, dict) else {}
    search = routes.get("search")
    advisor = routes.get("advisor")
    try:
        await ps.lock_execution_routes(
            session,
            executor=capability_snapshot,
            search=search if isinstance(search, dict) else None,
            advisor=advisor if isinstance(advisor, dict) else None,
        )
    except ps.ProviderConfigurationChangedError as exc:
        raise DurableRunConflict("provider_configuration_changed") from exc


_ASSET_MIME_PREFIXES = {
    "image": ("image/",),
    "audio": ("audio/",),
    "video": ("video/",),
    "document": ("application/pdf",),
}


async def _bind_user_assets(
    session,
    *,
    parts: list[dict[str, Any]],
    message_id: int | None,
    run_id: str,
    user_id: str,
    project_id: str,
) -> None:
    """Lock and bind clean, owned assets before the run can enter the queue."""
    try:
        parsed = validate_user_input_parts(parts)
    except (TypeError, ValueError) as exc:
        raise DurableRunInputError("input asset parts are invalid") from exc
    requested = [(index, part) for index, part in enumerate(parsed) if part.type != "text"]
    if not requested:
        return
    asset_ids = [part.asset_id for _, part in requested]
    if len(set(asset_ids)) != len(asset_ids):
        raise DurableRunInputError("an input asset may appear only once")
    rows = (
        (await session.execute(select(ChatAsset).where(ChatAsset.id.in_(asset_ids)).with_for_update())).scalars().all()
    )
    by_id = {row.id: row for row in rows}
    for index, part in requested:
        asset = by_id.get(part.asset_id)
        if asset is None:
            raise DurableRunInputError("input asset was not found")
        if asset.user_id != user_id or asset.project_id != project_id:
            raise DurableRunInputError("input asset is not owned by this project")
        if asset.status != "clean":
            raise DurableRunInputError("input asset is not ready")
        prefixes = _ASSET_MIME_PREFIXES[part.type]
        if not asset.mime_type.startswith(prefixes):
            raise DurableRunInputError("input asset type does not match its detected media type")
        session.add(ChatRunAsset(run_id=run_id, asset_id=asset.id, purpose="input"))
        if message_id is not None:
            session.add(ChatMessageAsset(message_id=message_id, asset_id=asset.id, part_index=index))


async def create_persistent_run(
    *,
    project_id: str,
    user_id: str,
    client_request_id: str,
    intent: dict[str, Any],
    conversation_id: str,
    model_name: str,
    agent_id: int | None,
    user_content: str,
    user_parts: list[dict[str, Any]],
    request_payload: dict[str, Any],
    capability_snapshot: dict[str, Any],
    pricing_snapshot: dict[str, Any],
    client_timezone: str | None = None,
    execution_protocol_version: int = 1,
) -> ChatRunDescriptor:
    """Atomically create the user turn, run, and first journal event.

    The conversation row is locked before both idempotency and active-run checks;
    failed conflicts therefore roll back the user turn and active leaf together.
    """
    try:
        uuid.UUID(client_request_id)
    except ValueError as exc:
        raise DurableRunError("Idempotency-Key must be a UUID") from exc
    _require_supported_execution_protocol_version(execution_protocol_version)
    request_payload = {
        **request_payload,
        "client_timezone": client_timezone,
        "execution_protocol_version": execution_protocol_version,
    }
    request_payload = _freeze_v2_tool_call_limit(request_payload, execution_protocol_version)
    request_payload = await _freeze_v2_lumen_snapshot(
        request_payload,
        user_id=user_id,
        project_id=project_id,
        execution_protocol_version=execution_protocol_version,
    )
    fingerprint = _fingerprint(intent)
    factory = _factory()
    async with factory() as session, session.begin():
        existing = (
            await session.execute(
                select(ChatRun)
                .where(
                    ChatRun.project_id == project_id,
                    ChatRun.user_id == user_id,
                    ChatRun.client_request_id == client_request_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if existing is not None:
            if existing.request_fingerprint != fingerprint:
                raise DurableRunConflict("idempotency_key_reused_with_different_intent")
            return descriptor(existing)
        await _lock_run_configurations(session, capability_snapshot, model_name=model_name)

        conversation = (
            await session.execute(
                select(ChatConversation)
                .where(
                    ChatConversation.id == conversation_id,
                    ChatConversation.project_id == project_id,
                    ChatConversation.user_id == user_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if conversation is None:
            raise DurableRunNotFound("conversation was not found")
        active = (
            await session.execute(
                select(ChatRun.id)
                .where(ChatRun.conversation_id == conversation_id, ChatRun.status.in_(NONTERMINAL))
                .with_for_update()
            )
        ).scalar_one_or_none()
        if active is not None:
            raise DurableRunConflict("conversation_run_active")

        created_at, created_at_local, created_timezone = message_timestamps(client_timezone)
        user_message = ChatMessage(
            conversation_id=conversation_id,
            role="user",
            parent_id=conversation.active_leaf_id,
            content=encrypt_chat_content(user_content),
            parts=serialize_parts(user_parts),
            parts_version=1,
            status="complete",
            created_at=created_at,
            created_at_local=created_at_local,
            created_timezone=created_timezone,
        )
        session.add(user_message)
        await session.flush()
        conversation.active_leaf_id = user_message.id

        run = ChatRun(
            id=str(uuid.uuid4()),
            run_scope="persistent",
            conversation_id=conversation_id,
            user_message_id=user_message.id,
            project_id=project_id,
            user_id=user_id,
            model_name=model_name,
            agent_id=agent_id,
            execution_protocol_version=execution_protocol_version,
            capability_snapshot=capability_snapshot,
            pricing_snapshot=pricing_snapshot,
            request_payload=encrypt_chat_content(
                json.dumps(request_payload, ensure_ascii=False, separators=(",", ":"))
            ),
            client_request_id=client_request_id,
            request_fingerprint=fingerprint,
            fingerprint_version=1,
            last_seq=0,
            current_ordinal=0,
            status="queued",
        )
        _add_run_providers(session, run, capability_snapshot)
        session.add(run)
        await _bind_user_assets(
            session,
            parts=user_parts,
            message_id=user_message.id,
            run_id=run.id,
            user_id=user_id,
            project_id=project_id,
        )
        await append_event(
            session,
            run,
            _event(
                run,
                "run.started",
                {
                    "conversation_id": conversation_id,
                    "temp_thread_id": None,
                    "model_name": model_name,
                    "effective_features": request_payload["features"],
                },
            ),
        )
        await append_event(session, run, _event(run, "run.stage.changed", {"stage": "queued"}))
        created = descriptor(run)
    await wake_run(created.run_id)
    return created


async def create_run(
    *,
    project_id: str,
    user_id: str,
    client_request_id: str,
    intent: dict[str, Any],
    conversation_id: str | None,
    temp_thread_id: str | None,
    model_name: str,
    agent_id: int | None,
    user_message_id: int | None,
    request_payload: dict[str, Any],
    capability_snapshot: dict[str, Any],
    pricing_snapshot: dict[str, Any],
    execution_protocol_version: int = 1,
) -> ChatRunDescriptor:
    """Create/reuse one owner-scoped run and its first durable event.

    The fingerprint contains only client intent.  Resolution snapshots are persisted
    separately in the encrypted worker payload so a lost POST response remains
    idempotent across configuration changes.
    """
    try:
        uuid.UUID(client_request_id)
    except ValueError as exc:
        raise DurableRunError("Idempotency-Key must be a UUID") from exc
    _require_supported_execution_protocol_version(execution_protocol_version)
    request_payload = {**request_payload, "execution_protocol_version": execution_protocol_version}
    request_payload = _freeze_v2_tool_call_limit(request_payload, execution_protocol_version)
    request_payload = await _freeze_v2_lumen_snapshot(
        request_payload,
        user_id=user_id,
        project_id=project_id,
        execution_protocol_version=execution_protocol_version,
    )
    fingerprint = _fingerprint(intent)
    factory = _factory()
    async with factory() as session, session.begin():
        existing = (
            await session.execute(
                select(ChatRun)
                .where(
                    ChatRun.project_id == project_id,
                    ChatRun.user_id == user_id,
                    ChatRun.client_request_id == client_request_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if existing is not None:
            if existing.request_fingerprint != fingerprint:
                raise DurableRunConflict("idempotency_key_reused_with_different_intent")
            return descriptor(existing)

        await _lock_run_configurations(session, capability_snapshot, model_name=model_name)
        if conversation_id is not None:
            active = (
                await session.execute(
                    select(ChatRun.id)
                    .where(ChatRun.conversation_id == conversation_id, ChatRun.status.in_(NONTERMINAL))
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if active is not None:
                raise DurableRunConflict("conversation_run_active")
        if temp_thread_id is not None:
            thread = (
                await session.execute(
                    select(ChatTempThread)
                    .where(
                        ChatTempThread.id == temp_thread_id,
                        ChatTempThread.project_id == project_id,
                        ChatTempThread.user_id == user_id,
                        ChatTempThread.expires_at > _now(),
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if thread is None:
                raise DurableRunNotFound("temporary chat thread was not found")
        if temp_thread_id is not None:
            active = (
                await session.execute(
                    select(ChatRun.id)
                    .where(ChatRun.temp_thread_id == temp_thread_id, ChatRun.status.in_(NONTERMINAL))
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if active is not None:
                raise DurableRunConflict("conversation_run_active")

        run = ChatRun(
            id=str(uuid.uuid4()),
            run_scope="persistent" if conversation_id else "temp",
            conversation_id=conversation_id,
            temp_thread_id=temp_thread_id,
            user_message_id=user_message_id,
            project_id=project_id,
            user_id=user_id,
            model_name=model_name,
            agent_id=agent_id,
            execution_protocol_version=execution_protocol_version,
            capability_snapshot=capability_snapshot,
            pricing_snapshot=pricing_snapshot,
            request_payload=encrypt_chat_content(
                json.dumps(request_payload, ensure_ascii=False, separators=(",", ":"))
            ),
            client_request_id=client_request_id,
            request_fingerprint=fingerprint,
            fingerprint_version=1,
            last_seq=0,
            current_ordinal=0,
            status="queued",
        )
        session.add(run)
        _add_run_providers(session, run, capability_snapshot)
        if temp_thread_id is not None:
            thread.active_run_id = run.id
        await append_event(
            session,
            run,
            _event(
                run,
                "run.started",
                {
                    "conversation_id": conversation_id,
                    "temp_thread_id": temp_thread_id,
                    "model_name": model_name,
                    "effective_features": request_payload["features"],
                },
            ),
        )
        await append_event(session, run, _event(run, "run.stage.changed", {"stage": "queued"}))
        created = descriptor(run)
    await wake_run(created.run_id)
    return created


async def create_temp_run(
    *,
    project_id: str,
    user_id: str,
    client_request_id: str,
    intent: dict[str, Any],
    temp_thread_id: str | None,
    model_name: str,
    request_payload: dict[str, Any],
    capability_snapshot: dict[str, Any],
    pricing_snapshot: dict[str, Any],
    execution_protocol_version: int = 1,
) -> ChatRunDescriptor:
    """Atomically create/reuse a temporary thread and its first durable run."""
    try:
        uuid.UUID(client_request_id)
    except ValueError as exc:
        raise DurableRunError("Idempotency-Key must be a UUID") from exc
    _require_supported_execution_protocol_version(execution_protocol_version)
    request_payload = {**request_payload, "execution_protocol_version": execution_protocol_version}
    request_payload = _freeze_v2_tool_call_limit(request_payload, execution_protocol_version)
    request_payload = await _freeze_v2_lumen_snapshot(
        request_payload,
        user_id=user_id,
        project_id=project_id,
        execution_protocol_version=execution_protocol_version,
    )
    fingerprint = _fingerprint(intent)
    factory = _factory()
    async with factory() as session, session.begin():
        existing = (
            await session.execute(
                select(ChatRun)
                .where(
                    ChatRun.project_id == project_id,
                    ChatRun.user_id == user_id,
                    ChatRun.client_request_id == client_request_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if existing is not None:
            if existing.request_fingerprint != fingerprint:
                raise DurableRunConflict("idempotency_key_reused_with_different_intent")
            return descriptor(existing)
        await _lock_run_configurations(session, capability_snapshot, model_name=model_name)
        if temp_thread_id is None:
            thread = ChatTempThread(
                id=str(uuid.uuid4()),
                project_id=project_id,
                user_id=user_id,
                history=encrypt_chat_content("[]"),
                expires_at=_now() + _TEMP_THREAD_RETENTION,
            )
            session.add(thread)
            await session.flush()
        else:
            thread = (
                await session.execute(
                    select(ChatTempThread)
                    .where(
                        ChatTempThread.id == temp_thread_id,
                        ChatTempThread.project_id == project_id,
                        ChatTempThread.user_id == user_id,
                        ChatTempThread.expires_at > _now(),
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if thread is None:
                raise DurableRunNotFound("temporary chat thread was not found")
        active = (
            await session.execute(
                select(ChatRun.id)
                .where(ChatRun.temp_thread_id == thread.id, ChatRun.status.in_(NONTERMINAL))
                .with_for_update()
            )
        ).scalar_one_or_none()
        if active is not None:
            raise DurableRunConflict("conversation_run_active")
        current_messages = request_payload.get("input_messages")
        if not isinstance(current_messages, list) or not current_messages:
            raise DurableRunError("temporary chat request payload is invalid")
        request_payload = {
            **request_payload,
            "input_messages": [*_temporary_history_messages(thread.history), *current_messages],
        }
        run = ChatRun(
            id=str(uuid.uuid4()),
            run_scope="temp",
            temp_thread_id=thread.id,
            project_id=project_id,
            user_id=user_id,
            model_name=model_name,
            execution_protocol_version=execution_protocol_version,
            capability_snapshot=capability_snapshot,
            pricing_snapshot=pricing_snapshot,
            request_payload=encrypt_chat_content(
                json.dumps(request_payload, ensure_ascii=False, separators=(",", ":"))
            ),
            client_request_id=client_request_id,
            request_fingerprint=fingerprint,
            fingerprint_version=1,
            last_seq=0,
            current_ordinal=0,
            status="queued",
        )
        session.add(run)
        input_parts = request_payload.get("input_parts")
        if not isinstance(input_parts, list):
            raise DurableRunError("temporary chat input parts are invalid")
        await _bind_user_assets(
            session,
            parts=input_parts,
            message_id=None,
            run_id=run.id,
            user_id=user_id,
            project_id=project_id,
        )
        thread.active_run_id = run.id
        _add_run_providers(session, run, capability_snapshot)
        await append_event(
            session,
            run,
            _event(
                run,
                "run.started",
                {
                    "conversation_id": None,
                    "temp_thread_id": thread.id,
                    "model_name": model_name,
                    "effective_features": request_payload["features"],
                },
            ),
        )
        await append_event(session, run, _event(run, "run.stage.changed", {"stage": "queued"}))
        created = descriptor(run)
    await wake_run(created.run_id)
    return created


async def create_temp_thread(*, project_id: str, user_id: str) -> ChatTempThread:
    factory = _factory()
    async with factory() as session, session.begin():
        row = ChatTempThread(
            id=str(uuid.uuid4()),
            project_id=project_id,
            user_id=user_id,
            history=encrypt_chat_content("[]"),
            expires_at=_now() + _TEMP_THREAD_RETENTION,
        )
        session.add(row)
        await session.flush()
        return row


async def purge_expired_temp_threads(*, limit: int = 100) -> int:
    """Remove expired temporary content while retaining terminal run accounting."""
    factory = _factory()
    async with factory() as session, session.begin():
        threads = (
            (
                await session.execute(
                    select(ChatTempThread)
                    .where(ChatTempThread.expires_at <= _now())
                    .order_by(ChatTempThread.expires_at.asc())
                    .limit(max(1, min(limit, 1_000)))
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        thread_ids = [thread.id for thread in threads]
        if not thread_ids:
            return 0

        active_ids = set(
            (
                await session.execute(
                    select(ChatRun.temp_thread_id).where(
                        ChatRun.temp_thread_id.in_(thread_ids),
                        ChatRun.status.in_(NONTERMINAL),
                    )
                )
            ).scalars()
        )
        purge_ids = [thread_id for thread_id in thread_ids if thread_id not in active_ids]
        if not purge_ids:
            return 0

        run_ids = list(
            (await session.execute(select(ChatRun.id).where(ChatRun.temp_thread_id.in_(purge_ids)))).scalars()
        )
        if run_ids:
            await session.execute(delete(ChatRunEventRow).where(ChatRunEventRow.run_id.in_(run_ids)))
            await session.execute(delete(ChatToolApproval).where(ChatToolApproval.run_id.in_(run_ids)))
            await session.execute(delete(ChatRunSegment).where(ChatRunSegment.run_id.in_(run_ids)))
            await session.execute(delete(ChatRunTurn).where(ChatRunTurn.run_id.in_(run_ids)))
            await session.execute(
                update(ChatRun).where(ChatRun.id.in_(run_ids)).values(temp_thread_id=None, request_payload=None)
            )
        result = await session.execute(delete(ChatTempThread).where(ChatTempThread.id.in_(purge_ids)))
        return int(result.rowcount or 0)


async def owned_run_response(*, run_id: str, project_id: str, user_id: str) -> ChatRunResponse:
    factory = _factory()
    async with factory() as session:
        try:
            run = await load_owned_run(session, run_id, user_id=user_id, project_id=project_id)
        except RunStoreError as exc:
            raise DurableRunNotFound(str(exc)) from exc
        return ChatRunResponse(
            run_id=run.id,
            status=run.status,
            conversation_id=run.conversation_id,
            temp_thread_id=run.temp_thread_id,
            effective_features=(run.capability_snapshot or {}).get("effective_features", {}),
            last_seq=run.last_seq,
            terminal=run.status not in NONTERMINAL,
        )


async def active_run_descriptors(*, conversation_id: str, project_id: str, user_id: str) -> list[ChatRunDescriptor]:
    factory = _factory()
    async with factory() as session:
        rows = (
            (
                await session.execute(
                    select(ChatRun)
                    .where(
                        ChatRun.conversation_id == conversation_id,
                        ChatRun.project_id == project_id,
                        ChatRun.user_id == user_id,
                        ChatRun.status.in_(NONTERMINAL),
                    )
                    .order_by(ChatRun.created_at.desc())
                )
            )
            .scalars()
            .all()
        )
        return [descriptor(row) for row in rows]


async def active_run_descriptors_for_owner(*, project_id: str, user_id: str) -> list[ChatRunDescriptor]:
    """Return the owner's server-authoritative nonterminal run snapshot."""

    factory = _factory()
    async with factory() as session:
        rows = (
            (
                await session.execute(
                    select(ChatRun)
                    .where(
                        ChatRun.project_id == project_id,
                        ChatRun.user_id == user_id,
                        ChatRun.status.in_(NONTERMINAL),
                    )
                    .order_by(ChatRun.created_at.desc())
                )
            )
            .scalars()
            .all()
        )
        return [descriptor(row) for row in rows]


async def owned_temp_thread(*, thread_id: str, project_id: str, user_id: str) -> dict[str, Any]:
    factory = _factory()
    async with factory() as session:
        row = (
            await session.execute(
                select(ChatTempThread).where(
                    ChatTempThread.id == thread_id,
                    ChatTempThread.project_id == project_id,
                    ChatTempThread.user_id == user_id,
                    ChatTempThread.expires_at > _now(),
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise DurableRunNotFound("temporary chat thread was not found")
        try:
            history = json.loads(decrypt_chat_content(row.history))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise DurableRunError("temporary chat history is invalid") from exc
        if not isinstance(history, list):
            raise DurableRunError("temporary chat history is invalid")
        latest = (
            await session.execute(
                select(ChatRun)
                .where(ChatRun.temp_thread_id == row.id, ChatRun.project_id == project_id, ChatRun.user_id == user_id)
                .order_by(ChatRun.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        return {
            "temp_thread_id": row.id,
            "public_history": history,
            "active_run": descriptor(latest).model_dump(mode="json")
            if latest and latest.status in NONTERMINAL
            else None,
            "latest_run": descriptor(latest).model_dump(mode="json") if latest else None,
        }


async def owned_events(
    *, run_id: str, project_id: str, user_id: str, after_seq: int
) -> tuple[list[ChatRunEvent], bool]:
    factory = _factory()
    async with factory() as session:
        try:
            run = await load_owned_run(session, run_id, user_id=user_id, project_id=project_id)
            if run.run_scope == "temp" and run.temp_thread_id is None:
                raise DurableRunCursorExpired("temporary chat event journal has expired")
            events = await replay_events(session, run, after_seq=after_seq)
        except RunStoreError as exc:
            if "cursor" in str(exc):
                raise DurableRunCursorExpired(str(exc)) from exc
            raise DurableRunNotFound(str(exc)) from exc
        return events, run.status not in NONTERMINAL


def _require_owned_running_lease(run: ChatRun, owner: str) -> None:
    expires_at = run.lease_expires_at
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if run.status != "running" or run.lease_owner != owner or expires_at is None or expires_at <= _now():
        raise DurableRunLeaseLost(f"lease lost for {run.id}")


async def _append_temp_history(
    run_id: str, payload: dict[str, Any], parts: list[dict[str, Any]], *, owner: str
) -> None:
    factory = _factory()
    async with factory() as session, session.begin():
        run = (await session.execute(select(ChatRun).where(ChatRun.id == run_id).with_for_update())).scalar_one()
        _require_owned_running_lease(run, owner)
        if run.temp_thread_id is None:
            return
        thread = (
            await session.execute(
                select(ChatTempThread).where(ChatTempThread.id == run.temp_thread_id).with_for_update()
            )
        ).scalar_one_or_none()
        if thread is None:
            return
        try:
            history = json.loads(decrypt_chat_content(thread.history))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise DurableRunError("temporary chat history is invalid") from exc
        if not isinstance(history, list):
            raise DurableRunError("temporary chat history is invalid")
        input_parts = payload.get("input_parts")
        if not isinstance(input_parts, list):
            input_parts = [{"type": "text", "text": str(payload["input_messages"][-1]["content"])}]
        history.extend(
            [
                {"role": "user", "parts": input_parts},
                {"role": "assistant", "parts": parts},
            ]
        )
        thread.history = encrypt_chat_content(json.dumps(history, ensure_ascii=False, separators=(",", ":")))
        thread.active_run_id = None


async def _cancel_streaming_assistant_message(session, run: ChatRun) -> str | None:
    if run.assistant_message_id is None:
        return None
    message = (
        await session.execute(select(ChatMessage).where(ChatMessage.id == run.assistant_message_id).with_for_update())
    ).scalar_one_or_none()
    if message is None:
        return None
    if message.status == "streaming":
        deltas: dict[str, list[str]] = {"text": [], "reasoning": []}
        for event in await replay_events(session, run, after_seq=0):
            if event.type != "part.delta":
                continue
            payload = event.payload.model_dump()
            if payload.get("message_id") != str(message.id):
                continue
            part_type = payload.get("part_type")
            delta = payload.get("delta")
            if part_type in deltas and isinstance(delta, str):
                deltas[part_type].append(delta)
        if deltas["text"]:
            message.content = encrypt_chat_content("".join(deltas["text"]))
        if deltas["reasoning"]:
            message.reasoning = encrypt_chat_content("".join(deltas["reasoning"]))
        message.status = "canceled"
    return str(message.id)


async def request_cancelled(*, run_id: str, project_id: str, user_id: str) -> ChatRunResponse:
    factory = _factory()
    async with factory() as session, session.begin():
        try:
            run = await load_owned_run(session, run_id, user_id=user_id, project_id=project_id, lock=True)
        except RunStoreError as exc:
            raise DurableRunNotFound(str(exc)) from exc
        if run.status in NONTERMINAL and run.cancel_requested_at is None:
            run.cancel_requested_at = _now()
        if run.execution_protocol_version == 2 and run.status in {"awaiting_input", "queued"}:
            now = _now()
            approvals = list(
                (
                    await session.execute(
                        select(ChatToolApproval)
                        .where(ChatToolApproval.run_id == run.id, ChatToolApproval.status == "pending")
                        .order_by(ChatToolApproval.call_id)
                        .with_for_update()
                    )
                ).scalars()
            )
            interactions = list(
                (
                    await session.execute(
                        select(ChatRunInteraction)
                        .where(ChatRunInteraction.run_id == run.id, ChatRunInteraction.status == "pending")
                        .order_by(ChatRunInteraction.id)
                        .with_for_update()
                    )
                ).scalars()
            )
            for approval in approvals:
                approval.status = "canceled"
                approval.decided_at = now
                approval.decided_by_user_id = user_id
                await append_event(
                    session,
                    run,
                    _event(run, "tool.approval_resolved", _approval_resolved_payload(approval, "deny")),
                )
            for interaction in interactions:
                interaction.status = "canceled"
                interaction.response_ciphertext = None
                interaction.decided_at = now
                interaction.decided_by_user_id = user_id
                await append_event(
                    session,
                    run,
                    _event(
                        run,
                        "interaction.resolved",
                        {"interaction_id": interaction.id, "status": "canceled", "response": None},
                    ),
                )
            if not transition_allowed(run.status, "finalizing", cancel_or_failure=True):
                raise DurableRunConflict("cancellation cannot finalize this run")
            run.status = "finalizing"
            await append_event(session, run, _event(run, "run.stage.changed", {"stage": "finalizing"}))
            message_id = await _cancel_streaming_assistant_message(session, run)
            if run.temp_thread_id is not None:
                thread = (
                    await session.execute(
                        select(ChatTempThread)
                        .where(
                            ChatTempThread.id == run.temp_thread_id,
                            ChatTempThread.active_run_id == run.id,
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if thread is not None:
                    thread.active_run_id = None
            run.status = "canceled"
            await append_event(
                session,
                run,
                _event(
                    run,
                    "run.canceled",
                    {
                        "status": "canceled",
                        "message_id": message_id,
                        "error_code": "canceled_by_user",
                        "safe_message": "chat run canceled by user",
                    },
                ),
            )
        return ChatRunResponse(
            run_id=run.id,
            status=run.status,
            conversation_id=run.conversation_id,
            temp_thread_id=run.temp_thread_id,
            effective_features=(run.capability_snapshot or {}).get("effective_features", {}),
            last_seq=run.last_seq,
            terminal=run.status not in NONTERMINAL,
        )


async def _cancel_requested(run_id: str) -> bool:
    factory = _factory()
    async with factory() as session:
        run = (await session.execute(select(ChatRun).where(ChatRun.id == run_id))).scalar_one_or_none()
        return run is None or run.cancel_requested_at is not None


async def _append(run_id: str, event_type: str, payload: dict[str, Any], *, owner: str) -> None:
    factory = _factory()
    async with factory() as session, session.begin():
        run = (
            await session.execute(select(ChatRun).where(ChatRun.id == run_id).with_for_update())
        ).scalar_one_or_none()
        if run is None:
            return
        _require_owned_running_lease(run, owner)
        await append_event(session, run, _event(run, event_type, payload))


async def _set_stage(
    run_id: str,
    stage: str,
    *,
    owner: str,
    tool_name: str | None = None,
) -> None:
    payload: dict[str, Any] = {"stage": stage}
    if tool_name is not None:
        payload["tool_name"] = tool_name
    await _append(run_id, "run.stage.changed", payload, owner=owner)


async def _finish(
    run_id: str,
    *,
    status: str,
    message_id: str | None,
    owner: str,
    error_code: str | None = None,
    safe_message: str | None = None,
    usage_record: dict[str, Any] | None = None,
    message_finalization: dict[str, Any] | None = None,
    completed_parts: list[tuple[int, dict[str, Any]]] | None = None,
) -> None:
    factory = _factory()
    async with factory() as session, session.begin():
        run = (
            await session.execute(select(ChatRun).where(ChatRun.id == run_id).with_for_update())
        ).scalar_one_or_none()
        if run is None:
            return
        _require_owned_running_lease(run, owner)
        if run.status not in NONTERMINAL:
            return
        await append_event(session, run, _event(run, "run.stage.changed", {"stage": "finalizing"}))
        run.status = "finalizing"
        if usage_record is not None:
            credited = await credit.apply_usage_in_transaction(
                session,
                event_id=f"run:{run.id}",
                user_id=run.user_id,
                project_id=run.project_id,
                model_name=usage_record["model_name"],
                provider=usage_record["provider_name"],
                prompt_tokens=usage_record["prompt_tokens"],
                completion_tokens=usage_record["completion_tokens"],
                usage_cost=usage_record["usage_cost"],
                margin_multiplier=usage_record["margin_multiplier"],
                credit_per_usd=usage_record["credit_per_usd"],
                usage_components=usage_record["usage_components"],
                conversation_id=run.conversation_id,
            )
            run.usage_reconciled_at = _now()
            await append_event(
                session,
                run,
                _event(
                    run,
                    "usage.updated",
                    {
                        "components": usage_record["usage_components"],
                        "prompt_tokens": usage_record["prompt_tokens"],
                        "completion_tokens": usage_record["completion_tokens"],
                        "raw_cost": format(usage_record["usage_cost"].raw_cost, "f"),
                        "credited_cost": format(credited, "f"),
                    },
                ),
            )
        if completed_parts is not None and message_id is not None:
            terminal_turn = (
                await session.execute(
                    select(ChatRunTurn)
                    .where(
                        ChatRunTurn.run_id == run.id,
                        ChatRunTurn.assistant_message_id == int(message_id),
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if terminal_turn is None:
                raise DurableRunError("terminal assistant message has no durable turn")
            if terminal_turn.completion_event_seq is None:
                completion_event_seq = 0
                for part_index, part in completed_parts:
                    event = await append_event(
                        session,
                        run,
                        _event(
                            run, "part.completed", {"message_id": message_id, "part_index": part_index, "part": part}
                        ),
                    )
                    completion_event_seq = event.seq
                terminal_turn.completion_event_seq = completion_event_seq
        if message_finalization is not None:
            await cs.complete_message_in_transaction(
                session,
                message_finalization["conversation_id"],
                message_id=message_finalization["message_id"],
                content=message_finalization["content"],
                reasoning=message_finalization["reasoning"],
                model_name=message_finalization["model_name"],
                canonical_parts=message_finalization.get("canonical_parts"),
                status=message_finalization.get("message_status", "complete"),
                token_prompt=message_finalization["token_prompt"],
                token_completion=message_finalization["token_completion"],
            )
        elif status != "completed" and run.conversation_id is not None:
            turn_message_ids = (
                (
                    await session.execute(
                        select(ChatRunTurn.assistant_message_id).where(
                            ChatRunTurn.run_id == run.id,
                            ChatRunTurn.assistant_message_id.is_not(None),
                        )
                    )
                )
                .scalars()
                .all()
            )
            if turn_message_ids:
                deltas: dict[str, dict[str, list[str]]] = {
                    str(message_id): {"text": [], "reasoning": []} for message_id in turn_message_ids
                }
                for event in await replay_events(session, run, after_seq=0):
                    if event.type != "part.delta":
                        continue
                    payload = event.payload.model_dump()
                    event_message_id = payload.get("message_id")
                    part_type = payload.get("part_type")
                    delta = payload.get("delta")
                    if (
                        isinstance(event_message_id, str)
                        and part_type in ("text", "reasoning")
                        and isinstance(delta, str)
                        and event_message_id in deltas
                    ):
                        deltas[event_message_id][part_type].append(delta)
                messages = (
                    (await session.execute(select(ChatMessage).where(ChatMessage.id.in_(turn_message_ids))))
                    .scalars()
                    .all()
                )
                for message in messages:
                    if message.status != "streaming":
                        continue
                    recovered = deltas[str(message.id)]
                    if recovered["text"]:
                        message.content = encrypt_chat_content("".join(recovered["text"]))
                    if recovered["reasoning"]:
                        message.reasoning = encrypt_chat_content("".join(recovered["reasoning"]))
                    message.status = status
        run.status = status
        memory_enabled = False
        if status == "completed":
            try:
                request = _payload(run)
                features = request.get("features")
                memory_enabled = isinstance(features, dict) and features.get("memory") is True
            except DurableRunError:
                logger.warning("completed chat run has unreadable memory feature setting run_id=%s", run.id)
        await enqueue_completed_run_in_transaction(session, run, memory_enabled=memory_enabled)
        event_type = {"completed": "run.completed", "failed": "run.failed", "canceled": "run.canceled"}[status]
        payload: dict[str, Any] = {"status": status, "message_id": message_id}
        if status != "completed":
            payload.update(error_code=error_code or "run_failed", safe_message=safe_message or "chat run failed")
        await append_event(session, run, _event(run, event_type, payload))


async def recover_stale_runs(*, owner: str, limit: int = 16) -> list[str]:
    """Requeue pre-I/O and completed work; indeterminate provider calls fail closed."""
    factory = _factory()
    now = _now()
    failed_run_ids: list[str] = []
    requeued_run_ids: list[str] = []
    async with factory() as session, session.begin():
        stale_runs = (
            (
                await session.execute(
                    select(ChatRun)
                    .where(
                        ChatRun.status == "running",
                        ChatRun.lease_expires_at.is_not(None),
                        ChatRun.lease_expires_at < now,
                    )
                    .order_by(ChatRun.lease_expires_at)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )
        for run in stale_runs:
            started_segments = (
                (
                    await session.execute(
                        select(ChatRunSegment)
                        .where(ChatRunSegment.run_id == run.id, ChatRunSegment.status == "provider_started")
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )
            if started_segments:
                for segment in started_segments:
                    fail_unresolved_segment(segment, error_code="provider_result_unknown")
                run.lease_owner = owner
                run.lease_expires_at = now + timedelta(seconds=_LEASE_SECONDS)
                failed_run_ids.append(run.id)
                continue
            run.status = "queued"
            run.lease_owner = None
            run.lease_expires_at = None
            requeued_run_ids.append(run.id)
    for run_id in failed_run_ids:
        await _finish(
            run_id,
            status="failed",
            message_id=None,
            owner=owner,
            error_code="provider_result_unknown",
            safe_message="이전 모델 호출 결과를 안전하게 확인할 수 없습니다",
        )
    return requeued_run_ids


async def _renew_lease(run_id: str, owner: str) -> bool:
    factory = _factory()
    async with factory() as session, session.begin():
        return await renew_run_lease(session, run_id, owner=owner) is not None


class _DurableExecutionHooks:
    """Persist and replay each external graph boundary for one leased run."""

    journals_tool_events = True

    def __init__(self, *, run_id: str, owner: str) -> None:
        self.run_id = run_id
        self.owner = owner
        self.active_turn_ordinal: int | None = None
        self.active_message_id: str | None = None

    async def loaded_tool_names(self) -> list[str]:
        """Restore completed deferred bindings after restart or approval resume."""
        factory = _factory()
        async with factory() as session:
            segments = (
                await session.execute(
                    select(ChatRunSegment)
                    .where(
                        ChatRunSegment.run_id == self.run_id,
                        ChatRunSegment.endpoint == "tool",
                        ChatRunSegment.status == "completed",
                    )
                    .order_by(ChatRunSegment.ordinal.asc())
                )
            ).scalars()
            loaded: list[str] = []
            for segment in segments:
                try:
                    payload = load_segment_payload(segment.result_payload)
                    if not isinstance(payload, dict) or payload.get("tool_name") != "list_available_tools":
                        continue
                    content = json.loads(str(payload.get("content") or ""))
                    names = content.get("loaded_tools") if isinstance(content, dict) else None
                except (RunStoreError, TypeError, ValueError, json.JSONDecodeError):
                    continue
                if not isinstance(names, list):
                    continue
                for name in names:
                    if isinstance(name, str) and name not in loaded:
                        loaded.append(name)
            return loaded

    async def _ensure_provider_turn(self, session, run: ChatRun, turn_ordinal: int) -> None:
        turn = (
            await session.execute(
                select(ChatRunTurn)
                .where(ChatRunTurn.run_id == run.id, ChatRunTurn.ordinal == turn_ordinal)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if turn is None and run.conversation_id is None:
            turn = ChatRunTurn(run_id=run.id, ordinal=turn_ordinal)
            session.add(turn)
            message_id = f"temp:{run.id}:assistant:{turn_ordinal}"
            event = await append_event(
                session,
                run,
                _event(
                    run,
                    "message.created",
                    {"message_id": message_id, "role": "assistant", "parent_id": None},
                ),
            )
            turn.message_event_seq = event.seq
        elif turn is None:
            previous_message_id = (
                await session.execute(
                    select(ChatRunTurn.assistant_message_id)
                    .where(ChatRunTurn.run_id == run.id, ChatRunTurn.ordinal < turn_ordinal)
                    .order_by(ChatRunTurn.ordinal.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            created_at, created_at_local, created_timezone = _message_timestamps_for_run(run)
            placeholder = ChatMessage(
                conversation_id=run.conversation_id,
                role="assistant",
                parent_id=previous_message_id or run.user_message_id,
                content=encrypt_chat_content(""),
                status="streaming",
                model_name=run.model_name,
                created_at=created_at,
                created_at_local=created_at_local,
                created_timezone=created_timezone,
            )
            session.add(placeholder)
            await session.flush()
            turn = ChatRunTurn(
                run_id=run.id,
                ordinal=turn_ordinal,
                assistant_message_id=placeholder.id,
            )
            session.add(turn)
            event = await append_event(
                session,
                run,
                _event(
                    run,
                    "message.created",
                    {
                        "message_id": str(placeholder.id),
                        "role": "assistant",
                        "parent_id": str(placeholder.parent_id) if placeholder.parent_id is not None else None,
                    },
                ),
            )
            turn.message_event_seq = event.seq
        if turn is None:
            raise DurableRunError("failed to create a durable run turn")
        run.current_ordinal = turn_ordinal
        self.active_turn_ordinal = turn_ordinal
        if run.conversation_id is None:
            self.active_message_id = f"temp:{run.id}:assistant:{turn_ordinal}"
        else:
            if turn.assistant_message_id is None:
                raise DurableRunError("persistent run turn is missing its assistant message")
            run.assistant_message_id = turn.assistant_message_id
            self.active_message_id = str(turn.assistant_message_id)

    async def _start(
        self,
        *,
        segment_id: str,
        ordinal: int,
        endpoint: str,
        turn_ordinal: int,
        call_id: str | None = None,
        journal_started: tuple[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any] | None:
        factory = _factory()
        async with factory() as session, session.begin():
            run = (
                await session.execute(select(ChatRun).where(ChatRun.id == self.run_id).with_for_update())
            ).scalar_one()
            _require_owned_running_lease(run, self.owner)
            if endpoint == "chat_completions":
                await self._ensure_provider_turn(session, run, turn_ordinal)
            segment = await prepare_segment(
                session,
                run,
                segment_id=segment_id,
                ordinal=ordinal,
                endpoint=endpoint,
                turn_ordinal=turn_ordinal,
                call_id=call_id,
            )
            if segment.status == "completed":
                payload = load_segment_payload(segment.result_payload)
                if payload is None:
                    raise DurableRunError("completed segment is missing its replay payload")
                return {**payload, "_durable_replay": True}
            if segment.status == "provider_started":
                fail_unresolved_segment(segment, error_code="provider_result_unknown")
                return {"_boundary_abort": "provider_result_unknown"}
            if segment.status == "failed":
                return {"_boundary_abort": "provider_result_unknown"}
            begin_segment_io(segment)
            if journal_started is not None:
                event_type, payload = journal_started
                event = await append_event(session, run, _event(run, event_type, payload))
                segment.started_event_seq = event.seq
            return None

    async def _complete(
        self,
        *,
        segment_id: str,
        ordinal: int,
        endpoint: str,
        turn_ordinal: int,
        result_payload: dict[str, Any] | None,
        usage_payload: dict[str, Any] | None = None,
        call_id: str | None = None,
        journal_completed: tuple[str, dict[str, Any]] | None = None,
    ) -> None:
        factory = _factory()
        async with factory() as session, session.begin():
            run = (
                await session.execute(select(ChatRun).where(ChatRun.id == self.run_id).with_for_update())
            ).scalar_one()
            _require_owned_running_lease(run, self.owner)
            segment = await prepare_segment(
                session,
                run,
                segment_id=segment_id,
                ordinal=ordinal,
                endpoint=endpoint,
                turn_ordinal=turn_ordinal,
                call_id=call_id,
            )
            if segment.status == "completed":
                return
            complete_segment_io(segment, result_payload=result_payload, usage_payload=usage_payload)
            if journal_completed is not None:
                event_type, payload = journal_completed
                event = await append_event(session, run, _event(run, event_type, payload))
                segment.completed_event_seq = event.seq

    async def _fail(
        self,
        *,
        segment_id: str,
        ordinal: int,
        endpoint: str,
        turn_ordinal: int,
        call_id: str | None = None,
    ) -> dict[str, str] | None:
        factory = _factory()
        async with factory() as session, session.begin():
            run = (
                await session.execute(select(ChatRun).where(ChatRun.id == self.run_id).with_for_update())
            ).scalar_one()
            _require_owned_running_lease(run, self.owner)
            segment = await prepare_segment(
                session,
                run,
                segment_id=segment_id,
                ordinal=ordinal,
                endpoint=endpoint,
                turn_ordinal=turn_ordinal,
                call_id=call_id,
            )
            if segment.status == "provider_started":
                fail_unresolved_segment(segment, error_code="provider_result_unknown")
            return {"_boundary_abort": "provider_result_unknown"}

    async def provider_started(self, *, round_index: int, attempt: int) -> dict[str, Any] | None:
        return await self._start(
            segment_id=f"provider:{round_index}:{attempt}",
            ordinal=round_index * 1_000 + attempt,
            endpoint="chat_completions",
            turn_ordinal=round_index,
        )

    async def provider_completed(
        self,
        *,
        round_index: int,
        attempt: int,
        usage: dict[str, Any],
        result_payload: dict[str, Any],
    ) -> None:
        await self._complete(
            segment_id=f"provider:{round_index}:{attempt}",
            ordinal=round_index * 1_000 + attempt,
            endpoint="chat_completions",
            turn_ordinal=round_index,
            result_payload=result_payload,
            usage_payload=usage,
        )

    async def provider_failed(self, *, round_index: int, attempt: int) -> dict[str, str]:
        return await self._fail(
            segment_id=f"provider:{round_index}:{attempt}",
            ordinal=round_index * 1_000 + attempt,
            endpoint="chat_completions",
            turn_ordinal=round_index,
        )

    async def tool_started(
        self,
        *,
        round_index: int,
        tool_index: int,
        tool_call_id: str,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        source: str = "builtin",
        category: str = "기본 도구",
    ) -> dict[str, Any] | None:
        return await self._start(
            segment_id=f"tool:{round_index}:{tool_index}",
            ordinal=round_index * 1_000 + 500 + tool_index,
            endpoint="tool",
            turn_ordinal=round_index,
            call_id=tool_call_id,
            journal_started=(
                "tool.call.started",
                {
                    "call_id": tool_call_id,
                    "name": tool_name,
                    "arguments": arguments if isinstance(arguments, dict) else {},
                    "source": source,
                    "category": category,
                },
            ),
        )

    async def managed_tool_allowed(self, *, tool_name: str, maximum: int) -> bool:
        """Reserve a managed-use slot from journaled starts, including crashed calls."""
        if maximum < 1:
            return False
        factory = _factory()
        async with factory() as session:
            run = (await session.execute(select(ChatRun).where(ChatRun.id == self.run_id))).scalar_one()
            used = 0
            for event in await replay_events(session, run, after_seq=0):
                if event.type != "tool.call.started":
                    continue
                payload = event.payload.model_dump()
                if payload.get("name") == tool_name:
                    used += 1
            # The current call has already committed its started boundary.
            return used <= maximum

    async def tool_completed(
        self,
        *,
        round_index: int,
        tool_index: int,
        tool_call_id: str,
        tool_name: str,
        result_payload: dict[str, Any],
        source: str = "builtin",
        category: str = "기본 도구",
    ) -> None:
        content = result_payload.get("content")
        if not isinstance(content, str):
            raise DurableRunError("tool result payload is missing text content")
        visible = result_payload.get("visible") is not False
        status = _tool_result_status(result_payload.get("status"))
        error_code = _tool_result_error_code(result_payload.get("error_code"))
        display_parts = _tool_display_parts(result_payload, content=content, visible=visible)
        await self._complete(
            segment_id=f"tool:{round_index}:{tool_index}",
            ordinal=round_index * 1_000 + 500 + tool_index,
            endpoint="tool",
            turn_ordinal=round_index,
            call_id=tool_call_id,
            result_payload=result_payload,
            journal_completed=(
                "tool.call.completed",
                {
                    "call_id": tool_call_id,
                    "name": tool_name,
                    "content": display_parts,
                    "status": status,
                    "source": source,
                    "category": category,
                    "error_code": error_code,
                },
            ),
        )

    async def tool_failed(
        self, *, round_index: int, tool_index: int, tool_call_id: str, tool_name: str
    ) -> dict[str, str]:
        return await self._fail(
            segment_id=f"tool:{round_index}:{tool_index}",
            ordinal=round_index * 1_000 + 500 + tool_index,
            endpoint="tool",
            turn_ordinal=round_index,
            call_id=tool_call_id,
        )

    async def replay_turn_projection(self) -> dict[str, Any]:
        """Read the committed display projection for the active durable turn."""
        if self.active_turn_ordinal is None:
            raise DurableRunError("active durable turn is not set")
        factory = _factory()
        async with factory() as session:
            run = (await session.execute(select(ChatRun).where(ChatRun.id == self.run_id))).scalar_one()
            turn = (
                await session.execute(
                    select(ChatRunTurn).where(
                        ChatRunTurn.run_id == self.run_id,
                        ChatRunTurn.ordinal == self.active_turn_ordinal,
                    )
                )
            ).scalar_one_or_none()
            if turn is None:
                raise DurableRunError("active durable turn is missing")
            message_id = (
                str(turn.assistant_message_id)
                if turn.assistant_message_id is not None
                else f"temp:{run.id}:assistant:{turn.ordinal}"
            )
            deltas: dict[str, list[str]] = {"text": [], "reasoning": []}
            for event in await replay_events(session, run, after_seq=turn.message_event_seq or 0):
                if event.type != "part.delta":
                    continue
                payload = event.payload.model_dump()
                if payload.get("message_id") != message_id:
                    continue
                part_type = payload.get("part_type")
                delta = payload.get("delta")
                if part_type in deltas and isinstance(delta, str):
                    deltas[part_type].append(delta)
            return {
                "text": "".join(deltas["text"]),
                "reasoning": "".join(deltas["reasoning"]),
                "completed": turn.completion_event_seq is not None,
            }


_MANAGED_USAGE_KEYS = {
    ("web_search_requests", "web_search_request_per_unit", "request", "search"),
    ("web_search_context", "web_search_context_low_per_unit", "context", "search"),
    ("web_search_context", "web_search_context_medium_per_unit", "context", "search"),
    ("web_search_context", "web_search_context_high_per_unit", "context", "search"),
    ("web_fetch_requests", "web_fetch_request_per_unit", "request", "fetch"),
    ("web_fetch_context", "web_fetch_context_per_unit", "context", "fetch"),
    ("advisor_input_tokens", "advisor_input_price_per_token", "token", "advisor"),
    ("advisor_output_tokens", "advisor_output_price_per_token", "token", "advisor"),
}


def _managed_usage_components(
    records: list[dict[str, Any]],
    *,
    pricing_snapshot: dict[str, Any],
    model_name: str,
) -> tuple[Decimal, list[dict[str, Any]]]:
    """Price worker-authored managed tool records from the immutable run snapshot."""
    prices = pricing_snapshot.get("component_prices")
    if not isinstance(prices, dict):
        raise DurableRunError("managed tool pricing snapshot is missing")
    total = Decimal("0")
    components: list[dict[str, Any]] = []
    for index, record in enumerate(records):
        kind = record.get("kind")
        price_key = record.get("price_key")
        unit = record.get("unit")
        source = record.get("source")
        if (kind, price_key, unit, source) not in _MANAGED_USAGE_KEYS:
            raise DurableRunError("managed tool usage record is invalid")
        try:
            quantity = Decimal(str(record.get("quantity")))
            unit_price = Decimal(str(prices[price_key]))
        except (InvalidOperation, KeyError, TypeError, ValueError) as exc:
            raise DurableRunError("managed tool pricing snapshot is invalid") from exc
        is_advisor = source == "advisor"
        if (
            not quantity.is_finite()
            or quantity < 0
            or (is_advisor and quantity != quantity.to_integral_value())
            or (not is_advisor and quantity != 1)
            or (is_advisor and quantity > 1_000_000)
            or not unit_price.is_finite()
            or unit_price < 0
        ):
            raise DurableRunError("managed tool usage record is invalid")
        cost = (quantity * unit_price).quantize(Decimal("0.0000000001"), rounding=ROUND_HALF_EVEN)
        total += cost
        components.append(
            {
                "segment_id": f"{source}:managed:{index}",
                "kind": kind,
                "quantity": format(quantity, "f"),
                "unit": unit,
                "unit_price_usd": format(unit_price, "f"),
                "cost_usd": format(cost, "f"),
                "source": source,
                "model_name": record.get("model_name") if isinstance(record.get("model_name"), str) else model_name,
                "metadata": {},
            }
        )
    return total.quantize(Decimal("0.0000000001"), rounding=ROUND_HALF_EVEN), components


async def _managed_tool_configs(
    payload: dict[str, Any], capability_snapshot: dict[str, Any]
) -> tuple[dict | None, dict | None, dict | None]:
    """Resolve non-secret snapshots into worker-only managed tool configs."""
    features = payload.get("features")
    if not isinstance(features, dict):
        return None, None, None
    search_config: dict | None = None
    fetch_config: dict | None = None
    advisor_config: dict | None = None
    routes = capability_snapshot.get("feature_routes")
    routes = routes if isinstance(routes, dict) else {}
    search_options = features.get("web_search")
    if isinstance(search_options, dict) and search_options.get("enabled") is True:
        route_snapshot = routes.get("search")
        if not isinstance(route_snapshot, dict):
            raise DurableRunError("managed search route snapshot is missing")
        route = await ps.resolve_provider_snapshot(route_snapshot)
        if route is None:
            raise DurableRunError("managed search provider configuration is unavailable")
        search_config = {"route": route, "options": search_options}
    fetch_options = features.get("web_fetch")
    if isinstance(fetch_options, dict) and fetch_options.get("enabled") is True:
        fetch_config = fetch_options
    advisor_options = features.get("advisor")
    if isinstance(advisor_options, dict) and advisor_options.get("enabled") is True:
        route_snapshot = routes.get("advisor")
        if not isinstance(route_snapshot, dict):
            raise DurableRunError("managed advisor route snapshot is missing")
        route = await ps.resolve_model_snapshot(route_snapshot)
        if route is None:
            raise DurableRunError("managed advisor model configuration is unavailable")
        advisor_config = {"route": route, "options": advisor_options}
    return search_config, fetch_config, advisor_config


async def execute_queued_run(run_id: str, *, owner: str) -> bool:
    """Claim one queued run and stream its normalized engine output into the journal."""
    factory = _factory()
    async with factory() as session, session.begin():
        run = await claim_queued_run(session, run_id, owner=owner)
        if run is None:
            return False
        payload = _payload(run)
        model_name = run.model_name
        project_id = run.project_id
        user_id = run.user_id
        conversation_id = run.conversation_id
        parent_id = run.user_message_id
        capability_snapshot = run.capability_snapshot
        pricing_snapshot = run.pricing_snapshot
        existing_message_id = run.assistant_message_id

    if conversation_id is not None and existing_message_id is not None:
        # The provider boundary selects the replay turn; never route it through
        # the run's latest assistant message before that boundary runs.
        message_id: str | None = None
    elif conversation_id is not None:
        async with factory() as session, session.begin():
            run = (await session.execute(select(ChatRun).where(ChatRun.id == run_id).with_for_update())).scalar_one()
            _require_owned_running_lease(run, owner)
            created_at, created_at_local, created_timezone = _message_timestamps_for_run(run)
            placeholder = ChatMessage(
                conversation_id=conversation_id,
                role="assistant",
                parent_id=parent_id,
                content=encrypt_chat_content(""),
                status="streaming",
                model_name=model_name,
                created_at=created_at,
                created_at_local=created_at_local,
                created_timezone=created_timezone,
            )
            session.add(placeholder)
            await session.flush()
            message_id = str(placeholder.id)
            run.assistant_message_id = placeholder.id
            run.current_ordinal = 0
            turn = ChatRunTurn(run_id=run.id, ordinal=0, assistant_message_id=placeholder.id)
            session.add(turn)
            event = await append_event(
                session,
                run,
                _event(
                    run,
                    "message.created",
                    {
                        "message_id": message_id,
                        "role": "assistant",
                        "parent_id": str(parent_id) if parent_id is not None else None,
                    },
                ),
            )
            turn.message_event_seq = event.seq
    else:
        message_id = None

    citations: list[dict[str, Any]] = []
    tool_invocations: dict[str, dict[str, Any]] = {}
    tool_results: dict[str, dict[str, Any]] = {}
    usage: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0}
    managed_tool_usage: list[dict[str, Any]] = []

    text: list[str] = []
    reasoning: list[str] = []
    pending_deltas: dict[str, list[str]] = {"text": [], "reasoning": []}
    last_delta_flush_at = monotonic()

    async def flush_deltas(*, check_cancel: bool = True) -> bool:
        """Persist bounded delta batches without stalling upstream chunk consumption."""
        if lease_lost.is_set():
            raise DurableRunLeaseLost(f"lease lost for {run_id}")
        nonlocal last_delta_flush_at
        if check_cancel and await _cancel_requested(run_id):
            await _finish(
                run_id,
                status="canceled",
                message_id=message_id,
                owner=owner,
                error_code="canceled",
                safe_message="chat run canceled",
            )
            return True
        if not pending_deltas["text"] and not pending_deltas["reasoning"]:
            return False
        for part_index, part_type, destination in (
            (0, "text", text),
            (1, "reasoning", reasoning),
        ):
            chunks = pending_deltas[part_type]
            if not chunks:
                continue
            delta = "".join(chunks)
            chunks.clear()
            destination.append(delta)
            await _append(
                run_id,
                "part.delta",
                {"message_id": message_id, "part_index": part_index, "part_type": part_type, "delta": delta},
                owner=owner,
            )
        last_delta_flush_at = monotonic()
        return False

    async def finalize_current_turn() -> None:
        if conversation_id is None or message_id is None:
            return
        canonical_parts: list[dict[str, Any]] = []
        content = "".join(text)
        visible_reasoning = "".join(reasoning)
        if content:
            canonical_parts.append({"type": "text", "text": content})
        if visible_reasoning:
            canonical_parts.append({"type": "reasoning", "text": visible_reasoning, "visibility": "user"})
        canonical_parts.extend(tool_invocations.values())
        canonical_parts.extend(tool_results.values())
        parts = list(enumerate(canonical_parts))
        async with factory() as session, session.begin():
            run = (await session.execute(select(ChatRun).where(ChatRun.id == run_id).with_for_update())).scalar_one()
            _require_owned_running_lease(run, owner)
            if execution_hooks.active_turn_ordinal is None:
                raise DurableRunError("finalized durable turn is not set")
            turn = (
                await session.execute(
                    select(ChatRunTurn)
                    .where(
                        ChatRunTurn.run_id == run.id,
                        ChatRunTurn.ordinal == execution_hooks.active_turn_ordinal,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if turn is None or turn.assistant_message_id != int(message_id):
                raise DurableRunError("finalized durable turn does not match its assistant message")
            if turn.completion_event_seq is not None:
                return
            completion_event_seq = 0
            for part_index, part in parts:
                event = await append_event(
                    session,
                    run,
                    _event(run, "part.completed", {"message_id": message_id, "part_index": part_index, "part": part}),
                )
                completion_event_seq = event.seq
            await cs.complete_message_in_transaction(
                session,
                conversation_id,
                message_id=int(message_id),
                content=content,
                reasoning=visible_reasoning or None,
                model_name=model_name,
                canonical_parts=canonical_parts,
            )
            turn.completion_event_seq = completion_event_seq

    heartbeat_stop = asyncio.Event()
    lease_lost = asyncio.Event()

    async def heartbeat() -> None:
        while True:
            try:
                await asyncio.wait_for(heartbeat_stop.wait(), timeout=10)
                return
            except TimeoutError:
                try:
                    renewed = await _renew_lease(run_id, owner)
                except Exception:
                    logger.exception("durable chat lease renewal failed run_id=%s owner=%s", run_id, owner)
                    renewed = False
                if not renewed:
                    lease_lost.set()
                    logger.warning("durable chat run lease was lost run_id=%s owner=%s", run_id, owner)
                    return

    heartbeat_task = asyncio.create_task(heartbeat())

    def ensure_lease() -> None:
        if lease_lost.is_set():
            raise DurableRunLeaseLost(f"lease lost for {run_id}")

    response_writing = False

    async def mark_response_writing() -> None:
        nonlocal response_writing
        if not response_writing:
            await _set_stage(run_id, "response_writing", owner=owner)
            response_writing = True

    try:
        if not _validate_run_protocol_payload(run, payload):
            raise DurableRunProtocolMismatch("run execution protocol does not match this worker")
        await _set_stage(run_id, "model_request", owner=owner)
        resolved = await ps.resolve_model_snapshot(capability_snapshot)
        if resolved is None:
            raise DurableRunError("requested model configuration is unavailable")
        managed_search, managed_fetch, managed_advisor = await _managed_tool_configs(payload, capability_snapshot)
        extension_snapshot = payload.get("extension_snapshot")
        if extension_snapshot is None:
            selected_tool_ids = _selected_tool_ids(payload.get("features"))
            selected_mcp_ids = None
            expected_mcp_credential_versions = None
            expected_extension_fingerprints = None
        else:
            try:
                selected_tool_ids, selected_mcp_ids, warnings = await extensions_store.validated_frozen_selection(
                    extension_snapshot, user_id=user_id, project_id=project_id
                )
            except extensions_store.ChatStorageUnavailable as exc:
                raise DurableRunError("frozen extension configuration could not be verified") from exc
            for warning in warnings:
                await _append(
                    run_id, "run.warning", {"code": "extension_config_changed", "safe_message": warning}, owner=owner
                )
            expected_mcp_credential_versions = _selected_mcp_credential_versions(extension_snapshot, selected_mcp_ids)
            expected_extension_fingerprints = extensions_store.frozen_selection_fingerprints(
                extension_snapshot,
                selected_tool_ids,
                selected_mcp_ids,
            )
        input_messages = [dict(message) for message in payload["input_messages"]]
        input_parts = payload.get("input_parts")
        if isinstance(input_parts, list) and any(
            part.get("type") != "text" for part in input_parts if isinstance(part, dict)
        ):
            document_gate = ((resolved.get("capabilities") or {}).get("feature_gates") or {}).get("document_input")
            try:
                materialized = await assets.provider_content_for_input_parts(
                    input_parts,
                    user_id=user_id,
                    project_id=project_id,
                    allow_document=bool(isinstance(document_gate, dict) and document_gate.get("available")),
                )
            except assets.AssetError as exc:
                raise DurableRunInputError(str(exc)) from exc
            for message in reversed(input_messages):
                if message.get("role") == "user":
                    message["content"] = materialized
                    break
        awaiting_model_response = True
        pending_tool_results: int | None = None
        execution_hooks = _DurableExecutionHooks(run_id=run_id, owner=owner)
        feature_options = payload.get("features")
        tool_policy = feature_options.get("tool_policy") if isinstance(feature_options, dict) else None
        approval_mode = tool_policy.get("approval_mode") if isinstance(tool_policy, dict) else None
        workspace_write_mode = (
            tool_policy.get("workspace_write_mode")
            if isinstance(tool_policy, dict) and tool_policy.get("workspace_write_mode") in {"ask", "auto_edit"}
            else "ask"
        )
        resume = await _v2_approval_resume(run_id=run_id, owner=owner) if run.execution_protocol_version == 2 else None
        hydrated_turn_ordinal: int | None = None

        async def hydrate_replayed_turn() -> None:
            nonlocal hydrated_turn_ordinal, text, reasoning, pending_deltas, last_delta_flush_at
            if execution_hooks.active_turn_ordinal == hydrated_turn_ordinal:
                return
            projection = await execution_hooks.replay_turn_projection()
            text = [projection["text"]] if projection["text"] else []
            reasoning = [projection["reasoning"]] if projection["reasoning"] else []
            pending_deltas = {"text": [], "reasoning": []}
            last_delta_flush_at = monotonic()
            hydrated_turn_ordinal = execution_hooks.active_turn_ordinal

        async for event in engine.stream(
            model=resolved["model_name"],
            max_tool_calls=int(payload.get("v2_max_tool_calls", _V2_DEFAULT_MAX_TOOL_CALLS)),
            max_model_turns=int(payload.get("v2_max_model_turns", _V2_DEFAULT_MAX_MODEL_TURNS)),
            allowed_direct_effects=tuple(payload.get("allowed_direct_effects") or ()),
            messages=input_messages,
            project_id=project_id,
            user_id=user_id,
            custom_llm_provider=resolved.get("provider_type"),
            api_base=resolved.get("api_base"),
            api_key=resolved.get("api_key"),
            max_tokens=payload.get("max_tokens"),
            temperature=payload.get("temperature"),
            reasoning_effort=payload.get("reasoning_effort"),
            response_format=_provider_response_format(payload.get("features")),
            selected_tool_ids=selected_tool_ids,
            selected_mcp_ids=selected_mcp_ids,
            expected_mcp_credential_versions=expected_mcp_credential_versions,
            expected_extension_fingerprints=expected_extension_fingerprints,
            tools_enabled=_tools_enabled(payload.get("features")),
            managed_search=managed_search,
            managed_fetch=managed_fetch,
            managed_advisor=managed_advisor,
            run_id=run_id,
            execution_hooks=execution_hooks,
            execution_protocol_version=run.execution_protocol_version,
            lumen_snapshot=payload.get("lumen_snapshot") if isinstance(payload.get("lumen_snapshot"), dict) else None,
            lumen_snapshot_frozen=run.execution_protocol_version == 2,
            approval_mode=approval_mode if isinstance(approval_mode, str) else None,
            workspace_write_mode=workspace_write_mode,
            resume=resume,
        ):
            ensure_lease()
            active_message_id = execution_hooks.active_message_id
            if active_message_id is not None and active_message_id != message_id:
                if message_id is not None:
                    if await flush_deltas():
                        return True
                    await finalize_current_turn()
                message_id = active_message_id
                text = []
                reasoning = []
                pending_deltas = {"text": [], "reasoning": []}
                hydrated_turn_ordinal = None
                response_writing = False
            is_durable_replay = event.get("_durable_replay") is True
            if is_durable_replay:
                await hydrate_replayed_turn()
            etype = event.get("type")
            if awaiting_model_response:
                await _set_stage(run_id, "model_response", owner=owner)
                awaiting_model_response = False
            if etype == "token" and event.get("text"):
                await mark_response_writing()
                token = str(event["text"])
                if is_durable_replay:
                    existing = "".join(text)
                    if not token.startswith(existing):
                        raise DurableRunError("provider replay does not extend the committed text projection")
                    token = token[len(existing) :]
                if token:
                    pending_deltas["text"].append(token)
            elif etype == "reasoning" and event.get("text"):
                await mark_response_writing()
                reasoning_delta = str(event["text"])
                if is_durable_replay:
                    existing = "".join(reasoning)
                    if not reasoning_delta.startswith(existing):
                        raise DurableRunError("provider replay does not extend the committed reasoning projection")
                    reasoning_delta = reasoning_delta[len(existing) :]
                if reasoning_delta:
                    pending_deltas["reasoning"].append(reasoning_delta)
            else:
                if await flush_deltas():
                    return True
                if etype == "assistant_tool_calls":
                    calls = event.get("tool_calls")
                    pending_tool_results = len(calls) if isinstance(calls, list) else None
                elif etype == "input.interrupted":
                    payload = event.get("payload")
                    if not isinstance(payload, dict) or run.execution_protocol_version != 2:
                        raise DurableRunError("unexpected durable input interrupt")
                    await _persist_v2_approval_interrupt(run_id=run_id, owner=owner, calls=payload.get("calls"))
                    return True
                elif etype == "tool_call":
                    try:
                        arguments = json.loads(event.get("args") or "{}")
                    except (TypeError, json.JSONDecodeError):
                        arguments = {}
                    tool_name = str(event.get("name") or "tool")
                    await _set_stage(run_id, "tool_execution", owner=owner, tool_name=tool_name)
                    call_id = str(event.get("tool_call_id") or event.get("name"))
                    tool_invocations[call_id] = {
                        "type": "tool_call",
                        "call_id": call_id,
                        "name": tool_name,
                        "arguments": arguments if isinstance(arguments, dict) else {},
                        "status": "running",
                    }
                    if event.get("_durable_journaled") is not True:
                        await _append(
                            run_id,
                            "tool.call.started",
                            {
                                "call_id": str(event.get("tool_call_id") or event.get("name")),
                                "name": tool_name,
                                "arguments": arguments if isinstance(arguments, dict) else {},
                            },
                            owner=owner,
                        )
                elif etype == "tool_result":
                    visible = event.get("hidden") is not True
                    status = _tool_result_status(event.get("status"))
                    error_code = _tool_result_error_code(event.get("error_code"))
                    display_parts = _tool_display_parts(
                        event,
                        content=str(event.get("content") or ""),
                        visible=visible,
                    )
                    if event.get("_durable_journaled") is not True:
                        await _append(
                            run_id,
                            "tool.call.completed",
                            {
                                "call_id": str(event.get("tool_call_id") or event.get("name")),
                                "name": str(event.get("name") or "tool"),
                                "content": display_parts,
                                "status": status,
                                "error_code": error_code,
                            },
                            owner=owner,
                        )
                    call_id = str(event.get("tool_call_id") or event.get("name"))
                    tool_name = str(event.get("name") or "tool")
                    invocation = tool_invocations.setdefault(
                        call_id,
                        {
                            "type": "tool_call",
                            "call_id": call_id,
                            "name": tool_name,
                            "arguments": {},
                            "status": "running",
                        },
                    )
                    invocation["status"] = status
                    if visible:
                        tool_results[call_id] = {
                            "type": "tool_result",
                            "call_id": call_id,
                            "name": tool_name,
                            "content": display_parts,
                            "is_error": status == "failed",
                        }
                    if pending_tool_results is not None:
                        pending_tool_results -= 1
                        if pending_tool_results > 0:
                            continue
                        pending_tool_results = None
                    if message_id is not None:
                        if await flush_deltas():
                            return True
                        await finalize_current_turn()
                        text = []
                        tool_invocations.clear()
                        tool_results.clear()
                        reasoning = []
                        pending_deltas = {"text": [], "reasoning": []}
                        message_id = None
                    await _set_stage(run_id, "model_request", owner=owner)
                    awaiting_model_response = True
                    response_writing = False
                elif etype == "warning":
                    if event.get("code") == "advisor_call_failed":
                        await _append(
                            run_id,
                            "run.warning",
                            {"code": "advisor_call_failed", "safe_message": "Advisor request failed."},
                            owner=owner,
                        )
                elif etype == "citations":
                    for item in event.get("items") or []:
                        if not isinstance(item, dict):
                            continue
                        source_kind = item.get("source_kind", "web")
                        url = item.get("url")
                        document_index = item.get("document_index")
                        if source_kind == "web" and not isinstance(url, str):
                            continue
                        if source_kind == "document" and (not isinstance(document_index, int) or document_index < 0):
                            continue
                        citations.append(
                            {
                                "type": "citation",
                                "citation_id": str(
                                    item.get("citation_id")
                                    or (
                                        f"document-{document_index}-{len(citations) + 1}"
                                        if source_kind == "document"
                                        else f"citation-{len(citations) + 1}"
                                    )
                                ),
                                "source_kind": source_kind,
                                **({"url": url} if isinstance(url, str) else {}),
                                **({"document_index": document_index} if isinstance(document_index, int) else {}),
                                **({"title": item["title"]} if isinstance(item.get("title"), str) else {}),
                                **({"snippet": item["snippet"]} if isinstance(item.get("snippet"), str) else {}),
                                **(
                                    {"start_index": item["start_index"]}
                                    if isinstance(item.get("start_index"), int)
                                    else {}
                                ),
                                **({"end_index": item["end_index"]} if isinstance(item.get("end_index"), int) else {}),
                            }
                        )
                elif etype == "usage":
                    value = event.get("usage")
                    if isinstance(value, dict):
                        usage["prompt_tokens"] = max(0, int(value.get("prompt_tokens") or 0))
                        usage["completion_tokens"] = max(0, int(value.get("completion_tokens") or 0))
                    emitted_tool_usage = event.get("tool_usage")
                    if isinstance(emitted_tool_usage, list):
                        managed_tool_usage = [item for item in emitted_tool_usage if isinstance(item, dict)]
                elif etype == "error":
                    if event.get("code") == "provider_result_unknown":
                        raise DurableRunProviderResultUnknown("provider result is indeterminate")
                    raise DurableRunError(str(event.get("message") or "provider error"))

            if (
                sum(len(chunk) for chunks in pending_deltas.values() for chunk in chunks) >= 128
                or monotonic() - last_delta_flush_at >= 0.1
            ) and await flush_deltas():
                return True

        if await flush_deltas():
            return True

        raw_text = "".join(text)
        parts: list[dict[str, Any]] = []
        terminal_status = "completed"
        terminal_error_code: str | None = None
        terminal_safe_message: str | None = None
        feature_options = payload.get("features")
        response_format = (
            feature_options.get("response_format", {"kind": "text"})
            if isinstance(feature_options, dict)
            else {"kind": "text"}
        )
        if response_format["kind"] == "text":
            if raw_text:
                parts.append({"type": "text", "text": raw_text})
        else:
            try:
                parts.append(parse_structured_output(raw_text, response_format))
            except StructuredOutputError as exc:
                terminal_status = "failed"
                terminal_error_code = "structured_output_invalid"
                terminal_safe_message = "모델 응답이 요청한 구조화 형식과 일치하지 않습니다"
                if raw_text:
                    parts.append({"type": "text", "text": raw_text})
                parts.append(
                    {
                        "type": "structured",
                        "schema_name": response_format.get("name") or "json_object",
                        "schema_version": response_format.get("version") or "1",
                        "value": None,
                        "valid": False,
                        "validation_error": str(exc),
                    }
                )
        if reasoning:
            parts.append({"type": "reasoning", "text": "".join(reasoning), "visibility": "user"})
        parts.extend(citations)
        completed_parts = list(enumerate(parts, start=1 if parts and parts[0]["type"] == "reasoning" else 0))
        ensure_lease()
        usage_cost = credit.usage_cost_from_pricing_snapshot(
            pricing_snapshot,
            prompt_tokens=usage["prompt_tokens"],
            completion_tokens=usage["completion_tokens"],
        )
        managed_cost, managed_components = _managed_usage_components(
            managed_tool_usage,
            pricing_snapshot=pricing_snapshot,
            model_name=resolved["model_name"],
        )
        if managed_cost:
            usage_cost = UsageCost(
                raw_cost=(usage_cost.raw_cost + managed_cost).quantize(
                    Decimal("0.0000000001"), rounding=ROUND_HALF_EVEN
                ),
                input_cost=usage_cost.input_cost,
                output_cost=usage_cost.output_cost,
                pricing_status=usage_cost.pricing_status,
                pricing_snapshot=usage_cost.pricing_snapshot,
            )
        usage_components = []
        for kind, quantity, cost in (
            ("input_tokens", usage["prompt_tokens"], usage_cost.input_cost),
            ("output_tokens", usage["completion_tokens"], usage_cost.output_cost),
        ):
            unit_price = cost / quantity if quantity else 0
            usage_components.append(
                {
                    "segment_id": "executor:aggregate",
                    "kind": kind,
                    "quantity": str(quantity),
                    "unit": "token",
                    "unit_price_usd": format(unit_price, "f"),
                    "cost_usd": format(cost, "f"),
                    "source": "executor",
                    "model_name": resolved["model_name"],
                    "metadata": {},
                }
            )
        usage_components.extend(managed_components)
        usage_record = {
            "usage_cost": usage_cost,
            "prompt_tokens": usage["prompt_tokens"],
            "completion_tokens": usage["completion_tokens"],
            "usage_components": usage_components,
            "model_name": resolved["model_name"],
            "provider_name": resolved["provider_name"],
            "margin_multiplier": pricing_snapshot["margin_multiplier"],
            "credit_per_usd": pricing_snapshot["chat_credit_per_usd"],
        }
        message_finalization = (
            {
                "conversation_id": conversation_id,
                "message_id": int(message_id),
                "content": raw_text,
                "reasoning": "".join(reasoning) or None,
                "model_name": resolved["model_name"],
                "canonical_parts": parts,
                "token_prompt": usage["prompt_tokens"],
                "token_completion": usage["completion_tokens"],
                "message_status": "complete" if terminal_status == "completed" else "failed",
            }
            if conversation_id is not None and message_id is not None
            else None
        )
        if conversation_id is None:
            await _append_temp_history(run_id, payload, parts, owner=owner)
        await _finish(
            run_id,
            status=terminal_status,
            message_id=message_id,
            owner=owner,
            error_code=terminal_error_code,
            safe_message=terminal_safe_message,
            usage_record=usage_record,
            message_finalization=message_finalization,
            completed_parts=completed_parts,
        )
    except DurableRunLeaseLost:
        logger.warning("stopped stale durable chat worker run_id=%s owner=%s", run_id, owner)
    except asyncio.CancelledError:
        logger.info("durable chat worker interrupted; deferring run recovery run_id=%s owner=%s", run_id, owner)
        raise
    except DurableRunProviderResultUnknown:
        await _finish(
            run_id,
            status="failed",
            message_id=message_id,
            owner=owner,
            error_code="provider_result_unknown",
            safe_message="이전 모델 호출 결과를 안전하게 확인할 수 없습니다",
        )
    except DurableRunProtocolMismatch:
        await _finish(
            run_id,
            status="failed",
            message_id=message_id,
            owner=owner,
            error_code="execution_protocol_mismatch",
            safe_message="chat run execution protocol is unavailable",
        )
    except Exception:
        try:
            await flush_deltas(check_cancel=False)
        except Exception:
            logger.exception("failed to persist buffered chat deltas run_id=%s", run_id)
        logger.exception("durable chat provider execution failed run_id=%s", run_id)
        await _finish(
            run_id,
            status="failed",
            message_id=message_id,
            owner=owner,
            error_code="provider_failed",
            safe_message="chat provider failed",
        )
    finally:
        heartbeat_stop.set()
        heartbeat_task.cancel()
        await asyncio.gather(heartbeat_task, return_exceptions=True)
    return True


async def queued_run_ids(*, limit: int = 4) -> list[str]:
    factory = _factory()
    async with factory() as session:
        return list(
            (
                await session.execute(
                    select(ChatRun.id).where(ChatRun.status == "queued").order_by(ChatRun.created_at).limit(limit)
                )
            ).scalars()
        )
