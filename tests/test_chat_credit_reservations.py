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


@pytest.mark.parametrize("modality", ["image", "audio"])
@pytest.mark.parametrize("direction", ["input", "cache_read", "output"])
@pytest.mark.asyncio
async def test_modality_prices_bound_actual_child_usage_and_reject_underfunding(monkeypatch, modality, direction):
    from lumen.services import litellm_client
    from lumen.services.usage_breakdown import UsageBreakdown

    monkeypatch.setattr(litellm_client, "count_context_tokens", lambda *_args:
                        litellm_client.ContextTokenCount(tokens=1, measurement="known"))
    root = SimpleNamespace(id="root", project_id="project", parent_run_id=None, root_run_id=None,
                           credit_ceiling=Decimal("100"), reserved_credits=Decimal("0"),
                           descendant_credits_reserved=Decimal("0.5"))
    child = SimpleNamespace(
        id="child", project_id="project", parent_run_id="root", root_run_id="root", model_name="model",
        credit_ceiling=Decimal("0.5"), reserved_credits=Decimal("0"),
        capability_snapshot={"capabilities": {"context_limit": 10}},
        pricing_snapshot={"input_price_per_token": "0", "output_price_per_token": "0",
                          "margin_multiplier": "1", "chat_credit_per_usd": "1",
                          "token_rates": {modality: {"input_per_million": "0", "cache_read_per_million": "0",
                                                     "output_per_million": "0", f"{direction}_per_million": "100000"}}},
    )
    bound = _provider_credit_bound(child, messages=[], tool_schemas=[], max_tokens=10)
    counts = {"input_tokens": 0 if direction == "output" else 10,
              "output_tokens": 10 if direction == "output" else 0,
              "cache_read_input_tokens": 10 if direction == "cache_read" else 0}
    usage = UsageBreakdown.from_runtime({"prompt_tokens": counts["input_tokens"],
        "completion_tokens": counts["output_tokens"], "cache_read_input_tokens": counts["cache_read_input_tokens"],
        "modality_tokens": {modality: counts}}).as_usage_dict()
    actual = _DurableExecutionHooks(run_id="child", owner="worker")._actual_credit(child, "chat_completions", usage, None)
    assert actual == Decimal("1")
    assert Decimal("1") <= bound <= Decimal("1.00000001")
    quota = SimpleNamespace(project_id="project", max_credit_reservation=Decimal("100"), credits_reserved=Decimal("0.5"))
    allocation = SimpleNamespace(run_id="child", root_run_id="root", project_id="project",
                                 status="reserved", amount=Decimal("0.5"))
    session = _LedgerSession(allocation)
    with pytest.raises(budgets.BudgetExceeded, match="child credit ceiling"):
        await budgets.reserve_call_credit(session, run=child, root=root, quota=quota,
            segment=SimpleNamespace(run_id="child", segment_id="provider:0:1", status="prepared"), amount=bound)
    assert child.reserved_credits == 0


def test_actual_call_requirements_allow_text_after_media_but_never_missing_split():
    run = SimpleNamespace(model_name="model", pricing_snapshot={
        "input_price_per_token": "0.001", "output_price_per_token": "0.002",
        "margin_multiplier": "1", "chat_credit_per_usd": "1",
        "token_rates": {"image": {"input_per_million": "100000"}},
        "required_token_modalities": ["image_input"],
    })
    hooks = _DurableExecutionHooks(run_id="root", owner="worker")
    text = {"prompt_tokens": 10, "completion_tokens": 2, "required_token_modalities": []}
    assert hooks._actual_credit(run, "chat_completions", text, None) == Decimal("0.014")
    with pytest.raises(ValueError, match="image_input"):
        hooks._actual_credit(run, "chat_completions", {**text, "required_token_modalities": ["image_input"]}, None)
    # Historical snapshots without actual-call evidence retain the fail-closed contract.
    with pytest.raises(ValueError, match="image_input"):
        hooks._actual_credit(run, "chat_completions", {"prompt_tokens": 10, "completion_tokens": 2}, None)


def test_modality_only_prices_still_require_provider_capacity(monkeypatch):
    from lumen.services import litellm_client
    monkeypatch.setattr(litellm_client, "count_context_tokens", lambda *_args:
                        litellm_client.ContextTokenCount(tokens=None, measurement="unknown"))
    run = SimpleNamespace(model_name="model", capability_snapshot={"capabilities": {}}, pricing_snapshot={
        "input_price_per_token": "0", "output_price_per_token": "0", "margin_multiplier": "1", "chat_credit_per_usd": "1",
        "token_rates": {"audio": {"input_per_million": "100000"}},
    })
    with pytest.raises(Exception, match="context limit"):
        _provider_credit_bound(run, messages=[], tool_schemas=[], max_tokens=1)
    run.capability_snapshot = {"capabilities": {"context_limit": 10}}
    run.pricing_snapshot["token_rates"] = {"audio": {"output_per_million": "100000"}}
    with pytest.raises(Exception, match="max_tokens"):
        _provider_credit_bound(run, messages=[], tool_schemas=[], max_tokens=None)


@pytest.mark.asyncio
async def test_frozen_main_summary_modality_rates_survive_edits_and_reach_ledger(monkeypatch):
    from lumen.services import chat_admission, litellm_client
    from lumen.services.durable_runs import execution

    route = {"model_name": "main", "provider_id": 1, "model_id": 2, "provider_name": "gemini",
             "provider_type": "gemini", "config_version_hash": "frozen", "capabilities": {"context_limit": 10},
             "input_price_per_token": Decimal("0.001"), "output_price_per_token": Decimal("0.002"),
             "media_pricing": {"token_rates": {"audio": {"input_per_million": "100000"}}}}
    summary = {**route, "model_name": "summary", "media_pricing": {
        "token_rates": {"audio": {"input_per_million": "200000"}}}}
    capabilities, pricing = chat_admission._run_snapshots(route, {}, summary_route=summary)
    pricing["chat_credit_per_usd"] = "1"
    run = SimpleNamespace(id="root", user_id="user", project_id="project", conversation_id=None, api_key_id=None,
                          model_name="main", pricing_snapshot=pricing, capability_snapshot=capabilities)
    route["media_pricing"]["token_rates"]["audio"]["input_per_million"] = "9000000"
    summary["media_pricing"]["token_rates"]["audio"]["input_per_million"] = "9000000"
    usage = {"prompt_tokens": 10, "completion_tokens": 1, "required_token_modalities": ["audio_input"],
             "modality_tokens": {"audio": {"input_tokens": 10, "output_tokens": 0, "cache_read_input_tokens": 0}}}
    hooks = _DurableExecutionHooks(run_id="root", owner="worker")
    assert hooks._actual_credit(run, "chat_completions", usage, None) == Decimal("1.002")
    assert hooks._actual_credit(run, "context_compaction", usage, None) == Decimal("2.002")
    # Optional summary rates do not require an absent audio split.
    assert hooks._actual_credit(run, "context_compaction", {
        "prompt_tokens": 10, "completion_tokens": 1, "required_token_modalities": []}, None) == Decimal("0.012")
    monkeypatch.setattr(litellm_client, "count_context_tokens", lambda *_args:
                        litellm_client.ContextTokenCount(tokens=10, measurement="known"))
    assert _provider_credit_bound(run, messages=[], tool_schemas=[], max_tokens=1) == Decimal("1.00200001")
    assert _provider_credit_bound(run, messages=[], tool_schemas=[], max_tokens=1, summary=True) == Decimal("2.00200001")

    class Result:
        def __init__(self, value):
            self.value = value
        def scalar_one(self):
            return self.value
        def scalar_one_or_none(self):
            return self.value
    class Session:
        def __init__(self):
            self.results = iter([Result(run), Result(None)])
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_args):
            return None
        def begin(self):
            return self
        async def execute(self, *_args):
            return next(self.results)
    recorded = []
    async def apply_usage(_session, **kwargs):
        recorded.append(kwargs)
    monkeypatch.setattr(execution, "_factory", lambda: lambda: Session())
    monkeypatch.setattr(execution, "_require_owned_running_lease", lambda *_args: None)
    monkeypatch.setattr(execution.credit, "apply_usage_in_transaction", apply_usage)
    await hooks._record_summary_usage(segment_id="context:0:0:map", usage_payload=usage, route=summary)
    assert recorded[0]["usage_cost"].raw_cost == Decimal("2.002")
    assert sum(Decimal(item["cost_usd"]) for item in recorded[0]["usage_components"]) == Decimal("2.002")


def test_credit_hold_covers_rounding_of_all_text_and_modality_categories(monkeypatch):
    from lumen.services import litellm_client
    monkeypatch.setattr(litellm_client, "count_context_tokens", lambda *_args:
                        litellm_client.ContextTokenCount(tokens=8, measurement="known"))
    pricing = {key: "0.0000000000501" for key in (
        "input_price_per_token", "output_price_per_token", "cache_read_price_per_token",
        "cache_write_price_per_token", "cache_write_1h_price_per_token")}
    pricing.update({"margin_multiplier": "1", "chat_credit_per_usd": "100000000", "token_rates": {
        name: {direction: "0.0000501" for direction in ("input_per_million", "cache_read_per_million", "output_per_million")}
        for name in ("image", "audio")}})
    run = SimpleNamespace(model_name="model", pricing_snapshot=pricing,
                          capability_snapshot={"capabilities": {"context_limit": 8}})
    usage = {"prompt_tokens": 8, "completion_tokens": 3, "cache_read_input_tokens": 3,
             "cache_creation_5m_input_tokens": 1, "cache_creation_1h_input_tokens": 1,
             "modality_tokens": {name: {"input_tokens": 2, "output_tokens": 1, "cache_read_input_tokens": 1}
                                 for name in ("image", "audio")}}
    actual = _DurableExecutionHooks(run_id="root", owner="worker")._actual_credit(run, "chat_completions", usage, None)
    bound = _provider_credit_bound(run, messages=[], tool_schemas=[], max_tokens=3)
    assert actual == Decimal("0.11")
    assert bound == Decimal("0.16511")
    assert actual <= bound



