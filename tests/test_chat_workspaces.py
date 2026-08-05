"""빌트인 AI 채팅 프로젝트(workspace) 라우터 테스트 — 소유자 주입·403/404 매핑."""

from lumen.services import workspace_store as ws

_URL = "/api/v1/chat/workspaces"


def _public(**over) -> dict:
    base = {
        "id": 1,
        "owner_user_id": "test-user-123",
        "name": "내 프로젝트",
        "description": None,
        "instructions": "이 프로젝트는 파이썬 전용",
        "created_at": None,
        "updated_at": None,
    }
    base.update(over)
    return base


class TestCrud:
    async def test_create_injects_owner(self, client, monkeypatch):
        captured = {}

        async def fake_create(**kwargs):
            captured.update(kwargs)
            return _public(name=kwargs["name"])

        monkeypatch.setattr(ws, "create_workspace", fake_create)
        resp = await client.post(_URL, json={"name": "백엔드", "instructions": "FastAPI 규칙"})
        assert resp.status_code == 201
        assert captured["owner_user_id"] == "test-user-123"  # 소유자는 token 에서만

    async def test_list(self, client, monkeypatch):
        captured = {}

        async def fake_list(**kwargs):
            captured.update(kwargs)
            return [_public()]

        monkeypatch.setattr(ws, "list_workspaces", fake_list)
        resp = await client.get(_URL)
        assert resp.status_code == 200
        assert captured["user_id"] == "test-user-123"

    async def test_list_graceful_empty_on_storage_unavailable(self, client, monkeypatch):
        """저장소 미가용/데이터 없음은 503 이 아니라 빈 목록(200)으로 degrade."""

        async def fake_list(**kwargs):
            raise ws.ChatStorageUnavailable("chat DB 를 사용할 수 없습니다")

        monkeypatch.setattr(ws, "list_workspaces", fake_list)
        resp = await client.get(_URL)
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_get_forbidden_403(self, client, monkeypatch):
        async def fake_get(wid, **kwargs):
            raise ws.WorkspaceForbidden("타인")

        monkeypatch.setattr(ws, "get_workspace", fake_get)
        resp = await client.get(f"{_URL}/9")
        assert resp.status_code == 403

    async def test_update_not_found_404(self, client, monkeypatch):
        async def fake_update(wid, **kwargs):
            raise ws.WorkspaceNotFound("없음")

        monkeypatch.setattr(ws, "update_workspace", fake_update)
        resp = await client.patch(f"{_URL}/9", json={"name": "x"})
        assert resp.status_code == 404

    async def test_delete(self, client, monkeypatch):
        captured = {}

        async def fake_delete(wid, **kwargs):
            captured["wid"] = wid
            captured.update(kwargs)

        monkeypatch.setattr(ws, "delete_workspace", fake_delete)
        resp = await client.delete(f"{_URL}/9")
        assert resp.status_code == 204
        assert captured["wid"] == 9 and captured["user_id"] == "test-user-123"
