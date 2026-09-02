"""프로바이더 모델 discovery 테스트 — 라이브 /models 조회 + litellm 정적 fallback."""

import pytest

from lumen.services import model_discovery
from lumen.services.providers import errors


class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload or {}

    def json(self):
        return self._payload


class _Client:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, headers=None):
        return self._resp


def _client_factory(resp):
    def _mk(**kwargs):
        return _Client(resp)

    return _mk


class TestDiscover:
    async def test_provider_not_found(self, monkeypatch):
        async def fake_get(pid):
            return None

        monkeypatch.setattr(model_discovery.provider_store, "get_provider_for_discovery", fake_get)
        with pytest.raises(errors.ProviderNotFoundError):
            await model_discovery.discover_models(999)

    async def test_live_api_success(self, monkeypatch):
        async def fake_get(pid):
            return {"provider_type": "openai", "api_base": None, "api_key": "sk"}

        monkeypatch.setattr(model_discovery.provider_store, "get_provider_for_discovery", fake_get)
        resp = _Resp(200, {"data": [{"id": "gpt-4o"}, {"id": "gpt-4o-mini"}]})
        monkeypatch.setattr("httpx.AsyncClient", _client_factory(resp))
        out = await model_discovery.discover_models(1)
        assert out["source"] == "api"
        assert "gpt-4o" in out["models"]
        assert "gpt-4o-mini" in out["models"]

    async def test_perplexity_uses_its_default_models_endpoint(self, monkeypatch):
        import httpx

        seen = {}

        class _Response:
            status_code = 200

            @staticmethod
            def json():
                return {"data": [{"id": "sonar-pro"}]}

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def get(self, url, *, headers):
                seen.update(url=url, headers=headers)
                return _Response()

        monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: _Client())

        out = await model_discovery._fetch_openai_compatible("perplexity", None, "pplx-key")

        assert out == ["sonar-pro"]
        assert seen == {
            "url": "https://api.perplexity.ai/models",
            "headers": {"Authorization": "Bearer pplx-key"},
        }

    async def test_non_openai_uses_litellm(self, monkeypatch):
        async def fake_get(pid):
            return {"provider_type": "anthropic", "api_base": None, "api_key": "sk"}

        monkeypatch.setattr(model_discovery.provider_store, "get_provider_for_discovery", fake_get)
        monkeypatch.setattr(model_discovery, "_litellm_static", lambda pt: ["claude-3-5-sonnet"])
        out = await model_discovery.discover_models(1)
        assert out["source"] == "litellm"
        assert "claude-3-5-sonnet" in out["models"]

    async def test_live_fail_falls_back_to_litellm(self, monkeypatch):
        async def fake_get(pid):
            return {"provider_type": "openai", "api_base": None, "api_key": "sk"}

        monkeypatch.setattr(model_discovery.provider_store, "get_provider_for_discovery", fake_get)
        monkeypatch.setattr("httpx.AsyncClient", _client_factory(_Resp(401, {})))
        monkeypatch.setattr(model_discovery, "_litellm_static", lambda pt: ["gpt-4o"])
        out = await model_discovery.discover_models(1)
        assert out["source"] == "litellm"
        assert out["models"] == ["gpt-4o"]
