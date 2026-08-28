"""OpenAPI schema, security alternatives, typed public responses, and startup propagation tests."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from lumen import auth as deps
from lumen.config import Settings
from lumen.main import HealthResponse, RootDiscoveryResponse, VersionDiscoveryResponse, app, lifespan
from lumen.models.chat_contracts import ChatRunDescriptor
from lumen.models.chat_runs import ChatRun
from lumen.services.durable_runs.common import descriptor


class TestOpenAPIContract:
    def test_app_title_description_version(self):
        assert app.title == "Lumen"
        assert app.description == "Lumen durable agent, LLM, and chat service API"
        assert app.version == "1.0.0"

    def test_openapi_security_schemes_and_alternatives(self):
        schema = app.openapi()
        schemes = schema["components"]["securitySchemes"]
        assert "KeystoneToken" in schemes
        assert "KeystoneBearer" in schemes
        assert "APIKeyBearer" in schemes
        assert "XApiKey" in schemes
        assert "description" in schemes["APIKeyBearer"]
        assert "description" in schemes["XApiKey"]

        # Native routes accept Keystone or scoped API-key principals.
        conv_path = schema["paths"]["/v1/conversations"]["get"]
        assert conv_path["security"] == [
            {"KeystoneToken": []},
            {"KeystoneBearer": []},
            {"APIKeyBearer": []},
            {"XApiKey": []},
        ]

        # Verify AI compat endpoints have APIKeyBearer OR XApiKey alternatives
        compat_models_path = schema["paths"]["/v1/models"]["get"]
        assert compat_models_path["security"] == [{"APIKeyBearer": []}, {"XApiKey": []}]

    def test_unique_operation_ids_and_routes(self):
        schema = app.openapi()
        op_ids = []
        path_methods = []
        for path, item in schema["paths"].items():
            for method, op in item.items():
                if method in ("get", "post", "put", "patch", "delete"):
                    pm = f"{method.upper()} {path}"
                    assert pm not in path_methods, f"Duplicate method+path: {pm}"
                    path_methods.append(pm)
                    op_id = op.get("operationId")
                    assert op_id, f"Missing operationId for {pm}"
                    assert op_id not in op_ids, f"Duplicate operationId: {op_id}"
                    op_ids.append(op_id)

    def test_canonical_vs_compat_model_paths(self):
        schema = app.openapi()
        assert "/v1/chat/models" in schema["paths"], "Canonical /v1/chat/models missing"
        assert "/v1/models" in schema["paths"], "Compat /v1/models missing"

    async def test_typed_discovery_and_health_responses(self, client):
        resp_root = await client.get("/")
        assert resp_root.status_code == 200
        root_data = RootDiscoveryResponse.model_validate(resp_root.json())
        assert root_data.versions[0].id == "v1.0"

        resp_v1 = await client.get("/v1/")
        assert resp_v1.status_code == 200
        v1_data = VersionDiscoveryResponse.model_validate(resp_v1.json())
        assert v1_data.version.id == "v1.0"

        resp_health = await client.get("/v1/health")
        assert resp_health.status_code == 200
        health_data = HealthResponse.model_validate(resp_health.json())
        assert health_data.status == "ok"

    def test_openapi_contract_version_and_profiles(self):
        schema = app.openapi()
        assert schema.get("x-contract-version") == "1.0.0"
        profiles = schema.get("x-profiles", {})
        assert "openai_stateless" in profiles
        assert "anthropic_stateless" in profiles
        assert "lumen_native" in profiles

    def test_openapi_vendor_extensions_scopes_and_sse(self):
        schema = app.openapi()
        openai_op = schema["paths"]["/v1/chat/completions"]["post"]
        assert openai_op["x-required-api-key-scopes"] == ["compat:completions:write"]
        assert "text/event-stream" in openai_op["responses"]["200"]["content"]

        native_op = schema["paths"]["/v1/conversations/{conversation_id}/completions"]["post"]
        assert native_op["x-required-api-key-scopes"] == ["native:conversations:write", "native:runs:write"]
        assert "features.memory=true" in native_op["x-conditional-api-key-scopes"]

        events_op = schema["paths"]["/v1/runs/{run_id}/events"]["get"]
        sse_media = events_op["responses"]["200"]["content"]["text/event-stream"]
        assert sse_media["schema"] == {"type": "string", "format": "event-stream"}
        assert sse_media["x-event-data-schema"]["$ref"] == "#/components/schemas/ChatRunEvent"

    def test_openapi_typed_response_models(self):
        schema = app.openapi()
        schemas = schema["components"]["schemas"]
        assert "ChatRunEvent" in schemas
        assert "ChatRunDescriptor" in schemas
        assert "ChatRunResponse" in schemas
        assert "OpenAIChatResponse" in schemas
        assert "AnthropicMessagesResponse" in schemas
        assert "OpenAIModelListResponse" in schemas
        assert "UsageRecordPage" in schemas
        assert "properties" in schemas["OpenAIChatResponse"]
        assert "properties" in schemas["AnthropicMessagesResponse"]

        def _check_refs(obj: dict | list | str) -> None:
            if isinstance(obj, dict):
                for k, v in obj.items():
                    if k == "$ref" and isinstance(v, str):
                        assert not v.startswith("#/$defs/"), f"Unresolved $defs ref: {v}"
                        ref_target = v.replace("#/components/schemas/", "")
                        assert ref_target in schemas, f"Missing schema for ref: {v}"
                    else:
                        _check_refs(v)
            elif isinstance(obj, list):
                for item in obj:
                    _check_refs(item)

        _check_refs(schema)


class TestKeystoneProjectScoping:
    def test_unscoped_keystone_token_fails_closed(self, monkeypatch):
        class FakeToken:
            def get_token_data(self, sess):
                return {
                    "token": {
                        "user": {"id": "u1", "name": "user1"},
                        "roles": [{"name": "member"}],
                        "project": None,
                        # project missing -> unscoped
                    }
                }

        monkeypatch.setattr("keystoneauth1.identity.v3.Token", lambda **kwargs: FakeToken())
        monkeypatch.setattr("keystoneauth1.session.Session", lambda **kwargs: None)

        with pytest.raises(HTTPException) as ei:
            deps.validate_token("unscoped-token")
        assert ei.value.status_code == 401
        assert "Project-scoped" in ei.value.detail

    async def test_keystone_bearer_token_support(self, monkeypatch):
        monkeypatch.setattr(
            deps,
            "validate_token",
            lambda tok, project_id="": {"user_id": "u1", "project_id": "p1", "token": tok},
        )
        info = await deps.require_token(
            x_auth_token=None,
            bearer=deps.HTTPAuthorizationCredentials(scheme="Bearer", credentials="tok123"),
            x_project_id="p1",
        )
        assert info["token"] == "tok123"
        assert info["project_id"] == "p1"


class TestStartupFailurePropagation:
    async def test_checkpointer_failure_returns_false_propagates_on_startup(self, monkeypatch):
        monkeypatch.setattr(
            "lumen.main.get_settings",
            lambda: Settings(database_url="", chat_checkpointer_postgres_url="postgresql://invalid:5432/db"),
        )

        async def false_start(url):
            return False

        monkeypatch.setattr("lumen.services.checkpointer.chat_checkpointer.start", false_start)

        with pytest.raises(RuntimeError, match="configured chat checkpointer failed to initialize"):
            async with lifespan(app):
                pass

    async def test_semantic_memory_failure_propagates_on_startup(self, monkeypatch):
        monkeypatch.setattr(
            "lumen.main.get_settings",
            lambda: Settings(
                database_url="",
                chat_checkpointer_postgres_url="",
                chat_semantic_memory_enabled=True,
            ),
        )

        async def failing_setup():
            raise RuntimeError("pgvector extension unavailable")

        monkeypatch.setattr("lumen.services.semantic_memory.setup_semantic_memory", failing_setup)

        with pytest.raises(RuntimeError, match="pgvector extension unavailable"):
            async with lifespan(app):
                pass


class TestDurableRunsDescriptorContract:
    def test_descriptor_emits_native_v1_urls(self):
        run = ChatRun(id="run-123", conversation_id="c1", temp_thread_id=None, status="queued")
        desc = descriptor(run)
        assert isinstance(desc, ChatRunDescriptor)
        assert desc.events_url == "/v1/runs/run-123/events"
        assert desc.cancel_url == "/v1/runs/run-123/cancel"
