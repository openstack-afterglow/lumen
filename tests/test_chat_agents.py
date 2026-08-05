"""빌트인 AI 채팅 에이전트 라우터 테스트.

DB 없이 agent_store 를 monkeypatch 하여 라우터 계약만 검증:
- 소유자(user_id)는 token_info 에서만 주입(IDOR 경계)
- 조회 소유권/공개, 수정·삭제 소유자 한정 → 403/404 매핑
- 허브 검색·복제 위임
"""

from lumen.services import agent_store as ags

_URL = "/api/v1/chat/agents"


def _public(**over) -> dict:
    base = {
        "id": 1,
        "owner_user_id": "test-user-123",
        "name": "내 에이전트",
        "description": None,
        "avatar": None,
        "instructions": "너는 전문가야",
        "model_name": "gpt-4o",
        "params": {},
        "mcp_ids": [],
        "tool_ids": [],
        "visibility": "private",
        "cloned_from_id": None,
        "clone_count": 0,
        "is_owner": True,
        "created_at": None,
        "updated_at": None,
    }
    base.update(over)
    return base


class TestCreateAndList:
    async def test_create_injects_owner_from_token(self, client, monkeypatch):
        captured = {}

        async def fake_create(**kwargs):
            captured.update(kwargs)
            return _public(name=kwargs["name"])

        monkeypatch.setattr(ags, "create_agent", fake_create)
        resp = await client.post(_URL, json={"name": "코드 리뷰어", "instructions": "리뷰해", "visibility": "public"})
        assert resp.status_code == 201
        assert captured["owner_user_id"] == "test-user-123"  # 소유자는 token 에서만
        assert captured["project_id"] == "test-project-123"
        assert captured["visibility"] == "public"

    async def test_list_own(self, client, monkeypatch):
        captured = {}

        async def fake_list(**kwargs):
            captured.update(kwargs)
            return [_public()]

        monkeypatch.setattr(ags, "list_agents", fake_list)
        resp = await client.get(_URL)
        assert resp.status_code == 200
        assert captured["user_id"] == "test-user-123"
        assert captured["project_id"] == "test-project-123"

    async def test_list_graceful_empty_on_storage_unavailable(self, client, monkeypatch):
        """저장소 미가용/데이터 없음은 503 이 아니라 빈 목록(200)으로 degrade."""

        async def fake_list(**kwargs):
            raise ags.ChatStorageUnavailable("chat DB 를 사용할 수 없습니다")

        monkeypatch.setattr(ags, "list_agents", fake_list)
        resp = await client.get(_URL)
        assert resp.status_code == 200
        assert resp.json() == []


class TestHubAndClone:
    async def test_hub_graceful_empty_on_storage_unavailable(self, client, monkeypatch):
        """허브 검색도 저장소 미가용 시 빈 목록(200)으로 degrade."""

        async def fake_public(**kwargs):
            raise ags.ChatStorageUnavailable("chat DB 를 사용할 수 없습니다")

        monkeypatch.setattr(ags, "list_public", fake_public)
        resp = await client.get(f"{_URL}/hub")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_hub_search(self, client, monkeypatch):
        captured = {}

        async def fake_public(**kwargs):
            captured.update(kwargs)
            return [_public(id=9, owner_user_id="other", visibility="public", is_owner=False, clone_count=5)]

        monkeypatch.setattr(ags, "list_public", fake_public)
        resp = await client.get(f"{_URL}/hub?query=리뷰&limit=10")
        assert resp.status_code == 200
        assert captured["query"] == "리뷰" and captured["limit"] == 10
        assert captured["project_id"] == "test-project-123"
        assert captured["user_id"] == "test-user-123"
        assert resp.json()[0]["clone_count"] == 5

    async def test_clone_delegates(self, client, monkeypatch):
        captured = {}

        async def fake_clone(agent_id, **kwargs):
            captured["agent_id"] = agent_id
            captured.update(kwargs)
            return _public(id=100, cloned_from_id=agent_id)

        monkeypatch.setattr(ags, "clone_agent", fake_clone)
        resp = await client.post(f"{_URL}/9/clone")
        assert resp.status_code == 201
        assert captured["agent_id"] == 9 and captured["user_id"] == "test-user-123"
        assert captured["project_id"] == "test-project-123"
        assert resp.json()["cloned_from_id"] == 9

    async def test_clone_forbidden_403(self, client, monkeypatch):
        async def fake_clone(agent_id, **kwargs):
            raise ags.AgentForbidden("비공개")

        monkeypatch.setattr(ags, "clone_agent", fake_clone)
        resp = await client.post(f"{_URL}/9/clone")
        assert resp.status_code == 403


class TestOwnership:
    async def test_get_forbidden_403(self, client, monkeypatch):
        async def fake_get(agent_id, **kwargs):
            raise ags.AgentForbidden("비공개 타인")

        monkeypatch.setattr(ags, "get_agent", fake_get)
        resp = await client.get(f"{_URL}/5")
        assert resp.status_code == 403

    async def test_update_not_found_404(self, client, monkeypatch):
        async def fake_update(agent_id, **kwargs):
            raise ags.AgentNotFound("없음")

        monkeypatch.setattr(ags, "update_agent", fake_update)
        resp = await client.patch(f"{_URL}/5", json={"name": "x"})
        assert resp.status_code == 404

    async def test_delete_forbidden_403(self, client, monkeypatch):
        async def fake_delete(agent_id, **kwargs):
            raise ags.AgentForbidden("소유자 아님")

        monkeypatch.setattr(ags, "delete_agent", fake_delete)
        resp = await client.delete(f"{_URL}/5")
        assert resp.status_code == 403

    async def test_get_update_and_delete_inject_project(self, client, monkeypatch):
        captured = {}

        async def fake_get(agent_id, **kwargs):
            captured["get"] = (agent_id, kwargs)
            return _public(id=agent_id)

        async def fake_update(agent_id, **kwargs):
            captured["update"] = (agent_id, kwargs)
            return _public(id=agent_id, name=kwargs["patch"]["name"])

        async def fake_delete(agent_id, **kwargs):
            captured["delete"] = (agent_id, kwargs)

        monkeypatch.setattr(ags, "get_agent", fake_get)
        monkeypatch.setattr(ags, "update_agent", fake_update)
        monkeypatch.setattr(ags, "delete_agent", fake_delete)

        assert (await client.get(f"{_URL}/7")).status_code == 200
        assert (await client.patch(f"{_URL}/7", json={"name": "수정"})).status_code == 200
        assert (await client.delete(f"{_URL}/7")).status_code == 204

        for agent_id, kwargs in captured.values():
            assert agent_id == 7
            assert kwargs["user_id"] == "test-user-123"
            assert kwargs["project_id"] == "test-project-123"
