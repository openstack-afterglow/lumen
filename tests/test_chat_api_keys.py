"""외부 API 키 — 저장소 순수 로직 + 인증 의존성 + 라우터 테스트.

- 순수 로직은 DB 없이 검증하고 store transaction은 fake session으로 실제 mutation을 검사한다.
- 인증 의존성은 verify_key monkeypatch로 fail-closed 401을 검증한다.
- 라우터는 store monkeypatch로 발급 평문 1회·목록 마스킹·폐기/한도 소유 컨텍스트를 검증한다.
"""

from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from lumen import auth as deps
from lumen.api import completions
from lumen.main import app
from lumen.models.chat_contracts import ChatFeatureOptions, TextPart
from lumen.services import api_key_store as aks

_USER_KEYS = "/api/v1/chat/api-keys"


def _request(**headers: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [(key.replace("_", "-").lower().encode(), value.encode()) for key, value in headers.items()],
        }
    )


class TestStorePureLogic:
    def test_hash_deterministic_and_hex(self):
        h1 = aks._hash_key("sk-afgl-abc")
        h2 = aks._hash_key("sk-afgl-abc")
        assert h1 == h2 and len(h1) == 64
        assert h1 != aks._hash_key("sk-afgl-abd")

    def test_public_never_leaks_hash(self):
        row = SimpleNamespace(
            id=1,
            name="n",
            key_prefix="sk-afgl-AbCd",
            key_hash="x" * 64,
            is_active=True,
            last_used_at=None,
            created_at=None,
            scopes=["models:read"],
            revoked_at=None,
            owner_monthly_credit_limit=Decimal("50.5"),
            admin_monthly_credit_limit=Decimal("100.0"),
        )
        pub = aks._public(row, system_quota=Decimal("200.0"), month_usage=Decimal("12.34"))
        assert "key_hash" not in pub and "key" not in pub
        assert pub["key_prefix"] == "sk-afgl-AbCd"
        assert pub["owner_monthly_credit_limit"] == "50.5"
        assert pub["admin_monthly_credit_limit"] == "100.0"
        assert pub["system_monthly_credit_limit"] == "200.0"
        assert pub["effective_monthly_credit_limit"] == "50.5"
        assert pub["month_credited_cost"] == "12.34"

    def test_public_admin_includes_owner_ids(self):
        row = SimpleNamespace(
            id=1,
            name="n",
            key_prefix="sk-afgl-AbCd",
            key_hash="x" * 64,
            is_active=True,
            last_used_at=None,
            created_at=None,
            scopes=["models:read"],
            revoked_at=None,
            owner_user_id="u-admin-test",
            owner_project_id="p-admin-test",
            owner_monthly_credit_limit=None,
            admin_monthly_credit_limit=None,
        )
        pub = aks._public_admin(row, system_quota=Decimal("100.0"), month_usage=Decimal("0"))
        assert pub["owner_user_id"] == "u-admin-test"
        assert pub["owner_project_id"] == "p-admin-test"
        assert pub["owner_monthly_credit_limit"] is None
        assert pub["admin_monthly_credit_limit"] is None
        assert pub["system_monthly_credit_limit"] == "100.0"
        assert pub["effective_monthly_credit_limit"] == "100.0"
        assert pub["month_credited_cost"] == "0"

    def test_calculate_effective_limit_precedence_and_null_handling(self):
        assert aks.calculate_effective_limit(Decimal("10"), Decimal("20"), Decimal("30")) == Decimal("10")
        assert aks.calculate_effective_limit(Decimal("50"), Decimal("20"), Decimal("30")) == Decimal("20")
        assert aks.calculate_effective_limit(Decimal("50"), None, Decimal("30")) == Decimal("30")
        assert aks.calculate_effective_limit(None, None, Decimal("30")) == Decimal("30")
        assert aks.calculate_effective_limit(Decimal("10"), None, Decimal("0")) == Decimal("10")
        assert aks.calculate_effective_limit(None, None, Decimal("0")) is None
        assert aks.calculate_effective_limit(None, None, None) is None

    def test_validate_monthly_credit_limit(self):
        assert aks._validate_monthly_credit_limit(None) is None
        assert aks._validate_monthly_credit_limit(Decimal("10.5")) == Decimal("10.5")
        assert aks._validate_monthly_credit_limit("50") == Decimal("50")
        with pytest.raises(ValueError, match="유한한"):
            aks._validate_monthly_credit_limit(Decimal("0"))
        with pytest.raises(ValueError, match="유한한"):
            aks._validate_monthly_credit_limit(Decimal("-5"))
        with pytest.raises(ValueError, match="유한한"):
            aks._validate_monthly_credit_limit(Decimal("NaN"))

    def test_validate_monthly_credit_limit_boundaries(self):
        # Maximum representable values
        assert aks._validate_monthly_credit_limit(Decimal("9999999999.99999999")) == Decimal("9999999999.99999999")
        assert aks._validate_monthly_credit_limit("9999999999.99999999") == Decimal("9999999999.99999999")
        assert aks._validate_monthly_credit_limit(Decimal("9999999999.999999990000")) == Decimal("9999999999.999999990000")

        # 1E+10 and 1E+18 overflow
        with pytest.raises(ValueError):
            aks._validate_monthly_credit_limit("1E+10")
        with pytest.raises(ValueError):
            aks._validate_monthly_credit_limit(Decimal("10000000000"))
        with pytest.raises(ValueError):
            aks._validate_monthly_credit_limit("1E+18")
        with pytest.raises(ValueError):
            aks._validate_monthly_credit_limit(Decimal("1E+18"))

        # 1E-9 excessive scale
        with pytest.raises(ValueError):
            aks._validate_monthly_credit_limit("1E-9")
        with pytest.raises(ValueError):
            aks._validate_monthly_credit_limit(Decimal("0.000000001"))

        # Trailing-zero exact values
        assert aks._validate_monthly_credit_limit(Decimal("100.500000000000")) == Decimal("100.500000000000")
        assert aks._validate_monthly_credit_limit("100.500000000000") == Decimal("100.500000000000")

    async def test_update_admin_limits_clamps_owner_limit_after_flush(self, monkeypatch):
        class FakeSession:
            def __init__(self, row):
                self.row = row
                self.flushed = False

            async def get(self, model, key_id):
                return self.row if key_id == self.row.id else None

            async def flush(self):
                self.flushed = True

            def begin(self):
                class _BeginCM:
                    async def __aenter__(self):
                        return None
                    async def __aexit__(self, exc_type, exc_val, exc_tb):
                        return False
                return _BeginCM()

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                return False

        row = SimpleNamespace(
            id=1,
            key_prefix="sk-afgl-test",
            name="test-key",
            scopes=["models:read"],
            is_active=True,
            created_at=None,
            last_used_at=None,
            revoked_at=None,
            owner_user_id="u-owner",
            owner_project_id="p-owner",
            owner_monthly_credit_limit=Decimal("500.0"),
            admin_monthly_credit_limit=None,
        )

        session = FakeSession(row)

        monkeypatch.setattr(aks, "_require_db", lambda: (lambda: session))

        async def fake_quota(s, u):
            return Decimal("1000.0")

        async def fake_cost(s, k, dt):
            return Decimal("0")

        monkeypatch.setattr(aks, "_get_system_quota", fake_quota)
        monkeypatch.setattr(aks, "_get_month_credited_cost", fake_cost)

        result = await aks.update_admin_limits(1, Decimal("100.0"))

        assert session.flushed is True
        assert row.admin_monthly_credit_limit == Decimal("100.0")
        assert row.owner_monthly_credit_limit == Decimal("100.0")
        assert result["admin_monthly_credit_limit"] == "100.0"
        assert result["owner_monthly_credit_limit"] == "100.0"

    def test_scope_validation_fails_closed(self):
        assert aks._valid_scopes([]) is None
        assert aks._valid_scopes(["not-a-scope"]) is None
        assert aks._valid_scopes(["models:read", "models:read"]) is None
        assert aks._valid_scopes(list(aks.DEFAULT_API_KEY_SCOPES)) == aks.DEFAULT_API_KEY_SCOPES

    async def test_verify_rejects_non_prefix(self):
        assert await aks.verify_key("") is None
        assert await aks.verify_key("bogus-token") is None


class TestApiKeyAuthDependency:
    async def test_missing_key_401(self):
        req = SimpleNamespace(state=SimpleNamespace())
        with pytest.raises(HTTPException) as ei:
            await deps.get_api_key_info(req, bearer=None, x_api_key=None)
        assert ei.value.status_code == 401

    async def test_invalid_key_401(self, monkeypatch):
        async def fake_verify(raw):
            return None

        monkeypatch.setattr(aks, "verify_key", fake_verify)
        req = SimpleNamespace(state=SimpleNamespace())
        with pytest.raises(HTTPException) as ei:
            await deps.get_api_key_info(
                req,
                bearer=deps.HTTPAuthorizationCredentials(scheme="Bearer", credentials="sk-afgl-bad"),
                x_api_key=None,
            )
        assert ei.value.status_code == 401

    async def test_valid_bearer_sets_token_info(self, monkeypatch):
        async def fake_verify(raw):
            assert raw == "sk-afgl-good"
            return {
                "user_id": "u1",
                "project_id": "p1",
                "api_key_id": 7,
                "scopes": ("models:read",),
            }

        monkeypatch.setattr(aks, "verify_key", fake_verify)
        req = SimpleNamespace(state=SimpleNamespace())
        await deps.get_api_key_info(
            req,
            bearer=deps.HTTPAuthorizationCredentials(scheme="Bearer", credentials=" sk-afgl-good "),
            x_api_key=None,
        )
        assert req.state.token_info["source"] == "api"

    async def test_valid_x_api_key_header(self, monkeypatch):
        async def fake_verify(raw):
            assert raw == "sk-afgl-viakey"
            return {
                "user_id": "u1",
                "project_id": "p1",
                "api_key_id": 9,
                "scopes": ("models:read",),
            }

        monkeypatch.setattr(aks, "verify_key", fake_verify)
        req = SimpleNamespace(state=SimpleNamespace())
        info = await deps.get_api_key_info(req, bearer=None, x_api_key="sk-afgl-viakey")
        assert info["api_key_id"] == 9


class TestPrincipalDependency:
    async def test_api_key_principal_enforces_owner_project(self, monkeypatch):
        async def fake_verify(raw):
            assert raw == "sk-afgl-key"
            return {
                "user_id": "u1",
                "project_id": "p1",
                "api_key_id": 7,
                "scopes": ("models:read",),
            }

        monkeypatch.setattr(aks, "verify_key", fake_verify)
        principal = await deps.get_principal(_request(authorization="Bearer sk-afgl-key"))
        assert principal["auth_type"] == "api_key"
        assert principal["project_id"] == "p1"
        assert principal["roles"] == []
        with pytest.raises(HTTPException, match="API 키 프로젝트와 X-Project-Id가 일치하지 않습니다") as exc_info:
            await deps.get_principal(_request(x_api_key="sk-afgl-key", x_project_id="other"))
        assert exc_info.value.status_code == 403

    async def test_multiple_credentials_are_rejected(self):
        with pytest.raises(HTTPException, match="여러 인증 credential을 동시에 보낼 수 없습니다") as exc_info:
            await deps.get_principal(_request(x_api_key="sk-afgl-key", x_auth_token="ks-token"))
        assert exc_info.value.status_code == 400

    async def test_missing_scope_is_reported_in_sorted_order(self):
        dependency = deps.require_scopes("native:runs:write", "native:conversations:write")
        with pytest.raises(HTTPException, match="native:conversations:write, native:runs:write") as exc_info:
            await dependency(
                {
                    "auth_type": "api_key",
                    "user_id": "u1",
                    "project_id": "p1",
                    "api_key_id": 7,
                    "scopes": (),
                    "source": "api",
                    "roles": [],
                    "is_system_admin": False,
                }
            )
        assert exc_info.value.status_code == 403

    def test_native_defaults_require_memory_and_tool_scopes(self):
        with pytest.raises(
            HTTPException, match="native:memory:read, native:memory:write, native:tools:execute"
        ) as exc_info:
            completions._require_native_admission_scopes(
                {
                    "auth_type": "api_key",
                    "user_id": "u1",
                    "project_id": "p1",
                    "api_key_id": 7,
                    "scopes": (),
                    "source": "api",
                    "roles": [],
                    "is_system_admin": False,
                },
                ChatFeatureOptions(),
                parts=[TextPart(type="text", text="hello")],
                execution_mode="chat",
                skill_ids=[],
                agent=None,
                extension_selection={"tools": [], "mcp": []},
            )
        assert exc_info.value.status_code == 403

    def test_native_api_key_rejects_non_text_input(self):
        with pytest.raises(HTTPException, match="text chat만 지원합니다") as exc_info:
            completions._require_native_admission_scopes(
                {
                    "auth_type": "api_key",
                    "user_id": "u1",
                    "project_id": "p1",
                    "api_key_id": 7,
                    "scopes": ("native:memory:read", "native:memory:write", "native:tools:execute"),
                    "source": "api",
                    "roles": [],
                    "is_system_admin": False,
                },
                ChatFeatureOptions(),
                parts=[SimpleNamespace(type="image")],
                execution_mode="chat",
                skill_ids=[],
                agent=None,
                extension_selection={"tools": [], "mcp": []},
            )
        assert exc_info.value.status_code == 403


class TestRouter:
    async def test_create_returns_plaintext_once(self, client, monkeypatch):
        async def fake_create(user_id, project_id, name, scopes, monthly_credit_limit=None):
            return {
                "id": 1,
                "name": name,
                "key_prefix": "sk-afgl-AbCd",
                "scopes": scopes,
                "key": "sk-afgl-FULLSECRET",
            }

        monkeypatch.setattr(aks, "create_key", fake_create)
        resp = await client.post(_USER_KEYS, json={"name": "my key"})
        assert resp.status_code == 201
        assert resp.json()["key"] == "sk-afgl-FULLSECRET"

    async def test_create_forwarding_monthly_limit(self, client, monkeypatch):
        captured = {}

        async def fake_create(user_id, project_id, name, scopes, monthly_credit_limit=None):
            captured.update(
                user_id=user_id,
                project_id=project_id,
                name=name,
                scopes=scopes,
                monthly_credit_limit=monthly_credit_limit,
            )
            return {
                "id": 1,
                "name": name,
                "key_prefix": "sk-afgl-AbCd",
                "scopes": scopes,
                "owner_monthly_credit_limit": "50.0",
                "admin_monthly_credit_limit": None,
                "system_monthly_credit_limit": "100.0",
                "effective_monthly_credit_limit": "50.0",
                "month_credited_cost": "0",
                "key": "sk-afgl-FULLSECRET",
            }

        monkeypatch.setattr(aks, "create_key", fake_create)
        resp = await client.post(_USER_KEYS, json={"name": "my key", "monthly_credit_limit": "50.0"})
        assert resp.status_code == 201
        assert captured["monthly_credit_limit"] == Decimal("50.0")
        assert resp.json()["owner_monthly_credit_limit"] == "50.0"
        assert resp.json()["effective_monthly_credit_limit"] == "50.0"

    async def test_patch_owner_limits_success_and_conflicts(self, client, monkeypatch):
        captured = {}

        async def fake_update(key_id, user_id, project_id, monthly_credit_limit):
            captured.update(key_id=key_id, user_id=user_id, project_id=project_id, limit=monthly_credit_limit)
            return {
                "id": key_id,
                "owner_monthly_credit_limit": str(monthly_credit_limit) if monthly_credit_limit else None,
                "admin_monthly_credit_limit": None,
                "system_monthly_credit_limit": "100.0",
                "effective_monthly_credit_limit": str(monthly_credit_limit) if monthly_credit_limit else "100.0",
                "month_credited_cost": "0",
            }

        monkeypatch.setattr(aks, "update_owner_limits", fake_update)

        resp = await client.patch(f"{_USER_KEYS}/1/limits", json={"monthly_credit_limit": "40.0"})
        assert resp.status_code == 200
        assert captured["limit"] == Decimal("40.0")

        resp = await client.patch(f"{_USER_KEYS}/1/limits", json={"monthly_credit_limit": None})
        assert resp.status_code == 200
        assert captured["limit"] is None

        async def fake_admin_conflict(key_id, user_id, project_id, monthly_credit_limit):
            raise aks.ApiKeyLimitConflict("API 키 월 한도는 관리자 한도를 초과할 수 없습니다")

        monkeypatch.setattr(aks, "update_owner_limits", fake_admin_conflict)
        resp = await client.patch(f"{_USER_KEYS}/1/limits", json={"monthly_credit_limit": "200.0"})
        assert resp.status_code == 409
        assert resp.json()["detail"] == "API 키 월 한도는 관리자 한도를 초과할 수 없습니다"

        async def fake_sys_conflict(key_id, user_id, project_id, monthly_credit_limit):
            raise aks.ApiKeyLimitConflict("API 키 월 한도는 시스템 월 쿼터를 초과할 수 없습니다")

        monkeypatch.setattr(aks, "update_owner_limits", fake_sys_conflict)
        resp = await client.patch(f"{_USER_KEYS}/1/limits", json={"monthly_credit_limit": "200.0"})
        assert resp.status_code == 409
        assert resp.json()["detail"] == "API 키 월 한도는 시스템 월 쿼터를 초과할 수 없습니다"

        async def fake_forbidden(key_id, user_id, project_id, monthly_credit_limit):
            raise aks.ApiKeyForbidden("소유자가 아닙니다")

        monkeypatch.setattr(aks, "update_owner_limits", fake_forbidden)
        resp = await client.patch(f"{_USER_KEYS}/1/limits", json={"monthly_credit_limit": "10.0"})
        assert resp.status_code == 403

        async def fake_not_found(key_id, user_id, project_id, monthly_credit_limit):
            raise aks.ApiKeyNotFound("100 를 찾을 수 없습니다")

        monkeypatch.setattr(aks, "update_owner_limits", fake_not_found)
        resp = await client.patch(f"{_USER_KEYS}/100/limits", json={"monthly_credit_limit": "10.0"})
        assert resp.status_code == 404

    async def test_api_key_bearer_rejected_at_keystone_boundary(self, client, monkeypatch):
        def reject_api_key_as_keystone(token: str, project_id: str = ""):
            assert token == "sk-afgl-testkey"
            raise HTTPException(status_code=401, detail="인증 실패")

        monkeypatch.setattr(deps, "validate_token", reject_api_key_as_keystone)
        token_override = app.dependency_overrides.pop(deps.require_token)
        try:
            resp = await client.patch(
                f"{_USER_KEYS}/1/limits",
                headers={"X-Auth-Token": "", "Authorization": "Bearer sk-afgl-testkey"},
                json={"monthly_credit_limit": "10.0"},
            )
        finally:
            app.dependency_overrides[deps.require_token] = token_override
        assert resp.status_code == 401

    async def test_admin_gate_for_admin_routes(self, non_admin_client):
        resp = await non_admin_client.get("/v1/admin/api-keys")
        assert resp.status_code == 403
        assert resp.json()["detail"] == "관리자 권한이 필요합니다"

        resp = await non_admin_client.patch("/v1/admin/api-keys/1/limits", json={"monthly_credit_limit": "10.0"})
        assert resp.status_code == 403
        assert resp.json()["detail"] == "관리자 권한이 필요합니다"

    async def test_admin_list_cursor_and_filter_forwarding(self, admin_client, monkeypatch):
        captured = {}

        async def fake_list_admin(owner_user_id=None, owner_project_id=None, before_id=None, limit=50):
            captured.update(
                owner_user_id=owner_user_id,
                owner_project_id=owner_project_id,
                before_id=before_id,
                limit=limit,
            )
            return {
                "items": [
                    {
                        "id": 5,
                        "name": "zero usage key",
                        "owner_user_id": "u-test",
                        "owner_project_id": "p-test",
                        "key_prefix": "sk-afgl-Zero",
                        "scopes": ["models:read"],
                        "owner_monthly_credit_limit": None,
                        "admin_monthly_credit_limit": "100.0",
                        "system_monthly_credit_limit": "500.0",
                        "effective_monthly_credit_limit": "100.0",
                        "month_credited_cost": "0",
                        "is_active": True,
                    }
                ],
                "next_before_id": 5,
            }

        monkeypatch.setattr(aks, "list_keys_admin", fake_list_admin)
        resp = await admin_client.get("/v1/admin/api-keys?owner_user_id=u-test&owner_project_id=p-test&before_id=10&limit=25")
        assert resp.status_code == 200
        assert captured == {
            "owner_user_id": "u-test",
            "owner_project_id": "p-test",
            "before_id": 10,
            "limit": 25,
        }
        body = resp.json()
        assert len(body["items"]) == 1
        assert body["items"][0]["owner_user_id"] == "u-test"
        assert body["items"][0]["month_credited_cost"] == "0"
        assert body["next_before_id"] == 5

    async def test_admin_update_limits_and_clamp(self, admin_client, monkeypatch):
        captured = {}

        async def fake_update_admin(key_id, monthly_credit_limit):
            captured.update(key_id=key_id, limit=monthly_credit_limit)
            return {
                "id": key_id,
                "owner_user_id": "u-owner",
                "owner_project_id": "p-owner",
                "owner_monthly_credit_limit": "30.0",
                "admin_monthly_credit_limit": "30.0",
                "system_monthly_credit_limit": "100.0",
                "effective_monthly_credit_limit": "30.0",
                "month_credited_cost": "0",
            }

        monkeypatch.setattr(aks, "update_admin_limits", fake_update_admin)
        resp = await admin_client.patch("/v1/admin/api-keys/1/limits", json={"monthly_credit_limit": "30.0"})
        assert resp.status_code == 200
        assert captured == {"key_id": 1, "limit": Decimal("30.0")}
        assert resp.json()["owner_monthly_credit_limit"] == "30.0"
        assert resp.json()["admin_monthly_credit_limit"] == "30.0"

        async def fake_admin_sys_conflict(key_id, monthly_credit_limit):
            raise aks.ApiKeyLimitConflict("API 키 월 한도는 시스템 월 쿼터를 초과할 수 없습니다")

        monkeypatch.setattr(aks, "update_admin_limits", fake_admin_sys_conflict)
        resp = await admin_client.patch("/v1/admin/api-keys/1/limits", json={"monthly_credit_limit": "1000.0"})
        assert resp.status_code == 409
        assert resp.json()["detail"] == "API 키 월 한도는 시스템 월 쿼터를 초과할 수 없습니다"

    async def test_list_graceful_empty_on_unavailable(self, client, monkeypatch):
        async def fake_list(user_id, project_id):
            raise aks.ApiKeyStorageUnavailable("down")

        monkeypatch.setattr(aks, "list_keys", fake_list)
        resp = await client.get(_USER_KEYS)
        assert resp.status_code == 200 and resp.json() == []

    async def test_revoke_passes_owner_context(self, client, monkeypatch):
        captured = {}

        async def fake_revoke(key_id, user_id, project_id):
            captured.update(key_id=key_id, user_id=user_id, project_id=project_id)

        monkeypatch.setattr(aks, "revoke_key", fake_revoke)
        resp = await client.delete(f"{_USER_KEYS}/5")
        assert resp.status_code == 204
        assert captured["key_id"] == 5 and captured["user_id"] == "test-user-123"

    async def test_revoke_foreign_forbidden(self, client, monkeypatch):
        async def fake_revoke(key_id, user_id, project_id):
            raise aks.ApiKeyForbidden("소유자가 아닙니다")

        monkeypatch.setattr(aks, "revoke_key", fake_revoke)
        resp = await client.delete(f"{_USER_KEYS}/999")
        assert resp.status_code == 403
