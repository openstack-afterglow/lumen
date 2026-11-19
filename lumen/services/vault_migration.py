"""Idempotent online import from the old encrypted chat-memory source.

``python -m lumen.services.vault_migration status`` prints read-only, content-free import progress.
"""

import argparse
import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from time import monotonic

from cryptography.exceptions import InvalidTag
from keystoneclient.exceptions import NotFound
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool

from lumen import auth
from lumen.config import get_settings
from lumen.crypto import decrypt_chat_content, derive_encryption_subkey
from lumen.db import close_db, get_session_factory, init_db
from lumen.models.chat_db import ChatMemory, ChatWorkspace
from lumen.services import auxiliary, vault_store

_LEGACY_LIMIT = 1 << 40
# Deferred rows stay pending (status NULL); the reason is logged because no column carries it.
_RETRY = {
    "origin_disabled": timedelta(minutes=15),
    "origin_unresolved": timedelta(minutes=60),
    "directory_unavailable": timedelta(minutes=1),
    "decrypt_failed": timedelta(minutes=5),
}
logger = logging.getLogger(__name__)


class UnresolvedProject(RuntimeError):
    """Project/domain authority could not be established; leave source sealed."""


class DisabledProject(RuntimeError):
    """A confirmed disabled origin cannot supply migration authority."""


def _native_domain(project_id: str) -> str:
    ks = auth._get_admin_ks_client()
    project = ks.projects.get(project_id)
    domain_id = getattr(project, "domain_id", None)
    if project.id != project_id or not isinstance(domain_id, str) or not 0 < len(domain_id) <= 64:
        raise UnresolvedProject
    domain = ks.domains.get(domain_id)
    if domain.id != domain_id:
        raise UnresolvedProject
    if project.enabled is False or domain.enabled is False:
        raise DisabledProject
    if project.enabled is not True or domain.enabled is not True:
        raise UnresolvedProject
    return domain_id


async def _import_one(session, row: ChatMemory, domain_id: str | None) -> None:
    # Do not hydrate ciphertext until the exact origin project has a verified domain.
    if row.scope == "account":
        if row.project_id is not None or row.workspace_id is not None or domain_id is not None:
            raise UnresolvedProject
    elif (
        not row.project_id or not domain_id or row.scope not in {"project", "workspace"}
        or (row.scope == "workspace") != (row.workspace_id is not None)
    ):
        raise UnresolvedProject
    if row.workspace_id is not None and not await session.scalar(select(ChatWorkspace.id).where(
        ChatWorkspace.id == row.workspace_id, ChatWorkspace.owner_user_id == row.user_id,
    )):
        row.vault_migration_status = "no_workspace"
        return
    if row.status != "active" or not row.content:
        row.vault_migration_status = "inactive"
        return
    if not 0 < row.id < _LEGACY_LIMIT:
        row.vault_migration_status = "id_out_of_range"
        return
    plaintext = decrypt_chat_content(row.content)
    try:
        vault_store.check_payload(plaintext)
    except vault_store.VaultError as exc:
        row.vault_migration_status = "secret" if exc.code == "secret_detected" else "invalid"
        return
    note = vault_store.new_note(
        id=row.id, user_id=row.user_id, domain_id=domain_id, project_id=row.project_id,
        slug=f"legacy-{row.id}", title="Chat memory", kind="user_note", workspace_id=row.workspace_id,
        category=row.category, confidence=row.confidence, expires_at=row.expires_at, is_active=row.is_active,
    )
    note.created_at = row.created_at
    note.updated_at = row.updated_at
    note.details = {"imported_from": "chat_memories", "extraction_status": row.extraction_status}
    session.add(note)
    await vault_store.revise(session, note, plaintext=plaintext, change="import", imported=True,
                             vector_event="upsert" if note.is_active else "delete")
    row.vault_migration_status = "imported"


def _defer(row: ChatMemory, reason: str, now: datetime) -> str:
    row.vault_migration_retry_at = now + _RETRY[reason]
    logger.debug("vault_import_deferred_row memory_id=%s reason=%s", row.id, reason)
    return reason


@auxiliary.retry_db
async def _finish_one(factory, note_id: int, project_id: str | None, domain_id: str | None, *,
                      blocked: str | None, deferral: str | None = None) -> str | None:
    """Return the deferral reason when the row stays pending."""
    now = datetime.now(UTC)
    async with factory() as session, session.begin():
        row = await session.get(ChatMemory, note_id, with_for_update=True)
        if row is None or row.vault_migration_status is not None or row.project_id != project_id:
            return None
        if blocked is not None:
            row.vault_migration_status = blocked
            return None
        if project_id and domain_id is None:
            return _defer(row, deferral or "directory_unavailable", now)
        try:
            async with session.begin_nested():
                await _import_one(session, row, domain_id)
        except InvalidTag:
            # Misconfigured encryption keys and bad ciphertext are indistinguishable.
            return _defer(row, "decrypt_failed", now)
        except (UnresolvedProject, ValueError, IntegrityError):
            row.vault_migration_status = "invalid"
        return None


async def migrate_batch(*, limit: int = 32) -> int:
    factory = get_session_factory()
    if factory is None:
        return 0
    try:
        # A missing or malformed service key is deployment state, not evidence that a row is invalid.
        derive_encryption_subkey(b"agent_vault_body_hash")
    except ValueError:
        logger.error("Vault legacy import paused: Lumen encryption key is missing or invalid")
        return 0
    async with factory() as session:
        targets = (await session.execute(select(ChatMemory.id, ChatMemory.project_id).where(
            ChatMemory.vault_migration_status.is_(None),
            or_(ChatMemory.vault_migration_retry_at.is_(None), ChatMemory.vault_migration_retry_at <= datetime.now(UTC)),
        ).order_by(ChatMemory.id).limit(limit))).all()
    domains: dict[str, tuple[str | None, str | None, str | None]] = {}
    deferred: dict[tuple[str | None, str], int] = {}
    for note_id, project_id in targets:
        if project_id is None:
            domain_id, blocked, deferral = None, None, None
        else:
            if project_id not in domains:
                try:
                    domains[project_id] = await run_in_threadpool(_native_domain, project_id), None, None
                except NotFound:
                    domains[project_id] = None, "origin_missing", None
                except DisabledProject:
                    # A disabled project or domain can be re-enabled; keep its rows pending.
                    domains[project_id] = None, None, "origin_disabled"
                except UnresolvedProject:
                    # Inconsistent identity metadata needs operator attention, not minute-by-minute retries.
                    domains[project_id] = None, None, "origin_unresolved"
                except Exception:
                    domains[project_id] = None, None, "directory_unavailable"
            domain_id, blocked, deferral = domains[project_id]
        reason = await _finish_one(factory, note_id, project_id, domain_id, blocked=blocked, deferral=deferral)
        if reason is not None:
            deferred[(project_id, reason)] = deferred.get((project_id, reason), 0) + 1
    for (project_id, reason), rows in sorted(deferred.items(), key=lambda item: (item[0][0] or "", item[0][1])):
        logger.warning(
            "vault_import_deferred project_id=%s reason=%s rows=%s retry_after_seconds=%s",
            project_id or "-", reason, rows, int(_RETRY[reason].total_seconds()),
        )
    return len(targets)


@auxiliary.worker_step
async def process_one(*, owner: str) -> bool:
    deadline = monotonic() + 2
    found = False
    while True:
        batch = await migrate_batch(limit=32)
        found = found or bool(batch)
        if batch < 32 or monotonic() >= deadline:
            return found


async def migration_status() -> dict:
    """Content-free progress: sealed statuses, due pending rows and deferred origins."""
    factory = get_session_factory()
    if factory is None:
        raise RuntimeError("chat DB is not configured")
    now = datetime.now(UTC)
    pending = ChatMemory.vault_migration_status.is_(None)
    deferred = ChatMemory.vault_migration_retry_at > now
    async with factory() as session:
        statuses = (await session.execute(
            select(ChatMemory.vault_migration_status, func.count(ChatMemory.id))
            .group_by(ChatMemory.vault_migration_status)
        )).all()
        due = await session.scalar(select(func.count(ChatMemory.id)).where(
            pending, or_(ChatMemory.vault_migration_retry_at.is_(None), ChatMemory.vault_migration_retry_at <= now),
        ))
        origins = (await session.execute(
            select(ChatMemory.project_id, func.count(ChatMemory.id), func.max(ChatMemory.vault_migration_retry_at))
            .where(pending, deferred).group_by(ChatMemory.project_id).order_by(ChatMemory.project_id)
        )).all()
    return {
        "statuses": {status or "pending": int(count) for status, count in sorted(statuses, key=lambda item: item[0] or "")},
        "pending": {"due": int(due or 0), "deferred": sum(int(count) for _project, count, _retry in origins)},
        "deferred_origins": [
            {"project_id": project, "rows": int(count), "next_attempt_at": retry.isoformat() if retry else None}
            for project, count, retry in origins
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m lumen.services.vault_migration")
    parser.add_argument("command", choices=["status"])
    parser.parse_args(argv)

    async def run() -> dict:
        settings = get_settings()
        if not settings.database_url:
            raise SystemExit("database_url is not configured")
        init_db(settings.database_url, pool_size=1, max_overflow=0)
        try:
            return await migration_status()
        finally:
            await close_db()

    print(json.dumps(asyncio.run(run()), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
