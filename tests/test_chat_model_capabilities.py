"""모델 능력(capability) 해석 precedence 단위 테스트 — override > models_dev > litellm."""

from __future__ import annotations

from lumen.services import provider_store as ps


class _Row:
    """LlmModel 스텁 — _effective_capabilities 가 읽는 속성만."""

    def __init__(self, model_name="m", capabilities=None, capability_source=None):
        self.model_name = model_name
        self.capabilities = capabilities
        self.capability_source = capability_source


class TestEffectiveCapabilities:
    def test_stored_override_wins(self):
        caps = {"vision": True, "reasoning": False, "tool_call": True}
        row = _Row(capabilities=caps, capability_source="override")
        eff, source = ps._effective_capabilities(row, "anthropic")
        assert source == "override"
        assert eff["vision"] is True and eff["reasoning"] is False and eff["tool_call"] is True
        assert eff["function_calling"] is True
        assert eff["feature_gates"]["image_input"]["available"] is True

    def test_models_dev_source_preserved(self):
        caps = {"vision": True}
        row = _Row(capabilities=caps, capability_source="models_dev")
        eff, source = ps._effective_capabilities(row, "openai")
        assert source == "models_dev"
        assert eff["vision"] is True
        assert eff["feature_gates"]["image_input"]["available"] is True

    def test_litellm_fallback_when_no_stored(self, monkeypatch):
        import litellm

        monkeypatch.setattr(litellm, "supports_vision", lambda model: True)
        monkeypatch.setattr(litellm, "supports_reasoning", lambda model: True)
        monkeypatch.setattr(litellm, "supports_function_calling", lambda model: False)
        row = _Row(model_name="claude-x", capabilities=None)
        eff, source = ps._effective_capabilities(row, "anthropic")
        assert source == "litellm"
        assert eff["vision"] is True and eff["reasoning"] is True and eff["tool_call"] is False
        assert eff["attachment"] is True  # attachment ≈ vision
        assert eff["modalities"] is None  # litellm 은 modality 목록 미제공

    def test_litellm_errors_are_safe_false(self, monkeypatch):
        import litellm

        def _boom(model):
            raise RuntimeError("unknown model")

        monkeypatch.setattr(litellm, "supports_vision", _boom)
        monkeypatch.setattr(litellm, "supports_reasoning", _boom)
        monkeypatch.setattr(litellm, "supports_function_calling", _boom)
        eff, source = ps._effective_capabilities(_Row(capabilities=None), None)
        assert source == "litellm"
        assert eff["vision"] is False and eff["reasoning"] is False and eff["tool_call"] is False
