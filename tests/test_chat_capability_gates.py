from lumen.services.capabilities import litellm_capabilities


def test_litellm_capabilities_fail_closed_for_unknown_model(monkeypatch):
    import litellm

    monkeypatch.setattr(litellm, "supports_vision", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(litellm, "supports_reasoning", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(litellm, "supports_function_calling", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(litellm, "supports_response_schema", lambda *_args, **_kwargs: False, raising=False)

    caps = litellm_capabilities("unknown", "openai")

    assert caps["input_modalities"] == ["text"]
    assert caps["feature_gates"]["image_input"] == {
        "available": False,
        "mode": "none",
        "reason_code": "provider_unsupported",
        "pricing_available": False,
    }
    assert caps["feature_gates"]["web_search"]["available"] is False


def test_litellm_capabilities_preserve_custom_provider_probe(monkeypatch):
    import litellm

    observed = []

    def vision(*, model, custom_llm_provider):
        observed.append((model, custom_llm_provider))
        return True

    monkeypatch.setattr(litellm, "supports_vision", vision)
    monkeypatch.setattr(litellm, "supports_reasoning", lambda **_kwargs: False)
    monkeypatch.setattr(litellm, "supports_function_calling", lambda **_kwargs: False)
    monkeypatch.setattr(litellm, "supports_response_schema", lambda **_kwargs: False, raising=False)

    caps = litellm_capabilities("provider/model", "custom")

    assert observed == [("provider/model", "custom")]
    assert caps["feature_gates"]["image_input"]["available"] is True
