"""Real MariaDB: a registry-installed MCP source is opt-in in actual chat admission.

Uses the real ``extensions_store.create`` path and the real ``chat_admission`` selection. The DNS policy is
replaced because the hosts are deliberately non-resolving; unit tests cover URL validation.
``install_all`` is not called here, because it would write the whole shipped catalog into the shared database.
"""

from __future__ import annotations

import os
import uuid

import pytest
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


async def test_registry_source_is_opt_in_against_real_admission(database, monkeypatch):
    token = uuid.uuid4().hex[:10]
    registry_url = f"https://registry-{token}.invalid/mcp"
    bundle_url = f"https://bundle-{token}.invalid/mcp"
    entry = {
        "id": f"reg-{token}",
        "registry_name": f"org/reg-{token}",
        "name": f"Registry {token}",
        "description": "",
        "url": registry_url,
        "transport": "http",
        "variables": [],
        "header_hints": [],
        "docs_url": "",
        "access": "open",
    }
    monkeypatch.setattr(catalog, "_load", lambda: (entry,))

    installed = await catalog.install(entry["id"])
    assert installed["created"] is True
    registry_id = installed["server"]["id"]
    database.append(registry_id)
    assert installed["server"]["implicit_selection"] is False

    again = await catalog.install(entry["id"])
    assert again["created"] is False
    assert again["server"]["id"] == registry_id

    bundle = await es.create(
        "mcp",
        {
            "name": f"Bundle {token}",
            "transport": "http",
            "url": bundle_url,
            "auth_mode": "none",
            "load_policy": "on_demand",
            "is_active": True,
        },
        scope="global",
    )
    database.append(bundle["id"])
    assert bundle["implicit_selection"] is True

    user, project = f"u-{token}", f"p-{token}"
    implicit = await chat_admission._resolve_extension_selection(
        None, ChatFeatureOptions(), user_id=user, project_id=project
    )
    implicit_ids = {item["id"] for item in implicit["mcp"]}
    assert bundle["id"] in implicit_ids
    assert registry_id not in implicit_ids

    named = await chat_admission._resolve_extension_selection(
        None,
        ChatFeatureOptions(tool_policy={"enabled_mcp_ids": [registry_id]}),
        user_id=user,
        project_id=project,
    )
    assert [item["id"] for item in named["mcp"]] == [registry_id]
