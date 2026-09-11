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

    async def test_perplexity_agent_discovery_uses_v1_catalog_and_canonical_ids(self, monkeypatch):
        import httpx

        seen = {}

        async def fake_get(_pid):
            return {
                "provider_type": "perplexity",
                "api_base": None,
                "api_key": "pplx-key",
            }

        class _Response:
            status_code = 200

            @staticmethod
            def json():
                return {
                    "data": [
                        {"id": "sonar"},
                        {"id": "anthropic/claude-sonnet-4-6"},
                        {"id": "preset/research"},
                    ]
                }

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def get(self, url, *, headers):
                seen.update(url=url, headers=headers)
                return _Response()

        monkeypatch.setattr(model_discovery.provider_store, "get_provider_for_discovery", fake_get)
        monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: _Client())

        out = await model_discovery.discover_models(1)

        assert out == {
            "models": ["anthropic/claude-sonnet-4-6", "perplexity/sonar"],
            "source": "api",
        }
        assert seen == {
            "url": "https://api.perplexity.ai/v1/models",
            "headers": {"Authorization": "Bearer pplx-key"},
        }

    @pytest.mark.parametrize(
        ("api_base", "expected_url"),
        (
            ("https://api.perplexity.ai", "https://api.perplexity.ai/v1/models"),
            ("https://api.perplexity.ai/v1", "https://api.perplexity.ai/v1/models"),
            ("https://api.perplexity.ai/router", "https://api.perplexity.ai/router/v1/models"),
            ("https://api.perplexity.ai/router/v1", "https://api.perplexity.ai/router/v1/models"),
        ),
    )
    async def test_perplexity_discovery_keeps_agent_and_router_catalogs_separate(
        self, monkeypatch, api_base, expected_url
    ):
        seen = {}

        async def fake_get(_pid):
            return {
                "provider_type": "perplexity",
                "api_base": api_base,
                "api_key": "pplx-key",
            }

        async def fake_fetch(provider_type, received_base, api_key):
            seen.update(provider_type=provider_type, api_base=received_base, api_key=api_key)
            assert model_discovery._models_url(provider_type, received_base) == expected_url
            return []

        monkeypatch.setattr(model_discovery.provider_store, "get_provider_for_discovery", fake_get)
        monkeypatch.setattr(model_discovery, "_fetch_openai_compatible", fake_fetch)
        monkeypatch.setattr(
            model_discovery,
            "_litellm_static",
            lambda _provider: ["perplexity/perplexity/sonar", "perplexity/sonar", "preset/hidden"],
        )

        out = await model_discovery.discover_models(1)

        if "/router" in api_base:
            assert out == {"models": [], "source": "none"}
        else:
            assert out == {"models": ["perplexity/sonar"], "source": "litellm"}
        assert seen == {
            "provider_type": "perplexity",
            "api_base": api_base,
            "api_key": "pplx-key",
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

    async def test_chatgpt_subscription_uses_static_canonical_catalog_without_live_auth(self, monkeypatch):
        async def fake_get(pid):
            assert pid == 7
            return {
                "provider_type": "chatgpt",
                "auth_mode": "chatgpt_device",
                "api_base": None,
                "api_key": None,
            }

        async def forbidden_fetch(*args, **kwargs):
            raise AssertionError("subscription discovery must not call a live models endpoint")

        monkeypatch.setattr(model_discovery.provider_store, "get_provider_for_discovery", fake_get)
        monkeypatch.setattr(model_discovery, "_fetch_openai_compatible", forbidden_fetch)
        monkeypatch.setattr(
            model_discovery,
            "_litellm_static",
            lambda provider: ["chatgpt/gpt-5.2-codex", "gpt-5.3-codex"] if provider == "chatgpt" else [],
        )

        out = await model_discovery.discover_models(7)

        assert out == {
            "models": ["chatgpt/gpt-5.2-codex", "chatgpt/gpt-5.3-codex"],
            "source": "litellm",
        }

    async def test_anthropic_subscription_catalog_keeps_distinct_namespace(self, monkeypatch):
        async def fake_get(_pid):
            return {
                "provider_type": "anthropic",
                "auth_mode": "anthropic_subscription",
                "api_base": None,
                "api_key": None,
            }

        monkeypatch.setattr(model_discovery.provider_store, "get_provider_for_discovery", fake_get)
        monkeypatch.setattr(model_discovery, "_litellm_static", lambda provider: ["claude-opus-4-1"])

        out = await model_discovery.discover_models(8)

        assert out["models"] == ["anthropic-subscription/claude-opus-4-1"]
