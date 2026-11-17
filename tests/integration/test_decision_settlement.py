"""Real MariaDB quota/ledger proof; provider work is synthetic and never paid."""
from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, func, select, text

from lumen.db import close_db, get_session_factory, init_db
from lumen.models.chat_db import ChatApiKey, ChatUsageLog, UserWallet
from lumen.models.chat_runs import ChatModelCallReservation, ChatRun
from lumen.models.decision_contracts import DecisionRequest
from lumen.models.stateless_reservations import StatelessCallReservation
from lumen.services import credit, decisions, stateless_credit

pytestmark = pytest.mark.integration


@pytest.fixture
async def sql_owner(monkeypatch):
    init_db(os.environ["DATABASE_URL"], pool_size=8, max_overflow=0)
    factory = get_session_factory()
    nonce = uuid.uuid4().hex
    user, project = f"decision-{nonce}", "decision-project"
    async with factory() as session, session.begin():
        session.add(UserWallet(user_id=user, project_id=project, max_quota_monthly=Decimal("10"),
                               quota_period_start=date.today().replace(day=1)))
    monkeypatch.setattr(decisions, "authorize_new_io", AsyncMock())
    try:
        yield factory, user, project
    finally:
        async with factory() as session, session.begin():
            run_ids = select(ChatRun.id).where(ChatRun.user_id == user)
            await session.execute(delete(ChatModelCallReservation).where(ChatModelCallReservation.run_id.in_(run_ids)))
            await session.execute(delete(ChatRun).where(ChatRun.user_id == user))
            await session.execute(delete(StatelessCallReservation).where(StatelessCallReservation.user_id == user))
            await session.execute(delete(ChatUsageLog).where(ChatUsageLog.user_id == user))
            await session.execute(delete(ChatApiKey).where(ChatApiKey.owner_user_id == user))
            await session.execute(delete(UserWallet).where(UserWallet.user_id == user))
        await close_db()


def _route():
    return {"model_name": "synthetic-decision-model", "api_model_name": "wire-model", "model_kind": "text",
            "provider_type": "openai", "capabilities": {"decisions": True}, "margin_multiplier": Decimal("1"),
            "input_price_per_token": Decimal("0.0000001"), "provider_name": "synthetic"}


def _body():
    return DecisionRequest.model_validate({"model": "synthetic-decision-model", "input": "Private input not logged",
                                          "questions": [{"type": "predicate", "instructions": "Private question"}]})


def _response():
    return {"model": "wire-model", "answers": [{"type": "predicate", "name": None, "probability": 0.8}],
            "usage": {"input_tokens": 100, "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
                      "output_tokens": 0, "output_tokens_details": {"reasoning_tokens": 0}, "total_tokens": 100}}


@pytest.mark.parametrize("key_limited", [False, True])
async def test_concurrent_decisions_only_one_fitting_request_enters_upstream(sql_owner, monkeypatch, key_limited):
    factory, user, project = sql_owner
    key_id = None
    if key_limited:
        async with factory() as session, session.begin():
            wallet = await session.get(UserWallet, user)
            wallet.max_quota_monthly = Decimal("100")
            key = ChatApiKey(owner_user_id=user, owner_project_id=project, key_prefix="test-only", key_hash=uuid.uuid4().hex * 2,
                             scopes=["compat:completions:write"], owner_monthly_credit_limit=Decimal("10"))
            session.add(key)
            await session.flush()
            key_id = key.id
    entered = asyncio.Event()
    release = asyncio.Event()
    count = 0

    async def upstream(*_args):
        nonlocal count
        count += 1
        entered.set()
        await release.wait()
        return _response()

    monkeypatch.setattr(decisions, "credit_bound", lambda *_args: Decimal("6"))
    monkeypatch.setattr(decisions, "transport", upstream)

    async def call():
        return await decisions.decide(route=_route(), body=_body(), user_id=user, project_id=project,
                                      api_key_id=key_id, required_scopes=("native:runs:write",))
    first = asyncio.create_task(call())
    try:
        await asyncio.wait_for(entered.wait(), 10)
        with pytest.raises(decisions.DecisionError) as rejected:
            await asyncio.wait_for(call(), 10)
        assert rejected.value.status_code == 429
        assert count == 1
    finally:
        release.set()
        result = await asyncio.wait_for(first, 10)
    async with factory() as session:
        hold = await session.get(StatelessCallReservation, result["usage_log_id"])
        rows = (await session.execute(select(ChatUsageLog).where(ChatUsageLog.user_id == user))).scalars().all()
        wallet = await session.get(UserWallet, user)
        assert hold.status == "settled" and len(rows) == 1
        assert wallet.used_quota_this_month == rows[0].credited_cost
        assert rows[0].raw_cost == Decimal("0.00001")
        assert rows[0].usage_components[0]["category"] == "decision"
        assert "Private" not in str(rows[0].pricing_snapshot) + str(rows[0].usage_components)


async def test_settlement_is_atomic_and_idempotent(sql_owner):
    factory, user, project = sql_owner
    event_id = str(uuid.uuid4())
    await stateless_credit.reserve(event_id=event_id, user_id=user, project_id=project, api_key_id=None, bound=Decimal("1"))
    raw = _response()["usage"]
    _, breakdown = decisions.validate_usage(raw)
    usage = dict(event_id=event_id, user_id=user, project_id=project, api_key_id=None, model_name=_route()["model_name"],
                 provider="synthetic", prompt_tokens=100, completion_tokens=0,
                 usage_cost=decisions.decision_cost(_route(), raw), margin_multiplier=Decimal("1"),
                 source="api", breakdown=breakdown, execution_route=_route())
    amounts = await asyncio.gather(stateless_credit.settle(**usage), stateless_credit.settle(**usage))
    assert amounts[0] == amounts[1]
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(ChatUsageLog).where(ChatUsageLog.event_id == event_id)) == 1
        wallet = await session.get(UserWallet, user)
        assert wallet.used_quota_this_month == amounts[0]
        hold = await session.get(StatelessCallReservation, event_id)
        assert hold.status == "settled" and hold.actual_credits == amounts[0]


@pytest.mark.parametrize("status", ["reserved", "unknown"])
async def test_old_crashed_or_unknown_holds_never_age_out(sql_owner, monkeypatch, status):
    factory, user, project = sql_owner
    event_id = str(uuid.uuid4())
    await stateless_credit.reserve(event_id=event_id, user_id=user, project_id=project, api_key_id=None, bound=Decimal("6"))
    if status == "unknown":
        await stateless_credit.finish(event_id, uncertain=True)
    async with factory() as session, session.begin():
        hold = await session.get(StatelessCallReservation, event_id)
        hold.created_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=800)
    with pytest.raises(credit.QuotaExceeded):
        await stateless_credit.reserve(event_id=str(uuid.uuid4()), user_id=user, project_id=project,
                                       api_key_id=None, bound=Decimal("6"))
    async with factory() as session:
        assert (await session.get(StatelessCallReservation, event_id)).status == status
    # Explicit evidence-backed no-charge disposition is not an automatic expiry.
    await stateless_credit.finish(event_id, uncertain=False)
    await stateless_credit.reserve(event_id=str(uuid.uuid4()), user_id=user, project_id=project,
                                   api_key_id=None, bound=Decimal("6"))


async def test_definite_pre_send_failure_releases_and_allows_next_request(sql_owner, monkeypatch):
    factory, user, project = sql_owner
    monkeypatch.setattr(decisions, "credit_bound", lambda *_args: Decimal("6"))
    monkeypatch.setattr(decisions, "transport", AsyncMock(side_effect=decisions.DecisionError(502, "connection refused")))
    with pytest.raises(decisions.DecisionError):
        await decisions.decide(route=_route(), body=_body(), user_id=user, project_id=project,
                               api_key_id=None, required_scopes=("native:runs:write",))
    async with factory() as session:
        holds = (await session.execute(select(StatelessCallReservation).where(StatelessCallReservation.user_id == user))).scalars().all()
        assert len(holds) == 1 and holds[0].status == "released"
    await stateless_credit.reserve(event_id=str(uuid.uuid4()), user_id=user, project_id=project, api_key_id=None, bound=Decimal("6"))


async def test_stateless_and_durable_holds_share_quota_both_directions(sql_owner):
    factory, user, project = sql_owner
    run_id = str(uuid.uuid4())
    async with factory() as session, session.begin():
        session.add(ChatRun(id=run_id, run_scope="image", run_kind="image", project_id=project,
                            user_id=user, model_name="durable-hold", status="running", capability_snapshot={},
                            pricing_snapshot={}, client_request_id=str(uuid.uuid4()), request_fingerprint=run_id,
                            fingerprint_version=1, execution_protocol_version=1))
    async with factory() as session, session.begin():
        await session.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
        await credit.reserve_call_credit_in_transaction(session, user_id=user, project_id=project, api_key_id=None, bound=Decimal("6"))
        session.add(ChatModelCallReservation(run_id=run_id, segment_id="image:1", bound_credits=Decimal("6"), status="reserved"))
    with pytest.raises(credit.QuotaExceeded):
        await stateless_credit.reserve(event_id=str(uuid.uuid4()), user_id=user, project_id=project, api_key_id=None, bound=Decimal("6"))
    async with factory() as session, session.begin():
        hold = await session.get(ChatModelCallReservation, (run_id, "image:1"))
        hold.status = "settled"
    await stateless_credit.reserve(event_id=str(uuid.uuid4()), user_id=user, project_id=project, api_key_id=None, bound=Decimal("6"))
    async with factory() as session, session.begin():
        await session.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
        with pytest.raises(credit.QuotaExceeded):
            await credit.reserve_call_credit_in_transaction(session, user_id=user, project_id=project, api_key_id=None, bound=Decimal("6"))
