"""Durable run admission and atomic persistence."""

import json
import uuid
from typing import Any, Literal

from sqlalchemy import select

from lumen.crypto import encrypt_chat_content
from lumen.models.chat_assets import ChatAsset, ChatMessageAsset, ChatRunAsset
from lumen.models.chat_contracts import ChatRunDescriptor, validate_user_input_parts
from lumen.models.chat_db import ChatConversation, ChatMessage
from lumen.models.chat_runs import ChatRun, ChatRunProvider, ChatTempThread
from lumen.services.message_parts import serialize_parts
from lumen.services.message_timestamps import message_timestamps
from lumen.services.providers import routing as ps
from lumen.services.run_store import NONTERMINAL, append_event

from .common import (
    _TEMP_THREAD_RETENTION,
    _event,
    _factory,
    _fingerprint,
    _freeze_v2_lumen_snapshot,
    _freeze_v2_tool_call_limit,
    _now,
    _require_supported_execution_protocol_version,
    descriptor,
    wake_run,
)
from .errors import DurableRunConflict, DurableRunError, DurableRunInputError, DurableRunNotFound


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
        raise DurableRunInputError("Idempotency-Key must be a UUID") from exc
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
    summary = capability_snapshot.get("summary_route")
    if isinstance(summary, dict):
        session.add(
            ChatRunProvider(
                run_id=run.id,
                purpose="summary",
                provider_id=summary["provider_id"],
                model_id=summary["model_id"],
                provider_label=summary["provider_name"],
                model_label=summary["model_name"],
                config_version_hash=summary["config_version_hash"],
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
    summary = capability_snapshot.get("summary_route")
    try:
        await ps.lock_execution_routes(
            session,
            executor=capability_snapshot,
            search=search if isinstance(search, dict) else None,
            advisor=advisor if isinstance(advisor, dict) else None,
            summary=summary if isinstance(summary, dict) else None,
        )
    except ps.ProviderConfigurationChangedError as exc:
        raise DurableRunConflict("provider_configuration_changed") from exc


_ASSET_MIME_PREFIXES = {
    "image": ("image/",),
    "audio": ("audio/",),
    "video": ("video/",),
    "document": ("application/pdf",),
}


def _asset_metadata_int(metadata: dict[str, Any], key: str, *, minimum: int) -> int:
    try:
        return max(minimum, int(metadata.get(key) or minimum))
    except (TypeError, ValueError, OverflowError):
        return minimum


def _canonical_asset_part(part, asset: ChatAsset) -> dict[str, Any]:
    """Project an input asset reference into the durable canonical part shape."""
    metadata = asset.media_metadata if isinstance(asset.media_metadata, dict) else {}
    base: dict[str, Any] = {
        "type": part.type,
        "asset_id": asset.id,
        "mime_type": asset.mime_type,
        "name": asset.original_name,
    }
    if part.type == "image":
        base.update(
            width=_asset_metadata_int(metadata, "width", minimum=1),
            height=_asset_metadata_int(metadata, "height", minimum=1),
        )
    elif part.type == "audio":
        base["duration_ms"] = _asset_metadata_int(metadata, "duration_ms", minimum=0)
    elif part.type == "video":
        base.update(
            duration_ms=_asset_metadata_int(metadata, "duration_ms", minimum=0),
            width=_asset_metadata_int(metadata, "width", minimum=1),
            height=_asset_metadata_int(metadata, "height", minimum=1),
        )
    elif part.type == "document":
        base["page_count"] = _asset_metadata_int(metadata, "page_count", minimum=1)
    return base


async def _canonical_user_parts(
    session,
    *,
    parts: list[dict[str, Any]],
    user_id: str,
    project_id: str,
) -> list[dict[str, Any]]:
    """Validate input references and return encrypted-message-compatible parts."""
    try:
        parsed = validate_user_input_parts(parts)
    except (TypeError, ValueError) as exc:
        raise DurableRunInputError("input parts are invalid") from exc
    requested = [(index, part) for index, part in enumerate(parsed) if part.type != "text"]
    if not requested:
        return [{"type": "text", "text": part.text} for part in parsed]
    asset_ids = [part.asset_id for _, part in requested]
    if len(set(asset_ids)) != len(asset_ids):
        raise DurableRunInputError("an input asset may appear only once")
    rows = (
        (await session.execute(select(ChatAsset).where(ChatAsset.id.in_(asset_ids)).with_for_update())).scalars().all()
    )
    by_id = {row.id: row for row in rows}
    canonical: list[dict[str, Any]] = []
    for part in parsed:
        if part.type == "text":
            canonical.append({"type": "text", "text": part.text})
            continue
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
        canonical.append(_canonical_asset_part(part, asset))
    return canonical


async def _bind_user_assets(
    session,
    *,
    parts: list[dict[str, Any]],
    message_id: int | None,
    run_id: str,
    user_id: str,
    project_id: str,
) -> list[dict[str, Any]]:
    """Bind owned input assets and return the canonical parts persisted for the message."""
    canonical = await _canonical_user_parts(session, parts=parts, user_id=user_id, project_id=project_id)
    for index, part in enumerate(canonical):
        if part["type"] == "text":
            continue
        session.add(ChatRunAsset(run_id=run_id, asset_id=part["asset_id"], purpose="input"))
        if message_id is not None:
            session.add(ChatMessageAsset(message_id=message_id, asset_id=part["asset_id"], part_index=index))
    return canonical


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
    run_kind: Literal["completion", "compaction"] = "completion",
    source: str = "web",
    api_key_id: int | None = None,
    expected_parent_id: int | None = None,
) -> ChatRunDescriptor:
    """Atomically create the user turn, run, and first journal event.

    The conversation row is locked before both idempotency and active-run checks;
    failed conflicts therefore roll back the user turn and active leaf together.
    """
    try:
        uuid.UUID(client_request_id)
    except ValueError as exc:
        raise DurableRunInputError("Idempotency-Key must be a UUID") from exc
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

        active = (
            await session.execute(
                select(ChatRun.id)
                .where(ChatRun.conversation_id == conversation_id, ChatRun.status.in_(NONTERMINAL))
                .with_for_update()
            )
        ).scalar_one_or_none()
        if active is not None:
            raise DurableRunConflict("conversation_run_active")

        if conversation.active_leaf_id != expected_parent_id:
            raise DurableRunConflict("context_revision_changed")
        await _lock_run_configurations(session, capability_snapshot, model_name=model_name)

        # Build and persist the canonical user row before constructing the
        # durable run. Asset rows are bound to the preallocated run ID in the
        # same transaction, so no accepted run can lack its input ownership.
        created_at, created_at_local, created_timezone = message_timestamps(client_timezone)
        user_message = ChatMessage(
            conversation_id=conversation_id,
            role="user",
            parent_id=conversation.active_leaf_id,
            content=encrypt_chat_content(user_content) if user_content else None,
            status="complete",
            token_prompt=0,
            token_completion=0,
            created_at=created_at,
            created_at_local=created_at_local,
            created_timezone=created_timezone,
        )
        session.add(user_message)
        await session.flush()
        conversation.active_leaf_id = user_message.id

        canonical_parts = await _canonical_user_parts(session, parts=user_parts, user_id=user_id, project_id=project_id)
        user_message.parts = serialize_parts(canonical_parts)
        user_message.parts_version = 1
        run_id = str(uuid.uuid4())
        for index, part in enumerate(canonical_parts):
            if part["type"] == "text":
                continue
            session.add(ChatRunAsset(run_id=run_id, asset_id=part["asset_id"], purpose="input"))
            session.add(ChatMessageAsset(message_id=user_message.id, asset_id=part["asset_id"], part_index=index))

        run = ChatRun(
            id=run_id,
            run_scope="persistent",
            conversation_id=conversation_id,
            user_message_id=user_message.id,
            project_id=project_id,
            user_id=user_id,
            model_name=model_name,
            source=source,
            api_key_id=api_key_id,
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
            run_kind=run_kind,
            current_ordinal=0,
            status="queued",
        )
        _add_run_providers(session, run, capability_snapshot)
        session.add(run)
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
                    "run_kind": run_kind,
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
    pricing_snapshot: dict[str, Any] | None = None,
    execution_protocol_version: int = 1,
    run_kind: Literal["completion", "compaction"] = "completion",
    source: str = "web",
    api_key_id: int | None = None,
    expected_parent_id: int | None = None,
) -> ChatRunDescriptor:
    """Create/reuse one owner-scoped run and its first durable event."""
    try:
        uuid.UUID(client_request_id)
    except ValueError as exc:
        raise DurableRunInputError("Idempotency-Key must be a UUID") from exc
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
        conv = None
        thread = None
        if conversation_id is not None:
            conv = (
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
            if conv is None:
                raise DurableRunNotFound("conversation was not found")
        elif temp_thread_id is not None:
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
            if conv.active_leaf_id != expected_parent_id:
                raise DurableRunConflict("context_revision_changed")
        elif temp_thread_id is not None:
            active = (
                await session.execute(
                    select(ChatRun.id)
                    .where(ChatRun.temp_thread_id == temp_thread_id, ChatRun.status.in_(NONTERMINAL))
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if active is not None:
                raise DurableRunConflict("conversation_run_active")

        await _lock_run_configurations(session, capability_snapshot, model_name=model_name)
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
            source=source,
            api_key_id=api_key_id,
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
            run_kind=run_kind,
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
                    "run_kind": run_kind,
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
    run_kind: Literal["completion", "compaction"] = "completion",
    source: str = "web",
    api_key_id: int | None = None,
) -> ChatRunDescriptor:
    """Atomically create/reuse a temporary thread and its first durable run."""
    try:
        uuid.UUID(client_request_id)
    except ValueError as exc:
        raise DurableRunInputError("Idempotency-Key must be a UUID") from exc
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
        run = ChatRun(
            id=str(uuid.uuid4()),
            run_scope="temp",
            temp_thread_id=thread.id,
            project_id=project_id,
            user_id=user_id,
            model_name=model_name,
            source=source,
            api_key_id=api_key_id,
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
            run_kind=run_kind,
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
                    "run_kind": run_kind,
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


async def create_compaction_run(
    *,
    project_id: str,
    user_id: str,
    client_request_id: str,
    intent: dict[str, Any],
    conversation_id: str | None = None,
    temp_thread_id: str | None = None,
    expected_context_revision: str,
    model_name: str,
    request_payload: dict[str, Any],
    capability_snapshot: dict[str, Any],
    pricing_snapshot: dict[str, Any],
    execution_protocol_version: int = 1,
    source: str = "web",
    api_key_id: int | None = None,
) -> ChatRunDescriptor:
    """Admit a manual compaction run with strict parent->run->checkpoint locking and revision fence."""
    try:
        uuid.UUID(client_request_id)
    except ValueError as exc:
        raise DurableRunInputError("Idempotency-Key must be a UUID") from exc
    if (conversation_id is None and temp_thread_id is None) or (
        conversation_id is not None and temp_thread_id is not None
    ):
        raise DurableRunInputError("compaction requires exactly one parent conversation or temp thread")

    _require_supported_execution_protocol_version(execution_protocol_version)
    request_payload = {**request_payload, "execution_protocol_version": execution_protocol_version}
    fingerprint = _fingerprint(intent)
    factory = _factory()
    async with factory() as session, session.begin():
        conv = None
        thread = None
        if conversation_id is not None:
            conv = (
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
            if conv is None:
                raise DurableRunNotFound("conversation was not found")
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
        else:
            active = (
                await session.execute(
                    select(ChatRun.id)
                    .where(ChatRun.temp_thread_id == temp_thread_id, ChatRun.status.in_(NONTERMINAL))
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if active is not None:
                raise DurableRunConflict("conversation_run_active")
        from lumen.services import context_store

        current_source = await context_store.load_context_source(
            conversation_id=conversation_id,
            temp_thread_id=temp_thread_id,
            user_id=user_id,
            project_id=project_id,
            session=session,
        )
        if current_source.get("revision") != expected_context_revision:
            raise DurableRunConflict("context_revision_changed")

        await _lock_run_configurations(session, capability_snapshot, model_name=model_name)

        run = ChatRun(
            id=str(uuid.uuid4()),
            run_scope="persistent" if conversation_id else "temp",
            conversation_id=conversation_id,
            temp_thread_id=temp_thread_id,
            user_message_id=None,
            assistant_message_id=None,
            project_id=project_id,
            user_id=user_id,
            model_name=model_name,
            source=source,
            api_key_id=api_key_id,
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
            run_kind="compaction",
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
                    "effective_features": request_payload.get("features", {}),
                    "run_kind": "compaction",
                },
            ),
        )
        await append_event(session, run, _event(run, "run.stage.changed", {"stage": "queued"}))
        created = descriptor(run)
    await wake_run(created.run_id)
    return created
