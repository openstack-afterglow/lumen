from decimal import Decimal
from types import SimpleNamespace

import pytest

import lumen.services.capabilities as capability_service
import lumen.services.providers.credentials as provider_credentials
import lumen.services.providers.pricing as provider_pricing
import lumen.services.providers.routing as provider_store
from lumen.services.capabilities import litellm_capabilities, normalize_capabilities
from lumen.services.providers.pricing import _effective_capabilities, _model_public, _pricing_aware_capabilities
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


def _detected_native_search_capabilities(*, available: bool, required: bool = False):
    return {
        "web_search": available,
        "web_search_required": required,
        "feature_gates": {
            "web_search": {
                "available": available,
                "mode": "native" if available else "none",
                "reason_code": None if available else "provider_unsupported",
                "pricing_available": available,
            }
        },
    }


def test_perplexity_catalog_context_limit_uses_canonical_agent_identifier(monkeypatch):
    import litellm

    monkeypatch.setattr(
        litellm,
        "model_cost",
        {"perplexity/sonar": {"max_input_tokens": 128_000}},
    )
    monkeypatch.setattr(capability_service, "_probe", lambda *_args, **_kwargs: False)

    capabilities = litellm_capabilities("perplexity/perplexity/sonar", "perplexity")

    assert capabilities["context_limit"] == 128_000


def test_unknown_perplexity_catalog_context_limit_remains_unknown(monkeypatch):
    import litellm

    monkeypatch.setattr(litellm, "model_cost", {})
    monkeypatch.setattr(capability_service, "_probe", lambda *_args, **_kwargs: False)

    capabilities = litellm_capabilities("perplexity/perplexity/glm-5.3", "perplexity")

    assert capabilities["context_limit"] is None


def test_admin_override_without_a_window_keeps_the_exact_catalog_limit(monkeypatch):
    """CapabilitiesInput always serializes context_limit, so null must not erase detection."""
    import litellm

    monkeypatch.setattr(litellm, "model_cost", {"perplexity/sonar": {"max_input_tokens": 128_000}})
    monkeypatch.setattr(capability_service, "_probe", lambda *_args, **_kwargs: False)
    detected = litellm_capabilities("perplexity/perplexity/sonar", "perplexity")

    stored_flag_only = normalize_capabilities({"tool_call": False, "context_limit": None}, detected)
    assert stored_flag_only["context_limit"] == 128_000
    assert stored_flag_only["function_calling"] is False

    assert normalize_capabilities({"context_limit": 0}, detected)["context_limit"] == 128_000
    assert normalize_capabilities({"context_limit": "200000"}, detected)["context_limit"] == 128_000
    assert normalize_capabilities({"context_limit": 64_000}, detected)["context_limit"] == 64_000


def test_admin_override_cannot_invent_a_window_for_an_uncatalogued_model(monkeypatch):
    import litellm

    monkeypatch.setattr(litellm, "model_cost", {})
    monkeypatch.setattr(capability_service, "_probe", lambda *_args, **_kwargs: False)
    detected = litellm_capabilities("perplexity/perplexity/glm-5.3", "perplexity")

    assert normalize_capabilities({"tool_call": True, "context_limit": None}, detected)["context_limit"] is None


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


def test_native_search_uses_selected_model_price_not_managed_component_prices():
    caps = _capabilities()
    caps["feature_gates"]["web_search"]["mode"] = "native"

    gated = _pricing_aware_capabilities(_model(metadata={}), caps)

    assert gated["feature_gates"]["web_search"]["pricing_available"] is True

    unpriced = _pricing_aware_capabilities(_model(metadata={}, input_price=None, output_price=None), caps)
    assert unpriced["feature_gates"]["web_search"]["pricing_available"] is False


def test_native_token_pricing_does_not_admit_unpriced_managed_search():
    from fastapi import HTTPException

    from lumen.models.chat_contracts import ChatFeatureOptions
    from lumen.services.chat_admission import _require_execution_capability

    caps = _capabilities()
    caps["feature_gates"]["web_search"]["mode"] = "native"
    resolved = {
        "capabilities": _pricing_aware_capabilities(_model(metadata={}), caps),
        "input_price_per_token": Decimal("0.000001"),
        "output_price_per_token": Decimal("0.000002"),
        "price_metadata": {},
    }
    features = ChatFeatureOptions.model_validate({"web_search": {"enabled": True, "mode": "managed", "provider_id": 7}})

    with pytest.raises(HTTPException) as error:
        _require_execution_capability(features, resolved)
    assert error.value.status_code == 422
    assert "pricing_unavailable" in error.value.detail


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


def test_managed_search_does_not_retain_native_sonar_requirement():
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
        _detected_native_search_capabilities(available=True, required=True),
    )

    assert normalized["feature_gates"]["web_search"]["mode"] == "managed"
    assert normalized["web_search_required"] is False


def test_stored_native_search_gate_cannot_enable_unsupported_transport():
    normalized = normalize_capabilities(
        {
            "feature_gates": {
                "web_search": {
                    "available": True,
                    "mode": "native",
                    "reason_code": None,
                    "pricing_available": True,
                }
            }
        },
        _detected_native_search_capabilities(available=False),
    )

    assert normalized["web_search"] is False
    assert normalized["web_search_required"] is False
    assert normalized["feature_gates"]["web_search"] == {
        "available": False,
        "mode": "none",
        "reason_code": "provider_unsupported",
        "pricing_available": False,
    }


def test_admin_native_search_disable_wins_over_detected_support():
    normalized = normalize_capabilities(
        {
            "feature_gates": {
                "web_search": {
                    "available": False,
                    "mode": "native",
                    "reason_code": "admin_disabled",
                    "pricing_available": True,
                }
            }
        },
        _detected_native_search_capabilities(available=True),
    )

    assert normalized["web_search"] is False
    assert normalized["feature_gates"]["web_search"] == {
        "available": False,
        "mode": "none",
        "reason_code": "admin_disabled",
        "pricing_available": False,
    }


def test_legacy_native_search_enable_preserves_detected_api_support():
    normalized = normalize_capabilities(
        {"web_search": True},
        _detected_native_search_capabilities(available=True),
    )

    assert normalized["web_search"] is True
    assert normalized["feature_gates"]["web_search"] == {
        "available": True,
        "mode": "native",
        "reason_code": None,
        "pricing_available": True,
    }


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
    monkeypatch.setattr(provider_pricing, "effective_prices_per_million", lambda *_, **__: (Decimal("1"), Decimal("2")))

    resolved = _resolved_model(model, provider)

    assert resolved["price_source"] == "litellm"
    assert resolved["input_price_per_token"] == Decimal("0.0000010000")
    assert resolved["output_price_per_token"] == Decimal("0.0000020000")
    assert resolved["capabilities"]["feature_gates"]["text"]["pricing_available"] is True


def test_perplexity_agent_prices_resolve_from_canonical_or_documented_exact_routes():
    provider = SimpleNamespace(provider_type="perplexity", api_base="https://api.perplexity.ai/v1")
    sonar = SimpleNamespace(
        model_name="perplexity/perplexity/sonar",
        input_price=None,
        output_price=None,
        price_source=None,
        price_metadata=None,
        updated_at=None,
    )
    glm = SimpleNamespace(
        model_name="perplexity/perplexity/glm-5.3",
        input_price=None,
        output_price=None,
        price_source=None,
        price_metadata=None,
        updated_at=None,
    )

    assert provider_pricing._resolved_base_prices(sonar, provider)[:3] == (
        Decimal("0.0000002500"),
        Decimal("0.0000025000"),
        "perplexity_agent_api_2026-09",
    )
    assert provider_pricing._resolved_base_prices(glm, provider)[:3] == (
        Decimal("0.0000014000"),
        Decimal("0.0000044000"),
        "perplexity_agent_api_2026-09",
    )


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


def test_chatgpt_subscription_capabilities_use_static_metadata_without_provider_probe(monkeypatch):
    monkeypatch.setattr(
        capability_service,
        "_probe",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("provider probe is forbidden")),
    )

    capabilities = litellm_capabilities("chatgpt/gpt-5.2-codex", "chatgpt")

    assert capabilities["responses_api"] is True
    assert capabilities["endpoints"] == ["responses"]
    assert capabilities["structured_output"] is False


def test_chatgpt_subscription_override_cannot_enable_structured_output():
    model = SimpleNamespace(
        model_name="chatgpt/gpt-5.2-codex",
        capabilities={"structured_output": True, "tools": True},
        capability_source="override",
    )

    capabilities, source = _effective_capabilities(model, "chatgpt", "chatgpt_device")

    assert source == "override"
    assert capabilities["tools"] is True
    assert capabilities["structured_output"] is False


@pytest.mark.parametrize("auth_mode", ("chatgpt_device", "anthropic_subscription"))
def test_subscription_capability_limits_disable_native_web_search(auth_mode):
    capabilities = capability_service.apply_subscription_capability_limits(
        {
            "web_search": True,
            "web_search_required": True,
            "feature_gates": {
                "web_search": {"available": True, "mode": "native", "reason_code": None, "pricing_available": True}
            },
        },
        auth_mode,
    )

    assert capabilities["web_search"] is False
    assert capabilities["web_search_required"] is False
    assert capabilities["feature_gates"]["web_search"] == {
        "available": False,
        "mode": "none",
        "reason_code": "provider_unsupported",
        "pricing_available": False,
    }
