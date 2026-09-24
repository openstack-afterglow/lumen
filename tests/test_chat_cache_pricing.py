"""Prompt-cache billing across direct provider and configured custom routes.

Provider token counts are authoritative; exact bundled cache prices fill only
unset direct-provider categories. Durable runs freeze their admission prices.
"""

from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace

import litellm
import pytest
from litellm.llms.anthropic.chat.transformation import AnthropicConfig

from lumen.api.compat import openai as openai_api
from lumen.api.models import ModelCreateRequest, ModelResponse, ModelUpdateRequest
from lumen.models.chat_contracts import UsageComponent, UsageUpdatedPayload
from lumen.models.chat_db import ChatUsageLog, LlmModel, LlmProvider
from lumen.scripts.migrate import MIGRATIONS, _sha256, _statements, load_manifest
from lumen.services import advisor, completion_api, credit, graph, litellm_client
from lumen.services import stats as stats_service
from lumen.services.chat_admission import _run_snapshots
from lumen.services.durable_runs import execution
from lumen.services.providers import billing, pricing, repository, routing
from lumen.services.providers.errors import ActiveRunConfigurationConflict, ProviderValidationError
from lumen.services.tool_runtime import managed
from lumen.services.tools import ToolContext
from lumen.services.usage_breakdown import UsageBreakdown

_MODEL = "anthropic/claude-sonnet-5"

# One raw Anthropic Messages usage: a compaction iteration plus the message
# iteration, cache read in both, and a 5m/1h cache-creation split. Raw
# Anthropic ``input_tokens`` excludes cache; the top-level counters exclude the
# compaction iteration.
_RAW_ANTHROPIC_USAGE = {
    "input_tokens": 10,
    "output_tokens": 5,
    "cache_read_input_tokens": 100,
    "cache_creation_input_tokens": 30,
    "cache_creation": {"ephemeral_5m_input_tokens": 20, "ephemeral_1h_input_tokens": 10},
    "iterations": [
        {
            "type": "compaction",
            "input_tokens": 200,
            "output_tokens": 40,
            "cache_read_input_tokens": 50,
            "cache_creation_input_tokens": 0,
        },
        {
            "type": "message",
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_read_input_tokens": 100,
            "cache_creation_input_tokens": 30,
        },
    ],
}
# Expected ledger: total input 210 uncached + 150 read + 20 (5m) + 10 (1h).
_EXPECTED_COLUMNS = {
    "prompt_tokens": 390,
    "completion_tokens": 45,
    "cache_read_input_tokens": 150,
    "cache_creation_5m_input_tokens": 20,
    "cache_creation_1h_input_tokens": 10,
}

_INPUT = Decimal("0.000003")
_OUTPUT = Decimal("0.000015")
_CACHE_PRICES = {
    "cache_read_price_per_token": Decimal("0.0000003"),
    "cache_write_price_per_token": Decimal("0.00000375"),
    "cache_write_1h_price_per_token": Decimal("0.000006"),
}
_NO_CACHE_PRICES = dict.fromkeys(_CACHE_PRICES)


class _LedgerSession:
    """Capture the ChatUsageLog row ``apply_usage_in_transaction`` appends."""

    def __init__(self):
        self.rows: list[ChatUsageLog] = []

    def add(self, row):
        self.rows.append(row)

    async def flush(self):
        return None


async def _ledger_row(usage_cost, breakdown: UsageBreakdown) -> ChatUsageLog:
    session = _LedgerSession()
    await credit.apply_usage_in_transaction(
        session,
        event_id="event-1",
        user_id="u1",
        project_id="p1",
        model_name=_MODEL,
        provider="anthropic",
        prompt_tokens=breakdown.input_tokens,
        completion_tokens=breakdown.output_tokens,
        usage_cost=usage_cost,
        margin_multiplier=Decimal("1"),
        credit_per_usd=Decimal("1"),
        charge_wallet=False,
        breakdown=breakdown,
    )
    return session.rows[0]


def _columns(row: ChatUsageLog) -> dict[str, int]:
    return {key: getattr(row, key) for key in _EXPECTED_COLUMNS}


def _frozen_snapshot(cache_prices: dict) -> dict:
    return {
        "input_price_per_token": str(_INPUT),
        "output_price_per_token": str(_OUTPUT),
        **{key: str(value) if value is not None else None for key, value in cache_prices.items()},
    }


class TestCrossEndpointLedger:
    """The chat path (LiteLLM-normalized usage) and the /v1/messages passthrough
    (raw Anthropic usage) must write the same ledger for the same provider report."""

    async def _chat_path(self, cache_prices: dict):
        usage = AnthropicConfig().calculate_usage(json.loads(json.dumps(_RAW_ANTHROPIC_USAGE)), None)
        # graph round → provider_completed journal → executor usage event.
        round_usage = graph._usage_breakdown(usage)
        replayed = graph._replayed_usage(round_usage.as_usage_dict())
        aggregate = execution._usage_payload_breakdown((graph._replayed_usage({}) + replayed).as_usage_dict())
        cost = credit.usage_cost_from_pricing_snapshot(
            _frozen_snapshot(cache_prices),
            prompt_tokens=aggregate.input_tokens,
            completion_tokens=aggregate.output_tokens,
            breakdown=aggregate,
        )
        return aggregate, cost

    async def _passthrough_path(self, cache_prices: dict):
        runtime = completion_api._native_usage(
            {"usage": json.loads(json.dumps(_RAW_ANTHROPIC_USAGE))}, protocol="anthropic"
        )
        breakdown = litellm_client.extract_usage_breakdown(_MODEL, [], "", runtime)
        cost = litellm_client.cost_from_usage(
            _MODEL,
            breakdown.input_tokens,
            breakdown.output_tokens,
            input_price_per_token=_INPUT,
            output_price_per_token=_OUTPUT,
            price_source="manual",
            provider_type="anthropic",
            breakdown=breakdown,
            **cache_prices,
        )
        return breakdown, cost

    @pytest.mark.parametrize(
        ("cache_prices", "raw_cost", "status"),
        [
            # 210×3e-6 + 150×3e-7 + 20×3.75e-6 + 10×6e-6 + 45×1.5e-5
            (_CACHE_PRICES, Decimal("0.0014850000"), "priced"),
            # Cache categories bill 0 until a rate is set on the model.
            (_NO_CACHE_PRICES, Decimal("0.0013050000"), "partial"),
        ],
    )
    async def test_chat_and_passthrough_write_identical_ledgers(self, cache_prices, raw_cost, status):
        chat_breakdown, chat_cost = await self._chat_path(cache_prices)
        passthrough_breakdown, passthrough_cost = await self._passthrough_path(cache_prices)

        chat_row = await _ledger_row(chat_cost, chat_breakdown)
        passthrough_row = await _ledger_row(passthrough_cost, passthrough_breakdown)

        assert _columns(chat_row) == _columns(passthrough_row) == _EXPECTED_COLUMNS
        assert chat_cost.raw_cost == passthrough_cost.raw_cost == raw_cost
        assert chat_row.raw_cost == passthrough_row.raw_cost == raw_cost
        assert chat_cost.pricing_status == passthrough_cost.pricing_status == status
        for cost in (chat_cost, passthrough_cost):
            assert cost.raw_cost == (
                cost.input_cost
                + cost.output_cost
                + cost.cache_read_cost
                + cost.cache_creation_5m_cost
                + cost.cache_creation_1h_cost
            )

    @pytest.mark.parametrize("cache_prices", [_CACHE_PRICES, _NO_CACHE_PRICES], ids=["rates-set", "rates-unset"])
    async def test_streaming_passthrough_merges_start_and_delta_usage(self, monkeypatch, cache_prices):
        """message_start carries input/cache; the cumulative message_delta repeats
        them and adds output plus the iteration breakdown."""
        start_usage = {key: value for key, value in _RAW_ANTHROPIC_USAGE.items() if key != "iterations"}
        start_usage["output_tokens"] = 1
        events = [
            {"type": "message_start", "message": {"usage": start_usage}},
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "안녕"}},
            {"type": "message_delta", "usage": {"output_tokens": 5, "iterations": _RAW_ANTHROPIC_USAGE["iterations"]}},
            {"type": "message_stop"},
        ]

        async def stream():
            for event in events:
                yield event

        async def fake_messages(**_kwargs):
            return stream()

        captured: dict = {}

        async def apply_usage(**kwargs):
            captured.update(kwargs)
            return Decimal("0")

        monkeypatch.setattr(completion_api.litellm_client, "aanthropic_messages", fake_messages)
        monkeypatch.setattr(completion_api, "_with_passthrough_compaction", lambda options, **_kwargs: options)
        monkeypatch.setattr(completion_api.credit, "apply_usage", apply_usage)

        iterator = await completion_api.complete_anthropic(
            resolved={
                "model_name": _MODEL,
                "provider_type": "anthropic",
                "provider_name": "anthropic",
                "margin_multiplier": Decimal("1"),
                "input_price_per_token": _INPUT,
                "output_price_per_token": _OUTPUT,
                "price_source": "manual",
                **cache_prices,
            },
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=1024,
            stream=True,
            user_id="u1",
            project_id="p1",
            api_key_id=None,
            options={},
        )
        assert [event["type"] async for event in iterator][-1] == "message_stop"

        breakdown = captured["breakdown"]
        assert {"prompt_tokens": captured["prompt_tokens"], "completion_tokens": captured["completion_tokens"]} == {
            "prompt_tokens": 390,
            "completion_tokens": 45,
        }
        assert breakdown.cache_fields() == {
            "cache_read_input_tokens": 150,
            "cache_creation_5m_input_tokens": 20,
            "cache_creation_1h_input_tokens": 10,
        }
        # The streamed passthrough bills exactly what the chat path bills.
        chat_breakdown, chat_cost = await self._chat_path(cache_prices)
        assert breakdown == chat_breakdown
        assert captured["usage_cost"].raw_cost == chat_cost.raw_cost
        assert captured["usage_cost"].pricing_status == chat_cost.pricing_status


class TestProviderShapes:
    def test_responses_cached_tokens_are_not_double_counted(self):
        usage = {"input_tokens": 1000, "output_tokens": 50, "input_tokens_details": {"cached_tokens": 800}}
        runtime = completion_api._native_usage({"usage": usage}, protocol="responses")

        assert runtime == {
            "prompt_tokens": 1000,
            "completion_tokens": 50,
            "cache_read_input_tokens": 800,
            "cache_creation_5m_input_tokens": 0,
            "cache_creation_1h_input_tokens": 0,
        }
        assert UsageBreakdown.from_runtime(runtime).uncached_input_tokens == 200

    def test_openai_chat_cached_tokens_split_from_litellm_usage(self):
        usage = litellm.Usage(prompt_tokens=1000, completion_tokens=10, prompt_tokens_details={"cached_tokens": 800})
        breakdown = litellm_client.extract_usage_breakdown("gpt-5.6-sol", [], "", usage)

        assert (breakdown.input_tokens, breakdown.output_tokens) == (1000, 10)
        assert breakdown.cache_read_input_tokens == 800
        assert breakdown.cache_creation_input_tokens == 0
        assert breakdown.uncached_input_tokens == 200
        # The tuple API keeps prompt_tokens as total input for context accounting.
        assert litellm_client.extract_usage("gpt-5.6-sol", [], "", usage) == (1000, 10)

    def test_creation_without_ttl_split_is_attributed_to_five_minutes(self):
        breakdown = UsageBreakdown.from_anthropic(
            {"input_tokens": 5, "output_tokens": 1, "cache_creation_input_tokens": 40}
        )
        assert (breakdown.cache_creation_5m_input_tokens, breakdown.cache_creation_1h_input_tokens) == (40, 0)
        assert breakdown.input_tokens == 45

    def test_reported_one_hour_share_is_clamped_to_creation_total(self):
        breakdown = UsageBreakdown.from_anthropic(
            {
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_creation_input_tokens": 10,
                "cache_creation": {"ephemeral_1h_input_tokens": 25, "ephemeral_5m_input_tokens": 0},
            }
        )
        assert (breakdown.cache_creation_5m_input_tokens, breakdown.cache_creation_1h_input_tokens) == (0, 10)

    def test_defensive_parsing_ignores_invalid_and_clamps_cache(self):
        breakdown = UsageBreakdown.from_runtime(
            {
                "prompt_tokens": 100,
                "completion_tokens": 1,
                "cache_read_input_tokens": True,
                "cache_creation_5m_input_tokens": -5,
                "cache_creation_1h_input_tokens": 500,
            }
        )
        assert breakdown.cache_read_input_tokens == 0
        assert breakdown.cache_creation_5m_input_tokens == 0
        assert breakdown.cache_creation_1h_input_tokens == 100
        assert breakdown.uncached_input_tokens == 0
        responses = UsageBreakdown.from_responses(
            {"input_tokens": 10, "output_tokens": 1, "input_tokens_details": {"cached_tokens": 99}}
        )
        assert responses.cache_read_input_tokens == 10


class TestCacheRates:
    _BREAKDOWN = UsageBreakdown(
        input_tokens=1000,
        output_tokens=100,
        cache_read_input_tokens=600,
        cache_creation_5m_input_tokens=200,
        cache_creation_1h_input_tokens=100,
    )

    def _cost(self, **cache_prices):
        return litellm_client.cost_from_usage(
            _MODEL,
            1000,
            100,
            input_price_per_token=_INPUT,
            output_price_per_token=_OUTPUT,
            price_source="manual",
            breakdown=self._BREAKDOWN,
            **cache_prices,
        )

    def test_unset_rate_bills_zero_and_is_partial(self):
        cost = self._cost()
        assert cost.input_cost == Decimal("0.0003000000")  # uncached 100 tokens only
        assert cost.cache_read_cost == cost.cache_creation_5m_cost == cost.cache_creation_1h_cost == Decimal("0")
        assert cost.raw_cost == Decimal("0.0018000000")
        assert cost.pricing_status == "partial"
        assert cost.pricing_snapshot["input"]["tokens"] == 100
        assert cost.pricing_snapshot["cache_read"] == {
            "tokens": 600,
            "effective_price_per_token": None,
            "effective_price_per_million": None,
            "cost": "0",
            "source": None,
        }

    def test_set_rates_bill_exactly(self):
        cost = self._cost(**_CACHE_PRICES)
        assert cost.cache_read_cost == Decimal("0.0001800000")
        assert cost.cache_creation_5m_cost == Decimal("0.0007500000")
        assert cost.cache_creation_1h_cost == Decimal("0.0006000000")
        assert cost.raw_cost == Decimal("0.0033300000")
        assert cost.pricing_status == "priced"
        assert cost.pricing_snapshot["cache_creation_1h"]["source"] == "manual"
        assert cost.pricing_snapshot["cache_creation_1h"]["effective_price_per_million"] == "6.000000"

    def test_one_missing_rate_is_partial_and_only_that_category_is_free(self):
        cost = self._cost(**{**_CACHE_PRICES, "cache_write_1h_price_per_token": None})
        assert cost.cache_creation_1h_cost == Decimal("0")
        assert cost.raw_cost == Decimal("0.0027300000")
        assert cost.pricing_status == "partial"

    def test_zero_cache_tokens_without_rate_stays_priced(self):
        cost = litellm_client.cost_from_usage(
            _MODEL,
            10,
            5,
            input_price_per_token=_INPUT,
            output_price_per_token=_OUTPUT,
            price_source="manual",
        )
        assert cost.pricing_status == "priced"


    def test_breakdown_totals_must_match(self):
        with pytest.raises(ValueError):
            litellm_client.cost_from_usage(
                _MODEL,
                999,
                100,
                input_price_per_token=_INPUT,
                output_price_per_token=_OUTPUT,
                price_source="manual",
                breakdown=self._BREAKDOWN,
            )

    def test_frozen_snapshot_before_cache_rates_bills_zero_and_is_partial(self):
        legacy = {"input_price_per_token": str(_INPUT), "output_price_per_token": str(_OUTPUT)}
        cost = credit.usage_cost_from_pricing_snapshot(
            legacy, prompt_tokens=1000, completion_tokens=100, breakdown=self._BREAKDOWN
        )
        assert cost.raw_cost == Decimal("0.0018000000")
        assert cost.pricing_status == "partial"
        assert cost.pricing_snapshot["token_components"]["cache_read"]["source"] is None

    def test_frozen_snapshot_with_rates_matches_live_pricing(self):
        frozen = credit.usage_cost_from_pricing_snapshot(
            _frozen_snapshot(_CACHE_PRICES), prompt_tokens=1000, completion_tokens=100, breakdown=self._BREAKDOWN
        )
        assert frozen.raw_cost == self._cost(**_CACHE_PRICES).raw_cost
        assert frozen.pricing_status == "priced"

    def test_frozen_snapshot_without_cache_tokens_stays_priced(self):
        cost = credit.usage_cost_from_pricing_snapshot(
            {"input_price_per_token": "0.000001", "output_price_per_token": "0.000002"},
            prompt_tokens=3,
            completion_tokens=5,
        )
        assert cost.pricing_status == "priced"
        assert cost.raw_cost == Decimal("0.000013")

    def test_invalid_frozen_cache_rate_is_rejected(self):
        with pytest.raises(ValueError):
            credit.usage_cost_from_pricing_snapshot(
                {**_frozen_snapshot(_NO_CACHE_PRICES), "cache_read_price_per_token": "-1"},
                prompt_tokens=1,
                completion_tokens=1,
            )


class TestUsageComponents:
    def test_executor_components_price_each_category_and_contract_accepts_them(self):
        breakdown = TestCacheRates._BREAKDOWN
        cost = credit.usage_cost_from_pricing_snapshot(
            _frozen_snapshot(_CACHE_PRICES), prompt_tokens=1000, completion_tokens=100, breakdown=breakdown
        )
        components = execution._token_usage_components(
            breakdown, cost, segment_id="executor:aggregate", source="executor", model_name=_MODEL, metadata={}
        )

        assert {item["kind"]: item["quantity"] for item in components} == {
            "input_tokens": "100",
            "output_tokens": "100",
            "cache_read_input_tokens": "600",
            "cache_creation_5m_input_tokens": "200",
            "cache_creation_1h_input_tokens": "100",
        }
        for item in components:
            assert Decimal(item["unit_price_usd"]) * Decimal(item["quantity"]) == Decimal(item["cost_usd"])
        assert sum(Decimal(item["cost_usd"]) for item in components) == cost.raw_cost
        UsageUpdatedPayload(
            components=components,
            prompt_tokens=1000,
            completion_tokens=100,
            raw_cost=format(cost.raw_cost, "f"),
            credited_cost="0",
        )

    def test_components_omit_cache_categories_without_tokens(self):
        breakdown = UsageBreakdown(10, 5)
        cost = credit.usage_cost_from_pricing_snapshot(
            _frozen_snapshot(_NO_CACHE_PRICES), prompt_tokens=10, completion_tokens=5
        )
        components = execution._token_usage_components(
            breakdown, cost, segment_id="s", source="executor", model_name=_MODEL, metadata={}
        )
        assert [item["kind"] for item in components] == ["input_tokens", "output_tokens"]

    async def test_summary_route_bills_cache_from_frozen_summary_prices(self, monkeypatch):
        run = SimpleNamespace(
            id="run-1",
            user_id="u1",
            project_id="p1",
            model_name="summary-model",
            conversation_id="c1",
            api_key_id=None,
            pricing_snapshot={
                "margin_multiplier": "1",
                "chat_credit_per_usd": "1",
                "summary_route": _frozen_snapshot(_CACHE_PRICES),
            },
        )

        class _Result:
            def __init__(self, value):
                self.value = value

            def scalar_one(self):
                return self.value

            def scalar_one_or_none(self):
                return self.value

        class _Session:
            def __init__(self):
                self.results = iter((_Result(run), _Result(None)))

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            def begin(self):
                return self

            async def execute(self, *_args):
                return next(self.results)

        captured: list[dict] = []

        async def apply_usage(_session, **kwargs):
            captured.append(kwargs)

        monkeypatch.setattr(execution, "_factory", lambda: lambda: _Session())
        monkeypatch.setattr(execution, "_require_owned_running_lease", lambda *_args: None)
        monkeypatch.setattr(execution.credit, "apply_usage_in_transaction", apply_usage)

        await execution._DurableExecutionHooks(run_id="run-1", owner="worker-1")._record_summary_usage(
            segment_id="context:1:0:map",
            usage_payload=TestCacheRates._BREAKDOWN.as_usage_dict(),
            route={"model_name": "summary-model", "provider_name": "anthropic"},
        )

        recorded = captured[0]
        assert recorded["prompt_tokens"] == 1000
        assert recorded["breakdown"] == TestCacheRates._BREAKDOWN
        assert recorded["usage_cost"].raw_cost == Decimal("0.0033300000")
        assert {item["kind"] for item in recorded["usage_components"]} >= {"cache_creation_1h_input_tokens"}


class TestConfigFingerprint:
    @staticmethod
    def _rows(**cache_columns):
        provider = LlmProvider(
            id=1,
            name="anthropic-prod",
            provider_type="anthropic",
            auth_mode="api_key",
            api_base=None,
            is_active=True,
            margin_multiplier=Decimal("1.2"),
        )
        model = LlmModel(
            id=10,
            provider_id=1,
            model_name=_MODEL,
            is_active=True,
            input_price=Decimal("0.0000030000"),
            output_price=Decimal("0.0000150000"),
            price_source="manual",
            price_metadata=None,
            **cache_columns,
        )
        return model, provider

    @pytest.fixture(autouse=True)
    def _routing(self, monkeypatch):
        monkeypatch.setattr(routing, "resolve_api_key", lambda _provider: "key-1")
        monkeypatch.setattr(routing, "derive_encryption_subkey", lambda _domain: b"test-routing-key")
        monkeypatch.setattr(routing, "_effective_capabilities", lambda *_args, **_kwargs: ({}, "test"))
        monkeypatch.setattr(
            routing, "_pricing_aware_capabilities", lambda _model, capabilities, **_kwargs: capabilities
        )
        monkeypatch.setattr(
            routing,
            "_resolved_base_prices",
            lambda model, _provider: (model.input_price, model.output_price, "manual", "v1"),
        )


    def test_setting_any_cache_rate_changes_the_hash(self):
        baseline = routing._resolved_model(*self._rows())["config_version_hash"]
        with_read = routing._resolved_model(*self._rows(cache_read_price=Decimal("0.0000003000")))
        with_other = routing._resolved_model(*self._rows(cache_read_price=Decimal("0.0000004000")))
        with_1h = routing._resolved_model(*self._rows(cache_write_1h_price=Decimal("0.0000060000")))

        assert len({baseline, with_read["config_version_hash"], with_other["config_version_hash"]}) == 3
        assert with_1h["config_version_hash"] != baseline
        assert with_read["cache_read_price_per_token"] == Decimal("0.0000003000")

    def test_admission_snapshot_freezes_cache_rates(self, monkeypatch):
        resolved = routing._resolved_model(*self._rows(cache_write_price=Decimal("0.0000037500")))
        summary = {**resolved, "cache_write_1h_price_per_token": Decimal("0.0000060000")}
        _capability, pricing_snapshot = _run_snapshots(resolved, {}, summary_route=summary)

        assert pricing_snapshot["cache_read_price_per_token"] is None
        assert Decimal(pricing_snapshot["cache_write_price_per_token"]) == Decimal("0.00000375")
        assert pricing_snapshot["cache_write_1h_price_per_token"] is None
        assert Decimal(pricing_snapshot["summary_route"]["cache_write_1h_price_per_token"]) == Decimal("0.000006")


    async def test_direct_gemini_cache_hit_uses_frozen_catalog_rate_and_ledger(self):
        model, provider = self._rows()
        model.model_name = "gemini/gemini-2.5-flash-lite"
        model.input_price = Decimal("0.0000001000")
        model.output_price = Decimal("0.0000004000")
        provider.provider_type = "gemini"
        resolved = routing._resolved_model(model, provider)
        assert resolved["cache_read_price_per_token"] == Decimal("0.0000000100")
        assert resolved["cache_price_sources"]["cache_read"] == "litellm"

        _, snapshot = _run_snapshots(resolved, {})
        observed = UsageBreakdown.from_runtime(litellm.Usage(
            prompt_tokens=2806, completion_tokens=2,
            prompt_tokens_details={"cached_tokens": 2038},
        ))
        direct = litellm_client.cost_from_usage(
            model.model_name, observed.input_tokens, observed.output_tokens,
            input_price_per_token=resolved["input_price_per_token"],
            output_price_per_token=resolved["output_price_per_token"],
            price_source=resolved["price_source"], provider_type=resolved["provider_type"],
            breakdown=observed, cache_read_price_per_token=resolved["cache_read_price_per_token"],
            cache_price_sources=resolved["cache_price_sources"],
        )
        frozen = credit.usage_cost_from_pricing_snapshot(
            snapshot, prompt_tokens=2806, completion_tokens=2, breakdown=observed,
        )
        row = await _ledger_row(frozen, observed)
        assert direct.raw_cost == frozen.raw_cost == row.raw_cost == Decimal("0.0000979800")
        assert direct.pricing_status == frozen.pricing_status == "priced"
        assert row.cache_read_input_tokens == 2038
        assert direct.pricing_snapshot["cache_read"]["source"] == "litellm"
        assert frozen.pricing_snapshot["token_components"]["cache_read"]["source"] == "litellm"
        admin = stats_service._cache_price_projection(row.pricing_snapshot)
        assert admin["cache_costs_usd"]["cache_read"] == "0.0000203800"
        assert admin["cache_price_sources"]["cache_read"] == "litellm"

    @pytest.mark.parametrize("provider_type,model_name", [
        ("gemini", "gemini/gemini-pro-latest"),
        ("anthropic", "anthropic/claude-sonnet-5"),
    ])
    def test_catalog_long_context_tiers_freeze_at_admission(self, monkeypatch, provider_type, model_name):
        catalog_key = model_name
        metadata = {
            "litellm_provider": provider_type,
            "cache_read_input_token_cost": "0.0000002",
            "cache_read_input_token_cost_above_200k_tokens": "0.0000004",
            "cache_creation_input_token_cost": "0.0000005",
            "cache_creation_input_token_cost_above_200k_tokens": "0.0000008",
            "cache_creation_input_token_cost_above_1hr": "0.000001",
            "cache_creation_input_token_cost_above_1hr_above_200k_tokens": "0.0000015",
        }
        monkeypatch.setitem(litellm.model_cost, catalog_key, metadata)
        model, provider = self._rows()
        model.model_name = model_name
        provider.provider_type = provider_type
        resolved = routing._resolved_model(model, provider)
        _, frozen = _run_snapshots(resolved, {})
        assert frozen["cache_price_sources"] == {
            "cache_read": "litellm", "cache_creation_5m": "litellm", "cache_creation_1h": "litellm",
        }
        assert Decimal(frozen["cache_read_price_per_token_above_200k"]) == Decimal("0.0000004")
        metadata["cache_read_input_token_cost_above_200k_tokens"] = "0.000009"

        for prompt in (200_000, 200_001):
            breakdown = UsageBreakdown.from_totals(
                prompt, 0, cache_read_input_tokens=prompt - 100_000,
                cache_creation_5m_input_tokens=50_000, cache_creation_1h_input_tokens=50_000,
            )
            direct = litellm_client.cost_from_usage(
                model_name, prompt, 0, input_price_per_token=resolved["input_price_per_token"],
                output_price_per_token=resolved["output_price_per_token"],
                price_source=resolved["price_source"], provider_type=provider_type,
                breakdown=breakdown, allow_catalog_cache=False,
                **{key: resolved[key] for key in (
                    "cache_read_price_per_token", "cache_write_price_per_token", "cache_write_1h_price_per_token",
                    "cache_read_price_per_token_above_200k", "cache_write_price_per_token_above_200k",
                    "cache_write_1h_price_per_token_above_200k", "cache_price_sources",
                )},
            )
            durable = credit.usage_cost_from_pricing_snapshot(
                frozen, prompt_tokens=prompt, completion_tokens=0, breakdown=breakdown,
            )
            high = prompt > 200_000
            read_rate = Decimal("0.0000004" if high else "0.0000002")
            write_rate = Decimal("0.0000008" if high else "0.0000005")
            write_1h_rate = Decimal("0.0000015" if high else "0.000001")
            expected = read_rate * (prompt - 100_000) + write_rate * 50_000 + write_1h_rate * 50_000
            assert direct.raw_cost == durable.raw_cost == expected
            assert Decimal(direct.pricing_snapshot["cache_read"]["effective_price_per_token"]) == read_rate
            assert Decimal(durable.pricing_snapshot["token_components"]["cache_creation_1h"]["price_per_token"]) == write_1h_rate

        model.cache_read_price = Decimal("0.0000003")
        manual = routing._resolved_model(model, provider)
        assert manual["cache_price_sources"]["cache_read"] == "manual"
        assert manual["cache_read_price_per_token_above_200k"] is None
        manual_frozen = _run_snapshots(manual, {})[1]
        assert credit.usage_cost_from_pricing_snapshot(
            manual_frozen, prompt_tokens=200_001, completion_tokens=0,
            breakdown=UsageBreakdown.from_totals(200_001, 0, cache_read_input_tokens=200_001),
        ).cache_read_cost == Decimal("0.0600003000")

    def test_custom_base_uses_configured_prices_not_public_catalog(self):
        model, provider = self._rows(cache_read_price=Decimal("0.0000002500"))
        model.model_name = "gemini/gemini-2.5-flash-lite"
        provider.provider_type = "gemini"
        provider.api_base = "https://tenant.example/v1"
        resolved = routing._resolved_model(model, provider)
        assert resolved["cache_price_sources"]["cache_read"] == "manual"
        breakdown = UsageBreakdown.from_totals(2806, 2, cache_read_input_tokens=2038)
        cost = litellm_client.cost_from_usage(
            model.model_name, breakdown.input_tokens, breakdown.output_tokens,
            input_price_per_token=resolved["input_price_per_token"],
            output_price_per_token=resolved["output_price_per_token"],
            price_source=resolved["price_source"], provider_type="gemini",
            api_base=provider.api_base, breakdown=breakdown,
            cache_read_price_per_token=resolved["cache_read_price_per_token"],
            cache_price_sources=resolved["cache_price_sources"],
        )
        assert cost.cache_read_cost == Decimal("0.0005095000")
        assert cost.pricing_snapshot["cache_read"]["source"] == "manual"
        model.cache_read_price = None
        assert routing._resolved_model(model, provider)["cache_read_price_per_token"] is None
        assert litellm_client.effective_prices_per_million(
            model.model_name, "gemini", api_base=provider.api_base
        ) == (None, None)


    async def test_compat_completion_uses_provider_hit_for_charge_and_openai_usage(self, monkeypatch):
        model, provider = self._rows()
        model.model_name = "gemini/gemini-2.5-flash-lite"
        model.input_price = Decimal("0.0000001000")
        model.output_price = Decimal("0.0000004000")
        provider.provider_type = "gemini"
        resolved = routing._resolved_model(model, provider)
        usage = litellm.Usage(
            prompt_tokens=2806, completion_tokens=2,
            prompt_tokens_details={"cached_tokens": 2038},
        )
        captured = []

        async def complete(*_args, **_kwargs):
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="OK", tool_calls=None), finish_reason="stop")],
                usage=usage,
            )

        async def apply_usage(**kwargs):
            captured.append(kwargs)
            return Decimal("0.00009798")

        monkeypatch.setattr(completion_api.litellm_client, "acompletion", complete)
        monkeypatch.setattr(completion_api.credit, "apply_usage", apply_usage)
        result = await completion_api.complete_once(
            resolved=resolved, messages=[{"role": "user", "content": "hello"}],
            user_id="u1", project_id="p1", api_key_id=1,
            max_tokens=16, temperature=None,
        )
        wire = openai_api.OpenAIChatResponse.model_validate(
            openai_api.nonstream_response(result, cmpl_id="chatcmpl-test", created=1)
        )
        assert wire.usage.prompt_tokens_details["cached_tokens"] == 2038
        assert wire.usage.prompt_tokens == 2806
        assert captured[0]["usage_cost"].raw_cost == Decimal("0.0000979800")
        assert captured[0]["breakdown"].uncached_input_tokens == 768

        async def complete_stream(*_args, **_kwargs):
            async def chunks():
                yield SimpleNamespace(usage=usage, choices=[])
            return chunks()

        monkeypatch.setattr(completion_api.litellm_client, "acompletion_stream", complete_stream)
        events = [event async for event in completion_api.complete_stream(
            resolved=resolved, messages=[{"role": "user", "content": "hello"}],
            user_id="u1", project_id="p1", api_key_id=1,
            max_tokens=16, temperature=None,
        )]
        wire_chunk = openai_api.usage_chunk(events[-1], cmpl_id="chatcmpl-test", created=1, model=model.model_name)
        assert wire_chunk["usage"]["prompt_tokens_details"]["cached_tokens"] == 2038
        assert len(captured) == 2
        assert captured[1]["usage_cost"].raw_cost == captured[0]["usage_cost"].raw_cost


class _Tx:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, *_args):
        (self.session.sync.commit if exc_type is None else self.session.sync.rollback)()
        return False


class _AsyncSession:
    def __init__(self, sync, ids):
        self.sync = sync
        self.ids = ids

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        self.sync.close()
        return False

    def begin(self):
        return _Tx(self)

    async def execute(self, statement):
        return self.sync.execute(statement)

    async def get(self, entity, identity):
        return self.sync.get(entity, identity)

    async def scalar(self, statement):
        return self.sync.scalar(statement)

    def add(self, row):
        self.sync.add(row)

    async def flush(self):
        for row in self.sync.new:
            if isinstance(row, LlmModel) and row.id is None:
                row.id = self.ids["model"]
                self.ids["model"] += 1
        self.sync.flush()


@pytest.fixture
def model_db(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine("sqlite:///:memory:")
    LlmProvider.__table__.create(engine)
    LlmModel.__table__.create(engine)
    sync_factory = sessionmaker(engine, expire_on_commit=False)
    ids = {"model": 100}
    monkeypatch.setattr(repository, "_require_db", lambda: lambda: _AsyncSession(sync_factory(), ids))
    monkeypatch.setattr(pricing, "_effective_capabilities", lambda *_args, **_kwargs: ({}, "test"))
    monkeypatch.setattr(pricing, "_pricing_aware_capabilities", lambda _model, capabilities, **_kwargs: capabilities)

    async def lock(session, *, provider_id, model_ids=None):
        provider = await session.get(LlmProvider, provider_id)
        return provider, [await session.get(LlmModel, model_id) for model_id in sorted(model_ids or ())]

    monkeypatch.setattr(repository, "_lock_mutable_route", lock)
    with sync_factory.begin() as session:
        session.add(
            LlmProvider(
                id=1,
                name="anthropic-prod",
                provider_type="anthropic",
                auth_mode="api_key",
                is_active=True,
                margin_multiplier=Decimal("1"),
            )
        )

    def row(model_id):
        with sync_factory() as session:
            return session.get(LlmModel, model_id)

    yield row
    engine.dispose()


class TestAdminModelCachePrices:
    async def test_create_accepts_cache_rates_independently_and_projects_per_million(self, model_db):
        created = await repository.create_model(
            provider_id=1,
            model_name="claude-sonnet-5",
            cache_read_price_per_million="0.3",
        )

        stored = model_db(created["id"])
        assert stored.input_price is None and stored.output_price is None
        assert stored.cache_read_price == Decimal("0.0000003000")
        assert stored.cache_write_price is None and stored.cache_write_1h_price is None
        body = ModelResponse(**created).model_dump(mode="json")
        assert Decimal(body["cache_read_price_per_million"]) == Decimal("0.3")
        assert body["cache_write_price_per_million"] is None
        assert body["cache_write_1h_price_per_million"] is None

    async def test_patch_sets_clears_and_leaves_absent_keys_unchanged(self, model_db):
        created = await repository.create_model(
            provider_id=1,
            model_name="claude-sonnet-5",
            input_price_per_million="3",
            output_price_per_million="15",
            cache_read_price_per_million="0.3",
            cache_write_price_per_million="3.75",
        )
        model_id = created["id"]

        await repository.update_model(model_id, {"cache_write_1h_price_per_million": Decimal("6")})
        stored = model_db(model_id)
        assert stored.cache_read_price == Decimal("0.0000003000")
        assert stored.cache_write_price == Decimal("0.0000037500")
        assert stored.cache_write_1h_price == Decimal("0.0000060000")
        assert stored.input_price == Decimal("0.0000030000")

        await repository.update_model(model_id, {"cache_read_price_per_million": None})
        stored = model_db(model_id)
        assert stored.cache_read_price is None
        assert stored.cache_write_price == Decimal("0.0000037500")

    async def test_cache_price_patch_takes_the_active_run_lock(self, model_db, monkeypatch):
        created = await repository.create_model(provider_id=1, model_name="claude-sonnet-5")

        async def locked(*_args, **_kwargs):
            raise ActiveRunConfigurationConflict("active run")

        monkeypatch.setattr(repository, "_lock_mutable_route", locked)
        with pytest.raises(ActiveRunConfigurationConflict):
            await repository.update_model(created["id"], {"cache_read_price_per_million": Decimal("0.3")})
        assert model_db(created["id"]).cache_read_price is None

    async def test_repository_rejects_negative_and_underflowing_cache_rates(self, model_db):
        with pytest.raises(ProviderValidationError):
            await repository.create_model(provider_id=1, model_name="m", cache_write_price_per_million="-1")
        with pytest.raises(ProviderValidationError):
            await repository.update_model(1, {"cache_read_price_per_million": "0.00004"})

    def test_request_schema_validation(self):
        assert ModelCreateRequest(provider_id=1, model_name="m", cache_read_price_per_million="0.3")
        # Cache rates are independent of the input/output pair rule on PATCH.
        patch = ModelUpdateRequest(cache_write_1h_price_per_million=None)
        assert patch.model_dump(exclude_unset=True) == {"cache_write_1h_price_per_million": None}
        for value in ("-1", "NaN", "Infinity", "0.00004"):
            with pytest.raises(ValueError):
                ModelCreateRequest(provider_id=1, model_name="m", cache_read_price_per_million=value)
            with pytest.raises(ValueError):
                ModelUpdateRequest(cache_write_price_per_million=value)

    async def test_http_create_and_patch_carry_cache_rates(self, admin_client, monkeypatch):
        captured: dict = {}

        def public(**fields):
            return {
                "id": 5,
                "provider_id": 1,
                "model_name": "claude-sonnet-5",
                "api_model_name": "claude-sonnet-5",
                "api_provider": "anthropic",
                "display_name": None,
                "is_active": True,
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
                **fields,
            }

        async def fake_create(**kwargs):
            captured["create"] = kwargs
            return public(cache_read_price_per_million=kwargs["cache_read_price_per_million"])

        async def fake_update(model_id, patch):
            captured["patch"] = patch
            return public(cache_write_1h_price_per_million=Decimal("6"))

        monkeypatch.setattr(repository, "create_model", fake_create)
        monkeypatch.setattr(repository, "update_model", fake_update)

        created = await admin_client.post(
            "/api/v1/chat/admin/models",
            json={"provider_id": 1, "model_name": "claude-sonnet-5", "cache_read_price_per_million": "0.3"},
        )
        assert created.status_code == 201
        assert created.json()["cache_read_price_per_million"] == "0.3"
        assert created.json()["cache_write_price_per_million"] is None
        assert captured["create"]["cache_write_1h_price_per_million"] is None

        patched = await admin_client.patch(
            "/api/v1/chat/admin/models/5", json={"cache_write_1h_price_per_million": "6"}
        )
        assert patched.status_code == 200
        assert set(captured["patch"]) == {"cache_write_1h_price_per_million"}
        assert patched.json()["cache_write_1h_price_per_million"] == "6"

        rejected = await admin_client.patch("/api/v1/chat/admin/models/5", json={"cache_read_price_per_million": "-1"})
        assert rejected.status_code == 422


class TestBillingBreakdownRoute:
    async def test_provider_billing_route_keeps_token_breakdowns(self, admin_client, monkeypatch):
        periods = {"daily": "1", "weekly": "2", "monthly": "3", "total": "4"}
        provider_periods = {"daily": "1", "weekly": "2", "monthly": "3", "total": None}

        async def fake_snapshots():
            return [
                {
                    "provider_id": 2,
                    "provider_name": "anthropic-prod",
                    "provider_type": "anthropic",
                    "capability": "anthropic_admin_usage",
                    "status": "available",
                    "reason": None,
                    "fetched_at": "2026-09-11T00:00:00+00:00",
                    "billing_url": None,
                    "usage_url": None,
                    "has_billing_admin_key": True,
                    "local_usage": {
                        "currency": "USD",
                        "requests": periods,
                        "tokens": periods,
                        "raw_cost": periods,
                        "token_breakdown": dict.fromkeys(billing.TOKEN_CATEGORIES, periods),
                    },
                    "provider_usage": {
                        "source": "anthropic_admin_usage",
                        "currency": "USD",
                        "cost": provider_periods,
                        "requests": None,
                        "tokens": provider_periods,
                        "token_breakdown": dict.fromkeys(billing.TOKEN_CATEGORIES, provider_periods),
                    },
                    "is_available": None,
                    "is_free_tier": None,
                    "limit": None,
                    "remaining": None,
                    "usage_total": None,
                    "usage_daily": None,
                    "usage_weekly": None,
                    "usage_monthly": None,
                    "balances": [],
                }
            ]

        monkeypatch.setattr(billing, "list_provider_billing", fake_snapshots)
        response = await admin_client.get("/v1/admin/providers/billing")

        assert response.status_code == 200
        body = response.json()[0]
        assert set(body["local_usage"]["token_breakdown"]) == set(billing.TOKEN_CATEGORIES)
        assert body["local_usage"]["token_breakdown"]["cache_creation_1h"]["monthly"] == 3
        assert body["provider_usage"]["token_breakdown"]["cache_read"]["total"] is None


def test_prompt_cache_migration_is_registered_additive_and_checksummed():
    migration = next(item for item in load_manifest() if item.logical_id == "014-prompt-cache-pricing")
    assert migration.sha256 == _sha256(MIGRATIONS / migration.relative_path)
    statements = _statements(MIGRATIONS / migration.relative_path)

    assert len(statements) == 2
    models, usage_logs = statements
    assert models.startswith("ALTER TABLE llm_models")
    for column in ("cache_read_price", "cache_write_price", "cache_write_1h_price"):
        assert f"ADD COLUMN IF NOT EXISTS {column} NUMERIC(20, 10) NULL" in models
    assert usage_logs.startswith("ALTER TABLE chat_usage_logs")
    for column in ("cache_read_input_tokens", "cache_creation_5m_input_tokens", "cache_creation_1h_input_tokens"):
        assert f"ADD COLUMN IF NOT EXISTS {column} INT NOT NULL DEFAULT 0" in usage_logs
        assert column in ChatUsageLog.__table__.columns
    for column in ("cache_read_price", "cache_write_price", "cache_write_1h_price"):
        assert column in LlmModel.__table__.columns


async def _execute_with_usage_event(monkeypatch, usage_event: dict, *, component_prices: dict | None = None):
    """Run the durable worker with one engine usage event and return its usage record."""
    import asyncio

    run = SimpleNamespace(
        id="run-1",
        model_name="model",
        project_id="project-1",
        user_id="user-1",
        conversation_id=None,
        user_message_id=None,
        assistant_message_id=None,
        capability_snapshot={},
        pricing_snapshot={
            **_frozen_snapshot(_CACHE_PRICES),
            "component_prices": component_prices or {},
            "margin_multiplier": "1",
            "chat_credit_per_usd": "1",
        },
        execution_protocol_version=1,
        status="queued",
        lease_owner="worker-1#1",
        assigned_resource_id=None,
        parent_run_id=None,
        credit_ceiling=None,
        sandbox_seconds_ceiling=None,
        depth=0,
    )

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def begin(self):
            return self

    finished: list[dict] = []

    async def engine_stream(**_kwargs):
        yield {"type": "token", "text": "완료"}
        yield usage_event

    async def finish(_run_id, **kwargs):
        finished.append(kwargs)

    async def _return(value):
        return value

    monkeypatch.setattr(execution, "_factory", lambda: lambda: _Session())
    monkeypatch.setattr(execution, "claim_queued_run", lambda *_args, **_kwargs: _return(run))
    monkeypatch.setattr(
        execution,
        "_payload",
        lambda _run: {"input_messages": [{"role": "user", "content": "x"}], "features": {}},
    )
    monkeypatch.setattr(execution, "_validate_run_protocol_payload", lambda *_args: True)
    monkeypatch.setattr(
        execution.ps,
        "resolve_model_snapshot",
        lambda *_args, **_kwargs: _return({"model_name": "model", "provider_name": "provider"}),
    )
    monkeypatch.setattr(execution, "_managed_tool_configs", lambda *_args, **_kwargs: _return((None, None, None)))
    monkeypatch.setattr(execution.engine, "stream", engine_stream)
    monkeypatch.setattr(execution, "_append", lambda *_args, **_kwargs: _return(None))
    monkeypatch.setattr(execution, "_finish", finish)
    monkeypatch.setattr(execution, "_set_stage", lambda *_args, **_kwargs: _return(None))
    monkeypatch.setattr(execution, "_cancel_requested", lambda _run_id: _return(False))
    monkeypatch.setattr(execution, "_renew_lease", lambda *_args, **_kwargs: _return(True))
    monkeypatch.setattr(execution.credit, "precheck", lambda *_args, **_kwargs: _return(None))
    monkeypatch.setattr(execution, "_append_temp_history", lambda *_args, **_kwargs: _return(None))

    assert await asyncio.wait_for(execution.execute_queued_run("run-1", owner="worker-1"), timeout=5) is True
    return finished[-1]["usage_record"]


async def test_executor_bills_the_usage_event_cache_split(monkeypatch):
    """The worker's usage event → frozen-snapshot cost → ledger record hop keeps the split."""
    record = await _execute_with_usage_event(
        monkeypatch, {"type": "usage", "usage": TestCacheRates._BREAKDOWN.as_usage_dict()}
    )
    assert record["prompt_tokens"] == 1000
    assert record["breakdown"] == TestCacheRates._BREAKDOWN
    assert record["usage_cost"].raw_cost == Decimal("0.0033300000")
    assert record["usage_cost"].pricing_status == "priced"
    assert {item["kind"]: item["quantity"] for item in record["usage_components"]}["input_tokens"] == "100"


class TestInterruptedAnthropicStream:
    """A /v1/messages stream cut before message_stop still bills message_start's cache split."""

    _START_USAGE = {"input_tokens": 100, "cache_read_input_tokens": 90_000, "output_tokens": 1}

    async def _interrupted_bill(self, monkeypatch, cache_prices: dict, events: list[dict], count_tokens=None) -> dict:
        async def stream():
            for event in events:
                yield event
            raise ConnectionError("upstream stream reset")

        async def fake_messages(**_kwargs):
            return stream()

        captured: dict = {}

        async def apply_usage(**kwargs):
            captured.update(kwargs)
            return Decimal("0")

        def reported_input_only(_model, *, messages=None, text=None):
            # The prompt fallback would bill 90_100 uncached tokens; it must not run.
            assert messages is None
            return 2

        monkeypatch.setattr(completion_api.litellm_client, "aanthropic_messages", fake_messages)
        monkeypatch.setattr(completion_api.litellm_client, "count_tokens", count_tokens or reported_input_only)
        monkeypatch.setattr(completion_api, "_with_passthrough_compaction", lambda options, **_kwargs: options)
        monkeypatch.setattr(completion_api.credit, "apply_usage", apply_usage)

        iterator = await completion_api.complete_anthropic(
            resolved={
                "model_name": _MODEL,
                "provider_type": "anthropic",
                "provider_name": "anthropic",
                "margin_multiplier": Decimal("1"),
                "input_price_per_token": _INPUT,
                "output_price_per_token": _OUTPUT,
                "price_source": "manual",
                **cache_prices,
            },
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=1024,
            stream=True,
            user_id="u1",
            project_id="p1",
            api_key_id=None,
            options={},
        )
        with pytest.raises(ConnectionError):
            async for _event in iterator:
                pass
        return captured

    @pytest.mark.parametrize(
        ("cache_prices", "raw_cost", "status"),
        [
            # 100×3e-6 + 2×1.5e-5; the 90_000 cache-read tokens bill 0 until a rate is set.
            (_NO_CACHE_PRICES, Decimal("0.0003300000"), "partial"),
            # + 90_000×3e-7
            (_CACHE_PRICES, Decimal("0.0273300000"), "priced"),
        ],
        ids=["rates-unset", "rates-set"],
    )
    async def test_cut_stream_keeps_message_start_cache_split(self, monkeypatch, cache_prices, raw_cost, status):
        captured = await self._interrupted_bill(
            monkeypatch,
            cache_prices,
            [
                {"type": "message_start", "message": {"usage": dict(self._START_USAGE)}},
                {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "부분"}},
            ],
        )

        breakdown = captured["breakdown"]
        assert (captured["prompt_tokens"], captured["completion_tokens"]) == (90_100, 2)
        assert breakdown.cache_read_input_tokens == 90_000
        assert breakdown.uncached_input_tokens == 100
        assert captured["usage_cost"].input_cost == Decimal("0.0003000000")
        assert captured["usage_cost"].raw_cost == raw_cost
        assert captured["usage_cost"].pricing_status == status
        row = await _ledger_row(captured["usage_cost"], breakdown)
        assert (row.prompt_tokens, row.cache_read_input_tokens) == (90_100, 90_000)

    async def test_reported_output_wins_when_it_exceeds_the_local_count(self, monkeypatch):
        captured = await self._interrupted_bill(
            monkeypatch,
            _NO_CACHE_PRICES,
            [
                {"type": "message_start", "message": {"usage": dict(self._START_USAGE)}},
                {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "부분"}},
                {"type": "message_delta", "usage": {"output_tokens": 7}},
            ],
        )
        assert captured["completion_tokens"] == 7
        assert captured["breakdown"].cache_read_input_tokens == 90_000

    async def test_adapter_zero_start_usage_counts_the_prompt_locally(self, monkeypatch):
        # LiteLLM's chat/completions and Responses /v1/messages adapters open
        # with an all-zero usage; trusting it would bill the prompt as free.
        counted: list[str] = []

        def count_tokens(_model, *, messages=None, text=None):
            counted.append("messages" if messages is not None else "text")
            return 4_321 if messages is not None else 2

        captured = await self._interrupted_bill(
            monkeypatch,
            _NO_CACHE_PRICES,
            [
                {
                    "type": "message_start",
                    "message": {
                        "usage": {
                            "input_tokens": 0,
                            "output_tokens": 0,
                            "cache_creation_input_tokens": 0,
                            "cache_read_input_tokens": 0,
                        }
                    },
                },
                {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "부분"}},
            ],
            count_tokens=count_tokens,
        )

        assert "messages" in counted
        assert (captured["prompt_tokens"], captured["completion_tokens"]) == (4_321, 2)
        assert captured["breakdown"].cache_read_input_tokens == 0
        assert captured["usage_cost"].input_cost == Decimal("0.0129630000")
        assert captured["usage_cost"].pricing_status == "priced"


class TestAdvisorCachePricing:
    """The managed advisor splits its prompt like every other billing path."""

    _ADVISOR_ROUTE = {
        "model_name": "gpt-5.6-sol",
        "provider_type": "openai",
        "input_price_per_token": _INPUT,
        "output_price_per_token": _OUTPUT,
        "cache_read_price_per_token": None,
        "cache_write_price_per_token": None,
        "cache_write_1h_price_per_token": None,
    }

    async def _advisor_usage(self, monkeypatch) -> tuple[advisor.AdvisorResult, tuple[dict, ...]]:
        async def acompletion(**_kwargs):
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="advice"))],
                usage=litellm.Usage(
                    prompt_tokens=1000, completion_tokens=10, prompt_tokens_details={"cached_tokens": 800}
                ),
            )

        async def use_allowed(*_args):
            return True

        monkeypatch.setattr(advisor.litellm_client, "acompletion", acompletion)
        monkeypatch.setattr(managed, "_managed_use_allowed", use_allowed)
        result = await advisor.ask_with_route(route=self._ADVISOR_ROUTE, goal="review", visible_messages=[])
        executed = await managed._execute_managed_advisor(
            {"goal": "review"},
            ToolContext(
                project_id="p1",
                user_id="u1",
                managed_advisor={"route": self._ADVISOR_ROUTE, "options": {"max_uses": 2}},
            ),
        )
        return result, executed.usage

    async def test_advisor_result_keeps_total_input_and_cache_split(self, monkeypatch):
        result, usage = await self._advisor_usage(monkeypatch)

        assert (result.prompt_tokens, result.completion_tokens) == (1000, 10)
        assert result.breakdown.cache_read_input_tokens == 800
        assert [(item["kind"], item["price_key"], item["quantity"]) for item in usage] == [
            ("advisor_input_tokens", "advisor_input_price_per_token", "200"),
            ("advisor_output_tokens", "advisor_output_price_per_token", "10"),
            ("advisor_cache_read_tokens", "advisor_cache_read_price_per_token", "800"),
        ]
        assert usage[-1]["prompt_tokens"] == 1000

    def test_advisor_cache_tier_uses_each_call_prompt_size(self):
        def call(prompt):
            return [
                {"kind": "advisor_input_tokens", "price_key": "advisor_input_price_per_token",
                 "unit": "token", "source": "advisor", "quantity": str(prompt - 120_000)},
                {"kind": "advisor_output_tokens", "price_key": "advisor_output_price_per_token",
                 "unit": "token", "source": "advisor", "quantity": "0"},
                {"kind": "advisor_cache_read_tokens", "price_key": "advisor_cache_read_price_per_token",
                 "unit": "token", "source": "advisor", "quantity": "120000", "prompt_tokens": prompt},
            ]

        frozen = {"component_prices": {
            "advisor_input_price_per_token": "0", "advisor_output_price_per_token": "0",
            "advisor_cache_read_price_per_token": "0.0000002",
            "advisor_cache_read_price_per_token_above_200k": "0.0000004",
        }}
        separate = call(150_000) + call(150_000)
        total, rows = execution._managed_usage_components(separate, pricing_snapshot=frozen, model_name="gemini")
        assert total == Decimal("0.0480000000")
        assert [row["unit_price_usd"] for row in rows if row["kind"] == "advisor_cache_read_tokens"] == [
            "0.0000002", "0.0000002",
        ]

        total, rows = execution._managed_usage_components(call(200_001), pricing_snapshot=frozen, model_name="gemini")
        assert total == Decimal("0.0480000000")
        assert rows[-1]["unit_price_usd"] == "0.0000004"
        with pytest.raises(execution.DurableRunError, match="prompt size"):
            execution._managed_usage_components(
                [{key: value for key, value in row.items() if key != "prompt_tokens"} for row in call(200_001)],
                pricing_snapshot=frozen, model_name="gemini",
            )


    def test_admission_freezes_only_set_advisor_cache_rates(self):
        resolved = {
            "provider_id": 1,
            "model_id": 10,
            "provider_name": "anthropic",
            "model_name": _MODEL,
            "config_version_hash": "hash",
            "input_price_per_token": _INPUT,
            "output_price_per_token": _OUTPUT,
            **_NO_CACHE_PRICES,
        }
        advisor_route = {
            **self._ADVISOR_ROUTE,
            "provider_id": 2,
            "model_id": 20,
            "provider_name": "openai",
            "config_version_hash": "advisor-hash",
            "cache_read_price_per_token": Decimal("0.0000003"),
        }
        _capability, pricing_snapshot = _run_snapshots(resolved, {}, feature_routes={"advisor": advisor_route})
        prices = pricing_snapshot["component_prices"]

        assert Decimal(prices["advisor_cache_read_price_per_token"]) == Decimal("0.0000003")
        assert "advisor_cache_write_price_per_token" not in prices
        assert "advisor_cache_write_1h_price_per_token" not in prices

    @pytest.mark.parametrize(
        ("component_prices", "cost", "unpriced"),
        [
            # 200×3e-6 + 10×1.5e-5; the 800 cached tokens bill 0 without a rate.
            ({}, Decimal("0.0007500000"), True),
            # + 800×3e-7
            ({"advisor_cache_read_price_per_token": "0.0000003"}, Decimal("0.0009900000"), False),
        ],
        ids=["rate-unset", "rate-set"],
    )
    async def test_managed_pricing_bills_cache_at_its_rate_or_zero(self, monkeypatch, component_prices, cost, unpriced):
        _result, usage = await self._advisor_usage(monkeypatch)
        total, components = execution._managed_usage_components(
            list(usage),
            pricing_snapshot={
                "component_prices": {
                    "advisor_input_price_per_token": str(_INPUT),
                    "advisor_output_price_per_token": str(_OUTPUT),
                    **component_prices,
                }
            },
            model_name="executor",
        )

        assert total == cost
        cache = next(item for item in components if item["kind"] == "advisor_cache_read_tokens")
        assert cache["metadata"] == ({"unpriced": True} if unpriced else {})
        assert all(
            Decimal(item["quantity"]) * Decimal(item["unit_price_usd"]) == Decimal(item["cost_usd"])
            for item in components
        )
        for component in components:
            UsageComponent.model_validate(component)

    def test_missing_required_advisor_price_still_fails_closed(self):
        with pytest.raises(execution.DurableRunError):
            execution._managed_usage_components(
                [
                    {
                        "kind": "advisor_input_tokens",
                        "price_key": "advisor_input_price_per_token",
                        "quantity": "10",
                        "unit": "token",
                        "source": "advisor",
                    }
                ],
                pricing_snapshot={"component_prices": {}},
                model_name="executor",
            )

    @pytest.mark.parametrize(
        ("component_prices", "raw_cost", "status"),
        [
            # executor 0.00333 + advisor 0.00075 (cache read free)
            ({}, Decimal("0.0040800000"), "partial"),
            ({"advisor_cache_read_price_per_token": "0.0000003"}, Decimal("0.0043200000"), "priced"),
        ],
        ids=["rate-unset", "rate-set"],
    )
    async def test_executor_marks_unpriced_advisor_cache_partial(self, monkeypatch, component_prices, raw_cost, status):
        _result, usage = await self._advisor_usage(monkeypatch)
        record = await _execute_with_usage_event(
            monkeypatch,
            {"type": "usage", "usage": TestCacheRates._BREAKDOWN.as_usage_dict(), "tool_usage": list(usage)},
            component_prices={
                "advisor_input_price_per_token": str(_INPUT),
                "advisor_output_price_per_token": str(_OUTPUT),
                **component_prices,
            },
        )

        assert record["usage_cost"].raw_cost == raw_cost
        assert record["usage_cost"].pricing_status == status
        kinds = {item["kind"]: item["quantity"] for item in record["usage_components"]}
        assert (kinds["advisor_input_tokens"], kinds["advisor_cache_read_tokens"]) == ("200", "800")
