"""Real MariaDB proof that Claude Gateway /v1/messages takes a bounded user-wallet hold."""
from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import delete, select

from lumen import auth
from lumen.api import claude_gateway as gateway_api
from lumen.db import close_db, get_session_factory, init_db
from lumen.models.billing import Payer
from lumen.models.chat_db import ChatUsageLog, UserWallet
from lumen.models.stateless_reservations import StatelessCallReservation
from lumen.services import claude_gateway, completion_api

pytestmark = pytest.mark.integration

_RESPONSE = {
    "id": "msg_gateway", "type": "message", "role": "assistant", "model": "claude-gateway-fixture",
    "content": [{"type": "text", "text": "done"}], "stop_reason": "end_turn",
    "usage": {"input_tokens": 5, "output_tokens": 50},
}


@pytest.fixture
async def gateway_owner(monkeypatch):
    init_db(os.environ["DATABASE_URL"], pool_size=8, max_overflow=0)
    factory = get_session_factory()
    user, project = f"gateway-{uuid.uuid4().hex}", "gateway-project"
    async with factory() as session, session.begin():
        session.add(UserWallet(user_id=user, project_id=project, max_quota_monthly=Decimal("1000"),
                               quota_period_start=date.today().replace(day=1)))
    route = {
        "model_name": "claude-gateway-fixture", "api_model_name": "claude-gateway-fixture",
        "provider_type": "anthropic", "provider_name": "fixture-anthropic", "api_base": "https://fixture.invalid",
        "margin_multiplier": Decimal(1), "price_source": "manual",
        # 1 credit per input token and 2 per output token (chat_credit_per_usd = 1).
        "input_price_per_token": Decimal("1"), "output_price_per_token": Decimal("2"),
        "capabilities": {"context_limit": 10, "output_budget_enforced": True},
    }
    monkeypatch.setattr(completion_api.get_settings(), "chat_credit_per_usd", Decimal(1))
    monkeypatch.setattr(claude_gateway, "configured_route", lambda: ("claude-gateway-fixture", "anthropic"))
    monkeypatch.setattr(completion_api, "resolve_api", AsyncMock(return_value=route))
    monkeypatch.setattr(completion_api, "_authorize_completion", AsyncMock())
    monkeypatch.setattr(completion_api, "_with_passthrough_compaction", lambda options, **_kwargs: options)
    app = FastAPI()
    app.include_router(gateway_api.public_router, prefix="/v1/claude-gateway")
    app.dependency_overrides[auth.get_principal] = lambda: {
        "auth_type": "api_key", "credential_class": "claude_gateway", "credential_kind": "claude_gateway",
        "user_id": user, "project_id": project, "api_key_id": None, "payer": Payer("user", user, None),
        "scopes": claude_gateway.GATEWAY_SCOPES, "expires_at": datetime.now(UTC) + timedelta(hours=1),
        "source": "api", "roles": ["member"], "is_system_admin": False,
    }
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            yield factory, user, client
    finally:
        async with factory() as session, session.begin():
            await session.execute(delete(StatelessCallReservation).where(StatelessCallReservation.user_id == user))
            await session.execute(delete(ChatUsageLog).where(ChatUsageLog.user_id == user))
            await session.execute(delete(UserWallet).where(UserWallet.user_id == user))
        await close_db()


def _message(max_tokens: int) -> dict:
    return {"model": "client-alias-is-ignored", "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": "gateway billing contract"}]}


async def test_gateway_message_holds_and_settles_once(gateway_owner, monkeypatch):
    factory, user, client = gateway_owner
    upstream = AsyncMock(return_value=dict(_RESPONSE))
    monkeypatch.setattr(completion_api.provider_client, "aanthropic_messages", upstream)
    response = await client.post("/v1/claude-gateway/v1/messages", json=_message(64))
    assert response.status_code == 200, response.text
    upstream.assert_awaited_once()
    async with factory() as session:
        hold = (await session.scalars(select(StatelessCallReservation).where(
            StatelessCallReservation.user_id == user))).one()
        ledger = (await session.scalars(select(ChatUsageLog).where(ChatUsageLog.user_id == user))).one()
        wallet = await session.get(UserWallet, user)
    # The full context window at the input rate plus the output budget at the output rate.
    assert hold.bound_credits >= Decimal(10 * 1 + 64 * 2)
    assert hold.status == "settled" and hold.event_id == ledger.event_id
    assert hold.payer_user_id == user and hold.payer_domain_id is None
    assert ledger.credited_cost == hold.actual_credits == Decimal(5 * 1 + 50 * 2)
    assert wallet.used_quota_this_month == ledger.credited_cost


@pytest.mark.parametrize("complete", [True, False])
async def test_streaming_gateway_message_settles_once_or_stays_unknown(gateway_owner, monkeypatch, complete):
    factory, user, client = gateway_owner

    async def upstream(**kwargs):
        assert kwargs["stream"] is True

        async def events():
            yield {"type": "message_start", "message": {"usage": {"input_tokens": 5, "output_tokens": 0}}}
            yield {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "done"}}
            yield {"type": "message_delta", "usage": {"output_tokens": 50}}
            if not complete:
                raise ConnectionError("upstream stream cut before message_stop")
            yield {"type": "message_stop"}

        return events()

    monkeypatch.setattr(completion_api.provider_client, "aanthropic_messages", upstream)
    response = await client.post("/v1/claude-gateway/v1/messages", json={**_message(64), "stream": True})
    assert response.status_code == 200, response.text
    assert "event: message_start" in response.text
    assert ("event: message_stop" in response.text) is complete
    async with factory() as session:
        hold = (await session.scalars(select(StatelessCallReservation).where(
            StatelessCallReservation.user_id == user))).one()
        ledger = (await session.scalars(select(ChatUsageLog).where(ChatUsageLog.user_id == user))).all()
        wallet = await session.get(UserWallet, user)
    if complete:
        assert hold.status == "settled" and [row.event_id for row in ledger] == [hold.event_id]
        assert ledger[0].credited_cost == hold.actual_credits == wallet.used_quota_this_month == Decimal(5 + 50 * 2)
    else:
        # Dispatched but without terminal usage: the full bound stays held, nothing is charged.
        assert hold.status == "unknown" and ledger == []
        assert wallet.used_quota_this_month == 0


async def test_concurrent_gateway_messages_beyond_budget_are_refused_before_dispatch(gateway_owner, monkeypatch):
    factory, user, client = gateway_owner
    async with factory() as session, session.begin():
        # Room for exactly one bound (10 input + 40 output tokens -> 90 credits) but not two.
        (await session.get(UserWallet, user)).max_quota_monthly = Decimal("150")
    entered, release = asyncio.Event(), asyncio.Event()

    async def held_upstream(**_kwargs):
        entered.set()
        await release.wait()
        return dict(_RESPONSE, usage={"input_tokens": 5, "output_tokens": 5})

    upstream = AsyncMock(side_effect=held_upstream)
    monkeypatch.setattr(completion_api.provider_client, "aanthropic_messages", upstream)
    first = asyncio.create_task(client.post("/v1/claude-gateway/v1/messages", json=_message(40)))
    await asyncio.wait_for(entered.wait(), 5)
    # The first call's hold is committed while its provider call is in flight.
    refused = await asyncio.wait_for(client.post("/v1/claude-gateway/v1/messages", json=_message(40)), 5)
    assert refused.status_code == 429, refused.text
    assert upstream.await_count == 1
    release.set()
    assert (await first).status_code == 200
    async with factory() as session:
        holds = (await session.scalars(select(StatelessCallReservation).where(
            StatelessCallReservation.user_id == user))).all()
        wallet = await session.get(UserWallet, user)
    assert [hold.status for hold in holds] == ["settled"]
    assert wallet.used_quota_this_month == Decimal(5 * 1 + 5 * 2)
