"""Concurrent native text admissions by one user on one provider: no deadlock, no lost request, one run per key.

Text admission runs READ COMMITTED (the gap lock of a new idempotency key's ``SELECT ... FOR UPDATE`` used to
deadlock racing admissions, MariaDB 1213 surfaced as HTTP 500). The unique ``uq_chat_runs_idempotency`` index decides
a same-key race and the loser replays the winner's run.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from decimal import Decimal

import pytest
from provider_fixtures import configured_provider
from sqlalchemy import delete, event, func, select, text

from lumen import crypto
from lumen.db import close_db, get_session_factory, init_db
from lumen.models.chat_db import ChatConversation, ChatMessage, ChatMessageGraph, LlmModel, LlmProvider
from lumen.models.chat_runs import ChatRun
from lumen.services import conversation_store as cs
from lumen.services.durable_runs import admission
from lumen.services.durable_runs.errors import DurableRunConflict, DurableRunNotFound
from lumen.services.message_graph import projected_message_ids
from lumen.services.providers import routing
from lumen.services.providers.credentials import execution_identity

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("current_project_authority")]

_REQUESTS = 12
_MODEL = "gpt-text-fixture"
_PAYLOAD = {"features": {}, "input_parts": [{"type": "text", "text": "hello"}]}


@pytest.fixture
async def text_route(request, monkeypatch):
    from sqlalchemy.pool import Pool

    monkeypatch.setattr(crypto, "_get_key", lambda: bytes.fromhex("ab" * 32))

    async def no_wake(_run_id):
        return None

    monkeypatch.setattr(admission, "wake_run", no_wake)

    def pin_isolation(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute(f"SET SESSION innodb_snapshot_isolation={request.param}")
        cursor.close()

    event.listen(Pool, "connect", pin_isolation)
    request.addfinalizer(lambda: event.remove(Pool, "connect", pin_isolation))
    init_db(os.environ["DATABASE_URL"], pool_size=_REQUESTS + 2, max_overflow=0)
    factory = get_session_factory()
    nonce = uuid.uuid4().hex
    user_id, project_id = f"text-user-{nonce}", f"text-project-{nonce}"
    provider_id = None
    try:
        async with factory() as session, session.begin():
            provider = configured_provider(name=f"text-provider-{nonce}", provider_type="openai", is_active=True,
                                           margin_multiplier=Decimal("1"))
            session.add(provider)
            await session.flush()
            provider_id = provider.id
            model = LlmModel(provider_id=provider_id, model_name=_MODEL, model_kind="text", is_active=True)
            session.add(model)
            await session.flush()
            model_id = model.id
        async with factory() as session:  # hash exactly as the admission lock resolves it, from committed rows
            provider = (await session.execute(select(LlmProvider).where(LlmProvider.id == provider_id))).scalar_one()
            model = (await session.execute(select(LlmModel).where(LlmModel.id == model_id))).scalar_one()
            resolved = routing._resolved_model(model, provider, ref=None)
            snapshot = {
                "model_name": _MODEL, "provider_id": provider_id, "model_id": model_id,
                "provider_name": provider.name, "model_label": _MODEL,
                "config_version_hash": resolved["config_version_hash"], **execution_identity(resolved),
            }
        yield factory, user_id, project_id, snapshot
    finally:
        async with factory() as session, session.begin():
            await session.execute(text("SET FOREIGN_KEY_CHECKS=0"))
            owned = "SELECT id FROM chat_runs WHERE project_id = :p"
            for table in ("chat_run_events", "chat_run_providers"):
                await session.execute(text(f"DELETE FROM {table} WHERE run_id IN ({owned})"), {"p": project_id})
            for table in ("chat_conversation_active_path", "chat_conversation_messages", "chat_run_assets"):
                column = "conversation_id" if table.startswith("chat_conversation") else "run_id"
                subject = "SELECT id FROM chat_conversations WHERE project_id = :p" if column == "conversation_id" else owned
                await session.execute(text(f"DELETE FROM {table} WHERE {column} IN ({subject})"), {"p": project_id})
            await session.execute(text("DELETE FROM chat_runs WHERE project_id = :p"), {"p": project_id})
            await session.execute(text("DELETE FROM chat_temp_threads WHERE project_id = :p"), {"p": project_id})
            await session.execute(text("DELETE FROM chat_messages WHERE graph_id IN "
                                       "(SELECT id FROM chat_message_graphs WHERE project_id = :p)"), {"p": project_id})
            await session.execute(delete(ChatConversation).where(ChatConversation.project_id == project_id))
            await session.execute(delete(ChatMessageGraph).where(ChatMessageGraph.project_id == project_id))
            if provider_id is not None:
                await session.execute(delete(LlmModel).where(LlmModel.provider_id == provider_id))
                await session.execute(delete(LlmProvider).where(LlmProvider.id == provider_id))
            await session.execute(text("SET FOREIGN_KEY_CHECKS=1"))
        await close_db()


async def _conversations(factory, project_id, user_id, count):
    ids = [str(uuid.uuid4()) for _ in range(count)]
    async with factory() as session, session.begin():
        for conversation_id in ids:
            session.add(ChatMessageGraph(id=conversation_id, project_id=project_id, user_id=user_id))
        await session.flush()
        for conversation_id in ids:
            session.add(ChatConversation(id=conversation_id, graph_id=conversation_id, project_id=project_id,
                                         user_id=user_id, history_index_ready=True))
    return ids


def _admit_temp(snapshot, project_id, user_id):
    async def admit(key, _conversation_id):
        return (await admission.create_temp_run(
            project_id=project_id, user_id=user_id, client_request_id=key, intent={"prompt": "hello"},
            temp_thread_id=None, model_name=_MODEL, request_payload=dict(_PAYLOAD),
            capability_snapshot=snapshot, pricing_snapshot={})).run_id
    return admit


def _admit_persistent(snapshot, project_id, user_id):
    async def admit(key, conversation_id):
        return (await admission.create_persistent_run(
            project_id=project_id, user_id=user_id, client_request_id=key, intent={"prompt": "hello"},
            conversation_id=conversation_id, model_name=_MODEL, agent_id=None, user_content="hello",
            user_parts=[{"type": "text", "text": "hello"}], request_payload=dict(_PAYLOAD),
            capability_snapshot=snapshot, pricing_snapshot={})).run_id
    return admit


def _admit_run(snapshot, project_id, user_id):
    async def admit(key, conversation_id):
        return (await admission.create_run(
            project_id=project_id, user_id=user_id, client_request_id=key, intent={"prompt": "hello"},
            conversation_id=conversation_id, temp_thread_id=None, model_name=_MODEL, agent_id=None,
            user_message_id=None, request_payload=dict(_PAYLOAD), capability_snapshot=snapshot,
            pricing_snapshot={})).run_id
    return admit


@pytest.mark.parametrize("text_route", ["OFF", "ON"], indirect=True)
@pytest.mark.parametrize("same_key", [False, True], ids=["distinct-keys", "one-key"])
@pytest.mark.parametrize("admitter", [_admit_temp, _admit_persistent, _admit_run], ids=["temp", "persistent", "run"])
async def test_concurrent_text_admissions_all_succeed_and_one_key_is_one_run(text_route, admitter, same_key):
    factory, user_id, project_id, snapshot = text_route
    admit = admitter(snapshot, project_id, user_id)
    needs_conversation = admitter is not _admit_temp
    # One key on one conversation races the same row; distinct keys use one conversation each, like real clients.
    count = 1 if same_key else _REQUESTS
    conversations = await _conversations(factory, project_id, user_id, count) if needs_conversation else [None] * count
    shared_key = str(uuid.uuid4())
    results = await asyncio.gather(*(
        admit(shared_key if same_key else str(uuid.uuid4()), conversations[0 if same_key else index])
        for index in range(_REQUESTS)), return_exceptions=True)
    failures = [r for r in results if isinstance(r, BaseException)]
    assert not failures, f"{len(failures)} of {_REQUESTS} failed: {failures[0]!r} caused by {failures[0].__cause__!r}"
    async with factory() as session:
        rows = await session.scalar(select(func.count()).select_from(ChatRun).where(ChatRun.project_id == project_id))
    expected = 1 if same_key else _REQUESTS
    assert rows == expected and len(set(results)) == expected


@pytest.mark.parametrize("text_route", ["OFF", "ON"], indirect=True)
@pytest.mark.parametrize("source_status", ["failed", "canceled"])
@pytest.mark.parametrize("partial_answer", [False, True], ids=["user-leaf", "assistant-leaf"])
async def test_retry_reselects_saved_user_parent_and_completion_projects_a_sibling(
    text_route, source_status, partial_answer,
):
    factory, user_id, project_id, snapshot = text_route
    conversation_id = (await _conversations(factory, project_id, user_id, 1))[0]
    source_id = await _admit_persistent(snapshot, project_id, user_id)(str(uuid.uuid4()), conversation_id)
    async with factory() as session, session.begin():
        source = await session.get(ChatRun, source_id)
        source.status = source_status
        user_message_id = source.user_message_id
    original_answer = None
    if partial_answer:
        original_answer = await cs.add_message(
            conversation_id, role="assistant", content="interrupted", status=source_status,
            parent_id=user_message_id, set_leaf=True,
        )
    async with factory() as session:
        conversation = await session.get(ChatConversation, conversation_id)
        observed_leaf = conversation.active_leaf_id
        original_revision = conversation.history_revision
    retry_arguments = {
        "project_id": project_id, "user_id": user_id, "client_request_id": str(uuid.uuid4()),
        "intent": {"endpoint": "retry", "conversation_id": conversation_id, "source_run_id": source_id},
        "conversation_id": conversation_id, "temp_thread_id": None, "model_name": _MODEL, "agent_id": None,
        "user_message_id": user_message_id, "request_payload": dict(_PAYLOAD),
        "capability_snapshot": snapshot, "pricing_snapshot": {}, "expected_parent_id": observed_leaf,
    }
    retry = await admission.create_run(**retry_arguments)
    expected_revision = original_revision + int(partial_answer)
    async with factory() as session:
        conversation = await session.get(ChatConversation, conversation_id)
        run = await session.get(ChatRun, retry.run_id)
        assert run.user_message_id == conversation.active_leaf_id == user_message_id
        assert conversation.history_revision == expected_revision
        assert await projected_message_ids(session, conversation_id=conversation_id) == [user_message_id]
        assert await session.scalar(select(func.count()).select_from(ChatMessage).where(
            ChatMessage.conversation_id == conversation_id, ChatMessage.role == "user",
        )) == 1
        assert (await session.get(ChatRun, source_id)).status == source_status

    answer = await cs.add_message(
        conversation_id, role="assistant", content="", status="streaming", parent_id=user_message_id,
    )
    async with factory() as session, session.begin():
        await cs.complete_message_in_transaction(
            session, conversation_id, message_id=answer["id"], content="retried answer",
            reasoning=None, model_name=_MODEL,
        )
        run = await session.get(ChatRun, retry.run_id)
        run.status = "completed"
    # A lost-response replay must return the admitted run without reselecting its parent.
    replay = await admission.create_run(**retry_arguments)
    assert replay.run_id == retry.run_id
    assert replay.status == "completed"
    async with factory() as session:
        conversation = await session.get(ChatConversation, conversation_id)
        assert conversation.active_leaf_id == answer["id"]
        assert conversation.history_revision == expected_revision
        assert await projected_message_ids(session, conversation_id=conversation_id) == [user_message_id, answer["id"]]
        if original_answer is not None:
            previous = await session.get(ChatMessage, original_answer["id"])
            assert previous.parent_id == user_message_id
            assert previous.status == source_status


@pytest.mark.parametrize("text_route", ["OFF", "ON"], indirect=True)
@pytest.mark.parametrize("conflict", ["branch", "active", "provider", "unreachable"])
async def test_retry_rejection_does_not_reselect_saved_user_parent(text_route, conflict):
    factory, user_id, project_id, snapshot = text_route
    conversation_id = (await _conversations(factory, project_id, user_id, 1))[0]
    source_id = await _admit_persistent(snapshot, project_id, user_id)(str(uuid.uuid4()), conversation_id)
    async with factory() as session, session.begin():
        source = await session.get(ChatRun, source_id)
        source.status = "failed"
        user_message_id = source.user_message_id
    answer = await cs.add_message(
        conversation_id, role="assistant", content="failed answer", status="failed",
        parent_id=user_message_id, set_leaf=True,
    )
    expected_parent_id = answer["id"]
    capability_snapshot = snapshot
    retry_user_message_id = user_message_id
    if conflict == "branch":
        await cs.set_active_leaf(
            conversation_id, user_id=user_id, project_id=project_id, message_id=user_message_id,
        )
        error = "context_revision_changed"
    elif conflict == "active":
        async with factory() as session, session.begin():
            source = await session.get(ChatRun, source_id)
            source.status = "queued"
        error = "conversation_run_active"
    elif conflict == "provider":
        capability_snapshot = {**snapshot, "config_version_hash": "stale"}
        error = "provider_configuration_changed"
    else:
        other_id = (await _conversations(factory, project_id, user_id, 1))[0]
        other_user = await cs.add_message(other_id, role="user", content="outside conversation")
        retry_user_message_id = other_user["id"]
        error = "user message was not found in conversation"
    async with factory() as session:
        conversation = await session.get(ChatConversation, conversation_id)
        before = (conversation.active_leaf_id, conversation.history_revision)
        before_path = await projected_message_ids(session, conversation_id=conversation_id)
    error_type = DurableRunNotFound if conflict == "unreachable" else DurableRunConflict
    with pytest.raises(error_type, match=error):
        await admission.create_run(
            project_id=project_id, user_id=user_id, client_request_id=str(uuid.uuid4()),
            intent={"endpoint": "retry", "source_run_id": source_id},
            conversation_id=conversation_id, temp_thread_id=None, model_name=_MODEL, agent_id=None,
            user_message_id=retry_user_message_id, request_payload=dict(_PAYLOAD),
            capability_snapshot=capability_snapshot, pricing_snapshot={}, expected_parent_id=expected_parent_id,
        )
    async with factory() as session:
        conversation = await session.get(ChatConversation, conversation_id)
        assert (conversation.active_leaf_id, conversation.history_revision) == before
        assert await projected_message_ids(session, conversation_id=conversation_id) == before_path
        assert await session.scalar(select(func.count()).select_from(ChatRun).where(
            ChatRun.conversation_id == conversation_id,
        )) == 1
