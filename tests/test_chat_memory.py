"""빌트인 AI 채팅 사용자 메모리 라우터 테스트 — user 소유 주입·403/404 매핑."""

import hashlib
from types import SimpleNamespace

from lumen.services import memory_store as ms

_URL = "/api/v1/chat/memories"


def _public(**over) -> dict:
    base = {
        "id": 1,
        "user_id": "test-user-123",
        "content": "사용자는 Python 을 선호",
        "is_active": True,
        "created_at": None,
        "updated_at": None,
    }
    base.update(over)
    return base


class _AutomaticResult:
    def __init__(self, row=None):
        self.row = row

    def scalar_one_or_none(self):
        return self.row


class _AutomaticSession:
    def __init__(self, row):
        self.row = row
        self.calls = 0

    async def execute(self, _statement):
        self.calls += 1
        return _AutomaticResult(self.row if self.calls == 3 else None)


def _automatic_row(**overrides):
    values = {
        "id": 5,
        "user_id": "test-user-123",
        "project_id": "test-project-123",
        "scope": "project",
        "category": "general",
        "status": "active",
        "is_active": True,
        "content": "before",
        "expires_at": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class TestAutomaticOps:
    async def test_update_applies_matching_state_fingerprint(self, monkeypatch):
        row = _automatic_row()
        session = _AutomaticSession(row)
        queued = []

        async def fake_queue(_session, **kwargs):
            queued.append(kwargs)

        monkeypatch.setattr(ms, "decrypt_chat_content", lambda content: content)
        monkeypatch.setattr(ms, "encrypt_chat_content", lambda content: content)
        monkeypatch.setattr(ms, "_queue_semantic_mutation", fake_queue)

        applied = await ms.apply_automatic_ops_in_transaction(
            session,
            ops=[
                {
                    "op": "update",
                    "id": 5,
                    "category": "preference",
                    "content": "after",
                    "expected_state_fingerprint": ms.memory_state_fingerprint(
                        "before",
                        category="general",
                        status="active",
                        is_active=True,
                    ),
                }
            ],
            user_id="test-user-123",
            project_id="test-project-123",
        )

        assert applied == {"add": 0, "update": 1, "delete": 0}
        assert (row.content, row.category) == ("after", "preference")
        assert queued[0]["plaintext"] == "after"

    async def test_update_rejects_manual_edit_after_extraction(self, monkeypatch):
        row = _automatic_row(content="manual edit", category="preference")
        session = _AutomaticSession(row)

        monkeypatch.setattr(ms, "decrypt_chat_content", lambda content: content)

        applied = await ms.apply_automatic_ops_in_transaction(
            session,
            ops=[
                {
                    "op": "update",
                    "id": 5,
                    "category": "habit",
                    "content": "model overwrite",
                    "expected_state_fingerprint": ms.memory_state_fingerprint(
                        "before",
                        category="general",
                        status="active",
                        is_active=True,
                    ),
                }
            ],
            user_id="test-user-123",
            project_id="test-project-123",
        )

        assert applied == {"add": 0, "update": 0, "delete": 0}
        assert (row.content, row.category) == ("manual edit", "preference")

    async def test_delete_rejects_manual_edit_after_extraction(self, monkeypatch):
        row = _automatic_row(content="manual edit")
        session = _AutomaticSession(row)

        monkeypatch.setattr(ms, "decrypt_chat_content", lambda content: content)

        applied = await ms.apply_automatic_ops_in_transaction(
            session,
            ops=[
                {
                    "op": "delete",
                    "id": 5,
                    "expected_state_fingerprint": ms.memory_state_fingerprint(
                        "before",
                        category="general",
                        status="active",
                        is_active=True,
                    ),
                }
            ],
            user_id="test-user-123",
            project_id="test-project-123",
        )

        assert applied == {"add": 0, "update": 0, "delete": 0}
        assert (row.status, row.is_active) == ("active", True)

    async def test_delete_never_mutates_account_memory(self):
        row = _automatic_row(scope="account", project_id=None)
        session = _AutomaticSession(row)

        applied = await ms.apply_automatic_ops_in_transaction(
            session,
            ops=[
                {
                    "op": "delete",
                    "id": 5,
                    "expected_state_fingerprint": ms.memory_state_fingerprint(
                        "before",
                        category="general",
                        status="active",
                        is_active=True,
                    ),
                }
            ],
            user_id="test-user-123",
            project_id="test-project-123",
        )

        assert applied == {"add": 0, "update": 0, "delete": 0}
        assert (row.status, row.is_active) == ("active", True)


def test_memory_content_fingerprint_is_keyed(monkeypatch):
    monkeypatch.setattr(ms, "derive_encryption_subkey", lambda _domain: b"k" * 32)

    assert ms.memory_content_fingerprint("yes") != hashlib.sha256(b"yes").hexdigest()
    assert ms.memory_content_fingerprint("yes") == ms.memory_content_fingerprint("yes")
    assert ms.memory_content_fingerprint("yes") != ms.memory_content_fingerprint("no")


class TestCrud:
    async def test_create_injects_user(self, client, monkeypatch):
        captured = {}

        async def fake_create(**kwargs):
            captured.update(kwargs)
            return _public(content=kwargs["content"])

        monkeypatch.setattr(ms, "create_memory", fake_create)
        resp = await client.post(_URL, json={"content": "다크모드 선호"})
        assert resp.status_code == 201
        assert captured["user_id"] == "test-user-123"
        assert captured["content"] == "다크모드 선호"
        assert captured["category"] == "general"

    async def test_create_project_scope_uses_token_project(self, client, monkeypatch):
        captured = {}

        async def fake_create(**kwargs):
            captured.update(kwargs)
            return _public(content=kwargs["content"], scope=kwargs["scope"], project_id=kwargs["project_id"])

        monkeypatch.setattr(ms, "create_memory", fake_create)
        resp = await client.post(_URL, json={"content": "프로젝트 메모리", "scope": "project"})

        assert resp.status_code == 201
        assert captured["scope"] == "project"
        assert captured["project_id"] == "test-project-123"
        assert captured["workspace_id"] is None

    async def test_create_and_update_accept_memory_category(self, client, monkeypatch):
        captured = {}

        async def fake_create(**kwargs):
            captured["create"] = kwargs
            return _public(content=kwargs["content"], category=kwargs["category"])

        async def fake_update(memory_id, **kwargs):
            captured["update"] = {"memory_id": memory_id, **kwargs}
            return _public(id=memory_id, category=kwargs["patch"]["category"])

        monkeypatch.setattr(ms, "create_memory", fake_create)
        monkeypatch.setattr(ms, "update_memory", fake_update)

        created = await client.post(_URL, json={"content": "Rust를 자주 사용", "category": "development"})
        updated = await client.patch(f"{_URL}/1", json={"category": "preference"})

        assert created.status_code == 201
        assert updated.status_code == 200
        assert captured["create"]["category"] == "development"
        assert captured["update"] == {
            "memory_id": 1,
            "user_id": "test-user-123",
            "project_id": "test-project-123",
            "patch": {"category": "preference"},
        }

    async def test_account_scope_rejects_workspace_namespace(self, client):
        resp = await client.post(_URL, json={"content": "잘못된 범위", "scope": "account", "workspace_id": 7})

        assert resp.status_code == 400

    async def test_semantic_search_rehydrates_only_requested_namespace(self, client, monkeypatch):
        from types import SimpleNamespace

        from lumen.api import memory

        captured = {}

        async def fake_resolve_model(_model_name):
            return {"model_name": "embedding"}

        async def fake_candidates(**kwargs):
            captured["candidate"] = kwargs
            return [9, 4]

        async def fake_hydrate(**kwargs):
            captured["hydrate"] = kwargs
            return [_public(id=9, scope="project", project_id="test-project-123")]

        monkeypatch.setattr(
            memory,
            "get_settings",
            lambda: SimpleNamespace(
                chat_memory_embedding_model="embedding",
                chat_memory_embedding_dimensions=3,
                chat_memory_candidate_limit=20,
            ),
        )
        monkeypatch.setattr(memory, "semantic_memory_available", lambda: True)
        monkeypatch.setattr(memory.ps, "resolve_model", fake_resolve_model)
        monkeypatch.setattr(memory.mr, "candidate_ids", fake_candidates)
        monkeypatch.setattr(memory.ms, "hydrate_candidate_ids", fake_hydrate)

        resp = await client.post(f"{_URL}/search", json={"query": "선호", "scope": "project"})

        assert resp.status_code == 200
        assert captured["candidate"]["project_id"] == "test-project-123"
        assert captured["candidate"]["workspace_id"] is None
        assert captured["hydrate"]["ids"] == [9, 4]
        assert captured["hydrate"]["scope"] == "project"

    async def test_semantic_search_fails_closed_when_index_unavailable(self, client, monkeypatch):
        from lumen.api import memory

        monkeypatch.setattr(memory, "semantic_memory_available", lambda: False)

        response = await client.post(f"{_URL}/search", json={"query": "선호", "scope": "account"})

        assert response.status_code == 503

    async def test_create_requires_content(self, client):
        resp = await client.post(_URL, json={"content": ""})
        assert resp.status_code == 422  # min_length=1

    async def test_list(self, client, monkeypatch):
        captured = {}

        async def fake_list(**kwargs):
            captured.update(kwargs)
            return [_public()]

        monkeypatch.setattr(ms, "list_memories", fake_list)
        resp = await client.get(_URL)
        assert resp.status_code == 200
        assert captured["user_id"] == "test-user-123"
        assert captured["project_id"] == "test-project-123"

    async def test_list_graceful_empty_on_storage_unavailable(self, client, monkeypatch):
        """저장소 미가용/데이터 없음은 503 이 아니라 빈 목록(200)으로 degrade."""

        async def fake_list(**kwargs):
            raise ms.ChatStorageUnavailable("chat DB 를 사용할 수 없습니다")

        monkeypatch.setattr(ms, "list_memories", fake_list)
        resp = await client.get(_URL)
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_update_forbidden_403(self, client, monkeypatch):
        async def fake_update(mid, **kwargs):
            raise ms.MemoryForbidden("타인")

        monkeypatch.setattr(ms, "update_memory", fake_update)
        resp = await client.patch(f"{_URL}/9", json={"is_active": False})
        assert resp.status_code == 403

    async def test_delete(self, client, monkeypatch):
        captured = {}

        async def fake_delete(mid, **kwargs):
            captured["mid"] = mid
            captured.update(kwargs)

        monkeypatch.setattr(ms, "delete_memory", fake_delete)
        resp = await client.delete(f"{_URL}/9")
        assert resp.status_code == 204
        assert captured["mid"] == 9 and captured["user_id"] == "test-user-123"
        assert captured["project_id"] == "test-project-123"
