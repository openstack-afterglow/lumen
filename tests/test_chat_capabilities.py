from decimal import Decimal
from types import SimpleNamespace

import lumen.services.providers.credentials as provider_credentials
import lumen.services.providers.pricing as provider_pricing
import lumen.services.providers.routing as provider_store
from lumen.services.capabilities import litellm_capabilities, normalize_capabilities
from lumen.services.providers.pricing import _model_public, _pricing_aware_capabilities
from lumen.services.providers.routing import _resolved_model, _resolved_provider


def _model(*, metadata: dict, input_price: str | None = "0.000001", output_price: str | None = "0.000002"):
    return SimpleNamespace(price_metadata=metadata, input_price=input_price, output_price=output_price)


def _capabilities():
    return {
        "feature_gates": {
            "text": {"available": True, "pricing_available": True},
            "structured_output": {"available": True, "pricing_available": True},
            "web_search": {"available": True, "pricing_available": True},
            "web_fetch": {"available": True, "pricing_available": True},
            "image_output": {"available": True, "pricing_available": True},
        }
    }


def test_feature_gate_pricing_is_derived_from_model_prices_and_components():
    caps = _pricing_aware_capabilities(
        _model(
            metadata={
                "web_search_request_per_unit": "0.01",
                "web_search_context_low_per_unit": "0.001",
                "web_search_context_medium_per_unit": "0.002",
                "web_search_context_high_per_unit": "0.003",
            }
        ),
        _capabilities(),
    )

    gates = caps["feature_gates"]
    assert gates["text"]["pricing_available"] is True
    assert gates["structured_output"]["pricing_available"] is True
    assert gates["web_search"]["pricing_available"] is True
    assert gates["web_fetch"]["pricing_available"] is False
    assert gates["image_output"]["pricing_available"] is False


def test_image_input_is_disabled_when_the_scanned_asset_pipeline_is_unavailable(monkeypatch):
    caps = _capabilities()
    caps["feature_gates"]["image_input"] = {
        "available": True,
        "mode": "native",
        "reason_code": None,
        "pricing_available": True,
    }
    monkeypatch.setattr("lumen.services.assets.asset_pipeline_available", lambda: False)

    gated = _pricing_aware_capabilities(_model(metadata={}), caps)

    assert gated["feature_gates"]["image_input"] == {
        "available": False,
        "mode": "none",
        "reason_code": "asset_pipeline_unavailable",
        "pricing_available": False,
    }


def test_document_input_is_disabled_when_the_scanned_asset_pipeline_is_unavailable(monkeypatch):
    caps = _capabilities()
    caps["feature_gates"]["document_input"] = {
        "available": True,
        "mode": "native",
        "reason_code": None,
        "pricing_available": True,
    }
    monkeypatch.setattr("lumen.services.assets.asset_pipeline_available", lambda: False)

    gated = _pricing_aware_capabilities(_model(metadata={}), caps)

    assert gated["feature_gates"]["document_input"] == {
        "available": False,
        "mode": "none",
        "reason_code": "asset_pipeline_unavailable",
        "pricing_available": False,
    }


def test_text_and_structured_output_require_both_base_prices():
    caps = _pricing_aware_capabilities(_model(metadata={}, output_price=None), _capabilities())

    assert caps["feature_gates"]["text"]["pricing_available"] is False
    assert caps["feature_gates"]["structured_output"]["pricing_available"] is False


def test_component_prices_use_persisted_cost_envelope():
    caps = _pricing_aware_capabilities(
        _model(
            metadata={
                "source_url": "https://models.dev/example",
                "cost": {
                    "image_per_unit": "0.04",
                },
            }
        ),
        _capabilities(),
    )

    assert caps["feature_gates"]["image_output"]["pricing_available"] is True


def test_models_dev_legacy_capabilities_are_normalized_to_feature_gates():
    normalized = normalize_capabilities(
        {
            "vision": True,
            "tool_call": True,
            "modalities": {"input": ["text", "image"], "output": ["text", "image"]},
        },
        litellm_capabilities("example-model", "openai"),
    )

    assert normalized["function_calling"] is True
    assert normalized["feature_gates"]["image_input"]["available"] is True
    assert normalized["feature_gates"]["image_output"]["available"] is True


def test_models_dev_pdf_modality_enables_the_document_input_gate():
    normalized = normalize_capabilities(
        {
            "modalities": {"input": ["text", "pdf"], "output": ["text"]},
        },
        litellm_capabilities("example-model", "openai"),
    )

    assert normalized["input_modalities"] == ["text", "pdf"]
    assert normalized["feature_gates"]["document_input"]["available"] is True


def test_stored_canonical_gates_and_detected_modalities_are_preserved():
    detected = litellm_capabilities("example-model", "openai")
    detected["input_modalities"] = ["text"]
    detected["output_modalities"] = ["text"]
    normalized = normalize_capabilities(
        {
            "feature_gates": {
                "web_search": {
                    "available": True,
                    "mode": "managed",
                    "reason_code": None,
                    "pricing_available": True,
                }
            }
        },
        detected,
    )

    assert normalized["feature_gates"]["web_search"]["mode"] == "managed"
    assert normalized["feature_gates"]["web_search"]["pricing_available"] is True
    assert normalized["input_modalities"] == ["text"]
    assert normalized["output_modalities"] == ["text"]


def test_stored_canonical_modality_gates_are_not_clobbered_without_legacy_fields():
    normalized = normalize_capabilities(
        {
            "feature_gates": {
                "image_input": {"available": True, "mode": "managed", "reason_code": None, "pricing_available": True},
                "audio_output": {"available": True, "mode": "managed", "reason_code": None, "pricing_available": True},
            }
        },
        litellm_capabilities("example-model", "openai"),
    )

    assert normalized["feature_gates"]["image_input"]["mode"] == "managed"
    assert normalized["feature_gates"]["audio_output"]["mode"] == "managed"


def test_resolved_model_uses_a_secret_keyed_config_version_hash(monkeypatch):
    model = SimpleNamespace(
        id=7,
        model_name="gpt-test",
        input_price="0.000001",
        output_price="0.000002",
        price_source="manual",
        price_metadata={},
        capabilities=_capabilities(),
        capability_source="override",
        is_active=True,
    )
    provider = SimpleNamespace(
        id=3,
        name="test-provider",
        provider_type="openai",
        api_base="https://api.example.test/v1",
        encrypted_api_key="ciphertext",
        is_active=True,
        margin_multiplier="1.25",
    )
    decrypted_key = {"value": "first-secret"}
    monkeypatch.setattr(provider_credentials, "decrypt_llm_provider_key", lambda _: decrypted_key["value"])
    monkeypatch.setattr(provider_store, "derive_encryption_subkey", lambda _: b"test-hmac-key")

    first = _resolved_model(model, provider)
    decrypted_key["value"] = "rotated-secret"
    second = _resolved_model(model, provider)

    assert first["config_version_hash"] != second["config_version_hash"]
    assert first["config_version_hash"] != "first-secret"
    assert "first-secret" not in first["config_version_hash"]


def test_resolved_provider_uses_secret_keyed_config_version_hash(monkeypatch):
    provider = SimpleNamespace(
        id=3,
        name="search-provider",
        provider_type="perplexity",
        api_base="https://search.example.test",
        encrypted_api_key="ciphertext",
        is_active=True,
        margin_multiplier="1.25",
    )
    decrypted_key = {"value": "first-secret"}
    monkeypatch.setattr(provider_credentials, "decrypt_llm_provider_key", lambda _: decrypted_key["value"])
    monkeypatch.setattr(provider_store, "derive_encryption_subkey", lambda _: b"test-hmac-key")

    first = _resolved_provider(provider)
    decrypted_key["value"] = "rotated-secret"
    second = _resolved_provider(provider)

    assert first["config_version_hash"] != second["config_version_hash"]
    assert first["config_version_hash"] != "first-secret"
    assert first["provider_name"] == "search-provider"


def test_resolved_model_gates_litellm_fallback_prices(monkeypatch):
    model = SimpleNamespace(
        id=7,
        model_name="fallback-model",
        input_price=None,
        output_price=None,
        price_source=None,
        price_metadata=None,
        capabilities=_capabilities(),
        capability_source="override",
        is_active=True,
    )
    provider = SimpleNamespace(
        id=3,
        name="test-provider",
        provider_type="openai",
        api_base=None,
        encrypted_api_key=None,
        margin_multiplier="1",
        is_active=True,
    )
    monkeypatch.setattr(
        provider_pricing,
        "effective_prices_per_million",
        lambda *_: (Decimal("1"), Decimal("2")),
    )

    resolved = _resolved_model(model, provider)

    assert resolved["price_source"] == "litellm"
    assert resolved["input_price_per_token"] == Decimal("0.0000010000")
    assert resolved["output_price_per_token"] == Decimal("0.0000020000")
    assert resolved["capabilities"]["feature_gates"]["text"]["pricing_available"] is True


def test_public_model_marks_unpriced_text_route_unavailable():
    model = SimpleNamespace(
        id=7,
        provider_id=3,
        model_name="unpriced-model",
        display_name=None,
        is_active=True,
        is_title_model=False,
        is_memory_model=False,
        input_price=None,
        output_price=None,
        price_source=None,
        price_metadata=None,
        capabilities=_capabilities(),
        capability_source="override",
        models_dev_model_id=None,
        created_at=None,
        updated_at=None,
    )

    public = _model_public(model, provider_type="openai")

    assert public["effective_capabilities"]["feature_gates"]["text"]["pricing_available"] is False
