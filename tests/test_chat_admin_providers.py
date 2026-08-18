"""빌트인 AI 채팅 관리자 프로바이더/모델 CRUD 라우터 테스트.

DB 없이 provider_store 서비스 함수를 monkeypatch 하여 라우터 계약만 검증:
- require_admin 게이트(비관리자 403)
- api_key 응답 마스킹(has_api_key 만, 평문/암호문 미노출)
- 예외 → HTTP 상태 매핑(404/400/503)
"""

from decimal import Decimal

import pytest

from lumen.services.providers import errors, pricing
from lumen.services.providers import repository as ps

_PROVIDERS_URL = "/api/v1/chat/admin/providers"
_MODELS_URL = "/api/v1/chat/admin/models"


def _public_provider(**over) -> dict:
    base = {
        "id": 1,
        "name": "openai",
        "provider_type": "openai",
        "api_base": None,
        "has_api_key": True,
        "is_active": True,
        "margin_multiplier": 1.0,
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    base.update(over)
    return base


class TestAdminGate:
    async def test_list_providers_forbidden_for_non_admin(self, non_admin_client):
        resp = await non_admin_client.get(_PROVIDERS_URL)
        assert resp.status_code == 403

    async def test_create_provider_forbidden_for_non_admin(self, non_admin_client):
        resp = await non_admin_client.post(_PROVIDERS_URL, json={"name": "x"})
        assert resp.status_code == 403

    async def test_create_model_forbidden_for_non_admin(self, non_admin_client):
        resp = await non_admin_client.post(_MODELS_URL, json={"provider_id": 1, "model_name": "gpt-4o"})
        assert resp.status_code == 403


class TestProviderCrud:
    async def test_create_masks_api_key(self, admin_client, monkeypatch):
        captured = {}

        async def fake_create(**kwargs):
            captured.update(kwargs)
            return _public_provider(name=kwargs["name"], has_api_key=bool(kwargs.get("api_key")))

        monkeypatch.setattr(ps, "create_provider", fake_create)

        resp = await admin_client.post(
            _PROVIDERS_URL,
            json={"name": "openai", "api_key": "sk-super-secret-1234567890", "margin_multiplier": 1.2},
        )
        assert resp.status_code == 201
        body = resp.json()
        # 서비스는 평문 키를 전달받았지만, 응답에는 절대 노출되지 않아야 한다.
        assert captured["api_key"] == "sk-super-secret-1234567890"
        assert "api_key" not in body
        assert "encrypted_api_key" not in body
        assert body["has_api_key"] is True

    async def test_list_ok(self, admin_client, monkeypatch):
        async def fake_list():
            return [_public_provider()]

        monkeypatch.setattr(ps, "list_providers", fake_list)
        resp = await admin_client.get(_PROVIDERS_URL)
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 1
        assert "api_key" not in body[0]

    async def test_update_not_found_404(self, admin_client, monkeypatch):
        async def fake_update(provider_id, patch):
            raise errors.ProviderNotFoundError("없음")

        monkeypatch.setattr(ps, "update_provider", fake_update)
        resp = await admin_client.patch(f"{_PROVIDERS_URL}/999", json={"is_active": False})
        assert resp.status_code == 404

    async def test_update_active_executor_route_is_conflict(self, admin_client, monkeypatch):
        async def fake_update(provider_id, patch):
            raise errors.ActiveRunConfigurationConflict("run active")

        monkeypatch.setattr(ps, "update_provider", fake_update)
        resp = await admin_client.patch(f"{_PROVIDERS_URL}/1", json={"is_active": False})
        assert resp.status_code == 409

    async def test_create_validation_400(self, admin_client, monkeypatch):
        async def fake_create(**kwargs):
            raise errors.ProviderValidationError("중복")

        monkeypatch.setattr(ps, "create_provider", fake_create)
        resp = await admin_client.post(_PROVIDERS_URL, json={"name": "dup"})
        assert resp.status_code == 400

    async def test_storage_unavailable_503(self, admin_client, monkeypatch):
        async def fake_list():
            raise errors.ChatStorageUnavailable("DB down")

        monkeypatch.setattr(ps, "list_providers", fake_list)
        resp = await admin_client.get(_PROVIDERS_URL)
        assert resp.status_code == 503


class TestModelCrud:
    async def test_create_model_ok(self, admin_client, monkeypatch):
        async def fake_create(**kwargs):
            return {
                "id": 5,
                "provider_id": kwargs["provider_id"],
                "model_name": kwargs["model_name"],
                "display_name": kwargs.get("display_name"),
                "is_active": True,
                "input_price": None,
                "output_price": None,
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
            }

        monkeypatch.setattr(ps, "create_model", fake_create)
        resp = await admin_client.post(_MODELS_URL, json={"provider_id": 1, "model_name": "gpt-4o"})
        assert resp.status_code == 201
        assert resp.json()["model_name"] == "gpt-4o"

    async def test_create_model_bad_provider_400(self, admin_client, monkeypatch):
        async def fake_create(**kwargs):
            raise errors.ProviderValidationError("프로바이더 없음")

        monkeypatch.setattr(ps, "create_model", fake_create)
        resp = await admin_client.post(_MODELS_URL, json={"provider_id": 999, "model_name": "x"})
        assert resp.status_code == 400


class TestTitleModel:
    _URL = "/api/v1/chat/admin/title-model"

    async def test_forbidden_for_non_admin(self, non_admin_client):
        resp = await non_admin_client.put(self._URL, json={"model_id": 5})
        assert resp.status_code == 403

    async def test_set_title_model_ok(self, admin_client, monkeypatch):
        captured = {}

        async def fake_set(model_id):
            captured["model_id"] = model_id

        monkeypatch.setattr(ps, "set_title_model", fake_set)
        resp = await admin_client.put(self._URL, json={"model_id": 7})
        assert resp.status_code == 204
        assert captured["model_id"] == 7

    async def test_clear_title_model(self, admin_client, monkeypatch):
        captured = {}

        async def fake_set(model_id):
            captured["model_id"] = model_id

        monkeypatch.setattr(ps, "set_title_model", fake_set)
        resp = await admin_client.put(self._URL, json={"model_id": None})
        assert resp.status_code == 204
        assert captured["model_id"] is None

    async def test_set_title_model_not_found_404(self, admin_client, monkeypatch):
        async def fake_set(model_id):
            raise errors.ProviderNotFoundError("모델 없음")

        monkeypatch.setattr(ps, "set_title_model", fake_set)
        resp = await admin_client.put(self._URL, json={"model_id": 999})
        assert resp.status_code == 404


class TestModelPricingContract:
    async def test_create_uses_decimal_per_million_price_pair(self, admin_client, monkeypatch):
        captured = {}

        async def fake_create(**kwargs):
            captured.update(kwargs)
            return {
                "id": 5,
                "provider_id": kwargs["provider_id"],
                "model_name": kwargs["model_name"],
                "display_name": None,
                "is_active": True,
                "input_price_per_million": kwargs["input_price_per_million"],
                "output_price_per_million": kwargs["output_price_per_million"],
                "price_source": "manual",
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
            }

        monkeypatch.setattr(ps, "create_model", fake_create)
        resp = await admin_client.post(
            _MODELS_URL,
            json={
                "provider_id": 1,
                "model_name": "gpt-4o",
                "input_price_per_million": "2",
                "output_price_per_million": "8",
            },
        )

        assert resp.status_code == 201
        assert str(captured["input_price_per_million"]) == "2"
        assert str(captured["output_price_per_million"]) == "8"
        assert resp.json()["input_price_per_million"] == "2"
        assert resp.json()["output_price_per_million"] == "8"
        assert "input_price" not in resp.json()
        assert "output_price" not in resp.json()

    async def test_manual_price_requires_complete_pair(self, admin_client):
        resp = await admin_client.post(
            _MODELS_URL,
            json={"provider_id": 1, "model_name": "gpt-4o", "input_price_per_million": "2"},
        )
        assert resp.status_code == 422

    async def test_update_rejects_legacy_price_alias(self, admin_client):
        resp = await admin_client.patch(f"{_MODELS_URL}/5", json={"input_price": "0.000002"})
        assert resp.status_code == 422

    async def test_update_accepts_explicit_free_price_pair(self, admin_client, monkeypatch):
        captured = {}

        async def fake_update(model_id, patch):
            captured.update(patch)
            return {
                "id": model_id,
                "provider_id": 1,
                "model_name": "gpt-4o",
                "display_name": None,
                "is_active": True,
                "input_price_per_million": patch["input_price_per_million"],
                "output_price_per_million": patch["output_price_per_million"],
                "price_source": "manual",
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
            }

        monkeypatch.setattr(ps, "update_model", fake_update)
        resp = await admin_client.patch(
            f"{_MODELS_URL}/5",
            json={"input_price_per_million": "0", "output_price_per_million": "0"},
        )

        assert resp.status_code == 200
        assert str(captured["input_price_per_million"]) == "0"
        assert str(captured["output_price_per_million"]) == "0"
        assert resp.json()["price_source"] == "manual"

    async def test_precision_underflow_is_422_before_store(self, admin_client):
        resp = await admin_client.post(
            _MODELS_URL,
            json={
                "provider_id": 1,
                "model_name": "tiny",
                "input_price_per_million": "0.00004",
                "output_price_per_million": "0.00004",
            },
        )
        assert resp.status_code == 422

    def test_store_conversion_preserves_or_rejects_precision(self):
        assert pricing._per_token_price(Decimal("2"), "price") == Decimal("0.0000020000")
        with pytest.raises(errors.ProviderValidationError):
            pricing._per_token_price(Decimal("0.00004"), "price")

    async def test_update_rejects_mixed_null_price_pair(self, admin_client):
        response = await admin_client.patch(
            f"{_MODELS_URL}/5",
            json={"input_price_per_million": None, "output_price_per_million": "8"},
        )
        assert response.status_code == 422
