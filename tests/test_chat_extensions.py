"""빌트인 AI 채팅 확장(MCP/커스텀툴) 라우터 테스트.

DB 없이 extensions_store 를 monkeypatch 하여:
- 관리자 라우터 require_admin 게이트 + scope='global' 생성
- 사용자 라우터: 생성 시 scope='user' + 소유자(token) 주입, 타 소유자 접근 403
- 검증 실패 400, 저장소 장애 503
"""

from lumen.api import extensions
from lumen.services import extensions_store as es

_ADMIN_MCP = "/api/v1/chat/admin/mcp-servers"
_ADMIN_TOOL = "/api/v1/chat/admin/custom-tools"
_ADMIN_SKILL = "/api/v1/chat/admin/skills"
_USER_MCP = "/api/v1/chat/mcp-servers"
_USER_TOOL = "/api/v1/chat/custom-tools"
_USER_SKILL = "/api/v1/chat/skills"


class TestAdminGate:
    async def test_admin_mcp_forbidden_for_non_admin(self, non_admin_client):
        assert (await non_admin_client.get(_ADMIN_MCP)).status_code == 403

    async def test_admin_tool_create_forbidden_for_non_admin(self, non_admin_client):
        resp = await non_admin_client.post(_ADMIN_TOOL, json={"name": "x", "url": "https://e.com"})
        assert resp.status_code == 403


class TestAdminGlobal:
    async def test_admin_create_mcp_is_global(self, admin_client, monkeypatch):
        captured = {}

        async def fake_create(kind, fields, **kwargs):
            captured["kind"] = kind
            captured.update(kwargs)
            return {"id": 1, "scope": kwargs.get("scope"), "name": fields.get("name")}

        monkeypatch.setattr(es, "create", fake_create)
        resp = await admin_client.post(
            _ADMIN_MCP, json={"name": "ctx7", "transport": "http", "url": "https://mcp.example"}
        )
        assert resp.status_code == 201
        assert captured["kind"] == "mcp"
        assert captured["scope"] == "global"

    async def test_admin_route_accepts_static_oauth_client_fields(self, admin_client, monkeypatch):
        captured: dict = {}

        async def fake_create(kind, fields, **kwargs):
            captured["kind"] = kind
            captured["fields"] = fields
            captured.update(kwargs)
            return {
                "id": 1,
                "scope": kwargs["scope"],
                "name": fields["name"],
                "has_oauth_client": bool(fields.get("oauth_client_id")),
                "has_oauth_client_secret": bool(fields.get("oauth_client_secret")),
            }

        monkeypatch.setattr(es, "create", fake_create)
        payload = {
            "name": "protected-mcp",
            "url": "https://mcp.example/mcp",
            "auth_mode": "oauth",
            "oauth_client_id": "administrator-client",
            "oauth_client_secret": "administrator-secret",
        }

        admin_response = await admin_client.post(_ADMIN_MCP, json=payload)
        assert admin_response.status_code == 201
        assert captured["scope"] == "global"
        assert captured["fields"]["oauth_client_id"] == "administrator-client"
        assert captured["fields"]["oauth_client_secret"] == "administrator-secret"
        assert admin_response.json()["has_oauth_client"] is True
        assert admin_response.json()["has_oauth_client_secret"] is True
        assert "oauth_client_secret" not in admin_response.json()

    async def test_admin_create_accepts_global_loading_policy(self, admin_client, monkeypatch):
        captured: dict = {}

        async def fake_create(kind, fields, **kwargs):
            captured["kind"] = kind
            captured["fields"] = fields
            return {"id": 1, "scope": kwargs["scope"], "name": fields["name"]}

        monkeypatch.setattr(es, "create", fake_create)
        response = await admin_client.post(
            _ADMIN_TOOL,
            json={"name": "weather", "url": "https://api.example/weather", "load_policy": "preloaded"},
        )

        assert response.status_code == 201
        assert captured == {
            "kind": "tool",
            "fields": {
                "name": "weather",
                "url": "https://api.example/weather",
                "load_policy": "preloaded",
            },
        }


class TestSkills:
    async def test_admin_create_skill_is_global(self, admin_client, monkeypatch):
        captured = {}

        async def fake_create(kind, fields, **kwargs):
            captured["kind"] = kind
            captured["fields"] = fields
            captured.update(kwargs)
            return {"id": 1, "scope": kwargs.get("scope"), "name": fields.get("name")}

        monkeypatch.setattr(es, "create", fake_create)
        resp = await admin_client.post(_ADMIN_SKILL, json={"name": "리뷰어", "instructions": "코드를 리뷰하라"})
        assert resp.status_code == 201
        assert captured["kind"] == "skill"
        assert captured["scope"] == "global"
        assert captured["fields"]["instructions"] == "코드를 리뷰하라"

    async def test_admin_skill_forbidden_for_non_admin(self, non_admin_client):
        resp = await non_admin_client.post(_ADMIN_SKILL, json={"name": "x", "instructions": "y"})
        assert resp.status_code == 403

    async def test_user_create_skill_scoped_to_owner(self, client, monkeypatch):
        captured = {}

        async def fake_create(kind, fields, **kwargs):
            captured["kind"] = kind
            captured.update(kwargs)
            return {"id": 9, "scope": "user", "name": fields.get("name")}

        monkeypatch.setattr(es, "create", fake_create)
        resp = await client.post(_USER_SKILL, json={"name": "요약", "instructions": "간결히 요약하라"})
        assert resp.status_code == 201
        assert captured["kind"] == "skill"
        assert captured["scope"] == "user"
        assert captured["owner_user_id"] == "test-user-123"

    async def test_user_route_forbids_static_oauth_client_fields(self, client, monkeypatch):
        async def unexpected_create(*_args, **_kwargs):
            raise AssertionError("extra OAuth client fields must fail validation before storage")

        monkeypatch.setattr(es, "create", unexpected_create)
        response = await client.post(
            _USER_MCP,
            json={
                "name": "private-mcp",
                "url": "https://mcp.example/mcp",
                "oauth_client_id": "administrator-client",
                "oauth_client_secret": "administrator-secret",
            },
        )

        assert response.status_code == 422

    async def test_user_route_forbids_static_headers(self, client, monkeypatch):
        async def unexpected_create(*_args, **_kwargs):
            raise AssertionError("user header authentication must fail validation before storage")

        monkeypatch.setattr(es, "create", unexpected_create)
        response = await client.post(
            _USER_MCP,
            json={"name": "private-mcp", "url": "https://mcp.example/mcp", "headers": {"X-Api-Key": "secret"}},
        )

        assert response.status_code == 422

    async def test_user_list_skill_graceful_empty(self, client, monkeypatch):
        async def fake_list(kind, **kwargs):
            raise es.ChatStorageUnavailable("chat DB 를 사용할 수 없습니다")

        monkeypatch.setattr(es, "list_for_user", fake_list)
        assert (await client.get(_USER_SKILL)).json() == []
        assert (await client.get(_USER_SKILL)).status_code == 200


class TestUserScope:
    async def test_user_create_tool_scoped_to_owner(self, client, monkeypatch):
        captured = {}

        async def fake_create(kind, fields, **kwargs):
            captured["kind"] = kind
            captured["fields"] = fields
            captured.update(kwargs)
            return {"id": 5, "scope": "user", "name": fields.get("name")}

        monkeypatch.setattr(es, "create", fake_create)
        resp = await client.post(
            _USER_TOOL,
            json={"name": "weather", "description": "날씨", "method": "GET", "url": "https://api.example/w"},
        )
        assert resp.status_code == 201
        assert captured["kind"] == "tool"
        assert captured["scope"] == "user"
        assert captured["owner_user_id"] == "test-user-123"
        assert captured["owner_project_id"] == "test-project-123"

    async def test_user_tool_cannot_set_global_loading_policy(self, client, monkeypatch):
        async def unexpected_create(*_args, **_kwargs):
            raise AssertionError("user policy escalation must fail validation before storage")

        monkeypatch.setattr(es, "create", unexpected_create)
        response = await client.post(
            _USER_TOOL,
            json={"name": "weather", "url": "https://api.example/weather", "load_policy": "preloaded"},
        )

        assert response.status_code == 422

    async def test_user_create_detects_oauth_without_user_secret_fields(self, client, monkeypatch):
        captured: dict = {}

        async def fake_create(kind, fields, **kwargs):
            captured["fields"] = fields
            return {"id": 8, "scope": "user", "name": fields["name"]}

        async def fake_detect(server_id, *, user_id, project_id):
            assert (server_id, user_id, project_id) == (8, "test-user-123", "test-project-123")
            return {"auth_mode": "oauth", "oauth_required": True, "oauth_connection_available": True}

        monkeypatch.setattr(es, "create", fake_create)
        monkeypatch.setattr(extensions.mcp_oauth, "detect", fake_detect)

        response = await client.post(_USER_MCP, json={"name": "public-mcp", "url": "https://mcp.example/mcp"})

        assert response.status_code == 201
        assert captured["fields"] == {"name": "public-mcp", "url": "https://mcp.example/mcp"}
        assert response.json()["oauth_required"] is True

    async def test_user_update_foreign_forbidden(self, client, monkeypatch):
        async def fake_update(kind, item_id, patch, **kwargs):
            raise es.ExtensionForbidden("소유자가 아닙니다")

        monkeypatch.setattr(es, "update", fake_update)
        resp = await client.patch(f"{_USER_MCP}/999", json={"is_active": False})
        assert resp.status_code == 403

    async def test_user_delete_passes_owner_context(self, client, monkeypatch):
        captured = {}

        async def fake_delete(kind, item_id, **kwargs):
            captured.update(kwargs)

        monkeypatch.setattr(es, "delete", fake_delete)
        resp = await client.delete(f"{_USER_TOOL}/3")
        assert resp.status_code == 204
        assert captured["requester_user_id"] == "test-user-123"
        assert captured["requester_project_id"] == "test-project-123"

    async def test_user_list_mcp_ok(self, client, monkeypatch):
        async def fake_list(kind, **kwargs):
            return [{"id": 1, "scope": "global", "name": "shared"}]

        monkeypatch.setattr(es, "list_for_user", fake_list)
        resp = await client.get(_USER_MCP)
        assert resp.status_code == 200
        assert resp.json()[0]["name"] == "shared"

    async def test_user_list_graceful_empty_on_storage_unavailable(self, client, monkeypatch):
        async def fake_list(kind, **kwargs):
            raise es.ChatStorageUnavailable("chat DB 를 사용할 수 없습니다")

        monkeypatch.setattr(es, "list_for_user", fake_list)
        assert (await client.get(_USER_MCP)).json() == []
        assert (await client.get(_USER_MCP)).status_code == 200
        assert (await client.get(_USER_TOOL)).json() == []
        assert (await client.get(_USER_TOOL)).status_code == 200


class TestErrors:
    async def test_validation_400(self, client, monkeypatch):
        async def fake_create(kind, fields, **kwargs):
            raise es.ExtensionValidationError("url 필수")

        monkeypatch.setattr(es, "create", fake_create)
        resp = await client.post(_USER_TOOL, json={"name": "bad"})
        assert resp.status_code == 400

    async def test_storage_unavailable_503(self, admin_client, monkeypatch):
        async def fake_list(kind):
            raise es.ChatStorageUnavailable("DB down")

        monkeypatch.setattr(es, "list_global", fake_list)
        resp = await admin_client.get(_ADMIN_TOOL)
        assert resp.status_code == 503

    async def test_removed_user_credential_endpoint_returns_not_found(self, client):
        response = await client.put(f"{_USER_MCP}/7/credentials", json={"values": {"Authorization": "x"}})

        assert response.status_code == 404
