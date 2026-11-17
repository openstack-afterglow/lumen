from unittest.mock import Mock

import pytest

from lumen.services.capabilities import normalize_capabilities, provider_capabilities


def test_provider_capabilities_fail_closed_for_unknown_model(monkeypatch):
    from lumen.services import capabilities

    monkeypatch.setattr(capabilities, "catalog_model_metadata", Mock(return_value={}))

    caps = provider_capabilities("unknown", "openai")

    assert caps["input_modalities"] == ["text"]
    assert caps["feature_gates"]["image_input"] == {
        "available": False,
        "mode": "none",
        "reason_code": "metadata_unknown",
        "pricing_available": False,
    }
    assert caps["feature_gates"]["web_search"]["available"] is False


@pytest.mark.parametrize("provider_type", ["openai", "anthropic"])
def test_compatible_provider_skips_catalog_and_requires_explicit_vision_override(
    monkeypatch, provider_type
):
    from lumen.services import provider_metadata

    catalog = Mock(
        side_effect=AssertionError("Compatible endpoints must not read the native catalog")
    )
    monkeypatch.setattr(provider_metadata, "native_catalog", catalog)
    detected = provider_capabilities(
        "provider/model", provider_type, "https://compatible.example/v1"
    )

    assert detected["feature_gates"]["image_input"]["available"] is False
    assert detected["metadata_status"] == "missing"
    assert detected["capability_observations"]["supports_vision"] is None
    assert detected["capability_observations"]["supports_reasoning"] is None

    caps = normalize_capabilities({"vision": True}, detected)

    assert caps["feature_gates"]["image_input"]["available"] is True
    assert caps["vision"] is True
    assert caps["reasoning"] is False
    assert caps["function_calling"] is False
    assert caps["feature_gates"]["document_input"]["available"] is False
    assert caps["feature_gates"]["structured_output"]["available"] is False
    assert caps["feature_gates"]["web_search"]["available"] is False
    catalog.assert_not_called()


@pytest.mark.parametrize(
    ("model_name", "source", "overrides", "unsupported"),
    [
        pytest.param("opaque", None, None, False, id="missing-reasoning-is-not-denial"),
        pytest.param("unknown", None, None, False, id="missing-model-is-not-denial"),
        pytest.param("denied", None, None, True, id="catalog-false-is-denial"),
        pytest.param(
            "unknown", "override", {"reasoning": False}, True, id="override-false-is-denial"
        ),
        pytest.param(
            "denied", "override", {"reasoning": True}, False, id="override-true-wins-denial"
        ),
    ],
)
def test_reasoning_requires_explicit_denial(
    monkeypatch, model_name, source, overrides, unsupported
):
    from lumen.services import capabilities
    from lumen.services.capabilities import reasoning_explicitly_unsupported

    metadata = {
        "opaque": {"supports_vision": True},
        "denied": {"supports_reasoning": False},
    }
    catalog_metadata = Mock(
        side_effect=lambda model, provider_type, api_base: metadata.get(model, {})
    )
    monkeypatch.setattr(capabilities, "catalog_model_metadata", catalog_metadata)

    assert reasoning_explicitly_unsupported(
        model_name, source, overrides, provider_type="openai"
    ) is unsupported
    if source == "override":
        catalog_metadata.assert_not_called()
    else:
        catalog_metadata.assert_called_once_with(model_name, "openai", None)
