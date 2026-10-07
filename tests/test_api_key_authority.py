"""Keys attenuate current effective owner authority, never replace it."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request

from lumen import auth, service_authority
from lumen.api import api_keys
from lumen.services import api_key_store as aks
from lumen.services import claude_gateway as gateway

_RAW = "sk-afgl-lifecycle-test"
_CHAT = "compat:completions:write"


class KeySession:
    def __init__(self, row):
        self.row = row
        self.statements = []
        self.added = []
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def begin(self):
        return self

    async def execute(self, statement):
        self.statements.append(statement)
        return SimpleNamespace(scalar_one_or_none=lambda: self.row)

    async def get(self, model, key_id):
        return self.row if key_id == self.row.id else None

    def add(self, row):
        self.added.append(row)

    async def flush(self):
        for row in self.added:
            row.id = 7

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        return None


def key_row(**changes):
    fields = dict(
        id=7, owner_user_id="u", owner_project_id="p", is_active=True,
        revoked_at=None, expires_at=None, scopes=["models:read", _CHAT, "compat:images:write"],
        key_hash=aks._hash_key(_RAW), credential_kind="api_key", last_used_at=None,
    )
    fields.update(changes)
    return SimpleNamespace(**fields)


def stored_key(monkeypatch, **changes):
    session = KeySession(key_row(**changes))
    monkeypatch.setattr(aks, "is_db_available", lambda: True)
    monkeypatch.setattr(aks, "get_session_factory", lambda: lambda: session)
    monkeypatch.setattr(aks, "_require_db", lambda: lambda: session)
    return session


def current_roles(monkeypatch, roles, *, system_admin=False):
    lookup = AsyncMock(return_value={"roles": roles, "is_system_admin": system_admin})
    monkeypatch.setattr(auth, "resolve_project_authority", lookup)
    return lookup


def test_issuable_scope_vocabulary_matches_shared_service_contract():
    assert aks.API_KEY_SCOPES == frozenset(
        scope for scope in service_authority.SCOPE_CAPABILITIES if not scope.startswith("native:keys:"))
    assert {"native:conversations:delete", "native:assets:delete", "compat:files:delete",
            "native:agents:read", "native:agents:write", "native:agents:delete", "native:mcp:write"} <= aks.API_KEY_SCOPES


@pytest.mark.parametrize("roles", [
    ["member"], ["member", "lumen-chat_user"], ["reader", "lumen-keys_editor", "lumen-chat_user"],
    ["lumen-keys_editor", "lumen-chat_user"], ["admin", "manager", "lumen-keys_editor"],
    ["member", "project_owner", "project_admin"], ["member", "lumen_editor"],
])
async def test_every_issuer_requires_current_keys_editor(monkeypatch, roles):
    lookup = current_roles(monkeypatch, roles)
    for kind in ("api_key", "claude_gateway"):
        with pytest.raises(aks.ApiKeyForbidden):
            await aks.prepare_key_record("u", "p", "test", [_CHAT], credential_kind=kind)
    with pytest.raises(aks.ApiKeyForbidden):
        await aks.create_key("u", "p", "test", [_CHAT])
    assert lookup.await_count == 3
    lookup.assert_called_with("u", "p")


async def test_issuance_scopes_must_be_subset_of_current_owner_caps(monkeypatch):
    current_roles(monkeypatch, ["member", "lumen-keys_editor", "lumen-chat_user"])
    row, raw = await aks.prepare_key_record("u", "p", "chat only", [_CHAT])
    assert row.scopes == [_CHAT] and row.key_hash == aks._hash_key(raw)
    assert row.key_hash != raw
    for denied_scope in ("models:read", "compat:images:write", "native:assets:delete", "native:tools:execute"):
        with pytest.raises(aks.ApiKeyForbidden):
            await aks.prepare_key_record("u", "p", "overbroad", [_CHAT, denied_scope])


async def test_verified_system_owner_can_issue_service_scopes(monkeypatch):
    current_roles(monkeypatch, ["admin"], system_admin=True)
    row, _ = await aks.prepare_key_record("u", "p", "system", sorted(aks.API_KEY_SCOPES))
    assert set(row.scopes) == aks.API_KEY_SCOPES
    assert not any(scope.startswith("native:keys:") for scope in row.scopes)


async def test_verify_returns_current_roles_and_attenuates_downgraded_owner(monkeypatch):
    session = stored_key(monkeypatch)
    roles = ["member", "lumen-inventory_reader", "lumen-chat_user", "lumen-images_user"]
    lookup = current_roles(monkeypatch, roles)
    first = await aks.verify_key(_RAW)
    assert first["roles"] == roles
    assert set(first["scopes"]) == set(session.row.scopes)
    lookup.return_value = {"roles": ["member", "lumen-chat_user"], "is_system_admin": False}
    downgraded = await aks.verify_key(_RAW)
    assert downgraded["roles"] == ["member", "lumen-chat_user"]
    assert downgraded["scopes"] == (_CHAT,)
    assert session.row.scopes == ["models:read", _CHAT, "compat:images:write"]
    auth.ensure_scopes({"auth_type": "api_key", **downgraded}, _CHAT)
    with pytest.raises(HTTPException) as error:
        auth.ensure_scopes({"auth_type": "api_key", **downgraded}, "compat:images:write")
    assert error.value.status_code == 403
    lookup.return_value = {"roles": ["member"], "is_system_admin": False}
    assert await aks.verify_key(_RAW) is None


@pytest.mark.parametrize("changes", [
    {"is_active": False}, {"revoked_at": datetime.now(UTC)},
    {"expires_at": datetime.now(UTC) - timedelta(seconds=1)},
])
async def test_revoked_or_expired_key_never_authorizes(monkeypatch, changes):
    stored_key(monkeypatch, **changes)
    lookup = current_roles(monkeypatch, ["member", "lumen-chat_user"])
    assert await aks.verify_key(_RAW) is None
    lookup.assert_not_awaited()


@pytest.mark.parametrize("status", [403, 503])
async def test_missing_membership_differs_from_unavailable_keystone(monkeypatch, status):
    stored_key(monkeypatch)
    lookup = AsyncMock(side_effect=HTTPException(status_code=status, detail="authority lookup"))
    monkeypatch.setattr(auth, "resolve_project_authority", lookup)
    if status == 403:
        assert await aks.verify_key(_RAW) is None
    else:
        with pytest.raises(HTTPException) as error:
            await aks.verify_key(_RAW)
        assert error.value.status_code == 503
    expected = aks.ApiKeyForbidden if status == 403 else aks.ApiKeyAuthorityUnavailable
    with pytest.raises(expected):
        await aks.prepare_key_record("u", "p", "issue", [_CHAT])


async def test_current_group_effective_roles_are_used_without_direct_member_fallback(monkeypatch):
    stored_key(monkeypatch)
    names = ["member", "lumen-chat_user", "lumen-keys_editor"]
    roles = [SimpleNamespace(id=str(i), name=name, domain_id=None) for i, name in enumerate(names)]
    assignments = [SimpleNamespace(role={"id": role.id}, scope={"project": {"id": "p"}},
                                   group={"id": "group-1"}) for role in roles]
    calls = []

    def effective_assignments(**kwargs):
        calls.append(kwargs)
        assert kwargs == {"user": "u", "project": "p", "effective": True}
        return assignments

    ks = SimpleNamespace(role_assignments=SimpleNamespace(list=effective_assignments),
                         roles=SimpleNamespace(list=lambda: roles),
                         users=SimpleNamespace(get=lambda _id: SimpleNamespace(enabled=True)),
                         projects=SimpleNamespace(get=lambda _id: SimpleNamespace(enabled=True)),
                         inference_rules=SimpleNamespace(list_inference_roles=lambda: []))
    monkeypatch.setattr(auth, "_get_admin_ks_client", lambda: ks)
    info = await aks.verify_key(_RAW)
    assert set(info["roles"]) == set(names) and info["scopes"] == (_CHAT,)
    await aks.prepare_key_record("u", "p", "group issue", [_CHAT])
    assignments[:] = assignments[:1]  # Current group service grants revoked, member remains.
    assert await aks.verify_key(_RAW) is None
    assignments.clear()
    with pytest.raises(aks.ApiKeyForbidden):
        await aks.prepare_key_record("u", "p", "removed", [_CHAT])
    assert len(calls) == 4


@pytest.mark.parametrize("api_key_id", [None, 7])
async def test_before_new_io_key_and_web_revalidate_current_caps_without_keys_editor(monkeypatch, api_key_id):
    session = KeySession(key_row())
    lookup = current_roles(monkeypatch, ["member", "lumen-chat_user"])
    await aks.authorize_api_key_in_transaction(
        session, api_key_id=api_key_id, user_id="u", project_id="p", required_scopes=iter([_CHAT]))
    assert len(session.statements) == (1 if api_key_id else 0)
    if api_key_id:
        assert session.statements[0].get_execution_options()["populate_existing"] is True
    lookup.return_value = {"roles": ["reader", "lumen-inventory_reader", "lumen-history_reader"], "is_system_admin": False}
    with pytest.raises(aks.ApiKeyForbidden):
        await aks.authorize_api_key_in_transaction(
            session, api_key_id=api_key_id, user_id="u", project_id="p", required_scopes=(_CHAT,))
    lookup.side_effect = HTTPException(status_code=403, detail="removed")
    with pytest.raises(aks.ApiKeyForbidden):
        await aks.authorize_api_key_in_transaction(
            session, api_key_id=api_key_id, user_id="u", project_id="p", required_scopes=(_CHAT,))
    lookup.side_effect = HTTPException(status_code=503, detail="Keystone unavailable")
    with pytest.raises(aks.ApiKeyAuthorityUnavailable):
        await aks.authorize_api_key_in_transaction(
            session, api_key_id=api_key_id, user_id="u", project_id="p", required_scopes=(_CHAT,))


async def test_key_refresh_still_enforces_revocation_scope_and_owner(monkeypatch):
    current_roles(monkeypatch, ["member", "lumen-chat_user"])
    session = KeySession(key_row())
    for changes in ({"scopes": ["models:read"]}, {"revoked_at": datetime.now(UTC)},
                    {"owner_user_id": "someone-else"}, {"owner_project_id": "other"}):
        session.row = key_row(**changes)
        with pytest.raises(aks.ApiKeyForbidden):
            await aks.authorize_api_key_in_transaction(
                session, api_key_id=7, user_id="u", project_id="p", required_scopes=(_CHAT,))


async def test_system_owner_key_has_service_authority_not_platform_admin(monkeypatch):
    stored_key(monkeypatch)
    current_roles(monkeypatch, ["admin"], system_admin=True)
    request = Request({"type": "http", "method": "GET", "path": "/",
                       "headers": [(b"x-api-key", _RAW.encode())]})
    principal = await auth.get_principal(request)
    assert principal["roles"] == ["admin"]
    assert principal["service_system_admin"] is True
    assert principal["is_system_admin"] is False
    auth.ensure_scopes(principal, _CHAT)


@pytest.mark.parametrize("roles,expected", [
    (["member"], [403, 403, 403, 403, 403]),
    (["member", "lumen-chat_user", "lumen-images_user", "lumen-tools_user"], [403, 403, 403, 403, 403]),
    (["member", "lumen-keys_editor"], [200, 201, 200, 200, 403]),
    (["member", "lumen-keys_editor", "lumen-resources_admin"], [200, 201, 200, 200, 204]),
])
async def test_keys_editor_manages_resources_admin_revokes_preserving_owner_context(monkeypatch, roles, expected):
    test_app = FastAPI()
    test_app.include_router(api_keys.router)
    async def principal():
        return {"user_id": "u", "project_id": "p", "roles": roles, "is_system_admin": False}
    test_app.dependency_overrides[auth.get_token_info] = principal
    listing = AsyncMock(return_value=[])
    create = AsyncMock(return_value={"key": _RAW})
    rename = AsyncMock(return_value={"id": 7})
    limits = AsyncMock(return_value={"id": 7})
    revoke = AsyncMock()
    for name, fake in (("list_keys", listing), ("create_key", create), ("rename_key", rename),
                       ("update_owner_limits", limits), ("revoke_key", revoke)):
        monkeypatch.setattr(aks, name, fake)
    async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
        responses = [await client.get("/api-keys"),
                     await client.post("/api-keys", json={"scopes": [_CHAT]}),
                     await client.patch("/api-keys/7", json={"name": "renamed"}),
                     await client.patch("/api-keys/7/limits", json={"weekly_credit_limit": None}),
                     await client.delete("/api-keys/7")]
    assert [response.status_code for response in responses] == expected
    if expected[0] == 200:
        listing.assert_awaited_once_with("u", "p")
        rename.assert_awaited_once_with(7, "u", "p", "renamed")
    else:
        listing.assert_not_awaited()
        create.assert_not_awaited()
        rename.assert_not_awaited()
        limits.assert_not_awaited()
    if expected[-1] == 204:
        revoke.assert_awaited_once_with(7, "u", "p")
    else:
        revoke.assert_not_awaited()


async def test_resources_admin_cannot_revoke_another_owner_or_project(monkeypatch):
    session = stored_key(monkeypatch)
    test_app = FastAPI()
    test_app.include_router(api_keys.router)
    caller = {"user_id": "other", "project_id": "p", "roles": ["member", "lumen-resources_admin"],
              "is_system_admin": False}

    async def principal():
        return caller

    test_app.dependency_overrides[auth.get_token_info] = principal
    async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
        for user_id, project_id in (("other", "p"), ("u", "other")):
            caller.update(user_id=user_id, project_id=project_id)
            response = await client.delete("/api-keys/7")
            assert response.status_code == 403
            assert session.row.is_active is True
        caller.update(user_id="u", project_id="p")
        response = await client.delete("/api-keys/7")
        assert response.status_code == 204
    assert session.row.is_active is False and session.row.revoked_at is not None


@pytest.mark.parametrize("authority,status,code", [
    ({"roles": ["member", "lumen-inventory_reader", "lumen-chat_user"], "is_system_admin": False}, 403, "access_denied"),
    ({"roles": ["member", "lumen-keys_editor", "lumen-chat_user"], "is_system_admin": False}, 403, "access_denied"),
    (HTTPException(status_code=403, detail="membership removed"), 403, "access_denied"),
    (HTTPException(status_code=503, detail="unavailable"), 503, "temporarily_unavailable"),
])
async def test_gateway_mint_revalidates_authority_after_approval(monkeypatch, authority, status, code):
    now = datetime.now(UTC)
    grant = SimpleNamespace(
        client_id_hash=gateway._hash("claude-code"), expires_at=now + timedelta(minutes=5),
        status="approved", next_poll_at=now - timedelta(seconds=1), owner_user_id="u",
        owner_project_id="p", issued_api_key_id=None,
    )
    session = KeySession(grant)
    monkeypatch.setattr(gateway, "_factory", lambda: lambda: session)
    lookup = AsyncMock(side_effect=authority) if isinstance(authority, Exception) else AsyncMock(return_value=authority)
    monkeypatch.setattr(auth, "resolve_project_authority", lookup)
    with pytest.raises(gateway.GatewayError) as error:
        await gateway.exchange_device_code(
            device_code="dc_secret", grant_type=gateway.DEVICE_GRANT_TYPE, client_id="claude-code")
    assert (error.value.status_code, error.value.code) == (status, code)
    assert session.added == [] and grant.status == "approved" and grant.issued_api_key_id is None
