"""Real MariaDB coverage for active-path revisions and one-time Gateway keys."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from lumen.crypto import decrypt_chat_content, encrypt_chat_content
from lumen.db import close_db, get_session_factory, init_db
from lumen.models.chat_assets import ChatAsset, ChatMessageAsset
from lumen.models.chat_db import (
    ChatApiKey,
    ChatConversation,
    ChatConversationMessage,
    ChatGatewayDeviceGrant,
    ChatMessage,
    ChatMessageGraph,
)
from lumen.models.chat_jobs import ChatJob
from lumen.models.chat_runs import ChatRun
from lumen.services import claude_gateway as gateway
from lumen.services import context_store, title_jobs
from lumen.services import conversation_store as conversations
from lumen.services.durable_runs import execution
from lumen.services.message_graph import MessageGraphError
from lumen.services.run_store import replay_events

pytestmark = pytest.mark.integration


async def test_active_path_page_revision_fences_branch_switches() -> None:
    init_db(os.environ["DATABASE_URL"], pool_size=1, max_overflow=0)
    nonce = uuid.uuid4().hex
    user_id = f"history-user-{nonce}"
    project_id = f"history-project-{nonce}"

    try:
        conversation = await conversations.create_conversation(
            project_id=project_id,
            user_id=user_id,
            title="Revision fence",
            model_name="integration-model",
        )
        conversation_id = conversation["id"]
        user = await conversations.add_message(
            conversation_id,
            role="user",
            content="choose a branch",
            parent_id=None,
            set_leaf=True,
        )
        first = await conversations.add_message(
            conversation_id,
            role="assistant",
            content="first answer",
            parent_id=user["id"],
            set_leaf=True,
        )
        alternate = await conversations.add_message(
            conversation_id,
            role="assistant",
            content="alternate answer",
            parent_id=user["id"],
            set_leaf=False,
        )

        first_page = await conversations.list_message_page(
            conversation_id,
            user_id=user_id,
            project_id=project_id,
            anchor="latest",
            limit=40,
        )
        assert [item["id"] for item in first_page["messages"]] == [user["id"], first["id"]]
        assert first_page["messages"][-1]["branch"]["next_id"] == alternate["id"]
        initial_revision = first_page["history_revision"]

        switched = await conversations.set_active_leaf(
            conversation_id,
            user_id=user_id,
            project_id=project_id,
            message_id=alternate["id"],
        )
        assert switched["active_leaf_id"] == alternate["id"]
        assert switched["history_revision"] == initial_revision + 1

        with pytest.raises(conversations.HistoryRevisionChanged):
            await conversations.list_message_page(
                conversation_id,
                user_id=user_id,
                project_id=project_id,
                cursor_direction="before",
                cursor_position=1,
                expected_revision=initial_revision,
                limit=40,
            )

        current_page = await conversations.list_message_page(
            conversation_id,
            user_id=user_id,
            project_id=project_id,
            anchor="latest",
            limit=40,
        )
        assert [item["id"] for item in current_page["messages"]] == [user["id"], alternate["id"]]
    finally:
        await close_db()


@pytest.mark.parametrize("delete_source", [False, True])
async def test_shared_ancestor_ids_survive_either_conversation_deletion(delete_source: bool) -> None:
    init_db(os.environ["DATABASE_URL"], pool_size=1, max_overflow=0)
    nonce = uuid.uuid4().hex
    owner = {"user_id": f"shared-user-{nonce}", "project_id": f"shared-project-{nonce}"}
    try:
        source = await conversations.create_conversation(title=None, model_name=None, **owner)
        user = await conversations.add_message(source["id"], role="user", content="shared question", set_leaf=True)
        answer = await conversations.add_message(
            source["id"], role="assistant", content="shared answer", parent_id=user["id"], set_leaf=True,
        )
        source_only = await conversations.add_message(
            source["id"], role="user", content="source only turn", parent_id=answer["id"], set_leaf=False,
        )
        factory = get_session_factory()
        assert factory is not None
        asset_id = str(uuid.uuid4())
        async with factory() as session, session.begin():
            session.add(ChatAsset(
                id=asset_id, **owner, object_key=f"graph-lifetime/{nonce}", original_name="shared.txt",
                mime_type="text/plain", size_bytes=6, sha256="a" * 64, status="ready",
            ))
            await session.flush()
            session.add(ChatMessageAsset(message_id=user["id"], asset_id=asset_id, part_index=0))
        fork = await conversations.fork_conversation(source["id"], message_id=answer["id"], **owner)
        for conversation_id in (source["id"], fork["id"]):
            page = await conversations.list_message_page(conversation_id, **owner)
            assert [item["id"] for item in page["messages"]] == [user["id"], answer["id"]]
            assert {item["conversation_id"] for item in page["messages"]} == {conversation_id}
        await conversations.set_active_leaf(source["id"], message_id=user["id"], **owner)
        with pytest.raises(MessageGraphError):
            await conversations.complete_message(
                source["id"], message_id=answer["id"], content="rewritten shared answer",
                reasoning=None, model_name="integration-model",
            )
        await conversations.set_active_leaf(source["id"], message_id=answer["id"], **owner)

        deleted, survivor = (source, fork) if delete_source else (fork, source)
        await conversations.delete_conversation(deleted["id"], **owner)
        with pytest.raises(conversations.ConversationNotFound):
            await conversations.get_conversation(deleted["id"], **owner)
        async with factory() as session:
            survivor_conv = await session.get(ChatConversation, survivor["id"])
            assert survivor_conv is not None
            graph_id = survivor_conv.graph_id
            assert await session.get(ChatMessageGraph, graph_id) is not None
            assert await session.get(ChatMessage, user["id"]) is not None
            assert await session.get(ChatMessage, answer["id"]) is not None
            assert await session.get(ChatMessage, source_only["id"]) is not None
            assert await session.get(ChatMessageAsset, (user["id"], asset_id)) is not None
        inherited = await context_store.load_context_source(
            conversation_id=survivor["id"], temp_thread_id=None, **owner,
        )
        assert inherited["message_ids"] == [str(user["id"]), str(answer["id"])]
        assert inherited["messages"] == [
            {"role": "user", "content": "shared question"},
            {"role": "assistant", "content": "shared answer"},
        ]
        appended = await conversations.add_message(
            survivor["id"], role="user", content="continue independently", parent_id=answer["id"], set_leaf=True,
        )
        page = await conversations.list_message_page(survivor["id"], **owner)
        assert [item["id"] for item in page["messages"]] == [user["id"], answer["id"], appended["id"]]
        updated = await context_store.load_context_source(
            conversation_id=survivor["id"], temp_thread_id=None, **owner,
        )
        assert updated["revision"] != inherited["revision"]
        await conversations.delete_conversation(survivor["id"], **owner)
        async with factory() as session:
            assert await session.get(ChatConversation, survivor["id"]) is None
            assert await session.get(ChatMessageGraph, graph_id) is None
            for mid in (user["id"], answer["id"], source_only["id"], appended["id"]):
                assert await session.get(ChatMessage, mid) is None
            mappings = await session.scalar(
                select(ChatConversationMessage.message_id)
                .where(
                    ChatConversationMessage.message_id.in_([user["id"], answer["id"], source_only["id"], appended["id"]])
                )
                .limit(1)
            )
            assert mappings is None
            assert await session.get(ChatMessageAsset, (user["id"], asset_id)) is None
            # Asset storage is separately owner-managed; graph cleanup drops links,
            # not unrelated reusable owner uploads.
            assert await session.get(ChatAsset, asset_id) is not None
    finally:
        await close_db()


async def test_fork_branches_cursors_and_context_are_reachability_scoped() -> None:
    init_db(os.environ["DATABASE_URL"], pool_size=1, max_overflow=0)
    nonce = uuid.uuid4().hex
    owner = {"user_id": f"branch-user-{nonce}", "project_id": f"branch-project-{nonce}"}
    try:
        source = await conversations.create_conversation(title=None, model_name=None, **owner)
        user = await conversations.add_message(source["id"], role="user", content="question", set_leaf=True)
        answer = await conversations.add_message(
            source["id"], role="assistant", content="inherited answer", parent_id=user["id"], set_leaf=True,
        )
        source_sibling = await conversations.add_message(
            source["id"], role="assistant", content="source only", parent_id=user["id"], set_leaf=False,
        )
        fork = await conversations.fork_conversation(source["id"], message_id=answer["id"], **owner)
        fork_sibling = await conversations.add_message(
            fork["id"], role="assistant", content="fork only", parent_id=user["id"], set_leaf=False,
        )
        for conversation_id, sibling in ((source["id"], source_sibling), (fork["id"], fork_sibling)):
            page = await conversations.list_message_page(conversation_id, anchor="latest", limit=1, **owner)
            assert [item["id"] for item in page["messages"]] == [answer["id"]]
            assert page["messages"][0]["branch"]["next_id"] == sibling["id"]
            assert page["has_before"] is True
            assert page["has_after"] is False
            before = await conversations.list_message_page(
                conversation_id, cursor_direction="before", cursor_position=page["before_position"],
                expected_revision=page["history_revision"], limit=1, **owner,
            )
            assert [item["id"] for item in before["messages"]] == [user["id"]]
            after = await conversations.list_message_page(
                conversation_id, cursor_direction="after", cursor_position=before["after_position"],
                expected_revision=page["history_revision"], limit=1, **owner,
            )
            assert [item["id"] for item in after["messages"]] == [answer["id"]]

        with pytest.raises(conversations.ConversationNotFound):
            await conversations.set_active_leaf(fork["id"], message_id=source_sibling["id"], **owner)
        with pytest.raises(conversations.ConversationNotFound):
            await conversations.get_message_owned(fork["id"], message_id=source_sibling["id"], **owner)
        with pytest.raises(ValueError):
            await context_store.load_context_source(
                conversation_id=fork["id"], temp_thread_id=None, leaf_id=source_sibling["id"], **owner,
            )
        old = await conversations.list_message_page(fork["id"], **owner)
        await conversations.set_active_leaf(fork["id"], message_id=fork_sibling["id"], **owner)
        with pytest.raises(conversations.HistoryRevisionChanged):
            await conversations.list_message_page(
                fork["id"], cursor_direction="before", cursor_position=1,
                expected_revision=old["history_revision"], **owner,
            )
        explicit = await context_store.load_context_source(
            conversation_id=fork["id"], temp_thread_id=None, leaf_id=answer["id"], **owner,
        )
        active = await context_store.load_context_source(conversation_id=fork["id"], temp_thread_id=None, **owner)
        assert explicit["messages"][-1]["content"] == "inherited answer"
        assert active["messages"][-1]["content"] == "fork only"
        assert explicit["revision"] != active["revision"]
        source_page = await conversations.list_message_page(source["id"], **owner)
        assert source_page["active_leaf_id"] == answer["id"]
        for denied in (
            {**owner, "user_id": "different-user"},
            {**owner, "project_id": "different-project"},
        ):
            with pytest.raises(conversations.ConversationForbidden):
                await conversations.list_message_page(fork["id"], **denied)
            with pytest.raises(conversations.ConversationForbidden):
                await conversations.fork_conversation(fork["id"], message_id=user["id"], **denied)
            with pytest.raises(conversations.ConversationForbidden):
                await context_store.load_context_source(conversation_id=fork["id"], temp_thread_id=None, **denied)
        await conversations.delete_conversation(fork["id"], **owner)
        await conversations.delete_conversation(source["id"], **owner)
    finally:
        await close_db()


async def test_regenerated_run_replays_registered_turns_from_an_inherited_user() -> None:
    init_db(os.environ["DATABASE_URL"], pool_size=1, max_overflow=0)
    nonce = uuid.uuid4().hex
    owner = {"user_id": f"resume-user-{nonce}", "project_id": f"resume-project-{nonce}"}
    run_id = str(uuid.uuid4())
    try:
        source = await conversations.create_conversation(title=None, model_name=None, **owner)
        user = await conversations.add_message(source["id"], role="user", content="regenerate me", set_leaf=True)
        fork = await conversations.fork_conversation(source["id"], message_id=user["id"], **owner)
        await conversations.delete_conversation(source["id"], **owner)
        factory = get_session_factory()
        assert factory is not None
        async with factory() as session, session.begin():
            await session.get(ChatConversation, fork["id"], with_for_update=True)
            run = ChatRun(
                id=run_id, run_scope="persistent", conversation_id=fork["id"], user_message_id=user["id"],
                model_name="integration-model", capability_snapshot={}, pricing_snapshot={},
                request_payload=encrypt_chat_content("{}"),
                client_request_id=str(uuid.uuid4()), request_fingerprint=nonce, fingerprint_version=1,
                execution_protocol_version=2, status="running", **owner,
            )
            session.add(run)
            await session.flush()
            hooks = execution._DurableExecutionHooks(run_id=run_id, owner="integration-worker")
            await hooks._ensure_provider_turn(session, run, 0)
            first_id = run.assistant_message_id
        replayed = await conversations.list_messages_for_run(run_id, **owner)
        assert [item["id"] for item in replayed] == [user["id"], first_id]
        assert [item["status"] for item in replayed] == ["complete", "streaming"]
        assert {item["conversation_id"] for item in replayed} == {fork["id"]}

        async with factory() as session, session.begin():
            await session.get(ChatConversation, fork["id"], with_for_update=True)
            run = await session.get(ChatRun, run_id, with_for_update=True)
            resumed = execution._DurableExecutionHooks(run_id=run_id, owner="resumed-worker")
            await resumed._ensure_provider_turn(session, run, 0)
            assert run.assistant_message_id == first_id
            await conversations.complete_message_in_transaction(
                session, fork["id"], message_id=first_id, content="first turn", reasoning=None,
                model_name="integration-model",
            )
            await resumed._ensure_provider_turn(session, run, 1)
            second_id = run.assistant_message_id
            await conversations.complete_message_in_transaction(
                session, fork["id"], message_id=second_id, content="final turn", reasoning=None,
                model_name="integration-model",
            )
            run.status = "completed"
            created = [event for event in await replay_events(session, run, after_seq=0) if event.type == "message.created"]
            assert [event.payload.model_dump()["message_id"] for event in created] == [str(first_id), str(second_id)]
        page = await conversations.list_message_page(fork["id"], **owner)
        assert [item["id"] for item in page["messages"]] == [user["id"], first_id, second_id]
        assert [item["parent_id"] for item in page["messages"]] == [None, user["id"], first_id]
        assert await conversations.list_messages_for_run(run_id, **{**owner, "user_id": "not-owner"}) == []
        await conversations.delete_conversation(fork["id"], **owner)
    finally:
        await close_db()

async def test_fork_title_first_exchange_enqueues_with_inherited_user_and_shared_graph() -> None:
    init_db(os.environ["DATABASE_URL"], pool_size=1, max_overflow=0)
    nonce = uuid.uuid4().hex
    owner = {"user_id": f"title-user-{nonce}", "project_id": f"title-project-{nonce}"}
    run_id = str(uuid.uuid4())
    try:
        source = await conversations.create_conversation(title=None, model_name=None, **owner)
        user = await conversations.add_message(
            source["id"], role="user", content="inherited title question", set_leaf=True,
        )
        fork = await conversations.fork_conversation(source["id"], message_id=user["id"], **owner)
        factory = get_session_factory()
        assert factory is not None
        summary_route = {
            "provider_id": 1,
            "provider_name": "openai",
            "model_id": 1,
            "model_name": "title-model",
            "config_version_hash": "cfg-1",
        }
        async with factory() as session, session.begin():
            await session.get(ChatConversation, fork["id"], with_for_update=True)
            run = ChatRun(
                id=run_id,
                run_scope="persistent",
                conversation_id=fork["id"],
                user_message_id=user["id"],
                model_name="integration-model",
                capability_snapshot={"summary_route": summary_route},
                pricing_snapshot={"title-model": {"input_price": 0.001, "output_price": 0.002}},
                request_payload=encrypt_chat_content("{}"),
                client_request_id=str(uuid.uuid4()),
                request_fingerprint=nonce,
                fingerprint_version=1,
                execution_protocol_version=2,
                status="running",
                **owner,
            )
            session.add(run)
            await session.flush()
            hooks = execution._DurableExecutionHooks(run_id=run_id, owner="title-worker")
            await hooks._ensure_provider_turn(session, run, 0)
            assistant_id = run.assistant_message_id
            await conversations.complete_message_in_transaction(
                session, fork["id"], message_id=assistant_id, content="fork answer for title", reasoning=None,
                model_name="integration-model",
            )
            run.status = "completed"
            enqueued = await title_jobs.enqueue_completed_run_in_transaction(session, run)
            assert enqueued is True
            job = (
                await session.execute(
                    select(ChatJob).where(ChatJob.idempotency_key == f"title:first:{fork['id']}")
                )
            ).scalar_one()
            payload = json.loads(decrypt_chat_content(job.payload))
            assert payload["conversation_id"] == fork["id"]
            assert payload["exchange"] == [
                {"role": "user", "content": "inherited title question"},
                {"role": "assistant", "content": "fork answer for title"},
            ]
        fork_conv = await conversations.get_conversation(fork["id"], **owner)
        assert fork_conv["title_status"] == "pending"
        await conversations.delete_conversation(fork["id"], **owner)
        await conversations.delete_conversation(source["id"], **owner)
    finally:
        await close_db()

async def test_concurrent_deletion_of_last_two_conversations_cleans_graph_safely() -> None:
    init_db(os.environ["DATABASE_URL"], pool_size=3, max_overflow=1)
    nonce = uuid.uuid4().hex
    owner = {"user_id": f"concurrent-user-{nonce}", "project_id": f"concurrent-project-{nonce}"}
    try:
        source = await conversations.create_conversation(title=None, model_name=None, **owner)
        user = await conversations.add_message(source["id"], role="user", content="concurrent question", set_leaf=True)
        answer = await conversations.add_message(
            source["id"], role="assistant", content="concurrent answer", parent_id=user["id"], set_leaf=True,
        )
        fork = await conversations.fork_conversation(source["id"], message_id=answer["id"], **owner)
        factory = get_session_factory()
        assert factory is not None
        async with factory() as session:
            source_conv = await session.get(ChatConversation, source["id"])
            fork_conv = await session.get(ChatConversation, fork["id"])
            assert source_conv is not None and fork_conv is not None
            graph_id = source_conv.graph_id
            assert fork_conv.graph_id == graph_id

        await asyncio.gather(
            conversations.delete_conversation(source["id"], **owner),
            conversations.delete_conversation(fork["id"], **owner),
        )

        async with factory() as session:
            assert await session.get(ChatConversation, source["id"]) is None
            assert await session.get(ChatConversation, fork["id"]) is None
            assert await session.get(ChatMessageGraph, graph_id) is None
            assert await session.get(ChatMessage, user["id"]) is None
            assert await session.get(ChatMessage, answer["id"]) is None
    finally:
        await close_db()

async def test_concurrent_fork_and_delete_source_resolves_cleanly() -> None:
    init_db(os.environ["DATABASE_URL"], pool_size=3, max_overflow=1)
    nonce = uuid.uuid4().hex
    owner = {"user_id": f"contend-user-{nonce}", "project_id": f"contend-project-{nonce}"}
    try:
        source = await conversations.create_conversation(title=None, model_name=None, **owner)
        user = await conversations.add_message(source["id"], role="user", content="contention question", set_leaf=True)
        answer = await conversations.add_message(
            source["id"], role="assistant", content="contention answer", parent_id=user["id"], set_leaf=True,
        )

        fork_res = None
        delete_res = None

        async def do_fork():
            nonlocal fork_res
            try:
                fork_res = await conversations.fork_conversation(source["id"], message_id=answer["id"], **owner)
            except conversations.ConversationNotFound as exc:
                fork_res = exc

        async def do_delete():
            nonlocal delete_res
            try:
                delete_res = await conversations.delete_conversation(source["id"], **owner)
            except Exception as exc:
                delete_res = exc

        await asyncio.gather(do_fork(), do_delete())

        factory = get_session_factory()
        assert factory is not None
        if isinstance(fork_res, dict):
            # Fork acquired lock first: fork succeeded and preserved shared messages; delete then removed source
            assert delete_res is None
            fork_page = await conversations.list_message_page(fork_res["id"], **owner)
            assert [item["id"] for item in fork_page["messages"]] == [user["id"], answer["id"]]
            await conversations.delete_conversation(fork_res["id"], **owner)
        else:
            # Delete acquired lock first: fork cleanly raised ConversationNotFound without ORM errors
            assert isinstance(fork_res, conversations.ConversationNotFound)
            assert delete_res is None
            async with factory() as session:
                assert await session.get(ChatMessage, user["id"]) is None
                assert await session.get(ChatMessage, answer["id"]) is None
    finally:
        await close_db()


async def test_gateway_device_exchange_mints_one_expiring_hashed_key(monkeypatch) -> None:
    init_db(os.environ["DATABASE_URL"], pool_size=1, max_overflow=0)
    monkeypatch.setattr(
        gateway,
        "get_settings",
        lambda: SimpleNamespace(
            claude_gateway_base_url="https://lumen.example/v1/claude-gateway",
            frontend_base_url="https://afterglow.example",
        ),
    )

    try:
        issued = await gateway.create_device_grant(client_id="claude-code", scope=gateway.GATEWAY_SCOPE)
        assert issued["verification_uri"] == "https://afterglow.example/oauth/claude/authorize"

        approved = await gateway.authorize_user_code(
            user_code=issued["user_code"],
            approve=True,
            owner_user_id="gateway-user",
            owner_project_id="gateway-project",
        )
        assert approved == {"status": "approved"}

        with pytest.raises(gateway.GatewayError) as early_poll:
            await gateway.exchange_device_code(
                device_code=issued["device_code"],
                grant_type=gateway.DEVICE_GRANT_TYPE,
                client_id="claude-code",
            )
        assert early_poll.value.code == "slow_down"

        factory = get_session_factory()
        assert factory is not None
        async with factory() as session, session.begin():
            grant_id = (
                await session.execute(
                    select(ChatGatewayDeviceGrant.id).where(
                        ChatGatewayDeviceGrant.device_code_hash == gateway._hash(issued["device_code"])
                    )
                )
            ).scalar_one()
            grant = await session.get(ChatGatewayDeviceGrant, grant_id)
            assert grant is not None
            grant.next_poll_at = datetime.now(UTC) - timedelta(seconds=1)

        token = await gateway.exchange_device_code(
            device_code=issued["device_code"],
            grant_type=gateway.DEVICE_GRANT_TYPE,
            client_id="claude-code",
        )
        assert token["access_token"].startswith("sk-afgl-")
        assert token["expires_in"] == 24 * 60 * 60
        assert token["scope"] == gateway.GATEWAY_SCOPE
        assert "refresh_token" not in token

        async with factory() as session:
            grant_id = (
                await session.execute(
                    select(ChatGatewayDeviceGrant.id).where(
                        ChatGatewayDeviceGrant.device_code_hash == gateway._hash(issued["device_code"])
                    )
                )
            ).scalar_one()
            grant = await session.get(ChatGatewayDeviceGrant, grant_id)
            assert grant is not None and grant.status == "consumed"
            key = await session.get(ChatApiKey, grant.issued_api_key_id)
            assert key is not None
            assert key.credential_kind == "claude_gateway"
            assert key.key_hash != token["access_token"]
            assert key.expires_at is not None

        with pytest.raises(gateway.GatewayError) as replay:
            await gateway.exchange_device_code(
                device_code=issued["device_code"],
                grant_type=gateway.DEVICE_GRANT_TYPE,
                client_id="claude-code",
            )
        assert replay.value.code == "invalid_grant"
    finally:
        await close_db()
