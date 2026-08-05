from types import SimpleNamespace

import pytest

from lumen.services import memory_outbox
from lumen.services.memory_outbox import mark_generation_applied


def test_generation_application_completes_only_after_required_set_is_drained():
    row = SimpleNamespace(
        required_generations=[1, 2],
        applied_generations=[],
        status="running",
        lease_owner="worker",
        lease_expires_at=object(),
    )

    assert mark_generation_applied(row, generation=1) is False
    assert row.applied_generations == [1]
    assert row.status == "running"

    assert mark_generation_applied(row, generation=2) is True
    assert row.applied_generations == [1, 2]
    assert row.status == "completed"
    assert row.lease_owner is None
    assert row.lease_expires_at is None


def test_generation_application_rejects_unrequired_generation():
    row = SimpleNamespace(required_generations=[1], applied_generations=[], status="running")

    with pytest.raises(ValueError, match="not required"):
        mark_generation_applied(row, generation=2)


@pytest.mark.asyncio
async def test_apply_claimed_skips_superseded_plaintext_hash(monkeypatch):
    class Session:
        async def get(self, model, memory_id):
            return SimpleNamespace(
                id=memory_id,
                user_id="user",
                project_id="project",
                workspace_id=None,
                status="active",
                content="ciphertext",
            )

    class Index:
        async def upsert(self, vector):
            raise AssertionError("superseded content must not be indexed")

    row = SimpleNamespace(
        memory_id=7,
        mutation="upsert",
        content_hash="a" * 64,
        required_generations=[1],
        applied_generations=[],
        status="running",
        lease_owner="worker",
        lease_expires_at=object(),
    )
    monkeypatch.setattr(memory_outbox, "configured_memory_index", lambda: Index())
    monkeypatch.setattr(memory_outbox, "decrypt_chat_content", lambda value: "newer plaintext")

    assert await memory_outbox.apply_claimed(Session(), row) is True
    assert row.status == "completed"


@pytest.mark.asyncio
async def test_vector_receives_keyed_fingerprint_not_plaintext_digest(monkeypatch):
    captured = []

    class Index:
        async def upsert(self, vector):
            captured.append(vector)

    claim = memory_outbox.ClaimedMemoryMutation(
        change_seq=1,
        memory_id=7,
        mutation="upsert",
        content_hash="h" * 64,
        required_generations=(1,),
        plaintext="yes",
        user_id="user",
        project_id="project",
        workspace_id=None,
        active=True,
    )
    monkeypatch.setattr(memory_outbox, "configured_memory_index", lambda: Index())
    monkeypatch.setattr(memory_outbox, "memory_content_fingerprint", lambda _plaintext: "h" * 64)
    monkeypatch.setattr(memory_outbox, "embed_maintenance", lambda _plaintext: _return([1.0, 0.0]))
    monkeypatch.setattr(
        memory_outbox,
        "get_settings",
        lambda: SimpleNamespace(chat_memory_embedding_model="memory-embedding"),
    )

    assert await memory_outbox._apply_snapshot(claim) == {1}
    assert captured[0].content_hash == "h" * 64


@pytest.mark.asyncio
async def test_claim_one_releases_mysql_transaction_before_external_work(monkeypatch):
    state = {"transaction_exited": False}

    class Transaction:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            state["transaction_exited"] = True

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def begin(self):
            return Transaction()

        async def get(self, _model, _memory_id):
            return SimpleNamespace(
                id=7,
                user_id="user",
                project_id="project",
                workspace_id=None,
                status="active",
                is_active=True,
                content="ciphertext",
            )

    row = SimpleNamespace(
        change_seq=3,
        memory_id=7,
        mutation="upsert",
        content_hash="a" * 64,
        required_generations=[1],
    )
    session = Session()
    monkeypatch.setattr(memory_outbox, "get_session_factory", lambda: lambda: session)
    monkeypatch.setattr(
        memory_outbox, "configured_memory_index", lambda: SimpleNamespace(required_generations=lambda: _return([1]))
    )
    monkeypatch.setattr(memory_outbox, "activate_pending_generations", lambda *_args, **_kwargs: _return(0))
    monkeypatch.setattr(memory_outbox, "claim_next", lambda *_args, **_kwargs: _return(row))
    monkeypatch.setattr(memory_outbox, "decrypt_chat_content", lambda _value: "plaintext")

    claim = await memory_outbox.claim_one(owner="worker")

    assert claim is not None
    assert state["transaction_exited"] is True


async def _return(value):
    return value
