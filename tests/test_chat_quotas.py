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
            self.wallet = None

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            return False

        def begin(self):
            return self

        async def get(self, model, user_id):
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
        credit,
        "get_settings",
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
