from types import SimpleNamespace

import pytest

from lumen.services import agent_workspace_runtime as runtime
from lumen.services.code_workspace_store import CodeWorkspaceValidationError, validate_workspace_source
from lumen.services.sandbox_runtime import SandboxPolicy


def _policy():
    return runtime.WorkspaceRuntimePolicy(
        base_url="https://workspace.example",
        sandbox=SandboxPolicy(
            url="https://sandbox.example",
            api_key="secret",
            image_digest="sha256:" + "a" * 64,
            policy_version="v1",
            egress_allowlist=("github.com",),
        ),
    )


def test_workspace_policy_requires_safe_https_runtime_url(monkeypatch):
    monkeypatch.setattr(runtime, "configured_policy", lambda _settings: _policy().sandbox)

    assert (
        runtime.configured_workspace_policy(SimpleNamespace(chat_sandbox_workspace_url="http://workspace.example"))
        is None
    )
    assert (
        runtime.configured_workspace_policy(
            SimpleNamespace(chat_sandbox_workspace_url="https://user@workspace.example")
        )
        is None
    )
    assert runtime.configured_workspace_policy(
        SimpleNamespace(chat_sandbox_workspace_url="https://workspace.example/v1")
    ) == runtime.WorkspaceRuntimePolicy(base_url="https://workspace.example/v1", sandbox=_policy().sandbox)


async def test_workspace_provision_rejects_invalid_source_before_remote_request(monkeypatch):
    called = False

    async def request(*_args, **_kwargs):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(runtime, "_request", request)

    with pytest.raises(runtime.WorkspaceRuntimeUnavailable, match="git workspace source"):
        await runtime.provision(
            _policy(), workspace_id="workspace-1", source={"kind": "git", "url": "https://git.example/repo"}
        )

    assert called is False


async def test_workspace_provision_accepts_only_identity_bound_result(monkeypatch):
    async def request(policy, method, suffix, payload):
        assert method == "POST"
        assert suffix == "/v1/workspaces"
        assert payload["source"] == {"kind": "empty"}
        return {
            "workspace_id": "workspace-1",
            "state_revision": 0,
            "capabilities": {},
            "image_digest": policy.sandbox.image_digest,
            "policy_version": policy.sandbox.policy_version,
        }

    monkeypatch.setattr(runtime, "_request", request)

    result = await runtime.provision(_policy(), workspace_id="workspace-1", source={"kind": "empty"})

    assert result["state_revision"] == 0


def test_workspace_source_rejects_credentials_and_cross_host_git_credentials():
    with pytest.raises(CodeWorkspaceValidationError, match="origin is not allowed"):
        validate_workspace_source(
            source_kind="git",
            repository_url="https://token@git.example/repo.git",
            source_revision=None,
            credential_host=None,
        )
    with pytest.raises(CodeWorkspaceValidationError, match="origin is not allowed"):
        validate_workspace_source(
            source_kind="git",
            repository_url="https://127.0.0.1/repo.git",
            source_revision=None,
            credential_host=None,
        )
    with pytest.raises(CodeWorkspaceValidationError, match="does not match"):
        validate_workspace_source(
            source_kind="git",
            repository_url="https://git.example/repo.git",
            source_revision=None,
            credential_host="other.example",
        )

    url, host = validate_workspace_source(
        source_kind="git",
        repository_url="https://GIT.example/repo.git/",
        source_revision=None,
        credential_host="git.example",
    )

    assert url == "https://git.example/repo.git"
    assert host == "git.example"


def test_workspace_source_enforces_allowed_git_origins():
    for url in ("https://localhost/repo.git", "https://[::1]/repo.git", "https://gitlab.com/repo.git"):
        with pytest.raises(CodeWorkspaceValidationError, match="origin is not allowed"):
            validate_workspace_source(
                source_kind="git",
                repository_url=url,
                source_revision=None,
                credential_host=None,
                allowed_domains=("github.com",),
            )

    url, host = validate_workspace_source(
        source_kind="git",
        repository_url="https://api.github.com/org/repo.git",
        source_revision="main",
        credential_host=None,
        allowed_domains=("github.com",),
    )

    assert url == "https://api.github.com/org/repo.git"
    assert host == "api.github.com"
