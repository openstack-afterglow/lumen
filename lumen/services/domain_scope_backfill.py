"""Promote legacy project-owned records using the exact native Keystone project."""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from urllib.parse import urlsplit

from sqlalchemy import and_, cast, column, exists, func, or_, select, table, text, union, update
from sqlalchemy.dialects.mysql import CHAR
from sqlalchemy.exc import IntegrityError

from lumen.db import get_session_factory
from lumen.models.chat_db import (
    ChatAgent,
    ChatConversation,
    ChatCustomTool,
    ChatExtensionArchive,
    ChatMcpActivation,
    ChatMcpOAuthConnection,
    ChatMcpServer,
    ChatMessageGraph,
)
from lumen.services.domain_scope import personal_mcp_scope_key, scope_clause
from lumen.services.extension_archive import archive_duplicate, archive_mcp_state
from lumen.services.mcp_domain_authority import project_domain_id

_LOGGER = logging.getLogger(__name__)
_BATCH = 100
_LOCK = "lumen:036:user-domain-scope"
_VERIFIED = table("chat_scope_backfill_origins", column("project_id"), column("domain_id"))
# Production tables mix utf8mb3_general_ci, utf8mb4_general_ci and utf8mb4_unicode_ci identifier
# columns, and fresh servers pick their own defaults. Keystone IDs are exact identifiers, so every
# compared column is converted to one charset first: COLLATE alone cannot change a utf8mb3 column.
_ID_COLLATION = "utf8mb4_bin"


def _exact_id(column):
    """``CAST(column AS CHAR(64) CHARACTER SET utf8mb4) COLLATE utf8mb4_bin``."""
    return cast(column, CHAR(64, charset="utf8mb4")).collate(_ID_COLLATION)


@dataclass
class BackfillReport:
    lock_acquired: bool = False
    projects_resolved: int = 0
    projects_unresolved: int = 0
    items_failed: int = 0
    graphs: int = 0
    conversations: int = 0
    agents: int = 0
    mcp_servers: int = 0
    custom_tools: int = 0
    activations: int = 0
    oauth_connections: int = 0
    archived_mcp_servers: int = 0
    archived_custom_tools: int = 0


def _origins():
    def project(column):
        return _exact_id(column).label("project_id")

    return union(
        select(project(ChatMessageGraph.project_id)).where(ChatMessageGraph.domain_id.is_(None)),
        select(project(ChatAgent.project_id)).where(ChatAgent.domain_id.is_(None)),
        select(project(ChatCustomTool.owner_project_id)).where(
            ChatCustomTool.scope == "user", ChatCustomTool.domain_id.is_(None),
        ),
        select(project(ChatMcpServer.owner_project_id)).where(
            ChatMcpServer.scope == "user", ChatMcpServer.owner_domain_id.is_(None),
        ),
        select(project(ChatMcpActivation.owner_project_id)).where(ChatMcpActivation.owner_domain_id.is_(None)),
        select(project(ChatMcpOAuthConnection.owner_project_id)).join(
            ChatMcpServer, ChatMcpServer.id == ChatMcpOAuthConnection.mcp_server_id,
        ).where(ChatMcpServer.scope != "domain", ChatMcpOAuthConnection.owner_domain_id.is_(None)),
    ).subquery()


def _url_identity(url: str | None) -> tuple | None:
    if not url:
        return None
    try:
        parsed = urlsplit(url.strip())
        scheme = parsed.scheme.lower()
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if scheme not in {"http", "https"} or not host or parsed.username is not None or parsed.password is not None:
        return None
    default_port = 443 if scheme == "https" else 80
    return scheme, host.lower(), port or default_port, parsed.path or "/", parsed.query


def _identity(kind: str, row):
    if kind == "mcp" and row.template_key:
        return "template", row.template_key.casefold()
    url = _url_identity(row.url)
    return ("url", url) if url is not None else None


def _groups(rows, labels_by_id: dict[int, str], domain_column: str) -> list:
    per_project = defaultdict(set)
    assignments = {row.id: row.legacy_domain_group or labels_by_id[row.id] for row in rows}
    for row in rows:
        if getattr(row, domain_column) is None:
            per_project[row.owner_project_id].add(assignments[row.id])
    labels = {label for group in per_project.values() if len(group) > 1 for label in group}
    labels.update(row.legacy_domain_group for row in rows if row.legacy_domain_group)
    if not labels:
        return [(None, rows)]
    groups = {label: [] for label in sorted(labels)}
    unmatched = []
    for row in rows:
        groups.get(assignments[row.id], unmatched).append(row)
    newest = max(groups.values(), key=lambda group: max(map(_rank, group)))
    newest.extend(unmatched)
    return list(groups.items())


def _rank(row):
    return row.updated_at, getattr(row, "id", None) or row.owner_project_id


async def _promote_history(session, project_id: str, domain_id: str, report: BackfillReport) -> None:
    # A NULL graph can only have origin-matching mappings. Invalid historical
    # mappings prevent promotion rather than acquiring new domain visibility.
    graph = ChatMessageGraph
    conversation = ChatConversation
    invalid = exists().where(
        conversation.graph_id == graph.id,
        or_(conversation.user_id != graph.user_id, conversation.project_id != graph.project_id,
            conversation.domain_id.is_not(None)),
    )
    after_id = ""
    while True:
        async with session.begin():
            graph_ids = (await session.scalars(select(graph.id).where(
                graph.project_id == project_id, graph.domain_id.is_(None), graph.id > after_id, ~invalid,
            ).order_by(graph.id).limit(_BATCH))).all()
            if not graph_ids:
                break
            result = await session.execute(update(conversation).where(
                conversation.graph_id.in_(graph_ids), conversation.project_id == project_id, conversation.domain_id.is_(None),
            ).values(domain_id=domain_id, updated_at=conversation.updated_at))
            await session.execute(update(graph).where(graph.id.in_(graph_ids)).values(domain_id=domain_id))
        after_id = graph_ids[-1]
        report.graphs += len(graph_ids)
        report.conversations += result.rowcount

    agent = ChatAgent
    while True:
        async with session.begin():
            ids = (await session.scalars(select(agent.id).where(
                agent.project_id == project_id, agent.domain_id.is_(None),
            ).order_by(agent.id).limit(_BATCH))).all()
            if not ids:
                break
            result = await session.execute(update(agent).where(agent.id.in_(ids), agent.domain_id.is_(None)).values(
                domain_id=domain_id, updated_at=agent.updated_at,
            ))
        report.agents += result.rowcount


async def _rewrite_agent_bindings(session, kind: str, loser, winner_id: int, domain_id: str) -> None:
    agent = ChatAgent
    field = agent.mcp_ids if kind == "mcp" else agent.tool_ids
    after_id = 0
    while True:
        rows = (await session.scalars(select(agent).where(
            agent.owner_user_id == loser.owner_user_id, agent.id > after_id, field.is_not(None),
            scope_clause(agent.domain_id, agent.project_id, domain_id=domain_id, project_id=loser.owner_project_id),
        ).order_by(agent.id).limit(_BATCH).with_for_update())).all()
        if not rows:
            break
        after_id = rows[-1].id
        for row in rows:
            ids = getattr(row, field.key)
            if isinstance(ids, list) and loser.id in ids:
                rewritten = []
                for item in ids:
                    item = winner_id if item == loser.id else item
                    if item not in rewritten:
                        rewritten.append(item)
                await session.execute(update(agent).where(agent.id == row.id).values({
                    field: rewritten, agent.updated_at: agent.updated_at,
                }))


async def _migrate_mcp_state(session, server, model, user_id: str, project_id: str, domain_id: str) -> bool:
    """Collapse one current-origin approval/credential into its domain entry."""
    if server.scope == "user" and (server.owner_user_id != user_id or server.owner_domain_id != domain_id):
        return False
    if server.scope == "domain" and (model is ChatMcpOAuthConnection or server.owner_domain_id != domain_id):
        return False
    rows = (await session.scalars(select(model).where(
        model.mcp_server_id == server.id, model.owner_user_id == user_id,
        scope_clause(model.owner_domain_id, model.owner_project_id, domain_id=domain_id, project_id=project_id),
    ).with_for_update())).all()
    if not any(row.owner_domain_id is None for row in rows):
        return False
    winner = max(rows, key=_rank)
    # Clear the uniqueness slot before assigning it to a newer legacy row.
    for row in rows:
        if row is not winner:
            archive_mcp_state(session, server, row, domain_id)
            await session.delete(row)
    await session.flush()
    values = {"owner_domain_id": domain_id, "updated_at": winner.updated_at}
    if model is ChatMcpActivation and len(rows) > 1:
        values["generation"] = max(row.generation for row in rows) + 1
    if model is ChatMcpOAuthConnection and len(rows) > 1:
        values["credential_version"] = max(row.credential_version for row in rows) + 1
    identity = (model.id == winner.id,) if model is ChatMcpOAuthConnection else (
        model.mcp_server_id == server.id, model.owner_user_id == user_id, model.owner_project_id == winner.owner_project_id,
    )
    await session.execute(update(model).where(*identity).values(**values))
    return True


async def _rewrite_personal_approval(session, archived_activations: list[dict], winner, domain_id: str) -> None:
    if not any(item["owner_user_id"] == winner.owner_user_id and (
        item["owner_domain_id"] == domain_id or item["owner_domain_id"] is None
    ) for item in archived_activations):
        return
    activation = ChatMcpActivation
    if await session.scalar(select(activation.mcp_server_id).where(
        activation.mcp_server_id == winner.id, activation.owner_user_id == winner.owner_user_id,
        scope_clause(activation.owner_domain_id, activation.owner_project_id,
                     domain_id=domain_id, project_id=winner.owner_project_id),
    ).limit(1)) is None:
        # Approval of a different server/config is not consent to the winning
        # config. Keep a moved but inactive entry; the exact original is recoverable.
        session.add(activation(
            mcp_server_id=winner.id, owner_user_id=winner.owner_user_id, owner_project_id=winner.owner_project_id,
            owner_domain_id=domain_id, config_version=winner.config_version, is_active=False,
        ))


async def _promote_extension(session, kind: str, item_id: int, project_id: str, domain_id: str, report: BackfillReport):
    is_mcp = kind == "mcp"
    model = ChatMcpServer if is_mcp else ChatCustomTool
    scope_domain = model.owner_domain_id if is_mcp else model.domain_id
    row = await session.get(model, item_id, with_for_update=True)
    if (row is None or row.scope != "user" or row.owner_project_id != project_id
            or getattr(row, scope_domain.key) is not None or not row.owner_user_id):
        return
    groups = [(None, [row])]
    key = _identity(kind, row)
    if key is not None:
        # Leave intentionally restored records independent on every later run.
        restored = exists().where(
            ChatExtensionArchive.kind == kind, ChatExtensionArchive.original_id == model.id,
            ChatExtensionArchive.entry_type == "extension", ChatExtensionArchive.restored_at.is_not(None),
        )
        label = func.coalesce(model.connection_label, model.name) if is_mcp else model.name
        # Use the same collation as the scoped template/label unique index.
        label_digest = func.sha2(func.weight_string(func.trim(label)), 256)
        query = select(model, label_digest).where(
            model.scope == "user", model.owner_user_id == row.owner_user_id, ~restored,
            or_(scope_domain == domain_id, and_(
                scope_domain.is_(None), _exact_id(model.owner_project_id).in_(
                    select(_VERIFIED.c.project_id).where(_VERIFIED.c.domain_id == domain_id),
                ),
            )),
        )
        templated = key[0] == "template"
        if is_mcp:
            query = query.where(model.template_key == row.template_key if templated else model.template_key.is_(None))
        peers = [
            (peer, digest) for peer, digest in await session.execute(query.order_by(model.id).with_for_update())
            if templated or _identity(kind, peer) == key
        ]
        groups = _groups([peer for peer, _ in peers], {peer.id: digest for peer, digest in peers}, scope_domain.key)
    for group_key, group in groups:
        await _merge_extension_group(session, kind, model, group, group_key, domain_id, report)


async def _merge_extension_group(session, kind, model, peers, group_key, domain_id: str, report: BackfillReport) -> None:
    is_mcp = kind == "mcp"
    winner = max(peers, key=_rank)
    archived_activations = []
    for loser in peers:
        if loser is winner:
            continue
        await _rewrite_agent_bindings(session, kind, loser, winner.id, domain_id)
        archived_activations.extend(await archive_duplicate(session, kind, loser, winner.id, domain_id))
        if is_mcp:
            report.archived_mcp_servers += 1
        else:
            report.archived_custom_tools += 1
    if group_key is not None and winner.legacy_domain_group != group_key:
        await session.execute(update(model).where(model.id == winner.id).values(
            legacy_domain_group=group_key, updated_at=winner.updated_at,
        ))
    if is_mcp:
        if winner.owner_domain_id is None:
            await session.execute(update(model).where(model.id == winner.id).values(
                owner_domain_id=domain_id, connection_scope_key=personal_mcp_scope_key(
                    winner.owner_user_id, winner.owner_project_id, domain_id,
                ), updated_at=winner.updated_at,
            ))
            report.mcp_servers += 1
        for counter, state in (("activations", ChatMcpActivation), ("oauth_connections", ChatMcpOAuthConnection)):
            if await _migrate_mcp_state(session, winner, state, winner.owner_user_id, winner.owner_project_id, domain_id):
                setattr(report, counter, getattr(report, counter) + 1)
        await _rewrite_personal_approval(session, archived_activations, winner, domain_id)
    elif winner.domain_id is None:
        await session.execute(update(model).where(model.id == winner.id).values(
            domain_id=domain_id, updated_at=winner.updated_at,
        ))
        report.custom_tools += 1


async def _promote_extensions(session, project_id: str, domain_id: str, report: BackfillReport) -> None:
    for kind, model, scope_domain in (
        ("mcp", ChatMcpServer, ChatMcpServer.owner_domain_id), ("tool", ChatCustomTool, ChatCustomTool.domain_id),
    ):
        after_id = 0
        while True:
            async with session.begin():
                ids = (await session.scalars(select(model.id).where(
                    model.scope == "user", model.owner_project_id == project_id, scope_domain.is_(None),
                    model.id > after_id,
                ).order_by(model.id).limit(_BATCH))).all()
            if not ids:
                break
            for item_id in ids:
                staged = BackfillReport()
                try:
                    async with session.begin():
                        await _promote_extension(session, kind, item_id, project_id, domain_id, staged)
                except IntegrityError:
                    report.items_failed += 1
                    continue
                for counter in ("mcp_servers", "custom_tools", "activations", "oauth_connections",
                                "archived_mcp_servers", "archived_custom_tools"):
                    setattr(report, counter, getattr(report, counter) + getattr(staged, counter))
            after_id = ids[-1]

    for counter, model in (("activations", ChatMcpActivation), ("oauth_connections", ChatMcpOAuthConnection)):
        after_server, after_user = 0, ""
        while True:
            async with session.begin():
                query = select(model.mcp_server_id, model.owner_user_id).where(
                    model.owner_project_id == project_id, model.owner_domain_id.is_(None),
                    or_(model.mcp_server_id > after_server,
                        and_(model.mcp_server_id == after_server, model.owner_user_id > after_user)),
                ).order_by(model.mcp_server_id, model.owner_user_id).limit(_BATCH)
                rows = (await session.execute(query)).all()
            if not rows:
                break
            for server_id, user_id in rows:
                try:
                    async with session.begin():
                        server = await session.get(ChatMcpServer, server_id, with_for_update=True)
                        changed = server is not None and await _migrate_mcp_state(
                            session, server, model, user_id, project_id, domain_id,
                        )
                except IntegrityError:
                    report.items_failed += 1
                    continue
                if changed:
                    setattr(report, counter, getattr(report, counter) + 1)
            after_server, after_user = rows[-1]


async def backfill_domain_scope(
    *, resolve: Callable[[str], Awaitable[str]] = project_domain_id,
) -> BackfillReport:
    """Use one MariaDB connection for the advisory lock and bounded DB transactions."""
    report = BackfillReport()
    factory = get_session_factory()
    if factory is None:
        return report
    engine = factory.kw["bind"]
    async with engine.connect() as connection:
        report.lock_acquired = (await connection.scalar(text("SELECT GET_LOCK(:name, 0)"), {"name": _LOCK})) == 1
        await connection.commit()
        if not report.lock_acquired:
            return report
        try:
            await connection.exec_driver_sql("""
                CREATE TEMPORARY TABLE chat_scope_backfill_origins (
                    project_id VARCHAR(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin PRIMARY KEY,
                    domain_id VARCHAR(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
                    KEY idx_domain (domain_id, project_id)
                ) ENGINE=InnoDB
            """)
            await connection.commit()
            origins = _origins()
            after_project = ""
            async with factory(bind=connection) as session:
                while True:
                    async with session.begin():
                        projects = (await session.scalars(select(origins.c.project_id).where(
                            origins.c.project_id > after_project,
                        ).order_by(origins.c.project_id).limit(_BATCH))).all()
                    if not projects:
                        break
                    for project_id in projects:
                        try:
                            domain_id = await resolve(project_id)
                            if not isinstance(domain_id, str) or not domain_id or len(domain_id) > 64:
                                raise ValueError("Invalid Keystone domain identity")
                        except Exception:
                            report.projects_unresolved += 1
                            continue
                        report.projects_resolved += 1
                        async with session.begin():
                            await session.execute(_VERIFIED.insert().values(project_id=project_id, domain_id=domain_id))
                        await _promote_history(session, project_id, domain_id, report)
                    after_project = projects[-1]
                after_project = ""
                while True:
                    async with session.begin():
                        resolved = (await session.execute(
                            select(_VERIFIED.c.project_id, _VERIFIED.c.domain_id).where(
                                _VERIFIED.c.project_id > after_project,
                            ).order_by(_VERIFIED.c.project_id).limit(_BATCH)
                        )).all()
                    if not resolved:
                        break
                    for project_id, domain_id in resolved:
                        await _promote_extensions(session, project_id, domain_id, report)
                    after_project = resolved[-1].project_id
        finally:
            try:
                await connection.exec_driver_sql("DROP TEMPORARY TABLE IF EXISTS chat_scope_backfill_origins")
                await connection.scalar(text("SELECT RELEASE_LOCK(:name)"), {"name": _LOCK})
            except BaseException:
                await connection.invalidate()
                raise
    return report


async def startup_domain_scope_backfill() -> None:
    """Backfill independently of readiness; unresolved rows remain project-only."""
    try:
        report = await backfill_domain_scope()
    except Exception as exc:
        # SQL exceptions can include bound parameters (including archive ciphertext).
        _LOGGER.error("Domain scope backfill stopped (%s); rerun the admin command", type(exc).__name__)
        return
    _LOGGER.info("Domain scope backfill: %s", asdict(report))
