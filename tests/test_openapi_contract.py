"""OpenAPI schema, security alternatives, typed public responses, and startup propagation tests."""

from __future__ import annotations

import asyncio
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import Depends, FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from keystoneclient.exceptions import Forbidden, NotFound, Unauthorized

from lumen import auth as deps
from lumen.config import Settings
from lumen.main import HealthResponse, RootDiscoveryResponse, VersionDiscoveryResponse, app, lifespan
from lumen.models.chat_contracts import ChatRunDescriptor
from lumen.models.chat_runs import ChatRun
from lumen.services.durable_runs.common import descriptor


class TestOpenAPIContract:
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
        assert schema["paths"]["/v1/policy/capabilities"]["get"]["security"] == conv_path["security"]

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
        root_links = {link.rel: link.href for link in root_data.versions[0].links}
        assert root_links["models"] == "http://test/v1/models"

        resp_v1 = await client.get("/v1/")
        assert resp_v1.status_code == 200
        v1_data = VersionDiscoveryResponse.model_validate(resp_v1.json())
        assert v1_data.version.id == "v1.0"
        v1_links = {link.rel: link.href for link in v1_data.version.links}
        assert v1_links["models"] == "http://test/v1/models"

        resp_health = await client.get("/v1/health")
        assert resp_health.status_code == 200
        health_data = HealthResponse.model_validate(resp_health.json())
        assert health_data.status == "ok"

    async def test_version_discovery_models_link_uses_configured_public_origin(self, client, monkeypatch):
        monkeypatch.setattr(
            "lumen.main.get_settings",
            lambda: SimpleNamespace(public_api_base="https://lumen.example/base/"),
        )

        for path in ("/", "/v1/"):
            response = await client.get(path)
            assert response.status_code == 200
            payload = response.json()
            version = payload["versions"][0] if path == "/" else payload["version"]
            links = {link["rel"]: link["href"] for link in version["links"]}
            assert links["models"] == "https://lumen.example/base/v1/models"

    def test_openapi_contract_version_and_profiles(self):
        schema = app.openapi()
        assert schema.get("x-contract-version") == "1.0.0"
        profiles = schema.get("x-profiles", {})
        assert "openai_lumen" in profiles
        assert "durable completion" in profiles["openai_lumen"]
        assert "openai_stateless" in profiles
        assert "stateless provider" in profiles["openai_stateless"]
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

    def test_openapi_documents_batch_and_file_routes_with_scopes_and_bodies(self):
        schema = app.openapi()
        paths = schema["paths"]
        schemas = schema["components"]["schemas"]
        api_key_only = [{"APIKeyBearer": []}, {"XApiKey": []}]
        expected = {
            ("/v1/chat/batches", "post"): "native:batches:write",
            ("/v1/chat/batches", "get"): "native:batches:read",
            ("/v1/chat/batches/{batch_id}", "get"): "native:batches:read",
            ("/v1/chat/batches/{batch_id}/items", "get"): "native:batches:read",
            ("/v1/chat/batches/{batch_id}/cancel", "post"): "native:batches:write",
            ("/v1/files", "post"): "compat:files:write",
            ("/v1/files", "get"): "compat:files:read",
            ("/v1/files/{file_id}", "get"): "compat:files:read",
            ("/v1/files/{file_id}", "delete"): "compat:files:write",
            ("/v1/files/{file_id}/content", "get"): "compat:files:read",
            ("/v1/batches", "post"): "compat:batches:write",
            ("/v1/batches", "get"): "compat:batches:read",
            ("/v1/batches/{batch_id}", "get"): "compat:batches:read",
            ("/v1/batches/{batch_id}/cancel", "post"): "compat:batches:write",
        }
        for (path, method), scope in expected.items():
            operation = paths[path][method]
            assert operation["x-required-api-key-scopes"] == [scope], (path, method)
            if path.startswith(("/v1/files", "/v1/batches")):
                assert operation["security"] == api_key_only, (path, method)
            else:
                assert {"KeystoneToken": []} in operation["security"], (path, method)

        native_create = paths["/v1/chat/batches"]["post"]
        assert native_create["requestBody"]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/NativeBatchCreateRequest"
        }
        assert any(param["name"] == "Idempotency-Key" and param["required"] for param in native_create["parameters"])
        assert native_create["x-conditional-api-key-scopes"]["operation images.edits"] == [
            "native:images:write", "native:assets:read",
        ]
        assert "202" in native_create["responses"]
        compat_create = paths["/v1/batches"]["post"]
        assert compat_create["requestBody"]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/OpenAIBatchCreateRequest"
        }
        assert compat_create["x-conditional-api-key-scopes"]["endpoint /v1/images/generations"] == ["compat:images:write"]
        assert "multipart/form-data" in paths["/v1/files"]["post"]["requestBody"]["content"]
        for name in ("NativeBatchCreateRequest", "NativeBatchItemRequest", "OpenAIBatchCreateRequest",
                     "OutputExpiresAfter", "NativeBatchDescriptor", "NativeBatchPage", "NativeBatchItemPage",
                     "OpenAIBatchObject", "OpenAIBatchList", "OpenAIFileObject", "OpenAIFileList", "OpenAIFileDeleted"):
            assert name in schemas, name
        batch_object = schemas["OpenAIBatchObject"]["properties"]
        assert batch_object["created_at"]["type"] == "integer"
        assert {"lumen_batch", "openai_batch"} <= set(schema["x-profiles"])


def original_token(monkeypatch, *, project_id="p1", user_id="u1", username="user1", role_names=None,
                   methods=None, metadata=None):
    """Return the SDK token-inspection body; AccessInfo parsing stays real."""
    data = {
        "methods": ["password"] if methods is None else methods,
        "user": {"id": user_id, "name": username, "domain": {"id": "user-domain", "name": "Users"}},
        "roles": [{"id": name, "name": name} for name in (["member"] if role_names is None else role_names)],
        "issued_at": "2026-01-01T00:00:00Z", "expires_at": "2099-01-01T00:00:00Z",
    }
    if project_id is not None:
        data["project"] = {"id": project_id, "name": project_id,
                           "domain": {"id": "project-domain", "name": "Projects"}}
    data.update(metadata or {})
    body = {"token": data}
    inspect = Mock(return_value=body)
    monkeypatch.setattr(deps, "_get_admin_ks_client", lambda: SimpleNamespace(
        tokens=SimpleNamespace(get_token_data=inspect)))
    monkeypatch.setattr("keystoneauth1.identity.v3.Token", Mock(
        side_effect=AssertionError("Original inspection must not exchange the caller token")))
    monkeypatch.setattr(deps, "_is_system_admin", lambda user_id: False)
    return body, inspect


@pytest.fixture
def current_authority(monkeypatch):
    async def resolve(user_id, project_id):
        return {"roles": ["member"], "is_system_admin": user_id == "sys-admin-1"}
    monkeypatch.setattr(deps, "resolve_project_authority", resolve)


@pytest.mark.parametrize("dependency", [deps.get_principal, deps.require_token])
async def test_slow_keystone_validation_does_not_block_public_requests(dependency, monkeypatch, current_authority):
    started, release = Event(), Event()

    def validate(*_args, **_kwargs):
        started.set()
        release.wait(5)
        return {
            "user_id": "user-1",
            "username": "user",
            "project_id": "project-1",
            "connection_project_id": "project-1",
            "roles": ["member"],
            "is_system_admin": False,
        }

    monkeypatch.setattr(deps, "validate_token", validate)
    application = FastAPI()

    @application.get("/protected")
    async def protected(_principal=Depends(dependency)):
        return {"authenticated": True}

    @application.get("/health")
    async def health():
        return {"status": "ok"}

    async with AsyncClient(transport=ASGITransport(app=application), base_url="http://local") as client:
        pending = asyncio.create_task(client.get("/protected", headers={"X-Auth-Token": "caller-token"}))
        try:
            assert await asyncio.to_thread(started.wait, 5)
            response = await client.get("/health")
            assert response.status_code == 200
            assert not pending.done(), "Public requests must complete before Keystone validation finishes"
        finally:
            release.set()
            authenticated = await pending
        assert authenticated.status_code == 200


class TestKeystoneProjectScoping:
    def test_scoped_keystone_token_success_and_rescope(self, monkeypatch):
        _, inspect = original_token(monkeypatch, project_id="home", user_id="usr-123", username="alice",
                                    role_names=["member", "admin"])
        exchanged = SimpleNamespace(auth_token="rescoped-token-789", project_id="proj-456", user_id="usr-123",
                                    system_scoped=False)
        plugin = SimpleNamespace(get_access=Mock(return_value=exchanged))
        exchange = Mock(return_value=plugin)
        session = SimpleNamespace(session=SimpleNamespace(close=Mock()))
        monkeypatch.setattr("keystoneauth1.identity.v3.Token", exchange)
        monkeypatch.setattr("keystoneauth1.session.Session", Mock(return_value=session))

        info = deps.validate_token("submitted-raw-token", project_id="proj-456")
        assert info["user_id"] == "usr-123"
        assert info["username"] == "alice"
        assert info["project_id"] == "proj-456"
        assert info["roles"] == ["member", "admin"]
        assert info["auth_token"] == "rescoped-token-789"
        inspect.assert_called_once_with("submitted-raw-token", include_catalog=False)
        assert exchange.call_args.kwargs["token"] == "submitted-raw-token"
        assert exchange.call_args.kwargs["project_id"] == "proj-456"
        session.session.close.assert_called_once_with()
        assert info["source_project_id"] == "home"

    async def test_scoped_keystone_token_propagation_in_get_principal_and_require_token(
        self, monkeypatch, current_project_authority
    ):
        _, inspect = original_token(monkeypatch, project_id="proj-777", user_id="usr-555", username="bob")

        from starlette.requests import Request

        request = Request(
            {
                "type": "http",
                "headers": [
                    (b"x-auth-token", b"submitted-token-111"),
                    (b"x-project-id", b"proj-777"),
                ],
            }
        )

        principal = await deps.get_principal(request)
        assert principal["auth_type"] == "keystone"
        assert principal["user_id"] == "usr-555"
        assert principal["project_id"] == "proj-777"
        assert principal["token"] == "submitted-token-111"

        req_info = await deps.require_token(x_auth_token="submitted-token-111", bearer=None, x_project_id="proj-777")
        assert req_info["token"] == "submitted-token-111"
        assert req_info["project_id"] == "proj-777"
        assert inspect.call_count == 2
        inspect.assert_called_with("submitted-token-111", include_catalog=False)

    def test_unscoped_keystone_token_fails_closed(self, monkeypatch):
        original_token(monkeypatch, project_id=None)

        with pytest.raises(HTTPException) as ei:
            deps.validate_token("unscoped-token")
        assert ei.value.status_code == 401
        assert "Project-scoped" in ei.value.detail

    @pytest.mark.parametrize("failure,status", [(Unauthorized("invalid"), 401), (NotFound("expired"), 401),
                                               (Forbidden("revoked"), 401), (RuntimeError("outage"), 503)])
    def test_keystone_token_validation_failure(self, monkeypatch, failure, status):
        _, inspect = original_token(monkeypatch)
        inspect.side_effect = failure
        with pytest.raises(HTTPException) as error:
            deps.validate_token("invalid-token")
        assert error.value.status_code == status

    async def test_api_key_isolation_never_invokes_keystone(self, monkeypatch):
        def forbidden_keystone(**kwargs):
            raise AssertionError("Keystone v3.Token should never be instantiated for API key authentication")

        monkeypatch.setattr("keystoneauth1.identity.v3.Token", forbidden_keystone)
        monkeypatch.setattr(deps, "_get_admin_ks_client", forbidden_keystone)

        async def fake_verify_key(key: str):
            if key == "sk-afgl-secret123":
                return {
                    "user_id": "api-usr-1",
                    "project_id": "api-proj-1",
                    "api_key_id": 42,
                    "scopes": ("read", "write"),
                }
            return None

        monkeypatch.setattr("lumen.services.api_key_store.verify_key", fake_verify_key)

        from starlette.requests import Request

        request = Request(
            {
                "type": "http",
                "headers": [
                    (b"authorization", b"Bearer sk-afgl-secret123"),
                    (b"x-project-id", b"api-proj-1"),
                ],
            }
        )

        principal = await deps.get_principal(request)
        assert principal["auth_type"] == "api_key"
        assert principal["user_id"] == "api-usr-1"
        assert principal["project_id"] == "api-proj-1"
        assert principal["api_key_id"] == 42
        assert principal["scopes"] == ("read", "write")

    async def test_keystone_bearer_token_support(self, monkeypatch, current_authority):
        monkeypatch.setattr(
            deps,
            "validate_token",
            lambda tok, project_id="": {"user_id": "u1", "project_id": "p1", "auth_token": tok},
        )
        info = await deps.require_token(
            x_auth_token=None,
            bearer=deps.HTTPAuthorizationCredentials(scheme="Bearer", credentials="tok123"),
            x_project_id="p1",
        )
        assert info["token"] == "tok123"
        assert info["project_id"] == "p1"

    async def test_system_admin_foreign_target_project_success(self, monkeypatch, current_authority):
        original_token(monkeypatch, project_id="home-proj", user_id="sys-admin-1", username="admin_user",
                       role_names=["admin"])
        monkeypatch.setattr(deps, "_is_system_admin", lambda user_id: True)

        from starlette.requests import Request

        request = Request(
            {
                "type": "http",
                "headers": [
                    (b"x-auth-token", b"admin-home-token"),
                    (b"x-project-id", b"home-proj"),
                    (b"x-target-project-id", b"foreign-proj"),
                ],
            }
        )

        principal = await deps.get_principal(request)
        assert principal["auth_type"] == "keystone"
        assert principal["user_id"] == "sys-admin-1"
        assert principal["project_id"] == "foreign-proj"
        assert principal["connection_project_id"] == "home-proj"
        assert principal["token"] == "admin-home-token"
        assert principal["is_system_admin"] is True

        req_info = await deps.require_token(
            x_auth_token="admin-home-token",
            bearer=None,
            x_project_id="home-proj",
            x_target_project_id="foreign-proj",
        )
        assert req_info["project_id"] == "foreign-proj"
        assert req_info["connection_project_id"] == "home-proj"
        assert req_info["token"] == "admin-home-token"

    async def test_non_admin_foreign_target_project_rejected(self, monkeypatch):
        original_token(monkeypatch, project_id="home-proj", user_id="regular-user-1")

        from starlette.requests import Request

        request = Request(
            {
                "type": "http",
                "headers": [
                    (b"x-auth-token", b"user-home-token"),
                    (b"x-project-id", b"home-proj"),
                    (b"x-target-project-id", b"foreign-proj"),
                ],
            }
        )

        with pytest.raises(HTTPException) as ei:
            await deps.get_principal(request)
        assert ei.value.status_code == 403

        with pytest.raises(HTTPException) as ei:
            await deps.require_token(
                x_auth_token="user-home-token",
                bearer=None,
                x_project_id="home-proj",
                x_target_project_id="foreign-proj",
            )
        assert ei.value.status_code == 403

    async def test_system_admin_same_target_project_success(self, monkeypatch, current_authority):
        original_token(monkeypatch, project_id="home-proj", user_id="sys-admin-1", username="admin_user",
                       role_names=["admin"])
        monkeypatch.setattr(deps, "_is_system_admin", lambda user_id: True)

        from starlette.requests import Request

        request = Request(
            {
                "type": "http",
                "headers": [
                    (b"x-auth-token", b"admin-home-token"),
                    (b"x-project-id", b"home-proj"),
                    (b"x-target-project-id", b"home-proj"),
                ],
            }
        )

        principal = await deps.get_principal(request)
        assert principal["project_id"] == "home-proj"
        assert principal["connection_project_id"] == "home-proj"

    async def test_api_key_target_project_mismatch_rejected(self, monkeypatch):
        async def fake_verify_key(key: str):
            if key == "sk-afgl-secret123":
                return {
                    "user_id": "api-usr-1",
                    "project_id": "api-proj-1",
                    "api_key_id": 42,
                    "scopes": ("read", "write"),
                }
            return None

        monkeypatch.setattr("lumen.services.api_key_store.verify_key", fake_verify_key)

        from starlette.requests import Request

        request = Request(
            {
                "type": "http",
                "headers": [
                    (b"authorization", b"Bearer sk-afgl-secret123"),
                    (b"x-target-project-id", b"foreign-proj"),
                ],
            }
        )

        with pytest.raises(HTTPException) as ei:
            await deps.get_principal(request)
        assert ei.value.status_code == 403
        assert "API 키 프로젝트와 X-Target-Project-Id가 일치하지 않습니다" in ei.value.detail

    @pytest.mark.parametrize(("connection_project", "system_scope"), [
        ("home-connection-proj", ""), ("", "all"), ("", ""),
    ], ids=["project", "system", "unscoped"])
    async def test_get_os_conn_uses_connection_project_id(self, monkeypatch, connection_project, system_scope):
        captured_kwargs = {}

        def fake_connect(**kwargs):
            captured_kwargs.update(kwargs)

            class FakeConn:
                def close(self):
                    pass

            return FakeConn()

        monkeypatch.setattr("openstack.connect", fake_connect)

        token_info = {
            "token": "effective-ks-token",
            "project_id": "foreign-logical-proj",
            "connection_project_id": connection_project,
            "connection_system_scope": system_scope,
            "is_system_admin": True,
        }

        conn_gen = deps.get_os_conn(token_info=token_info)
        await conn_gen.__anext__()
        assert captured_kwargs["project_id"] == (connection_project or None)
        assert captured_kwargs["system_scope"] == (system_scope or None)
        assert captured_kwargs["load_yaml_config"] is False
        assert captured_kwargs["load_envvars"] is False
        assert captured_kwargs["token"] == "effective-ks-token"
        try:
            await conn_gen.__anext__()
        except StopAsyncIteration:
            pass


@pytest.mark.parametrize("dependency", [deps.get_principal, deps.require_token])
@pytest.mark.parametrize("lookup_status", [None, 403, 503])
async def test_foreign_target_requires_admin_at_final_authority_lookup(monkeypatch, dependency, lookup_status):
    """An early admin result cannot survive revocation before target lookup."""
    from starlette.requests import Request

    monkeypatch.setattr(deps, "validate_token", lambda *args, **kwargs: {
        "user_id": "former-admin", "project_id": "target", "connection_project_id": "home",
        "roles": ["admin"], "is_system_admin": True, "auth_token": "validated-caller-token",
    })

    async def current(user_id, project_id):
        assert (user_id, project_id) == ("former-admin", "target")
        if lookup_status is not None:
            raise HTTPException(status_code=lookup_status, detail="Current authority denied/unavailable")
        # Even retained, entitled target membership cannot authorize the admin-only header.
        return {"roles": ["member", "lumen-chat_user"], "is_system_admin": False}

    monkeypatch.setattr(deps, "resolve_project_authority", current)
    with pytest.raises(HTTPException) as exc:
        if dependency is deps.get_principal:
            await dependency(Request({"type": "http", "headers": [
                (b"x-auth-token", b"caller-token"), (b"x-project-id", b"home"),
                (b"x-target-project-id", b"target"),
            ]}))
        else:
            await dependency(x_auth_token="caller-token", bearer=None,
                             x_project_id="home", x_target_project_id="target")
    assert exc.value.status_code == (lookup_status or 403)


def test_system_admin_lookup_outage_is_not_a_negative_assignment(monkeypatch):
    def unavailable():
        raise RuntimeError("Synthetic directory outage")

    monkeypatch.setattr(deps, "_get_admin_ks_client", unavailable)
    with pytest.raises(HTTPException) as exc:
        deps._is_system_admin("admin-user")
    assert exc.value.status_code == 503


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


@pytest.mark.parametrize("project_selector", ["", "p1"])
def test_original_project_inspection_does_not_exchange_token(monkeypatch, project_selector):
    _, inspect = original_token(monkeypatch)
    info = deps.validate_token("original-token", project_id=project_selector)
    inspect.assert_called_once_with("original-token", include_catalog=False)
    assert info["auth_token"] == "original-token"
    assert info["source_project_id"] == info["connection_project_id"] == info["project_id"] == "p1"
    assert info["credential_restricted"] is False


@pytest.mark.parametrize("methods,metadata", [
    ([], {}), (["unknown"], {}), (["password", "unknown"], {}),
    (["application_credential"], {"application_credential": {"id": "app-1"}}),
    (["token"], {"application_credential": {}}),
    (["token"], {"OS-TRUST:trust": {"id": "trust-1"}}),
    (["token"], {"OS-OAUTH1:access_token": {"id": "oauth-1"}}),
    (["token"], {"OS-OAUTH2": {}}), (["token"], {"oauth2_credential": {}}),
])
def test_original_delegation_or_unknown_method_cannot_become_human_authority(monkeypatch, methods, metadata):
    original_token(monkeypatch, methods=methods, metadata=metadata)
    admin = Mock(side_effect=AssertionError("Reduced originals cannot use human system authority"))
    monkeypatch.setattr(deps, "_is_system_admin", admin)
    info = deps.validate_token("original-token", project_id="p1")
    assert info["credential_restricted"] is True and info["is_system_admin"] is False
    assert info["issued_role_ids"] == ("member",)
    for selectors in ({"project_id": "foreign"}, {"target_project_id": "foreign"}):
        with pytest.raises(HTTPException) as error:
            deps.validate_token("original-token", **selectors)
        assert error.value.status_code == 403
    admin.assert_not_called()


@pytest.mark.parametrize("method", ["password", "token", "totp", "mapped", "external", "oidc", "saml2"])
def test_supported_original_human_methods_remain_human(monkeypatch, method):
    original_token(monkeypatch, methods=[method])
    admin = Mock(return_value=True)
    monkeypatch.setattr(deps, "_is_system_admin", admin)
    info = deps.validate_token("original-token", target_project_id="target")
    assert info["credential_restricted"] is False and info["is_system_admin"] is True
    assert info["project_id"] == "target" and info["connection_project_id"] == "p1"
    admin.assert_called_once_with("u1")


@pytest.mark.parametrize("metadata", [
    {"methods": "password"}, {"methods": [None]}, {"application_credential": ["invalid"]},
    {"roles": "member"}, {"roles": [{"name": "member"}]}, {"roles": [{"id": ""}]},
    {"user": {"id": "", "name": "invalid"}}, {"project": "p1"},
    {"system": {"all": True}},
])
def test_malformed_original_metadata_is_503_not_invalid_token_or_human_fallback(monkeypatch, metadata):
    original_token(monkeypatch, metadata=metadata)
    with pytest.raises(HTTPException) as error:
        deps.validate_token("original-token")
    assert error.value.status_code == 503


@pytest.mark.parametrize("metadata", [
    {"bind": {"kerberos": "subject"}}, {"access_rules": [{"path": "/v1/*"}]},
    {"application_credential": {"id": "app-1", "access_rules": [{"path": "/v1/*"}]}},
])
def test_original_native_restrictions_not_enforced_by_lumen_are_rejected(monkeypatch, metadata):
    original_token(monkeypatch, metadata=metadata)
    with pytest.raises(HTTPException) as error:
        deps.validate_token("original-token")
    assert error.value.status_code == 403


@pytest.mark.parametrize("projectless_scope", [{}, {"system": {"all": True}}], ids=["unscoped", "system"])
def test_original_projectless_human_admin_preserves_connection_scope(monkeypatch, projectless_scope):
    original_token(monkeypatch, project_id=None, metadata=projectless_scope)
    monkeypatch.setattr(deps, "_is_system_admin", lambda user_id: True)
    info = deps.validate_token("original-token", target_project_id="target")
    assert info["connection_project_id"] == info["source_project_id"] == ""
    assert info["connection_system_scope"] == info["source_system_scope"] == ("all" if projectless_scope else "")
    assert info["project_id"] == "target" and info["auth_token"] == "original-token"


@pytest.mark.parametrize("changed", [{"user_id": "foreign", "project_id": "target"},
                                     {"user_id": "u1", "project_id": "foreign"}, None])
def test_explicit_human_rescope_rejects_foreign_identity_and_closes_session(monkeypatch, changed):
    original_token(monkeypatch)
    access = Mock(side_effect=RuntimeError("rescope failed")) if changed is None else Mock(
        return_value=SimpleNamespace(**changed))
    monkeypatch.setattr("keystoneauth1.identity.v3.Token", Mock(return_value=SimpleNamespace(get_access=access)))
    session = SimpleNamespace(session=SimpleNamespace(close=Mock()))
    monkeypatch.setattr("keystoneauth1.session.Session", Mock(return_value=session))
    with pytest.raises(HTTPException) as error:
        deps.validate_token("original-token", project_id="target")
    assert error.value.status_code == (401 if changed is None else 403)
    session.session.close.assert_called_once_with()


@pytest.mark.parametrize("dependency", [deps.get_principal, deps.require_token])
async def test_both_dependencies_keep_original_restriction_ceiling_against_larger_owner(monkeypatch, dependency):
    from starlette.requests import Request

    _, inspect = original_token(monkeypatch, role_names=["member", "lumen-chat_user"],
                                methods=["token"], metadata={"application_credential": {"id": "app-1"}})
    rows = [{"user": {"id": "u1"}, "scope": {"project": {"id": "p1"}}, "role": {"id": rid}}
            for rid in ("member", "lumen-chat_user", "lumen-tools_user")]
    def get(path):
        if path == "/roles":
            key, values = "roles", [{"id": rid, "name": rid, "domain_id": None}
                                     for rid in ("member", "lumen-chat_user", "lumen-tools_user", "admin")]
        elif path == "/role_inferences":
            key, values = "role_inferences", []
        else:
            assert path == "/role_assignments?user.id=u1&scope.project.id=p1&effective=True"
            key, values = "role_assignments", rows
        return None, {key: values, "links": {}, "truncated": False}
    ks = SimpleNamespace(
        tokens=SimpleNamespace(get_token_data=inspect), get=get,
        users=SimpleNamespace(get=lambda identity: SimpleNamespace(id="u1", domain_id="user-domain", enabled=True)),
        projects=SimpleNamespace(get=lambda identity: SimpleNamespace(id="p1", domain_id="project-domain", enabled=True)),
        domains=SimpleNamespace(get=lambda identity: SimpleNamespace(id=identity, enabled=True)),
    )
    monkeypatch.setattr(deps, "_get_admin_ks_client", lambda: ks)
    monkeypatch.setattr(deps, "resolve_project_authority", Mock(
        side_effect=AssertionError("Reduced credentials must use the issued-ID resolver")))
    if dependency is deps.get_principal:
        info = await dependency(Request({"type": "http", "headers": [(b"x-auth-token", b"original-token")]}))
    else:
        info = await dependency(x_auth_token="original-token", bearer=None, x_project_id=None)
    assert info["credential_restricted"] is True and info["is_system_admin"] is False
    assert info["source_project_id"] == info["connection_project_id"] == info["project_id"] == "p1"
    assert info["issued_role_ids"] == ("lumen-chat_user", "member")
    assert set(info["roles"]) == {"member", "lumen-chat_user"}
    assert info["token"] == "original-token"
    deps.ensure_scopes(info, "compat:completions:write")
    with pytest.raises(HTTPException) as error:
        deps.ensure_scopes(info, "native:tools:execute")
    assert error.value.status_code == 403
    inspect.assert_called_once_with("original-token", include_catalog=False)
