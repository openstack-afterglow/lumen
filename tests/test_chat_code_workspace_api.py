"""Contract tests for project-scoped code-workspace routes."""

from lumen.api import code_workspaces as api
from lumen.services import code_workspace_service as store
from lumen.services.agent_workspace_runtime import WorkspaceRuntimePolicy
from lumen.services.sandbox_runtime import SandboxPolicy

_BASE = "/api/v1/chat"


def _policy() -> WorkspaceRuntimePolicy:
    return WorkspaceRuntimePolicy(
        base_url="https://workspace.example",
        sandbox=SandboxPolicy(
            url="https://sandbox.example",
            api_key="secret",
            image_digest="sha256:" + "a" * 64,
            policy_version="v1",
            egress_allowlist=("github.com",),
        ),
    )


class TestGitCredentials:
    async def test_create_uses_authenticated_owner_project_only(self, client, monkeypatch):
        captured = {}

        async def create(**kwargs):
            captured.update(kwargs)
            return {"id": 1, "host": "github.com", "token_present": True}

        monkeypatch.setattr(api, "create_git_credential", create)
        response = await client.post(f"{_BASE}/git-credentials", json={"host": "github.com", "token": "secret"})

        assert response.status_code == 201
        assert captured["user_id"] == "test-user-123"
        assert captured["project_id"] == "test-project-123"
        assert captured["token"] == "secret"
        assert "token" not in response.json()

    async def test_cross_project_credential_is_hidden_as_not_found(self, client, monkeypatch):
        async def revoke(*_args, **_kwargs):
            raise store.CodeWorkspaceNotFound("missing")

        monkeypatch.setattr(api, "revoke_git_credential", revoke)
        response = await client.delete(f"{_BASE}/git-credentials/23")

        assert response.status_code == 404
        assert response.json()["detail"] == "resource was not found"


class TestCodeWorkspaces:
    async def test_create_requires_runtime_before_store_side_effect(self, client, monkeypatch):
        monkeypatch.setattr(api, "configured_workspace_policy", lambda _settings: None)
        response = await client.post(
            f"{_BASE}/code-workspaces",
            headers={"Idempotency-Key": "request-1"},
            json={"name": "Repo", "source_kind": "empty"},
        )

        assert response.status_code == 422

    async def test_create_passes_policy_and_idempotency_key(self, client, monkeypatch):
        captured = {}
        monkeypatch.setattr(api, "configured_workspace_policy", lambda _settings: _policy())

        async def create(**kwargs):
            captured.update(kwargs)
            return {"id": "workspace-1", "lifecycle_status": "creating"}

        monkeypatch.setattr(api, "create_workspace", create)
        response = await client.post(
            f"{_BASE}/code-workspaces",
            headers={"Idempotency-Key": "request-1"},
            json={"name": "Repo", "source_kind": "empty"},
        )

        assert response.status_code == 202
        assert captured["user_id"] == "test-user-123"
        assert captured["project_id"] == "test-project-123"
        assert captured["idempotency_key"] == "request-1"
        assert captured["policy"] == _policy()

    async def test_assignment_rejects_cross_project_workspace_as_not_found(self, client, monkeypatch):
        async def assign(*_args, **_kwargs):
            raise store.CodeWorkspaceNotFound("missing")

        monkeypatch.setattr(api, "assign_conversation_workspace", assign)
        response = await client.put(
            f"{_BASE}/conversations/conversation-1/code-workspace", json={"code_workspace_id": "workspace-2"}
        )

        assert response.status_code == 404
        assert response.json()["detail"] == "resource was not found"
