"""Persisted model-key ownership, live manager checks, and exact HTTP/WS scope gates."""

from contextlib import asynccontextmanager
from itertools import count
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from keystoneclient.exceptions import NotFound
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from starlette.requests import Request
from starlette.websockets import WebSocketDisconnect

from lumen import auth
from lumen.api import api_keys
from lumen.main import app
from lumen.models.chat_db import ChatApiKey, ChatQuotaPolicy, ChatUsageLog, UserWallet
from lumen.service_authority import MODEL_API_KEY_ROUTE_SCOPES, MODEL_API_KEY_SCOPES
from lumen.services import api_key_store as store


@pytest.fixture
def key_db(monkeypatch):
    engine = create_engine("sqlite://")
    for model in (ChatApiKey, ChatQuotaPolicy, ChatUsageLog, UserWallet):
        model.__table__.create(engine)
    next_id = count(100)

    class AsyncKeySession:
        def __init__(self):
            self.session = Session(engine, expire_on_commit=False)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            self.session.close()

        @asynccontextmanager
        async def begin(self):
            with self.session.begin():
                yield

        async def execute(self, statement):
            return self.session.execute(statement)

        async def get(self, model, identity):
            return self.session.get(model, identity)

        def add(self, row):
            if isinstance(row, ChatApiKey) and row.id is None:
                row.id = next(next_id)
            self.session.add(row)

        async def flush(self):
            self.session.flush()

        async def commit(self):
            self.session.commit()

        async def rollback(self):
            self.session.rollback()

    monkeypatch.setattr(store, "get_session_factory", lambda: AsyncKeySession)
    monkeypatch.setattr(store, "is_db_available", lambda: True)
    yield AsyncKeySession
    engine.dispose()


@pytest.fixture
def directory(monkeypatch):
    domains = {name: SimpleNamespace(id=name, enabled=True) for name in ("domain-a", "domain-b")}
    users = {name: SimpleNamespace(id=name, domain_id="domain-a", enabled=True) for name in ("creator", "successor")}
    lookups = []

    def get(table, identity):
        lookups.append(identity)
        if identity not in table:
            raise NotFound()
        return table[identity]

    ks = SimpleNamespace(
        users=SimpleNamespace(get=lambda key: get(users, key)),
        domains=SimpleNamespace(get=lambda key: get(domains, key)),
    )
    monkeypatch.setattr(auth, "_get_admin_ks_client", lambda: ks)
    return SimpleNamespace(domains=domains, users=users, lookups=lookups)


async def _key(key_db, kind, scopes):
    row, raw = store._new_key("test", tuple(scopes), expires_at=None, key_kind=kind, **(
        {"owner_domain_id": "domain-a", "created_by_user_id": "creator"} if kind == "service" else
        {"owner_user_id": "creator", "owner_project_id": "historical-project"}
    ))
    async with key_db() as session, session.begin():
        session.add(row)
        await session.flush()
    return row, raw


def _header(raw):
    return {"Authorization": f"Bearer {raw}"}


@pytest.mark.parametrize("kind", ["personal", "service"])
async def test_key_principal_and_new_io_use_only_the_live_owner(key_db, directory, kind):
    row, raw = await _key(key_db, kind, ["models:read", "native:tools:execute"])
    principal = await store.verify_key(raw)
    expected_storage = auth.model_key_storage_id(row.id)
    assert principal["project_id"] == expected_storage and principal["roles"] == []
    assert principal["credential_class"] == f"{kind}_key"
    assert principal["scopes"] == ("models:read",)
    if kind == "personal":
        assert principal["user_id"] == "creator" and principal["domain_id"] is None
        assert principal["payer"] == auth.Payer("user", "creator", None)
        assert directory.lookups == ["creator", "domain-a"]
    else:
        assert principal["user_id"] == expected_storage and principal["domain_id"] == "domain-a"
        assert principal["payer"] == auth.Payer("domain", None, "domain-a")
        assert directory.lookups == ["domain-a"]
    owner = dict(api_key_id=row.id, user_id=principal["user_id"], project_id=principal["project_id"])
    async with key_db() as session:
        await store.authorize_api_key_in_transaction(session, **owner, required_scopes=("models:read",))
        with pytest.raises(store.ApiKeyForbidden):
            await store.authorize_api_key_in_transaction(session, **owner, required_scopes=("native:tools:execute",))
        with pytest.raises(store.ApiKeyForbidden):
            await store.authorize_api_key_in_transaction(
                session, **{**owner, "project_id": "historical-project"}, required_scopes=("models:read",),
            )
        directory.domains["domain-a"].enabled = False
        assert await store.verify_key(raw) is None
        with pytest.raises(store.ApiKeyForbidden):
            await store.authorize_api_key_in_transaction(session, **owner, required_scopes=("models:read",))
    del directory.domains["domain-a"]
    assert await store.verify_key(raw) is None


async def test_personal_key_ignores_project_headers_and_can_be_managed_from_other_project(key_db, directory, monkeypatch):
    row, raw = await _key(key_db, "personal", ["models:read"])
    monkeypatch.setattr("lumen.api.compat.openai.routing.list_api_models", AsyncMock(return_value=[]))

    def token_info(token, project_id="", target_project_id=""):
        return {"user_id": token, "project_id": project_id or "project-b", "roles": ["member"], "is_system_admin": False}

    monkeypatch.setattr(auth, "validate_token", token_info)
    monkeypatch.setattr(auth, "resolve_project_authority", AsyncMock(return_value={
        "roles": ["member"], "is_system_admin": False, "domain_id": "domain-b",
    }))
    principal = await auth.get_principal(Request({
        "type": "http", "method": "GET", "headers": [(b"x-auth-token", b"creator")],
    }))
    assert principal["credential_class"] == "keystone"
    assert principal["payer"] == auth.Payer("domain", None, "domain-b")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for project_headers in ({}, {"X-Project-Id": "arbitrary", "X-Target-Project-Id": "not-a-project"}):
            assert (await client.get("/v1/models", headers={**_header(raw), **project_headers})).status_code == 200
        session = {"X-Auth-Token": "creator"}
        listing = await client.get("/v1/api-keys", headers=session)
        assert [item["id"] for item in listing.json()] == [row.id]
        assert (await client.post("/v1/api-keys", headers=session, json={"name": "new"})).status_code == 201
        assert (await client.delete(f"/v1/api-keys/{row.id}", headers=session)).status_code == 204
        assert (await client.get("/v1/models", headers=_header(raw))).status_code == 401
    async with key_db() as table:
        assert (await table.get(ChatApiKey, row.id + 1)).owner_project_id is None


async def test_only_current_domain_managers_manage_service_keys(key_db, directory, monkeypatch):
    grants = {("creator", "domain-a"), ("successor", "domain-a"), ("foreign", "domain-b")}
    project_domain = {"project-a": "domain-a", "project-b": "domain-b"}
    monkeypatch.setattr("lumen.api.compat.openai.routing.list_api_models", AsyncMock(return_value=[]))

    def token_info(token, project_id="", target_project_id=""):
        connection = project_id or "project-a"
        return {
            "user_id": token, "project_id": target_project_id or connection,
            "connection_project_id": connection, "roles": ["member"],
            "is_system_admin": token == "sysadmin", "credential_restricted": token == "restricted",
            "source_project_id": connection, "issued_role_ids": (),
        }

    def native_authority(user, project, **kwargs):
        return {"roles": ["member"], "is_system_admin": user == "sysadmin", "domain_id": project_domain[project]}

    async def domain_authority(user, project, *, unrestricted_human, compute_manage, allow_system_admin):
        assert unrestricted_human and compute_manage and not allow_system_admin
        domain = project_domain[project]
        return {"domain_id": domain, "is_member": True, "can_manage": (user, domain) in grants}

    manager_check = AsyncMock(side_effect=domain_authority)
    monkeypatch.setattr(auth, "validate_token", token_info)
    monkeypatch.setattr(auth, "resolve_project_authority", AsyncMock(side_effect=native_authority))
    monkeypatch.setattr(auth, "_resolve_project_authority", native_authority)
    monkeypatch.setattr(api_keys, "resolve_domain_authority", manager_check)

    def session(user, project="project-a"):
        return {"X-Auth-Token": user, "X-Project-Id": project}

    base = "/v1/service-keys"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for user in ("ordinary", "restricted", "sysadmin"):
            assert (await client.get(base, headers=session(user))).status_code == 403
        targeted_admin = {**session("sysadmin"), "X-Target-Project-Id": "project-b"}
        assert (await client.get(base, headers=targeted_admin)).status_code == 403
        owner = session("creator")
        issued = await client.post(base, headers=owner, json={"name": "runner"})
        assert issued.status_code == 201, issued.text
        item = issued.json()
        assert item["owner_domain_id"] == "domain-a" and item["created_by_user_id"] == "creator"
        assert item["owner_user_id"] is None and item["owner_project_id"] is None
        assert item["key_kind"] == "service" and item["key"].startswith("sk-afgl-")
        key_path = f"{base}/{item['id']}"
        assert (await client.get(base, headers=owner)).json()[0].keys() == (item.keys() - {"key"})
        updated = await client.patch(f"{key_path}/limits", headers=owner, json={"monthly_credit_limit": "17"})
        assert updated.status_code == 200 and updated.json()["effective_monthly_credit_limit"] == "17"
        foreign = session("foreign", "project-b")
        assert (await client.get(base, headers=foreign)).json() == []
        assert (await client.delete(key_path, headers=foreign)).status_code == 404
        assert (await client.patch(f"{key_path}/limits", headers=foreign, json={"weekly_credit_limit": "1"})).status_code == 404
        assert (await client.get(base, headers=_header(item["key"]))).status_code == 403
        assert (await client.get("/v1/admin/service-keys", headers=owner)).status_code == 403
        inv = await client.get("/v1/admin/service-keys?owner_domain_id=domain-a", headers=session("sysadmin"))
        assert inv.status_code == 200 and [key["id"] for key in inv.json()["items"]] == [item["id"]]
        assert "key" not in inv.json()["items"][0]
        grants.remove(("creator", "domain-a"))
        for method, path in (("GET", base), ("POST", base), ("PATCH", f"{key_path}/limits"), ("DELETE", key_path)):
            data = {"monthly_credit_limit": "2"} if method == "PATCH" else {}
            assert (await client.request(method, path, headers=owner, json=data)).status_code == 403
        directory.users["creator"].enabled = False
        assert (await client.get("/v1/models", headers=_header(item["key"]))).status_code == 200
        assert (await client.delete(key_path, headers=session("successor"))).status_code == 204
        assert (await client.get("/v1/models", headers=_header(item["key"]))).status_code == 401
    assert manager_check.await_count == 13


@pytest.mark.parametrize("kind", ["personal", "service"])
async def test_exact_model_http_routes_and_scope_attenuation(key_db, directory, kind):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        keys = {}
        for required in MODEL_API_KEY_SCOPES:
            _, keys[required] = await _key(key_db, kind, [required])
        for (method, route), required in MODEL_API_KEY_ROUTE_SCOPES.items():
            if method == "WEBSOCKET":
                continue
            path = route.replace("{model}", "dummy")
            wrong_scope = "models:read" if required != "models:read" else "embeddings:write"
            denied = await client.request(method, path, headers=_header(keys[wrong_scope]), json={})
            admitted = await client.request(method, path, headers=_header(keys[required]), json={})
            assert denied.status_code == 403, (route, denied.text)
            # Empty bodies intentionally reach schema/config errors, not auth.
            assert admitted.status_code not in {401, 403, 405}, (route, admitted.text)


@pytest.mark.parametrize("kind", ["personal", "service"])
async def test_legacy_scopes_cannot_access_nonmodel_routes_or_server_tools(key_db, directory, kind):
    from lumen.service_authority import SCOPE_CAPABILITIES

    _, raw = await _key(key_db, kind, SCOPE_CAPABILITIES)
    headers = _header(raw)
    paths = ("/v1/conversations", "/v1/runs/x", "/v1/memories", "/v1/agents", "/v1/mcp-servers",
             "/v1/custom-tools", "/v1/assets/00000000-0000-0000-0000-000000000000", "/v1/files",
             "/v1/batches", "/v1/chat/batches", "/v1/capabilities", "/v1/policy/capabilities")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for path in paths:
            response = await client.get(path, headers=headers)
            assert response.status_code in {401, 403}, (path, response.text)
        for path in ("/v1/chat/images/generations", "/v1/chat/realtime/sessions", "/v1/temp-completions"):
            response = await client.post(path, headers=headers, json={})
            assert response.status_code == 403, (path, response.text)
        result = await client.post("/v1/chat/completions", headers=headers, json={
            "model": "lumen", "messages": [{"role": "user", "content": "test"}],
            "tools": [{"type": "function", "function": {"name": "server_tool", "parameters": {"type": "object"}}}],
        })
        assert result.status_code == 403 and "native:tools:execute" in result.text


@pytest.mark.parametrize("kind", ["personal", "service"])
async def test_realtime_scope_is_explicit(key_db, directory, kind, monkeypatch):
    # This auth-only socket test substitutes the stored key lookup because TestClient
    # uses a separate thread from the in-memory SQLite connection.
    _, raw = await _key(key_db, kind, ["compat:realtime:write"])
    principal = await store.verify_key(raw)
    monkeypatch.setattr(store, "verify_key", AsyncMock(return_value=principal))
    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect) as admitted, client.websocket_connect("/v1/realtime", headers=_header(raw)) as ws:
        ws.receive_json()
    assert admitted.value.code == 4400  # Auth passed, missing model.
    principal["scopes"] = ("models:read",)
    with pytest.raises(WebSocketDisconnect) as denied:
        with client.websocket_connect("/v1/realtime", headers=_header(raw)):
            pytest.fail("socket accepted without realtime scope")
    assert denied.value.code == 4401


@pytest.mark.parametrize("kind", ["personal", "service"])
async def test_model_keys_receive_compat_images_audio_and_embeddings(key_db, directory, monkeypatch, kind):
    row, raw = await _key(key_db, kind, ["compat:images:write", "compat:audio:write", "embeddings:write"])
    storage_id = auth.model_key_storage_id(row.id)
    actor = {"user_id": "creator" if kind == "personal" else storage_id, "project_id": storage_id}

    def check_actor(api_key_id, **caller):
        assert api_key_id == row.id
        assert {field: caller[field] for field in actor} == actor

    async def admit(_request, *, required_scopes, api_key_id, **caller):
        check_actor(api_key_id, **caller)
        async with key_db() as session:
            await store.authorize_api_key_in_transaction(
                session, api_key_id=api_key_id, required_scopes=required_scopes, **actor,
            )
        return SimpleNamespace(run_id="completed-run")

    monkeypatch.setattr("lumen.api.images.admit_image_run", admit)
    monkeypatch.setattr("lumen.api.audio.admit_audio_run", admit)
    owned = AsyncMock(return_value=SimpleNamespace(status="completed"))
    monkeypatch.setattr("lumen.api.audio.queries.owned_run_response", owned)
    image = AsyncMock(return_value={"created": 5, "data": [{"b64_json": "aW1hZ2U="}]})
    monkeypatch.setattr("lumen.api.compat.images.image_result", image)
    wav = b"RIFF-test-response-WAVE"

    async def audio_chunks():
        yield wav

    asset = AsyncMock(return_value=SimpleNamespace(size_bytes=len(wav), mime_type="audio/wav", chunks=audio_chunks))
    monkeypatch.setattr("lumen.api.audio.assets.open_download", asset)
    monkeypatch.setattr("lumen.api.audio.audio_result", AsyncMock(return_value={
        "kind": "tts", "asset_id": "owned-audio", "size_bytes": len(wav), "mime_type": "audio/wav",
    }))
    monkeypatch.setattr("lumen.services.embeddings.resolve", AsyncMock(return_value={"model": "embed"}))

    async def embed(*, api_key_id, required_scopes, **caller):
        check_actor(api_key_id, **caller)
        assert required_scopes == ("embeddings:write",)
        return {
            "model": "embed", "embeddings": [{"object": "embedding", "index": 0, "embedding": [0.25, 0.5]}],
            "usage": {"prompt_tokens": 2, "total_tokens": 2},
        }

    monkeypatch.setattr("lumen.services.embeddings.embed", embed)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = _header(raw)
        response = await client.post("/v1/images/generations", headers=headers, json={"model": "image", "prompt": "test"})
        assert response.status_code == 200 and response.json()["data"] == [{"b64_json": "aW1hZ2U="}]
        response = await client.post("/v1/audio/speech", headers=headers, json={
            "model": "tts", "input": "test", "voice": "alloy", "response_format": "wav",
        })
        assert response.status_code == 200 and response.content == wav and response.headers["content-type"] == "audio/wav"
        response = await client.post("/v1/embeddings", headers=headers, json={"model": "embed", "input": "test"})
        assert response.status_code == 200 and response.json()["data"][0]["embedding"] == [0.25, 0.5]
    owned.assert_awaited_with(run_id="completed-run", **actor)
    image.assert_awaited_once_with(run_id="completed-run", **actor)
    asset.assert_awaited_once_with(asset_id="owned-audio", **actor)


@pytest.mark.parametrize("kind", ["personal", "service"])
async def test_usage_remains_keystone_only(key_db, directory, monkeypatch, kind):
    _, raw = await _key(key_db, kind, ["models:read", "usage:read"])
    monkeypatch.setattr(auth, "validate_token", lambda *args, **kwargs: {
        "user_id": "creator", "project_id": "project-a", "roles": ["member"], "is_system_admin": False,
    })
    monkeypatch.setattr(auth, "resolve_project_authority", AsyncMock(return_value={
        "roles": ["member"], "is_system_admin": False, "domain_id": "domain-a",
    }))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for path in ("/v1/usage", "/v1/usage/timeseries", "/v1/usage/keys", "/v1/usage/records"):
            denied = await client.get(path, headers=_header(raw))
            assert denied.status_code == 403
            allowed = await client.get(path, headers={"X-Auth-Token": "dashboard"})
            assert allowed.status_code == 200, allowed.text


def test_openapi_documents_only_model_key_routes():
    paths = app.openapi()["paths"]
    for (method, route), scope in MODEL_API_KEY_ROUTE_SCOPES.items():
        if method != "WEBSOCKET":
            operation = paths[route][method.lower()]
            assert operation["x-model-api-key-allowed"] is True
            assert operation["x-required-api-key-scopes"] == [scope]
    assert not paths["/v1/chat/images/generations"]["post"]["x-model-api-key-allowed"]
    assert not paths["/v1/chat/realtime/sessions"]["post"]["x-model-api-key-allowed"]
    assert not paths["/v1/runs/{run_id}"]["get"]["x-model-api-key-allowed"]
