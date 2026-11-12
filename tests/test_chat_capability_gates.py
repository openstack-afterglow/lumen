from lumen.services.capabilities import provider_capabilities


def test_provider_capabilities_fail_closed_for_unknown_model(monkeypatch):
    from lumen.services import provider_metadata

    monkeypatch.setattr(provider_metadata, "_catalog", lambda: {})

    caps = provider_capabilities("unknown", "openai")

    assert caps["input_modalities"] == ["text"]
    assert caps["feature_gates"]["image_input"] == {
        "available": False,
        "mode": "none",
        "reason_code": "metadata_unknown",
        "pricing_available": False,
    }
    assert caps["feature_gates"]["web_search"]["available"] is False


def test_provider_capabilities_preserve_exact_custom_provider_metadata(monkeypatch):
    from lumen.services import provider_metadata

    monkeypatch.setattr(provider_metadata, "_catalog", lambda: {
        "provider/model": {"litellm_provider": "custom", "supports_vision": True},
    })
    caps = provider_capabilities("provider/model", "custom")
    assert caps["feature_gates"]["image_input"]["available"] is True
    assert caps["capability_observations"]["supports_vision"] is True
    assert caps["capability_observations"]["supports_reasoning"] is None
    assert provider_capabilities("provider/model", "openai")["metadata_status"] == "missing"


def test_missing_reasoning_observation_is_not_a_denial(monkeypatch):
    from lumen.services import provider_metadata
    from lumen.services.capabilities import reasoning_explicitly_unsupported

    monkeypatch.setattr(provider_metadata, "_catalog", lambda: {
        "opaque": {"litellm_provider": "openai", "supports_vision": True},
        "denied": {"litellm_provider": "openai", "supports_reasoning": False},
    })
    assert not reasoning_explicitly_unsupported("opaque", None, None)
    assert not reasoning_explicitly_unsupported("unknown", None, None)
    assert reasoning_explicitly_unsupported("denied", None, None)
    assert reasoning_explicitly_unsupported("unknown", "override", {"reasoning": False})
