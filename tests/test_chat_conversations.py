"""빌트인 AI 채팅 대화/메시지 라우터 테스트.

DB 없이 conversation_store 를 monkeypatch 하여:
- 생성 시 token_info 의 project_id/user_id 가 소유자로 전달되는지(IDOR 경계)
- 타 소유자 접근 시 403(ConversationForbidden), 미존재 시 404(ConversationNotFound)
- 저장소 장애 시 503
"""

import pytest

from lumen.services import conversation_store as cs
from lumen.services.providers import errors, repository

_URL = "/api/v1/chat/conversations"


def _public_conv(**over) -> dict:
    base = {
        "id": "conv-1",
        "project_id": "test-project-123",
        "user_id": "test-user-123",
        "title": None,
        "title_source": "auto",
        "title_status": "idle",
        "title_revision": 0,
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
        # Domain authority comes only from the verified principal, never caller input.
        resp = await client.get(
            f"{_URL}/search", params={"q": "OpenStack", "limit": 20, "domain_id": "caller-domain"},
            headers={"X-Domain-Id": "caller-domain"},
        )
        assert resp.status_code == 200
        assert resp.json()[0]["title"] == "오래된 OpenStack 대화"
        assert captured == {
            "user_id": "test-user-123",
            "project_id": "test-project-123",
            "domain_id": "test-domain-123",
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
        async def fake_get(conv_id, **kwargs):
            raise cs.ConversationForbidden("접근 불가")

        monkeypatch.setattr(cs, "get_conversation", fake_get)
        resp = await client.get(f"{_URL}/other-project-conv/messages", params={"cursor": "malformed"})
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
                    "api_model_name": "gpt-4o",
                    "api_provider": "openai",
                    "display_name": "GPT-4o",
                    "is_active": True,
                    "input_price": 0.0000025,
                    "output_price": 0.00001,
                    "provider_family": "native",
                    "price_origin": "native_catalog",
                    "catalog_version": "2026-11-17",
                    "catalog_entry": {"id": "gpt-4o", "status": "active",
                                      "verified_at": "2026-11-17", "source_urls": ["https://openai.com/api/pricing/"]},
                    "effective_capabilities": {"vision": True, "reasoning": False, "context_limit": 128000},
                    "created_at": None,
                    "updated_at": None,
                },
            ]

        async def fake_providers():
            return [{"id": 1, "name": "openai", "provider_type": "openai", "has_api_key": False}]

        monkeypatch.setattr(repository, "list_models", fake_models)
        monkeypatch.setattr(repository, "list_providers", fake_providers)
        resp = await client.get("/api/v1/chat/models")
        assert resp.status_code == 200
        body = resp.json()
        # 능력·context_limit 노출, 키/가격은 미노출
        assert body[0]["model_name"] == "gpt-4o"
        assert body[0]["api_model_name"] == "gpt-4o"
        assert body[0]["api_provider"] == "openai"
        assert body[0]["display_name"] == "GPT-4o"
        assert body[0]["provider"] == "openai"
        assert body[0]["capabilities"] == {"vision": True, "reasoning": False, "context_limit": 128000}
        assert body[0]["context_limit"] == 128000
        assert body[0]["provider_api_key_configured"] is False
        assert body[0]["reasoning_none_supported"] is False
        assert body[0]["provider_family"] == "native"
        assert body[0]["price_origin"] == "native_catalog"
        assert body[0]["catalog_version"] == "2026-11-17"
        assert body[0]["catalog_entry"]["id"] == "gpt-4o"
        assert "input_price" not in body[0]
        assert "api_key" not in body[0]
        assert body[0]["id"] == 1

    async def test_reasoning_none_supported_follows_the_admission_rule(self, client, monkeypatch):
        cases = [
            ("openai", "gpt-5", [{"type": "effort", "values": ["minimal", "low", "medium", "high"]}], False),
            ("openai", "gpt-5.1", [{"type": "effort", "values": ["none", "low", "medium", "high"]}], True),
            ("gemini", "gemini-2.5-pro", [{"type": "budget_tokens", "min": 128, "max": 32768}], False),
            ("gemini", "gemini-2.5-flash", [{"type": "budget_tokens", "min": 0, "max": 24576}], True),
            ("anthropic", "claude-sonnet-4-5", [{"type": "budget_tokens", "min": 1024}], True),
            ("openai", "opaque-openai-reasoning", [{"type": "budget_tokens", "min": 1024}], False),
        ]

        async def fake_models(*, active_only=False):
            return [
                {
                    "id": index,
                    "provider_id": index,
                    "model_name": model_name,
                    "api_model_name": model_name,
                    "api_provider": "anthropic"
                    if model_name == "opaque-openai-reasoning"
                    else f"renamed-{provider_type}",
                    "provider_type": provider_type,
                    "display_name": model_name,
                    "effective_capabilities": {"reasoning": True, "reasoning_options": options},
                }
                for index, (provider_type, model_name, options, _) in enumerate(cases, start=1)
            ]

        async def fake_providers():
            return [
                {"id": index, "name": "provider", "provider_type": provider_type, "has_api_key": True}
                for index, (provider_type, _, _, _) in enumerate(cases, start=1)
            ]

        monkeypatch.setattr(repository, "list_models", fake_models)
        monkeypatch.setattr(repository, "list_providers", fake_providers)
        resp = await client.get("/api/v1/chat/models")

        assert resp.status_code == 200
        assert {item["model_name"]: item["reasoning_none_supported"] for item in resp.json()} == {
            model_name: expected for _, model_name, _, expected in cases
        }

    async def test_graceful_empty_on_storage_unavailable(self, client, monkeypatch):
        async def fake_models(*, active_only=False):
            raise errors.ChatStorageUnavailable("DB down")

        monkeypatch.setattr(repository, "list_models", fake_models)
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
        assert captured["descend"] is False

    async def test_set_active_leaf_returns_409_when_conversation_run_active(self, client, monkeypatch):
        async def fake_set(conv_id, **kwargs):
            raise cs.ConversationRunActive("conversation has an active run")

        monkeypatch.setattr(cs, "set_active_leaf", fake_set)
        resp = await client.patch(f"{_URL}/c1/active-leaf", json={"message_id": 7})
        assert resp.status_code == 409
        assert resp.json()["detail"] == "conversation_run_active"

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

    @pytest.mark.parametrize(
        ("error", "status", "detail"),
        [
            (cs.ConversationRunActive, 409, "conversation_run_active"),
            (cs.HistoryIndexUnavailable, 503, "history_index_unavailable"),
        ],
    )
    async def test_fork_returns_retryable_graph_conflicts(self, client, monkeypatch, error, status, detail):
        async def unavailable(*_args, **_kwargs):
            raise error("fork unavailable")

        monkeypatch.setattr(cs, "fork_conversation", unavailable)
        response = await client.post(f"{_URL}/c1/fork", json={"message_id": 5})
        assert response.status_code == status
        assert response.json()["detail"] == detail

    async def test_delete_rejects_active_run(self, client, monkeypatch):
        async def active(*_args, **_kwargs):
            raise cs.ConversationRunActive("conversation has an active run")

        monkeypatch.setattr(cs, "delete_conversation", active)
        response = await client.delete(f"{_URL}/c1")
        assert response.status_code == 409
        assert response.json()["detail"] == "conversation_run_active"


async def test_messages_uses_anchor_and_returns_signed_page_cursors(client, monkeypatch):
    from lumen.api import conversations

    captured = {}

    async def fake_get(conv_id, **kwargs):
        return _public_conv(id=conv_id, active_leaf_id=99, history_revision=7)

    async def fake_page(conv_id, **kwargs):
        captured["conv_id"] = conv_id
        captured.update(kwargs)
        return {
            "messages": [],
            "active_leaf_id": 99,
            "history_revision": 7,
            "has_before": False,
            "has_after": True,
            "before_position": None,
            "after_position": 39,
        }

    monkeypatch.setattr(conversations.cs, "get_conversation", fake_get)
    monkeypatch.setattr(conversations.cs, "list_message_page", fake_page)

    response = await client.get(
        f"{_URL}/conv-1/messages?anchor=first&limit=40&domain_id=caller-domain",
        headers={"X-Domain-Id": "caller-domain"},
    )

    assert response.status_code == 200
    assert captured == {
        "conv_id": "conv-1",
        "user_id": "test-user-123",
        "project_id": "test-project-123",
        "domain_id": "test-domain-123",
        "anchor": "first",
        "cursor_direction": None,
        "cursor_position": None,
        "expected_revision": None,
        "limit": 40,
    }
    body = response.json()
    assert body["before_cursor"] is None
    decoded = conversations._decode_history_cursor(body["after_cursor"], conversation_id="conv-1")
    assert decoded == {
        "v": 1,
        "conversation_id": "conv-1",
        "revision": 7,
        "direction": "after",
        "position": 39,
    }


async def test_messages_rejects_tampered_cursor_after_owner_check(client, monkeypatch):
    owner_checked = False

    async def fake_get(conv_id, **kwargs):
        nonlocal owner_checked
        owner_checked = True
        return _public_conv(id=conv_id)

    monkeypatch.setattr(cs, "get_conversation", fake_get)
    response = await client.get(f"{_URL}/conv-1/messages", params={"cursor": "bad.cursor"})
    assert owner_checked is True
    assert response.status_code == 422
    assert response.json()["detail"] == "invalid_history_cursor"


async def test_text_capability_response_includes_endpoint_capabilities(monkeypatch):
    from lumen.api import conversations

    async def resolve(_model_id):
        return {"model_name": "gpt-6-luna", "capabilities": {"decisions": True}}

    monkeypatch.setattr(conversations.routing, "resolve_model_by_id", resolve)
    result = await conversations.get_chat_capabilities(model_id=1, model_kind="text", token_info={})
    assert result["model_kind"] == "text"
    assert result["model_capabilities"] == {"decisions": True}


@pytest.mark.parametrize("api_base", [None, "https://compatible.invalid/v1"])
async def test_embedding_capabilities_http_uses_resolved_override_without_catalog(client, monkeypatch, api_base):
    from lumen.api import conversations
    from lumen.services import provider_metadata

    embedding = {
        "default_dimensions": 512, "supports_dimensions": True,
        "min_dimensions": 256, "max_dimensions": 1024, "allowed_dimensions": [256, 512, 1024],
    }
    caps = {"embedding": embedding, "context_limit": 4096}

    async def resolve(model_id, *, model_kind):
        assert (model_id, model_kind) == (7, "embedding")
        return {"model_name": "text-embedding-3-large", "provider_type": "openai",
                "api_base": api_base, "capabilities": caps}

    def forbidden(*_args, **_kwargs):
        pytest.fail("capabilities HTTP must use the resolved embedding override, not the native catalog")

    monkeypatch.setattr(conversations.routing, "resolve_model_by_id", resolve)
    monkeypatch.setattr(provider_metadata, "catalog_model_metadata", forbidden)
    response = await client.get("/v1/capabilities", params={"model_id": 7, "model_kind": "embedding"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["model_capabilities"] == caps
    assert {key: body[key] for key in embedding} == embedding
    assert body["max_input_tokens"] == 4096
    assert body["max_inputs"] == 16


async def test_available_embedding_models_preserve_effective_dimension_metadata(client, monkeypatch):
    embedding = {
        "default_dimensions": 3072, "supports_dimensions": True,
        "min_dimensions": 128, "max_dimensions": 3072, "allowed_dimensions": None,
    }
    caps = {"embedding": embedding, "context_limit": 2048}

    async def models(*, active_only):
        assert active_only is True
        return [{
            "id": 7, "provider_id": 1, "model_name": "gemini-embedding-001",
            "api_model_name": "gemini-embedding-001", "api_provider": "gemini",
            "provider_type": "gemini", "model_kind": "embedding", "display_name": "Gemini Embedding",
            "effective_capabilities": caps,
        }]

    async def providers():
        return [{"id": 1, "name": "Gemini", "provider_type": "gemini", "has_api_key": True}]

    monkeypatch.setattr(repository, "list_models", models)
    monkeypatch.setattr(repository, "list_providers", providers)
    response = await client.get("/v1/chat/models", params={"model_kind": "embedding"})
    assert response.status_code == 200, response.text
    assert response.json()[0]["capabilities"] == caps
    assert response.json()[0]["context_limit"] == 2048
