"""빌트인 AI 채팅 대화/메시지 라우터 테스트.

DB 없이 conversation_store 를 monkeypatch 하여:
- 생성 시 token_info 의 project_id/user_id 가 소유자로 전달되는지(IDOR 경계)
- 타 소유자 접근 시 403(ConversationForbidden), 미존재 시 404(ConversationNotFound)
- 저장소 장애 시 503
"""

from lumen.services import conversation_store as cs
from lumen.services import provider_store

_URL = "/api/v1/chat/conversations"


def _public_conv(**over) -> dict:
    base = {
        "id": "conv-1",
        "project_id": "test-project-123",
        "user_id": "test-user-123",
        "title": None,
        "model_name": None,
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    base.update(over)
    return base


class TestCreateAndList:
    async def test_create_uses_token_owner(self, client, monkeypatch):
        captured = {}

        async def fake_create(**kwargs):
            captured.update(kwargs)
            return _public_conv(title=kwargs.get("title"))

        monkeypatch.setattr(cs, "create_conversation", fake_create)
        resp = await client.post(_URL, json={"title": "테스트 대화", "workspace_id": 77})
        assert resp.status_code == 201
        # 소유자는 반드시 token_info 에서 주입 — 클라이언트 입력이 아님.
        assert captured["project_id"] == "test-project-123"
        assert captured["user_id"] == "test-user-123"
        assert captured["workspace_id"] == 77

    async def test_list_scoped_to_owner(self, client, monkeypatch):
        captured = {}

        async def fake_list(**kwargs):
            captured.update(kwargs)
            return [_public_conv()]

        monkeypatch.setattr(cs, "list_conversations", fake_list)
        resp = await client.get(_URL)
        assert resp.status_code == 200
        assert captured["user_id"] == "test-user-123"
        assert captured["project_id"] == "test-project-123"

    async def test_search_scoped_to_owner_project(self, client, monkeypatch):
        captured = {}

        async def fake_search(**kwargs):
            captured.update(kwargs)
            return [_public_conv(title="오래된 OpenStack 대화")]

        monkeypatch.setattr(cs, "search_conversations", fake_search)
        resp = await client.get(f"{_URL}/search", params={"q": "OpenStack", "limit": 20})
        assert resp.status_code == 200
        assert resp.json()[0]["title"] == "오래된 OpenStack 대화"
        assert captured == {
            "user_id": "test-user-123",
            "project_id": "test-project-123",
            "query": "OpenStack",
            "limit": 20,
        }


class TestOwnership:
    async def test_get_forbidden_403(self, client, monkeypatch):
        async def fake_get(conv_id, **kwargs):
            raise cs.ConversationForbidden("접근 불가")

        monkeypatch.setattr(cs, "get_conversation", fake_get)
        resp = await client.get(f"{_URL}/other-project-conv")
        assert resp.status_code == 403

    async def test_get_not_found_404(self, client, monkeypatch):
        async def fake_get(conv_id, **kwargs):
            raise cs.ConversationNotFound("없음")

        monkeypatch.setattr(cs, "get_conversation", fake_get)
        resp = await client.get(f"{_URL}/nope")
        assert resp.status_code == 404

    async def test_delete_forbidden_403(self, client, monkeypatch):
        async def fake_delete(conv_id, **kwargs):
            raise cs.ConversationForbidden("접근 불가")

        monkeypatch.setattr(cs, "delete_conversation", fake_delete)
        resp = await client.delete(f"{_URL}/other-project-conv")
        assert resp.status_code == 403

    async def test_messages_forbidden_403(self, client, monkeypatch):
        async def fake_msgs(conv_id, **kwargs):
            raise cs.ConversationForbidden("접근 불가")

        monkeypatch.setattr(cs, "list_message_tree", fake_msgs)
        resp = await client.get(f"{_URL}/other-project-conv/messages")
        assert resp.status_code == 403

    async def test_create_rejects_another_users_workspace(self, client, monkeypatch):
        async def fake_create(**kwargs):
            assert kwargs["workspace_id"] == 77
            raise cs.WorkspaceForbidden("프로젝트에 접근할 권한이 없습니다")

        monkeypatch.setattr(cs, "create_conversation", fake_create)
        resp = await client.post(_URL, json={"workspace_id": 77})
        assert resp.status_code == 403

    async def test_assign_rejects_another_users_workspace(self, client, monkeypatch):
        async def fake_set(conv_id, **kwargs):
            assert conv_id == "conv-1"
            assert kwargs["workspace_id"] == 77
            raise cs.WorkspaceForbidden("프로젝트에 접근할 권한이 없습니다")

        monkeypatch.setattr(cs, "set_workspace", fake_set)
        resp = await client.patch(f"{_URL}/conv-1/workspace", json={"workspace_id": 77})
        assert resp.status_code == 403


class TestStorageUnavailable:
    async def test_list_503(self, client, monkeypatch):
        async def fake_list(**kwargs):
            raise cs.ChatStorageUnavailable("DB down")

        monkeypatch.setattr(cs, "list_conversations", fake_list)
        resp = await client.get(_URL)
        assert resp.status_code == 503


class TestAvailableModels:
    async def test_returns_active_models_without_keys(self, client, monkeypatch):
        async def fake_models(*, active_only=False):
            assert active_only is True
            return [
                {
                    "id": 1,
                    "provider_id": 1,
                    "model_name": "gpt-4o",
                    "display_name": "GPT-4o",
                    "is_active": True,
                    "input_price": 0.0000025,
                    "output_price": 0.00001,
                    "effective_capabilities": {"vision": True, "reasoning": False, "context_limit": 128000},
                    "created_at": None,
                    "updated_at": None,
                },
            ]

        async def fake_providers():
            return [{"id": 1, "name": "openai"}]

        monkeypatch.setattr(provider_store, "list_models", fake_models)
        monkeypatch.setattr(provider_store, "list_providers", fake_providers)
        resp = await client.get("/api/v1/chat/models")
        assert resp.status_code == 200
        body = resp.json()
        # 능력·context_limit 노출, 키/가격은 미노출
        assert body[0]["model_name"] == "gpt-4o"
        assert body[0]["display_name"] == "GPT-4o"
        assert body[0]["provider"] == "openai"
        assert body[0]["capabilities"] == {"vision": True, "reasoning": False, "context_limit": 128000}
        assert body[0]["context_limit"] == 128000
        assert "input_price" not in body[0]
        assert "api_key" not in body[0]
        assert body[0]["id"] == 1

    async def test_graceful_empty_on_storage_unavailable(self, client, monkeypatch):
        async def fake_models(*, active_only=False):
            raise provider_store.ChatStorageUnavailable("DB down")

        monkeypatch.setattr(provider_store, "list_models", fake_models)
        resp = await client.get("/api/v1/chat/models")
        assert resp.status_code == 200
        assert resp.json() == []


class TestForkAndActiveLeaf:
    async def test_set_active_leaf_delegates(self, client, monkeypatch):
        captured = {}

        async def fake_set(conv_id, **kwargs):
            captured.update(kwargs)
            captured["conv_id"] = conv_id
            return _public_conv(active_leaf_id=kwargs["message_id"])

        monkeypatch.setattr(cs, "set_active_leaf", fake_set)
        resp = await client.patch(f"{_URL}/c1/active-leaf", json={"message_id": 7})
        assert resp.status_code == 200
        assert captured["message_id"] == 7
        assert captured["user_id"] == "test-user-123"
        assert captured["project_id"] == "test-project-123"

    async def test_fork_delegates_and_returns_new(self, client, monkeypatch):
        captured = {}

        async def fake_fork(conv_id, **kwargs):
            captured.update(kwargs)
            return _public_conv(
                id="forked-1", parent_conversation_id=conv_id, forked_from_message_id=kwargs["message_id"]
            )

        monkeypatch.setattr(cs, "fork_conversation", fake_fork)
        resp = await client.post(f"{_URL}/c1/fork", json={"message_id": 5})
        assert resp.status_code == 201
        body = resp.json()
        assert body["id"] == "forked-1"
        assert body["parent_conversation_id"] == "c1"
        assert captured["message_id"] == 5 and captured["user_id"] == "test-user-123"

    async def test_fork_not_found_404(self, client, monkeypatch):
        async def fake_fork(conv_id, **kwargs):
            raise cs.ConversationNotFound("없음")

        monkeypatch.setattr(cs, "fork_conversation", fake_fork)
        resp = await client.post(f"{_URL}/c1/fork", json={"message_id": 999})
        assert resp.status_code == 404


async def test_messages_uses_backward_cursor_and_returns_page_metadata(client, monkeypatch):
    from lumen.api import conversations

    captured = {}

    async def fake_page(conv_id, **kwargs):
        captured["conv_id"] = conv_id
        captured.update(kwargs)
        return {"messages": [], "active_leaf_id": 99, "has_more": True, "next_before_id": 42}

    monkeypatch.setattr(conversations.cs, "list_message_tree", fake_page)

    response = await client.get(f"{_URL}/conv-1/messages?before_id=100&limit=40")

    assert response.status_code == 200
    assert captured == {
        "conv_id": "conv-1",
        "user_id": "test-user-123",
        "project_id": "test-project-123",
        "before_id": 100,
        "limit": 40,
    }
    assert response.json()["next_before_id"] == 42
