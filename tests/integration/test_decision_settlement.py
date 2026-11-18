"""Real MariaDB quota/ledger proof; provider work is synthetic and never paid."""
from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import httpx
import pytest
from provider_fixtures import configured_provider
from sqlalchemy import delete, func, select, text

from lumen.db import close_db, get_session_factory, init_db
from lumen.models.chat_db import ChatApiKey, ChatUsageLog, LlmModel, LlmProvider, UserWallet
from lumen.models.chat_runs import ChatModelCallReservation, ChatRun
from lumen.models.decision_contracts import DecisionRequest
from lumen.models.gemini_decision_contracts import CONTEXT_TOKENS, GeminiDecisionRequest
from lumen.models.stateless_reservations import StatelessCallReservation
from lumen.services import completion_api, credit, decisions, gemini_decisions, stateless_credit
from lumen.services.providers import routing

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


@pytest.mark.parametrize("outcome", ["settled", "missing", "tier", "over_hold", "tool_over_cap", "no_container_id", "low"])
async def test_openai_hosted_text_stateless_hold_and_atomic_ledger(sql_owner, monkeypatch, outcome):
    from test_openai_text_protocol import MODEL, OUTPUT, TOOLS, input_details, route_for

    factory, user, project = sql_owner
    route = route_for()
    monkeypatch.setattr(completion_api.get_settings(), "chat_credit_per_usd", Decimal(1))
    monkeypatch.setattr(completion_api, "_authorize_completion", AsyncMock())
    totals = 272001
    payload = {
        "status": "completed", "service_tier": "default", "output": OUTPUT,
        "usage": {"input_tokens": totals, "output_tokens": 20, "total_tokens": totals + 20,
                  "input_tokens_details": input_details()},
    }
    if outcome == "missing":
        payload.pop("usage")
    elif outcome == "tier":
        payload["service_tier"] = "fast"
    elif outcome == "tool_over_cap":
        payload["output"] = [{"type": "web_search_call"}] * 11
    elif outcome == "no_container_id":
        payload["output"] = [{"type": "shell_call"}]
    cap = credit.call_credit_bound(
        {**route, "chat_credit_per_usd": Decimal(1)}, input_tokens=922000, output_tokens=64,
    ) + Decimal("1.30")
    if outcome == "over_hold":
        # Exercise transaction rollback, not only the earlier count fence.
        cap = Decimal("1.30")
        monkeypatch.setattr(credit, "call_credit_bound", lambda *_args, **_kwargs: Decimal("0"))
    async with factory() as session, session.begin():
        wallet = await session.get(UserWallet, user)
        wallet.max_quota_monthly = cap - Decimal("0.01") if outcome == "low" else cap
    upstream = AsyncMock(return_value=payload)
    monkeypatch.setattr(completion_api.provider_client, "aresponses", upstream)

    async def dispatch():
        return await completion_api.complete_responses(
            resolved=route, input="not persisted", stream=False, user_id=user, project_id=project,
            api_key_id=None, options={"tools": TOOLS, "max_output_tokens": 64, "service_tier": "auto",
                                     "prompt_cache_key": "test-cache", "prompt_cache_options": {"ttl": "12h"}},
        )

    if outcome == "settled":
        assert await dispatch() == payload
    else:
        with pytest.raises(completion_api.CompletionError) as failure:
            await dispatch()
        assert failure.value.status_code == (429 if outcome == "low" else 502)
        if outcome != "low":
            assert failure.value.code == "provider_result_unknown"
    if outcome == "low":
        upstream.assert_not_awaited()
    else:
        upstream.assert_awaited_once()
        assert upstream.await_args.kwargs["service_tier"] == "default"
        assert upstream.await_args.kwargs["max_tool_calls"] == 10
        assert upstream.await_args.kwargs["prompt_cache_options"] == {"ttl": "12h"}
    async with factory() as session:
        hold = (await session.scalars(select(StatelessCallReservation).where(
            StatelessCallReservation.user_id == user))).one_or_none()
        ledger = (await session.scalars(select(ChatUsageLog).where(ChatUsageLog.user_id == user))).one_or_none()
        wallet = await session.get(UserWallet, user)
        if outcome == "low":
            assert hold is ledger is None and wallet.used_quota_this_month == 0
        else:
            assert hold.bound_credits == cap
            if outcome == "settled":
                assert hold.status == "settled" and hold.event_id == ledger.event_id
                assert wallet.used_quota_this_month == hold.actual_credits == ledger.credited_cost
                assert ledger.model_name == MODEL and ledger.raw_cost == Decimal("1.339924")
                assert ledger.cache_read_input_tokens == 100
                assert ledger.cache_creation_5m_input_tokens == 200
                assert ledger.cache_creation_1h_input_tokens == 300
                assert "first" not in str(ledger.pricing_snapshot)
                assert ledger.pricing_snapshot["hosted_tools"]["container"]["blocks"] == 2
            else:
                assert hold.status == "unknown" and ledger is None and wallet.used_quota_this_month == 0


async def test_small_openai_text_prompt_fits_low_credit_wallet(sql_owner, monkeypatch):
    from test_openai_text_protocol import route_for

    factory, user, project = sql_owner
    monkeypatch.setattr(completion_api.get_settings(), "chat_credit_per_usd", 1.0)
    monkeypatch.setattr(completion_api, "_authorize_completion", AsyncMock())
    reply = {"status": "completed", "output": [], "usage": {"input_tokens": 20, "output_tokens": 10}}
    upstream = AsyncMock(return_value=reply)
    monkeypatch.setattr(completion_api.provider_client, "aresponses", upstream)
    async with factory() as session, session.begin():
        (await session.get(UserWallet, user)).max_quota_monthly = Decimal("0.02")
    assert await completion_api.complete_responses(
        resolved=route_for(), input="test", stream=False, user_id=user, project_id=project,
        api_key_id=None, options={"max_output_tokens": 128},
    ) == reply
    upstream.assert_awaited_once()
    async with factory() as session:
        hold = (await session.scalars(select(StatelessCallReservation).where(
            StatelessCallReservation.user_id == user))).one()
        assert hold.status == "settled" and hold.bound_credits < Decimal("0.02")
        assert hold.actual_credits == Decimal("0.00014")


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

    async def upstream(*_args, on_dispatch):
        nonlocal count
        count += 1
        on_dispatch()
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


async def test_settlement_is_atomic_and_idempotent(sql_owner, monkeypatch):
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
    # Verified billable usage settles even when the provider answer is invalid.
    invalid = _response()
    invalid["answers"] = []
    monkeypatch.setattr(decisions, "transport", AsyncMock(return_value=invalid))
    with pytest.raises(decisions.DecisionError, match="invalid decision answers"):
        await decisions.decide(route=_route(), body=_body(), user_id=user, project_id=project,
                               api_key_id=None, required_scopes=("native:runs:write",))
    async with factory() as session:
        holds = (await session.execute(select(StatelessCallReservation).where(
            StatelessCallReservation.user_id == user))).scalars().all()
        assert len(holds) == 2 and all(hold.status == "settled" for hold in holds)
        total = await session.scalar(select(func.sum(ChatUsageLog.credited_cost)).where(ChatUsageLog.user_id == user))
        assert (await session.get(UserWallet, user)).used_quota_this_month == total == amounts[0] * 2


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


@pytest.mark.parametrize("outcome", ["settled", "bad-answer", "unknown", "overspent", "released"])
async def test_gemini_real_hold_and_wallet_with_synthetic_http(sql_owner, monkeypatch, outcome):
    factory, user, project = sql_owner
    configured = {**_route(), "model_name": "gemini/gemini-decision-1", "api_model_name": "gemini-decision-1",
                  "model_kind": "decision", "provider_type": "gemini", "provider_name": "Gemini"}
    body = GeminiDecisionRequest.model_validate({
        "model": configured["model_name"], "input": "Private input not logged",
        "questions": {"q": {"type": "noul", "instructions": "Private question"}},
    })
    total = CONTEXT_TOKENS + 1 if outcome == "overspent" else 100
    reply = {
        "answers": {"q": {"type": "noul", "noul": 0.8, "confidence": 0.8}},
        "usage": {"total_input_tokens": total, "total_output_tokens": 0, "total_tokens": total,
                  "input_tokens_by_modality": [{"modality": "text", "tokens": total}]},
    }
    if outcome == "bad-answer":
        reply["answers"] = {}
    elif outcome == "unknown":
        reply.pop("usage")
    elif outcome == "released":
        reply = {"error": {"code": "safety", "message": "Private provider error"}}
    status = 400 if outcome == "released" else 200
    events = []

    def upstream(request):
        assert request.url == gemini_decisions.API_URL
        events.append(request.method)
        return httpx.Response(status, json=reply)

    monkeypatch.setattr(gemini_decisions, "resolve_route_api_key", AsyncMock(return_value="synthetic-api-key"))
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda **_: httpx.MockTransport(upstream))
    bound = gemini_decisions.credit_bound(
        configured["input_price_per_token"], Decimal(1), Decimal(str(gemini_decisions.get_settings().chat_credit_per_usd)),
    )
    async with factory() as session, session.begin():
        wallet = await session.get(UserWallet, user)
        wallet.max_quota_monthly = bound

    async def execute():
        return await gemini_decisions.decide(route=configured, body=body, user_id=user, project_id=project,
                                             api_key_id=None, required_scopes=("native:runs:write",))

    if outcome == "settled":
        assert (await execute())["usage"] == reply["usage"]
    else:
        with pytest.raises(decisions.DecisionError) as exc:
            await execute()
        assert exc.value.status_code == (400 if outcome == "released" else 502)
        assert "Private" not in exc.value.message
    assert events == ["POST"]
    async with factory() as session:
        hold = (await session.scalars(select(StatelessCallReservation).where(
            StatelessCallReservation.user_id == user))).one()
        rows = (await session.scalars(select(ChatUsageLog).where(ChatUsageLog.user_id == user))).all()
        wallet = await session.get(UserWallet, user)
        assert hold.bound_credits == bound
        if outcome in {"settled", "bad-answer"}:
            assert hold.status == "settled" and len(rows) == 1
            row = rows[0]
            assert row.event_id == hold.event_id and row.provider == "Gemini"
            assert (row.prompt_tokens, row.completion_tokens, row.raw_cost, row.pricing_status) == (
                100, 0, Decimal("0.00001"), "priced",
            )
            assert row.usage_components[0]["input_tokens_by_modality"] == reply["usage"]["input_tokens_by_modality"]
            assert "Private" not in str(row.usage_components) + str(row.pricing_snapshot)
            assert wallet.used_quota_this_month == row.credited_cost == hold.actual_credits
        else:
            assert not rows and wallet.used_quota_this_month == 0
            assert hold.status == ("released" if outcome == "released" else "unknown")
    if outcome in {"unknown", "overspent"}:
        with pytest.raises(credit.QuotaExceeded):
            await stateless_credit.reserve(event_id=str(uuid.uuid4()), user_id=user, project_id=project,
                                           api_key_id=None, bound=Decimal("0.00000001"))
    elif outcome == "released":
        await stateless_credit.reserve(event_id=str(uuid.uuid4()), user_id=user, project_id=project,
                                       api_key_id=None, bound=bound)


async def test_decision_kind_isolation_and_exact_catalog_price(sql_owner):
    factory, user, _project = sql_owner
    model_name = "gemini/gemini-decision-1"
    async with factory() as session, session.begin():
        provider = configured_provider(name=user, api_provider=user[:40], provider_type="gemini", is_active=True)
        session.add(provider)
        await session.flush()
        model = LlmModel(provider_id=provider.id, model_name=model_name, model_kind="decision", is_active=True)
        session.add(model)
        await session.flush()
        provider_id, model_id = provider.id, model.id
    exact = f"lumen/{provider_id}/{model_id}"
    try:
        selected = await decisions.resolve(exact, provider_type="gemini")
        assert selected["model_kind"] == "decision" and selected["model_name"] == model_name
        assert selected["input_price_per_token"] == Decimal("0.0000001")
        assert selected["output_price_per_token"] is None
        assert selected["price_source"] == "native_catalog"
        assert selected["provider_backend"] == "lumen-decisions-http"
        assert await routing.resolve_model_by_id(model_id) is None
        assert await routing.resolve_model(model_name) is None
        for kind in ("text", "embedding", "image", "tts", "stt", "realtime"):
            assert await routing.resolve_api_model(exact, model_kind=kind) is None
            assert not any(row["provider_id"] == provider_id for row in await routing.list_api_models(model_kind=kind))
        assert not any(row["provider_id"] == provider_id for row in await routing.list_cli_models())
        with pytest.raises(decisions.DecisionError, match="/v1beta/decisions"):
            await decisions.resolve(exact, openai_compat=True)
    finally:
        async with factory() as session, session.begin():
            await session.execute(delete(LlmProvider).where(LlmProvider.id == provider_id))
