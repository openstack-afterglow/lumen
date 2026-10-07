"""Owner-scoped batch admission and bounded, registration-owned orchestration.

The coordinator never invokes inference. Requests use the durable single-call
executors; item results are immutable publication snapshots, not a second ledger.
All locked transactions follow project queue -> batch -> item -> run.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import wraps
from typing import Literal
from uuid import UUID, uuid4, uuid5

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.mysql import insert
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeout

from lumen.config import get_settings
from lumen.crypto import decrypt_chat_content, encrypt_chat_content
from lumen.db import get_session_factory, is_db_available, mark_db_unhealthy
from lumen.models.batch_contracts import (
    BATCH_ENDPOINTS,
    NativeBatchCreateRequest,
    OpenAIBatchCreateRequest,
    OpenAIBatchRow,
    validate_batch_body,
)
from lumen.models.chat_assets import ChatAsset
from lumen.models.chat_batches import ChatBatch, ChatBatchFile, ChatBatchItem, ChatBatchProjectQueue
from lumen.models.chat_runs import ChatModelCallReservation, ChatRun, ChatRunEventRow, ChatRunSegment
from lumen.services import auxiliary
from lumen.services.api_key_store import ApiKeyAuthorityUnavailable, ApiKeyForbidden, current_api_key_scopes
from lumen.services.credit import QuotaExceeded
from lumen.services.durable_runs import api_completion, audio, images, lifecycle
from lumen.services.durable_runs.budgets import retry_deadlocks
from lumen.services.durable_runs.common import wake_run
from lumen.services.durable_runs.errors import DurableRunConflict, DurableRunInputError
from lumen.services.durable_runs.media import MediaAuthorizationRevoked
from lumen.services.inference_authority import completion_scopes
from lumen.services.run_store import load_segment_payload
from lumen.services.worker_routing import use_read_committed


class BatchError(Exception):
    code = "batch_error"
    status_code = 400

    def __init__(self, code: str | None = None, message: str | None = None, *, start_byte: int | None = None):
        self.code = code or type(self).code
        self.message = message or self.code.replace("_", " ")
        self.start_byte = start_byte
        super().__init__(self.message)


class BatchNotFound(BatchError):
    code = "batch_not_found"
    status_code = 404


class BatchConflict(BatchError):
    code = "batch_conflict"
    status_code = 409


class BatchInputError(BatchError):
    code = "invalid_batch_input"
    status_code = 400


class BatchUnavailable(BatchError):
    code = "batch_unavailable"
    status_code = 503


@dataclass(frozen=True)
class BatchView:
    id: str
    contract: Literal["native", "openai"]
    endpoint: str | None
    status: str
    metadata: dict[str, str]
    errors: list[dict]
    input_file_id: str | None
    output_file_id: str | None
    error_file_id: str | None
    output_expires_after_seconds: int | None
    counts: dict[str, int]
    created_at: datetime
    expires_at: datetime
    in_progress_at: datetime | None
    finalizing_at: datetime | None
    completed_at: datetime | None
    failed_at: datetime | None
    cancelling_at: datetime | None
    cancelled_at: datetime | None
    expired_at: datetime | None


@dataclass(frozen=True)
class BatchItemView:
    custom_id: str
    ordinal: int
    operation: str
    run_id: str | None
    status: str
    response: dict | None
    error: dict | None
    settlement_status: str


_STATES = ("pending", "queued", "running", "completed", "failed", "cancelled", "expired", "unknown")
_TERMINAL = frozenset(_STATES[3:])
_ACTIVE_BATCHES = ("validating", "in_progress", "cancelling", "finalizing")
_STOP_FREEZE_PAGE = 5000
_TIMES = ("created_at", "expires_at", "in_progress_at", "finalizing_at", "completed_at", "failed_at",
          "cancelling_at", "cancelled_at", "expired_at")
_step_lock = asyncio.Lock()
_last_project: str | None = None


def _now():
    return datetime.now(UTC)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=value.tzinfo or UTC)


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _seal(value) -> str:
    return encrypt_chat_content(_json(value))


def _unseal(value: str):
    return json.loads(decrypt_chat_content(value))


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _factory():
    if not get_settings().batch_enabled:
        raise BatchUnavailable()
    factory = get_session_factory()
    if factory is None or not is_db_available():
        raise BatchUnavailable()
    return factory


def _view(row: ChatBatch) -> BatchView:
    return BatchView(
        id=row.id, contract=row.contract, endpoint=row.endpoint, status=row.status,
        metadata=dict(row.metadata_json), errors=list(row.validation_errors),
        input_file_id=row.input_file_id, output_file_id=row.output_file_id, error_file_id=row.error_file_id,
        output_expires_after_seconds=row.output_expires_after_seconds,
        counts={"total": row.request_total, **{state: getattr(row, "request_" + state) for state in _STATES}},
        **{name: _utc(getattr(row, name)) if getattr(row, name) else None for name in _TIMES},
    )


def _item_view(item: ChatBatchItem) -> BatchItemView:
    frozen = _unseal(item.result_ciphertext) if item.result_ciphertext else {}
    return BatchItemView(item.custom_id, item.ordinal, item.operation, item.run_id, item.state,
                         frozen.get("response"), frozen.get("error"),
                         "none" if item.settlement_status == "pending" else item.settlement_status)


def _key_hash(key: str | None):
    if key is None:
        return None
    if not isinstance(key, str) or not 1 <= len(key) <= 128 or any(not 32 <= ord(c) <= 126 for c in key):
        raise BatchInputError("invalid_idempotency_key")
    return _hash(key)


def _scopes(operation: str, contract: str, body: dict) -> tuple[str, ...]:
    # The route-admitted Batch write authority is frozen with each item so a
    # worker re-checks it before any new provider I/O.
    batch = "native:batches:write" if contract == "native" else "compat:batches:write"
    if operation in {"chat.completions", "responses"}:
        return completion_scopes(body, base=(batch, "compat:completions:write"))
    if contract == "openai":
        return (batch, "compat:images:write")
    scope = "native:images:write" if operation.startswith("images.") else "native:audio:write"
    return (batch, scope, *(("native:assets:read",) if body.get("source_asset_id") or body.get("input_asset_id") else ()))


def _check_scopes(scopes, required):
    if scopes is not None and not set(required).issubset(scopes):
        raise BatchInputError("missing_scope")


async def _owned(session, *, project_id, user_id, batch_id, contract=None, lock=False):
    query = select(ChatBatch).where(ChatBatch.id == batch_id, ChatBatch.project_id == project_id,
                                    ChatBatch.user_id == user_id)
    if contract is not None:
        query = query.where(ChatBatch.contract == contract)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    row = (await session.execute(query)).scalar_one_or_none()
    if row is None:
        raise BatchNotFound()
    return row


async def _create(*, project_id, user_id, api_key_id, contract, key_hash, request, input_file_id=None):
    fingerprint = _hash(_json(request.model_dump(mode="json")))

    async def transaction():
        async with _factory()() as session, session.begin():
            await use_read_committed(session)
            # A project row is durable even after all its batches finish.
            await session.execute(insert(ChatBatchProjectQueue).values(project_id=project_id)
                                  .on_duplicate_key_update(project_id=project_id))
            if key_hash is not None:
                existing = (await session.execute(select(ChatBatch).where(
                    ChatBatch.project_id == project_id, ChatBatch.user_id == user_id,
                    ChatBatch.contract == contract, ChatBatch.idempotency_key_hash == key_hash,
                ).with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
                if existing is not None:
                    if existing.request_fingerprint != fingerprint:
                        raise BatchConflict("idempotency_key_reused")
                    return _view(existing), False
            if input_file_id:
                file = await session.get(ChatBatchFile, input_file_id, with_for_update=True, populate_existing=True)
                if (file is None or (file.project_id, file.user_id) != (project_id, user_id)
                        or file.purpose != "batch" or file.state != "processed" or _utc(file.expires_at) <= _now()):
                    raise BatchInputError("input_file_unavailable")
            now = _now()
            row = ChatBatch(id=str(uuid4()), project_id=project_id, user_id=user_id, api_key_id=api_key_id,
                            contract=contract, endpoint=getattr(request, "endpoint", None),
                            metadata_json=request.metadata, idempotency_key_hash=key_hash,
                            request_fingerprint=fingerprint, input_file_id=input_file_id,
                            output_expires_after_seconds=(request.output_expires_after.seconds
                                if contract == "openai" and request.output_expires_after else None),
                            created_at=now, expires_at=now + timedelta(hours=24),
                            request_total=len(request.items) if contract == "native" else 0,
                            request_pending=len(request.items) if contract == "native" else 0)
            session.add(row)
            await session.flush()
            if contract == "native":
                for ordinal, item in enumerate(request.items, 1):
                    session.add(ChatBatchItem(batch_id=row.id, ordinal=ordinal, custom_id=item.custom_id,
                        custom_id_hash=_hash(item.custom_id), operation=item.operation,
                        request_ciphertext=_seal(item.body)))
            await session.flush()
            return _view(row), True

    try:
        return await retry_deadlocks(transaction)
    except IntegrityError:
        # The unique owner/key index is the final arbiter of concurrent creates.
        if key_hash is None:
            raise
        async with _factory()() as session:
            row = (await session.execute(select(ChatBatch).where(
                ChatBatch.project_id == project_id, ChatBatch.user_id == user_id,
                ChatBatch.contract == contract, ChatBatch.idempotency_key_hash == key_hash,
            ))).scalar_one_or_none()
            if row is None:
                raise
            if row.request_fingerprint != fingerprint:
                raise BatchConflict("idempotency_key_reused") from None
            return _view(row), False


def _public_service(function):
    @wraps(function)
    async def available(*args, **kwargs):
        try:
            return await function(*args, **kwargs)
        except (OperationalError, PoolTimeout) as exc:
            mark_db_unhealthy(exc)
            raise BatchUnavailable() from exc
    return available


@_public_service
async def create_native_batch(*, project_id, user_id, api_key_id, scopes, source,
                              idempotency_key: str, request: NativeBatchCreateRequest) -> tuple[BatchView, bool]:
    _factory()
    settings = get_settings()
    if len(request.items) > settings.batch_native_max_items:
        raise BatchInputError("too_many_items")
    if len(_json(request.model_dump(mode="json")).encode("utf-8")) > settings.batch_native_max_bytes:
        raise BatchInputError("request_too_large")
    for item in request.items:
        _check_scopes(scopes, _scopes(item.operation, "native", item.body))
    if idempotency_key is None:
        raise BatchInputError("invalid_idempotency_key")
    return await _create(project_id=project_id, user_id=user_id, api_key_id=api_key_id, contract="native",
                         key_hash=_key_hash(idempotency_key), request=request)


@_public_service
async def create_openai_batch(*, project_id, user_id, api_key_id, scopes, source,
                              request: OpenAIBatchCreateRequest, idempotency_key: str | None) -> BatchView:
    _factory()
    _check_scopes(scopes, _scopes(BATCH_ENDPOINTS[request.endpoint], "openai", {}))
    view, _ = await _create(project_id=project_id, user_id=user_id, api_key_id=api_key_id, contract="openai",
                            key_hash=_key_hash(idempotency_key), request=request,
                            input_file_id=str(UUID(request.input_file_id.removeprefix("file-"))))
    return view


@_public_service
async def get_batch(*, project_id, user_id, batch_id: str, contract) -> BatchView:
    async with _factory()() as session:
        return _view(await _owned(session, project_id=project_id, user_id=user_id, batch_id=batch_id, contract=contract))


@_public_service
async def list_batches(*, project_id, user_id, contract, after: str | None, limit: int) -> tuple[list[BatchView], bool]:
    async with _factory()() as session:
        query = select(ChatBatch).where(ChatBatch.project_id == project_id, ChatBatch.user_id == user_id,
                                        ChatBatch.contract == contract)
        if after:
            cursor = await _owned(session, project_id=project_id, user_id=user_id, batch_id=after, contract=contract)
            query = query.where(or_(ChatBatch.created_at < cursor.created_at,
                and_(ChatBatch.created_at == cursor.created_at, ChatBatch.id < cursor.id)))
        rows = (await session.execute(query.order_by(ChatBatch.created_at.desc(), ChatBatch.id.desc())
                                      .limit(limit + 1))).scalars().all()
        return [_view(row) for row in rows[:limit]], len(rows) > limit


@_public_service
async def list_batch_items(*, project_id, user_id, batch_id: str, after: int | None, limit: int) -> tuple[list[BatchItemView], int | None]:
    async with _factory()() as session:
        await _owned(session, project_id=project_id, user_id=user_id, batch_id=batch_id, contract="native")
        rows = (await session.execute(select(ChatBatchItem).where(ChatBatchItem.batch_id == batch_id,
            ChatBatchItem.ordinal > (after or 0)).order_by(ChatBatchItem.ordinal).limit(limit + 1))).scalars().all()
        return [_item_view(row) for row in rows[:limit]], rows[limit - 1].ordinal if len(rows) > limit else None


@_public_service
async def owned_cancel_scopes(*, project_id, user_id, batch_id: str, contract) -> tuple[str, ...]:
    """Every owned item needs its matching primary generation action, including prevalidation."""
    async with _factory()() as session:
        row = await _owned(session, project_id=project_id, user_id=user_id, batch_id=batch_id, contract=contract)
        if contract == "openai":
            operation = BATCH_ENDPOINTS.get(row.endpoint)
            if operation is None:
                raise BatchInputError("unsupported_operation")
            operations = {operation}
        else:
            operations = set((await session.execute(
                select(ChatBatchItem.operation).where(ChatBatchItem.batch_id == row.id).distinct()
            )).scalars().all())
        if not operations or not operations.issubset(set(BATCH_ENDPOINTS.values()) | {"images.edits", "audio.speech", "audio.transcriptions"}):
            raise BatchInputError("unsupported_operation")
        return tuple(sorted({scope for operation in operations for scope in _scopes(operation, contract, {})}))


@_public_service
async def cancel_batch(*, project_id, user_id, batch_id: str, contract) -> BatchView:
    async def transaction():
        async with _factory()() as session, session.begin():
            await use_read_committed(session)
            row = await _owned(session, project_id=project_id, user_id=user_id, batch_id=batch_id,
                               contract=contract, lock=True)
            if row.status == "cancelling" or row.status == "cancelled":
                return _view(row)
            if row.status not in {"validating", "in_progress"}:
                raise BatchConflict("invalid_batch_state")
            row.status, row.cancelling_at, row.final_target_status = "cancelling", _now(), "cancelled"
            return _view(row)
    return await retry_deadlocks(transaction)


@dataclass(frozen=True)
class _Lease:
    batch: ChatBatch
    owner: str
    fence: int
    queue_fence: int


async def _claim(owner: str) -> _Lease | None:
    global _last_project

    async def transaction():
        async with _factory()() as session, session.begin():
            await use_read_committed(session)
            now = _now()
            # One candidate per tick. Rotating projects prevents a permanently busy
            # project from starving others; the durable queue rotates its batches.
            query = select(ChatBatchProjectQueue).where(
                or_(ChatBatchProjectQueue.lease_expires_at.is_(None), ChatBatchProjectQueue.lease_expires_at <= now),
                select(ChatBatch.id).where(ChatBatch.project_id == ChatBatchProjectQueue.project_id,
                    ChatBatch.status.in_(_ACTIVE_BATCHES)).exists())
            rotated = query.where(ChatBatchProjectQueue.project_id > (_last_project or ""))
            queue = (await session.execute(rotated.order_by(ChatBatchProjectQueue.project_id).limit(1)
                .with_for_update(skip_locked=True).execution_options(populate_existing=True))).scalar_one_or_none()
            if queue is None:
                queue = (await session.execute(query.order_by(ChatBatchProjectQueue.project_id).limit(1)
                    .with_for_update(skip_locked=True).execution_options(populate_existing=True))).scalar_one_or_none()
            if queue is None:
                return None
            query = select(ChatBatch).where(ChatBatch.project_id == queue.project_id,
                ChatBatch.status.in_(_ACTIVE_BATCHES),
                or_(ChatBatch.lease_expires_at.is_(None), ChatBatch.lease_expires_at <= now))
            batch = (await session.execute(query.where(ChatBatch.id > (queue.last_batch_id or ""))
                .order_by(ChatBatch.id).limit(1).with_for_update(skip_locked=True)
                .execution_options(populate_existing=True))).scalar_one_or_none()
            if batch is None:
                batch = (await session.execute(query.order_by(ChatBatch.id).limit(1).with_for_update(skip_locked=True)
                    .execution_options(populate_existing=True))).scalar_one_or_none()
            if batch is None:
                return None
            queue.last_batch_id = batch.id
            for row in (queue, batch):
                row.lease_owner, row.lease_expires_at = owner, now + timedelta(seconds=30)
                row.lease_fence += 1
            await session.flush()
            return _Lease(batch, owner, batch.lease_fence, queue.lease_fence)

    lease = await retry_deadlocks(transaction)
    if lease:
        _last_project = lease.batch.project_id
    return lease


async def _locked(session, lease: _Lease):
    queue = await session.get(ChatBatchProjectQueue, lease.batch.project_id, with_for_update=True, populate_existing=True)
    batch = await session.get(ChatBatch, lease.batch.id, with_for_update=True, populate_existing=True)
    now = _now()
    if (queue is None or batch is None or queue.lease_owner != lease.owner or batch.lease_owner != lease.owner
            or queue.lease_fence != lease.queue_fence or batch.lease_fence != lease.fence
            or _utc(queue.lease_expires_at) <= now or _utc(batch.lease_expires_at) <= now):
        raise BatchConflict("coordinator_lease_lost")
    return queue, batch


@asynccontextmanager
async def _keep_lease(lease):
    stopped = asyncio.Event()

    async def renew():
        while True:
            try:
                await asyncio.wait_for(stopped.wait(), timeout=10)
                return
            except TimeoutError:
                async def transaction():
                    async with _factory()() as session, session.begin():
                        await use_read_committed(session)
                        queue, batch = await _locked(session, lease)
                        queue.lease_expires_at = batch.lease_expires_at = _now() + timedelta(seconds=30)
                await retry_deadlocks(transaction)

    task = asyncio.create_task(renew())
    try:
        yield
        if task.done():
            task.result()
    finally:
        stopped.set()
        try:
            await task
        finally:
            async def release():
                async with _factory()() as session, session.begin():
                    await use_read_committed(session)
                    for model, key, fence in ((ChatBatchProjectQueue, lease.batch.project_id, lease.queue_fence),
                                               (ChatBatch, lease.batch.id, lease.fence)):
                        row = await session.get(model, key, with_for_update=True, populate_existing=True)
                        if row and row.lease_owner == lease.owner and row.lease_fence == fence:
                            row.lease_owner = row.lease_expires_at = None
            await retry_deadlocks(release)


async def _authority(batch) -> frozenset[str]:
    # One directory lookup per validation chunk, never per row: a 50,000-row file
    # must not become 50,000 Keystone authority resolutions. Execution revalidates.
    try:
        async with _factory()() as session:
            return await current_api_key_scopes(session, api_key_id=batch.api_key_id,
                                                user_id=batch.user_id, project_id=batch.project_id)
    except ApiKeyForbidden:
        return frozenset()
    except ApiKeyAuthorityUnavailable as exc:
        raise BatchUnavailable("inference_authority_unavailable") from exc


async def _prepare(batch, operation, body, allowed: frozenset[str]):
    body = validate_batch_body(operation, body, contract=batch.contract)
    required = _scopes(operation, batch.contract, body)
    if not allowed.issuperset(required):
        raise BatchInputError("api_key_unauthorized")
    kwargs = dict(project_id=batch.project_id, user_id=batch.user_id,
                  source="api" if batch.api_key_id is not None else "web",
                  api_key_id=batch.api_key_id, required_scopes=required)
    if operation in {"chat.completions", "responses"}:
        return await api_completion.prepare_api_completion_run(body, operation=operation, **kwargs)
    if operation.startswith("images."):
        return await images.prepare_image_run(body, **kwargs)
    return await audio.prepare_audio_run(body, operation=operation, **kwargs)


def _prepared(batch, item):
    args = dict(payload=_unseal(item.request_ciphertext), capability_snapshot=item.capability_snapshot,
                pricing_snapshot=item.pricing_snapshot, required_scopes=tuple(item.required_scopes),
                project_id=batch.project_id, user_id=batch.user_id)
    if item.operation in {"chat.completions", "responses"}:
        return api_completion.PreparedApiCompletion(**args), api_completion.persist_api_completion_run_in_transaction
    args["source_asset_id"] = item.input_asset_id
    if item.operation.startswith("images."):
        return images.PreparedImageRun(**args), images.persist_image_run_in_transaction
    return audio.PreparedAudioRun(**args), audio.persist_audio_run_in_transaction


def _input_fault(exc):
    # Never persist exception text: it can contain prompts, provider URLs or credentials.
    return exc.code if isinstance(exc, BatchError) else "invalid_batch_input"


async def _validate(lease):
    from lumen.services import batch_files

    batch, settings = lease.batch, get_settings()
    max_rows = min(100, settings.batch_validation_chunk_rows)
    max_bytes = min(1024 * 1024, settings.batch_validation_chunk_bytes)
    records, errors = [], []
    next_byte = batch.validation_byte_cursor
    ordinal = batch.validation_ordinal_cursor
    eof = False
    if batch.contract == "native":
        async with _factory()() as session:
            sizes = (await session.execute(select(ChatBatchItem.ordinal, func.octet_length(ChatBatchItem.request_ciphertext))
                .where(ChatBatchItem.batch_id == batch.id, ChatBatchItem.ordinal > ordinal)
                .order_by(ChatBatchItem.ordinal).limit(max_rows))).all()
            chosen, used = [], 0
            for number, size in sizes:
                if chosen and used + size > max_bytes:
                    break
                chosen.append(number)
                used += size
            rows = (await session.execute(select(ChatBatchItem).where(ChatBatchItem.batch_id == batch.id,
                ChatBatchItem.ordinal.in_(chosen)).order_by(ChatBatchItem.ordinal))).scalars().all()
        for item in rows:
            records.append((item.ordinal, item.custom_id, item.operation, _unseal(item.request_ciphertext)))
        eof = not records or records[-1][0] == batch.request_total
    else:
        try:
            chunk = await batch_files.read_input_chunk(file_id=batch.input_file_id, start_byte=next_byte,
                max_rows=max_rows, max_bytes=max_bytes, max_line_bytes=settings.batch_jsonl_max_line_bytes)
            next_byte, eof = chunk.next_byte, chunk.eof
            for line in chunk.lines:
                ordinal += 1
                try:
                    if ordinal > settings.batch_jsonl_max_rows:
                        raise BatchInputError("too_many_items")
                    parsed = OpenAIBatchRow.model_validate_json(line.raw)
                    if parsed.url != batch.endpoint:
                        raise BatchInputError("endpoint_mismatch")
                    records.append((ordinal, parsed.custom_id, BATCH_ENDPOINTS[parsed.url], parsed.body))
                except (ValueError, BatchInputError) as exc:
                    errors.append({"code": _input_fault(exc), "message": "Invalid batch input", "line": ordinal})
        except BatchInputError as exc:
            errors.append({"code": exc.code, "message": "Invalid batch input", "line": ordinal + 1})
    prepared = []
    allowed = await _authority(batch) if records else frozenset()
    for number, custom_id, operation, body in records:
        try:
            value = await _prepare(batch, operation, body, allowed)
            prepared.append((number, custom_id, operation, value))
        except (ValueError, DurableRunInputError, BatchInputError, QuotaExceeded) as exc:
            errors.append({"code": _input_fault(exc), "message": "Invalid batch input",
                           **({"line": number} if batch.contract == "openai" else {"custom_id": custom_id})})

    async def transaction():
        async with _factory()() as session, session.begin():
            await use_read_committed(session)
            _, row = await _locked(session, lease)
            if row.status != "validating" or _utc(row.expires_at) <= _now():
                return
            local_errors = list(errors)
            for number, custom_id, operation, value in prepared:
                if row.contract == "native":
                    item = await session.get(ChatBatchItem, (row.id, number), with_for_update=True, populate_existing=True)
                else:
                    duplicate = (await session.execute(select(ChatBatchItem.ordinal).where(
                        ChatBatchItem.batch_id == row.id, ChatBatchItem.custom_id_hash == _hash(custom_id)))).first()
                    if duplicate:
                        local_errors.append({"code": "duplicate_custom_id", "message": "Duplicate custom_id", "line": number})
                        continue
                    item = ChatBatchItem(batch_id=row.id, ordinal=number, custom_id=custom_id,
                                         custom_id_hash=_hash(custom_id), operation=operation)
                    session.add(item)
                source_asset_id = getattr(value, "source_asset_id", None)
                if source_asset_id:
                    asset = await session.get(ChatAsset, source_asset_id, with_for_update=True, populate_existing=True)
                    if (asset is None or asset.status != "clean" or (asset.project_id, asset.user_id) != (row.project_id, row.user_id)
                            or asset.expires_at is not None and _utc(asset.expires_at) <= _now()):
                        local_errors.append({"code": "input_asset_unavailable", "message": "Input asset unavailable",
                                             "custom_id": custom_id})
                        continue
                item.request_ciphertext = _seal(value.payload)
                item.capability_snapshot = value.capability_snapshot
                item.pricing_snapshot = value.pricing_snapshot
                item.required_scopes = list(value.required_scopes)
                item.input_asset_id = source_asset_id
                await session.flush()
            if row.contract == "openai":
                row.request_total = row.request_pending = ordinal
            row.validation_byte_cursor = next_byte
            row.validation_ordinal_cursor = max((r[0] for r in records), default=ordinal)
            if local_errors or (eof and row.request_total == 0):
                row.validation_errors = (list(row.validation_errors) + local_errors or
                    [{"code": "empty_input", "message": "Batch input is empty"}])[:100]
                row.status, row.failed_at = "failed", _now()
            elif eof:
                row.status, row.in_progress_at = "in_progress", _now()
    await retry_deadlocks(transaction)
    return True


def _freeze(item, state, *, response=None, envelope=None, code=None):
    item.state = state
    item.error_code = code
    item.error_message = code.replace("_", " ") if code else None
    error = {"code": code, "message": item.error_message} if code else None
    item.result_ciphertext = _seal({"response": response, "envelope": envelope, "error": error})
    if envelope:
        item.http_status, item.request_id = envelope["status_code"], envelope["request_id"]


async def _counts(session, batch):
    groups = (await session.execute(select(ChatBatchItem.state, func.count()).where(
        ChatBatchItem.batch_id == batch.id).group_by(ChatBatchItem.state))).all()
    counts = dict(groups)
    for state in _STATES:
        setattr(batch, "request_" + state, counts.get(state, 0))
    if batch.contract == "openai":
        batch.request_failed += sum(counts.get(state, 0) for state in ("cancelled", "expired", "unknown"))
    return sum(counts.get(state, 0) for state in ("pending", "queued", "running"))


async def _native_media_response(session, run, segment):
    payload = load_segment_payload(segment.result_payload) if segment and segment.status == "completed" else None
    if not isinstance(payload, dict):
        raise BatchUnavailable("result_checkpoint_unavailable")
    if run.run_kind == "stt":
        return {key: payload[key] for key in ("text", "segments") if key in payload}
    ids = payload.get("asset_ids", []) if run.run_kind == "image" else [payload.get("asset_id")]
    rows = (await session.execute(select(ChatAsset).where(ChatAsset.id.in_(ids),
        ChatAsset.project_id == run.project_id, ChatAsset.user_id == run.user_id))).scalars().all()
    lookup = {row.id: {"asset_id": row.id, "mime_type": row.mime_type, "size_bytes": row.size_bytes} for row in rows}
    if any(asset_id not in lookup for asset_id in ids):
        raise BatchUnavailable("result_checkpoint_unavailable")
    return {"data": [lookup[asset_id] for asset_id in ids]} if run.run_kind == "image" else lookup[ids[0]]


async def _project(lease):
    # Compatible images require S3 reads to freeze their b64 response, outside locks.
    compat_images = {}
    if lease.batch.contract == "openai":
        async with _factory()() as session:
            ids = (await session.execute(select(ChatBatchItem.run_id).join(ChatRun, ChatRun.id == ChatBatchItem.run_id)
                .where(ChatBatchItem.batch_id == lease.batch.id, ChatBatchItem.state.in_(("queued", "running")),
                       ChatRun.status == "completed", ChatRun.run_kind == "image")
                .order_by(ChatBatchItem.ordinal).limit(1))).scalars().all()
        for run_id in ids:
            compat_images[run_id] = await images.image_result(run_id=run_id, project_id=lease.batch.project_id,
                                                             user_id=lease.batch.user_id)

    async def transaction():
        changed, cancel_ids = False, []
        async with _factory()() as session, session.begin():
            await use_read_committed(session)
            _, batch = await _locked(session, lease)
            now = _now()
            if batch.status in {"validating", "in_progress"} and _utc(batch.expires_at) <= now:
                batch.status, batch.cancelling_at, batch.final_target_status = "cancelling", now, "expired"
                changed = True
            stopping = batch.status == "cancelling"
            grace_over = stopping and (_utc(batch.cancelling_at) + timedelta(
                seconds=get_settings().batch_cancel_grace_seconds) <= now)
            if stopping:
                # Never-materialized items carry no run, hold or result: freeze them in bounded set-based
                # pages so a 50,000-row cancel/expiry finishes inside the grace window.
                last = (await session.execute(select(ChatBatchItem.ordinal).where(
                    ChatBatchItem.batch_id == batch.id, ChatBatchItem.run_id.is_(None),
                    ChatBatchItem.state.in_(("pending", "queued", "running")))
                    .order_by(ChatBatchItem.ordinal).offset(_STOP_FREEZE_PAGE - 1).limit(1))).scalar_one_or_none()
                target = batch.final_target_status
                code = "batch_expired" if target == "expired" else "batch_cancelled"
                frozen = await session.execute(update(ChatBatchItem).where(
                    ChatBatchItem.batch_id == batch.id, ChatBatchItem.run_id.is_(None),
                    ChatBatchItem.state.in_(("pending", "queued", "running")),
                    *(() if last is None else (ChatBatchItem.ordinal <= last,)),
                ).values(state=target, error_code=code, error_message=code.replace("_", " "),
                         result_ciphertext=_seal({"response": None, "envelope": None,
                                                  "error": {"code": code, "message": code.replace("_", " ")}}))
                    .execution_options(synchronize_session=False))
                changed |= frozen.rowcount > 0
            query = select(ChatBatchItem).where(ChatBatchItem.batch_id == batch.id,
                ChatBatchItem.state.in_(("pending", "queued", "running")))
            eligible_runs = select(ChatRun.id).where(ChatRun.batch_id == batch.id, or_(
                ChatRun.status.in_(("completed", "failed", "canceled")),
                and_(ChatRun.status != "queued", ChatBatchItem.state == "queued"),
            )).correlate(ChatBatchItem)
            if not stopping:
                query = query.where(ChatBatchItem.run_id.in_(eligible_runs))
            elif not grace_over:
                checkpointed = select(ChatRunSegment.run_id).where(
                    ChatRunSegment.run_id == ChatRun.id, ChatRunSegment.status == "completed").exists()
                cancellable = select(ChatRun.id).where(ChatRun.batch_id == batch.id, or_(
                    ChatRun.status.in_(("completed", "failed", "canceled")),
                    and_(ChatRun.status == "queued", ~checkpointed)))
                query = query.where(or_(ChatBatchItem.run_id.is_(None), ChatBatchItem.run_id.in_(cancellable)))
            query = query.order_by(ChatBatchItem.ordinal).limit(100)
            items = (await session.execute(query.with_for_update().execution_options(populate_existing=True))).scalars().all()
            for item in items:
                if item.run_id is None:
                    if stopping:
                        state = batch.final_target_status
                        _freeze(item, state, code="batch_expired" if state == "expired" else "batch_cancelled")
                        changed = True
                    continue
                run = await session.get(ChatRun, item.run_id, with_for_update=True, populate_existing=True)
                segment = await session.get(ChatRunSegment, (run.id, f"{run.run_kind}:1"), populate_existing=True)
                hold = await session.get(ChatModelCallReservation, (run.id, f"{run.run_kind}:1"), populate_existing=True)
                item.settlement_status = hold.status if hold else "none"
                if run.status in {"completed", "failed", "canceled"}:
                    state = {"completed": "completed", "failed": "failed", "canceled": "cancelled"}[run.status]
                    code, response, envelope = None, None, None
                    if state != "completed":
                        event_row = (await session.execute(select(ChatRunEventRow).where(
                            ChatRunEventRow.run_id == run.id, ChatRunEventRow.event_type.in_(("run.failed", "run.canceled")))
                            .order_by(ChatRunEventRow.seq.desc()).limit(1))).scalar_one_or_none()
                        event = _unseal(event_row.payload) if event_row else {}
                        code = event.get("error_code", "run_failed" if state == "failed" else "batch_cancelled")
                        if not isinstance(code, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,99}", code):
                            code = "run_failed"
                        if code == "provider_result_unknown" or hold and hold.status == "unknown":
                            state, code = "unknown", "provider_result_unknown"
                    if run.run_kind == "api_completion":
                        envelope = await api_completion.load_api_completion_envelope(session, run.id)
                        response = envelope["body"] if envelope else None
                        if state == "completed" and envelope is None:
                            raise BatchUnavailable("result_checkpoint_unavailable")
                    elif state == "completed":
                        response = await _native_media_response(session, run, segment)
                        if batch.contract == "openai":
                            if run.id not in compat_images:
                                continue
                            envelope = {"status_code": 200, "request_id": run.id, "body": compat_images[run.id]}
                    if hold and hold.status == "unknown":
                        state, code = "unknown", "provider_result_unknown"
                    elif (stopping and batch.final_target_status == "expired" and run.status == "canceled"
                          and run.provider_started_at is None):
                        state, code = "expired", "batch_expired"
                    _freeze(item, state, response=response, envelope=envelope, code=code)
                    changed = True
                elif stopping and run.status == "queued" and (segment is None or segment.status != "completed"):
                    cancel_ids.append(run.id)
                elif stopping and grace_over:
                    _freeze(item, "unknown", code="provider_result_unknown")
                    changed = True
                else:
                    state = "queued" if run.status == "queued" else "running"
                    changed |= item.state != state
                    item.state = state
            await session.flush()
            remaining = await _counts(session, batch)
            if remaining == 0 and batch.status in {"in_progress", "cancelling"}:
                batch.status, batch.finalizing_at = "finalizing", now
                batch.final_target_status = batch.final_target_status or "completed"
                batch.finalization_epoch += 1
                changed = True
            return changed, cancel_ids

    changed, cancel_ids = await retry_deadlocks(transaction)
    for run_id in cancel_ids:
        # Intent is already committed and gates claims. Do not cancel checkpointed
        # queued runs: recovery must settle their completed provider boundary.
        await lifecycle.request_cancelled(run_id=run_id, project_id=lease.batch.project_id,
                                          user_id=lease.batch.user_id)
        changed = True
    return changed


async def _dispatch_slots(session, batch) -> int:
    settings = get_settings()
    project_active = (await session.execute(select(func.count()).select_from(ChatBatchItem)
        .join(ChatBatch, ChatBatch.id == ChatBatchItem.batch_id).where(ChatBatch.project_id == batch.project_id,
            ChatBatchItem.state.in_(("queued", "running"))))).scalar_one()
    batch_active = (await session.execute(select(func.count()).select_from(ChatBatchItem).where(
        ChatBatchItem.batch_id == batch.id, ChatBatchItem.state.in_(("queued", "running"))))).scalar_one()
    return min(settings.batch_dispatch_window - batch_active,
               settings.batch_project_dispatch_window - project_active, 100)


async def _materialize(lease):
    # Directory I/O never runs under the project/batch/item locks of the retried
    # transaction: resolve current authority first, only when there is work to do.
    if lease.batch.status != "in_progress":
        return False
    async with _factory()() as session:
        pending = await session.scalar(select(ChatBatchItem.ordinal).where(
            ChatBatchItem.batch_id == lease.batch.id, ChatBatchItem.state == "pending").limit(1))
        if pending is None or await _dispatch_slots(session, lease.batch) <= 0:
            return False
    allowed = await _authority(lease.batch)

    async def transaction():
        wake_ids = []
        async with _factory()() as session, session.begin():
            await use_read_committed(session)
            _, batch = await _locked(session, lease)
            if batch.status != "in_progress" or _utc(batch.expires_at) <= _now():
                return wake_ids, False
            available = await _dispatch_slots(session, batch)
            if available <= 0:
                return wake_ids, False
            items = (await session.execute(select(ChatBatchItem).where(ChatBatchItem.batch_id == batch.id,
                ChatBatchItem.state == "pending").order_by(ChatBatchItem.ordinal).limit(available)
                .with_for_update().execution_options(populate_existing=True))).scalars().all()
            for item in items:
                prepared, persist = _prepared(batch, item)
                try:
                    async with session.begin_nested():
                        run = await persist(session, prepared, project_id=batch.project_id, user_id=batch.user_id,
                            client_request_id=str(uuid5(UUID(batch.id), item.custom_id)),
                            source="api" if batch.api_key_id is not None else "web", api_key_id=batch.api_key_id,
                            workload_class="batch", batch_id=batch.id, allowed_scopes=allowed)
                        # No ORM relationship orders this foreign key for us.
                        await session.flush()
                        item.run_id, item.state = run.id, "queued"
                        await session.flush()
                        wake_ids.append(run.id)
                except (DurableRunInputError, DurableRunConflict, ApiKeyForbidden) as exc:
                    code = ("api_key_unauthorized" if isinstance(exc, (ApiKeyForbidden, MediaAuthorizationRevoked))
                            or str(exc) == "api_key_unauthorized" else
                            "provider_configuration_changed" if isinstance(exc, DurableRunConflict) else "invalid_batch_input")
                    _freeze(item, "failed", code=code)
            await session.flush()
            await _counts(session, batch)
        return wake_ids, bool(items)
    ids, changed = await retry_deadlocks(transaction)
    for run_id in ids:
        await wake_run(run_id)
    return changed


async def _result_lines(batch_id, kind):
    ordinal = 0
    while True:
        async with _factory()() as session:
            sizes = (await session.execute(select(ChatBatchItem.ordinal, func.octet_length(ChatBatchItem.result_ciphertext))
                .where(ChatBatchItem.batch_id == batch_id, ChatBatchItem.ordinal > ordinal)
                .order_by(ChatBatchItem.ordinal).limit(100))).all()
            chosen, used = [], 0
            for number, size in sizes:
                if chosen and used + size > 1024 * 1024:
                    break
                chosen.append(number)
                used += size
            if not chosen:
                return
            items = (await session.execute(select(ChatBatchItem).where(ChatBatchItem.batch_id == batch_id,
                ChatBatchItem.ordinal.in_(chosen)).order_by(ChatBatchItem.ordinal))).scalars().all()
        if not items:
            return
        for item in items:
            ordinal = item.ordinal
            frozen = _unseal(item.result_ciphertext)
            envelope, error = frozen.get("envelope"), frozen.get("error")
            failed = item.state != "completed" or envelope is not None and envelope["status_code"] >= 400
            if failed != (kind == "error"):
                continue
            row = {"id": "batch_req_" + uuid5(UUID(batch_id), item.custom_id).hex,
                   "custom_id": item.custom_id, "response": envelope, "error": error if not envelope else None}
            yield (_json(row) + "\n").encode("utf-8")


async def _finalize(lease):
    from lumen.services import batch_files

    async with _factory()() as session:
        batch = await session.get(ChatBatch, lease.batch.id)
    if batch.status != "finalizing":
        return False
    stored = {}
    storage_failed = False
    if batch.contract == "openai":
        try:
            for kind in ("output", "error"):
                result = await batch_files.write_result_file(project_id=batch.project_id, batch_id=batch.id,
                    epoch=batch.finalization_epoch, kind=kind, lines=_result_lines(batch.id, kind))
                if result:
                    if result.file_id != batch_files.result_file_id(batch.id, batch.finalization_epoch, kind):
                        raise BatchUnavailable("result_storage_unavailable")
                    stored[kind] = result
        except BatchUnavailable:
            storage_failed = True

    async def publish():
        async with _factory()() as session, session.begin():
            await use_read_committed(session)
            _, row = await _locked(session, lease)
            if row.status != "finalizing" or row.finalization_epoch != batch.finalization_epoch:
                raise BatchConflict("coordinator_lease_lost")
            if storage_failed:
                row.validation_errors = [{"code": "result_storage_unavailable", "message": "Result storage unavailable"}]
                row.status, row.failed_at = "failed", _now()
                return
            # created_at is the output file's creation, not the batch admission:
            # a long-running batch must not publish an already-expired result.
            expires = _now() + timedelta(seconds=row.output_expires_after_seconds
                if row.output_expires_after_seconds else get_settings().batch_result_ttl_days * 86400)
            for kind, result in stored.items():
                session.add(batch_files.result_file_row(stored=result, project_id=row.project_id,
                    user_id=row.user_id, api_key_id=row.api_key_id, filename=f"{row.id}-{kind}.jsonl", expires_at=expires))
            await session.flush()
            row.output_file_id = stored["output"].file_id if "output" in stored else None
            row.error_file_id = stored["error"].file_id if "error" in stored else None
            row.status = row.final_target_status or "completed"
            setattr(row, row.status + "_at", _now())
    await retry_deadlocks(publish)
    return True


async def coordinate_once(*, owner: str) -> bool:
    if _step_lock.locked():
        return False
    async with _step_lock, auxiliary.step(owner=owner) as admitted:
        if not admitted:
            return False
        lease = await _claim(owner)
        if lease is None:
            return False
        async with _keep_lease(lease):
            if lease.batch.status == "validating" and _utc(lease.batch.expires_at) > _now():
                return await _validate(lease)
            if lease.batch.status == "finalizing":
                return await _finalize(lease)
            changed = await _project(lease)
            changed |= await _materialize(lease)
            changed |= await _finalize(lease)
            return changed
