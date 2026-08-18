"""Project-scoped remote code-workspace persistence and lifecycle helpers.

The service deliberately stores only resource metadata.  Repository checkout, Git
credentials in use, and every filesystem/process operation stay behind the remote
workspace runtime boundary.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError

from lumen.crypto import decrypt_chat_content, encrypt_chat_content
from lumen.db import get_session_factory, is_db_available, mark_db_unhealthy
from lumen.models.chat_agent_platform import ChatCodeWorkspace, ChatGitCredential
from lumen.models.chat_db import ChatConversation
from lumen.services import agent_workspace_runtime
from lumen.services.agent_workspace_runtime import WorkspaceRuntimePolicy
from lumen.services.code_workspace_store import (
    CodeWorkspaceValidationError,
    normalize_git_host,
    validate_workspace_source,
)

_WORKSPACE_TTL = timedelta(days=7)


class CodeWorkspaceNotFound(LookupError):
    pass


class CodeWorkspaceUnavailable(RuntimeError):
    pass


class CodeWorkspaceConflict(RuntimeError):
    pass


def _factory():
    if not is_db_available() or (factory := get_session_factory()) is None:
        raise CodeWorkspaceUnavailable("chat DB is unavailable")
    return factory


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _credential_public(row: ChatGitCredential) -> dict[str, Any]:
    return {
        "id": row.id,
        "host": row.host,
        "username": row.username,
        "token_present": bool(row.token_ciphertext),
        "credential_version": row.credential_version,
        "revoked_at": _iso(row.revoked_at),
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def _workspace_public(row: ChatCodeWorkspace) -> dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "source_kind": row.source_kind,
        "repository_url": row.repository_url,
        "source_revision": row.source_revision,
        "resolved_base_commit_sha": row.resolved_base_commit_sha,
        "working_branch": row.working_branch,
        "credential_id": row.credential_id,
        "credential_version": row.credential_version,
        "lifecycle_status": row.lifecycle_status,
        "state_revision": row.state_revision,
        "runtime_capabilities": row.runtime_capabilities or {},
        "image_digest": row.image_digest,
        "policy_fingerprint": row.policy_fingerprint,
        "writer_fence": row.writer_fence,
        "expires_at": _iso(row.expires_at),
        "error_code": row.error_code,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def _fingerprint(intent: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(intent, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def policy_fingerprint(policy: WorkspaceRuntimePolicy) -> str:
    material = {
        "image_digest": policy.sandbox.image_digest,
        "policy_version": policy.sandbox.policy_version,
        "egress_allowlist": list(policy.sandbox.egress_allowlist),
    }
    return _fingerprint(material)


async def _owned_credential(
    session, credential_id: int, *, user_id: str, project_id: str, lock: bool = False
) -> ChatGitCredential:
    stmt = select(ChatGitCredential).where(
        ChatGitCredential.id == credential_id,
        ChatGitCredential.owner_user_id == user_id,
        ChatGitCredential.owner_project_id == project_id,
    )
    if lock:
        stmt = stmt.with_for_update()
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise CodeWorkspaceNotFound("Git credential was not found")
    return row


async def _owned_workspace(
    session, workspace_id: str, *, user_id: str, project_id: str, lock: bool = False
) -> ChatCodeWorkspace:
    stmt = select(ChatCodeWorkspace).where(
        ChatCodeWorkspace.id == workspace_id,
        ChatCodeWorkspace.owner_user_id == user_id,
        ChatCodeWorkspace.owner_project_id == project_id,
    )
    if lock:
        stmt = stmt.with_for_update()
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        # Deliberately indistinguishable from an absent row to prevent cross-project discovery.
        raise CodeWorkspaceNotFound("code workspace was not found")
    return row


async def create_git_credential(
    *, user_id: str, project_id: str, host: str, username: str | None, token: str
) -> dict[str, Any]:
    normalized_host = normalize_git_host(host)
    if not isinstance(token, str) or not token.strip() or len(token) > 16_384:
        raise CodeWorkspaceValidationError("Git token is invalid")
    cleaned_username = username.strip() if isinstance(username, str) and username.strip() else None
    if cleaned_username is not None and (
        len(cleaned_username) > 255 or any(ch.isspace() or ord(ch) < 32 for ch in cleaned_username)
    ):
        raise CodeWorkspaceValidationError("Git username is invalid")
    factory = _factory()
    try:
        async with factory() as session, session.begin():
            existing = (
                await session.execute(
                    select(ChatGitCredential)
                    .where(
                        ChatGitCredential.owner_user_id == user_id,
                        ChatGitCredential.owner_project_id == project_id,
                        ChatGitCredential.host == normalized_host,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if existing is not None:
                existing.username = cleaned_username
                existing.token_ciphertext = encrypt_chat_content(token)
                existing.credential_version += 1
                existing.revoked_at = None
                await session.flush()
                return _credential_public(existing)
            row = ChatGitCredential(
                owner_user_id=user_id,
                owner_project_id=project_id,
                host=normalized_host,
                username=cleaned_username,
                token_ciphertext=encrypt_chat_content(token),
            )
            session.add(row)
            await session.flush()
            return _credential_public(row)
    except OperationalError as exc:
        mark_db_unhealthy()
        raise CodeWorkspaceUnavailable("chat DB operation failed") from exc


async def list_git_credentials(*, user_id: str, project_id: str) -> list[dict[str, Any]]:
    factory = _factory()
    try:
        async with factory() as session:
            rows = (
                (
                    await session.execute(
                        select(ChatGitCredential)
                        .where(
                            ChatGitCredential.owner_user_id == user_id, ChatGitCredential.owner_project_id == project_id
                        )
                        .order_by(ChatGitCredential.host)
                    )
                )
                .scalars()
                .all()
            )
            return [_credential_public(row) for row in rows]
    except OperationalError as exc:
        mark_db_unhealthy()
        raise CodeWorkspaceUnavailable("chat DB operation failed") from exc


async def update_git_credential(
    credential_id: int, *, user_id: str, project_id: str, username: str | None = None, token: str | None = None
) -> dict[str, Any]:
    if token is not None and (not token.strip() or len(token) > 16_384):
        raise CodeWorkspaceValidationError("Git token is invalid")
    if username is not None and (len(username.strip()) > 255 or any(ch.isspace() or ord(ch) < 32 for ch in username)):
        raise CodeWorkspaceValidationError("Git username is invalid")
    factory = _factory()
    try:
        async with factory() as session, session.begin():
            row = await _owned_credential(session, credential_id, user_id=user_id, project_id=project_id, lock=True)
            changed = False
            if username is not None:
                row.username = username.strip() or None
                changed = True
            if token is not None:
                row.token_ciphertext = encrypt_chat_content(token)
                row.revoked_at = None
                changed = True
            if changed:
                row.credential_version += 1
            await session.flush()
            return _credential_public(row)
    except OperationalError as exc:
        mark_db_unhealthy()
        raise CodeWorkspaceUnavailable("chat DB operation failed") from exc


async def revoke_git_credential(credential_id: int, *, user_id: str, project_id: str) -> None:
    factory = _factory()
    try:
        async with factory() as session, session.begin():
            row = await _owned_credential(session, credential_id, user_id=user_id, project_id=project_id, lock=True)
            if row.revoked_at is None:
                row.revoked_at = datetime.now(UTC)
                row.credential_version += 1
    except OperationalError as exc:
        mark_db_unhealthy()
        raise CodeWorkspaceUnavailable("chat DB operation failed") from exc


async def create_workspace(
    *,
    user_id: str,
    project_id: str,
    name: str,
    source_kind: str,
    repository_url: str | None,
    source_revision: str | None,
    credential_id: int | None,
    idempotency_key: str,
    policy: WorkspaceRuntimePolicy,
) -> dict[str, Any]:
    cleaned_name = name.strip() if isinstance(name, str) else ""
    if not cleaned_name or len(cleaned_name) > 100:
        raise CodeWorkspaceValidationError("workspace name is invalid")
    if not isinstance(idempotency_key, str) or not idempotency_key.strip() or len(idempotency_key) > 190:
        raise CodeWorkspaceValidationError("Idempotency-Key is required")
    credential_host: str | None = None
    factory = _factory()
    intent = {
        "idempotency_key": idempotency_key,
        "name": cleaned_name,
        "source_kind": source_kind,
        "repository_url": repository_url,
        "source_revision": source_revision,
        "credential_id": credential_id,
        "policy": policy_fingerprint(policy),
    }
    fingerprint = _fingerprint(intent)
    try:
        async with factory() as session, session.begin():
            existing = (
                await session.execute(
                    select(ChatCodeWorkspace)
                    .where(
                        ChatCodeWorkspace.owner_user_id == user_id,
                        ChatCodeWorkspace.owner_project_id == project_id,
                        ChatCodeWorkspace.request_fingerprint == fingerprint,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if existing is not None:
                return _workspace_public(existing)
            credential: ChatGitCredential | None = None
            if credential_id is not None:
                credential = await _owned_credential(
                    session, credential_id, user_id=user_id, project_id=project_id, lock=True
                )
                if credential.revoked_at is not None:
                    raise CodeWorkspaceConflict("Git credential is revoked")
                credential_host = credential.host
            normalized_url, _host = validate_workspace_source(
                source_kind=source_kind,
                repository_url=repository_url,
                source_revision=source_revision,
                credential_host=credential_host,
                allowed_domains=policy.sandbox.egress_allowlist,
            )
            if source_kind == "git" and credential is None:
                raise CodeWorkspaceValidationError("Git workspace requires an owned credential")
            workspace_id = str(uuid.uuid4())
            row = ChatCodeWorkspace(
                id=workspace_id,
                owner_user_id=user_id,
                owner_project_id=project_id,
                name=cleaned_name,
                source_kind=source_kind,
                repository_url=normalized_url,
                source_revision=source_revision.strip() if source_revision else None,
                working_branch=f"afterglow/{workspace_id}",
                credential_id=credential.id if credential else None,
                credential_version=credential.credential_version if credential else None,
                image_digest=policy.sandbox.image_digest,
                policy_fingerprint=policy_fingerprint(policy),
                expires_at=datetime.now(UTC) + _WORKSPACE_TTL,
                request_fingerprint=fingerprint,
            )
            session.add(row)
            await session.flush()
            return _workspace_public(row)
    except IntegrityError as exc:
        raise CodeWorkspaceConflict("workspace request conflicts") from exc
    except OperationalError as exc:
        mark_db_unhealthy()
        raise CodeWorkspaceUnavailable("chat DB operation failed") from exc


async def list_workspaces(*, user_id: str, project_id: str) -> list[dict[str, Any]]:
    factory = _factory()
    try:
        async with factory() as session:
            rows = (
                (
                    await session.execute(
                        select(ChatCodeWorkspace)
                        .where(
                            ChatCodeWorkspace.owner_user_id == user_id, ChatCodeWorkspace.owner_project_id == project_id
                        )
                        .order_by(ChatCodeWorkspace.updated_at.desc())
                    )
                )
                .scalars()
                .all()
            )
            return [_workspace_public(row) for row in rows]
    except OperationalError as exc:
        mark_db_unhealthy()
        raise CodeWorkspaceUnavailable("chat DB operation failed") from exc


async def get_workspace(workspace_id: str, *, user_id: str, project_id: str) -> dict[str, Any]:
    factory = _factory()
    try:
        async with factory() as session:
            return _workspace_public(
                await _owned_workspace(session, workspace_id, user_id=user_id, project_id=project_id)
            )
    except OperationalError as exc:
        mark_db_unhealthy()
        raise CodeWorkspaceUnavailable("chat DB operation failed") from exc


async def mark_workspace_provisioned(
    workspace_id: str,
    *,
    user_id: str,
    project_id: str,
    runtime_workspace_id: str,
    state_revision: int,
    capabilities: dict[str, Any],
    resolved_base_commit_sha: str | None = None,
) -> dict[str, Any]:
    if not runtime_workspace_id or state_revision < 0 or not isinstance(capabilities, dict):
        raise CodeWorkspaceValidationError("runtime workspace result is invalid")
    factory = _factory()
    try:
        async with factory() as session, session.begin():
            row = await _owned_workspace(session, workspace_id, user_id=user_id, project_id=project_id, lock=True)
            if row.lifecycle_status not in {"creating", "ready"}:
                raise CodeWorkspaceConflict("workspace cannot be provisioned")
            row.runtime_workspace_id = runtime_workspace_id
            row.state_revision = state_revision
            row.runtime_capabilities = capabilities
            row.resolved_base_commit_sha = resolved_base_commit_sha
            row.lifecycle_status = "ready"
            row.error_code = None
            await session.flush()
            return _workspace_public(row)
    except OperationalError as exc:
        mark_db_unhealthy()
        raise CodeWorkspaceUnavailable("chat DB operation failed") from exc


async def mark_workspace_error(workspace_id: str, *, error_code: str) -> None:
    factory = _factory()
    try:
        async with factory() as session, session.begin():
            row = (
                await session.execute(
                    select(ChatCodeWorkspace).where(ChatCodeWorkspace.id == workspace_id).with_for_update()
                )
            ).scalar_one_or_none()
            if row is not None and row.lifecycle_status == "creating":
                row.lifecycle_status = "error"
                row.error_code = error_code[:100]
    except OperationalError as exc:
        mark_db_unhealthy()
        raise CodeWorkspaceUnavailable("chat DB operation failed") from exc


async def request_delete_workspace(workspace_id: str, *, user_id: str, project_id: str) -> dict[str, Any]:
    factory = _factory()
    try:
        async with factory() as session, session.begin():
            row = await _owned_workspace(session, workspace_id, user_id=user_id, project_id=project_id, lock=True)
            if row.lifecycle_status not in {"deleted", "deleting"}:
                row.lifecycle_status = "deleting"
            await session.flush()
            return _workspace_public(row)
    except OperationalError as exc:
        mark_db_unhealthy()
        raise CodeWorkspaceUnavailable("chat DB operation failed") from exc


async def assign_conversation_workspace(
    conversation_id: str, *, workspace_id: str | None, user_id: str, project_id: str
) -> dict[str, Any]:
    factory = _factory()
    try:
        async with factory() as session, session.begin():
            conversation = (
                await session.execute(
                    select(ChatConversation)
                    .where(
                        ChatConversation.id == conversation_id,
                        ChatConversation.user_id == user_id,
                        ChatConversation.project_id == project_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if conversation is None:
                raise CodeWorkspaceNotFound("conversation was not found")
            if workspace_id is not None:
                workspace = await _owned_workspace(
                    session, workspace_id, user_id=user_id, project_id=project_id, lock=True
                )
                if workspace.lifecycle_status != "ready":
                    raise CodeWorkspaceConflict("code workspace is not ready")
            conversation.code_workspace_id = workspace_id
            return {"conversation_id": conversation_id, "code_workspace_id": workspace_id}
    except OperationalError as exc:
        mark_db_unhealthy()
        raise CodeWorkspaceUnavailable("chat DB operation failed") from exc


async def operation_credential(workspace_id: str, *, user_id: str, project_id: str) -> dict[str, Any] | None:
    """Decrypt a current owned credential only at runtime dispatch time.

    Callers must pass this directly to the remote runtime and must never persist
    or log the returned dictionary.
    """
    factory = _factory()
    try:
        async with factory() as session:
            workspace = await _owned_workspace(session, workspace_id, user_id=user_id, project_id=project_id)
            if workspace.credential_id is None:
                return None
            credential = await _owned_credential(
                session, workspace.credential_id, user_id=user_id, project_id=project_id
            )
            if credential.revoked_at is not None or credential.credential_version != workspace.credential_version:
                raise CodeWorkspaceConflict("Git credential changed or was revoked")
            return {
                "host": credential.host,
                "username": credential.username,
                "token": decrypt_chat_content(credential.token_ciphertext),
                "version": credential.credential_version,
            }
    except OperationalError as exc:
        mark_db_unhealthy()
        raise CodeWorkspaceUnavailable("chat DB operation failed") from exc


async def provision_pending_workspaces(policy: WorkspaceRuntimePolicy, *, limit: int = 4) -> int:
    """Provision pending rows through the remote runtime, never on the API host."""
    factory = _factory()
    try:
        async with factory() as session:
            rows = (
                (
                    await session.execute(
                        select(ChatCodeWorkspace)
                        .where(
                            ChatCodeWorkspace.lifecycle_status == "creating",
                            ChatCodeWorkspace.policy_fingerprint == policy_fingerprint(policy),
                        )
                        .order_by(ChatCodeWorkspace.created_at)
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
            candidates = [
                {
                    "id": row.id,
                    "user_id": row.owner_user_id,
                    "project_id": row.owner_project_id,
                    "source": {
                        "kind": row.source_kind,
                        "url": row.repository_url,
                        "source_revision": row.source_revision,
                        "credential_version": row.credential_version,
                    },
                }
                for row in rows
            ]
    except OperationalError as exc:
        mark_db_unhealthy()
        raise CodeWorkspaceUnavailable("chat DB operation failed") from exc

    completed = 0
    for candidate in candidates:
        try:
            credential = await operation_credential(
                candidate["id"], user_id=candidate["user_id"], project_id=candidate["project_id"]
            )
            result = await agent_workspace_runtime.provision(
                policy,
                workspace_id=candidate["id"],
                source=candidate["source"],
                operation_credential=credential,
            )
            await mark_workspace_provisioned(
                candidate["id"],
                user_id=candidate["user_id"],
                project_id=candidate["project_id"],
                runtime_workspace_id=result["workspace_id"],
                state_revision=result["state_revision"],
                capabilities=result["capabilities"],
                resolved_base_commit_sha=result.get("resolved_base_commit_sha"),
            )
            completed += 1
        except (CodeWorkspaceUnavailable, CodeWorkspaceConflict, agent_workspace_runtime.WorkspaceRuntimeUnavailable):
            await mark_workspace_error(candidate["id"], error_code="workspace_provision_failed")
    return completed


async def delete_pending_workspaces(policy: WorkspaceRuntimePolicy, *, limit: int = 4) -> int:
    """Delete tombstoned runtime workspaces; only the idempotent provider delete is retried."""
    factory = _factory()
    try:
        async with factory() as session:
            rows = (
                (
                    await session.execute(
                        select(ChatCodeWorkspace)
                        .where(
                            ChatCodeWorkspace.lifecycle_status == "deleting",
                            ChatCodeWorkspace.policy_fingerprint == policy_fingerprint(policy),
                        )
                        .order_by(ChatCodeWorkspace.updated_at)
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
            candidates = [(row.id, row.runtime_workspace_id) for row in rows]
    except OperationalError as exc:
        mark_db_unhealthy()
        raise CodeWorkspaceUnavailable("chat DB operation failed") from exc

    completed = 0
    for workspace_id, runtime_workspace_id in candidates:
        try:
            if runtime_workspace_id:
                await agent_workspace_runtime.delete(policy, runtime_workspace_id)
            async with factory() as session, session.begin():
                row = (
                    await session.execute(
                        select(ChatCodeWorkspace).where(ChatCodeWorkspace.id == workspace_id).with_for_update()
                    )
                ).scalar_one_or_none()
                if row is not None and row.lifecycle_status == "deleting":
                    row.lifecycle_status = "deleted"
                    completed += 1
        except agent_workspace_runtime.WorkspaceRuntimeUnavailable:
            # Keep the tombstone. A later worker pass may safely retry DELETE.
            continue
    return completed
