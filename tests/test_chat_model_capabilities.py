"""Capability precedence: administrator override > native catalog > safe defaults."""

from __future__ import annotations

import pytest

import lumen.services.capabilities as capability_service
from lumen.services.providers import routing as ps


@pytest.fixture
def native_catalog(monkeypatch):
    from lumen.services import provider_metadata

    def install(provider_type=None, model_name="m", **fields):
        providers = {}
        if provider_type is not None:
            source_url = {
                "openai": "https://developers.openai.com/api/docs/pricing",
                "anthropic": "https://docs.anthropic.com/en/docs/about-claude/pricing",
                "perplexity": "https://docs.perplexity.ai/guides/pricing",
            }[provider_type]
            flags = fields.pop("capabilities", {})
            entry = {
                "id": model_name,
                "aliases": [],
                "kind": "text",
                "display_name": model_name,
                "status": "active",
                "context_window": None,
                "max_output_tokens": None,
                "input_modalities": ["text"],
                "output_modalities": ["text"],
                "capabilities": {
                    flag: flags.get(flag, False)
                    for flag in (
                        "vision", "tool_call", "reasoning", "prompt_caching",
                        "structured_output", "web_search", "decisions",
                    )
                },
                "token_pricing": None,
                "media_pricing": None,
                "source_urls": [source_url],
                "verified_at": "2026-11-17",
                "notes": None,
                **fields,
            }
            providers[provider_type] = {
                "display_name": provider_type,
                "pricing_sources": [source_url],
                "models": [entry],
            }
        catalog = {"schema_version": 1, "catalog_version": "2099-01-01", "providers": providers}
        monkeypatch.setattr(provider_metadata, "native_catalog", lambda: catalog)
        return catalog

    return install


class _Row:
    """LlmModel 스텁 — _effective_capabilities 가 읽는 속성만."""

    def __init__(self, model_name="m", capabilities=None, capability_source=None):
        self.model_name = model_name
        self.capabilities = capabilities
        self.capability_source = capability_source


class TestEffectiveCapabilities:
    def test_stored_override_wins(self, native_catalog):
        native_catalog("anthropic")
        caps = {"vision": True, "reasoning": False, "tool_call": True}
        row = _Row(capabilities=caps, capability_source="override")
        eff, source = ps._effective_capabilities(row, "anthropic")
        assert source == "override"
        assert eff["vision"] is True and eff["reasoning"] is False and eff["tool_call"] is True
        assert eff["function_calling"] is True
        assert eff["feature_gates"]["image_input"]["available"] is True

    def test_manual_context_limit_override_wins_catalog_detection(self, native_catalog):
        native_catalog("perplexity", "perplexity/sonar", context_window=128_000)
        row = _Row(
            model_name="perplexity/perplexity/sonar",
            capabilities={"context_limit": 64_000},
            capability_source="override",
        )

        capabilities, source = ps._effective_capabilities(row, "perplexity")

        assert source == "override"
        assert capabilities["context_limit"] == 64_000

    @pytest.mark.parametrize("stored_source", ("models_dev", "models.dev", "manual", None))
    def test_imported_capabilities_do_not_override_native_catalog(self, native_catalog, stored_source):
        native_catalog("openai", capabilities={"reasoning": True})
        row = _Row(
            capabilities={"vision": True, "reasoning": False, "decisions": True},
            capability_source=stored_source,
        )

        eff, source = ps._effective_capabilities(row, "openai")

        assert source == "native_catalog"
        assert row.capability_source == stored_source
        assert eff["vision"] is False
        assert eff["reasoning"] is True
        assert eff["decisions"] is False
        assert eff["metadata_version"] == "2099-01-01"
        assert eff["feature_gates"]["image_input"]["available"] is False
        assert eff["feature_gates"]["decisions"]["available"] is False

    @pytest.mark.parametrize("stored_source", ("models_dev", "models.dev", "manual", None))
    def test_imported_capabilities_do_not_override_safe_defaults(self, native_catalog, stored_source):
        native_catalog()
        row = _Row(
            capabilities={"vision": True, "reasoning": True, "tool_call": True, "decisions": True},
            capability_source=stored_source,
        )

        eff, source = ps._effective_capabilities(row, "openai")

        assert source == "defaults"
        assert eff["vision"] is False
        assert eff["reasoning"] is False
        assert eff["tool_call"] is False
        assert eff["decisions"] is False
        assert eff["feature_gates"]["image_input"]["available"] is False
        assert eff["feature_gates"]["decisions"]["available"] is False

    def test_catalog_fallback_when_no_stored(self, native_catalog):
        native_catalog(
            "anthropic", "claude-x",
            capabilities={"vision": True, "reasoning": True, "tool_call": False},
            input_modalities=["text", "image"],
        )
        row = _Row(model_name="claude-x", capabilities=None)
        eff, source = ps._effective_capabilities(row, "anthropic")
        assert source == "native_catalog"
        assert eff["vision"] is True and eff["reasoning"] is True and eff["tool_call"] is False
        assert eff["attachment"] is True  # attachment ≈ vision
        assert eff["modalities"] is None

    def test_catalog_missing_is_safe_false(self, native_catalog):
        native_catalog()
        eff, source = ps._effective_capabilities(_Row(capabilities=None), None)
        assert source == "defaults"
        assert eff["vision"] is False and eff["reasoning"] is False and eff["tool_call"] is False

    @pytest.mark.parametrize("provider_type", ("openai", "anthropic"))
    @pytest.mark.parametrize("stored_source", (None, "models_dev", "override"))
    def test_compatible_route_uses_only_explicit_overrides(self, monkeypatch, provider_type, stored_source):
        from lumen.services import provider_metadata

        def forbidden_catalog():
            raise AssertionError("compatible routes must not read native catalog")

        monkeypatch.setattr(provider_metadata, "native_catalog", forbidden_catalog)
        row = _Row(
            model_name="gpt-6-luna",
            capabilities={
                "vision": True, "tool_call": True, "reasoning": True,
                "structured_output": True, "prompt_caching": True, "decisions": True,
            },
            capability_source=stored_source,
        )

        eff, source = ps._effective_capabilities(
            row, provider_type, api_base="https://compatible.example.test/v1",
        )

        overridden = stored_source == "override"
        assert source == ("override" if overridden else "defaults")
        for flag in (
            "vision", "tool_call", "function_calling", "reasoning",
            "structured_output", "prompt_caching", "decisions",
        ):
            assert eff[flag] is overridden
        for feature in ("image_input", "structured_output", "decisions"):
            assert eff["feature_gates"][feature]["available"] is overridden
        assert eff["web_search"] is False
        assert eff["context_limit"] is None
        assert eff["metadata_version"] is None

    @pytest.mark.parametrize("available", (False, True))
    def test_decisions_override_wins_native_catalog(self, native_catalog, available):
        native_catalog("openai", capabilities={"decisions": not available})
        row = _Row(capabilities={"decisions": available}, capability_source="override")

        eff, source = ps._effective_capabilities(row, "openai")

        assert source == "override"
        assert eff["decisions"] is available
        assert eff["feature_gates"]["decisions"] == {
            "available": available,
            "mode": "native" if available else "none",
            "reason_code": None if available else "provider_unsupported",
            "pricing_available": available,
        }

    @pytest.mark.parametrize("model_name", ("perplexity/sonar", "perplexity/glm-5.3"))
    def test_stale_perplexity_and_glm_legacy_search_overrides_cannot_enable_native_search(
        self, monkeypatch, model_name
    ):
        monkeypatch.setattr(capability_service, "_native_web_search_available", lambda _model, _provider, _base: False)

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
            lambda _model, _provider, _base: transport_available,
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
