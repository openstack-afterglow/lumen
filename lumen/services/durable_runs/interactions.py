"""Durable approval and interaction resolution."""

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from lumen.config import get_settings
from lumen.crypto import decrypt_chat_content, derive_encryption_subkey, encrypt_chat_content
from lumen.models.chat_agent_platform import ChatRunInteraction
from lumen.models.chat_contracts import validate_chat_parts
from lumen.models.chat_runs import ChatRun, ChatToolApproval
from lumen.services import ssrf
from lumen.services.execution_protocol import v2_runtime_ready
from lumen.services.run_protocol_v2 import transition_allowed
from lumen.services.run_store import RunStoreError, append_event, load_owned_run

from .common import _event, _factory, _now, wake_run
from .errors import (
    DurableRunConflict,
    DurableRunError,
    DurableRunInputError,
    DurableRunNotFound,
    DurableRunProtocolMismatch,
)
from .lifecycle import _require_owned_running_lease


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
