import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from lumen.api import completions
from lumen.models.chat_contracts import ChatFeatureOptions, ChatRunDescriptor, UserAssetInputPart
from lumen.services import capabilities, chat_admission, credit
from lumen.services import conversation_store as cs
from lumen.services.durable_runs import admission, common, queries
from lumen.services.durable_runs import errors as durable_errors
from lumen.services.providers import routing as ps

_BASE = "/api/v1/chat/conversations"
_HEADERS = {"Idempotency-Key": "d27ac16a-0e5b-465f-89cc-eefe6e9d0001"}


def _request(text: str = "hello", **extra):
    return {"parts": [{"type": "text", "text": text}], "model_id": "gpt-3.5-turbo", "features": {}, **extra}


async def test_selected_skills_are_loaded_once_from_owned_active_extensions(monkeypatch):
    async def fake_list(kind, **kwargs):
        assert kind == "skill"
        assert kwargs == {"user_id": "u1", "project_id": "p1", "active_only": True}
        return [
            {"id": 1, "name": "safe", "instructions": "first"},
            {"id": 2, "name": "unused", "instructions": "second"},
        ]

    monkeypatch.setattr(chat_admission.es, "list_for_user", fake_list)
    instructions, provenance = await completions._load_skill_snapshot(None, [1], "u1", "p1")

    assert instructions == ["first"]
    assert provenance[0]["id"] == 1
    assert provenance[0]["name"] == "safe"
    assert len(provenance[0]["content_hash"]) == 64


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

    no_agent = await completions._resolve_extension_selection(None, ChatFeatureOptions(), user_id="u1", project_id="p1")
    assert [item["id"] for item in no_agent["tools"]] == [7, 8]
    assert [item["id"] for item in no_agent["mcp"]] == [7, 8]
    assert [item["credential_version"] for item in no_agent["mcp"]] == [3, 4]

    agent = {"tool_ids": [7], "mcp_ids": []}
    agent_default = await completions._resolve_extension_selection(
        agent, ChatFeatureOptions(), user_id="u1", project_id="p1"
    )
    assert [item["id"] for item in agent_default["tools"]] == [7]
    assert agent_default["mcp"] == []

    explicit = await completions._resolve_extension_selection(
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

    selection = await completions._resolve_extension_selection(
        {"tool_ids": [], "mcp_ids": [7]},
        ChatFeatureOptions(),
        user_id="u1",
        project_id="p1",
    )

    assert selection["mcp"][0]["credential_version"] == 4


async def test_extension_selection_rejects_agent_allowlist_bypass(monkeypatch):
    monkeypatch.setattr(chat_admission.es, "list_for_user", lambda *_args, **_kwargs: _return([]))

    with pytest.raises(HTTPException, match="allowlist"):
        await completions._resolve_extension_selection(
            {"tool_ids": [7], "mcp_ids": []},
            ChatFeatureOptions(tool_policy={"enabled_tool_ids": [8]}),
            user_id="u1",
            project_id="p1",
        )


async def _ok_precheck(user_id, project_id=None):
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
    }


async def _patch_text_execution(monkeypatch):
    monkeypatch.setattr(credit, "precheck", _ok_precheck)
    monkeypatch.setattr(cs, "get_conversation", lambda *args, **kwargs: _return(_conv()))
    monkeypatch.setattr(
        cs, "get_active_path", lambda *args, **kwargs: _return({"messages": [], "active_leaf_id": None})
    )
    monkeypatch.setattr(cs, "add_message", lambda *args, **kwargs: _return({"id": 1}))
    monkeypatch.setattr(ps, "resolve_model", lambda *args, **kwargs: _return(_resolved()))
    monkeypatch.setattr(admission, "existing_run_for_intent", lambda *args, **kwargs: _return(None))
    monkeypatch.setattr(chat_admission.ms, "active_contents_for_run", lambda *args, **kwargs: _return([]))
    monkeypatch.setattr(chat_admission.ws, "get_instructions_for_run", lambda *args, **kwargs: _return(None))


async def _return(value):
    return value


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
        assert completions._validated_reasoning_effort("auto", resolved) == "auto"
        assert completions._validated_reasoning_effort("none", resolved) == "none"

    def test_only_models_dev_effort_values_are_allowed(self):
        resolved = {
            "capabilities": {
                "reasoning": True,
                "reasoning_options": [{"type": "effort", "values": ["low", "xhigh", "max", "ultra"]}],
            }
        }
        assert completions._validated_reasoning_effort("ultra", resolved) == "ultra"
        with pytest.raises(HTTPException, match="지원하지 않습니다"):
            completions._validated_reasoning_effort("high", resolved)

    def test_unlisted_effort_is_rejected_for_toggle_only_models(self):
        resolved = {"capabilities": {"reasoning": True, "reasoning_options": [{"type": "toggle"}]}}
        with pytest.raises(HTTPException, match="지원하지 않습니다"):
            completions._validated_reasoning_effort("low", resolved)

    def test_gpt5_tools_reject_explicit_reasoning_but_allow_auto_or_none(self):
        resolved = {"provider_type": "openai", "model_name": "gpt-5.6-luna"}
        tools_enabled = ChatFeatureOptions()

        completions._validate_tool_reasoning_compatibility("auto", resolved, tools_enabled)
        completions._validate_tool_reasoning_compatibility("none", resolved, tools_enabled)
        with pytest.raises(HTTPException, match="도구 사용과 명시적 추론 강도"):
            completions._validate_tool_reasoning_compatibility("high", resolved, tools_enabled)

    def test_gpt5_explicit_reasoning_is_allowed_when_tools_are_disabled(self):
        resolved = {"provider_type": "openai", "model_name": "gpt-5.6-luna"}
        tools_disabled = ChatFeatureOptions(tool_policy={"mode": "none"})

        completions._validate_tool_reasoning_compatibility("high", resolved, tools_disabled)


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
            completions._require_execution_capability(features, resolved)

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

        completions._require_execution_capability(features, resolved)

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

        completions._require_execution_capability(features, resolved, feature_routes=routes)

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
            completions._require_execution_capability(features, resolved)

    def test_accepts_priced_text_when_legacy_override_has_no_canonical_gates(self):
        completions._require_execution_capability(
            ChatFeatureOptions(),
            {"input_price_per_token": "0.000001", "output_price_per_token": "0.000002", "capabilities": {}},
        )

    def test_rejects_unpriced_text_execution(self):
        with pytest.raises(HTTPException, match="text"):
            completions._require_execution_capability(ChatFeatureOptions(), {"capabilities": {}})

    def test_accepts_manual_memory_until_semantic_retrieval_is_enabled(self):
        completions._require_execution_capability(
            ChatFeatureOptions(memory=True),
            {"input_price_per_token": "0.000001", "output_price_per_token": "0.000002", "capabilities": {}},
        )

    def test_rejects_unavailable_input_modality(self):
        image = UserAssetInputPart(type="image", asset_id="asset-1")
        with pytest.raises(HTTPException, match="image_input"):
            completions._require_execution_capability(
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
        monkeypatch.setattr(chat_admission.ms, "active_contents_for_run", fail_memory_lookup)

        workspace, memories = await completions._load_context({"workspace_id": 7}, "u1", "p1", include_memory=False)

        assert workspace == "workspace rule"
        assert memories == []

    def test_run_snapshot_excludes_provider_secret(self):
        capabilities, pricing = completions._run_snapshots(
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

        routes = await completions._resolve_feature_routes(
            ChatFeatureOptions(
                web_search={"enabled": True, "provider_id": 7},
                advisor={"enabled": True, "model_id": 9},
            )
        )

        assert routes == {"search": search_route, "advisor": advisor_route}

    async def test_rejects_unavailable_selected_feature_route(self, monkeypatch):
        monkeypatch.setattr(chat_admission.ps, "get_active_provider_route", lambda provider_id: _return(None))
        with pytest.raises(HTTPException, match="selected web search provider"):
            await completions._resolve_feature_routes(
                ChatFeatureOptions(web_search={"enabled": True, "provider_id": 7})
            )

    async def test_enabled_manual_memory_is_loaded(self, monkeypatch):
        captured = {}

        async def load_memory(**kwargs):
            captured.update(kwargs)
            return ["사용자는 Python을 선호합니다."]

        monkeypatch.setattr(chat_admission.ws, "get_instructions_for_run", lambda *_args, **_kwargs: _return(None))
        monkeypatch.setattr(chat_admission.ms, "active_contents_for_run", load_memory)

        workspace, memories = await completions._load_context({"workspace_id": None}, "u1", "p1", include_memory=True)

        assert workspace is None
        assert memories == ["사용자는 Python을 선호합니다."]
        assert captured == {"user_id": "u1", "project_id": "p1", "workspace_id": None}


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
            completions,
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

        async def unavailable_model(*_args, **_kwargs):
            raise AssertionError("retry must not resolve a mutable model route")

        monkeypatch.setattr(completions, "_resolve_model", unavailable_model)

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
            completions,
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
            completions,
            "_resolve_extension_selection",
            lambda agent, features, *, user_id, project_id: _return({"tools": [], "mcp": [mcp_item]}),
        )

        async def fake_list_for_user(kind, **kwargs):
            if kind == "skill":
                return [{"id": 3, "name": "skill-3", "instructions": "skill-3-instructions"}]
            return []

        monkeypatch.setattr(chat_admission.es, "list_for_user", fake_list_for_user)
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

        async def quota_exceeded(user_id, project_id=None):
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
