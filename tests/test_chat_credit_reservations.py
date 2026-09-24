"""Deterministic transaction-level credit bounds without an external database."""

from decimal import Decimal
from types import SimpleNamespace

import pytest

from lumen.models.chat_infrastructure import ChatAgentReservation
from lumen.models.chat_runs import ChatModelCallReservation
from lumen.services.durable_runs import budgets
from lumen.services.durable_runs.execution import _DurableExecutionHooks, _provider_credit_bound, _tool_credit_bound


class _Result:
    def __init__(self, row):
        self.row = row

    def scalar_one_or_none(self):
        return self.row


class _LedgerSession:
    def __init__(self, allocation):
        self.allocation = allocation
        self.calls = {}

    async def execute(self, statement):
        entity = statement.column_descriptions[0]["entity"]
        if entity is ChatAgentReservation:
            return _Result(self.allocation)
        if entity is ChatModelCallReservation:
            params = statement.compile().params
            run_id = next(value for key, value in params.items() if key.startswith("run_id_"))
            segment_id = next(value for key, value in params.items() if key.startswith("segment_id_"))
            return _Result(self.calls.get((run_id, segment_id)))
        raise AssertionError(f"unexpected ledger query: {entity}")

    def add(self, row):
        self.calls[row.run_id, row.segment_id] = row
        self.current = row



def test_frozen_output_and_tool_bounds_fail_closed_without_capacity(monkeypatch):
    from lumen.services import litellm_client

    monkeypatch.setattr(
        litellm_client, "count_context_tokens",
        lambda *_args: litellm_client.ContextTokenCount(tokens=None, measurement="unknown"),
    )
    run = SimpleNamespace(
        model_name="model", capability_snapshot={"capabilities": {"context_limit": 1}},
        pricing_snapshot={
            "input_price_per_token": "0.6", "output_price_per_token": "0.1",
            "margin_multiplier": "1", "chat_credit_per_usd": "1",
            "component_prices": {"web_fetch_request_per_unit": "0.8", "web_fetch_context_per_unit": "0.4"},
        },
    )
    with pytest.raises(Exception, match="max_tokens"):
        _provider_credit_bound(run, messages=[], tool_schemas=[], max_tokens=None)
    assert _provider_credit_bound(run, messages=[], tool_schemas=[], max_tokens=2) == Decimal("0.80000001")
    assert _tool_credit_bound(run, "managed_web_fetch") == Decimal("1.20000001")
    run.pricing_snapshot = {**run.pricing_snapshot, "input_price_per_token": "0", "output_price_per_token": "0"}
    run.capability_snapshot = {"capabilities": {}}
    assert _provider_credit_bound(run, messages=[], tool_schemas=[], max_tokens=None) == Decimal("0")
    run.capability_snapshot = {
        "capabilities": {},
        "effective_features": {"web_search": {"enabled": True, "mode": "native", "context_size": "low"}},
    }
    with pytest.raises(Exception, match="provider-enforced request bound"):
        _provider_credit_bound(run, messages=[], tool_schemas=[], max_tokens=None)
    run.pricing_snapshot["component_prices"] = {
        **run.pricing_snapshot["component_prices"],
        "web_search_request_per_unit": "0", "web_search_context_low_per_unit": "0",
    }
    assert _provider_credit_bound(run, messages=[], tool_schemas=[], max_tokens=None) == Decimal("0")

    assert _DurableExecutionHooks(run_id="root", owner="worker")._actual_credit(
        run, "chat_completions", {"prompt_tokens": 1, "completion_tokens": 1, "_durable_estimated": True}, None,
    ) is None

@pytest.mark.asyncio
async def test_managed_fetch_settles_actual_from_frozen_prices_once():

    run = SimpleNamespace(
        id="root", project_id="project", parent_run_id=None, root_run_id=None,
        model_name="model", credit_ceiling=Decimal("2"), reserved_credits=Decimal("0"),
        descendant_credits_reserved=Decimal("0"),
        pricing_snapshot={
            "margin_multiplier": "1", "chat_credit_per_usd": "1",
            "component_prices": {"web_fetch_request_per_unit": "0.8", "web_fetch_context_per_unit": "0.4"},
        },
    )
    quota = SimpleNamespace(project_id="project", max_credit_reservation=Decimal("2"), credits_reserved=Decimal("0"))
    session = _LedgerSession(None)
    segment = SimpleNamespace(run_id="root", segment_id="tool:0:0", status="prepared")
    bound = _tool_credit_bound(run, "managed_web_fetch")
    await budgets.reserve_call_credit(session, run=run, root=run, quota=quota, segment=segment, amount=bound)
    usage = [
        {"kind": "web_fetch_requests", "price_key": "web_fetch_request_per_unit",
         "quantity": "1", "unit": "request", "source": "fetch"},
        {"kind": "web_fetch_context", "price_key": "web_fetch_context_per_unit",
         "quantity": "1", "unit": "context", "source": "fetch"},
    ]
    actual = _DurableExecutionHooks(run_id="root", owner="worker")._actual_credit(
        run, "tool", None, {"status": "completed", "usage": usage},
    )
    assert actual == Decimal("1.2")
    assert _DurableExecutionHooks(run_id="root", owner="worker")._actual_credit(
        run, "tool", None, {"tool_name": "managed_web_fetch", "status": "completed", "usage": []},
    ) is None
    assert await budgets.settle_call_credit(session, run=run, root=run, quota=quota, segment=segment, actual=actual)
    assert not await budgets.settle_call_credit(session, run=run, root=run, quota=quota, segment=segment, actual=actual)
    assert run.reserved_credits == quota.credits_reserved == Decimal("1.2")


@pytest.mark.asyncio
async def test_child_parent_overrun_and_unknown_replay_preserve_headroom():
    quota = SimpleNamespace(project_id="project", max_credit_reservation=Decimal("10"), credits_reserved=Decimal("1"))
    root = SimpleNamespace(
        id="root", project_id="project", parent_run_id=None, root_run_id=None,
        credit_ceiling=Decimal("1.5"), reserved_credits=Decimal("0"),
        descendant_credits_reserved=Decimal("1"), reservation_released_at=None,
    )
    child = SimpleNamespace(
        id="child", project_id="project", parent_run_id="root", root_run_id="root",
        credit_ceiling=Decimal("1"), reserved_credits=Decimal("0"),
    )
    allocation = SimpleNamespace(
        run_id="child", root_run_id="root", project_id="project",
        status="reserved", amount=Decimal("1"),
    )
    session = _LedgerSession(allocation)
    first = SimpleNamespace(run_id="child", segment_id="provider:0:1", status="prepared")
    session.current = None
    await budgets.reserve_call_credit(
        session, run=child, root=root, quota=quota, segment=first, amount=Decimal("0.6"),
    )
    assert child.reserved_credits == Decimal("0.6")
    assert root.descendant_credits_reserved == quota.credits_reserved == Decimal("1")
    assert await budgets.settle_call_credit(
        session, run=child, root=root, quota=quota, segment=first, actual=Decimal("1.2"),
    )
    assert not await budgets.settle_call_credit(
        session, run=child, root=root, quota=quota, segment=first, actual=Decimal("0"),
    )
    assert child.reserved_credits == Decimal("1.2")
    assert session.current.actual_credits == Decimal("1.2")

    session.current = None
    second = SimpleNamespace(run_id="child", segment_id="provider:0:2", status="prepared")
    with pytest.raises(budgets.BudgetExceeded, match="child credit ceiling"):
        await budgets.reserve_call_credit(
            session, run=child, root=root, quota=quota, segment=second, amount=Decimal("0.1"),
        )
    assert child.reserved_credits == Decimal("1.2")

    parent = SimpleNamespace(run_id="root", segment_id="provider:0:1", status="prepared")
    with pytest.raises(budgets.BudgetExceeded, match="root credit ceiling"):
        await budgets.reserve_call_credit(
            session, run=root, root=root, quota=quota, segment=parent, amount=Decimal("0.6"),
        )
    assert root.reserved_credits == Decimal("0")
    await budgets.reserve_call_credit(
        session, run=root, root=root, quota=quota, segment=parent, amount=Decimal("0.4"),
    )
    assert quota.credits_reserved == Decimal("1.4")
    assert await budgets.settle_call_credit(
        session, run=root, root=root, quota=quota, segment=parent, actual=None,
    )
    assert not await budgets.settle_call_credit(
        session, run=root, root=root, quota=quota, segment=parent, actual=Decimal("0"),
    )
    assert session.current.status == "unknown"
    assert root.reserved_credits == Decimal("0.4")
    assert quota.credits_reserved == Decimal("1.4")
    assert await budgets.settle(
        session, quota=quota, root=root, run_id="child", kind="credit",
        actual=child.reserved_credits, order=budgets.LockOrder(),
    )
    assert not await budgets.settle(
        session, quota=quota, root=root, run_id="child", kind="credit",
        actual=Decimal("0"), order=budgets.LockOrder(),
    )
    assert root.descendant_credits_reserved == Decimal("1.2")
    assert quota.credits_reserved == Decimal("1.6")
    with pytest.raises(budgets.BudgetExceeded, match="root credit ceiling"):
        await budgets.reserve_call_credit(
            session, run=root, root=root, quota=quota,
            segment=SimpleNamespace(run_id="root", segment_id="provider:0:2", status="prepared"),
            amount=Decimal("0.2"),
        )
