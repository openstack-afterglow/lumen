"""Durable first-response title jobs.

Admission only snapshots the first exchange and frozen route.  This module owns
leased execution and the final title/usage transaction; it never runs from an
HTTP GET or completion request coroutine.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import or_, select

from lumen.crypto import decrypt_chat_content, encrypt_chat_content
from lumen.models.chat_db import ChatConversation, ChatMessage, ChatUsageLog
from lumen.models.chat_jobs import ChatJob
from lumen.models.chat_runs import ChatRun
from lumen.services import credit, litellm_client, title_summary
from lumen.services.providers import routing as ps

logger = logging.getLogger(__name__)

_KIND = "title_generate"
_LEASE_SECONDS = 120
_EXPECTED_REVISION = 1


def _now() -> datetime:
    return datetime.now(UTC)


def _json_load(ciphertext: str | None) -> dict[str, Any]:
    if not ciphertext:
        raise ValueError("title job payload is missing")
    value = json.loads(decrypt_chat_content(ciphertext))
    if not isinstance(value, dict):
        raise ValueError("title job payload is invalid")
    return value


def _enc_payload(value: dict[str, Any]) -> str:
    return encrypt_chat_content(json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str))


def _route_snapshot(route: object) -> dict[str, Any] | None:
    if not isinstance(route, dict):
        return None
    # Credentials never belong in a route snapshot.  The worker resolves the
    # exact provider/model config hash before invoking the frozen route.
    return {key: value for key, value in route.items() if key not in {"api_key", "secret", "credential"}}


def _summary_route(run: ChatRun) -> dict[str, Any] | None:
    capabilities = run.capability_snapshot if isinstance(run.capability_snapshot, dict) else {}
    route = capabilities.get("summary_route")
    if not isinstance(route, dict):
        # Older admitted runs have no dedicated route; their execution route is
        # the approved fallback for first-title generation.
        route = capabilities
    snapshot = _route_snapshot(route)
    if not snapshot or not isinstance(snapshot.get("model_name"), str):
        return None
    return snapshot


def _summary_prices(run: ChatRun) -> dict[str, Any]:
    pricing = run.pricing_snapshot if isinstance(run.pricing_snapshot, dict) else {}
    summary = pricing.get("summary_route")
    return dict(summary) if isinstance(summary, dict) else dict(pricing)


async def _get_for_update(session, model, key):
    try:
        return await session.get(model, key, with_for_update=True)
    except TypeError:
        return await session.get(model, key)


def _terminal_title_status(error_code: str) -> str:
    return "unavailable" if error_code in {"route_unavailable", "quota_exceeded"} else "failed"


async def _terminalize_pending_title(
    session, job: ChatJob, error_code: str, *, payload: dict[str, Any] | None = None
) -> None:
    """Close only the title reservation owned by this job's revision."""
    if payload is None:
        payload = _json_load(job.payload)
    expected = int(payload.get("expected_title_revision", _EXPECTED_REVISION))
    conversation = await _get_for_update(session, ChatConversation, job.conversation_id)
    if conversation is not None and conversation.title_revision == expected and conversation.title_status == "pending":
        conversation.title_status = _terminal_title_status(error_code)


async def _requeue_stored_result(job_id: str, *, owner: str) -> None:
    """Make a durably stored provider result immediately replayable."""
    from lumen.db import get_session_factory

    factory = get_session_factory()
    if factory is None:
        return
    async with factory() as session, session.begin():
        row = await _get_for_update(session, ChatJob, job_id)
        progress = row.progress if row is not None and isinstance(row.progress, dict) else {}
        if row is None or row.status != "running" or row.lease_owner != owner or progress.get("state") != "completed":
            return
        row.status = "queued"
        row.next_at = _now()
        row.error_code = "title_apply_unavailable"
        row.lease_owner = None
        row.lease_expires_at = None


async def enqueue_completed_run_in_transaction(session, run: ChatRun) -> None:
    """Reserve the one first-title revision after a successful root completion.

    The caller already owns the completion transaction.  All source text is
    encrypted in the job payload and no model/provider work occurs here.
    """
    if (
        run.status != "completed"
        or run.run_kind != "completion"
        or run.run_scope != "persistent"
        or run.parent_run_id is not None
        or run.conversation_id is None
        or run.user_message_id is None
        or run.assistant_message_id is None
    ):
        return
    route = _summary_route(run)
    if route is None:
        return
    conversation = await _get_for_update(session, ChatConversation, run.conversation_id)
    if conversation is None or conversation.title_source != "auto" or int(conversation.title_revision or 0) != 0:
        return
    # Locking the parent conversation before the job gives duplicate finishers
    # a single CAS winner; the unique key is retained as a second fence.
    existing = (
        await session.execute(
            select(ChatJob.id).where(ChatJob.idempotency_key == f"title:first:{run.conversation_id}").limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return
    user_message = await session.get(ChatMessage, run.user_message_id)
    assistant_message = await session.get(ChatMessage, run.assistant_message_id)
    if (
        user_message is None
        or assistant_message is None
        or user_message.conversation_id != run.conversation_id
        or assistant_message.conversation_id != run.conversation_id
        or user_message.role != "user"
        or assistant_message.role != "assistant"
        or user_message.status not in {"complete", "completed"}
        or assistant_message.status not in {"complete", "completed"}
    ):
        return
    user_content = decrypt_chat_content(user_message.content or "")
    assistant_content = decrypt_chat_content(assistant_message.content or "")
    if not user_content.strip() or not assistant_content.strip():
        return
    payload = {
        "conversation_id": run.conversation_id,
        "project_id": run.project_id,
        "user_id": run.user_id,
        "run_id": run.id,
        "expected_title_revision": _EXPECTED_REVISION,
        "exchange": [
            {"role": "user", "content": user_content},
            {"role": "assistant", "content": assistant_content},
        ],
        "summary_route": route,
        "pricing_snapshot": _summary_prices(run),
    }
    conversation.title_revision = _EXPECTED_REVISION
    conversation.title_status = "pending"
    session.add(
        ChatJob(
            id=str(uuid.uuid4()),
            kind=_KIND,
            run_id=run.id,
            conversation_id=run.conversation_id,
            payload=_enc_payload(payload),
            progress={"state": "prepared", "expected_revision": _EXPECTED_REVISION},
            status="queued",
            idempotency_key=f"title:first:{run.conversation_id}",
        )
    )


async def _claim_one(*, owner: str) -> dict[str, Any] | None:
    from lumen.db import get_session_factory

    factory = get_session_factory()
    if factory is None:
        return None
    now = _now()
    async with factory() as session, session.begin():
        job = (
            await session.execute(
                select(ChatJob)
                .where(
                    ChatJob.kind == _KIND,
                    or_(
                        (ChatJob.status == "queued") & (ChatJob.next_at <= now),
                        (ChatJob.status == "running")
                        & (ChatJob.lease_expires_at.is_not(None))
                        & (ChatJob.lease_expires_at < now),
                    ),
                )
                .order_by(ChatJob.next_at, ChatJob.created_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
        ).scalar_one_or_none()
        if job is None:
            return None
        progress = job.progress if isinstance(job.progress, dict) else {}
        state = progress.get("state")
        if job.status == "running" and state == "provider_started":
            payload = _json_load(job.payload)
            await _terminalize_pending_title(session, job, "provider_result_unknown", payload=payload)
            job.status = "failed"
            job.error_code = "provider_result_unknown"
            job.progress = {
                "state": "failed",
                "expected_revision": int(payload.get("expected_title_revision", _EXPECTED_REVISION)),
            }
            job.lease_owner = None
            job.lease_expires_at = None
            return {"terminal": True}
        job.status = "running"
        job.lease_owner = owner
        job.lease_expires_at = now + timedelta(seconds=_LEASE_SECONDS)
        job.attempts = int(job.attempts or 0) + 1
        return {
            "job_id": job.id,
            "run_id": job.run_id,
            "conversation_id": job.conversation_id,
            "owner": owner,
            "payload": _json_load(job.payload),
            "replay": state == "completed",
            "attempts": job.attempts,
        }


async def _mark_provider_started(job_id: str, *, owner: str, expected_revision: int) -> None:
    from lumen.db import get_session_factory

    factory = get_session_factory()
    if factory is None:
        raise RuntimeError("chat DB is unavailable")
    async with factory() as session, session.begin():
        job = await _get_for_update(session, ChatJob, job_id)
        if job is None or job.status != "running" or job.lease_owner != owner:
            raise RuntimeError("title job lease lost")
        job.progress = {"state": "provider_started", "expected_revision": expected_revision}
        job.lease_expires_at = _now() + timedelta(seconds=_LEASE_SECONDS)


async def _store_result(job: dict[str, Any], result: title_summary.TitleResult) -> None:
    from lumen.db import get_session_factory

    factory = get_session_factory()
    if factory is None:
        raise RuntimeError("chat DB is unavailable")
    async with factory() as session, session.begin():
        row = await _get_for_update(session, ChatJob, job["job_id"])
        if row is None or row.status != "running" or row.lease_owner != job["owner"]:
            return
        payload = _json_load(row.payload)
        payload["result"] = {
            "title": result.title,
            "prompt_tokens": result.prompt_tokens,
            "completion_tokens": result.completion_tokens,
            "model_name": result.model_name,
        }
        row.payload = _enc_payload(payload)
        row.progress = {
            "state": "completed",
            "expected_revision": int(payload.get("expected_title_revision", _EXPECTED_REVISION)),
        }
        row.lease_expires_at = _now() + timedelta(seconds=_LEASE_SECONDS)


async def _mark_failed(job_id: str, *, owner: str, error_code: str) -> None:
    from lumen.db import get_session_factory

    factory = get_session_factory()
    if factory is None:
        return
    async with factory() as session, session.begin():
        row = await _get_for_update(session, ChatJob, job_id)
        if row is None or row.status != "running" or row.lease_owner != owner:
            return
        row.status = "failed"
        row.error_code = error_code
        payload = _json_load(row.payload)
        row.progress = {
            "state": "failed",
            "expected_revision": int(payload.get("expected_title_revision", _EXPECTED_REVISION)),
        }
        await _terminalize_pending_title(session, row, error_code, payload=payload)


async def _retry_before_provider(job_id: str, *, owner: str, attempts: int) -> None:
    from lumen.db import get_session_factory

    factory = get_session_factory()
    if factory is None:
        return
    async with factory() as session, session.begin():
        row = await _get_for_update(session, ChatJob, job_id)
        if attempts <= 3:
            row.status = "queued"
            row.next_at = _now() + timedelta(seconds=2**attempts)
            row.error_code = "title_storage_unavailable"
            row.lease_owner = None
            row.lease_expires_at = None
        else:
            payload = _json_load(row.payload)
            row.status = "failed"
            row.error_code = "title_storage_unavailable"
            row.lease_owner = None
            row.lease_expires_at = None
            row.progress = {
                "state": "failed",
                "expected_revision": int(payload.get("expected_title_revision", _EXPECTED_REVISION)),
            }
            await _terminalize_pending_title(session, row, "title_storage_unavailable", payload=payload)


async def _apply_result(job: dict[str, Any]) -> bool:
    """CAS title and append independent system usage exactly once."""
    from lumen.db import get_session_factory

    factory = get_session_factory()
    if factory is None:
        raise RuntimeError("chat DB is unavailable")
    async with factory() as session, session.begin():
        row = await _get_for_update(session, ChatJob, job["job_id"])
        if row is None or row.status != "running" or row.lease_owner != job["owner"]:
            return False
        payload = _json_load(row.payload)
        result = payload.get("result")
        if not isinstance(result, dict) or not isinstance(result.get("title"), str):
            raise ValueError("title result is missing")
        conversation = await _get_for_update(session, ChatConversation, row.conversation_id)
        if conversation is None:
            row.status = "failed"
            row.error_code = "conversation_unavailable"
            return False
        expected = int(payload.get("expected_title_revision", _EXPECTED_REVISION))
        title_is_current = (
            conversation.title_source == "auto"
            and conversation.title_revision == expected
            and conversation.title_status == "pending"
        )
        if title_is_current:
            conversation.title = encrypt_chat_content(result["title"])
            conversation.title_status = "ready"
        event_id = f"title:{conversation.id}:1"
        existing_usage = (
            await session.execute(select(ChatUsageLog.id).where(ChatUsageLog.event_id == event_id).limit(1))
        ).scalar_one_or_none()
        if existing_usage is None:
            pricing = payload.get("pricing_snapshot")
            pricing = pricing if isinstance(pricing, dict) else {}
            model_name = str(result.get("model_name") or payload.get("summary_route", {}).get("model_name") or "")
            usage_cost = litellm_client.cost_from_usage(
                model_name,
                int(result.get("prompt_tokens", 0)),
                int(result.get("completion_tokens", 0)),
                input_price_per_token=_decimal_or_none(pricing.get("input_price_per_token")),
                output_price_per_token=_decimal_or_none(pricing.get("output_price_per_token")),
                price_source=pricing.get("price_source"),
                provider_type=payload.get("summary_route", {}).get("provider_type"),
            )
            await credit.apply_usage_in_transaction(
                session,
                event_id=event_id,
                user_id=str(payload["user_id"]),
                project_id=str(payload["project_id"]),
                model_name=model_name,
                provider=pricing.get("provider_name") or payload.get("summary_route", {}).get("provider_name"),
                prompt_tokens=int(result.get("prompt_tokens", 0)),
                completion_tokens=int(result.get("completion_tokens", 0)),
                usage_cost=usage_cost,
                margin_multiplier=_decimal_or_none(pricing.get("margin_multiplier")) or Decimal("1"),
                credit_per_usd=_decimal_or_none(pricing.get("chat_credit_per_usd")),
                conversation_id=conversation.id,
                source="system",
                api_key_id=None,
                run_id=str(payload.get("run_id") or "") or None,
                charge_wallet=False,
                usage_components=[
                    {
                        "kind": "input_tokens",
                        "quantity": int(result.get("prompt_tokens", 0)),
                        "metadata": {"operation": "title"},
                    },
                    {
                        "kind": "output_tokens",
                        "quantity": int(result.get("completion_tokens", 0)),
                        "metadata": {"operation": "title"},
                    },
                ],
            )
        row.status = "completed"
        row.error_code = None
        row.lease_owner = None
        row.lease_expires_at = None
        row.progress = {"state": "completed", "expected_revision": expected}
        return True


def _decimal_or_none(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        result = Decimal(str(value))
    except Exception:
        return None
    return result if result.is_finite() else None


async def process_one(*, owner: str) -> bool:
    """Claim and process at most one title job without blocking completions."""
    claimed = await _claim_one(owner=owner)
    if claimed is None:
        return False
    if claimed.get("terminal"):
        return True
    job_id = claimed["job_id"]
    payload = claimed["payload"]
    expected = int(payload.get("expected_title_revision", _EXPECTED_REVISION))
    if claimed.get("replay"):
        await _apply_result(claimed)
        return True
    try:
        route = payload.get("summary_route")
        if not isinstance(route, dict):
            await _mark_failed(job_id, owner=owner, error_code="route_unavailable")
            return True
        resolved = route
        if isinstance(route.get("provider_id"), int) and isinstance(route.get("model_id"), int):
            resolved = await ps.resolve_model_snapshot(route)
            if resolved is None:
                await _mark_failed(job_id, owner=owner, error_code="route_unavailable")
                return True
        # Quota/storage checks happen before the write-ahead provider fence;
        # failures here are safe to retry without risking a duplicate call.
        await credit.precheck(str(payload["user_id"]), str(payload["project_id"]), None)
        await _mark_provider_started(job_id, owner=owner, expected_revision=expected)
    except credit.QuotaExceeded:
        await _mark_failed(job_id, owner=owner, error_code="quota_exceeded")
        return True
    except Exception:
        logger.warning("title job preflight failed job=%s", job_id, exc_info=True)
        await _retry_before_provider(job_id, owner=owner, attempts=int(claimed.get("attempts", 1)))
        return True
    stored = False
    try:
        result = await title_summary.generate_title(exchange=payload.get("exchange", []), route=resolved)
        await _store_result(claimed, result)
        stored = True
        await _apply_result({**claimed, "replay": True})
    except Exception:
        logger.warning("first title job failed job=%s", job_id, exc_info=True)
        if stored:
            # The provider result is already durable.  A failed apply transaction
            # must be replayed, never paid for with a second provider request.
            await _requeue_stored_result(job_id, owner=claimed["owner"])
        else:
            # Provider I/O started but no result fence was committed; do not retry
            # an ambiguous provider call.
            await _mark_failed(job_id, owner=owner, error_code="title_generation_failed")
    return True
