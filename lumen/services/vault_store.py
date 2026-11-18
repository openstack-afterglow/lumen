"""Caller-scoped SQL access to encrypted Agent Vault notes."""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError

from lumen import auth, policy
from lumen.crypto import decrypt_chat_content, derive_encryption_subkey, encrypt_chat_content
from lumen.db import get_session_factory, is_db_available
from lumen.models.chat_db import ChatWorkspace
from lumen.models.vault import VaultNote, VaultRevision
from lumen.services.vault_secret import contains_secret

VaultKind = Literal[
    "user_note", "profile", "coding_standards", "short_term", "long_term", "role_memory", "handoff",
    "log", "worktree", "knowledge", "project", "spec", "change", "wiki", "persona",
]
Role = Literal["coordinator", "builder", "investigator", "reviewer"]
APPEND_ONLY = frozenset({"long_term", "role_memory", "handoff", "log", "worktree"})


class VaultError(ValueError):
    def __init__(self, code: str, status: int = 422):
        super().__init__(code)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class VaultScope:
    user_id: str
    domain_id: str
    project_id: str
    workspace_id: int | None = None

    def visible(self):
        expires = or_(VaultNote.expires_at.is_(None), VaultNote.expires_at > datetime.now(UTC))
        if self.workspace_id is not None:
            return (
                (VaultNote.user_id == self.user_id) & (VaultNote.domain_id == self.domain_id)
                & (VaultNote.workspace_id == self.workspace_id) & workspace_owner(self.user_id).exists()
                & expires
            )
        return (
            (VaultNote.user_id == self.user_id)
            & or_(VaultNote.domain_id.is_(None), VaultNote.domain_id == self.domain_id)
            & VaultNote.workspace_id.is_(None) & expires
        )


def human_domain(principal: dict | None) -> str | None:
    if (
        not principal
        or principal.get("auth_type") != "keystone"
        or principal.get("credential_class", "keystone") != "keystone"
        or principal.get("source") != "web"
        or principal.get("api_key_id") is not None
        or principal.get("credential_restricted", False)
        or principal.get("ingress_verified") is not True
    ):
        return None
    domain_id = principal.get("domain_id")
    return domain_id if isinstance(domain_id, str) and 0 < len(domain_id) <= 64 else None


def frozen_run_domain(run) -> str | None:
    """Only an admitted human dashboard run can carry a Vault scope."""
    if (
        run.source != "web" or run.api_key_id is not None or run.run_scope != "persistent"
        or run.parent_run_id is not None
    ):
        return None
    snapshot = run.capability_snapshot or {}
    if snapshot.get("credential_authority") is not None or snapshot.get("vault_ingress_verified") is not True:
        return None
    domain_id = snapshot.get("vault_domain_id")
    return domain_id if isinstance(domain_id, str) and 0 < len(domain_id) <= 64 else None


async def revalidate(scope: VaultScope, action: str, *, tool: bool = False) -> None:
    """Rebind the admitted project to its current Keystone domain and caller policy."""
    try:
        current = await auth.resolve_project_authority(scope.user_id, scope.project_id)
    except HTTPException as exc:
        raise VaultError("vault_unavailable" if exc.status_code >= 500 else "vault_authority_revoked",
                         503 if exc.status_code >= 500 else 403) from exc
    if current.get("domain_id") != scope.domain_id:
        raise VaultError("vault_authority_revoked", 403)
    principal = {**current, "user_id": scope.user_id, "project_id": scope.project_id, "auth_type": "keystone"}
    if not policy.authorize(f"native:memory:{action}", principal) or (
        tool and not policy.authorize("native:tools:execute", principal)
    ):
        raise VaultError("vault_authority_revoked", 403)


def require_db():
    factory = get_session_factory()
    if factory is None or not is_db_available():
        raise VaultError("vault_unavailable", 503)
    return factory


def workspace_owner(user_id: str):
    return select(ChatWorkspace.id).where(
        ChatWorkspace.id == VaultNote.workspace_id, ChatWorkspace.owner_user_id == user_id,
    )


async def _check_workspace(session, scope: VaultScope, *, lock: bool = False) -> None:
    if scope.workspace_id is None:
        return
    stmt = select(ChatWorkspace.id).where(
        ChatWorkspace.id == scope.workspace_id, ChatWorkspace.owner_user_id == scope.user_id,
    )
    if await session.scalar(stmt.with_for_update() if lock else stmt) is None:
        raise VaultError("workspace_not_found", 404)


def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False)


def scope_key(user_id: str, domain_id: str | None, project_key: str | None,
              kind: str, role: str | None, slug: str) -> str:
    return hashlib.sha256(_canonical([user_id, domain_id, project_key, kind, role, slug]).encode()).hexdigest()


def check_payload(content: str, *, title: str = "", slug: str = "", metadata: dict | None = None) -> None:
    try:
        metadata_text = _canonical(metadata or {})
    except (ValueError, TypeError, RecursionError) as exc:
        raise VaultError("invalid_metadata") from exc
    if not content.strip() or len(content) > 100_000 or len(metadata_text) > 8_000:
        raise VaultError("invalid_note")
    if any(contains_secret(text) for text in (content, title, slug, metadata_text)):
        raise VaultError("secret_detected")


def new_note(*, user_id: str, domain_id: str | None, slug: str, title: str, kind: str,
             project_key: str | None = None, role: str | None = None, **values) -> VaultNote:
    now = datetime.now(UTC)
    return VaultNote(
        user_id=user_id, domain_id=domain_id, project_key=project_key, role=role, kind=kind,
        scope_key=scope_key(user_id, domain_id, project_key, kind, role, slug), slug=slug, title=title,
        details={}, status="active", revision=0, created_at=now, updated_at=now, **values,
    )


async def revise(session, note: VaultNote, *, plaintext: str, change: str,
                 vector_event: str | None = None, imported: bool = False) -> None:
    """Store current value and audit snapshot in the same transaction."""
    note.revision += 1
    if not imported:
        note.updated_at = datetime.now(UTC)
    note.content = encrypt_chat_content(plaintext)
    note.body_hash = hmac.new(
        derive_encryption_subkey(b"agent_vault_body_hash"),
        _canonical([note.scope_key, note.revision, plaintext]).encode(), hashlib.sha256,
    ).hexdigest()
    await session.flush()
    session.add(VaultRevision(
        note_id=note.id, revision=note.revision, content=note.content, body_hash=note.body_hash,
        title=note.title, details=note.details, status=note.status, change_kind=change, created_at=note.updated_at,
    ))
    if vector_event is not None:
        from lumen.services.memory_store import queue_semantic_mutation

        await queue_semantic_mutation(session, row=note, mutation=vector_event, plaintext=plaintext)


def _public(note: VaultNote, *, content: str | None = None) -> dict:
    return {
        "id": note.id, "kind": note.kind, "slug": note.slug, "title": note.title,
        "content": decrypt_chat_content(note.content) if content is None else content,
        "hash": note.body_hash, "scope": "global" if note.domain_id is None else "domain",
        "project_key": note.project_key, "role": note.role, "metadata": note.details,
        "workspace_id": note.workspace_id,
        "revision": note.revision, "status": note.status, "supersedes_id": note.supersedes_id,
        "created_at": note.created_at.isoformat(), "updated_at": note.updated_at.isoformat(),
    }


def _owned(scope: VaultScope):
    return select(VaultNote).where(scope.visible(), VaultNote.status == "active")


async def _find(session, scope: VaultScope, note_id: int, *, lock: bool = False) -> VaultNote:
    stmt = _owned(scope).where(VaultNote.id == note_id)
    note = await session.scalar(stmt.with_for_update() if lock else stmt)
    if note is None:
        raise VaultError("note_not_found", 404)
    return note


async def read_note(scope: VaultScope, note_id: int) -> dict:
    async with require_db()() as session:
        await _check_workspace(session, scope)
        return _public(await _find(session, scope, note_id))


async def list_notes(scope: VaultScope, *, kind: str | None = None, project_key: str | None = None,
                     role: str | None = None, note_scope: str | None = None, query: str | None = None,
                     limit: int = 50, before: int | None = None, include_content: bool = True) -> list[dict]:
    stmt = _owned(scope)
    if kind is not None:
        stmt = stmt.where(VaultNote.kind == kind)
    if project_key is not None:
        stmt = stmt.where(VaultNote.project_key == project_key)
    if role is not None:
        stmt = stmt.where(VaultNote.role == role)
    if note_scope == "domain":
        stmt = stmt.where(VaultNote.domain_id == scope.domain_id)
    if note_scope == "global":
        stmt = stmt.where(VaultNote.domain_id.is_(None))
    if before is not None:
        stmt = stmt.where(VaultNote.id < before)
    stmt = stmt.order_by(VaultNote.id.desc())
    async with require_db()() as session:
        await _check_workspace(session, scope)
        if query is None:
            return [_public(note, content=None if include_content else "")
                    for note in (await session.scalars(stmt.limit(limit))).all()]
        # Encrypted text cannot be filtered by SQL. Stream only authorized rows and stop at the page limit.
        needle = query.casefold().strip()
        if not needle:
            raise VaultError("invalid_query")
        matches = []
        rows = await session.stream_scalars(stmt.execution_options(yield_per=32))
        try:
            async for note in rows:
                plaintext = decrypt_chat_content(note.content)
                if any(needle in value.casefold() for value in (note.title, note.slug, plaintext)):
                    matches.append(_public(note, content=plaintext))
                    if len(matches) == limit:
                        break
        finally:
            await rows.close()
        return matches


async def revisions(scope: VaultScope, note_id: int, *, limit: int = 50, before: int | None = None) -> list[dict]:
    async with require_db()() as session:
        await _check_workspace(session, scope)
        note = await _find(session, scope, note_id)
        stmt = select(VaultRevision).where(VaultRevision.note_id == note.id)
        if before is not None:
            stmt = stmt.where(VaultRevision.revision < before)
        rows = await session.scalars(stmt.order_by(VaultRevision.revision.desc()).limit(limit))
        return [{
            "revision": row.revision, "content": decrypt_chat_content(row.content), "hash": row.body_hash,
            "title": row.title, "metadata": row.details, "status": row.status, "change_kind": row.change_kind,
            "created_at": row.created_at.isoformat(),
        } for row in rows]


def check_replace(note: VaultNote, expected_hash: str | None) -> None:
    if note.kind in APPEND_ONLY:
        raise VaultError("append_only", 409)
    if not expected_hash or not expected_hash.isascii() or not hmac.compare_digest(expected_hash, note.body_hash):
        raise VaultError("hash_mismatch", 409)


async def put_note(scope: VaultScope, *, kind: str, slug: str, title: str, content: str,
                   mode: str = "create", expected_hash: str | None = None, note_scope: str = "domain",
                   project_key: str | None = None, role: str | None = None, metadata: dict | None = None) -> dict:
    check_payload(content, title=title, slug=slug, metadata=metadata)
    if scope.workspace_id is not None and (kind != "user_note" or note_scope != "domain" or project_key or role):
        raise VaultError("invalid_scope")
    if note_scope == "global" and project_key is not None:
        raise VaultError("invalid_scope")
    if kind in {"profile", "coding_standards"} and (note_scope != "global" or role is not None):
        raise VaultError("invalid_scope")
    if kind in APPEND_ONLY and mode == "replace":
        raise VaultError("append_only", 409)
    domain_id = None if note_scope == "global" else scope.domain_id
    key_slug = slug if scope.workspace_id is None else f"workspace:{scope.workspace_id}:{slug}"
    key = scope_key(scope.user_id, domain_id, project_key, kind, role, key_slug)
    try:
        async with require_db()() as session, session.begin():
            await _check_workspace(session, scope, lock=True)
            note = await session.scalar(_owned(scope).where(VaultNote.scope_key == key).with_for_update())
            if note is None:
                if mode == "replace" or expected_hash is not None:
                    raise VaultError("hash_mismatch", 409)
                note = new_note(user_id=scope.user_id, domain_id=domain_id, project_key=project_key,
                                kind=kind, role=role, slug=slug, title=title)
                note.scope_key = key
                note.workspace_id = scope.workspace_id
                if kind == "user_note":
                    note.category = "general"
                    note.project_id = scope.project_id if domain_id is not None else None
                session.add(note)
                change = "create"
            elif mode == "create" or kind in APPEND_ONLY:
                raise VaultError("append_only" if kind in APPEND_ONLY else "note_exists", 409)
            else:
                change = mode
                if mode == "replace":
                    check_replace(note, expected_hash)
                else:
                    content = f"{decrypt_chat_content(note.content)}\n\n{content}"
                    check_payload(content)
            note.title = title
            if metadata is not None:
                note.details = metadata
            await revise(session, note, plaintext=content, change=change,
                         vector_event="upsert" if kind == "user_note" and project_key is None else None)
            return _public(note, content=content)
    except IntegrityError as exc:
        raise VaultError("note_exists", 409) from exc


async def replace_note(scope: VaultScope, note_id: int, *, content: str, expected_hash: str,
                       title: str | None = None) -> dict:
    check_payload(content, title=title or "")
    async with require_db()() as session, session.begin():
        await _check_workspace(session, scope, lock=True)
        note = await _find(session, scope, note_id, lock=True)
        check_replace(note, expected_hash)
        if title is not None:
            note.title = title
        await revise(session, note, plaintext=content, change="replace",
                     vector_event="upsert" if note.kind == "user_note" and note.project_key is None else None)
        return _public(note, content=content)


async def read_slot(scope: VaultScope, *, kind: str, slug: str, note_scope: str,
                    project_key: str | None = None, role: str | None = None) -> dict:
    if note_scope == "global" and project_key is not None:
        raise VaultError("invalid_scope")
    domain_id = None if note_scope == "global" else scope.domain_id
    key = scope_key(scope.user_id, domain_id, project_key, kind, role, slug)
    async with require_db()() as session:
        note = await session.scalar(_owned(scope).where(VaultNote.scope_key == key))
        return {"exists": True, **_public(note)} if note is not None else {"exists": False}


async def put_slot(scope: VaultScope, *, kind: str, slug: str, title: str, content: str,
                   expected_hash: str | None, note_scope: str, project_key: str | None = None,
                   role: str | None = None) -> dict:
    try:
        return await put_note(
            scope, kind=kind, slug=slug, title=title, content=content, note_scope=note_scope,
            project_key=project_key, role=role, mode="replace" if expected_hash else "create",
            expected_hash=expected_hash,
        )
    except VaultError as exc:
        if exc.code == "note_exists":
            raise VaultError("hash_mismatch", 409) from exc
        raise
