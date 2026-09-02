"""MySQL-authoritative durable chat run and event journal operations."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen.crypto import decrypt_chat_content, encrypt_chat_content
from lumen.models.chat_contracts import ChatRunEvent, validate_chat_run_event
from lumen.models.chat_runs import ChatRun, ChatRunEventRow, ChatRunSegment


class RunStoreError(RuntimeError):
    pass


NONTERMINAL = {"queued", "running", "awaiting_approval", "awaiting_input", "waiting_children", "finalizing"}
TERMINAL = {"completed", "failed", "canceled"}


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


_MAX_SEGMENT_RESULT_BYTES = 64 * 1024
_MAX_PROVIDER_RESULT_BYTES = 4 * 1024 * 1024
_MAX_SEGMENT_USAGE_BYTES = 16 * 1024


def _encode_segment_payload(payload: Mapping[str, Any] | None, *, max_bytes: int) -> str | None:
    if payload is None:
        return None
    try:
        encoded = json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise RunStoreError("segment payload must be canonical JSON") from exc
    if len(encoded) > max_bytes:
        raise RunStoreError("segment payload exceeds its durable storage limit")
    return encrypt_chat_content(encoded.decode("utf-8"))


def load_segment_payload(ciphertext: str | None) -> dict[str, Any] | None:
    if ciphertext is None:
        return None
    try:
        payload = json.loads(decrypt_chat_content(ciphertext))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RunStoreError("segment payload is not valid encrypted JSON") from exc
    if not isinstance(payload, dict):
        raise RunStoreError("segment payload must decode to an object")
    return payload


def _event_payload(event: ChatRunEvent) -> str:
    return encrypt_chat_content(json.dumps(event.payload.model_dump(mode="json", by_alias=True), separators=(",", ":")))


def _normalize_replayed_payload(row: ChatRunEventRow, payload: object) -> object:
    """Supply v2 resolution provenance for pre-provenance journal rows."""
    if row.event_type != "tool.approval_resolved" or not isinstance(payload, dict):
        return payload
    return {
        **payload,
        "decided_by_user_id": payload.get("decided_by_user_id"),
        "decided_at": payload.get("decided_at", _as_utc(row.created_at).isoformat()),
    }


async def load_owned_run(
    session: AsyncSession, run_id: str, *, user_id: str, project_id: str, lock: bool = False
) -> ChatRun:
    statement: Select[tuple[ChatRun]] = select(ChatRun).where(
        ChatRun.id == run_id,
        ChatRun.user_id == user_id,
        ChatRun.project_id == project_id,
    )
    if lock:
        statement = statement.with_for_update()
    run = (await session.execute(statement)).scalar_one_or_none()
    if run is None:
        raise RunStoreError("chat run was not found")
    return run


async def append_event(session: AsyncSession, run: ChatRun, event: ChatRunEvent) -> ChatRunEventRow:
    """Append exactly the expected next event while the caller holds the run row lock."""
    validated = validate_chat_run_event(event)
    expected = run.last_seq + 1
    if validated.run_id != run.id or validated.seq != expected:
        raise RunStoreError("event sequence is not the next durable run sequence")
    row = ChatRunEventRow(
        run_id=run.id, seq=validated.seq, event_type=validated.type, payload=_event_payload(validated)
    )
    session.add(row)
    run.last_seq = validated.seq
    return row


async def replay_events(session: AsyncSession, run: ChatRun, *, after_seq: int) -> list[ChatRunEvent]:
    if after_seq < 0 or after_seq > run.last_seq:
        raise RunStoreError("event cursor is outside retained journal")
    rows = (
        await session.execute(
            select(ChatRunEventRow)
            .where(ChatRunEventRow.run_id == run.id, ChatRunEventRow.seq > after_seq)
            .order_by(ChatRunEventRow.seq)
        )
    ).scalars()
    events: list[ChatRunEvent] = []
    for row in rows:
        payload = _normalize_replayed_payload(row, json.loads(decrypt_chat_content(row.payload)))
        events.append(
            validate_chat_run_event(
                {
                    "event_id": f"{run.id}:{row.seq}",
                    "run_id": run.id,
                    "seq": row.seq,
                    "type": row.event_type,
                    "created_at": _as_utc(row.created_at).isoformat(),
                    "payload": payload,
                }
            )
        )
    return events


async def claim_queued_run(
    session: AsyncSession, run_id: str, *, owner: str, lease_seconds: int = 45
) -> ChatRun | None:
    run = (await session.execute(select(ChatRun).where(ChatRun.id == run_id).with_for_update())).scalar_one_or_none()
    if run is None or run.status != "queued" or run.cancel_requested_at is not None:
        return None
    now = datetime.now(UTC)
    run.status = "running"
    run.lease_owner = owner
    run.lease_expires_at = now + timedelta(seconds=lease_seconds)
    return run


async def renew_run_lease(session: AsyncSession, run_id: str, *, owner: str, lease_seconds: int = 45) -> ChatRun | None:
    """Extend only the currently-owned running lease; a stale worker loses authority."""
    run = (await session.execute(select(ChatRun).where(ChatRun.id == run_id).with_for_update())).scalar_one_or_none()
    now = datetime.now(UTC)
    if run is None:
        return None
    expires_at = run.lease_expires_at
    if run.status != "running" or run.lease_owner != owner or expires_at is None or _as_utc(expires_at) <= now:
        return None
    run.lease_expires_at = now + timedelta(seconds=lease_seconds)
    return run


async def prepare_segment(
    session: AsyncSession,
    run: ChatRun,
    *,
    segment_id: str,
    ordinal: int,
    endpoint: str,
    turn_ordinal: int | None = None,
    call_id: str | None = None,
) -> ChatRunSegment:
    """Create or load a durable execution boundary while the caller owns ``run``."""
    segment = (
        await session.execute(
            select(ChatRunSegment)
            .where(ChatRunSegment.run_id == run.id, ChatRunSegment.segment_id == segment_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if segment is None:
        segment = ChatRunSegment(
            run_id=run.id,
            segment_id=segment_id,
            ordinal=ordinal,
            endpoint=endpoint,
            turn_ordinal=turn_ordinal,
            call_id=call_id,
            status="prepared",
        )
        session.add(segment)
        return segment
    if (
        segment.ordinal != ordinal
        or segment.endpoint != endpoint
        or segment.turn_ordinal != turn_ordinal
        or segment.call_id != call_id
    ):
        raise RunStoreError("segment identity conflicts with persisted execution boundary")
    return segment


def begin_segment_io(segment: ChatRunSegment) -> None:
    """Fence non-idempotent I/O; started segments may never be invoked again blindly."""
    if segment.status != "prepared":
        raise RunStoreError(f"segment cannot begin external I/O from {segment.status}")
    segment.status = "provider_started"
    segment.provider_started_at = datetime.now(UTC)


def complete_segment_io(
    segment: ChatRunSegment,
    *,
    result_payload: Mapping[str, Any] | None,
    usage_payload: Mapping[str, Any] | None,
) -> None:
    """Encrypt bounded observed result/usage before the caller commits the checkpoint boundary."""
    if segment.status != "provider_started":
        raise RunStoreError(f"segment cannot complete from {segment.status}")
    segment.status = "completed"
    result_limit = _MAX_PROVIDER_RESULT_BYTES if segment.endpoint == "chat_completions" else _MAX_SEGMENT_RESULT_BYTES
    segment.result_payload = _encode_segment_payload(result_payload, max_bytes=result_limit)
    segment.usage_payload = _encode_segment_payload(usage_payload, max_bytes=_MAX_SEGMENT_USAGE_BYTES)
    segment.completed_at = datetime.now(UTC)


def fail_unresolved_segment(segment: ChatRunSegment, *, error_code: str) -> None:
    """Terminalize an observed-but-unresolved call without risking a duplicate side effect."""
    if segment.status != "provider_started":
        raise RunStoreError(f"segment cannot become result-unknown from {segment.status}")
    segment.status = "failed"
    segment.result_payload = _encode_segment_payload(
        {"code": error_code},
        max_bytes=_MAX_SEGMENT_RESULT_BYTES,
    )
    segment.completed_at = datetime.now(UTC)


async def request_cancel(session: AsyncSession, run_id: str, *, user_id: str, project_id: str) -> ChatRun:
    """Persist an idempotent DB-authoritative cancellation request under the run lock."""
    run = await load_owned_run(session, run_id, user_id=user_id, project_id=project_id, lock=True)
    if run.status in TERMINAL:
        return run
    if run.cancel_requested_at is None:
        run.cancel_requested_at = datetime.now(UTC)
    return run


async def transition_run(session: AsyncSession, run: ChatRun, *, expected: str, target: str) -> ChatRun:
    """Apply only legal durable state transitions while the caller owns the run row lock."""
    allowed = {
        ("queued", "running"),
        ("running", "awaiting_approval"),
        ("awaiting_approval", "queued"),
        ("queued", "finalizing"),
        ("running", "finalizing"),
        ("awaiting_approval", "finalizing"),
        ("finalizing", "completed"),
        ("finalizing", "failed"),
        ("finalizing", "canceled"),
    }
    if run.status != expected:
        raise RunStoreError(f"run transition expected {expected}, found {run.status}")
    if (expected, target) not in allowed:
        raise RunStoreError(f"illegal run transition {expected}->{target}")
    run.status = target
    return run


async def claim_finalizer(session: AsyncSession, run_id: str, *, owner: str, lease_seconds: int = 45) -> ChatRun | None:
    """Claim a finalizer lease without ever returning a run to a provider-call state."""
    run = (await session.execute(select(ChatRun).where(ChatRun.id == run_id).with_for_update())).scalar_one_or_none()
    if run is None or run.status not in NONTERMINAL:
        return None
    now = datetime.now(UTC)
    if (
        run.finalizer_lease_expires_at is not None
        and _as_utc(run.finalizer_lease_expires_at) > now
        and run.finalizer_lease_owner != owner
    ):
        return None
    if run.status != "finalizing":
        await transition_run(session, run, expected=run.status, target="finalizing")
    run.finalizer_lease_owner = owner
    run.finalizer_lease_expires_at = now + timedelta(seconds=lease_seconds)
    return run
