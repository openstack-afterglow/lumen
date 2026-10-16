"""Opt-in synthetic persistence for direct inference unit tests, never authority bypasses."""
from types import SimpleNamespace

import pytest

from lumen.models.chat_runs import ChatRun
from lumen.services import api_key_store, inference_authority


@pytest.fixture
def synthetic_inference_store(monkeypatch, current_project_authority):
    """Keep the real scope/key/owner helper; substitute only its DB and directory I/O.

    Direct compatibility tests use u1/p1/key 7. Non-durable tool tests have no key
    row lookup; their current project authority still resolves through the shared
    opt-in directory fixture. Tests may mutate the returned key/run rows.
    """
    key = SimpleNamespace(id=7, owner_user_id="u1", owner_project_id="p1", is_active=True,
                          revoked_at=None, expires_at=None, scopes=list(api_key_store.API_KEY_SCOPES))
    run = SimpleNamespace(user_id="u1", project_id="p1", api_key_id=7, source="web",
                          run_scope="persistent", conversation_id="c1")

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
