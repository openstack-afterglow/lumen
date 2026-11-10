"""Real MariaDB: a registry-installed source is published globally but runs only after owner activation.

Uses the real ``mcp_registry_catalog.install`` path, the real ``extensions_store`` rows and migrated schema, and
the real ``chat_admission`` selection. The DNS policy is replaced because the hosts are deliberately non-resolving.
``install_all`` is not called, because it would write the whole shipped catalog into the shared database.
"""

from __future__ import annotations

import os
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import delete

from lumen.db import close_db, get_session_factory, init_db
from lumen.models.chat_contracts import ChatFeatureOptions
from lumen.models.chat_db import ChatMcpServer
from lumen.services import chat_admission
from lumen.services import extensions_store as es
from lumen.services import mcp_registry_catalog as catalog

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


async def test_registry_source_runs_only_after_owner_activation(database, monkeypatch):
    token = uuid.uuid4().hex[:10]
    entry = {
        "id": f"reg-{token}",
        "registry_name": f"org/reg-{token}",
        "name": f"Registry {token}",
        "description": "",
        "url": f"https://registry-{token}.invalid/mcp",
        "transport": "http",
        "variables": [],
        "header_hints": [],
        "docs_url": "",
        "access": "open",
    }
    monkeypatch.setattr(catalog, "_load", lambda: (entry,))

    installed = await catalog.install(entry["id"])
    assert installed["created"] is True
    server = installed["server"]
    database.append(server["id"])
    again = await catalog.install(entry["id"])
    assert again["created"] is False and again["server"]["id"] == server["id"]

    user, project = f"u-{token}", f"p-{token}"
    before = await chat_admission._resolve_extension_selection(
        None, ChatFeatureOptions(), user_id=user, project_id=project
    )
    assert server["id"] not in {item["id"] for item in before["mcp"]}, "publication is not owner consent"

    with pytest.raises(HTTPException):
        await chat_admission._resolve_extension_selection(
            None,
            ChatFeatureOptions(tool_policy={"enabled_mcp_ids": [server["id"]]}),
            user_id=user,
            project_id=project,
        )

    await es.set_mcp_activation(
        server["id"], user_id=user, project_id=project, is_active=True, config_version=server["config_version"]
    )
    after = await chat_admission._resolve_extension_selection(
        None, ChatFeatureOptions(), user_id=user, project_id=project
    )
    assert server["id"] in {item["id"] for item in after["mcp"]}
