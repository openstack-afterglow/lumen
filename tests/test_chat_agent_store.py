"""Store-level project-isolation regressions for chat agents.

These tests exercise the SQLAlchemy predicates emitted by the store rather than
only asserting router-to-store argument forwarding.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from lumen.services import agent_store as ags


class _Result:
    def __init__(self, value=None):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback):
        return False


class _Session:
    def __init__(self):
        self.statements = []
        self.deleted = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback):
        return False

    def begin(self):
        return _Transaction()

    async def execute(self, statement):
        self.statements.append(statement)
        return _Result()

    async def delete(self, row):
        self.deleted.append(row)

    def add(self, row):
        self.added = row

    async def flush(self):
        return None


def _scope_store(monkeypatch, session: _Session) -> None:
    monkeypatch.setattr(ags, "_require_db", lambda: lambda: session)


def _statement_sql(session: _Session) -> str:
    assert len(session.statements) == 1
    return str(session.statements[0].compile(compile_kwargs={"literal_binds": True}))


@pytest.mark.parametrize("operation", ["get", "update", "delete"])
async def test_owned_agent_operations_reject_same_user_other_project(monkeypatch, operation):
    """Private rows from another project cannot be read or mutated by their owner."""
    session = _Session()
    _scope_store(monkeypatch, session)

    with pytest.raises(ags.AgentNotFound):
        if operation == "get":
            await ags.get_agent(41, user_id="user-1", project_id="project-current")
        elif operation == "update":
            await ags.update_agent(41, user_id="user-1", project_id="project-current", patch={"name": "new"})
        else:
            await ags.delete_agent(41, user_id="user-1", project_id="project-current")

    sql = _statement_sql(session)
    assert "chat_agents.id = 41" in sql
    assert "chat_agents.owner_user_id = 'user-1'" in sql
    assert "chat_agents.project_id = 'project-current'" in sql
    assert session.deleted == []


async def test_clone_rejects_same_user_private_agent_from_another_project(monkeypatch):
    session = _Session()
    _scope_store(monkeypatch, session)

    with pytest.raises(ags.AgentNotFound):
        await ags.clone_agent(41, user_id="user-1", project_id="project-current")

    sql = _statement_sql(session)
    assert "chat_agents.id = 41" in sql
    assert "chat_agents.visibility = 'public'" in sql
    assert "chat_agents.owner_user_id = 'user-1'" in sql
    assert "chat_agents.project_id = 'project-current'" in sql


async def test_clone_rejects_inactive_public_template(monkeypatch):
    session = _Session()
    _scope_store(monkeypatch, session)

    with pytest.raises(ags.AgentNotFound):
        await ags.clone_agent(41, user_id="user-1", project_id="project-current")

    sql = _statement_sql(session)
    assert "chat_agents.id = 41" in sql
    assert "chat_agents.is_active IS true" in sql


async def test_owned_legacy_template_can_only_be_cloned_into_the_current_project(monkeypatch):
    source = SimpleNamespace(
        id=41,
        owner_user_id="user-1",
        name="legacy template",
        description=None,
        avatar=None,
        instructions=None,
        model_name=None,
        params=None,
        is_active=True,
        visibility="private",
    )

    class LegacyCloneSession(_Session):
        async def execute(self, statement):
            self.statements.append(statement)
            return _Result(source)

    session = LegacyCloneSession()
    _scope_store(monkeypatch, session)

    clone = await ags.clone_agent(41, user_id="user-1", project_id="project-current")

    sql = _statement_sql(session)
    assert "chat_agents.project_id IS NULL" in sql
    assert clone["owner_user_id"] == "user-1"
    assert clone["cloned_from_id"] == 41
    assert session.added.project_id == "project-current"
    assert session.added.mcp_ids is None
    assert session.added.tool_ids is None


async def test_legacy_template_is_not_executable_until_cloned(monkeypatch):
    session = _Session()
    _scope_store(monkeypatch, session)

    assert await ags.get_agent_for_run(41, user_id="user-1", project_id="project-current") is None

    sql = _statement_sql(session)
    assert "chat_agents.project_id = 'project-current'" in sql
    assert "chat_agents.is_active IS true" in sql


async def test_hub_marks_same_user_different_project_template_as_not_owned(monkeypatch):
    source = SimpleNamespace(
        id=41,
        owner_user_id="user-1",
        project_id="project-other",
        name="other project template",
        description=None,
        avatar=None,
        instructions=None,
        model_name=None,
        params=None,
        mcp_ids=None,
        tool_ids=None,
        visibility="public",
        cloned_from_id=None,
        clone_count=0,
        created_at=None,
        updated_at=None,
    )

    class PublicRowsSession(_Session):
        async def execute(self, statement):
            self.statements.append(statement)
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [source]))

    session = PublicRowsSession()
    _scope_store(monkeypatch, session)

    hub = await ags.list_public(user_id="user-1", project_id="project-current")

    assert hub[0]["id"] == 41
    assert "chat_agents.is_active IS true" in _statement_sql(session)
    assert hub[0]["is_owner"] is False


async def test_same_user_can_clone_public_template_from_another_project(monkeypatch):
    source = SimpleNamespace(
        id=41,
        owner_user_id="user-1",
        project_id="project-other",
        name="other project template",
        description=None,
        avatar=None,
        instructions=None,
        model_name=None,
        params=None,
        is_active=True,
        visibility="public",
    )

    class PublicCloneSession(_Session):
        async def execute(self, statement):
            self.statements.append(statement)
            return _Result(source)

    session = PublicCloneSession()
    _scope_store(monkeypatch, session)

    clone = await ags.clone_agent(41, user_id="user-1", project_id="project-current")

    assert clone["cloned_from_id"] == 41
    assert session.added.project_id == "project-current"
