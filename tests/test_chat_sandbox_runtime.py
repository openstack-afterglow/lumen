from types import SimpleNamespace

import pytest

from lumen.services import sandbox_runtime


def _settings(**overrides):
    values = {
        "chat_sandbox_url": "https://sandbox.example/v1/execute",
        "chat_sandbox_api_key": "sandbox-token",
        "chat_sandbox_image_digest": "sha256:" + "a" * 64,
        "chat_sandbox_policy_version": "2026-07-24",
        "chat_sandbox_egress_allowlist": ["api.example", "api.example"],
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_sandbox_requires_complete_https_immutable_policy():
    policy = sandbox_runtime.configured_policy(_settings())

    assert policy is not None
    assert policy.egress_allowlist == ("api.example",)
    assert sandbox_runtime.configured_policy(_settings(chat_sandbox_url="http://sandbox.example")) is None
    assert sandbox_runtime.configured_policy(_settings(chat_sandbox_image_digest="latest")) is None
    assert sandbox_runtime.configured_policy(_settings(chat_sandbox_egress_allowlist=["*.example"])) is None


@pytest.mark.asyncio
async def test_sandbox_execution_freezes_policy_snapshot_and_rejects_drift(monkeypatch):
    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "policy_version": "2026-07-24",
                "stdout": "ok",
                "stderr": "",
                "exit_code": 0,
                "duration_seconds": 0.2,
            }

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, *, json):
            captured.update(url=url, payload=json)
            return Response()

    monkeypatch.setattr(sandbox_runtime.httpx, "AsyncClient", lambda **_kwargs: Client())
    policy = sandbox_runtime.configured_policy(_settings())
    assert policy is not None

    result = await sandbox_runtime.execute(policy, language="python", source="print('ok')", timeout_seconds=10)

    assert result.stdout == "ok"
    assert captured["url"] == policy.url
    assert captured["payload"] == {
        "language": "python",
        "source": "print('ok')",
        "timeout_seconds": 10,
        "image_digest": policy.image_digest,
        "policy_version": policy.policy_version,
        "egress_allowlist": ["api.example"],
    }
