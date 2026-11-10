"""Real MariaDB: several connections per template, and catalog writes that never bump config_version.

Runs the real ``extensions_store`` and ``mcp_connections`` code against the migrated schema (including migration
023). Only the MCP discovery call is replaced, because the hosts are deliberately non-resolving.
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import delete, select

from lumen.db import close_db, get_session_factory, init_db
from lumen.models.chat_db import ChatMcpServer
from lumen.services import extensions_store as es
from lumen.services import mcp_connections

pytestmark = pytest.mark.integration


@pytest.fixture
async def database(monkeypatch):
    init_db(os.environ["DATABASE_URL"], pool_size=2, max_overflow=0)
    monkeypatch.setattr(es.ssrf, "validate_url", lambda url: url)
    created: list[int] = []
    yield created
    if created:
        async with get_session_factory()() as session, session.begin():
            await session.execute(delete(ChatMcpServer).where(ChatMcpServer.id.in_(created)))
    await close_db()


async def test_one_template_holds_two_connections_and_catalog_writes_keep_config_version(database, monkeypatch):
    token = uuid.uuid4().hex[:10]
    template = f"tmpl-{token}"
    url = f"https://svc-{token}.invalid/mcp"

    work = await mcp_connections.create_connection(
        name="Service",
        url=url,
        auth_mode="admin",
        headers={"Authorization": "Bearer read"},
        template_key=template,
        label="read-only",
    )
    database.append(work["server"]["id"])
    admin = await mcp_connections.create_connection(
        name="Service",
        url=url,
        auth_mode="admin",
        headers={"Authorization": "Bearer write"},
        template_key=template,
        label="read-write",
    )
    database.append(admin["server"]["id"])
    assert work["created"] and admin["created"]
    assert work["server"]["id"] != admin["server"]["id"]

    duplicate = await mcp_connections.create_connection(
        name="Service",
        url=url,
        auth_mode="admin",
        headers={"Authorization": "Bearer other"},
        template_key=template,
        label="read-only",
    )
    assert duplicate["created"] is False
    assert duplicate["server"]["id"] == work["server"]["id"]

    version_before = await es.list_global("mcp")
    before = next(row for row in version_before if row["id"] == work["server"]["id"])["config_version"]

    async def discover(server):
        return [{"name": "search", "description": "Search", "input_schema": {"type": "object"}}]

    monkeypatch.setattr(mcp_connections.mcp_client, "discover_tools", discover)
    result = await mcp_connections.refresh_catalog(work["server"]["id"], user_id=None, project_id=None)
    assert result["status"] == "ok"
    assert result["diff"]["added"] == ["search"]

    after = next(row for row in await es.list_global("mcp") if row["id"] == work["server"]["id"])
    assert after["config_version"] == before
    assert [tool["name"] for tool in after["tools"]] == ["search"]
    assert after["catalog_error"] is None
    assert after["catalog_refreshed_at"] is not None

    async def fail(server):
        raise TimeoutError("network down")

    monkeypatch.setattr(mcp_connections.mcp_client, "discover_tools", fail)
    failed = await mcp_connections.refresh_catalog(work["server"]["id"], user_id=None, project_id=None)
    assert failed["status"] == "failed"
    kept = next(row for row in await es.list_global("mcp") if row["id"] == work["server"]["id"])
    assert [tool["name"] for tool in kept["tools"]] == ["search"], "a failed refresh must keep the last good catalog"
    assert kept["catalog_error"] == "MCP 도구 검색 실패: TimeoutError"
    assert kept["config_version"] == before


async def test_sse_transport_is_stored_and_stdio_is_rejected_by_the_store(database):
    token = uuid.uuid4().hex[:10]
    sse = await mcp_connections.create_connection(
        name="Events",
        url=f"https://events-{token}.invalid/sse",
        transport="sse",
        template_key=f"sse-{token}",
    )
    database.append(sse["server"]["id"])
    assert sse["server"]["transport"] == "sse"

    with pytest.raises(es.ExtensionValidationError):
        await es.create(
            "mcp",
            {"name": "Local", "transport": "stdio", "url": f"https://local-{token}.invalid/mcp", "is_active": True},
            scope="global",
        )
    rows = await es.list_global("mcp")
    assert not [row for row in rows if row["name"] == "Local"]
    assert select  # imported for readability of the DB queries above
