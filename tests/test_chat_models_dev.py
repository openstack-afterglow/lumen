"""models.dev advisory catalog and administrator import API tests."""

from __future__ import annotations

import json

import pytest

from lumen.services import models_dev
from lumen.services.providers import errors, pricing
from lumen.services.providers import repository as ps

_BASE = "/api/v1/chat/admin/models/pricing/models-dev"


def _catalog():
    return models_dev.parse_catalog(
        json.dumps(
            {
                "providers": {
                    "openai": {
                        "id": "openai",
                        "name": "OpenAI",
                        "models": {
                            "openai/gpt-test": {
                                "id": "openai/gpt-test",
                                "name": "GPT Test",
                                "last_updated": "2026-07-20",
                                "cost": {
                                    "input": 2,
                                    "output": 8,
                                    "reasoning": 16,
                                    "cache_read": 1,
                                    "tiers": {"batch": {"input": 1}},
                                },
                            },
                            "openai/unpriced": {"id": "openai/unpriced", "name": "Unpriced", "cost": {"input": 2}},
                        },
                    }
                }
            }
        ),
        "2026-07-20T00:00:00+00:00",
    )


class TestModelsDevCatalog:
    def test_provider_detail_uses_decimal_strings_and_warnings(self):
        detail = models_dev.provider_detail(_catalog(), "openai")
        assert detail is not None
        model = detail["models"][0]
        assert model["input_price_per_million"] == "2"
        assert model["output_price_per_million"] == "8"
        assert model["price_available"] is True
        assert model["unsupported_price_fields"] == ["cost.cache_read", "cost.reasoning", "cost.tiers"]
        raw_cost = _catalog().providers["openai"].models["openai/gpt-test"].cost
        assert pricing._json_decimal_strings(raw_cost)["input"] == "2"

    def test_capabilities_parsed_and_exposed(self):
        catalog = models_dev.parse_catalog(
            json.dumps(
                {
                    "providers": {
                        "anthropic": {
                            "id": "anthropic",
                            "name": "Anthropic",
                            "models": {
                                "claude-x": {
                                    "id": "claude-x",
                                    "name": "Claude X",
                                    "attachment": True,
                                    "reasoning": True,
                                    "tool_call": True,
                                    "modalities": {"input": ["text", "image", "pdf"], "output": ["text"]},
                                    "reasoning_options": [
                                        {"type": "effort", "values": ["low", "xhigh", "max", "ultra"]}
                                    ],
                                    "limit": {"context": 200000, "output": 64000},
                                    "cost": {"input": 5, "output": 25},
                                }
                            },
                        }
                    }
                }
            ),
            "2026-07-21T00:00:00+00:00",
        )
        caps = catalog.providers["anthropic"].models["claude-x"].capabilities
        assert caps["vision"] is True and caps["reasoning"] is True and caps["tool_call"] is True
        assert caps["attachment"] is True
        assert caps["modalities"] == {"input": ["text", "image", "pdf"], "output": ["text"]}
        assert caps["reasoning_options"] == [{"type": "effort", "values": ["low", "xhigh", "max", "ultra"]}]
        assert caps["context_limit"] == 200000
        # provider_detail 이 능력을 노출
        detail = models_dev.provider_detail(catalog, "anthropic")
        assert detail["models"][0]["capabilities"]["vision"] is True

    def test_capabilities_defaults_when_missing(self):
        catalog = _catalog()
        caps = catalog.providers["openai"].models["openai/gpt-test"].capabilities
        # 능력 필드가 없는 모델 → 방어적 기본값(전부 False, modalities 빈 목록)
        assert caps["vision"] is False and caps["reasoning"] is False and caps["tool_call"] is False
        assert caps["modalities"] == {"input": [], "output": []}
        assert caps["reasoning_options"] == [] and caps["context_limit"] is None

    def test_missing_price_pair_remains_visible_but_unimportable(self):
        detail = models_dev.provider_detail(_catalog(), "openai")
        assert detail is not None
        unpriced = detail["models"][1]
        assert unpriced["price_available"] is False
        assert unpriced["input_price_per_million"] is None
        assert unpriced["output_price_per_million"] is None

    def test_registered_provider_list_excludes_unregistered_catalogs_and_prioritizes_current(self):
        catalog = models_dev.parse_catalog(
            json.dumps(
                {
                    "providers": {
                        "zeta": {"id": "zeta", "name": "Zeta", "models": {}},
                        "openai": {"id": "openai", "name": "OpenAI", "models": {}},
                        "anthropic": {"id": "anthropic", "name": "Anthropic", "models": {}},
                        "unregistered": {"id": "unregistered", "name": "Unregistered", "models": {}},
                    }
                }
            ),
            "2026-07-31T00:00:00+00:00",
        )
        providers = models_dev.registered_provider_list(
            catalog,
            [
                {"id": 1, "name": "OpenAI gateway", "provider_type": "openai", "models_dev_provider_id": None},
                {"id": 2, "name": "Claude", "provider_type": "anthropic", "models_dev_provider_id": None},
                {"id": 3, "name": "Legacy", "provider_type": "custom", "models_dev_provider_id": "zeta"},
            ],
            current_provider_id=1,
        )
        assert [provider["id"] for provider in providers] == ["openai", "anthropic", "zeta"]

    def test_live_top_level_provider_map_is_supported(self):
        catalog = models_dev.parse_catalog(
            json.dumps(
                {
                    "stepfun-step-plan": {
                        "id": "stepfun-step-plan",
                        "name": "StepFun",
                        "models": {
                            "stepfun/step-3": {
                                "id": "stepfun/step-3",
                                "name": "Step 3",
                                "cost": {"input": 2, "output": 8},
                            }
                        },
                    }
                }
            ),
            "2026-07-20T00:00:00+00:00",
        )
        assert catalog.providers["stepfun-step-plan"].models["stepfun/step-3"].price_available is True

    @pytest.mark.parametrize(
        "payload",
        [
            {"providers": {"openai": {"id": "other", "models": {}}}},
            {"providers": {"openai": {"id": "openai", "models": {"x": {"id": "other"}}}}},
            {
                "providers": {
                    "openai": {"id": "openai", "models": {"x": {"id": "x", "cost": {"input": -1, "output": 1}}}}
                }
            },
        ],
    )
    def test_rejects_untrusted_catalog_identity_or_price(self, payload):
        with pytest.raises(models_dev.ModelsDevCatalogError):
            models_dev.parse_catalog(json.dumps(payload), "2026-07-20T00:00:00+00:00")


class TestModelsDevAdminRoutes:
    async def test_catalog_provider_and_detail_contract(self, admin_client, monkeypatch):
        async def fake_catalog(*, refresh=False):
            assert refresh is True
            return _catalog()

        async def fake_list_providers():
            return [
                {
                    "id": 1,
                    "name": "OpenAI gateway",
                    "provider_type": "openai",
                    "models_dev_provider_id": None,
                }
            ]

        monkeypatch.setattr(ps, "list_providers", fake_list_providers)

        monkeypatch.setattr(models_dev, "get_catalog", fake_catalog)
        provider_response = await admin_client.get(f"{_BASE}/providers?local_provider_id=1&refresh=true")
        assert provider_response.status_code == 200
        assert provider_response.json() == {
            "source_url": models_dev.SOURCE_URL,
            "fetched_at": "2026-07-20T00:00:00+00:00",
            "preferred_provider_ids": ["openai"],
            "providers": [{"id": "openai", "name": "OpenAI", "model_count": 2}],
        }

        detail_response = await admin_client.get(f"{_BASE}/providers/openai?refresh=true")
        assert detail_response.status_code == 200
        body = detail_response.json()
        assert body["provider"] == {"id": "openai", "name": "OpenAI"}
        assert body["models"][0]["input_price_per_million"] == "2"
        assert body["models"][0]["output_price_per_million"] == "8"

    async def test_provider_list_excludes_unregistered_catalog_providers(self, admin_client, monkeypatch):
        catalog = models_dev.parse_catalog(
            json.dumps(
                {
                    "providers": {
                        "zeta": {"id": "zeta", "name": "Zeta", "models": {}},
                        "openai": {"id": "openai", "name": "OpenAI", "models": {}},
                        "anthropic": {"id": "anthropic", "name": "Anthropic", "models": {}},
                        "unregistered": {"id": "unregistered", "name": "Unregistered", "models": {}},
                    }
                }
            ),
            "2026-07-31T00:00:00+00:00",
        )

        async def fake_catalog(*, refresh=False):
            return catalog

        async def fake_list_providers():
            return [
                {"id": 1, "name": "OpenAI gateway", "provider_type": "openai", "models_dev_provider_id": None},
                {"id": 2, "name": "Claude", "provider_type": "anthropic", "models_dev_provider_id": None},
                {"id": 3, "name": "Legacy", "provider_type": "custom", "models_dev_provider_id": "zeta"},
            ]

        monkeypatch.setattr(models_dev, "get_catalog", fake_catalog)
        monkeypatch.setattr(ps, "list_providers", fake_list_providers)
        response = await admin_client.get(f"{_BASE}/providers?local_provider_id=1")
        assert response.status_code == 200
        assert [provider["id"] for provider in response.json()["providers"]] == [
            "openai",
            "anthropic",
            "zeta",
        ]

    async def test_missing_local_provider_is_404_without_fetching_catalog(self, admin_client, monkeypatch):
        catalog_requested = False

        async def fake_catalog(*, refresh=False):
            nonlocal catalog_requested
            catalog_requested = True
            return _catalog()

        async def fake_list_providers():
            return [{"id": 1, "name": "OpenAI", "provider_type": "openai", "models_dev_provider_id": None}]

        monkeypatch.setattr(models_dev, "get_catalog", fake_catalog)
        monkeypatch.setattr(ps, "list_providers", fake_list_providers)
        response = await admin_client.get(f"{_BASE}/providers?local_provider_id=999")
        assert response.status_code == 404
        assert catalog_requested is False

    async def test_import_requires_admin(self, non_admin_client):
        response = await non_admin_client.post(
            f"{_BASE}/import",
            json={
                "local_provider_id": 1,
                "models_dev_provider_id": "openai",
                "selections": [{"local_model_id": 5, "models_dev_model_id": "openai/gpt-test"}],
            },
        )
        assert response.status_code == 403

    async def test_import_passes_exact_keys_and_refreshes_no_catalog_on_failure(self, admin_client, monkeypatch):
        captured = {}

        async def fake_catalog(*, refresh=False):
            captured["refresh"] = refresh
            return _catalog()

        async def fake_import(**kwargs):
            captured.update(kwargs)
            return [
                {
                    "id": 5,
                    "provider_id": 1,
                    "model_name": "gpt-test",
                    "display_name": None,
                    "is_active": True,
                    "input_price_per_million": "2",
                    "output_price_per_million": "8",
                    "models_dev_model_id": "openai/gpt-test",
                    "price_source": "models.dev",
                    "created_at": "2026-07-20T00:00:00+00:00",
                    "updated_at": "2026-07-20T00:00:00+00:00",
                }
            ]

        monkeypatch.setattr(models_dev, "get_catalog", fake_catalog)
        monkeypatch.setattr(ps, "import_models_dev_prices", fake_import)
        response = await admin_client.post(
            f"{_BASE}/import",
            json={
                "local_provider_id": 1,
                "models_dev_provider_id": "openai",
                "selections": [{"local_model_id": 5, "models_dev_model_id": "openai/gpt-test"}],
            },
        )
        assert response.status_code == 200
        assert captured["refresh"] is False
        assert captured["models_dev_provider_id"] == "openai"
        assert captured["selections"] == [{"local_model_id": 5, "models_dev_model_id": "openai/gpt-test"}]
        assert response.json()[0]["price_source"] == "models.dev"

    async def test_catalog_failure_never_starts_import(self, admin_client, monkeypatch):
        called = False

        async def failed_catalog(*, refresh=False):
            raise models_dev.ModelsDevCatalogError("timeout")

        async def fake_import(**kwargs):
            nonlocal called
            called = True
            return []

        monkeypatch.setattr(models_dev, "get_catalog", failed_catalog)
        monkeypatch.setattr(ps, "import_models_dev_prices", fake_import)
        response = await admin_client.post(
            f"{_BASE}/import",
            json={
                "local_provider_id": 1,
                "models_dev_provider_id": "openai",
                "selections": [{"local_model_id": 5, "models_dev_model_id": "openai/gpt-test"}],
            },
        )
        assert response.status_code == 502
        assert called is False

    async def test_import_conflict_is_409(self, admin_client, monkeypatch):
        async def fake_catalog(*, refresh=False):
            return _catalog()

        async def fake_import(**kwargs):
            raise errors.ModelsDevImportConflictError("remap")

        monkeypatch.setattr(models_dev, "get_catalog", fake_catalog)
        monkeypatch.setattr(ps, "import_models_dev_prices", fake_import)
        response = await admin_client.post(
            f"{_BASE}/import",
            json={
                "local_provider_id": 1,
                "models_dev_provider_id": "openai",
                "selections": [{"local_model_id": 5, "models_dev_model_id": "openai/gpt-test"}],
            },
        )
        assert response.status_code == 409


class _Response:
    def __init__(self, *, status_code=200, content_type="application/json", chunks=()):
        self.status_code = status_code
        self.headers = {"content-type": content_type}
        self._chunks = chunks

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk


class _Stream:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error

    async def __aenter__(self):
        if self.error:
            raise self.error
        return self.response

    async def __aexit__(self, *_):
        return False


class _Client:
    def __init__(self, stream):
        self._stream = stream

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    def stream(self, *_args, **_kwargs):
        return self._stream


class TestModelsDevDownload:
    async def test_download_preserves_body_and_fetch_time(self, monkeypatch):
        response = _Response(chunks=[b'{"providers":', b" {}}"])
        monkeypatch.setattr(models_dev.httpx, "AsyncClient", lambda **_: _Client(_Stream(response)))

        result = await models_dev._download()

        assert result["body"] == '{"providers": {}}'
        assert result["fetched_at"].endswith("+00:00")

    @pytest.mark.parametrize(
        "response",
        [
            _Response(status_code=502),
            _Response(content_type="text/html"),
            _Response(chunks=[b"x" * (10 * 1024 * 1024 + 1)]),
        ],
    )
    async def test_download_rejects_non_json_error_or_oversized_response(self, monkeypatch, response):
        monkeypatch.setattr(models_dev.httpx, "AsyncClient", lambda **_: _Client(_Stream(response)))
        with pytest.raises(models_dev.ModelsDevCatalogError):
            await models_dev._download()

    async def test_download_translates_transport_failure(self, monkeypatch):
        monkeypatch.setattr(
            models_dev.httpx,
            "AsyncClient",
            lambda **_: _Client(_Stream(error=models_dev.httpx.ReadTimeout("timeout"))),
        )
        with pytest.raises(models_dev.ModelsDevCatalogError):
            await models_dev._download()

    async def test_catalog_cache_keeps_original_fetch_time(self, monkeypatch):
        captured = {}

        async def fake_cached_call(key, ttl, fn, *, refresh=False):
            captured.update(key=key, ttl=ttl, refresh=refresh)
            return {"body": '{"providers": {}}', "fetched_at": "2026-07-20T00:00:00+00:00"}

        monkeypatch.setattr(models_dev, "cached_call", fake_cached_call)
        catalog = await models_dev.get_catalog(refresh=True)

        assert captured == {
            "key": "afterglow:chat:models-dev:api:v1",
            "ttl": 3600,
            "refresh": True,
        }
        assert catalog.fetched_at == "2026-07-20T00:00:00+00:00"
