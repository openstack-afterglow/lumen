"""모델 능력(capability) 해석 precedence 단위 테스트 — override > models_dev > litellm."""

from __future__ import annotations

import pytest

import lumen.services.capabilities as capability_service
from lumen.services.providers import routing as ps


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

    def test_manual_context_limit_override_wins_catalog_detection(self):
        row = _Row(
            model_name="perplexity/perplexity/sonar",
            capabilities={"context_limit": 64_000},
            capability_source="override",
        )

        capabilities, source = ps._effective_capabilities(row, "perplexity")

        assert source == "override"
        assert capabilities["context_limit"] == 64_000

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

    @pytest.mark.parametrize("model_name", ("perplexity/sonar", "perplexity/glm-5.3"))
    def test_stale_perplexity_and_glm_legacy_search_overrides_cannot_enable_native_search(
        self, monkeypatch, model_name
    ):
        monkeypatch.setattr(capability_service, "_native_web_search_available", lambda *_args: False)

        capabilities, source = ps._effective_capabilities(
            _Row(model_name=model_name, capabilities={"web_search": True}, capability_source="override"),
            "perplexity",
        )

        assert source == "override"
        assert capabilities["web_search"] is False
        assert capabilities["web_search_required"] is False
        assert capabilities["feature_gates"]["web_search"] == {
            "available": False,
            "mode": "none",
            "reason_code": "provider_unsupported",
            "pricing_available": False,
        }

    @pytest.mark.parametrize(("transport_available", "expected_required"), ((False, False), (True, True)))
    def test_sonar_requires_native_search_only_when_exact_transport_supports_it(
        self, monkeypatch, transport_available, expected_required
    ):
        monkeypatch.setattr(
            capability_service,
            "_native_web_search_available",
            lambda *_args: transport_available,
        )

        capabilities, _ = ps._effective_capabilities(
            _Row(
                model_name="perplexity/sonar",
                capabilities={"web_search": True},
                capability_source="override",
            ),
            "perplexity",
        )

        assert capabilities["web_search"] is transport_available
        assert capabilities["web_search_required"] is expected_required
