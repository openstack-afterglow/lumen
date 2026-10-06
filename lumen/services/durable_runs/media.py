"""Finite media source pins and the Batch-only pre-I/O authorization fence.

Online runs keep their accepted-run authority: only the existing lease, cancel and
wallet/key reservation checks apply before provider I/O. Batch runs additionally
re-authorize the frozen scopes, provider configuration and pinned source under a
batch→run lock order before any source read or provider request.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from lumen.models.chat_assets import ChatAsset, ChatRunAsset
from lumen.models.chat_batches import ChatBatch, ChatBatchItem
from lumen.models.chat_runs import ChatRun
from lumen.services.api_key_store import ApiKeyForbidden, authorize_api_key_in_transaction
from lumen.services.providers import audio_transport, image_transport
from lumen.services.worker_routing import lock_open_batch

from . import budgets
from .common import _factory, _payload
from .errors import DurableRunInputError
from .lifecycle import _require_owned_running_lease

_IMAGE_MIMES = frozenset({"image/png", "image/jpeg", "image/webp"})
# Terminal item states no longer protect a source asset from deletion.
_ACTIVE_ITEM_STATES = ("pending", "queued", "running")
# Why no new provider I/O may begin; recorded as the canceled run's error_code.
IO_BLOCKED = frozenset({"canceled", "batch_expired", "batch_inactive"})


class MediaAuthorizationRevoked(DurableRunInputError):
    """The accepted credential no longer authorizes this Batch run; raised before any I/O."""


async def authorize_media_in_transaction(
    session: AsyncSession, *, user_id: str, project_id: str,
    api_key_id: int | None, required_scopes: tuple[str, ...],
) -> None:
    """Map the shared credential fence into finite media's safe input error contract."""
    try:
        await authorize_api_key_in_transaction(session, user_id=user_id, project_id=project_id,
            api_key_id=api_key_id, required_scopes=required_scopes)
    except ApiKeyForbidden as exc:
        raise MediaAuthorizationRevoked("media credential is no longer authorized") from exc


def _source_matches(asset: ChatAsset, *, run_kind: str, pricing: dict) -> bool:
    if run_kind == "image":
        return asset.mime_type in _IMAGE_MIMES and 0 < asset.size_bytes <= image_transport._MAX_SOURCE_BYTES
    from .audio import _duration

    try:
        duration = _duration({"mime_type": asset.mime_type, "size_bytes": asset.size_bytes,
                              "media_metadata": asset.media_metadata})
    except DurableRunInputError:
        return False
    return (asset.mime_type in audio_transport._INPUT_FORMATS
            and asset.size_bytes <= pricing.get("max_source_bytes", audio_transport._MAX_INPUT_BYTES)
            and duration == pricing["max_duration_ms"])


async def lock_media_source_in_transaction(
    session: AsyncSession, *, asset_id: str, user_id: str, project_id: str, run_kind: str,
    pricing: dict, batch_id: str | None = None, run_id: str | None = None,
) -> ChatAsset:
    """Lock an owned source; a deleting asset is usable only with an accepted pin.

    New reuse requires ``clean``. An active Batch item that already pinned this
    asset (materialization) or the run's own input binding (execution) keeps a
    later-deleted source usable; nothing here clears the deletion intent.
    """
    asset = (await session.execute(select(ChatAsset).where(ChatAsset.id == asset_id,
        ChatAsset.user_id == user_id, ChatAsset.project_id == project_id)
        .with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
    if asset is None or asset.status not in {"clean", "deleting"}:
        raise DurableRunInputError("source media is unavailable")
    if asset.status == "deleting":
        pinned = None
        if run_id is not None:
            pinned = await session.scalar(select(ChatRunAsset.asset_id).where(ChatRunAsset.run_id == run_id,
                ChatRunAsset.asset_id == asset_id, ChatRunAsset.purpose == "input"))
        elif batch_id is not None:
            pinned = await session.scalar(select(ChatBatchItem.input_asset_id).where(
                ChatBatchItem.batch_id == batch_id, ChatBatchItem.input_asset_id == asset_id,
                ChatBatchItem.state.in_(_ACTIVE_ITEM_STATES)).limit(1))
        if pinned is None:
            raise DurableRunInputError("source media is unavailable")
    if not _source_matches(asset, run_kind=run_kind, pricing=pricing):
        raise DurableRunInputError("source media changed")
    return asset


def _blocked_reason(run: ChatRun, batch: ChatBatch | None, *, batch_open: bool) -> str | None:
    if run.cancel_requested_at is not None:
        return "canceled"
    if run.batch_id is None or batch_open:
        return None
    if batch is not None and batch.status in {"cancelling", "cancelled"}:
        return "canceled"
    expires_at = batch.expires_at if batch is not None else None
    if batch is not None and (batch.status == "expired" or (expires_at is not None and (
            expires_at if expires_at.tzinfo else expires_at.replace(tzinfo=UTC)) <= datetime.now(UTC))):
        return "batch_expired"
    return "batch_inactive"


async def lock_media_run_for_io(session: AsyncSession, run_id: str, *, owner: str) -> tuple[ChatRun, str | None]:
    """Lock batch (if any) before run; a non-None reason means no new provider I/O may begin."""
    batch_id = await session.scalar(select(ChatRun.batch_id).where(ChatRun.id == run_id))
    batch_open = await lock_open_batch(session, batch_id) if batch_id is not None else True
    # lock_open_batch loaded the row with populate_existing; this is an identity-map read.
    batch = await session.get(ChatBatch, batch_id) if batch_id is not None else None
    run = (await session.execute(select(ChatRun).where(ChatRun.id == run_id)
        .with_for_update().execution_options(populate_existing=True))).scalar_one()
    _require_owned_running_lease(run, owner)
    if run.batch_id != batch_id:
        raise DurableRunInputError("media batch identity changed")
    return run, _blocked_reason(run, batch, batch_open=batch_open)


async def validate_batch_media_io_in_transaction(session: AsyncSession, run: ChatRun) -> None:
    """Batch-only: re-authorize frozen scopes, route configuration and pinned source."""
    if run.batch_id is None:
        return
    from .admission import _lock_run_configurations

    await _lock_run_configurations(session, run.capability_snapshot, model_name=run.model_name)
    payload = _payload(run)
    scopes = payload.get("required_scopes")
    if not isinstance(scopes, list) or not scopes:
        raise DurableRunInputError("batch media scopes are unavailable")
    await authorize_media_in_transaction(session, user_id=run.user_id, project_id=run.project_id,
        api_key_id=run.api_key_id, required_scopes=tuple(scopes))
    source_id = payload.get("source_asset_id") if run.run_kind == "image" else payload.get("input_asset_id")
    if source_id is not None:
        await lock_media_source_in_transaction(session, asset_id=source_id, user_id=run.user_id,
            project_id=run.project_id, run_kind=run.run_kind, pricing=run.pricing_snapshot, run_id=run.id)


async def media_io_allowed(run_id: str, *, owner: str) -> tuple[str | None, bool]:
    """Fence before source object reads; returns ``(blocked_reason, is_batch)``.

    The provider-start transaction repeats the fence; only Batch runs may read a
    source deleted after its accepted pin.
    """
    async def transaction():
        async with _factory()() as session, session.begin():
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
            run, blocked = await lock_media_run_for_io(session, run_id, owner=owner)
            if blocked is None:
                await validate_batch_media_io_in_transaction(session, run)
            return blocked, run.batch_id is not None
    return await budgets.retry_deadlocks(transaction)


def confirmed_media_rejection(exc: BaseException) -> int | None:
    """Upstream HTTP status of a media refusal proven to precede inference, else None (unknown)."""
    from .api_completion import _REJECTION_STATUSES

    status = getattr(exc, "status_code", None)
    if (isinstance(exc, (image_transport.ImageTransportError, audio_transport.AudioTransportError))
            and type(status) is int and status in _REJECTION_STATUSES):
        return status
    return None
