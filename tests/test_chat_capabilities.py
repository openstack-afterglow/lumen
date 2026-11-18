from decimal import Decimal
from types import SimpleNamespace

import pytest

import lumen.services.capabilities as capability_service
import lumen.services.providers.credentials as provider_credentials
import lumen.services.providers.pricing as provider_pricing
import lumen.services.providers.routing as provider_store
from lumen.services.capabilities import normalize_capabilities, provider_capabilities
from lumen.services.providers.pricing import _effective_capabilities, _model_public, _pricing_aware_capabilities
from lumen.services.providers.routing import _resolved_model, _resolved_provider


@pytest.fixture
def native_catalog(monkeypatch):
    from lumen.services import provider_metadata

    def install(provider_type=None, model_name="example-model", **fields):
        providers = {}
        if provider_type is not None:
            source_url = {
                "openai": "https://developers.openai.com/api/docs/pricing",
                "anthropic": "https://docs.anthropic.com/en/docs/about-claude/pricing",
                "gemini": "https://ai.google.dev/gemini-api/docs/pricing",
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


def _attach_credentials(provider):
    credential = SimpleNamespace(
        id=11, account_id=9, auth_mode="api_key", generation=0, is_active=True,
        allowed_model_ids=None, allowed_capabilities=None, encrypted_api_key="ciphertext",
        api_key_env=None, subscription_status="configured", priority=0, weight=1,
    )
    account = SimpleNamespace(
        id=9, provider_id=provider.id, credentials=[credential], is_active=True,
        allowed_model_ids=None, allowed_capabilities=None, priority=0, weight=1, quota_group=None,
    )
    provider.accounts = [account]
    provider.auth_mode = "api_key"
    provider.selection_policy = "priority"
    provider.selected_credential_id = None
    return credential

def _model(*, metadata: dict, input_price: str | None = "0.000001", output_price: str | None = "0.000002"):
    return SimpleNamespace(price_metadata=metadata, input_price=input_price, output_price=output_price)


def _capabilities():
    return {
        "feature_gates": {
            "text": {"available": True, "pricing_available": True},
            "structured_output": {"available": True, "pricing_available": True},
            "web_search": {"available": True, "mode": "managed", "pricing_available": True},
            "web_fetch": {"available": True, "pricing_available": True},
            "image_output": {"available": True, "pricing_available": True},
        }
    }


def _detected_native_search_capabilities(*, available: bool, required: bool = False):
    return {
        "web_search": available,
        "web_search_required": required,
        "native_web_search_transport_supported": available,
        "feature_gates": {
            "web_search": {
                "available": available,
                "mode": "native" if available else "none",
                "reason_code": None if available else "provider_unsupported",
                "pricing_available": available,
            }
        },
    }


def test_perplexity_catalog_context_limit_uses_canonical_agent_identifier(native_catalog):
    native_catalog("perplexity", "perplexity/sonar", context_window=128_000)

    capabilities = provider_capabilities("perplexity/perplexity/sonar", "perplexity")

    assert capabilities["context_limit"] == 128_000


def test_unknown_perplexity_catalog_context_limit_remains_unknown(native_catalog):
    native_catalog()

    capabilities = provider_capabilities("perplexity/perplexity/glm-5.3", "perplexity")

    assert capabilities["context_limit"] is None


def test_admin_override_without_a_window_keeps_the_exact_catalog_limit(native_catalog):
    """CapabilitiesInput always serializes context_limit, so null must not erase detection."""
    native_catalog("perplexity", "perplexity/sonar", context_window=128_000)
    detected = provider_capabilities("perplexity/perplexity/sonar", "perplexity")

    stored_flag_only = normalize_capabilities({"tool_call": False, "context_limit": None}, detected)
    assert stored_flag_only["context_limit"] == 128_000
    assert stored_flag_only["function_calling"] is False

    assert normalize_capabilities({"context_limit": 0}, detected)["context_limit"] == 128_000
    assert normalize_capabilities({"context_limit": "200000"}, detected)["context_limit"] == 128_000
    assert normalize_capabilities({"context_limit": 64_000}, detected)["context_limit"] == 64_000


def test_admin_override_cannot_invent_a_window_for_an_uncatalogued_model(native_catalog):
    native_catalog()
    detected = provider_capabilities("perplexity/perplexity/glm-5.3", "perplexity")

    assert normalize_capabilities({"tool_call": True, "context_limit": None}, detected)["context_limit"] is None


@pytest.mark.parametrize("available", (False, True))
def test_native_catalog_flags_map_to_canonical_capabilities_and_gates(native_catalog, available):
    flags = {
        flag: available
        for flag in (
            "vision", "tool_call", "reasoning", "prompt_caching",
            "structured_output", "web_search", "decisions",
        )
    }
    native_catalog(
        "openai", "catalog-model", capabilities=flags,
        context_window=128_000, max_output_tokens=16_000,
        input_modalities=["text", "image"] if available else ["text"],
    )

    capabilities = provider_capabilities("catalog-model", "openai", "https://api.openai.com/v1")

    for flag in flags:
        assert capabilities[flag] is available
    assert capabilities["function_calling"] is available
    assert capabilities["attachment"] is available
    assert capabilities["context_limit"] == 128_000
    assert capabilities["max_output_tokens"] == 16_000
    assert capabilities["metadata_source"] == "catalog"
    assert capabilities["metadata_status"] == "observed"
    assert capabilities["metadata_version"] == "2099-01-01"
    for feature in ("image_input", "structured_output", "decisions"):
        assert capabilities["feature_gates"][feature] == {
            "available": available,
            "mode": "native" if available else "none",
            "reason_code": None if available else "provider_unsupported",
            "pricing_available": available,
        }
    assert capabilities["feature_gates"]["web_search"] == {
        "available": False, "mode": "none",
        "reason_code": "pricing_unavailable" if available else "provider_unsupported",
        "pricing_available": False,
    }


@pytest.mark.parametrize("provider_type", ("openai", "anthropic"))
def test_compatible_capabilities_never_read_native_catalog(monkeypatch, provider_type):
    from lumen.services import provider_metadata

    def forbidden_catalog():
        raise AssertionError("compatible routes must not read native catalog")

    monkeypatch.setattr(provider_metadata, "native_catalog", forbidden_catalog)

    capabilities = provider_capabilities("gpt-6-luna", provider_type, "https://compatible.example.test/v1")

    for flag in (
        "vision", "tool_call", "function_calling", "reasoning", "prompt_caching",
        "structured_output", "web_search", "decisions",
    ):
        assert capabilities[flag] is False
    assert capabilities["context_limit"] is None
    assert capabilities["metadata_source"] is None
    assert capabilities["metadata_status"] == "missing"
    assert capabilities["metadata_version"] is None


@pytest.mark.parametrize("available", (False, True))
def test_decisions_override_updates_boolean_and_gate(native_catalog, available):
    native_catalog("openai", "decision-model", capabilities={"decisions": not available})
    detected = provider_capabilities("decision-model", "openai")

    normalized = normalize_capabilities({"decisions": available}, detected)

    assert normalized["decisions"] is available
    assert normalized["feature_gates"]["decisions"] == {
        "available": available,
        "mode": "native" if available else "none",
        "reason_code": None if available else "provider_unsupported",
        "pricing_available": available,
    }


@pytest.mark.parametrize("input_price", (None, "0.000001"))
def test_decisions_pricing_requires_only_input_rate(native_catalog, input_price):
    native_catalog("openai", "decision-model", capabilities={"decisions": True})
    detected = provider_capabilities("decision-model", "openai")

    capabilities = _pricing_aware_capabilities(
        _model(metadata={}, input_price=input_price, output_price=None), detected,
    )

    assert capabilities["decisions"] is True
    assert capabilities["feature_gates"]["decisions"]["available"] is True
    assert capabilities["feature_gates"]["decisions"]["pricing_available"] is (input_price is not None)


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


def test_native_search_remains_unpriced_until_search_fees_are_billable():
    caps = _capabilities()
    caps["feature_gates"]["web_search"]["mode"] = "native"

    gated = _pricing_aware_capabilities(_model(metadata={}), caps)

    assert gated["feature_gates"]["web_search"]["pricing_available"] is False
    assert gated["feature_gates"]["web_search"]["available"] is False
    assert gated["feature_gates"]["web_search"]["reason_code"] == "pricing_unavailable"

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
        provider_capabilities("example-model", "openai"),
    )

    assert normalized["function_calling"] is True
    assert normalized["feature_gates"]["image_input"]["available"] is True
    assert normalized["feature_gates"]["image_output"]["available"] is True


def test_models_dev_pdf_modality_enables_the_document_input_gate():
    normalized = normalize_capabilities(
        {
            "modalities": {"input": ["text", "pdf"], "output": ["text"]},
        },
        provider_capabilities("example-model", "openai"),
    )

    assert normalized["input_modalities"] == ["text", "pdf"]
    assert normalized["feature_gates"]["document_input"]["available"] is True


def test_stored_canonical_gates_and_detected_modalities_are_preserved():
    detected = provider_capabilities("example-model", "openai")
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


def test_managed_search_does_not_remove_intrinsic_sonar_scope_floor():
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
    assert normalized["web_search_required"] is True


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
        "available": False,
        "mode": "none",
        "reason_code": "pricing_unavailable",
        "pricing_available": False,
    }


def test_stored_canonical_modality_gates_are_not_clobbered_without_legacy_fields():
    normalized = normalize_capabilities(
        {
            "feature_gates": {
                "image_input": {"available": True, "mode": "managed", "reason_code": None, "pricing_available": True},
                "audio_output": {"available": True, "mode": "managed", "reason_code": None, "pricing_available": True},
            }
        },
        provider_capabilities("example-model", "openai"),
    )

    assert normalized["feature_gates"]["image_input"]["mode"] == "managed"
    assert normalized["feature_gates"]["audio_output"]["mode"] == "managed"


def test_resolved_model_fences_selected_credential_generation_without_decrypting(monkeypatch):
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
        api_provider="openai",
        api_base="https://api.example.test/v1",
        selection_policy="priority",
        is_active=True,
        margin_multiplier="1.25",
    )
    credential = _attach_credentials(provider)
    monkeypatch.setattr(provider_credentials, "decrypt_llm_provider_key", lambda _: (_ for _ in ()).throw(AssertionError("route must be secret-free")))
    monkeypatch.setattr(provider_store, "derive_encryption_subkey", lambda _: b"test-hmac-key")

    first = _resolved_model(model, provider)
    credential.generation += 1
    second = _resolved_model(model, provider)

    assert first["config_version_hash"] != second["config_version_hash"]
    assert first["config_version_hash"] != "first-secret"
    assert "first-secret" not in first["config_version_hash"]
    assert first["provider_auth"]["credential_id"] == credential.id
    assert first["api_key"] is None


def test_resolved_provider_fences_selected_credential_generation_without_decrypting(monkeypatch):
    provider = SimpleNamespace(
        id=3,
        name="search-provider",
        provider_type="perplexity",
        api_base="https://api.perplexity.ai",
        selection_policy="priority",
        is_active=True,
        margin_multiplier="1.25",
    )
    credential = _attach_credentials(provider)
    monkeypatch.setattr(provider_credentials, "decrypt_llm_provider_key", lambda _: (_ for _ in ()).throw(AssertionError("route must be secret-free")))
    monkeypatch.setattr(provider_store, "derive_encryption_subkey", lambda _: b"test-hmac-key")

    first = _resolved_provider(provider)
    credential.generation += 1
    second = _resolved_provider(provider)

    assert first["config_version_hash"] != second["config_version_hash"]
    assert first["config_version_hash"] != "first-secret"
    assert first["provider_name"] == "search-provider"


def test_resolved_model_gates_frozen_catalog_prices(monkeypatch):
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
        api_provider="openai",
        api_base=None,
        selection_policy="priority",
        margin_multiplier="1",
        is_active=True,
    )
    _attach_credentials(provider)
    monkeypatch.setattr(provider_pricing, "effective_prices_per_million", lambda *_, **__: (Decimal("1"), Decimal("2")))

    resolved = _resolved_model(model, provider)

    assert resolved["price_source"] == "native_catalog"
    assert resolved["input_price_per_token"] == Decimal("0.0000010000")
    assert resolved["output_price_per_token"] == Decimal("0.0000020000")
    assert resolved["capabilities"]["feature_gates"]["text"]["pricing_available"] is True


def test_perplexity_agent_prices_resolve_from_canonical_native_catalog(native_catalog):
    native_catalog(
        "perplexity", "perplexity/sonar",
        token_pricing={"input": "0.25", "output": "2.50"},
    )
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
        "native_catalog",
    )
    native_catalog(
        "perplexity", "perplexity/glm-5.3",
        token_pricing={"input": "1.40", "output": "4.40"},
    )
    assert provider_pricing._resolved_base_prices(glm, provider)[:3] == (
        Decimal("0.0000014000"),
        Decimal("0.0000044000"),
        "native_catalog",
    )


def test_public_model_marks_unpriced_text_route_unavailable():
    model = SimpleNamespace(
        id=7,
        provider_id=3,
        model_name="unpriced-model",
        display_name=None,
        sort_order=0,
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

    public = _model_public(model, provider_type="openai", api_provider="openai")

    assert public["effective_capabilities"]["feature_gates"]["text"]["pricing_available"] is False


def _priced_compatible_model(capabilities=None, capability_source=None):
    return SimpleNamespace(
        id=8, provider_id=3, model_name="custom-compatible-model", display_name=None, sort_order=0,
        is_active=True, is_title_model=False, is_memory_model=False,
        input_price=Decimal("0.000001"), output_price=Decimal("0.000002"), price_source="manual",
        price_metadata=None, capabilities=capabilities, capability_source=capability_source,
        models_dev_model_id=None, created_at=None, updated_at=None,
    )


def test_domain_billing_reports_unknown_context_limit_until_admin_override(native_catalog):
    native_catalog()
    public = _model_public(_priced_compatible_model(), provider_type="openai", api_provider="openai",
                           api_base="https://compatible.example/v1")
    assert public["effective_capabilities"]["context_limit"] is None
    assert public["domain_billing"] == {"ready": False, "reason_code": "context_limit_unknown"}

    overridden = _model_public(
        _priced_compatible_model({"context_limit": 32_000}, "override"),
        provider_type="openai", api_provider="openai", api_base="https://compatible.example/v1",
    )
    assert overridden["effective_capabilities"]["context_limit"] == 32_000
    assert overridden["domain_billing"] == {"ready": True, "reason_code": None}


@pytest.mark.parametrize(("capabilities", "prices", "reason"), [
    ({"context_limit": 8000}, (None, Decimal("1")), "pricing_unavailable"),
    ({"context_limit": 8000, "output_budget_enforced": False}, (Decimal("1"), Decimal("1")), "output_budget_unenforced"),
    ({"context_limit": None}, (Decimal("1"), Decimal("1")), "context_limit_unknown"),
    ({"context_limit": 8000}, (Decimal("1"), Decimal("1")), None),
])
def test_domain_paid_admission_rejects_unready_model_before_run(capabilities, prices, reason):
    from fastapi import HTTPException

    from lumen.models.chat_contracts import ChatFeatureOptions
    from lumen.services.chat_admission import _require_execution_capability

    gates = {"text": {"available": True, "mode": "native", "reason_code": None, "pricing_available": True}}
    resolved = {"capabilities": {**capabilities, "feature_gates": gates},
                "input_price_per_token": prices[0], "output_price_per_token": prices[1]}
    if prices[0] is not None:
        # A user-funded run keeps today's admission; only domain-paid calls need the prepaid bound.
        _require_execution_capability(ChatFeatureOptions(), resolved)
    if reason is None:
        _require_execution_capability(ChatFeatureOptions(), resolved, domain_paid=True)
        return
    with pytest.raises(HTTPException) as error:
        _require_execution_capability(ChatFeatureOptions(), resolved, domain_paid=True)
    assert error.value.status_code == 422
    # Unpriced text is already refused by the text gate for every payer.
    expected = "text (pricing_unavailable)" if reason == "pricing_unavailable" else f"domain_billing ({reason})"
    assert error.value.detail.endswith(expected)


def test_chatgpt_subscription_uses_catalog_capabilities_without_api_prices(monkeypatch):
    original = capability_service.catalog_model_metadata
    observed = []

    def metadata(model_name, provider_type, api_base=None):
        observed.append((model_name, provider_type))
        return original(model_name, provider_type, api_base)

    monkeypatch.setattr(capability_service, "catalog_model_metadata", metadata)
    capabilities = provider_capabilities("chatgpt/gpt-5.5", "chatgpt")
    assert observed == [("gpt-5.5", "openai")]
    assert capabilities["vision"] is True
    assert capabilities["function_calling"] is True
    assert capabilities["reasoning"] is True
    assert capabilities["context_limit"] == 1050000
    assert capabilities["max_output_tokens"] == 128000

    assert capabilities["responses_api"] is True
    assert capabilities["endpoints"] == ["responses"]
    assert capabilities["structured_output"] is False
    assert capabilities["output_budget_enforced"] is False


def test_chatgpt_subscription_override_cannot_enable_structured_output():
    model = SimpleNamespace(
        model_name="chatgpt/gpt-5.2-codex",
        capabilities={"structured_output": True, "tools": True, "output_budget_enforced": True},
        capability_source="override",
    )

    capabilities, source = _effective_capabilities(model, "chatgpt", "chatgpt_device")

    assert source == "override"
    assert capabilities["tools"] is True
    assert capabilities["structured_output"] is False
    assert capabilities["output_budget_enforced"] is False


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


def test_image_variant_pricing_requires_exact_size_and_quality_without_base_fallback():
    pricing = {
        "image_per_unit": "0.04",
        "image_variants": {"1024x1024:high": "0.08"},
    }
    assert provider_pricing.exact_media_price(pricing, "image_per_unit", variant="1024x1024:high") == Decimal("0.08")
    assert provider_pricing.exact_media_price(pricing, "image_per_unit", variant="1024x1024:low") is None
    assert provider_pricing.exact_media_price(pricing, "image_per_unit") is None
    assert provider_pricing.exact_media_price({"image_per_unit": "0"}, "image_per_unit", variant="1024x1024:low") == Decimal("0")
