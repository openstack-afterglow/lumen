"""외부 API 키 — 저장소 순수 로직 + 인증 의존성 + 라우터 테스트.

- 순수 로직(해시/마스킹/prefix 거부)은 DB 없이.
- get_api_key_info 는 verify_key monkeypatch 로 fail-closed 401 검증.
- 라우터는 store monkeypatch 로 발급 평문 1회·목록 마스킹·폐기 소유컨텍스트 검증.
- DB 왕복(create→verify→revoke, IDOR)은 pytest.mark.db (AFTERGLOW_TEST_DATABASE_URL 필요, 없으면 skip).
"""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from lumen import auth as deps
from lumen.api import completions
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
        )
        pub = aks._public(row)
        assert "key_hash" not in pub and "key" not in pub
        assert pub["key_prefix"] == "sk-afgl-AbCd"

    def test_scope_validation_fails_closed(self):
        assert aks._valid_scopes([]) is None
        assert aks._valid_scopes(["not-a-scope"]) is None
        assert aks._valid_scopes(["models:read", "models:read"]) is None
        assert aks._valid_scopes(list(aks.DEFAULT_API_KEY_SCOPES)) == aks.DEFAULT_API_KEY_SCOPES

    async def test_verify_rejects_non_prefix(self):
        assert await aks.verify_key("") is None
        assert await aks.verify_key("bogus-token") is None  # sk-afgl- 접두 아님 → 조회 전 거부


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
        async def fake_create(user_id, project_id, name, scopes):
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
