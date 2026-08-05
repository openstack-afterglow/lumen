"""빌트인 AI 채팅 크레딧/쿼터 단위 테스트.

- credits_for_cost 순수 과금 계산(마진·환산율·음수 클램프·양자화).
- precheck fail-closed: DB 사용 불가 시 요청 거부(ChatStorageUnavailable).
(precheck/apply_usage 의 실 DB 동작은 db 마커 통합 테스트에서 검증.)
"""

from decimal import Decimal
from types import SimpleNamespace

import pytest

from lumen.services import credit


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
