import json
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from lumen.api import completions
from lumen.auth import get_principal
from lumen.main import app
from lumen.models.chat_contracts import (
    ChatFeatureOptions,
    ChatRunDescriptor,
    UserAssetInputPart,
    validate_user_input_parts,
)
from lumen.services import capabilities, chat_admission, context_store, credit
from lumen.services import conversation_store as cs
from lumen.services.durable_runs import admission, common, queries
from lumen.services.durable_runs import errors as durable_errors
from lumen.services.providers import repository
from lumen.services.providers import routing as ps

_BASE = "/api/v1/chat/conversations"
_HEADERS = {"Idempotency-Key": "d27ac16a-0e5b-465f-89cc-eefe6e9d0001"}


def _request(text: str = "hello", **extra):
    return {"parts": [{"type": "text", "text": text}], "model_id": "gpt-3.5-turbo", "features": {}, **extra}


async def test_selected_skills_are_loaded_once_from_owned_active_extensions(monkeypatch):
    """Selected DB skills resolve exactly once through the selected skills provider and freeze a digest."""
    calls: list[tuple[str, str, bool]] = []

    async def fake_list(kind, **kwargs):
        assert kind == "skill"
        calls.append((kwargs["user_id"], kwargs["project_id"], kwargs["active_only"]))
        return [
            {"id": 1, "name": "safe", "instructions": "first", "scope": "user", "is_active": True},
            {"id": 2, "name": "unused", "instructions": "second", "scope": "user", "is_active": True},
        ]

    monkeypatch.setattr(chat_admission.es, "list_for_user", fake_list)
    instructions, provenance = await chat_admission._load_skill_snapshot(None, [1], [], "u1", "p1")

    assert instructions == ["first"]
    assert provenance[0]["id"] == 1
    assert provenance[0]["name"] == "safe"
    assert len(provenance[0]["content_hash"]) == 64
    assert provenance[0]["snapshot"]["content_digest"] == provenance[0]["content_hash"]
    assert calls == [("u1", "p1", True)]


async def test_extension_selection_expands_omitted_ids_for_default_chat(monkeypatch):
    async def fake_list(kind, **_kwargs):
        return [
            {
                "id": 7,
                "name": f"{kind}-seven",
                "url": "https://tool.example/v1",
                "method": "GET",
                "transport": "http",
                "params_schema": {"type": "object"},
                "config_version": 1,
                "effect": "read",
            },
            {
                "id": 8,
                "name": f"{kind}-eight",
                "url": "https://tool.example/v2",
                "method": "POST",
                "transport": "http",
                "params_schema": {"type": "object"},
                "config_version": 1,
                "effect": "external_mutation",
            },
        ]

    async def fake_credential_versions(server_ids, *, user_id, project_id):
        assert server_ids == [7, 8]
        assert (user_id, project_id) == ("u1", "p1")
        return {7: 3, 8: 4}

    monkeypatch.setattr(chat_admission.es, "list_for_user", fake_list)
    monkeypatch.setattr(chat_admission.es, "mcp_credential_versions", fake_credential_versions)

    no_agent = await chat_admission._resolve_extension_selection(
        None, ChatFeatureOptions(), user_id="u1", project_id="p1"
    )
    assert [item["id"] for item in no_agent["tools"]] == [7, 8]
    assert [item["id"] for item in no_agent["mcp"]] == [7, 8]
    assert [item["credential_version"] for item in no_agent["mcp"]] == [3, 4]

    agent = {"tool_ids": [7], "mcp_ids": []}
    agent_default = await chat_admission._resolve_extension_selection(
        agent, ChatFeatureOptions(), user_id="u1", project_id="p1"
    )
    assert [item["id"] for item in agent_default["tools"]] == [7]
    assert agent_default["mcp"] == []

    explicit = await chat_admission._resolve_extension_selection(
        None,
        ChatFeatureOptions(tool_policy={"enabled_tool_ids": [], "enabled_mcp_ids": []}),
        user_id="u1",
        project_id="p1",
    )
    assert explicit == {"tools": [], "mcp": []}


async def test_mcp_selection_snapshots_credential_version(monkeypatch):
    async def fake_list(kind, **_kwargs):
        assert kind == "mcp"
        return [
            {
                "id": 7,
                "name": "mcp-seven",
                "url": "https://mcp.example",
                "transport": "http",
                "config_version": 1,
            }
        ]

    async def credential_versions(server_ids, *, user_id, project_id):
        assert server_ids == [7]
        assert (user_id, project_id) == ("u1", "p1")
        return {7: 4}

    monkeypatch.setattr(chat_admission.es, "list_for_user", fake_list)
    monkeypatch.setattr(chat_admission.es, "mcp_credential_versions", credential_versions)

    selection = await chat_admission._resolve_extension_selection(
        {"tool_ids": [], "mcp_ids": [7]},
        ChatFeatureOptions(),
        user_id="u1",
        project_id="p1",
    )

    assert selection["mcp"][0]["credential_version"] == 4


async def test_extension_selection_rejects_agent_allowlist_bypass(monkeypatch):
    monkeypatch.setattr(chat_admission.es, "list_for_user", lambda *_args, **_kwargs: _return([]))

    with pytest.raises(HTTPException, match="allowlist"):
        await chat_admission._resolve_extension_selection(
            {"tool_ids": [7], "mcp_ids": []},
            ChatFeatureOptions(tool_policy={"enabled_tool_ids": [8]}),
            user_id="u1",
            project_id="p1",
        )


async def test_preview_tool_schemas_match_the_first_provider_request(monkeypatch):
    """Preview must emit the exact schemas bindings would build, not raw admin names."""
    from lumen.services.tool_runtime import contracts

    preloaded = {
        "id": 1,
        "name": "preloaded tool",
        "description": "d",
        "params_schema": {"type": "object", "properties": {"q": {"type": "string"}}},
        "load_policy": "preloaded",
        "destination_origin": "https://tool.example",
    }
    selection = {
        "tools": [
            preloaded,
            {
                "id": 2,
                "name": "catalog_only_tool",
                "description": "d",
                "params_schema": {},
                "load_policy": "on_demand",
                "destination_origin": "https://tool.example",
            },
            {
                "id": 3,
                "name": "blocked_tool",
                "description": "d",
                "params_schema": {},
                "load_policy": "preloaded",
                "destination_origin": None,
            },
        ],
        "mcp": [{"id": 5, "name": "GitHub", "load_policy": "preloaded", "destination_origin": "https://mcp.example"}],
    }
    features = ChatFeatureOptions(
        web_search={"enabled": True, "mode": "managed", "provider_id": 7},
        web_fetch={"enabled": True},
    )

    async def resolve(self, kind, identifier, namespace):
        row = {1: preloaded, 2: selection["tools"][1], 3: selection["tools"][2]}[identifier]
        return {
            **row,
            "url": row["destination_origin"] and f"{row['destination_origin']}/run",
            "method": "GET",
            "timeout_seconds": 10,
            "effect": "read",
            "config_version": 1,
            "is_active": True,
        }

    from lumen.plugins import tools_host

    monkeypatch.setattr(tools_host.ToolsExtensionAccess, "resolve", resolve)

    schemas = await chat_admission._preview_tool_schemas(features, selection, user_id="user-A", project_id="proj-A")
    names = {schema["function"]["name"] for schema in schemas}
    from lumen.services.tools import ToolContext

    projected = await contracts.custom_tool_function_schema(preloaded, ToolContext(project_id="proj-A", user_id="user-A"))

    # Identical to the runtime binding projection: mangled provider name and closed schema.
    assert projected["name"] == "custom__1__preloaded_tool_" + projected["name"].rsplit("_", 1)[1]
    assert projected["parameters"]["additionalProperties"] is False
    assert next(schema for schema in schemas if schema["function"]["name"] == projected["name"])["function"] == (
        projected
    )
    assert "list_available_tools" in names
    assert projected["name"] in names
    # On-demand extensions, destination-blocked tools, managed tools, and undiscovered
    # MCP schemas are never part of the first provider request.
    assert not any(name.startswith("custom__2__") or name.startswith("custom__3__") for name in names)
    assert not {"managed_web_search", "managed_web_fetch"} & names
    assert not any(name.startswith("mcp__") for name in names)
    assert (
        await chat_admission._preview_tool_schemas(
            ChatFeatureOptions(tool_policy={"mode": "none"}), selection, user_id="user-A", project_id="proj-A"
        )
        == []
    )


async def _ok_precheck(user_id, project_id=None, api_key_id=None):
    return None


def test_input_asset_failures_map_to_client_validation_error():
    assert completions._run_error(durable_errors.DurableRunInputError("input asset is not ready")).status_code == 422


def _conv():
    return {"id": "c1", "project_id": "test-project-123", "user_id": "test-user-123", "model_name": None}


def _resolved():
    return {
        "model_name": "gpt-3.5-turbo",
        "model_id": 1,
        "provider_name": "openai",
        "provider_id": 1,
        "config_version_hash": "a" * 64,
        "margin_multiplier": "1",
        "input_price_per_token": "0.000001",
        "output_price_per_token": "0.000002",
        "capabilities": {"context_limit": 16_000, "max_output_tokens": 4096},
    }


async def _patch_text_execution(monkeypatch):
    monkeypatch.setattr(credit, "precheck", _ok_precheck)
    monkeypatch.setattr(cs, "get_conversation", lambda *args, **kwargs: _return(_conv()))
    monkeypatch.setattr(
        cs, "get_active_path", lambda *args, **kwargs: _return({"messages": [], "active_leaf_id": None})
    )
    monkeypatch.setattr(
        context_store,
        "load_context_source",
        lambda **_kwargs: _return(
            {
                "messages": [],
                "message_ids": [],
                "source_hashes": [],
                "active_leaf_id": None,
                "revision": "rev-empty",
                "checkpoint_id": None,
                "checkpoint": None,
            }
        ),
    )
    monkeypatch.setattr(cs, "add_message", lambda *args, **kwargs: _return({"id": 1}))
    monkeypatch.setattr(ps, "resolve_model", lambda *args, **kwargs: _return(_resolved()))
    monkeypatch.setattr(ps, "resolve_title_model", lambda *args, **kwargs: _return(None))
    monkeypatch.setattr(admission, "existing_run_for_intent", lambda *args, **kwargs: _return(None))
    monkeypatch.setattr(completions, "_active_context_compaction_run_id", lambda **_kwargs: _return(None))
    monkeypatch.setattr(chat_admission.memory_host, "recall_for_run", lambda *args, **kwargs: _return([]))
    monkeypatch.setattr(
        chat_admission,
        "_resolve_feature_routes",
        lambda *_args, **_kwargs: _return({}),
    )
    monkeypatch.setattr(
        chat_admission,
        "_load_skill_snapshot",
        lambda *_args, **_kwargs: _return(([], [])),
    )
    monkeypatch.setattr(
        chat_admission,
        "_resolve_extension_selection",
        lambda *_args, **_kwargs: _return({"tools": [], "mcp": []}),
    )
    monkeypatch.setattr(
        chat_admission,
        "_preview_tool_schemas",
        lambda *_args, **_kwargs: _return([]),
    )
    monkeypatch.setattr(chat_admission.ws, "get_instructions_for_run", lambda *args, **kwargs: _return(None))


async def _return(value):
    return value


async def test_context_planner_does_not_duplicate_saved_regenerate_input(monkeypatch):
    monkeypatch.setattr(
        context_store,
        "load_context_source",
        lambda **_kwargs: _return(
            {
                "messages": [{"role": "user", "content": "saved turn"}],
                "message_ids": ["1"],
                "source_hashes": ["h1"],
                "active_leaf_id": "1",
                "revision": "rev-1",
                "checkpoint_id": None,
                "checkpoint": None,
            }
        ),
    )
    monkeypatch.setattr(chat_admission, "_load_owned_conv", lambda *_args, **_kwargs: _return(_conv()))
    monkeypatch.setattr(chat_admission, "_resolve_model", lambda *_args, **_kwargs: _return(_resolved()))
    monkeypatch.setattr(
        chat_admission,
        "_resolve_extension_selection",
        lambda *_args, **_kwargs: _return({"tools": [], "mcp": []}),
    )
    monkeypatch.setattr(chat_admission, "_resolve_feature_routes", lambda *_args, **_kwargs: _return({}))
    monkeypatch.setattr(chat_admission, "resolve_summary_route", lambda route: _return(route))
    monkeypatch.setattr(chat_admission.memory_host, "recall_for_run", lambda *args, **kwargs: _return([]))

    parts = [{"type": "text", "text": "saved turn"}]
    features = ChatFeatureOptions(tool_policy={"mode": "none"})
    planned = await chat_admission.prepare_context_input(
        user_id="u1",
        project_id="p1",
        conversation_id="c1",
        model_id="gpt-3.5-turbo",
        parts=validate_user_input_parts(parts),
        features=features,
        append_draft=False,
    )
    assert planned["input_messages"] == [{"role": "user", "content": "saved turn"}]


def test_persisted_canonical_attachment_restores_input_reference():
    assert completions._user_input_parts_from_message(
        {
            "parts": [
                {
                    "type": "image",
                    "asset_id": "asset-1",
                    "mime_type": "image/png",
                    "name": "diagram.png",
                    "width": 640,
                    "height": 480,
                }
            ],
        }
    )[0].model_dump(mode="json") == {"type": "image", "asset_id": "asset-1"}


class TestRuntimeCapabilities:
    def test_runtime_capabilities_fail_closed_without_sandbox_policy(self):
        runtime = capabilities.runtime_capabilities(
            SimpleNamespace(chat_sandbox_workspace_url="https://workspace.example")
        )

        assert runtime["workspace_ready"] is False
        assert runtime["feature_gates"]["code_interpreter"]["available"] is False
        assert runtime["feature_gates"]["code_workspace"]["reason_code"] == "execution_protocol_v2_unavailable"

    def test_runtime_tool_features_require_model_function_calling(self):
        runtime = {
            "checkpointer_ready": True,
            "workspace_ready": True,
            "feature_gates": {"mcp": {"available": True, "mode": "native", "reason_code": None}},
        }

        effective = capabilities.effective_runtime_capabilities({"function_calling": False}, runtime)

        assert effective["feature_gates"]["mcp"] == {
            "available": False,
            "mode": "none",
            "reason_code": "model_function_calling_unsupported",
            "pricing_available": False,
        }

    async def test_capability_endpoint_intersects_model_and_runtime(self, client, monkeypatch):
        async def resolve(model_id):
            assert model_id == 7
            return {"model_name": "configured-model", "capabilities": {"function_calling": True}}

        monkeypatch.setattr(ps, "resolve_model_by_id", resolve)
        monkeypatch.setattr(
            capabilities,
            "runtime_capabilities",
            lambda: {
                "checkpointer_ready": True,
                "workspace_ready": False,
                "feature_gates": {
                    "mcp": {"available": True, "mode": "native", "reason_code": None},
                    "code_workspace": {
                        "available": False,
                        "mode": "none",
                        "reason_code": "workspace_or_checkpointer_unavailable",
                    },
                },
            },
        )

        response = await client.get("/api/v1/chat/capabilities?model_id=7")

        assert response.status_code == 200
        assert response.json()["model_name"] == "configured-model"
        assert response.json()["runtime"]["feature_gates"]["mcp"]["available"] is True
        assert response.json()["runtime"]["feature_gates"]["code_workspace"]["reason_code"] == (
            "workspace_or_checkpointer_unavailable"
        )


class TestReasoningEffortValidation:
    def test_auto_and_none_are_supported_for_reasoning_models(self):
        resolved = {"capabilities": {"reasoning": True, "reasoning_options": [{"type": "toggle"}]}}
        assert chat_admission._validated_reasoning_effort("auto", resolved) == "auto"
        assert chat_admission._validated_reasoning_effort("none", resolved) == "none"

    def test_only_models_dev_effort_values_are_allowed(self):
        resolved = {
            "capabilities": {
                "reasoning": True,
                "reasoning_options": [{"type": "effort", "values": ["low", "xhigh", "max", "ultra"]}],
            }
        }
        assert chat_admission._validated_reasoning_effort("ultra", resolved) == "ultra"
        with pytest.raises(HTTPException, match="지원하지 않습니다"):
            chat_admission._validated_reasoning_effort("high", resolved)

    def test_unlisted_effort_is_rejected_for_toggle_only_models(self):
        resolved = {"capabilities": {"reasoning": True, "reasoning_options": [{"type": "toggle"}]}}
        with pytest.raises(HTTPException, match="지원하지 않습니다"):
            chat_admission._validated_reasoning_effort("low", resolved)

    @pytest.mark.parametrize(
        ("provider_type", "options"),
        [
            # models.dev shapes: gpt-5, o3, gemini-2.5-pro, then unknown or stale metadata.
            ("openai", [{"type": "effort", "values": ["minimal", "low", "medium", "high"]}]),
            ("openai", [{"type": "effort", "values": ["low", "medium", "high"]}]),
            ("gemini", [{"type": "budget_tokens", "min": 128, "max": 32768}]),
            ("gemini", [{"type": "budget_tokens"}]),
            ("gemini", [{"type": "budget_tokens", "min": False}]),
            ("openai", []),
            ("openai", None),
        ],
    )
    def test_none_is_rejected_unless_the_model_advertises_disabling(self, provider_type, options):
        resolved = {"provider_type": provider_type, "capabilities": {"reasoning": True, "reasoning_options": options}}
        with pytest.raises(HTTPException) as exc_info:
            chat_admission._validated_reasoning_effort("none", resolved)
        assert exc_info.value.status_code == 422
        assert "'none'" in exc_info.value.detail
        assert chat_admission._validated_reasoning_effort("auto", resolved) == "auto"

    @pytest.mark.parametrize(
        ("provider_type", "options"),
        [
            # models.dev shapes: gpt-5.1, a toggle model, gemini-2.5-flash, then Anthropic budget models.
            ("openai", [{"type": "effort", "values": ["none", "low", "medium", "high"]}]),
            ("deepinfra", [{"type": "toggle"}]),
            ("gemini", [{"type": "toggle"}, {"type": "budget_tokens", "min": 0, "max": 24576}]),
            ("gemini", [{"type": "budget_tokens", "min": 0, "max": 24576}]),
            ("anthropic", [{"type": "budget_tokens", "min": 1024}]),
            ("anthropic", []),
        ],
    )
    def test_none_is_accepted_when_disabling_thinking_is_valid(self, provider_type, options):
        resolved = {"provider_type": provider_type, "capabilities": {"reasoning": True, "reasoning_options": options}}
        assert chat_admission._validated_reasoning_effort(" None ", resolved) == "none"

    def test_none_is_rejected_for_non_reasoning_models(self):
        resolved = {"provider_type": "anthropic", "capabilities": {"reasoning": False}}
        with pytest.raises(HTTPException, match="추론 강도를 지원하지 않습니다"):
            chat_admission._validated_reasoning_effort("none", resolved)

    def test_gpt5_tools_reject_explicit_reasoning_but_allow_auto_or_none(self):
        resolved = {
            "provider_type": "openai",
            "model_name": "gpt-5.6-luna",
            "capabilities": {
                "reasoning": True,
                "reasoning_options": [{"type": "effort", "values": ["none", "low", "medium", "high"]}],
            },
        }
        tools_enabled = ChatFeatureOptions()

        chat_admission._validate_tool_reasoning_compatibility("auto", resolved, tools_enabled)
        assert chat_admission._validated_reasoning_effort("none", resolved) == "none"
        chat_admission._validate_tool_reasoning_compatibility("none", resolved, tools_enabled)
        with pytest.raises(HTTPException, match="자동 또는 없음으로") as exc_info:
            chat_admission._validate_tool_reasoning_compatibility("high", resolved, tools_enabled)
        assert exc_info.value.status_code == 422

    def test_gpt5_tools_hint_omits_none_when_the_model_cannot_disable_reasoning(self):
        resolved = {
            "provider_type": "openai",
            "model_name": "gpt-5",
            "capabilities": {
                "reasoning": True,
                "reasoning_options": [{"type": "effort", "values": ["minimal", "low", "medium", "high"]}],
            },
        }
        tools_enabled = ChatFeatureOptions()

        with pytest.raises(HTTPException, match="'none'"):
            chat_admission._validated_reasoning_effort("none", resolved)
        with pytest.raises(HTTPException) as exc_info:
            chat_admission._validate_tool_reasoning_compatibility("high", resolved, tools_enabled)
        assert exc_info.value.detail.endswith("추론 강도를 자동으로 선택하세요.")
        assert "없음" not in exc_info.value.detail

    def test_gpt5_explicit_reasoning_is_allowed_when_tools_are_disabled(self):
        resolved = {"provider_type": "openai", "model_name": "gpt-5.6-luna"}
        tools_disabled = ChatFeatureOptions(tool_policy={"mode": "none"})

        chat_admission._validate_tool_reasoning_compatibility("high", resolved, tools_disabled)


class TestExecutionCapabilityGate:
    def test_rejects_feature_without_priced_execution_route(self):
        features = ChatFeatureOptions(web_search={"enabled": True, "provider_id": 7})
        resolved = {
            "input_price_per_token": "0.000001",
            "output_price_per_token": "0.000002",
            "capabilities": {
                "feature_gates": {
                    "text": {"available": True, "pricing_available": True},
                    "web_search": {
                        "available": True,
                        "pricing_available": False,
                        "reason_code": "pricing_unavailable",
                    },
                }
            },
        }

        with pytest.raises(HTTPException, match="web_search"):
            chat_admission._require_execution_capability(features, resolved)

    def test_accepts_priced_managed_web_search_and_fetch(self):
        features = ChatFeatureOptions(
            web_search={"enabled": True, "provider_id": 7},
            web_fetch={"enabled": True},
        )
        resolved = {
            "input_price_per_token": "0.000001",
            "output_price_per_token": "0.000002",
            "capabilities": {
                "feature_gates": {
                    "text": {"available": True, "pricing_available": True},
                    "web_search": {"available": True, "mode": "managed", "pricing_available": True},
                    "web_fetch": {"available": True, "mode": "managed", "pricing_available": True},
                }
            },
        }

        chat_admission._require_execution_capability(features, resolved)

    def test_accepts_priced_native_search_without_server_tool_execution(self):
        features = ChatFeatureOptions(web_search={"enabled": True, "mode": "native"}, tool_policy={"mode": "none"})
        resolved = {
            "input_price_per_token": "0.000001",
            "output_price_per_token": "0.000002",
            "capabilities": {
                "feature_gates": {
                    "text": {"available": True, "mode": "native", "pricing_available": True},
                    "web_search": {"available": True, "mode": "native", "pricing_available": True},
                }
            },
        }

        chat_admission._require_execution_capability(features, resolved)

    def test_rejects_native_search_when_executor_only_has_managed_search(self):
        features = ChatFeatureOptions(web_search={"enabled": True, "mode": "native"})
        resolved = {
            "input_price_per_token": "0.000001",
            "output_price_per_token": "0.000002",
            "capabilities": {
                "feature_gates": {
                    "text": {"available": True, "pricing_available": True},
                    "web_search": {"available": True, "mode": "managed", "pricing_available": True},
                }
            },
        }

        with pytest.raises(HTTPException, match="provider_unsupported"):
            chat_admission._require_execution_capability(features, resolved)

    def test_accepts_priced_user_selected_advisor_with_function_calling_executor(self):
        features = ChatFeatureOptions(advisor={"enabled": True, "model_id": 9})
        resolved = {
            "input_price_per_token": "0.000001",
            "output_price_per_token": "0.000002",
            "capabilities": {
                "function_calling": True,
                "feature_gates": {"text": {"available": True, "pricing_available": True}},
            },
        }
        routes = {
            "advisor": {
                "provider_id": 7,
                "model_id": 9,
                "input_price_per_token": "0.0003",
                "output_price_per_token": "0.0004",
            }
        }

        chat_admission._require_execution_capability(features, resolved, feature_routes=routes)

    def test_rejects_managed_feature_when_tools_are_disabled(self):
        features = ChatFeatureOptions(
            web_fetch={"enabled": True},
            tool_policy={"mode": "none"},
        )
        resolved = {
            "input_price_per_token": "0.000001",
            "output_price_per_token": "0.000002",
            "capabilities": {
                "feature_gates": {
                    "text": {"available": True, "pricing_available": True},
                    "web_fetch": {"available": True, "pricing_available": True},
                }
            },
        }

        with pytest.raises(HTTPException, match="requires tool execution"):
            chat_admission._require_execution_capability(features, resolved)

    def test_accepts_priced_text_when_legacy_override_has_no_canonical_gates(self):
        chat_admission._require_execution_capability(
            ChatFeatureOptions(),
            {"input_price_per_token": "0.000001", "output_price_per_token": "0.000002", "capabilities": {}},
        )

    def test_rejects_unpriced_text_execution(self):
        with pytest.raises(HTTPException, match="text"):
            chat_admission._require_execution_capability(ChatFeatureOptions(), {"capabilities": {}})

    def test_accepts_manual_memory_until_semantic_retrieval_is_enabled(self):
        chat_admission._require_execution_capability(
            ChatFeatureOptions(memory=True),
            {"input_price_per_token": "0.000001", "output_price_per_token": "0.000002", "capabilities": {}},
        )

    def test_rejects_unavailable_input_modality(self):
        image = UserAssetInputPart(type="image", asset_id="asset-1")
        with pytest.raises(HTTPException, match="image_input"):
            chat_admission._require_execution_capability(
                ChatFeatureOptions(),
                {"input_price_per_token": "0.000001", "output_price_per_token": "0.000002", "capabilities": {}},
                parts=[image],
            )

    async def test_disabled_memory_never_loads_user_memory(self, monkeypatch):
        async def fail_memory_lookup(**_kwargs):
            raise AssertionError("memory store must not be queried")

        monkeypatch.setattr(
            chat_admission.ws, "get_instructions_for_run", lambda *_args, **_kwargs: _return("workspace rule")
        )
        monkeypatch.setattr(chat_admission.memory_host, "recall_for_run", fail_memory_lookup)

        workspace, memories = await chat_admission._load_context({"workspace_id": 7}, "u1", "p1", include_memory=False)

        assert workspace == "workspace rule"
        assert memories == []

    def test_run_snapshot_excludes_provider_secret(self):
        capabilities, pricing = chat_admission._run_snapshots(
            {
                "model_name": "model-a",
                "capabilities": {"feature_gates": {}},
                "input_price_per_token": "0.000001",
                "output_price_per_token": "0.000002",
                "model_id": 1,
                "provider_id": 1,
                "config_version_hash": "a" * 64,
                "margin_multiplier": "1",
                "price_source": "models.dev",
                "provider_name": "provider-a",
                "api_key": "must-not-persist",
            },
            {"memory": True},
            feature_routes={
                "search": {
                    "provider_id": 2,
                    "provider_name": "search-provider",
                    "config_version_hash": "b" * 64,
                    "api_key": "must-not-persist",
                    "api_base": "https://search.example",
                },
                "advisor": {
                    "provider_id": 3,
                    "provider_name": "advisor-provider",
                    "model_id": 4,
                    "model_name": "advisor-model",
                    "config_version_hash": "c" * 64,
                    "input_price_per_token": "0.0003",
                    "output_price_per_token": "0.0004",
                    "api_key": "must-not-persist",
                },
            },
            summary_route={
                "provider_id": 5,
                "provider_name": "summary-provider",
                "model_id": 6,
                "model_name": "summary-model",
                "config_version_hash": "d" * 64,
                "capabilities": {"context_limit": 4_096},
                "api_key": "must-not-persist",
            },
        )

        assert "api_key" not in capabilities
        assert "api_key" not in pricing
        assert capabilities["feature_routes"]["search"] == {
            "provider_id": 2,
            "provider_name": "search-provider",
            "config_version_hash": "b" * 64,
        }
        assert pricing["input_price_per_token"] == "0.000001"
        assert "must-not-persist" not in json.dumps(capabilities)
        assert pricing["component_prices"]["advisor_input_price_per_token"] == "0.0003"
        assert pricing["component_prices"]["advisor_output_price_per_token"] == "0.0004"
        assert capabilities["summary_route"]["context_limit"] == 4_096
        assert capabilities["summary_route"]["model_name"] == "summary-model"

    async def test_resolves_only_user_selected_feature_routes(self, monkeypatch):
        search_route = {"provider_id": 7, "provider_name": "search", "config_version_hash": "s" * 64}
        advisor_route = {
            "provider_id": 8,
            "provider_name": "advisor",
            "model_id": 9,
            "model_name": "advisor-model",
            "config_version_hash": "a" * 64,
        }
        monkeypatch.setattr(chat_admission.ps, "get_active_provider_route", lambda provider_id: _return(search_route))
        monkeypatch.setattr(chat_admission.ps, "resolve_model_by_id", lambda model_id: _return(advisor_route))

        routes = await chat_admission._resolve_feature_routes(
            ChatFeatureOptions(
                web_search={"enabled": True, "provider_id": 7},
                advisor={"enabled": True, "model_id": 9},
            )
        )

        assert routes == {"search": search_route, "advisor": advisor_route}

    async def test_rejects_unavailable_selected_feature_route(self, monkeypatch):
        monkeypatch.setattr(chat_admission.ps, "get_active_provider_route", lambda provider_id: _return(None))
        with pytest.raises(HTTPException, match="selected web search provider"):
            await chat_admission._resolve_feature_routes(
                ChatFeatureOptions(web_search={"enabled": True, "provider_id": 7})
            )

    async def test_enabled_manual_memory_is_loaded(self, monkeypatch):
        captured = {}

        async def load_memory(**kwargs):
            captured.update(kwargs)
            return ["사용자는 Python을 선호합니다."]

        monkeypatch.setattr(chat_admission.ws, "get_instructions_for_run", lambda *_args, **_kwargs: _return(None))
        monkeypatch.setattr(chat_admission.memory_host, "recall_for_run", load_memory)

        workspace, memories = await chat_admission._load_context(
            {"workspace_id": None}, "u1", "p1", include_memory=True
        )

        assert workspace is None
        assert memories == ["사용자는 Python을 선호합니다."]
        # Recency is the default strategy and keeps today's unbounded 30-row hydration.
        assert captured == {
            "user_id": "u1",
            "project_id": "p1",
            "workspace_id": None,
            "include_account": True,
            "strategy": "recency",
            "query": "",
            "limit": 30,
            "token_budget": 0,
        }


class TestActiveRunRecovery:
    async def test_active_run_descriptor_is_available_after_client_disconnect(self, client, monkeypatch):
        monkeypatch.setattr(cs, "get_conversation", lambda *args, **kwargs: _return(_conv()))
        descriptor = ChatRunDescriptor(
            run_id="run-active",
            status="running",
            events_url="/api/v1/chat/runs/run-active/events",
            cancel_url="/api/v1/chat/runs/run-active/cancel",
        )
        monkeypatch.setattr(
            queries,
            "active_run_descriptors",
            lambda **_kwargs: _return([descriptor]),
        )

        response = await client.get(f"{_BASE}/c1/runs?active=true")

        assert response.status_code == 200
        assert response.json() == [descriptor.model_dump(mode="json")]

    async def test_owner_active_run_snapshot_includes_conversation_id(self, client, monkeypatch):
        descriptor = ChatRunDescriptor(
            run_id="run-active",
            conversation_id="c1",
            status="running",
            events_url="/api/v1/chat/runs/run-active/events",
            cancel_url="/api/v1/chat/runs/run-active/cancel",
        )
        monkeypatch.setattr(
            queries,
            "active_run_descriptors_for_owner",
            lambda **_kwargs: _return([descriptor]),
        )

        response = await client.get("/api/v1/chat/runs?active=true")

        assert response.status_code == 200
        assert response.json() == [descriptor.model_dump(mode="json")]


class TestCanonicalCompletionRequests:
    async def test_legacy_message_payload_is_rejected(self, client):
        response = await client.post(
            f"{_BASE}/c1/completions",
            headers=_HEADERS,
            json={"message": "legacy", "model": "gpt-3.5-turbo"},
        )
        assert response.status_code == 422

    async def test_quota_is_checked_after_canonical_body_validation(self, client, monkeypatch):
        async def reject(*_args, **_kwargs):
            raise credit.QuotaExceeded("quota exhausted")

        monkeypatch.setattr(credit, "precheck", reject)
        monkeypatch.setattr(admission, "existing_run_for_intent", lambda **_kwargs: _return(None))
        monkeypatch.setattr(completions, "_load_owned_conv", lambda *_args, **_kwargs: _return({}))
        monkeypatch.setattr(
            cs, "get_active_path", lambda *_args, **_kwargs: _return({"active_leaf_id": None, "messages": []})
        )
        response = await client.post(f"{_BASE}/c1/completions", headers=_HEADERS, json=_request())
        assert response.status_code == 402

    async def test_text_parts_create_durable_descriptor_without_provider_call(self, client, monkeypatch):
        await _patch_text_execution(monkeypatch)
        seen = {}

        async def create_persistent_run(**kwargs):
            seen.update(kwargs)
            return ChatRunDescriptor(
                run_id="run-1",
                status="queued",
                events_url="/api/v1/chat/runs/run-1/events",
                cancel_url="/api/v1/chat/runs/run-1/cancel",
            )

        monkeypatch.setattr(admission, "create_persistent_run", create_persistent_run)
        response = await client.post(
            f"{_BASE}/c1/completions",
            headers=_HEADERS,
            json=_request("first", client_timezone="Asia/Seoul"),
        )

        assert response.status_code == 202
        assert response.json()["run_id"] == "run-1"
        assert seen["client_request_id"] == _HEADERS["Idempotency-Key"]
        assert isinstance(seen["client_request_id"], str)
        assert seen["request_payload"]["input_messages"][-1] == {"role": "user", "content": "first"}
        assert seen["intent"]["parts"] == [{"type": "text", "text": "first"}]
        assert seen["execution_protocol_version"] == 1
        assert seen["capability_snapshot"]["execution_protocol_version"] == 1
        assert seen["client_timezone"] == "Asia/Seoul"
        assert seen["request_payload"]["client_timezone"] == "Asia/Seoul"

    async def test_v2_completion_freezes_effective_agent_policy(self, client, monkeypatch):
        await _patch_text_execution(monkeypatch)
        seen = {}
        settings = completions.get_settings().model_copy(update={"chat_execution_protocol_version": 2})
        monkeypatch.setattr(completions, "get_settings", lambda: settings)
        monkeypatch.setattr(common, "_require_supported_execution_protocol_version", lambda _version: None)
        monkeypatch.setattr(
            chat_admission,
            "_resolve_agent",
            lambda *_args: _return(
                {
                    "id": 9,
                    "role": "general",
                    "instructions": None,
                    "params": {},
                    "tool_ids": [],
                    "mcp_ids": [],
                    "execution_policy": {
                        "allowed_modes": ["chat"],
                        "can_delegate_read": False,
                        "can_delegate_write": False,
                        "max_model_turns": 3,
                        "max_tool_calls": 7,
                        "max_children": 0,
                        "max_parallel_children": 0,
                        "max_child_depth": 0,
                    },
                }
            ),
        )

        async def create_persistent_run(**kwargs):
            seen.update(kwargs)
            return ChatRunDescriptor(
                run_id="run-policy",
                status="queued",
                events_url="/api/v1/chat/runs/run-policy/events",
                cancel_url="/api/v1/chat/runs/run-policy/cancel",
            )

        monkeypatch.setattr(admission, "create_persistent_run", create_persistent_run)
        response = await client.post(f"{_BASE}/c1/completions", headers=_HEADERS, json=_request(agent_id="9"))

        assert response.status_code == 202
        policy = seen["request_payload"]["execution_policy"]
        assert policy["max_model_turns"] == 3
        assert policy["max_tool_calls"] == 7
        assert seen["capability_snapshot"]["execution_policy"] == policy

    async def test_same_idempotency_key_returns_existing_before_user_message_write(self, client, monkeypatch):
        await _patch_text_execution(monkeypatch)
        existing = ChatRunDescriptor(
            run_id="run-existing",
            status="queued",
            events_url="/api/v1/chat/runs/run-existing/events",
            cancel_url="/api/v1/chat/runs/run-existing/cancel",
        )
        monkeypatch.setattr(admission, "existing_run_for_intent", lambda *args, **kwargs: _return(existing))
        monkeypatch.setattr(completions, "get_settings", lambda: SimpleNamespace(chat_execution_protocol_version=2))

        async def unexpected_message(*_args, **_kwargs):
            raise AssertionError("retry must not create another user message")

        monkeypatch.setattr(cs, "add_message", unexpected_message)
        response = await client.post(f"{_BASE}/c1/completions", headers=_HEADERS, json=_request("first"))

        assert response.status_code == 202
        assert response.json()["run_id"] == "run-existing"

    async def test_asset_parts_are_rejected_until_asset_execution_exists(self, client, monkeypatch):
        await _patch_text_execution(monkeypatch)
        response = await client.post(
            f"{_BASE}/c1/completions",
            headers=_HEADERS,
            json={"parts": [{"type": "image", "asset_id": "asset-1"}], "model_id": "gpt-3.5-turbo", "features": {}},
        )
        assert response.status_code == 422
        assert "image" in response.json()["detail"]

    @pytest.mark.parametrize(
        ("effort_values", "expected_status"),
        [(["minimal", "low", "medium", "high"], 422), (["none", "low", "medium", "high"], 202)],
    )
    async def test_explicit_none_is_admitted_only_when_the_model_advertises_it(
        self, client, monkeypatch, effort_values, expected_status
    ):
        await _patch_text_execution(monkeypatch)
        resolved = {
            **_resolved(),
            "model_name": "gpt-5",
            "provider_type": "openai",
            "capabilities": {
                **_resolved()["capabilities"],
                "reasoning": True,
                "reasoning_options": [{"type": "effort", "values": effort_values}],
            },
        }
        monkeypatch.setattr(ps, "resolve_model", lambda *args, **kwargs: _return(resolved))
        seen = {}

        async def create_persistent_run(**kwargs):
            seen.update(kwargs)
            return ChatRunDescriptor(
                run_id="run-none",
                status="queued",
                events_url="/api/v1/chat/runs/run-none/events",
                cancel_url="/api/v1/chat/runs/run-none/cancel",
            )

        monkeypatch.setattr(admission, "create_persistent_run", create_persistent_run)
        response = await client.post(
            f"{_BASE}/c1/completions",
            headers=_HEADERS,
            json=_request(reasoning_effort="none", features={"memory": False, "tool_policy": {"mode": "none"}}),
        )

        assert response.status_code == expected_status
        if expected_status == 422:
            assert "'none'" in response.json()["detail"]
            assert seen == {}
        else:
            assert seen["request_payload"]["reasoning_effort"] == "none"

    async def test_clean_image_part_enters_durable_request_payload(self, client, monkeypatch):
        await _patch_text_execution(monkeypatch)
        resolved = {
            **_resolved(),
            "capabilities": {
                "vision": True,
                "feature_gates": {
                    "text": {"available": True, "pricing_available": True},
                    "image_input": {"available": True, "pricing_available": True},
                },
            },
        }
        monkeypatch.setattr(ps, "resolve_model", lambda *args, **kwargs: _return(resolved))
        seen = {}

        async def create_persistent_run(**kwargs):
            seen.update(kwargs)
            return ChatRunDescriptor(
                run_id="run-image",
                status="queued",
                events_url="/api/v1/chat/runs/run-image/events",
                cancel_url="/api/v1/chat/runs/run-image/cancel",
            )

        monkeypatch.setattr(admission, "create_persistent_run", create_persistent_run)
        response = await client.post(
            f"{_BASE}/c1/completions",
            headers=_HEADERS,
            json={
                "parts": [
                    {"type": "text", "text": "describe"},
                    {"type": "image", "asset_id": "f6d18ec7-c8d8-4db8-9bd7-2dceb0f0f68e"},
                ],
                "model_id": "gpt-3.5-turbo",
                "features": {},
            },
        )

        assert response.status_code == 202
        assert seen["user_parts"][1]["asset_id"] == "f6d18ec7-c8d8-4db8-9bd7-2dceb0f0f68e"
        assert seen["request_payload"]["input_parts"] == seen["user_parts"]

    async def test_regenerate_requires_canonical_model_and_feature_options(self, client):
        response = await client.post(
            f"{_BASE}/c1/messages/1/regenerate",
            headers=_HEADERS,
            json={"model": "gpt-3.5-turbo"},
        )
        assert response.status_code == 422


class TestRetryFailedRun:
    async def test_retry_preserves_agent_and_mcp_extensions(self, client, monkeypatch):
        await _patch_text_execution(monkeypatch)
        monkeypatch.setattr(
            completions,
            "get_settings",
            lambda: SimpleNamespace(chat_execution_protocol_version=1, chat_credit_per_usd=1000.0),
        )

        class FakeSourceRun:
            id = "run-source-1"
            conversation_id = "c1"
            user_message_id = 12
            user_id = "test-user-123"
            project_id = "test-project-123"
            status = "failed"
            model_name = "gpt-3.5-turbo"
            agent_id = 7
            execution_mode = "chat"
            credit_ceiling = None
            sandbox_seconds_ceiling = None
            wall_time_seconds = None
            request_payload = cs._enc(
                json.dumps(
                    {
                        "features": {},
                        "reasoning_effort": "auto",
                        "skill_ids": [3],
                        "input_parts": [{"type": "text", "text": "retry turn"}],
                    }
                )
            )

        def fake_factory():
            class FakeSession:
                async def __aenter__(self):
                    return self

                async def __aexit__(self, *args):
                    pass

                async def execute(self, query):
                    class FakeResult:
                        def scalars(self):
                            class FakeScalars:
                                def first(self):
                                    return FakeSourceRun()

                            return FakeScalars()

                    return FakeResult()

            return lambda: FakeSession()

        monkeypatch.setattr(admission, "existing_run_for_intent", lambda *args, **kwargs: _return(None))
        monkeypatch.setattr(common, "_factory", fake_factory)
        monkeypatch.setattr(
            chat_admission,
            "_resolve_agent",
            lambda agent_id, user_id, project_id: _return({"id": 7, "instructions": "agent-7"}),
        )
        mcp_item = {
            "id": 99,
            "name": "mcp-server",
            "transport": "http",
            "url": "https://mcp.example",
            "effect": "read",
            "origin": "https://mcp.example",
            "config_fingerprint": "a" * 64,
        }
        monkeypatch.setattr(
            chat_admission,
            "_resolve_extension_selection",
            lambda agent, features, *, user_id, project_id: _return({"tools": [], "mcp": [mcp_item]}),
        )

        async def fake_list_for_user(kind, **kwargs):
            if kind == "skill":
                return [{"id": 3, "name": "skill-3", "instructions": "skill-3-instructions"}]
            return []

        monkeypatch.setattr(chat_admission.es, "list_for_user", fake_list_for_user)
        monkeypatch.setattr(
            chat_admission,
            "_load_skill_snapshot",
            lambda *_args, **_kwargs: _return(
                (
                    ["skill-3-instructions"],
                    [{"id": 3, "name": "skill-3", "content_hash": "a" * 64}],
                )
            ),
        )
        monkeypatch.setattr(
            cs,
            "path_ending_at",
            lambda conversation_id, user_id, project_id, message_id: _return(
                [{"role": "user", "content": "retry turn"}]
            ),
        )

        seen_create = {}

        async def fake_create_run(**kwargs):
            seen_create.update(kwargs)
            return ChatRunDescriptor(
                run_id="run-retry-1",
                conversation_id="c1",
                status="queued",
                events_url="/events",
                cancel_url="/cancel",
            )

        monkeypatch.setattr(admission, "create_run", fake_create_run)

        response = await client.post(f"{_BASE}/c1/runs/run-source-1/retry", headers=_HEADERS)

        assert response.status_code == 202, f"Failed with: {response.json()}"
        assert seen_create["request_payload"]["extension_snapshot"] == {"tools": [], "mcp": [mcp_item]}
        assert seen_create["request_payload"]["skill_ids"] == [3]

    async def test_retry_idempotency_returns_same_descriptor_on_lost_response(self, client, monkeypatch):
        await _patch_text_execution(monkeypatch)
        created_descriptor = ChatRunDescriptor(
            run_id="run-retry-existing",
            conversation_id="c1",
            status="queued",
            events_url="/events",
            cancel_url="/cancel",
        )

        async def fake_existing(project_id, user_id, client_request_id, intent, conversation_id):
            assert intent["endpoint"] == "retry"
            assert intent["source_run_id"] == "run-source-1"
            assert client_request_id == _HEADERS["Idempotency-Key"]
            return created_descriptor

        monkeypatch.setattr(admission, "existing_run_for_intent", fake_existing)

        async def unexpected_db_lookup(*args, **kwargs):
            raise AssertionError("a replayed retry with the same idempotency key must not query DB for the source run")

        monkeypatch.setattr(common, "_factory", unexpected_db_lookup)

        response = await client.post(f"{_BASE}/c1/runs/run-source-1/retry", headers=_HEADERS)

        assert response.status_code == 202
        assert response.json()["run_id"] == "run-retry-existing"

    async def test_retry_rejects_quota_exceeded_before_creating_run(self, client, monkeypatch):
        await _patch_text_execution(monkeypatch)

        async def quota_exceeded(user_id, project_id=None, api_key_id=None):
            raise credit.QuotaExceeded("월간 사용량 쿼터를 초과했습니다")

        monkeypatch.setattr(credit, "precheck", quota_exceeded)

        async def unexpected_create(*args, **kwargs):
            raise AssertionError("create_run must not be called when credit precheck raises QuotaExceeded")

        monkeypatch.setattr(admission, "create_run", unexpected_create)

        response = await client.post(f"{_BASE}/c1/runs/run-source-1/retry", headers=_HEADERS)

        assert response.status_code == 402
        assert "월간 사용량 쿼터" in response.json()["detail"]


class TestCanonicalTempCompletion:
    async def test_temp_completion_creates_thread_and_descriptor(self, client, monkeypatch):
        await _patch_text_execution(monkeypatch)
        monkeypatch.setattr(credit, "precheck", _ok_precheck)
        monkeypatch.setattr(ps, "resolve_model", lambda *args, **kwargs: _return(_resolved()))
        monkeypatch.setattr(admission, "existing_run_for_intent", lambda **_kwargs: _return(None))
        seen = {}

        async def create_temp_run(**kwargs):
            seen.update(kwargs)
            return ChatRunDescriptor(
                run_id="run-temp",
                temp_thread_id="thread-1",
                status="queued",
                events_url="/api/v1/chat/runs/run-temp/events",
                cancel_url="/api/v1/chat/runs/run-temp/cancel",
            )

        monkeypatch.setattr(admission, "create_temp_run", create_temp_run)
        response = await client.post(
            "/api/v1/chat/temp-completions",
            headers=_HEADERS,
            json={"parts": [{"type": "text", "text": "temporary"}], "model_id": "gpt-3.5-turbo", "features": {}},
        )
        assert response.status_code == 202
        assert seen["temp_thread_id"] is None
        assert seen["request_payload"]["input_messages"] == [{"role": "user", "content": "temporary"}]

    async def test_native_completion_with_catalog_model_and_default_features(self, client, monkeypatch):
        await _patch_text_execution(monkeypatch)

        async def fake_list_models(active_only=True):
            return [
                {
                    "id": 1,
                    "provider_id": 10,
                    "model_name": "perplexity/perplexity/sonar",
                    "api_model_name": "perplexity/sonar",
                    "api_provider": "perplexity",
                    "display_name": "sonar",
                    "effective_capabilities": {"web_search": True, "tool_call": True},
                    "capabilities": {"web_search": True, "tool_call": True},
                }
            ]

        async def fake_list_providers():
            return [{"id": 10, "name": "Perplexity", "provider_type": "perplexity", "has_api_key": True}]

        monkeypatch.setattr(repository, "list_models", fake_list_models)
        monkeypatch.setattr(repository, "list_providers", fake_list_providers)

        models_resp = await client.get("/api/v1/chat/models")
        assert models_resp.status_code == 200
        catalog_models = models_resp.json()
        assert len(catalog_models) == 1
        model_entry = catalog_models[0]
        assert "provider_id" not in model_entry
        assert model_entry["capabilities"]["web_search"] is True

        monkeypatch.setattr(
            ps,
            "resolve_model",
            lambda name: _return(
                {
                    "model_name": name,
                    "provider_name": "Perplexity",
                    "provider_id": 10,
                    "model_id": 1,
                    "config_version_hash": "testhash",
                    "input_price_per_token": Decimal("0"),
                    "output_price_per_token": Decimal("0"),
                    "capabilities": {"web_search": True, "tool_call": True},
                }
            ),
        )

        async def fake_create_temp_run(**kwargs):
            return ChatRunDescriptor(
                run_id="run-native-search",
                status="queued",
                events_url="/api/v1/chat/runs/run-native-search/events",
                cancel_url="/api/v1/chat/runs/run-native-search/cancel",
            )

        monkeypatch.setattr(admission, "create_temp_run", fake_create_temp_run)

        response = await client.post(
            "/api/v1/chat/temp-completions",
            headers=_HEADERS,
            json={
                "parts": [{"type": "text", "text": "What is the latest news?"}],
                "model_id": model_entry["model_name"],
                "features": {
                    "tool_policy": {"mode": "agent_default"},
                    "web_search": {"enabled": False},
                },
            },
        )
        assert response.status_code == 202


class TestApiKeyLimitAdmission:
    async def test_all_native_admission_callsites_forward_api_key_id(self, client, monkeypatch):
        await _patch_text_execution(monkeypatch)
        recorded = []

        async def spy_precheck(user_id, project_id=None, api_key_id=None):
            recorded.append((user_id, project_id, api_key_id))
            raise HTTPException(status_code=418, detail="stop after precheck")

        monkeypatch.setattr(credit, "precheck", spy_precheck)
        monkeypatch.setattr(admission, "existing_run_for_intent", lambda **_kwargs: _return(None))
        monkeypatch.setattr(completions, "_load_owned_conv", lambda *_args, **_kwargs: _return({}))

        async def fake_key_principal():
            return {
                "auth_type": "api_key",
                "user_id": "test-user-1",
                "project_id": "test-project-1",
                "api_key_id": 42,
                "scopes": (
                    "native:conversations:write",
                    "native:runs:write",
                    "native:memory:read",
                    "native:memory:write",
                    "native:extensions:read",
                    "native:tools:execute",
                ),
                "source": "api",
                "roles": [],
                "is_system_admin": False,
            }

        monkeypatch.setitem(app.dependency_overrides, get_principal, fake_key_principal)
        recorded.clear()

        resp_create = await client.post(f"{_BASE}/c1/completions", headers=_HEADERS, json=_request())
        assert resp_create.status_code == 418
        assert len(recorded) == 1
        assert recorded[0] == ("test-user-1", "test-project-1", 42)

        monkeypatch.setattr(
            cs,
            "find_turn_start_user",
            lambda *args, **kwargs: _return(
                {
                    "id": "msg-1",
                    "parent_id": None,
                    "content": "original prompt",
                    "parts": [{"type": "text", "text": "original prompt"}],
                }
            ),
        )
        monkeypatch.setattr(cs, "path_ending_at", lambda *args, **kwargs: _return([]))
        resp_retry = await client.post(f"{_BASE}/c1/runs/run-source-1/retry", headers=_HEADERS)
        assert resp_retry.status_code == 418
        assert len(recorded) == 2
        assert recorded[1] == ("test-user-1", "test-project-1", 42)
        resp_temp = await client.post(
            "/api/v1/chat/temp-completions",
            headers=_HEADERS,
            json={"parts": [{"type": "text", "text": "temporary"}], "model_id": "gpt-3.5-turbo", "features": {}},
        )
        assert resp_temp.status_code == 418

        resp_regen = await client.post(
            f"{_BASE}/c1/messages/1/regenerate",
            headers=_HEADERS,
            json={"model_id": "gpt-3.5-turbo", "features": {}},
        )
        assert resp_regen.status_code == 418
        assert len(recorded) == 4
        assert recorded[3] == ("test-user-1", "test-project-1", 42)

    async def test_idempotent_replay_bypasses_credit_precheck(self, client, monkeypatch):
        await _patch_text_execution(monkeypatch)

        async def unexpected_precheck(*args, **kwargs):
            raise AssertionError("credit.precheck must NOT be called on idempotent replay")

        existing_desc = ChatRunDescriptor(
            run_id="run-existing-1",
            conversation_id="c1",
            status="queued",
            events_url="/api/v1/chat/runs/run-existing-1/events",
            cancel_url="/api/v1/chat/runs/run-existing-1/cancel",
        )

        monkeypatch.setattr(credit, "precheck", unexpected_precheck)
        monkeypatch.setattr(admission, "existing_run_for_intent", lambda **_kwargs: _return(existing_desc))
        monkeypatch.setattr(completions, "_load_owned_conv", lambda *_args, **_kwargs: _return({}))
        monkeypatch.setattr(
            cs,
            "find_turn_start_user",
            lambda *args, **kwargs: _return({"id": 1, "parts": [{"type": "text", "text": "hi"}]}),
        )

        resp_create = await client.post(f"{_BASE}/c1/completions", headers=_HEADERS, json=_request())
        assert resp_create.status_code == 202
        assert resp_create.json()["run_id"] == "run-existing-1"

        resp_retry = await client.post(f"{_BASE}/c1/runs/run-source-1/retry", headers=_HEADERS)
        assert resp_retry.status_code == 202
        assert resp_retry.json()["run_id"] == "run-existing-1"

        resp_temp = await client.post(
            "/api/v1/chat/temp-completions",
            headers=_HEADERS,
            json={"parts": [{"type": "text", "text": "temporary"}], "model_id": "gpt-3.5-turbo", "features": {}},
        )
        assert resp_temp.status_code == 202
        assert resp_temp.json()["run_id"] == "run-existing-1"

        resp_regen = await client.post(
            f"{_BASE}/c1/messages/1/regenerate",
            headers=_HEADERS,
            json={"model_id": "gpt-3.5-turbo", "features": {}},
        )
        assert resp_regen.status_code == 202
        assert resp_regen.json()["run_id"] == "run-existing-1"

    async def test_malformed_idempotency_key_returns_422(self, client):
        bad_headers = {"Idempotency-Key": "invalid-uuid"}
        resp1 = await client.post(f"{_BASE}/c1/completions", headers=bad_headers, json=_request())
        assert resp1.status_code == 422
        resp2 = await client.post(
            "/api/v1/chat/temp-completions",
            headers=bad_headers,
            json={"parts": [{"type": "text", "text": "temp"}], "model_id": "gpt-3.5-turbo", "features": {}},
        )
        assert resp2.status_code == 422
        resp3 = await client.post(
            f"{_BASE}/c1/messages/1/regenerate",
            headers=bad_headers,
            json={"model_id": "gpt-3.5-turbo", "features": {}},
        )
        assert resp3.status_code == 422
        resp4 = await client.post(f"{_BASE}/c1/runs/r1/retry", headers=bad_headers)
        assert resp4.status_code == 422

    async def test_admission_fallback_raises_input_error_for_invalid_uuid(self):
        with pytest.raises(durable_errors.DurableRunInputError) as exc_info:
            await admission.existing_run_for_intent(
                project_id="p1",
                user_id="u1",
                client_request_id="not-a-uuid",
                intent={},
                conversation_id="c1",
            )
        assert "Idempotency-Key must be a UUID" in str(exc_info.value)


class TestContextPreviewRoutes:
    async def test_conversation_context_preview_returns_reconciling_breakdown(self, client, monkeypatch):
        real_preview_schemas = chat_admission._preview_tool_schemas
        await _patch_text_execution(monkeypatch)
        monkeypatch.setattr(chat_admission, "_preview_tool_schemas", real_preview_schemas)
        monkeypatch.setattr(
            context_store,
            "load_context_source",
            lambda **_kwargs: _return(
                {
                    "messages": [
                        {"role": "user", "content": "first question"},
                        {"role": "assistant", "content": "first answer"},
                    ],
                    "message_ids": ["1", "2"],
                    "source_hashes": ["hash1", "hash2"],
                    "active_leaf_id": "2",
                    "revision": "rev-1",
                    "checkpoint_id": None,
                    "checkpoint": None,
                }
            ),
        )
        monkeypatch.setattr(
            chat_admission.ws, "get_instructions_for_run", lambda *_args, **_kwargs: _return("workspace rules")
        )
        monkeypatch.setattr(
            chat_admission,
            "_load_skill_snapshot",
            lambda *_args, **_kwargs: _return((["skill body"], [{"id": 3, "name": "Docs", "content_hash": "h"}])),
        )

        async def fake_custom_schema(item, *, user_id, project_id):
            return {
                "type": "function",
                "function": {
                    "name": chat_admission._custom_tool_identity(item),
                    "description": item["description"],
                    "parameters": {**item["params_schema"], "additionalProperties": False},
                },
            }

        monkeypatch.setattr(chat_admission, "custom_tool_schema", fake_custom_schema)
        monkeypatch.setattr(
            chat_admission,
            "_resolve_extension_selection",
            lambda *_args, **_kwargs: _return(
                {
                    "tools": [
                        {
                            "id": 1,
                            "name": "preloaded_tool",
                            "description": "always sent",
                            "params_schema": {"type": "object"},
                            "effect": "read",
                            "origin": None,
                            "config_fingerprint": "f1",
                            "load_policy": "preloaded",
                            "destination_origin": "https://tool.example",
                        },
                        {
                            "id": 2,
                            "name": "catalog_only_tool",
                            "description": "loaded on demand",
                            "params_schema": {"type": "object"},
                            "effect": "read",
                            "origin": None,
                            "config_fingerprint": "f2",
                            "load_policy": "on_demand",
                            "destination_origin": "https://tool.example",
                        },
                    ],
                    "mcp": [
                        {
                            "id": 5,
                            "name": "GitHub",
                            "effect": "read",
                            "origin": None,
                            "config_fingerprint": "f3",
                            "load_policy": "preloaded",
                            "destination_origin": "https://mcp.example",
                        }
                    ],
                }
            ),
        )

        resp = await client.post(
            f"{_BASE}/c1/context-preview",
            json={"model_id": "gpt-3.5-turbo", "features": {}, "parts": [{"type": "text", "text": "draft"}]},
        )

        assert resp.status_code == 200
        data = resp.json()
        breakdown = data["breakdown"]
        components = {component["id"]: component for component in breakdown["components"]}

        assert breakdown["scope"] == "preview"
        assert (
            sum(component["tokens"] for component in breakdown["components"] if component["included"])
            == (data["input_tokens"])
        )
        assert components["workspace"]["count"] == 1
        assert components["skills"]["items"] == ["Docs"]
        assert components["messages"]["count"] == 3
        # Preloaded extensions are in the first provider request; on-demand ones are not.
        assert any(name.startswith("custom__1__preloaded_tool_") for name in components["tools"]["items"])
        assert "list_available_tools" in components["tools"]["items"]
        assert not any(name.startswith("custom__2__") for name in components["tools"]["items"])
        assert components["deferred_tools"]["items"] == ["catalog_only_tool"]
        assert components["deferred_tools"]["included"] is False
        # Remote MCP schemas are never discovered in preview.
        assert components["mcp_tools"] == {
            "id": "mcp_tools",
            "tokens": None,
            "measurement": "unknown",
            "count": None,
            "included": False,
            "items": ["mcp:GitHub"],
        }
        assert breakdown["uncounted"] == ["mcp_tools"]
        assert breakdown["complete"] is False

    async def test_context_preview_breakdown_never_leaks_prompt_text(self, client, monkeypatch):
        real_preview_schemas = chat_admission._preview_tool_schemas
        await _patch_text_execution(monkeypatch)
        monkeypatch.setattr(chat_admission, "_preview_tool_schemas", real_preview_schemas)
        monkeypatch.setattr(
            chat_admission.ws, "get_instructions_for_run", lambda *_args, **_kwargs: _return("SECRET-WORKSPACE-RULE")
        )
        monkeypatch.setattr(
            context_store,
            "load_context_source",
            lambda **_kwargs: _return(
                {
                    "messages": [{"role": "user", "content": "SECRET-USER-TURN"}],
                    "message_ids": ["1"],
                    "source_hashes": ["hash1"],
                    "active_leaf_id": "1",
                    "revision": "rev-1",
                    "checkpoint_id": None,
                    "checkpoint": None,
                }
            ),
        )

        resp = await client.post(
            f"{_BASE}/c1/context-preview",
            json={"model_id": "gpt-3.5-turbo", "features": {}, "parts": []},
        )

        assert resp.status_code == 200
        serialized = json.dumps(resp.json()["breakdown"])
        assert "SECRET-WORKSPACE-RULE" not in serialized
        assert "SECRET-USER-TURN" not in serialized

    async def test_conversation_context_preview_returns_200_and_does_not_mutate_db_or_create_run(
        self, client, monkeypatch
    ):
        await _patch_text_execution(monkeypatch)
        monkeypatch.setattr(
            context_store,
            "load_context_source",
            lambda **kwargs: _return(
                {
                    "messages": [{"role": "user", "content": "hi"}],
                    "message_ids": ["1"],
                    "source_hashes": ["hash1"],
                    "active_leaf_id": "1",
                    "revision": "rev-1",
                    "checkpoint_id": None,
                    "checkpoint": None,
                }
            ),
        )

        body = {
            "model_id": "gpt-3.5-turbo",
            "features": {},
            "parts": [{"type": "text", "text": "draft part"}],
        }
        resp = await client.post(f"{_BASE}/c1/context-preview", json=body)
        assert resp.status_code == 200
        data = resp.json()
        assert data["model_name"] == "gpt-3.5-turbo"
        assert data["context_limit"] == 16000
        assert data["revision"] == "rev-1"
        assert data["measurement"] in {"tokenizer", "estimated"}
        assert "utilization" in data

    async def test_temp_thread_context_preview_returns_200(self, client, monkeypatch):
        await _patch_text_execution(monkeypatch)
        monkeypatch.setattr(
            context_store,
            "load_context_source",
            lambda **kwargs: _return(
                {
                    "messages": [{"role": "user", "content": "hello"}],
                    "message_ids": ["1"],
                    "source_hashes": ["hash1"],
                    "active_leaf_id": None,
                    "revision": "rev-temp-1",
                    "checkpoint_id": None,
                    "checkpoint": None,
                }
            ),
        )

        body = {
            "model_id": "gpt-3.5-turbo",
            "features": {},
            "parts": [],
        }
        resp = await client.post("/api/v1/chat/temp-threads/t1/context-preview", json=body)
        assert resp.status_code == 200
        data = resp.json()
        assert data["revision"] == "rev-temp-1"

    async def test_temp_thread_context_preview_rejects_agent_and_code_mode(self, client, monkeypatch):
        await _patch_text_execution(monkeypatch)
        body = {
            "model_id": "gpt-3.5-turbo",
            "features": {},
            "agent_id": 99,
        }
        resp = await client.post("/api/v1/chat/temp-threads/t1/context-preview", json=body)
        assert resp.status_code == 422


class TestCompactionRoutes:
    async def test_conversation_compaction_returns_202_descriptor_with_compaction_kind(self, client, monkeypatch):
        await _patch_text_execution(monkeypatch)
        monkeypatch.setattr(
            context_store,
            "load_context_source",
            lambda **kwargs: _return(
                {
                    "messages": [
                        {"role": "user", "content": "turn 1"},
                        {"role": "assistant", "content": "resp 1"},
                        {"role": "user", "content": "turn 2"},
                        {"role": "assistant", "content": "resp 2"},
                        {"role": "user", "content": "turn 3"},
                        {"role": "assistant", "content": "resp 3"},
                    ],
                    "message_ids": ["1", "2", "3", "4", "5", "6"],
                    "source_hashes": ["h1", "h2", "h3", "h4", "h5", "h6"],
                    "active_leaf_id": "6",
                    "revision": "rev-abc",
                    "checkpoint_id": None,
                    "checkpoint": None,
                }
            ),
        )

        desc = ChatRunDescriptor(
            run_id="run-compact-1",
            run_kind="compaction",
            status="queued",
            events_url="/v1/runs/run-compact-1/events",
            cancel_url="/v1/runs/run-compact-1/cancel",
        )
        monkeypatch.setattr(admission, "create_compaction_run", lambda **kwargs: _return(desc))
        seen_create: dict = {}

        async def create_compaction_run(**kwargs):
            seen_create.update(kwargs)
            return desc

        large_tool_schema = {
            "type": "function",
            "function": {
                "name": "large_schema",
                "description": "x" * 50_000,
                "parameters": {"type": "object"},
            },
        }
        resolved = _resolved()
        resolved["capabilities"]["context_limit"] = 100_000
        monkeypatch.setattr(ps, "resolve_model", lambda *args, **kwargs: _return(resolved))
        monkeypatch.setattr(
            chat_admission,
            "_preview_tool_schemas",
            lambda *_args, **_kwargs: _return([large_tool_schema]),
        )
        monkeypatch.setattr(admission, "create_compaction_run", create_compaction_run)

        body = {
            "model_id": "gpt-3.5-turbo",
            "features": {"tool_policy": {"mode": "agent_default"}},
            "expected_context_revision": "rev-abc",
        }
        resp = await client.post(
            f"{_BASE}/c1/compactions",
            headers=_HEADERS,
            json=body,
        )
        assert resp.status_code == 202
        data = resp.json()
        assert data["run_id"] == "run-compact-1"
        assert data["run_kind"] == "compaction"
        assert seen_create["request_payload"]["tool_schemas"] == [large_tool_schema]

    async def test_compaction_idempotency_returns_original_run(self, client, monkeypatch):
        await _patch_text_execution(monkeypatch)
        desc = ChatRunDescriptor(
            run_id="run-compact-orig",
            run_kind="compaction",
            status="queued",
            events_url="/v1/runs/run-compact-orig/events",
            cancel_url="/v1/runs/run-compact-orig/cancel",
        )
        monkeypatch.setattr(admission, "existing_run_for_intent", lambda **kwargs: _return(desc))

        body = {
            "model_id": "gpt-3.5-turbo",
            "features": {},
            "expected_context_revision": "any-rev",
        }
        resp = await client.post(
            f"{_BASE}/c1/compactions",
            headers=_HEADERS,
            json=body,
        )
        assert resp.status_code == 202
        assert resp.json()["run_id"] == "run-compact-orig"

    async def test_compaction_stale_revision_returns_409(self, client, monkeypatch):
        await _patch_text_execution(monkeypatch)
        monkeypatch.setattr(
            context_store,
            "load_context_source",
            lambda **kwargs: _return(
                {
                    "messages": [
                        {"role": "user", "content": "turn 1"},
                        {"role": "assistant", "content": "resp 1"},
                        {"role": "user", "content": "turn 2"},
                        {"role": "assistant", "content": "resp 2"},
                        {"role": "user", "content": "turn 3"},
                    ],
                    "message_ids": ["1", "2", "3", "4", "5"],
                    "source_hashes": ["h1", "h2", "h3", "h4", "h5"],
                    "active_leaf_id": "5",
                    "revision": "actual-rev-123",
                    "checkpoint_id": None,
                    "checkpoint": None,
                }
            ),
        )

        body = {
            "model_id": "gpt-3.5-turbo",
            "features": {},
            "expected_context_revision": "stale-rev-999",
        }
        resp = await client.post(
            f"{_BASE}/c1/compactions",
            headers=_HEADERS,
            json=body,
        )
        assert resp.status_code == 409
        assert resp.json()["detail"] == "context_revision_changed"

    async def test_compaction_nothing_to_compact_returns_422(self, client, monkeypatch):
        await _patch_text_execution(monkeypatch)
        monkeypatch.setattr(
            context_store,
            "load_context_source",
            lambda **kwargs: _return(
                {
                    "messages": [{"role": "user", "content": "short hello"}],
                    "message_ids": ["1"],
                    "source_hashes": ["h1"],
                    "active_leaf_id": "1",
                    "revision": "rev-short",
                    "checkpoint_id": None,
                    "checkpoint": None,
                }
            ),
        )

        body = {
            "model_id": "gpt-3.5-turbo",
            "features": {},
            "expected_context_revision": "rev-short",
        }
        resp = await client.post(
            f"{_BASE}/c1/compactions",
            headers=_HEADERS,
            json=body,
        )
        assert resp.status_code == 422
        assert resp.json()["detail"] == "nothing_to_compact"

    @pytest.mark.parametrize("path", [f"{_BASE}/c1/compactions", "/api/v1/chat/temp-threads/t1/compactions"])
    @pytest.mark.parametrize(
        ("context_limit", "tokens", "reason"),
        [(None, 12, "context_window_unknown"), (1, 12, "invalid_budget"), (16000, None, "token_count_unavailable")],
    )
    async def test_compaction_unavailable_reports_precise_reason(
        self, client, monkeypatch, path, context_limit, tokens, reason
    ):
        from lumen.services import context_manager
        from lumen.services.litellm_client import ContextTokenCount

        monkeypatch.setattr(
            context_manager,
            "count_context_tokens",
            lambda *_args: ContextTokenCount(
                tokens=tokens, measurement="estimated" if tokens is not None else "unknown"
            ),
        )
        await _patch_text_execution(monkeypatch)
        res = _resolved()
        res["capabilities"]["context_limit"] = context_limit
        monkeypatch.setattr(ps, "resolve_model", lambda *args, **kwargs: _return(res))
        monkeypatch.setattr(
            context_store,
            "load_context_source",
            lambda **kwargs: _return(
                {
                    "messages": [
                        {"role": "user", "content": "turn 1"},
                        {"role": "assistant", "content": "resp 1"},
                        {"role": "user", "content": "turn 2"},
                        {"role": "assistant", "content": "resp 2"},
                        {"role": "user", "content": "turn 3"},
                    ],
                    "message_ids": ["1", "2", "3", "4", "5"],
                    "source_hashes": ["h1", "h2", "h3", "h4", "h5"],
                    "active_leaf_id": "5",
                    "revision": "rev-nobudget",
                    "checkpoint_id": None,
                    "checkpoint": None,
                }
            ),
        )

        body = {
            "model_id": "gpt-3.5-turbo",
            "features": {},
            "expected_context_revision": "rev-nobudget",
        }
        resp = await client.post(
            path,
            headers=_HEADERS,
            json=body,
        )
        assert resp.status_code == 422
        assert resp.json()["detail"] == reason

    async def test_compaction_active_run_conflict_returns_409(self, client, monkeypatch):
        await _patch_text_execution(monkeypatch)
        monkeypatch.setattr(
            context_store,
            "load_context_source",
            lambda **kwargs: _return(
                {
                    "messages": [
                        {"role": "user", "content": "turn 1"},
                        {"role": "assistant", "content": "resp 1"},
                        {"role": "user", "content": "turn 2"},
                        {"role": "assistant", "content": "resp 2"},
                        {"role": "user", "content": "turn 3"},
                    ],
                    "message_ids": ["1", "2", "3", "4", "5"],
                    "source_hashes": ["h1", "h2", "h3", "h4", "h5"],
                    "active_leaf_id": "5",
                    "revision": "rev-active",
                    "checkpoint_id": None,
                    "checkpoint": None,
                }
            ),
        )

        async def active_conflict(**kwargs):
            raise durable_errors.DurableRunConflict("conversation_run_active")

        monkeypatch.setattr(admission, "create_compaction_run", active_conflict)

        body = {
            "model_id": "gpt-3.5-turbo",
            "features": {},
            "expected_context_revision": "rev-active",
        }
        resp = await client.post(
            f"{_BASE}/c1/compactions",
            headers=_HEADERS,
            json=body,
        )
        assert resp.status_code == 409
        assert resp.json()["detail"] == "conversation_run_active"
