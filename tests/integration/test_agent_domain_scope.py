"""MariaDB regressions for same-user agent scope and legacy origin isolation."""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import delete

from lumen.db import close_db, get_session_factory, init_db
from lumen.models.chat_db import ChatAgent
from lumen.services import agent_store as ags

pytestmark = pytest.mark.integration


@pytest.fixture
async def agent_scope():
    init_db(os.environ["DATABASE_URL"], pool_size=2, max_overflow=1)
    factory = get_session_factory()
    assert factory is not None
    user = f"agent-scope-{uuid.uuid4().hex}"
    try:
        yield factory, user
    finally:
        async with factory() as session, session.begin():
            await session.execute(delete(ChatAgent).where(ChatAgent.owner_user_id == user))
        await close_db()


async def test_same_domain_agent_crud_and_use_keep_selected_project_and_legacy_origin(agent_scope, monkeypatch):
    factory, user = agent_scope
    origin = {"project_id": "origin", "domain_id": "domain-a"}
    sibling = {"project_id": "sibling", "domain_id": "domain-a"}
    domain_agent = await ags.create_agent(owner_user_id=user, name="Domain agent", **origin)
    legacy_origin = await ags.create_agent(owner_user_id=user, name="Legacy origin", project_id="origin")
    legacy_sibling = await ags.create_agent(owner_user_id=user, name="Legacy sibling", project_id="sibling")
    agent_id = domain_agent["id"]
    async with factory() as session, session.begin():
        originless = ChatAgent(owner_user_id=user, name="Unresolved origin")
        session.add(originless)
        await session.flush()
        originless_id = originless.id

    async def listed(**scope):
        return {row["id"] for row in await ags.list_agents(user_id=user, **scope)}

    assert await listed(**sibling) == {agent_id, legacy_sibling["id"]}
    assert await listed(**origin) == {agent_id, legacy_origin["id"]}
    assert await listed(project_id="origin") == {legacy_origin["id"]}
    assert await listed(project_id="origin", domain_id="domain-b") == {legacy_origin["id"]}
    assert await ags.list_agents(user_id="other-user", **sibling) == []
    assert (await ags.get_agent(agent_id, user_id=user, **sibling))["is_owner"] is True
    assert (await ags.get_agent_for_run(agent_id, user_id=user, **sibling))["id"] == agent_id
    binding_id = str(uuid.uuid4())

    async def resolve_binding(identifier, *, kind, namespace):
        assert (identifier, kind) == (binding_id, "tool")
        assert (namespace.user_id, namespace.project_id) == (user, "sibling")

    monkeypatch.setattr(ags, "resolve_binding", resolve_binding)
    updated = await ags.update_agent(
        agent_id, user_id=user, patch={"name": "Renamed", "plugin_tool_ids": [binding_id]}, **sibling,
    )
    assert updated["name"] == "Renamed"
    clone = await ags.clone_agent(agent_id, user_id=user, **sibling)
    assert clone["cloned_from_id"] == agent_id
    assert clone["plugin_tool_ids"] == []
    async with factory() as session:
        source = await session.get(ChatAgent, agent_id)
        copy = await session.get(ChatAgent, clone["id"])
        assert (source.project_id, source.domain_id) == ("origin", "domain-a")
        assert (copy.project_id, copy.domain_id) == ("sibling", "domain-a")

    for rejected_id, scope in (
        (agent_id, {"user_id": user, "project_id": "origin", "domain_id": "domain-b"}),
        (agent_id, {"user_id": user, "project_id": "origin"}),
        (legacy_origin["id"], {"user_id": user, **sibling}),
        (originless_id, {"user_id": user, **sibling}),
        (agent_id, {"user_id": "other-user", **sibling}),
    ):
        with pytest.raises(ags.AgentNotFound):
            await ags.get_agent(rejected_id, **scope)
        with pytest.raises(ags.AgentNotFound):
            await ags.update_agent(rejected_id, patch={"name": "Wrong scope"}, **scope)
        with pytest.raises(ags.AgentNotFound):
            await ags.delete_agent(rejected_id, **scope)
        with pytest.raises(ags.AgentNotFound):
            await ags.clone_agent(rejected_id, **scope)
        assert await ags.get_agent_for_run(rejected_id, **scope) is None

    await ags.delete_agent(agent_id, user_id=user, **sibling)
    assert await ags.get_agent_for_run(agent_id, user_id=user, **origin) is None
