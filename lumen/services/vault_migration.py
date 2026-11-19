"""Idempotent online import from the old encrypted chat-memory source."""

import logging
from datetime import UTC, datetime, timedelta
from time import monotonic

from cryptography.exceptions import InvalidTag
from keystoneclient.exceptions import NotFound
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool

from lumen import auth
from lumen.crypto import decrypt_chat_content, derive_encryption_subkey
from lumen.db import get_session_factory
from lumen.models.chat_db import ChatMemory, ChatWorkspace
from lumen.services import auxiliary, vault_store

_LEGACY_LIMIT = 1 << 40
_DISABLED_RETRY = timedelta(minutes=15)
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


@auxiliary.retry_db
async def _finish_one(factory, note_id: int, project_id: str | None, domain_id: str | None, *,
                      blocked: str | None, retry_after: timedelta = timedelta(minutes=1)) -> None:
    now = datetime.now(UTC)
    async with factory() as session, session.begin():
        row = await session.get(ChatMemory, note_id, with_for_update=True)
        if row is None or row.vault_migration_status is not None or row.project_id != project_id:
            return
        if blocked is not None:
            row.vault_migration_status = blocked
            return
        if project_id and domain_id is None:
            row.vault_migration_retry_at = now + retry_after
            return
        try:
            async with session.begin_nested():
                await _import_one(session, row, domain_id)
        except InvalidTag:
            # Misconfigured encryption keys and bad ciphertext are indistinguishable.
            row.vault_migration_retry_at = now + timedelta(minutes=5)
        except (UnresolvedProject, ValueError, IntegrityError):
            row.vault_migration_status = "invalid"


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
    domains: dict[str, tuple[str | None, str | None, timedelta]] = {}
    for note_id, project_id in targets:
        if project_id is None:
            domain_id, blocked, retry_after = None, None, timedelta(minutes=1)
        else:
            if project_id not in domains:
                try:
                    domains[project_id] = await run_in_threadpool(_native_domain, project_id), None, timedelta(minutes=1)
                except NotFound:
                    domains[project_id] = None, "origin_missing", timedelta(minutes=1)
                except DisabledProject:
                    # A disabled project or domain can be re-enabled; keep its rows pending.
                    domains[project_id] = None, None, _DISABLED_RETRY
                except Exception:
                    domains[project_id] = None, None, timedelta(minutes=1)
            domain_id, blocked, retry_after = domains[project_id]
        await _finish_one(factory, note_id, project_id, domain_id, blocked=blocked, retry_after=retry_after)
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
