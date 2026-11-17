"""빌트인 AI 채팅 크레딧/쿼터 단위 테스트.

- credits_for_cost 순수 과금 계산(마진·환산율·음수 클램프·양자화).
- precheck fail-closed: DB 사용 불가 시 요청 거부(ChatStorageUnavailable).
(precheck/apply_usage 의 실 DB 동작은 db 마커 통합 테스트에서 검증.)
"""

from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from lumen.services import credit
from lumen.services.usage_breakdown import UsageBreakdown


class TestCreditsForCost:
    def test_basic_margin_and_rate(self):
        # 0.002 USD × 1.5 마진 × 1000 credit_per_usd = 3 크레딧
        assert credit.credits_for_cost(0.002, 1.5, 1000) == Decimal("3.00000000")

    def test_uses_config_defaults(self, monkeypatch):
        monkeypatch.setattr(
            "lumen.services.credit.get_settings",
            lambda: SimpleNamespace(chat_credit_per_usd=1000.0, chat_default_monthly_quota=100000.0),
        )
        # margin 기본 1.0, credit_per_usd 설정값 1000 → 0.001 USD = 1 크레딧
        assert credit.credits_for_cost(0.001) == Decimal("1.00000000")

    def test_negative_clamped_to_zero(self):
        assert credit.credits_for_cost(-5, 1, 1000) == Decimal("0")

    def test_zero_cost(self):
        assert credit.credits_for_cost(0, 2.0, 1000) == Decimal("0")

    def test_quantized_to_8_places(self):
        result = credit.credits_for_cost(0.000000001, 1, 1000)  # 0.000001 크레딧
        assert result.as_tuple().exponent == -8


class TestSnapshotUsageCost:
    def test_uses_only_frozen_unit_prices(self):
        cost = credit.usage_cost_from_pricing_snapshot(
            {
                "input_price_per_token": "0.000001",
                "output_price_per_token": "0.000002",
            },
            prompt_tokens=3,
            completion_tokens=5,
        )

        assert cost.raw_cost == Decimal("0.000013")
        assert cost.input_cost == Decimal("0.000003")
        assert cost.output_cost == Decimal("0.000010")

    def test_rejects_invalid_frozen_prices(self):
        with pytest.raises(ValueError):
            credit.usage_cost_from_pricing_snapshot(
                {"input_price_per_token": "invalid", "output_price_per_token": "0"},
                prompt_tokens=1,
                completion_tokens=1,
            )

    @pytest.mark.parametrize(
        ("prompt_tokens", "tier"),
        [(200_001, False), (272_000, False), (272_001, True)],
    )
    def test_frozen_model_threshold_selects_base_and_all_cache_tiers(self, prompt_tokens, tier):
        pricing = {
            "input_price_per_token": "0.000001",
            "output_price_per_token": "0.000002",
            "input_price_per_token_above_200k": "0.000003",
            "output_price_per_token_above_200k": "0.000004",
            "long_context_threshold_tokens": 272_000,
            "cache_read_price_per_token": "0.000005",
            "cache_read_price_per_token_above_200k": "0.000006",
            "cache_write_price_per_token": "0.000007",
            "cache_write_price_per_token_above_200k": "0.000008",
            "cache_write_1h_price_per_token": "0.000009",
            "cache_write_1h_price_per_token_above_200k": "0.000010",
            "cache_price_sources": {
                "cache_read": "native_catalog",
                "cache_creation_5m": "native_catalog",
                "cache_creation_1h": "native_catalog",
            },
        }
        breakdown = UsageBreakdown.from_totals(
            prompt_tokens, 3, cache_read_input_tokens=10,
            cache_creation_5m_input_tokens=10, cache_creation_1h_input_tokens=10,
        )
        cost = credit.usage_cost_from_pricing_snapshot(
            pricing, prompt_tokens=prompt_tokens, completion_tokens=3, breakdown=breakdown,
        )
        assert cost.input_cost == (prompt_tokens - 30) * Decimal("0.000003" if tier else "0.000001")
        assert cost.output_cost == 3 * Decimal("0.000004" if tier else "0.000002")
        assert cost.cache_read_cost == 10 * Decimal("0.000006" if tier else "0.000005")
        assert cost.cache_creation_5m_cost == 10 * Decimal("0.000008" if tier else "0.000007")
        assert cost.cache_creation_1h_cost == 10 * Decimal("0.000010" if tier else "0.000009")
        assert cost.pricing_status == "priced"

    @pytest.mark.parametrize(("prompt_tokens", "tier"), [(200_000, False), (200_001, True)])
    def test_legacy_snapshot_threshold_and_manual_cache_price_stay_literal(self, prompt_tokens, tier):
        cost = credit.usage_cost_from_pricing_snapshot(
            {
                "input_price_per_token": "0.000001",
                "output_price_per_token": "0.000002",
                "input_price_per_token_above_200k": "0.000003",
                "output_price_per_token_above_200k": "0.000004",
                "cache_read_price_per_token": "0",
                "cache_read_price_per_token_above_200k": "0.000006",
                "cache_price_sources": {"cache_read": "manual"},
            },
            prompt_tokens=prompt_tokens,
            completion_tokens=1,
            breakdown=UsageBreakdown.from_totals(prompt_tokens, 1, cache_read_input_tokens=10),
        )
        assert cost.input_cost == (prompt_tokens - 10) * Decimal("0.000003" if tier else "0.000001")
        assert cost.output_cost == Decimal("0.000004" if tier else "0.000002")
        assert cost.cache_read_cost == 0

    @pytest.mark.parametrize("threshold", [None, True, 0, -1, "272000", 272000.5])
    def test_invalid_frozen_threshold_fails_closed(self, threshold):
        with pytest.raises(ValueError, match="durable pricing snapshot is invalid"):
            credit.usage_cost_from_pricing_snapshot(
                {
                    "input_price_per_token": "0",
                    "output_price_per_token": "0",
                    "long_context_threshold_tokens": threshold,
                },
                prompt_tokens=1,
                completion_tokens=1,
            )

    def test_reservation_bounds_include_frozen_base_tiers(self):
        pricing = {
            "input_price_per_token": "0",
            "output_price_per_token": "0",
            "input_price_per_token_above_200k": "0.000003",
            "output_price_per_token_above_200k": "0.000004",
            "long_context_threshold_tokens": 272_000,
            "margin_multiplier": "1",
            "chat_credit_per_usd": "1",
        }
        assert credit.worst_case_token_rates(pricing)[:2] == (Decimal("0.000003"), Decimal("0.000004"))
        assert credit.call_credit_bound(pricing, input_tokens=300_000, output_tokens=10) == Decimal("0.90004001")


class TestPrecheckFailClosed:
    async def test_raises_when_db_unavailable(self, monkeypatch):
        monkeypatch.setattr("lumen.services.credit.is_db_available", lambda: False)
        with pytest.raises(credit.ChatStorageUnavailable):
            await credit.precheck("test-user-123", "test-project-123")

    async def test_raises_when_factory_none(self, monkeypatch):
        monkeypatch.setattr("lumen.services.credit.is_db_available", lambda: True)
        monkeypatch.setattr("lumen.services.credit.get_session_factory", lambda: None)
        with pytest.raises(credit.ChatStorageUnavailable):
            await credit.precheck("test-user-123", "test-project-123")


class TestPrecheckApiKeyLimit:
    """API 키 월 사용량 한도 precheck 테스트."""

    class FakeSession:
        def __init__(self, wallet, key_row=None, month_usage=Decimal("0")):
            self.wallet = wallet
            self.key_row = key_row
            self.month_usage = month_usage
            self.executed_stmts = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

        def begin(self):
            return self

        async def get(self, model, key_id):
            if model == credit.UserWallet:
                return self.wallet
            if model == credit.ChatApiKey:
                if self.key_row and getattr(self.key_row, "id", None) == key_id:
                    return self.key_row
                return None
            return None

        def add(self, obj):
            pass

        async def flush(self):
            pass

        async def execute(self, stmt):
            self.executed_stmts.append(stmt)

            class ScalarResult:
                def __init__(self, val):
                    self.val = val

                def scalar_one(self):
                    return self.val

            return ScalarResult(self.month_usage)

    @staticmethod
    def _make_wallet(max_quota=Decimal("100"), used_quota=Decimal("0"), period_start=None, is_active=True):
        if period_start is None:
            period_start = credit._first_of_month(datetime.now(UTC).date())
        return SimpleNamespace(
            is_active=is_active,
            max_quota_monthly=max_quota,
            max_quota_weekly=Decimal("0"),
            used_quota_this_month=used_quota,
            quota_period_start=period_start,
        )

    def _mock_db(self, monkeypatch, session):
        monkeypatch.setattr("lumen.services.credit.is_db_available", lambda: True)
        monkeypatch.setattr("lumen.services.credit.get_session_factory", lambda: lambda: session)

    async def test_no_key_preserves_old_behavior(self, monkeypatch):
        wallet = self._make_wallet(max_quota=Decimal("100"), used_quota=Decimal("0"))
        session = self.FakeSession(wallet=wallet)
        self._mock_db(monkeypatch, session)

        await credit.precheck("u1", "p1", api_key_id=None)
        assert len(session.executed_stmts) == 0

    async def test_unlimited_weekly_remains_bounded_by_monthly(self, monkeypatch):
        wallet = self._make_wallet(
            max_quota=Decimal("100"),
            used_quota=Decimal("100"),
        )
        wallet.max_quota_weekly = Decimal("0")
        session = self.FakeSession(wallet=wallet)
        self._mock_db(monkeypatch, session)

        with pytest.raises(credit.QuotaExceeded, match="월 사용 한도를 초과했습니다"):
            await credit.precheck("u1", "p1", api_key_id=None)
        assert session.executed_stmts == []

    async def test_weekly_wallet_limit_rejects_from_ledger(self, monkeypatch):
        wallet = self._make_wallet(max_quota=Decimal("100"), used_quota=Decimal("0"))
        wallet.max_quota_weekly = Decimal("10")
        session = self.FakeSession(wallet=wallet, month_usage=Decimal("10"))
        self._mock_db(monkeypatch, session)

        with pytest.raises(credit.QuotaExceeded, match="주간 사용 한도를 초과했습니다"):
            await credit.precheck("u1", "p1", api_key_id=None)
        assert len(session.executed_stmts) == 1

    async def test_weekly_api_key_limit_rejects_from_ledger(self, monkeypatch):
        wallet = self._make_wallet(max_quota=Decimal("100"), used_quota=Decimal("0"))
        key = SimpleNamespace(
            id=10,
            owner_user_id="u1",
            owner_project_id="p1",
            is_active=True,
            revoked_at=None,
            owner_monthly_credit_limit=None,
            admin_monthly_credit_limit=None,
            owner_weekly_credit_limit=Decimal("5"),
        )
        session = self.FakeSession(wallet=wallet, key_row=key, month_usage=Decimal("5"))
        self._mock_db(monkeypatch, session)

        with pytest.raises(credit.QuotaExceeded, match="API 키 주간 사용 한도를 초과했습니다"):
            await credit.precheck("u1", "p1", api_key_id=10)
        assert len(session.executed_stmts) == 2

    async def test_below_limit_pass(self, monkeypatch):
        wallet = self._make_wallet(max_quota=Decimal("100"), used_quota=Decimal("0"))
        key = SimpleNamespace(
            id=10,
            owner_user_id="u1",
            owner_project_id="p1",
            is_active=True,
            revoked_at=None,
            owner_monthly_credit_limit=Decimal("50"),
            admin_monthly_credit_limit=None,
            owner_weekly_credit_limit=None,
        )
        session = self.FakeSession(wallet=wallet, key_row=key, month_usage=Decimal("49.99"))
        self._mock_db(monkeypatch, session)

        await credit.precheck("u1", "p1", api_key_id=10)
        assert len(session.executed_stmts) == 1

    async def test_equal_or_over_rejection_with_exact_message(self, monkeypatch):
        wallet = self._make_wallet(max_quota=Decimal("100"), used_quota=Decimal("0"))
        key = SimpleNamespace(
            id=10,
            owner_user_id="u1",
            owner_project_id="p1",
            is_active=True,
            revoked_at=None,
            owner_monthly_credit_limit=Decimal("50"),
            admin_monthly_credit_limit=None,
            owner_weekly_credit_limit=None,
        )

        session_eq = self.FakeSession(wallet=wallet, key_row=key, month_usage=Decimal("50.00"))
        self._mock_db(monkeypatch, session_eq)
        with pytest.raises(credit.QuotaExceeded, match="API 키 월 사용 한도를 초과했습니다"):
            await credit.precheck("u1", "p1", api_key_id=10)

        session_over = self.FakeSession(wallet=wallet, key_row=key, month_usage=Decimal("50.01"))
        self._mock_db(monkeypatch, session_over)
        with pytest.raises(credit.QuotaExceeded, match="API 키 월 사용 한도를 초과했습니다"):
            await credit.precheck("u1", "p1", api_key_id=10)

    async def test_prior_month_exclusion(self, monkeypatch):
        wallet = self._make_wallet(max_quota=Decimal("100"), used_quota=Decimal("0"))
        key = SimpleNamespace(
            id=10,
            owner_user_id="u1",
            owner_project_id="p1",
            is_active=True,
            revoked_at=None,
            owner_monthly_credit_limit=Decimal("50"),
            admin_monthly_credit_limit=None,
            owner_weekly_credit_limit=None,
        )
        session = self.FakeSession(wallet=wallet, key_row=key, month_usage=Decimal("10"))
        self._mock_db(monkeypatch, session)

        await credit.precheck("u1", "p1", api_key_id=10)
        assert len(session.executed_stmts) == 1
        stmt = session.executed_stmts[0]
        compiled_sql = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        month_str = datetime.now(UTC).strftime("%Y-%m")
        assert "created_at >=" in compiled_sql
        assert month_str in compiled_sql
        assert "source =" in compiled_sql or "source" in compiled_sql
        assert "api_key_id =" in compiled_sql or "api_key_id" in compiled_sql

    async def test_month_reset_behavior(self, monkeypatch):
        past_start = date(2020, 1, 1)
        wallet = SimpleNamespace(
            is_active=True,
            max_quota_monthly=Decimal("100"),
            max_quota_weekly=Decimal("0"),
            used_quota_this_month=Decimal("100"),
            quota_period_start=past_start,
        )
        session = self.FakeSession(wallet=wallet)
        self._mock_db(monkeypatch, session)

        await credit.precheck("u1", "p1", api_key_id=None)
        assert wallet.used_quota_this_month == Decimal("0")
        assert wallet.quota_period_start == credit._first_of_month(datetime.now(UTC).date())

    async def test_missing_or_mismatched_key_fail_closed(self, monkeypatch):
        wallet = self._make_wallet(max_quota=Decimal("100"), used_quota=Decimal("0"))

        session_missing = self.FakeSession(wallet=wallet, key_row=None)
        self._mock_db(monkeypatch, session_missing)
        with pytest.raises(credit.ChatStorageUnavailable):
            await credit.precheck("u1", "p1", api_key_id=99)

        key_bad_user = SimpleNamespace(
            id=10,
            owner_user_id="u2",
            owner_project_id="p1",
            is_active=True,
            revoked_at=None,
            owner_monthly_credit_limit=Decimal("50"),
            admin_monthly_credit_limit=None,
            owner_weekly_credit_limit=None,
        )
        session_bad_user = self.FakeSession(wallet=wallet, key_row=key_bad_user)
        self._mock_db(monkeypatch, session_bad_user)
        with pytest.raises(credit.ChatStorageUnavailable):
            await credit.precheck("u1", "p1", api_key_id=10)

        key_bad_proj = SimpleNamespace(
            id=10,
            owner_user_id="u1",
            owner_project_id="p2",
            is_active=True,
            revoked_at=None,
            owner_monthly_credit_limit=Decimal("50"),
            admin_monthly_credit_limit=None,
            owner_weekly_credit_limit=None,
        )
        session_bad_proj = self.FakeSession(wallet=wallet, key_row=key_bad_proj)
        self._mock_db(monkeypatch, session_bad_proj)
        with pytest.raises(credit.ChatStorageUnavailable):
            await credit.precheck("u1", "p1", api_key_id=10)

        key_revoked = SimpleNamespace(
            id=10,
            owner_user_id="u1",
            owner_project_id="p1",
            is_active=True,
            revoked_at=datetime.now(UTC),
            owner_monthly_credit_limit=Decimal("50"),
            admin_monthly_credit_limit=None,
            owner_weekly_credit_limit=None,
        )
        session_revoked = self.FakeSession(wallet=wallet, key_row=key_revoked)
        self._mock_db(monkeypatch, session_revoked)
        with pytest.raises(credit.ChatStorageUnavailable):
            await credit.precheck("u1", "p1", api_key_id=10)

    async def test_effective_min_combinations(self, monkeypatch):
        cases = [
            (Decimal("10"), Decimal("20"), Decimal("30"), Decimal("10"), True),
            (Decimal("10"), Decimal("20"), Decimal("30"), Decimal("9.9"), False),
            (Decimal("20"), Decimal("10"), Decimal("30"), Decimal("10"), True),
            (Decimal("20"), Decimal("10"), Decimal("30"), Decimal("9.9"), False),
            (Decimal("30"), Decimal("20"), Decimal("10"), Decimal("10"), True),
            (Decimal("30"), Decimal("20"), Decimal("10"), Decimal("9.9"), False),
            (Decimal("10"), None, Decimal("30"), Decimal("10"), True),
            (None, Decimal("10"), Decimal("30"), Decimal("10"), True),
            (Decimal("10"), Decimal("10"), Decimal("0"), Decimal("10"), True),
            (Decimal("10"), Decimal("10"), Decimal("0"), Decimal("9.9"), False),
            (None, None, Decimal("0"), Decimal("9999"), False),
        ]

        for owner, admin, sys_q, usage, expect_reject in cases:
            wallet = self._make_wallet(max_quota=sys_q, used_quota=Decimal("0"))
            key = SimpleNamespace(
                id=1,
                owner_user_id="u1",
                owner_project_id="p1",
                is_active=True,
                revoked_at=None,
                owner_monthly_credit_limit=owner,
                admin_monthly_credit_limit=admin,
                owner_weekly_credit_limit=None,
            )
            session = self.FakeSession(wallet=wallet, key_row=key, month_usage=usage)
            self._mock_db(monkeypatch, session)

            if expect_reject:
                with pytest.raises(credit.QuotaExceeded, match="API 키 월 사용 한도를 초과했습니다"):
                    await credit.precheck("u1", "p1", api_key_id=1)
            else:
                await credit.precheck("u1", "p1", api_key_id=1)
