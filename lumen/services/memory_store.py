"""Chat recall and automatic extraction over encrypted Agent Vault user notes."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import false, or_, select
from sqlalchemy.dialects.mysql import insert
from sqlalchemy.exc import OperationalError

from lumen.crypto import decrypt_chat_content, derive_encryption_subkey
from lumen.db import get_session_factory, is_db_available, mark_db_unhealthy
from lumen.models.chat_db import ChatMemoryOwnerLock
from lumen.models.chat_jobs import ChatMemoryOutbox
from lumen.models.vault import VaultNote
from lumen.services import vault_store as vs

logger = logging.getLogger(__name__)

_SCOPES = frozenset({"account", "project", "workspace"})
_CATEGORIES = frozenset({"interest", "development", "habit", "preference", "general"})
_CATEGORY_HEADINGS = (
    ("preference", "Preferences"),
    ("development", "Development"),
    ("habit", "Habits"),
    ("interest", "Interests"),
    ("general", "General"),
)


_FINGERPRINT_DOMAIN = b"chat_memory_content_fingerprint"
_STATE_FINGERPRINT_DOMAIN = b"chat_memory_state_fingerprint"


def memory_content_fingerprint(plaintext: str) -> str:
    """Keyed version marker; a vector database must not receive a dictionary-checkable digest."""
    return hmac.new(
        derive_encryption_subkey(_FINGERPRINT_DOMAIN),
        plaintext.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def memory_state_fingerprint(
    plaintext: str,
    *,
    category: str,
    status: str,
    is_active: bool,
) -> str:
    """Keyed optimistic-concurrency marker for a model-derived target."""
    payload = json.dumps(
        [plaintext, category, status, is_active],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hmac.new(
        derive_encryption_subkey(_STATE_FINGERPRINT_DOMAIN),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


class ChatStorageUnavailable(RuntimeError):
    """chat DB 미구성/장애 — fail-closed(503)."""


class MemoryNotFound(LookupError):
    """메모리 미존재 — 404."""


class MemoryForbidden(PermissionError):
    """소유자 불일치 — 403."""


class MemoryValidationError(ValueError):
    """입력 검증 실패 — 400."""


def _require_db():
    if not is_db_available():
        raise ChatStorageUnavailable("chat DB 를 사용할 수 없습니다")
    factory = get_session_factory()
    if factory is None:
        raise ChatStorageUnavailable("chat DB 가 구성되지 않았습니다")
    return factory


def _iso(dt) -> str | None:
    return dt.isoformat() if dt else None


def _public(row: VaultNote) -> dict:
    return {
        "id": row.id,
        "user_id": row.user_id,
        "project_id": row.project_id,
        "workspace_id": row.workspace_id,
        "scope": row.scope,
        "category": row.category,
        "content": decrypt_chat_content(row.content) if row.content else None,
        "expectedHash": row.body_hash,
        "confidence": str(row.confidence) if row.confidence is not None else None,
        "expires_at": _iso(row.expires_at),
        "status": row.status,
        "is_active": row.is_active,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def render_memory_markdown(memories: list[dict]) -> str:
    """Render authorized plaintext rows without creating another persistence layer."""
    groups: dict[str, list[dict]] = {category: [] for category, _heading in _CATEGORY_HEADINGS}
    for memory in memories:
        category = memory.get("category")
        content = memory.get("content")
        if (
            category in groups
            and memory.get("status") == "active"
            and memory.get("is_active") is True
            and isinstance(content, str)
            and content.strip()
        ):
            groups[category].append(memory)

    lines = ["# Memory", ""]
    if not any(groups.values()):
        return "# Memory\n\nNo active memories have been saved yet.\n"

    for category, heading in _CATEGORY_HEADINGS:
        entries = groups[category]
        if not entries:
            continue
        lines.extend((f"## {heading}", ""))
        for memory in sorted(
            entries,
            key=lambda item: (
                str(item.get("scope") or ""),
                int(item.get("workspace_id") or 0),
                int(item.get("id") or 0),
            ),
        ):
            content_lines = str(memory["content"]).strip().splitlines()
            lines.append(f"- {content_lines[0].strip()}")
            lines.extend(f"  {line.rstrip()}" for line in content_lines[1:])
        lines.append("")
    return "\n".join(lines)


def _validate_namespace(*, scope: str, project_id: str | None, workspace_id: int | None) -> None:
    if scope not in _SCOPES:
        raise MemoryValidationError("지원하지 않는 메모리 scope 입니다")
    if scope == "account" and project_id is None and workspace_id is None:
        return
    if scope == "project" and project_id and workspace_id is None:
        return
    if scope == "workspace" and project_id and isinstance(workspace_id, int) and workspace_id > 0:
        return
    raise MemoryValidationError("메모리 scope namespace 가 올바르지 않습니다")


def _validate_category(category: str) -> None:
    if category not in _CATEGORIES:
        raise MemoryValidationError("지원하지 않는 메모리 category 입니다")


def _visible(user_id, domain_id, workspace_id, include_account, *, direct=False):
    choices = [VaultNote.domain_id.is_(None)] if include_account else []
    if domain_id:
        choices.append((VaultNote.domain_id == domain_id) & VaultNote.workspace_id.is_(None))
        if workspace_id or direct:
            choices.append(
                (VaultNote.domain_id == domain_id) & vs.workspace_owner(user_id).exists()
                & (VaultNote.workspace_id.is_not(None) if direct else VaultNote.workspace_id == workspace_id)
            )
    return or_(false(), *choices)


def _notes(user_id):
    return select(VaultNote).where(
        VaultNote.user_id == user_id, VaultNote.kind == "user_note", VaultNote.project_key.is_(None),
        VaultNote.status == "active", or_(VaultNote.expires_at.is_(None), VaultNote.expires_at > datetime.now(UTC)),
    )


async def _owned(session, memory_id, user_id, project_id, domain_id, include_account, *, lock=False):
    stmt = _notes(user_id).where(VaultNote.id == memory_id,
                                _visible(user_id, domain_id, None, include_account, direct=True))
    note = await session.scalar(stmt.with_for_update() if lock else stmt)
    if note is None:
        raise MemoryNotFound("memory_not_found")
    return note


async def queue_semantic_mutation(session, *, row: VaultNote, mutation: str, plaintext: str) -> None:
    """Always preserve the source mutation; freeze generations when the index is ready."""
    from lumen.services.semantic_memory import configured_memory_index, semantic_memory_available

    required_generations: list[int] = []
    status = "pending_generation"
    if semantic_memory_available():
        try:
            required_generations = await configured_memory_index().required_generations()
            status = "queued"
        except Exception:
            logger.warning("semantic memory generation snapshot deferred", exc_info=True)
    digest = memory_content_fingerprint(plaintext)
    session.add(
        ChatMemoryOutbox(
            event_key=f"memory:{row.id}:{mutation}:{digest}:{uuid.uuid4()}",
            vault_note_id=row.id,
            mutation=mutation,
            content_hash=digest,
            required_generations=required_generations,
            applied_generations=[],
            status=status,
        )
    )


def _checked_content(value: str) -> str:
    try:
        vs.check_payload(value)
    except vs.VaultError as exc:
        raise MemoryValidationError(exc.code) from exc
    return value.strip()


def _new_chat_note(user_id, domain_id, project_id, category, workspace_id=None):
    return vs.new_note(
        user_id=user_id, domain_id=domain_id, project_id=project_id, workspace_id=workspace_id,
        slug=f"chat-{uuid.uuid4().hex}", title="Chat memory", kind="user_note", category=category,
    )


async def create_memory(*, user_id: str, content: str, scope: str = "account", project_id: str | None = None,
                        workspace_id: int | None = None, category: str = "general", domain_id: str | None = None) -> dict:
    _validate_namespace(scope=scope, project_id=project_id, workspace_id=workspace_id)
    _validate_category(category)
    content = _checked_content(content)
    if scope != "account" and not domain_id:
        raise MemoryForbidden("vault_domain_unavailable")
    note = _new_chat_note(user_id, domain_id if scope != "account" else None, project_id, category, workspace_id)
    try:
        async with _require_db()() as session, session.begin():
            session.add(note)
            await vs.revise(session, note, plaintext=content, change="create", vector_event="upsert")
            return _public(note)
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc


async def list_memories(*, user_id: str, project_id: str | None = None, workspace_id: int | None = None,
                        include_account: bool = True, domain_id: str | None = None) -> list[dict]:
    try:
        async with _require_db()() as session:
            rows = await session.scalars(_notes(user_id).where(
                _visible(user_id, domain_id, workspace_id, include_account),
            ).order_by(VaultNote.updated_at.desc(), VaultNote.id.desc()))
            return [_public(row) for row in rows]
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc


async def _hydrate(ids, user_id, predicate):
    if not ids:
        return []
    try:
        async with _require_db()() as session:
            rows = await session.scalars(_notes(user_id).where(
                VaultNote.id.in_(set(ids)), VaultNote.is_active.is_(True), predicate,
            ))
            by_id = {row.id: _public(row) for row in rows}
            return [by_id[note_id] for note_id in ids if note_id in by_id]
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc


async def hydrate_candidate_ids(*, ids: list[int], user_id: str, scope: str, project_id: str | None,
                               workspace_id: int | None, domain_id: str | None = None) -> list[dict]:
    _validate_namespace(scope=scope, project_id=project_id, workspace_id=workspace_id)
    if scope == "account":
        predicate = VaultNote.domain_id.is_(None)
    elif not domain_id:
        return []
    elif scope == "project":
        predicate = (VaultNote.domain_id == domain_id) & VaultNote.workspace_id.is_(None)
    else:
        predicate = (VaultNote.domain_id == domain_id) & (VaultNote.workspace_id == workspace_id) & (
            vs.workspace_owner(user_id).exists()
        )
    return await _hydrate(ids, user_id, predicate)


async def hydrate_namespace_ids(*, ids: list[int], user_id: str, project_id: str | None, workspace_id: int | None,
                               include_account: bool = True, domain_id: str | None = None) -> list[dict]:
    return await _hydrate(ids, user_id, _visible(user_id, domain_id, workspace_id, include_account))


async def get_memory(memory_id: int, *, user_id: str, project_id: str | None = None,
                     include_account: bool = True, domain_id: str | None = None) -> dict:
    try:
        async with _require_db()() as session:
            return _public(await _owned(session, memory_id, user_id, project_id, domain_id, include_account))
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc


async def update_memory(memory_id: int, *, user_id: str, patch: dict, project_id: str | None = None,
                        include_account: bool = True, domain_id: str | None = None,
                        expected_hash: str | None = None) -> dict:
    try:
        async with _require_db()() as session, session.begin():
            row = await _owned(session, memory_id, user_id, project_id, domain_id, include_account, lock=True)
            vs.check_replace(row, expected_hash)
            content = _checked_content(patch["content"]) if "content" in patch else decrypt_chat_content(row.content)
            if "category" in patch:
                _validate_category(patch["category"])
                row.category = patch["category"]
            if patch.get("is_active") is not None:
                row.is_active = bool(patch["is_active"])
            await vs.revise(session, row, plaintext=content, change="replace",
                            vector_event="upsert" if row.is_active else "delete")
            return _public(row)
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc


async def _archive(session, row, plaintext):
    row.status = "archived"
    row.is_active = False
    await vs.revise(session, row, plaintext=plaintext, change="archive", vector_event="delete")


async def delete_memory(memory_id: int, *, user_id: str, project_id: str | None = None,
                        include_account: bool = True, domain_id: str | None = None) -> None:
    try:
        async with _require_db()() as session, session.begin():
            row = await _owned(session, memory_id, user_id, project_id, domain_id, include_account, lock=True)
            await _archive(session, row, decrypt_chat_content(row.content))
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc


async def apply_automatic_ops_in_transaction(session, *, ops: list[dict], user_id: str,
                                             project_id: str, domain_id: str) -> dict[str, int]:
    """Accept only unchanged automatic deltas in this caller's domain chat notes."""
    applied = {"add": 0, "update": 0, "delete": 0}
    if not domain_id:
        return applied
    await session.execute(insert(ChatMemoryOwnerLock).values(user_id=user_id, project_id=domain_id).prefix_with("IGNORE"))
    await session.execute(select(ChatMemoryOwnerLock).where(
        ChatMemoryOwnerLock.user_id == user_id, ChatMemoryOwnerLock.project_id == domain_id,
    ).with_for_update())
    for op in ops:
        if not isinstance(op, dict):
            continue
        kind, category, content = op.get("op"), op.get("category"), op.get("content")
        if kind in {"add", "update"}:
            if not isinstance(content, str) or not isinstance(category, str):
                continue
            try:
                _validate_category(category)
                content = _checked_content(content)
            except MemoryValidationError:
                continue
        if kind == "add":
            note = _new_chat_note(user_id, domain_id, project_id, category)
            session.add(note)
            await vs.revise(session, note, plaintext=content, change="extract", vector_event="upsert")
        elif kind in {"update", "delete"} and type(op.get("id")) is int:
            expected = op.get("expected_state_fingerprint")
            if not isinstance(expected, str) or not expected.isascii():
                continue
            note = await session.scalar(_notes(user_id).where(
                VaultNote.id == op["id"], VaultNote.domain_id == domain_id, VaultNote.workspace_id.is_(None),
                VaultNote.is_active.is_(True),
            ).with_for_update())
            if note is None:
                continue
            plaintext = decrypt_chat_content(note.content)
            if not hmac.compare_digest(expected, memory_state_fingerprint(
                plaintext, category=note.category, status=note.status, is_active=note.is_active,
            )):
                continue
            if kind == "update":
                note.category = category
                await vs.revise(session, note, plaintext=content, change="extract", vector_event="upsert")
            else:
                await _archive(session, note, plaintext)
        else:
            continue
        applied[kind] += 1
    return applied
