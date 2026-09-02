"""Durable run wakeup, cancellation, recovery, and leases."""

from datetime import UTC, timedelta

from sqlalchemy import delete, select, update

from lumen.models.chat_agent_platform import ChatRunInteraction
from lumen.models.chat_contracts import ChatRunResponse
from lumen.models.chat_runs import (
    ChatRun,
    ChatRunEventRow,
    ChatRunSegment,
    ChatRunTurn,
    ChatTempThread,
    ChatToolApproval,
)
from lumen.services.run_protocol_v2 import transition_allowed
from lumen.services.run_store import (
    NONTERMINAL,
    RunStoreError,
    append_event,
    fail_unresolved_segment,
    load_owned_run,
    renew_run_lease,
)

from .common import _LEASE_SECONDS, _event, _factory, _now
from .errors import DurableRunConflict, DurableRunLeaseLost, DurableRunNotFound


def _require_owned_running_lease(run: ChatRun, owner: str) -> None:
    expires_at = run.lease_expires_at
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if run.status != "running" or run.lease_owner != owner or expires_at is None or expires_at <= _now():
        raise DurableRunLeaseLost(f"lease lost for {run.id}")


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


async def request_cancelled(*, run_id: str, project_id: str, user_id: str) -> ChatRunResponse:
    from .execution import _cancel_streaming_assistant_message
    from .interactions import _approval_resolved_payload

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


async def recover_stale_runs(*, owner: str, limit: int = 16) -> list[str]:
    """Requeue pre-I/O and completed work; indeterminate provider calls fail closed."""
    from .execution import _finish

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
