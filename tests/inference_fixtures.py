"""Opt-in synthetic persistence for direct inference unit tests, never authority bypasses."""
from types import SimpleNamespace

import pytest

from lumen.models.chat_runs import ChatRun
from lumen.services import api_key_store, inference_authority


@pytest.fixture
def synthetic_inference_store(monkeypatch, current_project_authority):
    """Keep the real scope/key/owner helper; substitute only its DB and directory I/O.

    Direct compatibility tests use u1/p1/key 7. Key 7 is a personal Claude Gateway
    key: the one key class whose identity stays bound to a real project, so the
    shared current project authority remains its live owner authority. The run is a
    dashboard run; model keys never authorize tools. Non-durable tool tests have no
    key row lookup. Tests may mutate the returned key/run rows.
    """
    key = SimpleNamespace(id=7, key_kind="personal", credential_kind="claude_gateway", owner_user_id="u1",
                          owner_project_id="p1", owner_domain_id=None, created_by_user_id="u1", is_active=True,
                          revoked_at=None, expires_at=None, scopes=list(api_key_store.API_KEY_SCOPES))
    run = SimpleNamespace(user_id="u1", project_id="p1", api_key_id=None, source="web",
                          run_scope="persistent", conversation_id="c1", capability_snapshot={})

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def begin(self):
            return self

        async def execute(self, *args, **kwargs):
            return SimpleNamespace(scalar_one_or_none=lambda: key, scalar_one=lambda: key)

        async def get(self, model, identifier, **kwargs):
            return run if model is ChatRun else key

    monkeypatch.setattr(inference_authority, "get_session_factory", lambda: Session)
    return SimpleNamespace(key=key, run=run, authority=current_project_authority)
