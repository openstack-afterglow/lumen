from decimal import Decimal

from lumen.services import credit


async def test_admin_quota_routes_require_admin(non_admin_client):
    response = await non_admin_client.get("/v1/admin/quotas")
    assert response.status_code == 403
    assert response.json()["detail"] == "관리자 권한이 필요합니다"

    response = await non_admin_client.put(
        "/v1/admin/quotas/u1",
        json={"monthly_credit_limit": "5000", "weekly_credit_limit": None},
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "관리자 권한이 필요합니다"

    response = await non_admin_client.put(
        "/v1/admin/quotas/defaults",
        json={"monthly_credit_limit": "10000"},
    )
    assert response.status_code == 403

    response = await non_admin_client.delete("/v1/admin/quotas/u1")
    assert response.status_code == 403


async def test_admin_can_set_nullable_weekly_quota(admin_client, monkeypatch):
    captured = {}

    async def fake_set_user_quota(
        user_id,
        *,
        monthly_credit_limit,
        weekly_credit_limit,
    ):
        captured.update(
            user_id=user_id,
            monthly_credit_limit=monthly_credit_limit,
            weekly_credit_limit=weekly_credit_limit,
        )
        return {
            "user_id": user_id,
            "project_id": None,
            "monthly_credit_limit": "5000",
            "weekly_credit_limit": None,
            "month_credited_cost": "0",
            "week_credited_cost": "0",
            "is_active": True,
            "updated_at": None,
        }

    monkeypatch.setattr(credit, "set_user_quota", fake_set_user_quota)
    response = await admin_client.put(
        "/v1/admin/quotas/u1",
        json={"monthly_credit_limit": "5000", "weekly_credit_limit": None},
    )

    assert response.status_code == 200
    assert captured == {
        "user_id": "u1",
        "monthly_credit_limit": Decimal("5000"),
        "weekly_credit_limit": None,
    }


async def test_admin_can_update_default_and_reset_user(admin_client, monkeypatch):
    captured = {}

    async def fake_default(monthly_credit_limit):
        captured["default"] = monthly_credit_limit
        return {"default_monthly_credit_limit": "7500"}

    async def fake_reset(user_id):
        captured["reset"] = user_id
        return {
            "user_id": user_id,
            "project_id": None,
            "monthly_credit_limit": "7500",
            "weekly_credit_limit": "7500",
            "configured_monthly_credit_limit": None,
            "configured_weekly_credit_limit": None,
            "monthly_limit_source": "default",
            "weekly_limit_source": "default",
            "weekly_bound_by_monthly": True,
            "month_credited_cost": "0",
            "week_credited_cost": "0",
            "is_active": True,
            "updated_at": None,
        }

    monkeypatch.setattr(credit, "set_default_monthly_quota", fake_default)
    monkeypatch.setattr(credit, "reset_user_quota", fake_reset)

    response = await admin_client.put(
        "/v1/admin/quotas/defaults",
        json={"monthly_credit_limit": "7500"},
    )
    assert response.status_code == 200
    assert captured["default"] == Decimal("7500")

    response = await admin_client.delete("/v1/admin/quotas/u1")
    assert response.status_code == 200
    assert captured["reset"] == "u1"
    assert response.json()["monthly_limit_source"] == "default"


async def test_admin_rejects_weekly_limit_above_monthly(admin_client, monkeypatch):
    async def fake_set(*_args, **_kwargs):
        raise credit.QuotaLimitConflict("주간 한도는 월 한도를 초과할 수 없습니다")

    monkeypatch.setattr(credit, "set_user_quota", fake_set)
    response = await admin_client.put(
        "/v1/admin/quotas/u1",
        json={"monthly_credit_limit": "100", "weekly_credit_limit": "101"},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "주간 한도는 월 한도를 초과할 수 없습니다"


async def test_admin_quota_rejects_zero_limit(admin_client):
    response = await admin_client.put(
        "/v1/admin/quotas/u1",
        json={"monthly_credit_limit": "0", "weekly_credit_limit": None},
    )
    assert response.status_code == 422


async def test_set_user_quota_creates_wallet_and_represents_unlimited_weekly(monkeypatch):
    class ScalarResult:
        def scalar_one(self):
            return Decimal("0")

    class FakeSession:
        def __init__(self):
            self.policy = None
            self.wallet = None

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            return False

        def begin(self):
            return self

        async def get(self, model, user_id):
            if model == credit.ChatQuotaPolicy:
                return self.policy
            return self.wallet

        def add(self, wallet):
            self.wallet = wallet

        async def flush(self):
            return None

        async def execute(self, stmt):
            return ScalarResult()

    session = FakeSession()
    monkeypatch.setattr(credit, "is_db_available", lambda: True)
    monkeypatch.setattr(credit, "get_session_factory", lambda: lambda: session)
    monkeypatch.setattr(
        "lumen.services.quota_policy.get_settings",
        lambda: type(
            "Settings",
            (),
            {"chat_default_monthly_quota": 100000},
        )(),
    )

    result = await credit.set_user_quota(
        "u1",
        monthly_credit_limit=Decimal("5000"),
        weekly_credit_limit=None,
    )

    assert session.wallet is not None
    assert session.wallet.max_quota_monthly == Decimal("5000")
    assert session.wallet.max_quota_weekly == Decimal("0")
    assert result["weekly_credit_limit"] is None
    assert result["configured_weekly_credit_limit"] is None
    assert result["weekly_limit_source"] == "user"
    assert result["weekly_bound_by_monthly"] is True
