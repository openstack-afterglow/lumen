"""Real MariaDB coverage for active-path revisions and one-time Gateway keys."""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from lumen.db import close_db, get_session_factory, init_db
from lumen.models.chat_db import ChatApiKey, ChatGatewayDeviceGrant
from lumen.services import claude_gateway as gateway
from lumen.services import conversation_store as conversations

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
